"""Purolator public tracking API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import API_KEY_HEADER, BROWSER_USER_AGENT, TRACKING_API_URL
from .parcels import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

# A challenge response is never answered, and a refused request is not retried
# by another route; both just back the coordinator off.
_WAF_HEADER = "x-amzn-waf-action"
_WAF_ACTIONS = {"captcha", "challenge"}
_WAF_STATUSES = {202, 405}

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log an unseen response shape once, with a copy-paste issue link."""
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message + " Open an issue and paste this line: %s", *args, NEW_ISSUE_URL)


class PurolatorApiError(Exception):
    """Raised when a Purolator API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        blocked: bool = False,
    ) -> None:
        """Store the status code, the ``Retry-After`` header and the block flag."""
        super().__init__(f"Purolator API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after
        self.blocked = blocked

    @property
    def backoff(self) -> bool:
        """Whether the whole poll should back off after this error."""
        return self.status_code == 429 or self.blocked


class PurolatorApiClient:
    """Client for the public Purolator tracking endpoint.

    One POST per tracking code. HTTP 200 carries
    ``searchResult[0].status`` of ``FOUND`` or ``NOT FOUND``.
    """

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        """Initialise the client."""
        self._session = session
        self._api_key = api_key

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the full response for a found parcel, or ``None`` when the
        endpoint reports the code as unknown. A refused request or a bot
        challenge raises :class:`PurolatorApiError` with ``blocked`` set;
        network errors propagate as ``aiohttp.ClientError``.
        """
        body = {
            "search": [
                {"trackingId": tracking_code, "sequenceId": 1, "eventSortOrder": "d"}
            ],
            "language": "en",
        }
        async with self._session.post(
            TRACKING_API_URL,
            json=body,
            headers={API_KEY_HEADER: self._api_key, "User-Agent": BROWSER_USER_AGENT},
        ) as response:
            status = response.status
            if status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds
                raise PurolatorApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if status in _WAF_STATUSES and (
                response.headers.get(_WAF_HEADER, "").lower() in _WAF_ACTIONS
            ):
                raise PurolatorApiError(
                    f"HTTP {status} (bot challenge)", status_code=status, blocked=True
                )
            if status == 403:
                raise PurolatorApiError(
                    "HTTP 403 (access refused)", status_code=403, blocked=True
                )
            if status in (404, 410):
                return None
            if status != 200:
                raise PurolatorApiError(f"HTTP {status}", status_code=status)
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PurolatorApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise PurolatorApiError("unexpected body (not a JSON object)")

        results = payload.get("searchResult")
        result = results[0] if isinstance(results, list) and results else None
        search_status = result.get("status") if isinstance(result, dict) else None
        if search_status == "NOT FOUND":
            return None
        if search_status != "FOUND":
            _warn_once(
                "search-status",
                "Purolator returned an unexpected search result "
                "(response keys: %s).",
                sorted(payload),
            )
            raise PurolatorApiError("unexpected search result")
        shipments = payload.get("shipment")
        if not isinstance(shipments, list) or not shipments:
            _warn_once(
                "hollow",
                "Purolator reported a parcel as found without shipment data "
                "(response keys: %s).",
                sorted(payload),
            )
            return None
        return payload
