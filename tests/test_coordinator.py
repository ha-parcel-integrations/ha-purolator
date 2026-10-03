"""Tests for the Purolator coordinator: fetching, caching and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.purolator.api import PurolatorApiError
from custom_components.purolator.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    ParcelStatus,
)
from custom_components.purolator.coordinator import PurolatorCoordinator

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    in_transit_sample,
    registered_sample,
)

OTHER_CODE = "EXAMPLE888888"


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


def _in_transit(code: str = ACTIVE_CODE) -> dict:
    return in_transit_sample(code)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample() if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = PurolatorCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == ACTIVE_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # not found
    coordinator = PurolatorCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == OTHER_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates the cache

    client.async_get_parcel.side_effect = PurolatorApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_raises_when_every_parcel_fails(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = PurolatorApiError("HTTP 500")
    coordinator = PurolatorCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_reraises_unexpected_exceptions(hass):
    """Only API and network errors are tolerated; a bug must not be swallowed."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = PurolatorCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_a_tracking_code(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_tracking_number(hass):
    """An edge payload without a tracking number keeps the requested code."""
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    sample = active_sample()
    client = AsyncMock()
    client.async_get_parcel.return_value = sample
    coordinator = PurolatorCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == OTHER_CODE


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"trackingNumber": "GONE"}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert DELIVERED_CODE in coordinator._raw_cache


async def test_delivered_code_skipped_from_fetch(hass):
    """A delivered code stops being fetched from the next cycle on."""
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = (
        lambda code: active_sample(code) if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = PurolatorCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.call_count == 2
    assert coordinator.delivered_codes == {DELIVERED_CODE}

    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    # Only the still-active code is fetched — the delivered one is skipped.
    client.async_get_parcel.assert_called_once_with(ACTIVE_CODE)
    # The delivered parcel's sensor still keeps its data, from the cache.
    assert any(parcel["barcode"] == DELIVERED_CODE for parcel in coordinator.delivered)
    assert data[0]["barcode"] == ACTIVE_CODE


async def test_delivered_code_forgotten_when_untracked(hass):
    """Untracking a delivered code drops it from the skip set too."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {DELIVERED_CODE}

    hass.config_entries.async_update_entry(entry, options={CONF_PARCELS: []})
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == set()


async def test_update_fetches_parcels_concurrently(hass):
    """All tracked parcels go out in one gather, not one-by-one."""
    import asyncio

    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(code):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return active_sample(code)

    client = AsyncMock()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = PurolatorCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert peak == 2


async def test_cache_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    # Must still be active (not delivered) — a delivered code is skipped from
    # the fetch entirely from the next cycle on, which is covered separately
    # by test_delivered_code_skipped_from_fetch.
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = PurolatorCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = PurolatorApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = active_sample()  # out for delivery
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires nothing at all."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample(code) if code == ACTIVE_CODE else delivered_sample(code)
    )
    coordinator = PurolatorCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh seeds the state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: DELIVERED_CODE},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: active_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()  # first refresh: suppressed

    moved = active_sample()
    moved["shipment"][0]["package"][0]["estimatedDeliveryDate"] = "2026-01-02"
    client.async_get_parcel.return_value = moved
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_planned_from"] == "2026-01-01T00:00:00-05:00"
    assert events[0].data["new_planned_from"] == "2026-01-02T00:00:00-05:00"


async def test_losing_the_eta_is_silent(hass):
    """value -> null just means the carrier lost the window; not worth an alert."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()

    dropped = active_sample()
    dropped["shipment"][0]["package"][0]["estimatedDeliveryDate"] = None
    client.async_get_parcel.return_value = dropped
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


# ---------------------------------------------------------------------------
# outgoing parcels
# ---------------------------------------------------------------------------


def _registered(code: str = ACTIVE_CODE) -> dict:
    return registered_sample(code)


def _outgoing(code: str) -> dict:
    return {CONF_TRACKING_CODE: code, CONF_DIRECTION: DIRECTION_OUTGOING}


async def test_outgoing_codes_land_in_their_own_lists(hass):
    """Two identical payloads split purely on the list the code was filed in."""
    entry = _entry_with(
        [
            {CONF_TRACKING_CODE: ACTIVE_CODE, CONF_DIRECTION: DIRECTION_INCOMING},
            _outgoing(OTHER_CODE),
            _outgoing(DELIVERED_CODE),
        ]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        delivered_sample(code) if code == DELIVERED_CODE else active_sample(code)
    )
    coordinator = PurolatorCoordinator(hass, client, entry)

    active = await coordinator._async_update_data()

    assert [p["barcode"] for p in active] == [ACTIVE_CODE]
    assert [p["barcode"] for p in coordinator.outgoing] == [OTHER_CODE]
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [DELIVERED_CODE]
    assert coordinator.delivered == []


async def test_a_code_without_a_direction_reads_as_incoming(hass):
    """Entries stored before the option existed must not vanish from incoming."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    active = await coordinator._async_update_data()

    assert [p["barcode"] for p in active] == [ACTIVE_CODE]
    assert coordinator.outgoing == []


async def test_outgoing_parcel_keeps_polling_alive(hass):
    """A parcel the user sent is still in flight — it must hold a tier."""
    entry = _entry_with([_outgoing(ACTIVE_CODE)])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = in_transit_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    active = await coordinator._async_update_data()

    assert active == []
    assert [p["barcode"] for p in coordinator.outgoing] == [ACTIVE_CODE]
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES


async def test_fires_outgoing_status_changed_event(hass):
    entry = _entry_with([_outgoing(ACTIVE_CODE)])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    incoming_events = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: events.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: incoming_events.append(e)
    )

    client.async_get_parcel.return_value = _registered()
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.REGISTERED
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    # the incoming pair must stay silent for a parcel the user sent
    assert incoming_events == []


async def test_outgoing_delivery_fires_only_the_delivered_event(hass):
    entry = _entry_with([_outgoing(ACTIVE_CODE)])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: delivered.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE


async def test_no_registered_event_for_a_new_outgoing_parcel(hass):
    """Handing a parcel over is not news; only the incoming side announces one."""
    entry = _entry_with([_outgoing(ACTIVE_CODE)])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    for suffix in ("parcel_registered", "outgoing_parcel_registered"):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: events.append(e))

    client.async_get_parcel.side_effect = lambda code: _registered(code)
    await coordinator._async_update_data()
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [_outgoing(ACTIVE_CODE), _outgoing(OTHER_CODE)],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


async def test_an_unchanged_outgoing_status_is_silent(hass):
    entry = _entry_with([_outgoing(ACTIVE_CODE)])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = PurolatorCoordinator(hass, client, entry)

    events = []
    for suffix in ("outgoing_parcel_status_changed", "outgoing_parcel_delivered"):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: events.append(e))

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []
