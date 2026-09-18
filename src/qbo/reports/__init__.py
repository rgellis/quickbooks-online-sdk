"""Report parsing and the arithmetic checks applied to it.

QuickBooks returns reports as a nested tree whose sections each restate their
own totals. :func:`parse_report` flattens that into rows; the invariants then
compare what each section claims against what its children actually sum to, and
raise :class:`qbo.errors.QboInvariantError` when they disagree.

Typical use::

    from qbo.reports import check_report, parse_report

    payload = await client.report("ProfitAndLoss", start_date=..., end_date=...)
    report = check_report(parse_report(payload))
"""

from __future__ import annotations

from qbo.reports.invariants import (
    COMPUTED_GROUPS,
    DEFAULT_TOLERANCE,
    assert_balance_sheet_balances,
    assert_columns_sum_to_total,
    assert_section_totals_match_lines,
    check_report,
    describe_sections,
)
from qbo.reports.model import (
    Report,
    ReportColumn,
    ReportRow,
    parse_money,
    parse_report,
)

__all__ = [
    "COMPUTED_GROUPS",
    "DEFAULT_TOLERANCE",
    "Report",
    "ReportColumn",
    "ReportRow",
    "assert_balance_sheet_balances",
    "assert_columns_sum_to_total",
    "assert_section_totals_match_lines",
    "check_report",
    "describe_sections",
    "parse_money",
    "parse_report",
]
