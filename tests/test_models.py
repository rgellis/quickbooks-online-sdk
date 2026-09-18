"""Generated models parse what QuickBooks actually returns.

The point of a typed layer here is not tidiness -- it is that a wrong number
should be impossible to produce quietly. So these tests check the properties
that protect that: money stays decimal, repeated fields stay lists, polymorphic
line items resolve to the right shape, and fields QuickBooks adds in a future
minorversion survive rather than vanishing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from qbo.entities import ENTITIES
from qbo.models import (
    ENTITY_MODELS,
    Account,
    Invoice,
    JournalEntry,
    JournalEntryLine,
    QboModel,
    SalesItemLine,
    model_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

INVOICE_PAYLOAD = {
    "Id": "130",
    "SyncToken": "0",
    "DocNumber": "1037",
    "TxnDate": "2026-08-14",
    "CustomerRef": {"value": "24", "name": "Restoration Contractor"},
    "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
    "TotalAmt": 2780.92,
    "Balance": 0,
    "Line": [
        {
            "Id": "1",
            "LineNum": 1,
            "Amount": 2780.92,
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {
                "ItemRef": {"value": "1", "name": "Estimate"},
                "Qty": 1,
            },
        },
        {"Amount": 2780.92, "DetailType": "SubTotalLineDetail"},
    ],
}


class TestCoverage:
    def test_every_documented_entity_has_a_model(self) -> None:
        missing = set(ENTITIES) - set(ENTITY_MODELS)
        assert not missing, f"entities without a model: {sorted(missing)}"

    def test_models_are_not_stale(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "generate_models.py"),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_model_for_rejects_unknown_entities_helpfully(self) -> None:
        with pytest.raises(KeyError, match="Unknown entity"):
            model_for("NoSuchEntity")
        assert model_for("Invoice") is Invoice


class TestMoney:
    def test_amounts_parse_as_decimal_never_float(self) -> None:
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert isinstance(invoice.TotalAmt, Decimal)
        assert invoice.TotalAmt == Decimal("2780.92")
        assert not isinstance(invoice.TotalAmt, float)

    def test_decimal_survives_a_json_round_trip(self) -> None:
        """The client decodes with parse_float=Decimal; confirm that is enough
        to keep a value exact from wire to model."""
        raw = json.dumps({"TotalAmt": 10000000000000.05})
        decoded = json.loads(raw, parse_float=Decimal)
        invoice = Invoice.model_validate(decoded)
        assert invoice.TotalAmt == Decimal("10000000000000.05")

    def test_zero_balance_is_decimal_zero_not_none(self) -> None:
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert invoice.Balance == Decimal("0")
        assert invoice.Balance is not None


class TestShape:
    def test_repeated_fields_are_lists(self) -> None:
        """`Line [0..n]` in the docs means a JSON array. Losing that cardinality
        produces a model that rejects every real transaction."""
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert isinstance(invoice.Line, list)
        assert len(invoice.Line) == 2

    def test_polymorphic_lines_resolve_to_their_own_shapes(self) -> None:
        """Invoice.Line is a union over five line shapes. A SalesItemLineDetail
        payload must arrive as SalesItemLine, not as whichever member happens
        to accept the keys."""
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert invoice.Line is not None
        first = invoice.Line[0]
        assert isinstance(first, SalesItemLine)
        assert first.Amount == Decimal("2780.92")
        assert first.SalesItemLineDetail is not None
        assert first.SalesItemLineDetail.ItemRef is not None
        assert first.SalesItemLineDetail.ItemRef.name == "Estimate"

    def test_nested_references_parse_into_models(self) -> None:
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert invoice.CustomerRef is not None
        assert invoice.CustomerRef.value == "24"
        assert invoice.CustomerRef.name == "Restoration Contractor"

    def test_dates_parse_to_date_objects(self) -> None:
        invoice = Invoice.model_validate(INVOICE_PAYLOAD)
        assert invoice.TxnDate == date(2026, 8, 14)

    def test_unknown_fields_are_kept_not_dropped(self) -> None:
        """QuickBooks adds fields in new minorversions. Dropping them silently
        is how a field that exists on the wire becomes invisible."""
        invoice = Invoice.model_validate(
            {**INVOICE_PAYLOAD, "SomeFutureMinorVersionField": "value"}
        )
        assert invoice.model_extra == {"SomeFutureMinorVersionField": "value"}

    def test_a_response_missing_documented_fields_still_parses(self) -> None:
        """Partial reads and sparse updates return valid responses that omit
        documented fields. Refusing them would be worse than admitting absence."""
        assert Invoice.model_validate({"Id": "1"}).DocNumber is None


class TestEnums:
    def test_enum_members_come_from_the_xsd(self) -> None:
        """The docs name AccountTypeEnum but never list its values; the XSD
        enumerates all 16. Neither source suffices alone."""
        account = Account.model_validate(
            {"Id": "1004", "Name": "Stripe Clearing", "AccountType": "Bank"}
        )
        assert account.AccountType is not None
        assert str(account.AccountType) == "Bank"

    def test_account_number_and_type_are_available(self) -> None:
        """Account role must be read from AcctNum and AccountType, never the
        name -- files routinely call a bank account "Receivable"."""
        account = Account.model_validate(
            {"Id": "5", "Name": "Receivable", "AcctNum": "1002", "AccountType": "Bank"}
        )
        assert account.AcctNum == "1002"
        assert str(account.AccountType) == "Bank"


class TestJournalEntry:
    def test_journal_entry_lines_carry_posting_type(self) -> None:
        entry = JournalEntry.model_validate(
            {
                "Id": "9",
                "TxnDate": "2026-07-31",
                "Line": [
                    {
                        "Amount": 955.85,
                        "DetailType": "JournalEntryLineDetail",
                        "JournalEntryLineDetail": {
                            "PostingType": "Debit",
                            "AccountRef": {"value": "84", "name": "Sales - Unpaid"},
                        },
                    }
                ],
            }
        )
        assert entry.Line is not None
        line = entry.Line[0]
        assert isinstance(line, JournalEntryLine)
        assert line.Amount == Decimal("955.85")
        detail = line.JournalEntryLineDetail
        assert detail is not None
        assert str(detail.PostingType) == "Debit"
        assert detail.AccountRef is not None
        assert detail.AccountRef.value == "84"


class TestDocumentationDefects:
    """Regression tests for malformed entries in Intuit's own documentation."""

    def test_timeactivity_omits_the_malformed_label(self) -> None:
        """The docs declare a field literally named 'BreakHours BreakMinutes',
        which is not a wire key. It is skipped and reported by the generator."""
        model = model_for("TimeActivity")
        assert "BreakHours BreakMinutes" not in model.model_fields
        assert "BreakHours" not in model.model_fields

    def test_entitlements_duplicate_label_resolves_to_one_field(self) -> None:
        """Entitlements declares 'Entitlement [0..n]' and 'Entitlement'. Only
        one attribute can exist; the first wins and the clash is reported."""
        model = model_for("Entitlements")
        assert "Entitlement" in model.model_fields

    def test_every_model_subclasses_the_common_base(self) -> None:
        for name, model in ENTITY_MODELS.items():
            assert issubclass(model, QboModel), name
