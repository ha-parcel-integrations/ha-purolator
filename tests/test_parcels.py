"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.purolator.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.purolator.parcels import (
    apply_delivered_filter,
    build_history,
    event_zone,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracked_direction,
)

from .payloads import active_sample, delivered_sample, event, pickup_sample

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("3010", ParcelStatus.REGISTERED),
        ("2380", ParcelStatus.IN_TRANSIT),
        ("2300", ParcelStatus.IN_TRANSIT),
        ("7520", ParcelStatus.IN_TRANSIT),
        ("4100", ParcelStatus.IN_TRANSIT),
        ("7810", ParcelStatus.IN_TRANSIT),
        ("0300", ParcelStatus.IN_TRANSIT),
        ("4060", ParcelStatus.IN_TRANSIT),
        ("7220", ParcelStatus.IN_TRANSIT),
        ("1300", ParcelStatus.IN_TRANSIT),
        ("7525", ParcelStatus.IN_TRANSIT),
        ("4200", ParcelStatus.OUT_FOR_DELIVERY),
        ("9260", ParcelStatus.PROBLEM),
        ("9550", ParcelStatus.AT_PICKUP_POINT),
        ("9000", ParcelStatus.DELIVERED),
        ("9500", ParcelStatus.DELIVERED),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("8888") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("9000") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("9999") == ParcelStatus.UNKNOWN
    assert map_parcel_status("9999") == ParcelStatus.UNKNOWN
    assert caplog.text.count("status=9999") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_attaches_the_configured_timezone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("America/Toronto"))
    try:
        assert (
            "2026-01-01T12:00:00-05:00" == "2026-01-01T12:00:00-05:00"
        )
        assert to_iso_timestamp("2026-01-01") == "2026-01-01T00:00:00-05:00"
    finally:
        dt_util.set_default_time_zone(previous)


def test_to_iso_timestamp_rejects_missing_and_garbage():
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp("") is None
    assert to_iso_timestamp("not-a-date") is None
    assert to_iso_timestamp("2026-13-45") is None


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def _events(sample: dict) -> list[dict]:
    return sample["shipment"][0]["package"][0]["events"]


