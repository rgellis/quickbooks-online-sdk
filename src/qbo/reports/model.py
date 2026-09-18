"""Parsing QuickBooks report responses into flat, comparable rows.

QuickBooks returns reports as an arbitrarily deep tree. A balance sheet nests
``ASSETS -> Current Assets -> Bank Accounts -> Checking``, and each level carries
its own ``Summary`` alongside its children. Reading a number off that structure
by hand means picking a node and trusting its summary, which is precisely how a
wrong total gets reported as fact.

So the tree is flattened once, here, into rows that each know their full section
path, and the summaries are kept as rows in their own right rather than folded
away. The invariants in :mod:`qbo.reports.invariants` then compare the two --
what a section claims against what its children actually sum to.

The raw payload is retained on :class:`Report` so every value stays traceable
back to the response it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping, Sequence, cast

__all__ = [
    "ReportColumn",
    "ReportRow",
    "Report",
    "parse_report",
    "parse_money",
]

#: The first column of every report is the account or label, not a value.
_LABEL_COLUMN: Final[int] = 0


def _mapping(value: Any) -> Mapping[str, Any]:
    """Narrow a decoded JSON value to an object, or an empty one.

    Report payloads are ``Any`` and this package type-checks in strict mode.
    These are total on purpose: a report that arrives in an unexpected shape
    should flatten to no rows and be caught by an invariant, not raise a
    KeyError from inside the walk.
    """
    if not isinstance(value, Mapping):
        return {}
    return cast("Mapping[str, Any]", value)


def _sequence(value: Any) -> list[Any]:
    """Narrow a decoded JSON value to an array, or an empty one."""
    if not isinstance(value, list):
        return []
    return list(cast("list[Any]", value))


def parse_money(raw: Any) -> Decimal | None:
    """Parse a report cell into a decimal amount.

    Returns ``None`` for a cell QuickBooks left blank, which is a distinct
    state from zero and must stay distinct: a section reporting a blank total
    against line items that sum to something is the defect this package exists
    to catch, and collapsing blank to ``0`` at parse time would hide whether the
    number was absent or genuinely nil.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # QuickBooks renders negatives in parentheses in some locales and strips
    # thousands separators inconsistently between report types.
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _parse_date(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class ReportColumn:
    """One column of a report."""

    title: str
    col_type: str
    key: str

    @property
    def is_value(self) -> bool:
        """Whether this column carries an amount rather than a label."""
        return self.col_type.lower() == "money"


@dataclass(frozen=True)
class ReportRow:
    """One flattened row, keeping where it sat in the original tree."""

    label: str
    path: tuple[str, ...]
    values: Mapping[str, Decimal | None]
    depth: int
    is_summary: bool
    group: str | None = None
    account_id: str | None = None

    def value(self, column: str = "total") -> Decimal | None:
        """The amount in one column, by column key."""
        return self.values.get(column)


@dataclass(frozen=True)
class Report:
    """A parsed report."""

    name: str
    columns: tuple[ReportColumn, ...]
    rows: tuple[ReportRow, ...]
    start_period: date | None = None
    end_period: date | None = None
    currency: str = ""
    basis: str = ""
    summarize_by: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict[str, Any], repr=False)

    @property
    def value_columns(self) -> tuple[ReportColumn, ...]:
        """Columns carrying amounts, in report order."""
        return tuple(column for column in self.columns if column.is_value)

    @property
    def total_column(self) -> ReportColumn | None:
        """The column holding the period total, when the report has one."""
        for column in self.value_columns:
            if column.key == "total" or column.title.strip().lower() == "total":
                return column
        return None

    @property
    def period_columns(self) -> tuple[ReportColumn, ...]:
        """Value columns other than the total -- the months, usually."""
        total = self.total_column
        return tuple(c for c in self.value_columns if c is not total)

    def section(self, group: str) -> ReportRow | None:
        """The summary row for a named section group, e.g. ``TotalAssets``."""
        for row in self.rows:
            if row.is_summary and row.group == group:
                return row
        return None

    def children_of(self, row: ReportRow) -> tuple[ReportRow, ...]:
        """The rows one level beneath a section summary.

        A summary is emitted immediately after its own subtree, so its children
        are the rows that share its path prefix and sit exactly one level down.
        """
        prefix = row.path
        return tuple(
            candidate
            for candidate in self.rows
            if len(candidate.path) == len(prefix) + 1
            and candidate.path[: len(prefix)] == prefix
        )


