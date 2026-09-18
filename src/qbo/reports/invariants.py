"""Arithmetic that must hold, asserted rather than assumed.

This module is the reason the package exists. An existing QuickBooks connector
returned report widgets whose summary fields and equity rows were wrong, and
three separate false findings were built on those numbers before anyone checked
against an export. Every one of those reports was internally inconsistent and
said so plainly -- a section claiming a total its own line items contradicted --
but nothing was looking.

So these run before a report is handed to a caller, and they raise. A tool that
fails with

    ProfitAndLoss failed invariant: section 'Expenses' reports 0.00 but its
    line items sum to 37,551.89 (difference 37,551.89)

is worth more than one that returns the number.

A failed invariant is an error, never a warning. Returning a plausible-looking
figure alongside a caveat puts the burden of noticing on the reader, which is
exactly the failure being prevented.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, NoReturn

from qbo.errors import QboInvariantError
from qbo.reports.model import Report, ReportRow

__all__ = [
    "DEFAULT_TOLERANCE",
    "assert_section_totals_match_lines",
    "assert_balance_sheet_balances",
    "assert_columns_sum_to_total",
    "check_report",
]

#: QuickBooks rounds each displayed figure independently, so a section of many
#: rows can differ from their exact sum by a cent or so without anything being
#: wrong. This is deliberately tight: the failures worth catching are off by
#: whole balances, not by rounding.
DEFAULT_TOLERANCE: Final[Decimal] = Decimal("0.01")

#: Sections QuickBooks computes rather than aggregates. They legitimately carry
#: a total with no children beneath them, so "summary equals its children" does
#: not apply -- Gross Profit is income minus cost of sales, not a container.
COMPUTED_GROUPS: Final[frozenset[str]] = frozenset(
    {
        "GrossProfit",
        "NetOperatingIncome",
        "NetIncome",
        "NetOtherIncome",
        "NetIncomeLoss",
    }
)


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _fail(report: Report, detail: str) -> NoReturn:
    name = report.name or "Report"
    period = ""
    if report.start_period and report.end_period:
        period = f" for {report.start_period} to {report.end_period}"
    raise QboInvariantError(f"{name} failed invariant{period}: {detail}")


def assert_section_totals_match_lines(
    report: Report, *, tolerance: Decimal = DEFAULT_TOLERANCE
) -> None:
    """Every section's stated total must equal what sits beneath it.

    Sections with no children are skipped: a computed line such as Gross Profit
    has a total and nothing to aggregate, and an empty section legitimately
    reports a blank total with no line items to contradict it.

    A blank total *with* line items beneath it is not skipped. It is read as
    zero and compared, because that is the exact shape of the defect this
    package was written for.
    """
    column = report.total_column or (
        report.value_columns[0] if report.value_columns else None
    )
    if column is None:
        return

    for row in report.rows:
        if not row.is_summary or row.group in COMPUTED_GROUPS:
            continue
        children = report.children_of(row)
        if not children:
            continue

        stated = row.value(column.key)
        actual = sum(
            (child.value(column.key) or Decimal("0") for child in children),
            Decimal("0"),
        )
        # A blank stated total is read as zero here, but only because there are
        # children to contradict it. With none, the row was skipped above.
        effective = stated if stated is not None else Decimal("0")
        if abs(effective - actual) > tolerance:
            blank = " (reported blank)" if stated is None else ""
            _fail(
                report,
                f"section {row.label!r} reports {_money(effective)}{blank} but its "
                f"{len(children)} line items sum to {_money(actual)} "
                f"(difference {_money(abs(effective - actual))})",
            )


def assert_balance_sheet_balances(
    report: Report, *, tolerance: Decimal = DEFAULT_TOLERANCE
) -> None:
    """Assets must equal liabilities plus equity.

    Checked two ways where the report supports it: against the total QuickBooks
    states for liabilities and equity together, and against the sum of the two
    sections. A report can agree with itself on one and not the other -- the
    bad connector's equity rows contradicted its own stated total.
    """
    column = report.total_column or (
        report.value_columns[0] if report.value_columns else None
    )
    if column is None:
        return

    assets = report.section("TotalAssets")
    combined = report.section("TotalLiabilitiesAndEquity")
    if assets is None or combined is None:
        return

    assets_total = assets.value(column.key)
    combined_total = combined.value(column.key)
    if assets_total is None or combined_total is None:
        _fail(
            report,
            "balance sheet is missing a stated total for "
            + ("assets" if assets_total is None else "liabilities and equity"),
        )

    if abs(assets_total - combined_total) > tolerance:
        _fail(
            report,
            f"assets {_money(assets_total)} do not equal liabilities and equity "
            f"{_money(combined_total)} "
            f"(difference {_money(abs(assets_total - combined_total))})",
        )

    liabilities = report.section("Liabilities")
    equity = report.section("Equity")
    if liabilities is None or equity is None:
        return

    parts = (liabilities.value(column.key) or Decimal("0")) + (
        equity.value(column.key) or Decimal("0")
    )
    if abs(parts - combined_total) > tolerance:
        _fail(
            report,
            f"stated total for liabilities and equity is {_money(combined_total)} "
            f"but its own sections sum to {_money(parts)} "
            f"(liabilities {_money(liabilities.value(column.key) or Decimal('0'))}, "
            f"equity {_money(equity.value(column.key) or Decimal('0'))})",
        )


def assert_columns_sum_to_total(
    report: Report, *, tolerance: Decimal = DEFAULT_TOLERANCE
) -> None:
    """Each row's period columns must sum to its total column.

    Only meaningful on a report summarised by month or quarter. A row whose
    months do not add up to its own annual figure is the clearest possible
    signal that the response was assembled wrongly.
    """
    total_column = report.total_column
    periods = report.period_columns
    if total_column is None or len(periods) < 2:
        return

    for row in report.rows:
        stated = row.value(total_column.key)
        if stated is None:
            continue
        across = sum(
            (row.value(column.key) or Decimal("0") for column in periods),
            Decimal("0"),
        )
        if abs(stated - across) > tolerance:
            _fail(
                report,
                f"row {row.label!r} states a total of {_money(stated)} but its "
                f"{len(periods)} period columns sum to {_money(across)} "
                f"(difference {_money(abs(stated - across))})",
            )


def check_report(report: Report, *, tolerance: Decimal = DEFAULT_TOLERANCE) -> Report:
    """Run every invariant that applies, then return the report.

    Returning it makes this usable inline -- ``return check_report(parsed)`` --
    so there is no version of the call that validates and then forgets to.
    """
    assert_section_totals_match_lines(report, tolerance=tolerance)
    assert_columns_sum_to_total(report, tolerance=tolerance)
    if report.section("TotalAssets") is not None:
        assert_balance_sheet_balances(report, tolerance=tolerance)
    return report


def describe_sections(report: Report) -> tuple[ReportRow, ...]:
    """Every section summary, for diagnosing a failure."""
    return tuple(row for row in report.rows if row.is_summary)