def test_build_history_orders_oldest_to_newest():
    history = build_history(_events(delivered_sample()))
    assert len(history) == 4
    assert history[0]["raw_status"] == "Picked up by Purolator"
    assert history[0]["status"] == ParcelStatus.IN_TRANSIT
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    events = [
        event("1300", f"2026-04-{day:02d} 10:00:00", "moved") for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"code": "1300"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_drops_unparseable_timestamp():
    history = build_history(
        [
            event("3010", "2026-04-24 10:00:00", "fine"),
            event("1300", "not-a-date", "odd"),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["fine"]


def test_build_history_sorts_by_instant_not_provider_order():
    history = build_history(
        [
            event("1300", "2026-04-26 10:00:00", "later"),
            event("3010", "2026-04-24 10:00:00", "earlier"),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["earlier", "later"]


def test_build_history_falls_back_to_code_without_text():
    history = build_history([event("1300", "2026-04-24 10:00:00", "")])
    assert history[0]["raw_status"] == "1300"


def test_build_history_unmapped_code_keeps_null_status(caplog):
    history = build_history([event("9999", "2026-04-24 10:00:00", "New thing")])
    assert history[0]["status"] is None
    assert "status=9999" in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    """The docs site would have to pick one; "awaiting data" must not hide a confirmed field."""
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def _package(sample: dict) -> dict:
    return sample["shipment"][0]["package"][0]


def test_normalize_delivered_parcel():
    parcel = normalize_parcel({**delivered_sample(), "trackingNumber": "TST000000001"})
    assert parcel["carrier"] == "Purolator"
    assert parcel["barcode"] == "TST000000001"
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Shipment delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-01T12:00:00-05:00"
    # A delivered parcel drops its ETA once it has arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == "https://www.purolator.com/en/shipping/tracker?pin=TST000000001"
    assert parcel["weight"] == 1.134  # 2.5 lb
    assert parcel["dimensions"] is None
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_delivered_status_code_wins_over_an_unmapped_last_event():
    sample = delivered_sample()
    _package(sample)["events"][0]["code"] = "9999"
    parcel = normalize_parcel(sample)
    assert parcel["status"] == ParcelStatus.DELIVERED


def test_delivered_signal_can_come_from_the_shipment_status_alone():
    sample = delivered_sample()
    _package(sample)["status"] = {}
    assert normalize_parcel(sample)["status"] == ParcelStatus.DELIVERED


def test_mapped_delivery_event_without_status_code_still_delivers():
    sample = delivered_sample()
    _package(sample)["status"] = {}
    sample["shipment"][0]["status"] = {}
    assert normalize_parcel(sample)["status"] == ParcelStatus.DELIVERED


def test_unmapped_event_code_stays_unknown_and_never_delivers():
    sample = active_sample()
    _package(sample)["events"][0]["code"] = "9999"
    parcel = normalize_parcel(sample)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["raw_status"] == "On vehicle for delivery"


def test_delivered_at_falls_back_to_the_delivery_event_time():
    sample = delivered_sample()
    _package(sample)["details"] = {}
    parcel = normalize_parcel(sample)
    assert parcel["delivered_at"] == "2026-01-01T12:00:00-05:00"


def test_events_are_stored_with_the_configured_timezone():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("America/Toronto"))
    try:
        parcel = normalize_parcel(delivered_sample(), include_history=True)
    finally:
        dt_util.set_default_time_zone(previous)
    assert parcel["delivered_at"] == "2026-01-01T12:00:00-05:00"
    assert parcel["history"][-1]["timestamp"] == "2026-01-01T12:00:00-05:00"


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4
    assert parcel["history"][0]["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["history"][-1]["status"] == ParcelStatus.DELIVERED


def test_normalize_active_parcel_has_point_estimate():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-01-01T00:00:00-05:00"
    assert parcel["planned_to"] is None


def test_normalize_without_estimate_has_no_window():
    sample = active_sample()
    _package(sample)["estimatedDeliveryDate"] = None
    assert normalize_parcel(sample)["planned_from"] is None


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-found code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "TST000000009"})
    assert parcel["barcode"] == "TST000000009"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["history"] is None


def test_normalize_survives_null_histories():
    sample = active_sample()
    package = _package(sample)
    package["events"] = None
    package["lastEvent"] = None
    parcel = normalize_parcel(sample, include_history=True)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["history"] == []


def test_normalize_falls_back_to_last_event_field_without_events():
    sample = active_sample()
    package = _package(sample)
    package["events"] = []
    package["lastEvent"] = event("4200", "2026-01-01 09:00:00", "On vehicle for delivery")
    assert normalize_parcel(sample)["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_barcode_falls_back_to_package_then_shipment_pin():
    sample = active_sample("TST000000007")
    assert normalize_parcel(sample)["barcode"] == "TST000000007"
    del _package(sample)["pin"]
    assert normalize_parcel(sample)["barcode"] == "TST000000007"


def test_raw_status_falls_back_to_package_status_then_code():
    sample = active_sample()
    _package(sample)["events"][0]["description"] = ""
    assert normalize_parcel(sample)["raw_status"] == "In transit"
    _package(sample)["status"] = {}
    assert normalize_parcel(sample)["raw_status"] == "4200"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda shipment: shipment.update(pieceTotalCount=2),
        lambda shipment: shipment.update(pieceTotalCount=None),
        lambda shipment: shipment["details"]["weight"].update(unit="KG"),
        lambda shipment: shipment["details"]["weight"].update(value=None),
        lambda shipment: shipment["details"].pop("weight"),
        lambda shipment: shipment.pop("details"),
    ],
)
def test_weight_is_none_unless_single_piece_in_pounds(mutate):
    sample = delivered_sample()
    mutate(sample["shipment"][0])
    assert normalize_parcel(sample)["weight"] is None


def test_normalize_keeps_the_full_record_in_raw():
    sample = delivered_sample()
    sample["shipment"][0]["package"][0]["details"]["receiverName"] = "Jane Doe"
    raw = normalize_parcel(sample)["raw"]
    assert raw is sample
    assert raw["shipment"][0]["package"][0]["details"]["receiverName"] == "Jane Doe"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


# ---------------------------------------------------------------------------
# package selection and pre-1.0 warnings
# ---------------------------------------------------------------------------


def _two_package_response() -> dict:
    sample = delivered_sample("TST000000001")
    second = delivered_sample("TST000000002")["shipment"][0]["package"][0]
    sample["shipment"][0]["package"].append(second)
    sample["shipment"][0]["pieceTotalCount"] = 2
    return sample


def test_search_result_indices_pick_the_package():
    sample = _two_package_response()
    sample["searchResult"][0]["packageIndex"] = 1
    assert normalize_parcel(sample)["barcode"] == "TST000000002"


def test_search_result_indices_pick_the_shipment():
    sample = delivered_sample("TST000000001")
    other = delivered_sample("TST000000002")["shipment"][0]
    sample["shipment"].append(other)
    sample["searchResult"][0]["shipmentIndex"] = 1
    assert normalize_parcel(sample)["barcode"] == "TST000000002"


def test_multi_package_response_warns_once_without_identifiers(caplog):
    sample = _two_package_response()
    sample["searchResult"][0]["packageIndex"] = 1
    normalize_parcel(sample)
    normalize_parcel(sample)
    lines = [r.getMessage() for r in caplog.records if "package(s)" in r.getMessage()]
    assert len(lines) == 1
    assert "unrecognised_status.yml" in lines[0]
    assert "TST00000000" not in lines[0]


def test_single_package_response_does_not_warn(caplog):
    normalize_parcel(delivered_sample())
    assert "package(s)" not in caplog.text


@pytest.mark.parametrize("index", [-1, 7, "x", True, None])
def test_bad_indices_fall_back_to_the_first_package(index):
    sample = delivered_sample("TST000000001")
    sample["searchResult"][0]["packageIndex"] = index
    sample["searchResult"][0]["shipmentIndex"] = index
    parcel = normalize_parcel(sample)
    assert parcel["status"] in (ParcelStatus.DELIVERED, ParcelStatus.UNKNOWN)


def test_missing_search_result_still_normalizes():
    sample = delivered_sample()
    sample["searchResult"] = []
    assert normalize_parcel(sample)["status"] == ParcelStatus.DELIVERED
    sample["searchResult"] = ["junk"]
    assert normalize_parcel(sample)["status"] == ParcelStatus.DELIVERED


def test_unconfirmed_mapping_warns_once_per_code(caplog):
    assert map_parcel_status("9260") == ParcelStatus.PROBLEM
    assert map_event_status("9260") == ParcelStatus.PROBLEM
    lines = [r.getMessage() for r in caplog.records if "confirm this mapping" in r.getMessage()]
    assert len(lines) == 1
    assert "9260" in lines[0]
    assert "unrecognised_status.yml" in lines[0]


@pytest.mark.parametrize(
    "code",
    [
        "3010", "2300", "2380", "7810", "0300", "4060", "7220", "1300",
        "7520", "7525", "4100", "4200", "9000",
    ],
)
def test_wire_confirmed_codes_do_not_warn(code, caplog):
    map_parcel_status(code)
    assert "confirm this mapping" not in caplog.text


def test_unseen_package_status_code_warns_once_per_value(caplog):
    sample = active_sample()
    sample["shipment"][0]["package"][0]["status"]["code"] = "XYZ"
    sample["shipment"][0]["status"]["code"] = "XYZ"
    normalize_parcel(sample)
    normalize_parcel(sample)
    lines = [r.getMessage() for r in caplog.records if "package status code" in r.getMessage()]
    assert len(lines) == 1
    assert "XYZ" in lines[0]
    assert "unrecognised_status.yml" in lines[0]


def test_delivered_status_code_does_not_warn(caplog):
    normalize_parcel(delivered_sample())
    assert "package status code" not in caplog.text


def test_non_lb_weight_unit_warns_once_and_leaves_weight_empty(caplog):
    sample = delivered_sample()
    sample["shipment"][0]["details"]["weight"]["unit"] = "KG"
    assert normalize_parcel(sample)["weight"] is None
    normalize_parcel(sample)
    lines = [r.getMessage() for r in caplog.records if "weight unit" in r.getMessage()]
    assert len(lines) == 1
    assert "unrecognised_status.yml" in lines[0]


def test_tracked_direction_defaults_to_incoming():
    assert tracked_direction({"tracking_code": "X"}) == "incoming"
    assert tracked_direction({"tracking_code": "X", "direction": "outgoing"}) == "outgoing"


@pytest.mark.parametrize(
    ("province", "expected"),
    [
        ("BC", "2026-01-01T12:00:00-08:00"),
        ("ON", "2026-01-01T12:00:00-05:00"),
        ("NL", "2026-01-01T12:00:00-03:30"),
        ("SK", "2026-01-01T12:00:00-06:00"),
    ],
)
def test_event_time_uses_the_zone_of_the_scan_province(province, expected):
    scan = event("9000", "2026-01-01 12:00:00", "Delivered")
    scan["location"]["provinceState"] = province
    assert to_iso_timestamp(scan["dateTime"], event_zone(scan)) == expected


def test_unknown_or_missing_province_has_no_zone():
    assert event_zone({"location": {"provinceState": "ZZ"}}) is None
    assert event_zone({"location": {"city": "PUROLATOR"}}) is None
    assert event_zone({}) is None
    assert event_zone(None) is None


def test_history_scan_without_province_takes_the_first_known_zone():
    sample = delivered_sample()
    events = _package(sample)["events"]
    label = event("3010", "2025-12-30 08:00:00", "Label information")
    label["location"] = {"city": "PUROLATOR", "countryCode": "CA"}
    events.append(label)
    history = build_history(_events(sample))
    assert history[0]["raw_status"] == "Label information"
    assert history[0]["timestamp"] == "2025-12-30T08:00:00-05:00"
