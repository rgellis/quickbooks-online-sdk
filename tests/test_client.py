"""Client behaviour: paging, retry semantics, read-only enforcement, errors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from qbo.auth import AuthClient, MemoryTokenStore, TokenSet
from qbo.client import MAX_PAGE_SIZE, PRODUCTION_BASE_URL, QboClient
from qbo.errors import QboApiError, QboError, QboRateLimitError

REALM = "9130347"
BASE = f"{PRODUCTION_BASE_URL}/v3/company/{REALM}"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff is exercised for its control flow, not its wall-clock time."""

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("qbo.client.asyncio.sleep", instant)


def _client(**kwargs: Any) -> QboClient:
    tokens = TokenSet(
        access_token="good-access",
        refresh_token="refresh-v1",
        realm_id=REALM,
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=55),
    )
    auth = AuthClient(
        client_id="ABC", client_secret="shh", store=MemoryTokenStore(tokens)
    )
    return QboClient(realm_id=REALM, auth=auth, **kwargs)


def _page(entity: str, count: int, start: int = 1) -> dict[str, Any]:
    return {
        "QueryResponse": {
            entity: [
                {"Id": str(start + i), "DocNumber": f"D{start + i}"}
                for i in range(count)
            ],
            "startPosition": start,
            "maxResults": count,
        }
    }


def _requests(route: Any) -> list[httpx.Request]:
    """respx exposes recorded calls untyped; narrow once here."""
    return [cast(httpx.Request, call.request) for call in cast(list[Any], route.calls)]


def _params(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


class TestPaging:
    @respx.mock
    async def test_pages_past_the_thousand_row_cap(self) -> None:
        """The API caps every query at 1,000 rows whatever MAXRESULTS says."""
        pages = [_page("Invoice", MAX_PAGE_SIZE, 1), _page("Invoice", 250, 1001)]
        route = respx.get(f"{BASE}/query").mock(
            side_effect=[httpx.Response(200, json=p) for p in pages]
        )

        rows = await _client().query("SELECT * FROM Invoice")

        assert len(rows) == 1250
        assert route.call_count == 2
        first, second = (_params(r) for r in _requests(route))
        assert "STARTPOSITION 1 MAXRESULTS 1000" in first["query"][0]
        assert "STARTPOSITION 1001 MAXRESULTS 1000" in second["query"][0]

    @respx.mock
    async def test_a_short_first_page_ends_paging(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json=_page("Invoice", 7))
        )
        assert len(await _client().query("SELECT * FROM Invoice")) == 7
        assert route.call_count == 1

    @respx.mock
    async def test_exactly_one_full_page_probes_for_a_second(self) -> None:
        """1,000 rows is indistinguishable from 'more available', so a second
        request is required to learn the result set ended exactly on the cap."""
        route = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.Response(200, json=_page("Invoice", MAX_PAGE_SIZE, 1)),
                httpx.Response(200, json={"QueryResponse": {}}),
            ]
        )
        assert len(await _client().query("SELECT * FROM Invoice")) == MAX_PAGE_SIZE
        assert route.call_count == 2

    @respx.mock
    async def test_caller_supplied_paging_is_left_alone(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json=_page("Invoice", 5))
        )
        await _client().query("SELECT * FROM Invoice STARTPOSITION 50 MAXRESULTS 5")
        assert route.call_count == 1
        assert "STARTPOSITION 50" in _params(_requests(route)[0])["query"][0]

    @respx.mock
    async def test_limit_caps_collection_and_stops_early(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json=_page("Invoice", 10))
        )
        rows = await _client().query("SELECT * FROM Invoice", limit=10)
        assert len(rows) == 10
        assert "MAXRESULTS 10" in _params(_requests(route)[0])["query"][0]

    @respx.mock
    async def test_no_matches_returns_empty_list(self) -> None:
        """QuickBooks omits the entity key entirely rather than returning []."""
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {}})
        )
        assert await _client().query("SELECT * FROM Invoice WHERE Id = '0'") == []