def _columns(payload: Mapping[str, Any]) -> tuple[ReportColumn, ...]:
    entries = _sequence(_mapping(payload.get("Columns")).get("Column"))

    columns: list[ReportColumn] = []
    for index, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry)
        if not entry:
            continue
        title = str(entry.get("ColTitle") or "")
        col_type = str(entry.get("ColType") or "")
        key = ""
        for raw_item in _sequence(entry.get("MetaData")):
            item = _mapping(raw_item)
            if str(item.get("Name")) == "ColKey":
                key = str(item.get("Value") or "")
        columns.append(
            ReportColumn(
                title=title,
                col_type=col_type,
                key=key or title.lower() or f"col{index}",
            )
        )
    return tuple(columns)


def _cells(col_data: Any) -> list[Mapping[str, Any]]:
    return [cell for cell in (_mapping(c) for c in _sequence(col_data)) if cell]


def _row_values(
    cells: Sequence[Mapping[str, Any]], columns: Sequence[ReportColumn]
) -> dict[str, Decimal | None]:
    """Align a row's cells to the report's columns by position."""
    values: dict[str, Decimal | None] = {}
    for index, column in enumerate(columns):
        if index == _LABEL_COLUMN:
            continue
        cell = cells[index] if index < len(cells) else None
        values[column.key] = parse_money(cell.get("value")) if cell else None
    return values


def _label(cells: Sequence[Mapping[str, Any]]) -> tuple[str, str | None]:
    if not cells:
        return "", None
    first = cells[_LABEL_COLUMN]
    identifier = first.get("id")
    return str(first.get("value") or ""), str(identifier) if identifier else None


def _walk(
    node: Any,
    columns: Sequence[ReportColumn],
    path: tuple[str, ...],
    out: list[ReportRow],
) -> None:
    for raw_entry in _sequence(_mapping(node).get("Row")):
        entry = _mapping(raw_entry)
        if not entry:
            continue

        header_cells = _cells(_mapping(entry.get("Header")).get("ColData"))
        summary_cells = _cells(_mapping(entry.get("Summary")).get("ColData"))
        own_cells = _cells(entry.get("ColData"))
        group = entry.get("group")
        group_name = str(group) if group else None

        if own_cells:
            label, account_id = _label(own_cells)
            out.append(
                ReportRow(
                    label=label,
                    path=(*path, label),
                    values=_row_values(own_cells, columns),
                    depth=len(path),
                    is_summary=False,
                    group=group_name,
                    account_id=account_id,
                )
            )
            continue

        # A section. Its label comes from the header when it has one; some
        # sections (GrossProfit, NetIncome) carry only a summary.
        header_label, _ = _label(header_cells)
        summary_label, _ = _label(summary_cells)
        label = header_label or summary_label or group_name or ""
        child_path = (*path, label)

        _walk(entry.get("Rows"), columns, child_path, out)

        if summary_cells:
            out.append(
                ReportRow(
                    label=summary_label or label,
                    path=child_path,
                    values=_row_values(summary_cells, columns),
                    depth=len(path),
                    is_summary=True,
                    group=group_name,
                )
            )


def parse_report(payload: Mapping[str, Any]) -> Report:
    """Parse a report response into flat rows.

    Args:
        payload: The decoded body of a ``/reports/<Name>`` response.

    Returns:
        A :class:`Report`. Section summaries appear as rows with
        ``is_summary=True``, positioned after the subtree they summarise.
    """
    header_map = _mapping(payload.get("Header"))
    columns = _columns(payload)
    rows: list[ReportRow] = []
    _walk(payload.get("Rows"), columns, (), rows)

    return Report(
        name=str(header_map.get("ReportName") or ""),
        columns=columns,
        rows=tuple(rows),
        start_period=_parse_date(header_map.get("StartPeriod")),
        end_period=_parse_date(header_map.get("EndPeriod")),
        currency=str(header_map.get("Currency") or ""),
        basis=str(header_map.get("ReportBasis") or ""),
        summarize_by=str(header_map.get("SummarizeColumnsBy") or ""),
        raw=payload,
    )
