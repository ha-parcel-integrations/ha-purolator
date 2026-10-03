"""Synthetic Purolator tracking responses shared by the test modules.

Built from the documented response shape with made-up tracking codes; no real
parcel data.
"""
from __future__ import annotations

ACTIVE_CODE = "TST000000002"
DELIVERED_CODE = "TST000000001"


def event(code: str, date_time: str | None, description: str, **extra) -> dict:
    """One entry of the carrier's own event timeline."""
    return {
        "dateTime": date_time,
        "code": code,
        "description": description,
        "terminal": "TERM",
        "location": {"city": "Testville", "provinceState": "ON", "countryCode": "CA"},
        **extra,
    }


def _response(
    code: str,
    events: list[dict],
    *,
    status_code: str,
    status_description: str,
    estimated: str | None,
    delivered_at: str | None,
    weight_lb: float | None = 2.5,
    pieces: int = 1,
) -> dict:
    last = events[0] if events else None
    details: dict = {"receiverCountryCode": "CA"}
    if weight_lb is not None:
        details["weight"] = {"unit": "LB", "value": weight_lb}
    package: dict = {
        "pin": code,
        "status": {"code": status_code, "description": status_description},
        "estimatedDeliveryDate": estimated,
        "lastEvent": last,
        "details": {
            "deliveryDetails": {"deliveryDateTime": delivered_at}
            if delivered_at
            else {}
        },
        "events": events,
    }
    return {
        "searchResult": [
            {
                "trackingId": code,
                "status": "FOUND",
                "type": "PIN",
                "shipmentIndex": 0,
                "packageIndex": 0,
            }
        ],
        "shipment": [
            {
                "shipmentPin": code,
                "status": {"code": status_code, "description": status_description},
                "shipmentCreated": "2025-12-30",
                "pieceTotalCount": pieces,
                "details": details,
                "package": [package],
            }
        ],
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative tracking response for a delivered parcel."""
    return _response(
        code,
        [
            event("9000", "2026-01-01 12:00:00", "Shipment delivered", reasonCode="OSNR"),
            event("4200", "2026-01-01 09:00:00", "On vehicle for delivery"),
            event("1300", "2025-12-31 15:00:00", "Departed sort facility"),
            event("2380", "2025-12-30 08:00:00", "Picked up by Purolator"),
        ],
        status_code="DEL",
        status_description="Delivered",
        estimated="2026-01-01",
        delivered_at="2026-01-01 12:00:00",
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an estimated delivery date."""
    sample = _response(
        code,
        [
            event("4200", "2026-01-01 09:00:00", "On vehicle for delivery"),
            event("1300", "2025-12-31 15:00:00", "Departed sort facility"),
            event("2380", "2025-12-30 08:00:00", "Picked up by Purolator"),
        ],
        status_code="INT",
        status_description="In transit",
        estimated="2026-01-01",
        delivered_at=None,
    )
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel waiting for collection."""
    return _response(
        code,
        [event("9550", "2026-01-01 10:00:00", "Available for pickup for 5 business days")],
        status_code="INT",
        status_description="In transit",
        estimated=None,
        delivered_at=None,
    )


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel that has left a sort facility but is not yet on a vehicle."""
    return _response(
        code,
        [
            event("1300", "2025-12-31 15:00:00", "Departed sort facility"),
            event("2380", "2025-12-30 08:00:00", "Picked up by Purolator"),
        ],
        status_code="INT",
        status_description="In transit",
        estimated="2026-01-01",
        delivered_at=None,
    )


def registered_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel whose label exists but has not been picked up."""
    return _response(
        code,
        [event("3010", "2026-01-01 08:00:00", "Label information electronically submitted")],
        status_code="INT",
        status_description="In transit",
        estimated=None,
        delivered_at=None,
    )
