"""Report parsing and the invariants.

Two kinds of test here. The first parses Intuit's own published sample
responses and asserts the flattening is faithful and the invariants pass --
real data must not trip them, or they would be noise and get switched off.

The second builds reports that are deliberately, specifically wrong, in the
exact shapes the connector this package replaces produced: a section whose
stated total contradicts its own line items, and a balance sheet whose equity
rows contradict its stated total. Those must raise. They are the regression
tests for the failure this whole project exists to prevent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from qbo.errors import QboInvariantError
from qbo.reports import (
    Report,
    assert_balance_sheet_balances,
    assert_columns_sum_to_total,
    assert_section_totals_match_lines,
    check_report,
    describe_sections,
    parse_money,
    parse_report,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _money_cell(value: str) -> dict[str, str]:
    return {"value": value}


def _report(
    name: str,
    rows: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
) -> Report:
    """Assemble a report payload in QuickBooks' own shape."""
    titles = columns or ["", "Total"]
    return parse_report(
        {
            "Header": {
                "ReportName": name,
                "StartPeriod": "2026-08-01",
                "EndPeriod": "2026-08-31",
                "Currency": "USD",
                "ReportBasis": "Accrual",
            },
            "Columns": {
                "Column": [
                    {
                        "ColTitle": title,
                        "ColType": "Account" if index == 0 else "Money",
                        "MetaData": [
                            {
                                "Name": "ColKey",
                                "Value": "account" if index == 0 else title.lower(),
                            }
                        ],
                    }
                    for index, title in enumerate(titles)
                ]
            },
            "Rows": {"Row": rows},
        }
    )


def _section(
    label: str, group: str, total: str, children: list[dict[str, Any]]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "Header": {"ColData": [_money_cell(label), _money_cell("")]},
        "type": "Section",
        "group": group,
        "Summary": {"ColData": [_money_cell(f"Total {label}"), _money_cell(total)]},
    }
    if children:
        entry["Rows"] = {"Row": children}
    return entry


def _data(label: str, amount: str) -> dict[str, Any]:
    return {"ColData": [_money_cell(label), _money_cell(amount)], "type": "Data"}


