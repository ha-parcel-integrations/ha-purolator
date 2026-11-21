"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.purolator.const import ParcelStatus
from custom_components.purolator.parcels import normalize_parcel, parse_iso

from .payloads import active_sample, delivered_sample, event, pickup_sample

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


def _package(sample: dict) -> dict:
    return sample["shipment"][0]["package"][0]


def _with_events(sample: dict, *events: dict) -> dict:
    _package(sample)["events"] = list(events)
    return sample


def _collected() -> dict:
    sample = pickup_sample()
    _package(sample)["estimatedDeliveryDate"] = "2026-01-01"
    _package(sample)["status"] = {"code": "DEL", "description": "Delivered"}
    return _with_events(
        sample,
        event("9500", "2026-01-02 16:00:00", "Collected"),
        event("9550", "2026-01-01 10:00:00", "Available for pickup"),
    )


def _at_pickup_with_estimate() -> dict:
    sample = pickup_sample()
    _package(sample)["estimatedDeliveryDate"] = "2026-01-01"
    return sample


def _in_transit_to_pickup() -> dict:
    """A parcel that reached a pickup point once and was then scanned onwards."""
    return _with_events(
        active_sample(),
        event("1300", "2026-01-02 10:00:00", "Departed sort facility"),
        event("9550", "2026-01-01 10:00:00", "Available for pickup"),
    )


def _problem() -> dict:
    return _with_events(active_sample(), event("9260", "2026-01-01 10:00:00", "x"))


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": _at_pickup_with_estimate,
    "collected": _collected,
    "problem": _problem,
    "bare": lambda: {"trackingNumber": "TST000000009"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_an_estimate_is_emptied_on_arrival_at_a_pickup_point():
    parcel = normalize_parcel(_at_pickup_with_estimate())
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-02T16:00:00-05:00"
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_pickup_survives_a_scan_after_the_pickup_point():
    parcel = normalize_parcel(_in_transit_to_pickup())
    assert parcel["status"] is not ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True


def test_a_home_delivery_is_not_pickup():
    assert normalize_parcel(delivered_sample())["pickup"] is False


def test_a_date_covers_the_whole_local_day():
    parcel = normalize_parcel(active_sample())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert start.utcoffset() == end.utcoffset()


def test_the_day_follows_the_zone_of_the_last_event():
    sample = _with_events(
        active_sample(),
        {**event("4200", "2026-01-01 09:00:00", "x")}
        | {"location": {"provinceState": "BC"}},
    )
    parcel = normalize_parcel(sample)
    assert parcel["planned_from"] == "2026-01-01T00:00:00-08:00"
    assert parcel["planned_to"] == "2026-01-01T23:59:59-08:00"


def test_a_moment_with_a_time_ends_at_the_end_of_that_day():
    sample = active_sample()
    _package(sample)["estimatedDeliveryDate"] = "2026-01-01 13:00:00"
    parcel = normalize_parcel(sample)
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert start.hour == 13
    assert (end.hour, end.minute, end.second) == (23, 59, 59)
    assert start.date() == end.date()
