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

import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
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
    "9260": ParcelStatus.PROBLEM,
    "9550": ParcelStatus.AT_PICKUP_POINT,
    "9000": ParcelStatus.DELIVERED,
    "9500": ParcelStatus.DELIVERED,
}

# Shipment/package ``status.code`` once everything has been handed over.
_DELIVERED_STATUS_CODE = "DEL"

_LB_TO_KG = 0.45359237

# Event codes seen on the wire. The rest of the map comes from a third-party
# client and stays unconfirmed until a real parcel shows them.
_CONFIRMED_CODES = frozenset(
    {
        "3010", "2300", "2380", "7810", "0300", "4060", "7220", "1300",
        "7520", "7525", "4100", "4200", "9000",
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
    _LOGGER.warning(message + " Open an issue and paste this line: %s", *args, NEW_ISSUE_URL)


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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
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


def event_zone(event: Any) -> ZoneInfo | None:
    """Return the zone of the place an event happened, or ``None`` if unknown."""
    if not isinstance(event, dict):
        return None
    province = (event.get("location") or {}).get("provinceState")
    name = _PROVINCE_ZONES.get(str(province).strip().upper()) if province else None
    return ZoneInfo(name) if name else None


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
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
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
        parseable.append((parse_iso(timestamp), entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable]
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _first_dict(value: Any) -> dict:
    """Return the first element of a list when it is a dict, else ``{}``."""
    return _pick(value, 0)


def _pick(value: Any, index: Any) -> dict:
    """Return ``value[index]`` when it is a dict, else ``{}``."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        index = 0
    if isinstance(value, list) and index < len(value) and isinstance(value[index], dict):
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
        if code and code != _DELIVERED_STATUS_CODE:
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
    if (
        (package.get("status") or {}).get("code") == _DELIVERED_STATUS_CODE
        or (shipment.get("status") or {}).get("code") == _DELIVERED_STATUS_CODE
    ):
        status = ParcelStatus.DELIVERED
    else:
        status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        delivery_details = (package.get("details") or {}).get("deliveryDetails") or {}
        delivered_at = to_iso_timestamp(
            delivery_details.get("deliveryDateTime") or last_event.get("dateTime"),
            event_zone(last_event),
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
        "planned_from": None
        if delivered
        else to_iso_timestamp(
            package.get("estimatedDeliveryDate"), event_zone(last_event)
        ),
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
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
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
