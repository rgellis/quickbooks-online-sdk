# quickbooks-online-sdk

Typed Python client for the **QuickBooks Online Accounting API v3**, with API
coverage that is mechanically verified rather than claimed.

Intuit publishes V3 SDKs for .NET, Java and PHP. Python gets `intuit-oauth` — an
OAuth helper — and no accounting client at all. This is that missing client,
written directly against the REST API so fields added in a new `minorversion`
are reachable without waiting for an SDK release.

## Why coverage is verified, not asserted

Intuit publishes no OpenAPI or discovery document for this API, and the
available sources disagree with each other. Building on any one of them
silently inherits its errors:

| Source | What it gets wrong on its own |
|---|---|
| Either SDK XSD | declares 27 types with no REST endpoint — `SalesOrder`, `PriceLevel` and other QuickBooks Desktop concepts |
| Either SDK XSD | reports every field as `minOccurs=0`, so it cannot say what is required |
| Docs alone | abridged field lists; names 9 reports differently from the URL that serves them |
| Any SDK's `master` | not necessarily its default branch — the Java SDK defaults to `develop`, and its `master` is 20 complexTypes behind |

So all three are vendored into `refs/`, the registry is **generated** from the
documentation, and `tools/audit_coverage.py` reconciles the sources and reports
every disagreement. `API_COVERAGE.md` is the output. The test suite fails if any
of it drifts.

Current state: **43 entities, 29 reports, 119 models, 20 enums** — zero coverage gaps.

### Things this caught that hand-writing got wrong

- The entity is `CreditCardPayment`; only the XSD calls it `CreditCardPaymentTxn`.
- It is `Exchangerate`, with a lowercase `r`.
- Nine reports are served from a different name than the docs file them under.
  `APAgingDetail` lives at `/reports/AgedPayableDetail`; using the documented
  name returns HTTP 400. `report_route()` resolves either spelling.
- `TrialBalance` has a France-locale variant, `TrialBalanceFR`.
- `Budget` accepts full CRUD, not read-only.
- Seven entities have fields Intuit documents that neither SDK schema models —
  `JournalEntry.TaxRateRef`, `Payment.TaxExemptionRef` among them.
- Vendoring from `master` made Intuit's two SDKs appear to disagree by 20 types.
  They do not: the Java SDK's default branch is `develop`, and on their actual
  default branches the two schemas are byte-identical. `scripts/refresh_sources.py`
  now resolves each repository's default branch from the GitHub API rather than
  assuming, and records the branch and commit in `refs/SOURCES.json`.

## Typed models

Every documented entity has a Pydantic model, generated from the same sources:

```python
from qbo.models import Invoice, model_for

invoice = Invoice.model_validate(payload)
invoice.TotalAmt  # Decimal('2780.92') -- never a float
invoice.TxnDate  # datetime.date(2026, 8, 14)
invoice.Line[0]  # SalesItemLine, resolved from a 5-way union
invoice.model_extra  # fields QuickBooks added that the docs do not list
```

Each source supplies what only it can: the documentation gives the object graph,
field requirements and cardinality; the XSD supplies enum *values*, which the
docs name but never list. Neither generates these models alone.

Design decisions worth knowing:

- **Money is `Decimal` everywhere**, and responses are decoded with
  `parse_float=Decimal` so no amount passes through binary floating point.
- **Every field is optional.** Partial reads, sparse updates and locale
  differences all produce valid responses missing documented fields; a model
  that rejects them is worse than one that reports absence. Request-side
  requirements live on `EntitySpec` instead.
- **Unknown fields are kept**, reachable via `model_extra`. Dropping them is how
  a field that exists on the wire becomes invisible after a minorversion bump.
- **Classes are defined with a trailing underscore and exported as aliases.**
  QuickBooks names fields after their own types (`Invoice.CurrencyRef` of type
  `CurrencyRef`), which would otherwise shadow the annotation and break it.

### Documentation defects the generator reports

Rather than silently resolving them, `tools/generate_models.py` prints both:

- `TimeActivity` declares a field literally named `"BreakHours BreakMinutes"`,
  which is not a wire key. Skipped.