class TestParseMoney:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1350.55", Decimal("1350.55")),
            ("-100.00", Decimal("-100.00")),
            ("(250.00)", Decimal("-250.00")),
            ("1,234,567.89", Decimal("1234567.89")),
            ("$42.00", Decimal("42.00")),
            ("0", Decimal("0")),
        ],
    )
    def test_amount_formats(self, raw: str, expected: Decimal) -> None:
        assert parse_money(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", None, "not a number", "$"])
    def test_blank_and_unparseable_become_none(self, raw: Any) -> None:
        """Blank must stay distinct from zero -- a section reporting a blank
        total against real line items is the defect being hunted."""
        assert parse_money(raw) is None

    def test_zero_is_not_none(self) -> None:
        assert parse_money("0.00") == Decimal("0")
        assert parse_money("0.00") is not None

    def test_a_decimal_passes_through_unchanged(self) -> None:
        assert parse_money(Decimal("12.34")) == Decimal("12.34")


class TestParseRealBalanceSheet:
    def test_header_is_read(self) -> None:
        report = parse_report(_fixture("balance_sheet"))
        assert report.name == "BalanceSheet"
        assert report.currency == "USD"
        assert report.basis == "Accrual"
        assert report.start_period is not None
        assert report.end_period is not None

    def test_nested_tree_is_flattened_with_paths(self) -> None:
        report = parse_report(_fixture("balance_sheet"))
        checking = next(r for r in report.rows if r.label == "Checking")
        assert checking.value("total") == Decimal("1350.55")
        assert not checking.is_summary
        # ASSETS -> Current Assets -> Bank Accounts -> Checking
        assert checking.path[0] == "ASSETS"
        assert "Bank Accounts" in checking.path
        assert checking.account_id == "35"

    def test_section_summaries_are_kept_as_rows(self) -> None:
        report = parse_report(_fixture("balance_sheet"))
        assets = report.section("TotalAssets")
        assert assets is not None
        assert assets.is_summary
        assert assets.value("total") == Decimal("24742.44")

    def test_real_data_satisfies_every_invariant(self) -> None:
        """Intuit's own sample must pass, or the checks are noise."""
        assert check_report(parse_report(_fixture("balance_sheet"))) is not None


class TestParseRealProfitAndLoss:
    def test_income_section_matches_its_children(self) -> None:
        report = parse_report(_fixture("profit_and_loss"))
        income = report.section("Income")
        assert income is not None
        assert income.value("total") == Decimal("325.00")

    def test_a_blank_total_with_no_line_items_is_not_a_failure(self) -> None:
        """Intuit's sample reports 'Total Expenses' as an empty string with no
        expense rows beneath it. That is a period with no expenses, not a
        contradiction, and flagging it would make the check unusable."""
        report = parse_report(_fixture("profit_and_loss"))
        expenses = report.section("Expenses")
        assert expenses is not None
        assert expenses.value("total") is None
        assert report.children_of(expenses) == ()
        assert_section_totals_match_lines(report)

    def test_computed_sections_have_totals_and_no_children(self) -> None:
        report = parse_report(_fixture("profit_and_loss"))
        for group in ("GrossProfit", "NetOperatingIncome", "NetIncome"):
            row = report.section(group)
            assert row is not None, group
            assert report.children_of(row) == ()
        check_report(report)

    def test_other_real_reports_parse_and_pass(self) -> None:
        for name in ("trial_balance", "general_ledger"):
            check_report(parse_report(_fixture(name)))


class TestSectionTotalsInvariant:
    def test_the_exact_failure_this_package_exists_for(self) -> None:
        """A P&L whose expense total is 0.00 against line items summing to
        37,551.89. The bad connector returned this as fact three times."""
        report = _report(
            "ProfitAndLoss",
            [
                _section(
                    "Expenses",
                    "Expenses",
                    "0.00",
                    [
                        _data("Contract Labor", "31000.00"),
                        _data("Software Subscriptions", "4551.89"),
                        _data("Merchant Fees", "2000.00"),
                    ],
                )
            ],
        )
        with pytest.raises(QboInvariantError) as exc:
            assert_section_totals_match_lines(report)
        message = str(exc.value)
        assert "ProfitAndLoss failed invariant" in message
        assert "0.00" in message
        assert "37,551.89" in message

    def test_a_blank_total_contradicted_by_line_items_raises(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [
                _section(
                    "Expenses", "Expenses", "", [_data("Contract Labor", "31000.00")]
                )
            ],
        )
        with pytest.raises(QboInvariantError, match="reported blank"):
            assert_section_totals_match_lines(report)

    def test_a_section_that_agrees_with_its_children_passes(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [
                _section(
                    "Income",
                    "Income",
                    "325.00",
                    [_data("Services", "425.00"), _data("Refunds", "-100.00")],
                )
            ],
        )
        assert_section_totals_match_lines(report)

    def test_rounding_within_tolerance_is_accepted(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [_section("Income", "Income", "100.01", [_data("Services", "100.00")])],
        )
        assert_section_totals_match_lines(report)

    def test_a_difference_beyond_tolerance_raises(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [_section("Income", "Income", "100.02", [_data("Services", "100.00")])],
        )
        with pytest.raises(QboInvariantError):
            assert_section_totals_match_lines(report)

    def test_tolerance_is_configurable(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [_section("Income", "Income", "101.00", [_data("Services", "100.00")])],
        )
        assert_section_totals_match_lines(report, tolerance=Decimal("1.00"))
        with pytest.raises(QboInvariantError):
            assert_section_totals_match_lines(report, tolerance=Decimal("0.50"))


class TestBalanceSheetInvariant:
    @staticmethod
    def _sheet(assets: str, combined: str, liabilities: str, equity: str) -> Report:
        return _report(
            "BalanceSheet",
            [
                _section("ASSETS", "TotalAssets", assets, [_data("Checking", assets)]),
                _section(
                    "LIABILITIES AND EQUITY",
                    "TotalLiabilitiesAndEquity",
                    combined,
                    [
                        _section(
                            "Liabilities",
                            "Liabilities",
                            liabilities,
                            [_data("Loan Payable", liabilities)],
                        ),
                        _section(
                            "Equity", "Equity", equity, [_data("Retained", equity)]
                        ),
                    ],
                ),
            ],
        )

    def test_a_balanced_sheet_passes(self) -> None:
        assert_balance_sheet_balances(
            self._sheet("24742.44", "24742.44", "31548.42", "-6805.98")
        )

    def test_assets_not_equalling_liabilities_and_equity_raises(self) -> None:
        with pytest.raises(QboInvariantError, match="do not equal liabilities"):
            assert_balance_sheet_balances(
                self._sheet("24742.44", "19999.99", "31548.42", "-6805.98")
            )

    def test_equity_rows_contradicting_the_stated_total_raises(self) -> None:
        """The second shape the bad connector produced: the sheet balances at
        the top while its own equity rows disagree with the total beneath."""
        with pytest.raises(QboInvariantError, match="its own sections sum to"):
            assert_balance_sheet_balances(
                self._sheet("24742.44", "24742.44", "31548.42", "-100.00")
            )

    def test_a_missing_stated_total_raises(self) -> None:
        with pytest.raises(QboInvariantError, match="missing a stated total"):
            assert_balance_sheet_balances(
                self._sheet("24742.44", "", "31548.42", "-6805.98")
            )

    def test_a_report_without_balance_sheet_sections_is_skipped(self) -> None:
        report = _report("ProfitAndLoss", [_data("Services", "100.00")])
        assert_balance_sheet_balances(report)


class TestColumnInvariant:
    @staticmethod
    def _monthly(jan: str, feb: str, total: str) -> Report:
        return _report(
            "ProfitAndLoss",
            [
                {
                    "ColData": [
                        _money_cell("Estimate Income"),
                        _money_cell(jan),
                        _money_cell(feb),
                        _money_cell(total),
                    ],
                    "type": "Data",
                }
            ],
            columns=["", "Jan 2026", "Feb 2026", "Total"],
        )

    def test_months_summing_to_the_total_pass(self) -> None:
        assert_columns_sum_to_total(self._monthly("100.00", "150.00", "250.00"))

    def test_months_not_summing_to_the_total_raise(self) -> None:
        with pytest.raises(QboInvariantError, match="period columns sum to"):
            assert_columns_sum_to_total(self._monthly("100.00", "150.00", "900.00"))

    def test_a_single_column_report_is_skipped(self) -> None:
        assert_columns_sum_to_total(_report("ProfitAndLoss", [_data("X", "1.00")]))


class TestCheckReport:
    def test_it_returns_the_report_so_it_can_be_used_inline(self) -> None:
        report = parse_report(_fixture("balance_sheet"))
        assert check_report(report) is report

    def test_it_raises_on_the_first_broken_invariant(self) -> None:
        report = _report(
            "ProfitAndLoss",
            [_section("Expenses", "Expenses", "0.00", [_data("Labor", "500.00")])],
        )
        with pytest.raises(QboInvariantError):
            check_report(report)

    def test_describe_sections_lists_only_summaries(self) -> None:
        report = parse_report(_fixture("balance_sheet"))
        sections = describe_sections(report)
        assert sections
        assert all(row.is_summary for row in sections)


class TestDegenerateInput:
    def test_an_empty_payload_parses_to_an_empty_report(self) -> None:
        report = parse_report({})
        assert report.rows == ()
        assert report.columns == ()
        check_report(report)

    def test_a_report_with_no_value_columns_is_skipped(self) -> None:
        report = parse_report(
            {"Header": {"ReportName": "X"}, "Columns": {"Column": []}, "Rows": {}}
        )
        assert_section_totals_match_lines(report)
        assert_balance_sheet_balances(report)

    def test_malformed_rows_are_ignored_rather_than_crashing(self) -> None:
        report = parse_report(
            {
                "Header": {"ReportName": "X"},
                "Columns": {"Column": [{"ColTitle": "", "ColType": "Account"}]},
                "Rows": {"Row": ["not a row", 42, None]},
            }
        )
        assert report.rows == ()
