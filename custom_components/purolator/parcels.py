"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_DIRECTION,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-purolator/issues/new"
    "?template=unrecognised_status.yml"
)

# Keys are Purolator event codes. Unmapped codes surface as ``unknown`` plus a
# one-shot warning, which is how the map grows; return/exception codes have
# not been observed yet.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "3010": ParcelStatus.REGISTERED,
    "2300": ParcelStatus.IN_TRANSIT,
    "2380": ParcelStatus.IN_TRANSIT,
    "7810": ParcelStatus.IN_TRANSIT,
    "0300": ParcelStatus.IN_TRANSIT,
    "4060": ParcelStatus.IN_TRANSIT,
    "7220": ParcelStatus.IN_TRANSIT,
    "1300": ParcelStatus.IN_TRANSIT,
    "7520": ParcelStatus.IN_TRANSIT,
    "7525": ParcelStatus.IN_TRANSIT,
    "4100": ParcelStatus.IN_TRANSIT,
    "4200": ParcelStatus.OUT_FOR_DELIVERY,
    # Confirmed live 2026-10-17, each by its own event description.
    "3000": ParcelStatus.REGISTERED,  # shipper created a label
    "6720": ParcelStatus.IN_TRANSIT,  # scheduled for delivery next business day
    "0500": ParcelStatus.IN_TRANSIT,  # delayed in transit, missed connection
    "0510": ParcelStatus.IN_TRANSIT,  # delayed in transit, rail delay
    "6500": ParcelStatus.PROBLEM,  # address correction in progress
    "9230": ParcelStatus.PROBLEM,  # address correction, missing entry code
    "9260": ParcelStatus.PROBLEM,
    "9550": ParcelStatus.AT_PICKUP_POINT,
    "9000": ParcelStatus.DELIVERED,
    "9500": ParcelStatus.DELIVERED,
}

# Shipment/package ``status.code`` once everything has been handed over.
_DELIVERED_STATUS_CODE = "DEL"

# Other shipment/package ``status.code`` values seen live, so they stop asking
# to be reported. ``SDR`` is "Shipment Created" — a label, nothing moving yet.
_SEEN_STATUS_CODES = frozenset({_DELIVERED_STATUS_CODE, "SDR"})

_LB_TO_KG = 0.45359237

# Event codes seen on the wire. The rest of the map comes from a third-party
# client and stays unconfirmed until a real parcel shows them.
_CONFIRMED_CODES = frozenset(
    {
        "3010",
        "2300",
        "2380",
        "7810",
        "0300",
        "4060",
        "7220",
        "1300",
        "7520",
        "7525",
        "4100",
        "4200",
        "9000",
        # added by the 2026-10-17 sweep of six real parcels
        "3000",
        "6720",
        "0500",
        "0510",
        "6500",
        "9230",
        "9260",
        "9500",
        "9550",
    }
)

# Keys of everything already warned about, so each one is logged only once per
# HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Log a pre-1.0 unknown once, with a copy-paste issue link."""
    if key in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(key)
    _LOGGER.warning(
        "%s Open an issue and paste this line: %s",
        message % args,
        NEW_ISSUE_URL,
    )


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once."""
    _warn_once(
        code,
        "Unrecognised Purolator status, reported as 'unknown': status=%s.",
        code,
    )


def _lookup(code: str) -> ParcelStatus | None:
    """Return the mapping for ``code``, warning once if it is unconfirmed."""
    mapped = _STATUS_MAP.get(code)
    if mapped is not None and code not in _CONFIRMED_CODES:
        _warn_once(
            f"inferred:{code}",
            "Purolator status %s is mapped to '%s' but has not been confirmed "
            "on a real parcel - confirm this mapping.",
            code,
            mapped.value,
        )
    return mapped


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _lookup(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _lookup(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# Event times are wall-clock at the place of the scan; the province on the
# event is the only zone hint, so provinces with split zones get their
# majority zone.
_PROVINCE_ZONES = {
    "NL": "America/St_Johns",
    "NS": "America/Halifax",
    "NB": "America/Halifax",
    "PE": "America/Halifax",
    "QC": "America/Toronto",
    "ON": "America/Toronto",
    "MB": "America/Winnipeg",
    "SK": "America/Regina",
    "AB": "America/Edmonton",
    "BC": "America/Vancouver",
    "YT": "America/Whitehorse",
    "NT": "America/Yellowknife",
    "NU": "America/Iqaluit",
}


# Built once at import: constructing a ZoneInfo reads the tz file from disk.
_ZONES = {name: ZoneInfo(name) for name in set(_PROVINCE_ZONES.values())}


def event_zone(event: Any) -> ZoneInfo | None:
    """Return the zone of the place an event happened, or ``None`` if unknown."""
    if not isinstance(event, dict):
        return None
    province = (event.get("location") or {}).get("provinceState")
    name = _PROVINCE_ZONES.get(str(province).strip().upper()) if province else None
    return _ZONES[name] if name else None


def to_iso_timestamp(value: Any, zone: tzinfo | None = None) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Purolator stamps events in local wall time (``YYYY-MM-DD HH:MM:SS``, no
    offset). ``zone`` is the zone of the scan's province; without one Home
    Assistant's configured timezone is attached as an approximation. A bare
    date becomes midnight in that zone. Anything unparseable gives ``None``.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = (
            datetime.combine(date.fromisoformat(text), datetime.min.time())
            if len(text) == 10
            else datetime.fromisoformat(text)
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone or dt_util.get_default_time_zone())
    return parsed.isoformat()


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    # Events without a province (the label scan) take the first zone found.
    fallback_zone = next(
        (zone for zone in map(event_zone, events or []) if zone is not None), None
    )
    # The provider lists newest first; reversing keeps same-second ties in order.
    for event in reversed(events or []):
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(
            event.get("dateTime"), event_zone(event) or fallback_zone
        )
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("code")),
            "raw_status": event.get("description") or event.get("code"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            continue
        parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable]
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def _first_dict(value: Any) -> dict:
    """Return the first element of a list when it is a dict, else ``{}``."""
    return _pick(value, 0)


def _pick(value: Any, index: Any) -> dict:
    """Return ``value[index]`` when it is a dict, else ``{}``."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        index = 0
    if (
        isinstance(value, list)
        and index < len(value)
        and isinstance(value[index], dict)
    ):
        return value[index]
    return {}