- `Entitlements` declares both `Entitlement [0..n]` (with no definition) and
  `Entitlement` (typed `TelephoneNumber`). The first wins; the clash is reported.

## Reports, and the arithmetic that must hold

QuickBooks returns reports as a nested tree in which every section restates its
own totals. `parse_report` flattens that; the invariants then compare what each
section *claims* against what its children actually *sum to*, and raise when
they disagree.

```python
report = await client.report_as("ProfitAndLoss", start_date=..., end_date=...)
# QboInvariantError: ProfitAndLoss failed invariant for 2026-08-01 to 2026-08-31:
#   section 'Expenses' reports 0.00 but its 3 line items sum to 37,551.89
#   (difference 37,551.89)
```

Three checks run before a report is returned:

- **section totals against line items** — the defect that produced three false
  findings from the connector this replaces;
- **assets against liabilities plus equity**, both against the stated combined
  total and against the two sections summing to it, because a sheet can balance
  at the top while its own equity rows contradict the total beneath;
- **period columns against the total column** — months that do not add up to
  their own annual figure.

A failed invariant is an error, never a warning. Returning a plausible number
with a caveat puts the burden of noticing on the reader, which is the failure
being prevented. `report_as(..., validate=False)` exists for deliberately
inspecting a report already known to be inconsistent.

Two distinctions the checks depend on:

- **Blank is not zero.** A section reporting a blank total with no line items is
  a period with no activity. The same blank total *with* line items beneath it
  is a wrong number. Intuit's own sample P&L contains the first case, so
  conflating them would make the check unusable.
- **Computed sections are not containers.** Gross Profit, Net Operating Income
  and Net Income carry totals and legitimately have no children.

## Authorizing

```bash
./scripts/dev run python scripts/get_refresh_token.py
```

Prints an authorization URL, takes the redirect URL back, and writes the token
store. Nothing listens on a port, so it works unchanged inside a container.

`--print-token` emits only the refresh token on stdout, for seeding a deployment.

## Requirements

Docker. Nothing else — no Python, `uv` or Homebrew on the host.

```bash
./scripts/dev check            # ruff + pyright (strict) + pytest
./scripts/dev test -k paging   # pytest, arguments passed through
./scripts/dev shell            # interactive container shell
```

## Refreshing from upstream

```bash
./scripts/dev run python scripts/refresh_sources.py    # vendor docs + both XSDs
./scripts/dev run python tools/generate_entities.py    # regenerate the registry
./scripts/dev run python tools/generate_models.py      # regenerate the models
./scripts/dev run python tools/audit_coverage.py       # rewrite API_COVERAGE.md
./scripts/dev check                                    # confirm nothing drifted
```

`src/qbo/entities.py` and `src/qbo/models/` are generated. Do not edit by hand —
the test suite regenerates and fails on any difference.

## Token handling

Intuit issues a **new refresh token on every refresh** and invalidates the old
one, so `src/qbo/auth.py` is built around that single fact:

- the rotated token is persisted *before* the refresh result is returned, and a
  failed write raises rather than handing back a token that exists only in
  memory;
- refreshes are single-flighted, so concurrent callers make exactly one token
  request — two racing refreshes would have the second invalidate the first;
- writes are atomic (temp file, fsync, rename) under an advisory lock.

Lose a rotation and the only recovery is a human re-authorizing the app.

## Configuration

Copy `.env.example` to `.env`. See that file for what each variable does.

## Testing

```
183 tests · 100% statement and branch coverage · pyright strict, 0 errors
```

Coverage is pinned at 100 in `pyproject.toml`, statements and branches. This
package exists because numbers were trusted that nobody had checked; an
untested branch in the code doing the checking is the same failure one level
down. New code arrives with its tests or the suite fails.

Report fixtures under `tests/fixtures/` are Intuit's own published sample
responses, extracted from the vendored docs. The deliberately malformed reports
used by the invariant tests are built inline, next to the assertion that catches
them.

## Status

The SDK is complete: client, token handling, entity registry, typed models,
report parsing and invariants, and the coverage tooling that keeps all of it
honest. The MCP server built on it lives in a separate repository.

## License

MIT
