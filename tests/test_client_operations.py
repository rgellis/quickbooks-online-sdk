"""Entity operations, typed accessors, lifecycle and the remaining error paths.

The paging and retry rules are covered in test_client.py. This covers what a
caller actually invokes -- create, update, delete, void, batch, cdc and the
model-returning variants -- plus the failure paths that only appear when
something goes wrong at the transport level.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from qbo.auth import AuthClient, MemoryTokenStore, TokenSet
from qbo.client import PRODUCTION_BASE_URL, QboClient
from qbo.errors import QboApiError, QboError, QboInvariantError
from qbo.models import Account, Invoice

REALM = "9130347"
BASE = f"{PRODUCTION_BASE_URL}/v3/company/{REALM}"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _requests(route: Any) -> list[httpx.Request]:
    return [cast(httpx.Request, call.request) for call in cast(list[Any], route.calls)]


class TestEntityOperations:
    @respx.mock
    async def test_create_posts_and_unwraps_the_entity(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(
                200, json={"Invoice": {"Id": "130"}, "time": "x"}
            )
        )
        created = await _client().create("Invoice", {"Line": []})
        assert created == {"Id": "130"}
        assert route.called

    @respx.mock
    async def test_update_requires_and_sends_the_sync_token(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(
                200, json={"Invoice": {"Id": "1", "SyncToken": "4"}}
            )
        )
        await _client().update("Invoice", {"Id": "1", "SyncToken": "3"})
        body = _requests(route)[0].content.decode()
        assert '"SyncToken": "3"' in body or '"SyncToken":"3"' in body

    @respx.mock
    async def test_sparse_update_marks_the_payload_sparse(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(200, json={"Invoice": {"Id": "1"}})
        )
        await _client().update(
            "Invoice", {"Id": "1", "SyncToken": "3", "DocNumber": "X"}, sparse=True
        )
        assert '"sparse": true' in _requests(route)[0].content.decode().replace(
            '"sparse":true', '"sparse": true'
        )

    @respx.mock
    async def test_delete_uses_the_operation_parameter(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(200, json={"Invoice": {"status": "Deleted"}})
        )
        result = await _client().delete("Invoice", {"Id": "1", "SyncToken": "3"})
        assert result == {"status": "Deleted"}
        query = parse_qs(urlparse(str(_requests(route)[0].url)).query)
        assert query["operation"] == ["delete"]

    @respx.mock
    async def test_void_uses_the_operation_parameter(self) -> None:
        route = respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(200, json={"Invoice": {"Id": "1"}})
        )
        await _client().void("Invoice", {"Id": "1", "SyncToken": "3"})
        query = parse_qs(urlparse(str(_requests(route)[0].url)).query)
        assert query["operation"] == ["void"]

    @respx.mock
    async def test_get_reads_one_entity(self) -> None:
        respx.get(f"{BASE}/invoice/130").mock(
            return_value=httpx.Response(200, json={"Invoice": {"Id": "130"}})
        )
        assert await _client().get("Invoice", "130") == {"Id": "130"}

    @respx.mock
    async def test_an_unrecognised_envelope_falls_back_to_the_whole_payload(
        self,
    ) -> None:
        """Rather than returning nothing when QuickBooks keys a response in a
        way this client does not anticipate."""
        respx.get(f"{BASE}/invoice/130").mock(
            return_value=httpx.Response(200, json={"Unexpected": {"Id": "130"}})
        )
        assert await _client().get("Invoice", "130") == {"Unexpected": {"Id": "130"}}

    @respx.mock
    async def test_batch_submits_and_returns_the_responses(self) -> None:
        route = respx.post(f"{BASE}/batch").mock(
            return_value=httpx.Response(
                200, json={"BatchItemResponse": [{"bId": "1"}, {"bId": "2"}]}
            )
        )
        result = await _client().batch([{"bId": "1"}, {"bId": "2"}])
        assert [r["bId"] for r in result] == ["1", "2"]
        assert '"BatchItemRequest"' in _requests(route)[0].content.decode()

    @respx.mock
    async def test_a_batch_response_without_items_returns_empty(self) -> None:
        respx.post(f"{BASE}/batch").mock(return_value=httpx.Response(200, json={}))
        assert await _client().batch([{"bId": "1"}]) == []

    @respx.mock
    async def test_cdc_passes_entities_and_timestamp(self) -> None:
        route = respx.get(f"{BASE}/cdc").mock(
            return_value=httpx.Response(200, json={"CDCResponse": []})
        )
        await _client().cdc(["Invoice", "Payment"], "2026-08-01T00:00:00-07:00")
        query = parse_qs(urlparse(str(_requests(route)[0].url)).query)
        assert query["entities"] == ["Invoice,Payment"]
        assert query["changedSince"] == ["2026-08-01T00:00:00-07:00"]


class TestTypedAccessors:
    @respx.mock
    async def test_get_as_parses_into_the_given_model(self) -> None:
        respx.get(f"{BASE}/invoice/130").mock(
            return_value=httpx.Response(
                200, json={"Invoice": {"Id": "130", "TotalAmt": 2780.92}}
            )
        )
        invoice = await _client().get_as(Invoice, "Invoice", "130")
        assert isinstance(invoice, Invoice)
        assert invoice.TotalAmt == Decimal("2780.92")

    @respx.mock
    async def test_get_model_looks_the_model_up_by_entity_name(self) -> None:
        respx.get(f"{BASE}/account/1004").mock(
            return_value=httpx.Response(
                200, json={"Account": {"Id": "1004", "Name": "Stripe Clearing"}}
            )
        )
        account = await _client().get_model("Account", "1004")
        assert isinstance(account, Account)
        assert account.Name == "Stripe Clearing"

    @respx.mock
    async def test_query_as_parses_every_row(self) -> None:
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "QueryResponse": {
                        "Account": [
                            {"Id": "1", "Name": "Operating", "AcctNum": "1000"},
                            {"Id": "2", "Name": "Receivable", "AcctNum": "1002"},
                        ]
                    }
                },
            )
        )
        rows = await _client().query_as(Account, "SELECT * FROM Account")
        assert [r.AcctNum for r in rows] == ["1000", "1002"]

    @respx.mock
    async def test_query_models_defaults_to_selecting_everything(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(
                200, json={"QueryResponse": {"Account": [{"Id": "1"}]}}
            )
        )
        rows = await _client().query_models("Account")
        assert len(rows) == 1
        query = parse_qs(urlparse(str(_requests(route)[0].url)).query)["query"][0]
        assert query.startswith("SELECT * FROM Account")

    @respx.mock
    async def test_report_as_validates_by_default(self) -> None:
        """A self-contradicting report must raise, not be returned."""
        respx.get(f"{BASE}/reports/ProfitAndLoss").mock(
            return_value=httpx.Response(
                200,
                json={
                    "Header": {"ReportName": "ProfitAndLoss"},
                    "Columns": {
                        "Column": [
                            {"ColTitle": "", "ColType": "Account"},
                            {"ColTitle": "Total", "ColType": "Money"},
                        ]
                    },
                    "Rows": {
                        "Row": [
                            {
                                "Header": {
                                    "ColData": [{"value": "Expenses"}, {"value": ""}]
                                },
                                "group": "Expenses",
                                "Summary": {
                                    "ColData": [
                                        {"value": "Total Expenses"},
                                        {"value": "0.00"},
                                    ]
                                },
                                "Rows": {
                                    "Row": [
                                        {
                                            "ColData": [
                                                {"value": "Contract Labor"},
                                                {"value": "37551.89"},
                                            ],
                                            "type": "Data",
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                },
            )
        )
        with pytest.raises(QboInvariantError, match="37,551.89"):
            await _client().report_as("ProfitAndLoss")

    @respx.mock
    async def test_report_as_can_skip_validation_deliberately(self) -> None:
        respx.get(f"{BASE}/reports/BalanceSheet").mock(
            return_value=httpx.Response(
                200, json={"Header": {"ReportName": "BalanceSheet"}}
            )
        )
        report = await _client().report_as("BalanceSheet", validate=False)
        assert report.name == "BalanceSheet"

    @respx.mock
    async def test_report_resolves_a_documented_name_to_its_route(self) -> None:
        route = respx.get(f"{BASE}/reports/AgedPayableDetail").mock(
            return_value=httpx.Response(200, json={"Header": {}})
        )
        await _client().report("APAgingDetail")
        assert route.called, "APAgingDetail must be fetched from AgedPayableDetail"


class TestLifecycle:
    @respx.mock
    async def test_async_context_manager_closes_the_client(self) -> None:
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {}})
        )
        async with _client() as client:
            assert await client.query("SELECT * FROM Invoice") == []
        assert client._http is None  # pyright: ignore[reportPrivateUsage]

    async def test_closing_twice_is_harmless(self) -> None:
        client = _client()
        await client.aclose()
        await client.aclose()

    @respx.mock
    async def test_a_supplied_http_client_is_not_closed(self) -> None:
        """Closing a client someone else owns would break their next request."""
        respx.get(f"{BASE}/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {}})
        )
        async with httpx.AsyncClient() as shared:
            client = _client(http=shared)
            await client.query("SELECT * FROM Invoice")
            await client.aclose()
            assert not shared.is_closed


class TestTransportFailures:
    @respx.mock
    async def test_a_network_error_is_retried(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.ConnectError("connection reset"),
                httpx.Response(200, json={"QueryResponse": {"Invoice": [{"Id": "1"}]}}),
            ]
        )
        assert len(await _client().query("SELECT * FROM Invoice")) == 1
        assert route.call_count == 2

    @respx.mock
    async def test_exhausted_retries_report_what_was_attempted(self) -> None:
        respx.get(f"{BASE}/query").mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(QboError, match="failed after") as exc:
            await _client(max_retries=2).query("SELECT * FROM Invoice")
        assert "GET" in str(exc.value)

    @respx.mock
    async def test_a_non_json_error_body_is_still_reported(self) -> None:
        respx.get(f"{BASE}/invoice/1").mock(
            return_value=httpx.Response(500, text="<html>Gateway error</html>")
        )
        with pytest.raises(QboApiError) as exc:
            await _client(max_retries=0).get("Invoice", "1")
        assert "Gateway error" in str(exc.value)

    @respx.mock
    async def test_an_empty_success_body_returns_an_empty_mapping(self) -> None:
        respx.post(f"{BASE}/invoice").mock(
            return_value=httpx.Response(200, content=b"")
        )
        assert await _client().create("Invoice", {}) == {}

    @respx.mock
    async def test_a_non_object_json_response_is_wrapped(self) -> None:
        respx.get(f"{BASE}/invoice/1").mock(
            return_value=httpx.Response(200, json=[1, 2])
        )
        assert await _client().get("Invoice", "1") == {"value": [1, 2]}

    @respx.mock
    async def test_backoff_without_a_retry_after_header_still_retries(self) -> None:
        """Exercises the jittered path rather than the server's own hint."""
        route = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(200, json={"QueryResponse": {"Invoice": [{"Id": "1"}]}}),
            ]
        )
        assert len(await _client().query("SELECT * FROM Invoice")) == 1
        assert route.call_count == 2

    @respx.mock
    async def test_a_non_numeric_retry_after_falls_back_to_jitter(self) -> None:
        route = respx.get(f"{BASE}/query").mock(
            side_effect=[
                httpx.Response(
                    429,
                    headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                    json={},
                ),
                httpx.Response(200, json={"QueryResponse": {}}),
            ]
        )
        assert await _client().query("SELECT * FROM Invoice") == []
        assert route.call_count == 2


class TestAttachmentUpload:
    @respx.mock
    async def test_raw_content_sets_an_explicit_content_type(self) -> None:
        route = respx.post(f"{BASE}/upload").mock(
            return_value=httpx.Response(200, json={"AttachableResponse": []})
        )
        await _client().request(
            "POST", "upload", content=b"binary", content_type="multipart/form-data"
        )
        assert _requests(route)[0].headers["Content-Type"] == "multipart/form-data"

    @respx.mock
    async def test_raw_content_without_a_type_uses_the_api_default(self) -> None:
        route = respx.post(f"{BASE}/upload").mock(
            return_value=httpx.Response(200, json={})
        )
        await _client().request("POST", "upload", content=b"binary")
        assert _requests(route)[0].headers["Content-Type"] == "application/text"
