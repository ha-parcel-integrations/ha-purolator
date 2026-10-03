"""Diagnostics support for the Purolator parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PurolatorConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# The tracking response is redacted by field name; receiver-style fields are
# listed even though the endpoint has not been seen to return them.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # carrier payload fields
    "trackingNumber",
    "trackingId",
    "pin",
    "shipmentPin",
    "recipient",
    "receiverName",
    "receiverAddress",
    "deliveryAddress",
    "address",
    "postalCode",
    "postal_code",
    "postcode",
    "city",
    "street",
    "email",
    "phone",
    "name",
    "driver",
    "deliveryDetails",
    "signature",
    "signedBy",
    "proofOfDelivery",
    "proofUrl",
    "podUrl",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PurolatorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Purolator config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
        "outgoing": async_redact_data(coordinator.outgoing or [], TO_REDACT),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], TO_REDACT
        ),
    }
