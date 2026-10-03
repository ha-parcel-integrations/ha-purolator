"""Tests for Purolator diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.purolator.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .payloads import DELIVERED_CODE, delivered_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EXAMPLE123456"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "trackingNumber": "EXAMPLE123456",
                "recipient": "Jane Doe",
                "deliveryAddress": {"city": "Rotterdam", "street": "Coolsingel 1"},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = [{"barcode": "OUT1", "raw": {"pin": "OUT1"}}]
    entry.runtime_data.coordinator.delivered_outgoing = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["outgoing"][0]["barcode"] == "**REDACTED**"
    assert result["outgoing"][0]["raw"]["pin"] == "**REDACTED**"
    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 1,
        "outgoing_delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipient"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryAddress"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_redacts_the_real_payload_shape(hass):
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = 30
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=30)
    sample = delivered_sample()
    sample["shipment"][0]["package"][0]["details"]["deliveryDetails"].update(
        signature="Jane Doe",
        proofUrl="https://example.invalid/pod",
        someNewMember="Unlisted Person",
    )
    entry.runtime_data.coordinator.data = [{"raw": sample}]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    text = str(result)
    assert DELIVERED_CODE not in text
    assert "Jane Doe" not in text
    assert "example.invalid" not in text
    assert "Unlisted Person" not in text
    raw = result["incoming"][0]["raw"]
    assert raw["searchResult"][0]["trackingId"] == "**REDACTED**"
    assert raw["shipment"][0]["shipmentPin"] == "**REDACTED**"
    assert raw["shipment"][0]["package"][0]["pin"] == "**REDACTED**"
    assert raw["shipment"][0]["package"][0]["events"][0]["code"] == "9000"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