class TestRetrySemantics:
    @respx.mock
    async def test_a_401_triggers_exactly_one_refresh_and_one_retry(self) -> None:
        refresh = respx.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "access-v2",
                    "refresh_token": "refresh-v2",
                    "expires_in": 3600,
                },
            )
        )
        api = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.Response(401, json={"fault": "AuthenticationFailed"}),
                httpx.Response(200, json=_page("Invoice", 2)),
            ]
        )

        rows = await _client().query("SELECT * FROM Invoice")

        assert len(rows) == 2
        assert api.call_count == 2, "exactly one retry"
        assert refresh.call_count == 1, "exactly one forced refresh"
        assert _requests(api)[1].headers["Authorization"] == "Bearer access-v2"

    @respx.mock
    async def test_a_second_401_gives_up(self) -> None:
        """Repeated 401s are an authorization problem, not a stale token.
        Retrying would only burn rotations."""
        respx.post("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "a2", "refresh_token": "r2", "expires_in": 3600},
            )
        )
        api = respx.get(f"{BASE}/query").mock(return_value=httpx.Response(401, json={}))

        with pytest.raises(QboApiError) as exc:
            await _client().query("SELECT * FROM Invoice")
        assert exc.value.status_code == 401
        assert api.call_count == 2

    @respx.mock
    async def test_429_backs_off_then_succeeds(self) -> None:
        api = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}, json={}),
                httpx.Response(200, json=_page("Invoice", 1)),
            ]
        )
        assert len(await _client().query("SELECT * FROM Invoice")) == 1
        assert api.call_count == 2

    @respx.mock
    async def test_persistent_429_raises_with_the_retry_hint(self) -> None:
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
        )
        with pytest.raises(QboRateLimitError) as exc:
            await _client(max_retries=2).query("SELECT * FROM Invoice")
        assert exc.value.retry_after == 30.0


class TestRequestShape:
    @respx.mock
    async def test_minorversion_is_always_sent(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json=_page("Invoice", 1))
        )
        await _client(minor_version=75).query("SELECT * FROM Invoice")
        assert _params(_requests(route)[0])["minorversion"] == ["75"]

    @respx.mock
    async def test_errors_carry_the_intuit_tid(self) -> None:
        """intuit_tid is the first thing Intuit support asks for."""
        respx.get(f"{BASE}/invoice/42").mock(
            return_value=httpx.Response(
                404, headers={"intuit_tid": "1-abc-def"}, json={"Fault": {}}
            )
        )
        with pytest.raises(QboApiError) as exc:
            await _client().get("Invoice", "42")
        assert exc.value.intuit_tid == "1-abc-def"
        assert "1-abc-def" in str(exc.value)


class TestWriteGuards:
    @respx.mock
    async def test_read_only_mode_refuses_writes_before_any_request(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(QboError, match="read-only mode"):
            await _client(read_only=True).create("Invoice", {"Line": []})
        assert not route.called, "must not reach the network"

    @respx.mock
    async def test_read_only_mode_still_permits_reads(self) -> None:
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json=_page("Invoice", 1))
        )
        assert len(await _client(read_only=True).query("SELECT * FROM Invoice")) == 1

    async def test_update_without_sync_token_is_rejected_locally(self) -> None:
        with pytest.raises(QboError, match="SyncToken"):
            await _client().update("Invoice", {"Id": "1"})

    async def test_update_without_id_is_rejected_locally(self) -> None:
        with pytest.raises(QboError, match="Id"):
            await _client().update("Invoice", {"SyncToken": "3"})

    async def test_batch_larger_than_thirty_is_rejected_locally(self) -> None:
        with pytest.raises(QboError, match="at most 30"):
            await _client().batch([{"bId": str(i)} for i in range(31)])

    def test_realm_id_is_required(self) -> None:
        auth = AuthClient(client_id="a", client_secret="b", store=MemoryTokenStore())
        with pytest.raises(QboError, match="realm_id"):
            QboClient(realm_id="", auth=auth)