def _select_package(raw: dict) -> tuple[dict, dict]:
    """Return the (shipment, package) the search result points at.

    Anything beyond a single shipment with a single package is unseen, so it
    warns once; counts and indices only, never identifiers.
    """
    results = raw.get("searchResult")
    result = results[0] if isinstance(results, list) and results else {}
    result = result if isinstance(result, dict) else {}
    shipment_index = result.get("shipmentIndex")
    package_index = result.get("packageIndex")
    shipments = raw.get("shipment")
    shipment = _pick(shipments, shipment_index)
    package = _pick(shipment.get("package"), package_index)

    shipment_count = len(shipments) if isinstance(shipments, list) else 0
    packages = shipment.get("package")
    package_count = len(packages) if isinstance(packages, list) else 0
    if (
        shipment_count > 1
        or package_count > 1
        or shipment_index not in (0, None)
        or package_index not in (0, None)
    ):
        _warn_once(
            f"multi:{shipment_count}:{package_count}:{shipment_index}:{package_index}",
            "Purolator returned %s shipment(s) and %s package(s) with indices "
            "%s/%s; using the indexed package.",
            shipment_count,
            package_count,
            shipment_index,
            package_index,
        )
    return shipment, package


def _warn_unseen_status_code(*containers: dict) -> None:
    """Warn once per shipment/package ``status.code`` other than the delivered one."""
    for container in containers:
        code = (container.get("status") or {}).get("code")
        if code and code not in _SEEN_STATUS_CODES:
            _warn_once(
                f"pkgstatus:{code}",
                "Purolator package status code %s has not been seen before "
                "- confirm how it should map.",
                code,
            )


def _weight_kg(shipment: dict) -> float | None:
    """Return the shipment weight in kg, for single-piece shipments only.

    The weight sits on the shipment, so for a multi-piece shipment it is not
    this package's weight.
    """
    if shipment.get("pieceTotalCount") != 1:
        return None
    weight = (shipment.get("details") or {}).get("weight") or {}
    value = weight.get("value")
    unit = weight.get("unit")
    if unit != "LB":
        if unit is not None:
            _warn_once(
                f"unit:{unit}",
                "Purolator reported a weight unit %s instead of LB; weight left empty.",
                unit,
            )
        return None
    if not isinstance(value, (int, float)):
        return None
    return round(value * _LB_TO_KG, 3)


def tracked_direction(item: dict) -> str:
    """Return the declared direction of one tracked-parcel options entry.

    Entries stored before the option existed carry no ``direction`` key.
    """
    return item.get(CONF_DIRECTION) or DEFAULT_DIRECTION


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``raw`` is the whole tracking response; the package its search result
    points at is the parcel. The keys of the returned dict are the contract —
    every carrier returns exactly these, in this order.
    """
    shipment, package = _select_package(raw)
    _warn_unseen_status_code(package, shipment)
    events = package.get("events") or []
    last_event = _first_dict(events) or package.get("lastEvent") or {}

    tracking_code = (
        raw.get("trackingNumber") or package.get("pin") or shipment.get("shipmentPin")
    )
    status_code = last_event.get("code")
    if (package.get("status") or {}).get("code") == _DELIVERED_STATUS_CODE or (
        shipment.get("status") or {}
    ).get("code") == _DELIVERED_STATUS_CODE:
        status = ParcelStatus.DELIVERED
    else:
        status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    zone = event_zone(last_event) or dt_util.get_default_time_zone()
    delivered_at = None
    if delivered:
        delivery_details = (package.get("details") or {}).get("deliveryDetails") or {}
        delivered_at = to_iso_timestamp(
            delivery_details.get("deliveryDateTime") or last_event.get("dateTime"),
            event_zone(last_event),
        )

    planned_from, planned_to = planned_for_status(
        status,
        *planned_window(package.get("estimatedDeliveryDate"), None, zone=zone),
    )
    pickup = is_pickup(
        None,
        status,
        (map_event_status(e.get("code")) for e in events if isinstance(e, dict)),
    )

    return {
        "carrier": "Purolator",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": last_event.get("description")
        or (package.get("status") or {}).get("description")
        or status_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": _weight_kg(shipment),
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
