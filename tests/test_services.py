"""Tests for the Purolator services (track_parcel / untrack_parcel)."""
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.purolator.const import (
    CONF_DIRECTION,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
)

from .payloads import active_sample

_SAMPLE = active_sample()



async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE999999"},
            blocking=True,
        )
        await hass.async_block_till_done()

    parcels = entry.options[CONF_PARCELS]
    assert parcels == [
        {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_track_parcel_normalizes_code(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "example-999 999"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_INCOMING}
    ]


async def test_track_parcel_rejects_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: ""}, blocking=True
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN,
                "track_parcel",
                {CONF_TRACKING_CODE: "EXAMPLE999999"},
                blocking=True,
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_untrack_parcel_removes_from_options(hass):
    entry = await _setup(
        hass, parcels=[{CONF_TRACKING_CODE: "EXAMPLE999999"}]
    )
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE999999"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(
        hass, parcels=[{CONF_TRACKING_CODE: "EXAMPLE999999"}]
    )
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE000000"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_track_parcel_records_the_requested_direction(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_OUTGOING},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_track_parcel_again_changes_its_direction(hass):
    """Re-tracking is how a parcel filed the wrong way gets corrected."""
    entry = await _setup(
        hass,
        parcels=[
            {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_INCOMING}
        ],
    )
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_OUTGOING},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: DIRECTION_OUTGOING}
    ]


async def test_track_parcel_rejects_an_unknown_direction(hass):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "EXAMPLE999999", CONF_DIRECTION: "sideways"},
            blocking=True,
        )


async def test_track_parcel_accepts_any_non_empty_code(hass):
    """A short or odd-shaped code is accepted; formats vary too much to gate on."""
    entry = await _setup(hass)
    with patch(
        "custom_components.purolator.api.PurolatorApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "abc"}, blocking=True
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS][0][CONF_TRACKING_CODE] == "ABC"


async def test_track_parcel_rejects_a_code_with_no_usable_characters(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "--"}, blocking=True
        )
