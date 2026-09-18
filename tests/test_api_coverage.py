"""Coverage is a verified fact, not a claim.

These tests assert in both directions against the vendored documentation:

* every entity and report Intuit documents appears in the registry, so an
  endpoint added upstream fails the suite rather than passing unnoticed;
* every registry entry traces back to something Intuit documents, so the
  registry cannot drift into inventing endpoints -- which the hand-written
  version did, with ``CreditCardPaymentTxn`` and ``JournalCode``.

Refresh the vendored sources with ``scripts/refresh_sources.py``, then
regenerate with ``tools/generate_entities.py`` and ``tools/audit_coverage.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from qbo.entities import (
    DEFAULT_MINOR_VERSION,
    ENTITIES,
    REPORT_ROUTES,
    REPORTS,
    Operation,
    report_route,
    supports,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from docsource import as_dict, as_list, as_str  # noqa: E402

DOCS = REPO_ROOT / "refs" / "docs"
pytestmark = pytest.mark.skipif(
    not (DOCS / "EntityJsonObject_v1.json").exists(),
    reason="Vendored docs missing; run scripts/refresh_sources.py",
)


def _documented() -> dict[str, object]:
    """Every documented QBO object, straight from the vendored docs."""
    raw = json.loads((DOCS / "EntityJsonObject_v1.json").read_text(encoding="utf-8"))
    return as_dict(as_dict(as_dict(raw).get("entities")).get("qbo"))


def _routes(body: object) -> list[str]:
    """Every HTTP route one documented object declares."""
    found: list[str] = []
    for items in as_dict(as_dict(body).get("operations")).values():
        for item in as_list(items):
            route = as_str(as_dict(as_dict(item).get("definition")).get("Operation"))
            if route:
                found.append(route)
    return found


SPECIAL = {"Batch", "ChangeDataCapture"}


class TestBidirectionalCoverage:
    def test_every_documented_entity_is_registered(self) -> None:
        """An endpoint Intuit adds must fail the suite, not pass unnoticed."""
        documented = {
            name
            for name, body in _documented().items()
            if name not in SPECIAL and not any("/reports/" in r for r in _routes(body))
        }
        missing = documented - set(ENTITIES)
        assert not missing, f"documented but unregistered: {sorted(missing)}"

    def test_every_registered_entity_is_documented(self) -> None:
        """The registry must not invent endpoints."""
        invented = set(ENTITIES) - set(_documented())
        assert not invented, f"registered but undocumented: {sorted(invented)}"

    def test_every_documented_report_is_registered(self) -> None:
        documented = {
            name
            for name, body in _documented().items()
            if any("/reports/" in r for r in _routes(body))
        }
        missing = documented - set(REPORTS)
        assert not missing, f"documented reports missing: {sorted(missing)}"

    def test_registry_is_not_stale(self) -> None:
        """Regenerating from refs/ must reproduce the checked-in registry.

        Delegated to the generator's own --check mode rather than re-importing
        it, so the test exercises exactly the command a human or CI would run.
        """
        self._assert_generator_current("generate_entities.py")

    def test_coverage_artefacts_are_not_stale(self) -> None:
        self._assert_generator_current("audit_coverage.py")

    @staticmethod
    def _assert_generator_current(tool: str) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / tool), "--check"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{tool} reports stale output:\n{result.stdout}{result.stderr}"
        )


class TestRegistryShape:
    def test_every_entity_has_a_path_and_operations(self) -> None:
        for name, spec in ENTITIES.items():
            assert spec.path, f"{name} has no URL path"
            assert spec.path.islower(), f"{name} path should be lowercase"
            assert spec.operations, f"{name} declares no operations"

    def test_query_implies_read_for_queryable_entities(self) -> None:
        """Anything you can query, you can fetch by id -- with documented
        exceptions where Intuit exposes only one of the two."""
        exceptions = {"Exchangerate"}
        for name, spec in ENTITIES.items():
            if Operation.QUERY in spec.operations and name not in exceptions:
                assert Operation.READ in spec.operations, f"{name}: QUERY without READ"

    def test_update_requires_sync_token(self) -> None:
        """SyncToken is QuickBooks' optimistic-concurrency check; every
        updatable entity documents it as required for update."""
        for name, spec in ENTITIES.items():
            if Operation.UPDATE in spec.operations:
                assert "SyncToken" in spec.required_for_update, (
                    f"{name} is updatable but does not require SyncToken"
                )

    def test_known_entities_carry_their_documented_operations(self) -> None:
        assert supports("Invoice", Operation.SEND)
        assert supports("Invoice", Operation.PDF)
        assert supports("TaxService", Operation.CREATE)
        assert not supports("TaxService", Operation.QUERY)
        assert not supports("Account", Operation.DELETE), (
            "Accounts are deactivated by update, never deleted"
        )

    def test_entity_names_that_hand_writing_got_wrong(self) -> None:
        """Regression: the hand-written registry invented these."""
        assert "CreditCardPayment" in ENTITIES
        assert "CreditCardPaymentTxn" not in ENTITIES, "that is the XSD name"
        assert "Exchangerate" in ENTITIES
        assert "ExchangeRate" not in ENTITIES, "docs use a lowercase r"
        assert "JournalCode" not in ENTITIES, "not exposed by the Accounting API"
        for name in (
            "ChangeOrder",
            "InventoryAdjustment",
            "TaxPayment",
            "Entitlements",
        ):
            assert name in ENTITIES, f"{name} is documented and was wrongly excluded"

    def test_default_minor_version_matches_the_docs(self) -> None:
        raw = as_dict(json.loads((DOCS / "QboMinorVersions.json").read_text()))
        assert DEFAULT_MINOR_VERSION == int(as_str(raw.get("defaultMinorVersion")))

    def test_report_set_covers_the_financial_statements(self) -> None:
        for report in (
            "BalanceSheet",
            "ProfitAndLoss",
            "GeneralLedger",
            "TrialBalance",
            "TransactionList",
            "ARAgingSummary",
        ):
            assert report in REPORTS, f"{report} missing from the report registry"

    def test_reports_whose_url_differs_from_their_documented_name(self) -> None:
        """Nine reports are filed under a name the URL will not accept.
        Getting these wrong is an HTTP 400, so they are pinned here."""
        assert report_route("APAgingDetail") == "AgedPayableDetail"
        assert report_route("APAgingSummary") == "AgedPayables"
        assert report_route("ARAgingDetail") == "AgedReceivableDetail"
        assert report_route("ARAgingSummary") == "AgedReceivables"
        assert report_route("AccountListDetail") == "AccountList"
        assert report_route("SalesByCustomer") == "CustomerSales"
        assert report_route("SalesByProduct") == "ItemSales"
        assert report_route("SalesByClassSummary") == "ClassSales"
        assert report_route("SalesByDepartment") == "DepartmentSales"

    def test_report_route_accepts_either_name(self) -> None:
        assert report_route("AgedPayableDetail") == "AgedPayableDetail"
        assert report_route("BalanceSheet") == "BalanceSheet"

    def test_trial_balance_keeps_its_locale_variant(self) -> None:
        """One QUERY entry documents both TrialBalanceFR (France) and
        TrialBalance; the general route must be the default."""
        assert REPORTS["TrialBalance"].route == "TrialBalance"
        assert "TrialBalanceFR" in REPORTS["TrialBalance"].variants
        assert REPORT_ROUTES["TrialBalanceFR"] == "TrialBalance"

    def test_unknown_report_names_fail_loudly(self) -> None:
        with pytest.raises(KeyError, match="Unknown report"):
            report_route("NoSuchReport")
