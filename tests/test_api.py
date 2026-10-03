"""Tests for the Purolator API client."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.purolator.api import (
    PurolatorApiClient,
    PurolatorApiError,
)

from .payloads import delivered_sample

CODE = "TST000000001"
KEY = "test-key"


def _session_returning(
    status: int, body: object = None, headers: dict | None = None
) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


def _client(session: MagicMock) -> PurolatorApiClient:
    return PurolatorApiClient(session, KEY)


async def test_get_parcel_returns_full_response_and_sends_request_shape():
    session = _session_returning(200, delivered_sample(CODE))

    payload = await _client(session).async_get_parcel(CODE)

    assert payload["shipment"][0]["shipmentPin"] == CODE
    kwargs = session.post.call_args.kwargs
    assert kwargs["json"]["search"] == [
        {"trackingId": CODE, "sequenceId": 1, "eventSortOrder": "d"}
    ]
    assert kwargs["headers"]["x-api-key"] == KEY
    assert kwargs["headers"]["User-Agent"].startswith("Mozilla/")


async def test_semantic_miss_is_none_not_an_error():
    body = {"searchResult": [{"trackingId": CODE, "status": "NOT FOUND"}], "shipment": []}
    assert await _client(_session_returning(200, body)).async_get_parcel(CODE) is None


@pytest.mark.parametrize("status", [404, 410])
async def test_not_found_statuses_are_none(status):
    assert await _client(_session_returning(status)).async_get_parcel(CODE) is None


async def test_found_without_shipment_is_none_and_warns_once(caplog):
    body = {"searchResult": [{"status": "FOUND"}], "shipment": []}
    client = _client(_session_returning(200, body))
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_parcel(CODE) is None
        assert await client.async_get_parcel(CODE) is None
    assert len([r for r in caplog.records if "without shipment" in r.message]) == 1
    assert "unrecognised_status.yml" in caplog.text
    assert CODE not in caplog.text


async def test_403_is_a_blocked_failure_without_request_details_in_the_message():
    with pytest.raises(PurolatorApiError) as err:
        await _client(_session_returning(403, {"message": "Forbidden"})).async_get_parcel(CODE)
    assert err.value.status_code == 403
    assert err.value.backoff
    assert KEY not in str(err.value)


@pytest.mark.parametrize("status", [202, 405])
@pytest.mark.parametrize("action", ["captcha", "challenge"])
async def test_waf_challenge_is_a_blocked_failure(status, action):
    session = _session_returning(status, None, {"x-amzn-waf-action": action})
    with pytest.raises(PurolatorApiError) as err:
        await _client(session).async_get_parcel(CODE)
    assert err.value.backoff
    assert err.value.status_code == status


async def test_405_without_waf_header_is_a_plain_failure():
    with pytest.raises(PurolatorApiError) as err:
        await _client(_session_returning(405)).async_get_parcel(CODE)
    assert not err.value.backoff


async def test_429_carries_retry_after():
    session = _session_returning(429, None, {"Retry-After": "120"})
    with pytest.raises(PurolatorApiError) as err:
        await _client(session).async_get_parcel(CODE)
    assert err.value.backoff
    assert err.value.retry_after == 120


async def test_429_with_unparseable_retry_after():
    session = _session_returning(429, None, {"Retry-After": "Wed, 21 Oct 2026"})
    with pytest.raises(PurolatorApiError) as err:
        await _client(session).async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_other_error_status_does_not_back_off():
    with pytest.raises(PurolatorApiError) as err:
        await _client(_session_returning(500)).async_get_parcel(CODE)
    assert err.value.status_code == 500
    assert not err.value.backoff


async def test_unparseable_body_raises():
    with pytest.raises(PurolatorApiError):
        await _client(_session_returning(200, "not json")).async_get_parcel(CODE)


async def test_non_object_body_raises():
    with pytest.raises(PurolatorApiError):
        await _client(_session_returning(200, ["a"])).async_get_parcel(CODE)


@pytest.mark.parametrize("body", [{}, {"searchResult": []}, {"searchResult": [{"status": "WEIRD"}]}])
async def test_unexpected_search_result_raises_and_warns_once(body, caplog):
    client = _client(_session_returning(200, body))
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(PurolatorApiError):
                await client.async_get_parcel(CODE)
    warnings = [r for r in caplog.records if "unexpected search result" in r.message]
    assert len(warnings) == 1
    assert "unrecognised_status.yml" in warnings[0].getMessage()


async def test_network_error_propagates():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await _client(session).async_get_parcel(CODE)
