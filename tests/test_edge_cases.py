"""The paths that only run when something has already gone wrong.

Error handling is the part of a client least likely to be exercised by hand and
most likely to matter: these are the branches that decide whether a failure
arrives as a readable message or as a traceback from three frames deep. They are
covered deliberately rather than incidentally.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from qbo.auth import (
    TOKEN_ENDPOINT,
    AuthClient,
    FileTokenStore,
    MemoryTokenStore,
    TokenSet,
)
from qbo.entities import ENTITIES, REPORTS, entity_names, report_names
from qbo.errors import QboApiError, QboAuthError
from qbo.reports import (
    assert_balance_sheet_balances,
    assert_columns_sum_to_total,
    parse_money,
    parse_report,
)


def _expired() -> TokenSet:
    return TokenSet(
        access_token="stale",
        refresh_token="refresh-v1",
        realm_id="9130347",
        access_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


def _auth(store: Any) -> AuthClient:
    return AuthClient(client_id="ABC", client_secret="shh", store=store)


class TestTokenValidity:
    def test_an_empty_access_token_is_never_valid(self) -> None:
        """The seeded store written at deploy time has no access token, which
        must force a refresh on the first call rather than sending an empty
        bearer header."""
        seeded = TokenSet(access_token="", refresh_token="r", realm_id="1")
        assert seeded.access_token_valid is False

    def test_an_access_token_with_no_expiry_is_not_trusted(self) -> None:
        assert TokenSet(access_token="a", refresh_token="r").access_token_valid is False

    def test_a_token_expiring_inside_the_skew_is_not_valid(self) -> None:
        """Refreshing early avoids a request setting off with a token that dies
        in flight."""
        soon = TokenSet(
            access_token="a",
            refresh_token="r",
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        assert soon.access_token_valid is False


class TestTokenRefreshFailures:
    @respx.mock
    async def test_a_network_failure_reaching_intuit_is_reported(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("dns"))
        with pytest.raises(QboAuthError, match="could not reach Intuit"):
            await _auth(MemoryTokenStore(_expired())).access_token()

    @respx.mock
    async def test_a_server_error_is_not_mistaken_for_a_dead_token(self) -> None:
        """A 500 means try again later; telling someone to re-authorize would
        send them through the Playground for nothing."""
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(500, json={"error": "server_error"})
        )
        with pytest.raises(QboAuthError, match="HTTP 500") as exc:
            await _auth(MemoryTokenStore(_expired())).access_token()
        assert "re-authorize" not in str(exc.value)

    @respx.mock
    async def test_a_non_json_token_response_is_handled(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(502, text="<html>bad gateway</html>")
        )
        with pytest.raises(QboAuthError):
            await _auth(MemoryTokenStore(_expired())).access_token()

    @respx.mock
    async def test_a_200_without_an_access_token_is_rejected(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"refresh_token": "r2"})
        )
        with pytest.raises(QboAuthError, match="no access_token"):
            await _auth(MemoryTokenStore(_expired())).access_token()

    @respx.mock
    async def test_the_auth_client_creates_and_closes_its_own_transport(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "a2", "refresh_token": "r2", "expires_in": 3600},
            )
        )
        auth = _auth(MemoryTokenStore(_expired()))
        assert await auth.access_token() == "a2"
        await auth.aclose()
        await auth.aclose()


class TestFileStoreFailures:
    async def test_a_failed_rename_leaves_no_partial_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash mid-write must not leave a truncated store behind: the next
        start would read it, find no usable token, and the credential would be
        gone."""
        store = FileTokenStore(tmp_path / "tokens.json")

        def explode(_src: Any, _dst: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", explode)
        with pytest.raises(OSError, match="disk full"):
            await store.save(_expired())

        leftovers = [
            p.name for p in tmp_path.iterdir() if p.name.startswith(".qbo-token-")
        ]
        assert leftovers == [], f"partial files left behind: {leftovers}"
        assert not (tmp_path / "tokens.json").exists()


class TestErrorMessages:
    def test_an_api_error_without_a_body_still_reads_clearly(self) -> None:
        error = QboApiError(
            status_code=503, method="GET", url="https://example.invalid/v3/x"
        )
        message = str(error)
        assert "GET" in message
        assert "HTTP 503" in message
        assert "Response:" not in message

    def test_an_api_error_with_a_body_includes_it(self) -> None:
        error = QboApiError(
            status_code=400,
            method="POST",
            url="https://example.invalid/v3/x",
            body={"Fault": {"type": "ValidationFault"}},
        )
        assert "ValidationFault" in str(error)

    def test_a_url_carrying_a_token_is_redacted(self) -> None:
        error = QboApiError(
            status_code=400,
            method="GET",
            url="https://example.invalid/v3/x?access_token=supersecretvalue",
        )
        assert "supersecretvalue" not in str(error)


class TestRegistryHelpers:
    def test_entity_names_matches_the_registry(self) -> None:
        assert entity_names() == frozenset(ENTITIES)
        assert "Invoice" in entity_names()

    def test_report_names_matches_the_registry(self) -> None:
        assert report_names() == frozenset(REPORTS)
        assert "ProfitAndLoss" in report_names()


class TestReportParsingEdges:
    def test_a_malformed_number_is_treated_as_absent(self) -> None:
        assert parse_money("1.2.3") is None
        assert parse_money("--5") is None

    def test_columns_that_are_not_objects_are_skipped(self) -> None:
        report = parse_report(
            {
                "Columns": {
                    "Column": [
                        "not a column",
                        None,
                        {"ColTitle": "Total", "ColType": "Money"},
                    ]
                }
            }
        )
        assert [c.title for c in report.columns] == ["Total"]

    def test_a_column_without_a_colkey_falls_back_to_its_title(self) -> None:
        report = parse_report(
            {
                "Columns": {
                    "Column": [
                        {
                            "ColTitle": "Total",
                            "ColType": "Money",
                            "MetaData": [{"Name": "Other", "Value": "x"}],
                        }
                    ]
                }
            }
        )
        assert report.columns[0].key == "total"

    def test_a_column_with_neither_key_nor_title_gets_a_positional_key(self) -> None:
        report = parse_report({"Columns": {"Column": [{"ColType": "Money"}]}})
        assert report.columns[0].key == "col0"

    def test_rows_that_are_not_objects_are_ignored(self) -> None:
        assert parse_report({"Rows": "not rows"}).rows == ()

    def test_a_section_without_a_summary_yields_only_its_children(self) -> None:
        report = parse_report(
            {
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
                                "ColData": [{"value": "Ungrouped"}, {"value": ""}]
                            },
                            "Rows": {
                                "Row": [
                                    {
                                        "ColData": [
                                            {"value": "Checking"},
                                            {"value": "10.00"},
                                        ],
                                        "type": "Data",
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        )
        assert [r.label for r in report.rows] == ["Checking"]
        assert report.rows[0].value("total") == Decimal("10.00")


class TestInvariantEdges:
    def test_a_report_without_a_period_omits_it_from_the_message(self) -> None:
        report = parse_report(
            {
                "Header": {"ReportName": "BalanceSheet"},
                "Columns": {
                    "Column": [
                        {"ColTitle": "", "ColType": "Account"},
                        {"ColTitle": "Total", "ColType": "Money"},
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "group": "TotalAssets",
                            "Header": {"ColData": [{"value": "ASSETS"}, {"value": ""}]},
                            "Summary": {
                                "ColData": [
                                    {"value": "TOTAL ASSETS"},
                                    {"value": "100.00"},
                                ]
                            },
                        },
                        {
                            "group": "TotalLiabilitiesAndEquity",
                            "Header": {"ColData": [{"value": "L+E"}, {"value": ""}]},
                            "Summary": {
                                "ColData": [{"value": "TOTAL L+E"}, {"value": "999.00"}]
                            },
                        },
                    ]
                },
            }
        )
        with pytest.raises(Exception) as exc:
            assert_balance_sheet_balances(report)
        assert " for " not in str(exc.value)

    def test_a_balance_sheet_without_value_columns_is_skipped(self) -> None:
        report = parse_report(
            {
                "Columns": {"Column": [{"ColTitle": "", "ColType": "Account"}]},
                "Rows": {},
            }
        )
        assert_balance_sheet_balances(report)

    def test_a_report_with_only_one_of_the_two_totals_is_skipped(self) -> None:
        """Not every report carrying a TotalAssets section is a balance sheet."""
        report = parse_report(
            {
                "Columns": {
                    "Column": [
                        {"ColTitle": "", "ColType": "Account"},
                        {"ColTitle": "Total", "ColType": "Money"},
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "group": "TotalAssets",
                            "Header": {"ColData": [{"value": "ASSETS"}, {"value": ""}]},
                            "Summary": {
                                "ColData": [{"value": "TOTAL"}, {"value": "100.00"}]
                            },
                        }
                    ]
                },
            }
        )
        assert_balance_sheet_balances(report)

    def test_a_row_with_no_stated_total_is_skipped_by_the_column_check(self) -> None:
        report = parse_report(
            {
                "Columns": {
                    "Column": [
                        {"ColTitle": "", "ColType": "Account"},
                        {"ColTitle": "Jan", "ColType": "Money"},
                        {"ColTitle": "Feb", "ColType": "Money"},
                        {"ColTitle": "Total", "ColType": "Money"},
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "ColData": [
                                {"value": "Income"},
                                {"value": "10.00"},
                                {"value": "20.00"},
                                {"value": ""},
                            ],
                            "type": "Data",
                        }
                    ]
                },
            }
        )
        assert_columns_sum_to_total(report)


class TestRemainingBranches:
    """Branches that only occur on responses QuickBooks rarely produces."""

    def test_an_unparseable_period_date_is_dropped_not_fatal(self) -> None:
        report = parse_report(
            {"Header": {"ReportName": "X", "StartPeriod": "not-a-date"}}
        )
        assert report.start_period is None

    def test_a_query_response_that_is_not_an_object_yields_no_rows(self) -> None:
        from qbo.client import QboClient

        assert QboClient._rows({"QueryResponse": "unexpected"}) == []  # pyright: ignore[reportPrivateUsage]

    def test_paging_metadata_is_skipped_when_it_precedes_the_entity(self) -> None:
        """QuickBooks does not guarantee key order, so the entity array must be
        found past startPosition/maxResults/totalCount wherever they appear."""
        from qbo.client import QboClient

        rows = QboClient._rows(  # pyright: ignore[reportPrivateUsage]
            {
                "QueryResponse": {
                    "startPosition": 1,
                    "maxResults": 2,
                    "totalCount": 2,
                    "Invoice": [{"Id": "1"}, {"Id": "2"}],
                }
            }
        )
        assert [r["Id"] for r in rows] == ["1", "2"]

    def test_non_list_values_are_passed_over(self) -> None:
        from qbo.client import QboClient

        rows = QboClient._rows(  # pyright: ignore[reportPrivateUsage]
            {"QueryResponse": {"someScalar": "x", "Invoice": [{"Id": "1"}]}}
        )
        assert [r["Id"] for r in rows] == ["1"]

    def test_a_balance_sheet_without_liability_subsections_is_accepted(self) -> None:
        """Assets and the combined total agree and there is nothing further to
        cross-check; a summary-only balance sheet is legitimate."""
        report = parse_report(
            {
                "Columns": {
                    "Column": [
                        {"ColTitle": "", "ColType": "Account"},
                        {"ColTitle": "Total", "ColType": "Money"},
                    ]
                },
                "Rows": {
                    "Row": [
                        {
                            "group": "TotalAssets",
                            "Header": {"ColData": [{"value": "ASSETS"}, {"value": ""}]},
                            "Summary": {
                                "ColData": [{"value": "TOTAL"}, {"value": "100.00"}]
                            },
                        },
                        {
                            "group": "TotalLiabilitiesAndEquity",
                            "Header": {"ColData": [{"value": "L+E"}, {"value": ""}]},
                            "Summary": {
                                "ColData": [{"value": "TOTAL"}, {"value": "100.00"}]
                            },
                        },
                    ]
                },
            }
        )
        assert_balance_sheet_balances(report)

    async def test_the_transport_is_created_once_and_reused(self) -> None:
        auth = _auth(MemoryTokenStore(_expired()))
        first = await auth._client()  # pyright: ignore[reportPrivateUsage]
        second = await auth._client()  # pyright: ignore[reportPrivateUsage]
        assert first is second
        await auth.aclose()

    async def test_cleanup_tolerates_the_temp_file_already_being_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cleanup path must not fail on top of the failure it is cleaning
        up after -- that would mask the original error."""
        store = FileTokenStore(tmp_path / "tokens.json")

        def vanish_then_fail(src: Any, _dst: Any) -> None:
            os.unlink(src)
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", vanish_then_fail)
        with pytest.raises(OSError, match="disk full"):
            await store.save(_expired())
        assert not (tmp_path / "tokens.json").exists()
