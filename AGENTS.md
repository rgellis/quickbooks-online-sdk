## Objective

Typed Python client for the QuickBooks Online Accounting API v3. No official
Python SDK exists — Intuit ships .NET, Java and PHP only — so this package is
written directly against the REST API.

## Architecture

- `src/qbo/auth.py` — OAuth, refresh-token rotation, single-flight refresh, atomic token store
- `src/qbo/client.py` — request/retry/backoff, transparent query paging, entity CRUD, reports
- `src/qbo/entities.py` — **GENERATED.** Entity and report registry
- `src/qbo/models/` — **GENERATED.** Pydantic models, 119 classes and 20 enums
- `src/qbo/errors.py` — exception hierarchy and secret redaction
- `refs/docs/` — vendored documentation JSON (primary source)
- `refs/dotnet/`, `refs/java/` — vendored XSD from Intuit's own SDKs (cross-check)
- `scripts/refresh_sources.py` — re-vendor all three sources
- `tools/generate_entities.py` — regenerate the registry from `refs/docs/`
- `tools/generate_models.py` — regenerate the Pydantic models
- `tools/audit_coverage.py` — reconcile sources, write `API_COVERAGE.md`
- `tools/docsource.py` — shared JSON traversal and field-name normalisation

## Rules

1. **Everything runs in Docker.** `./scripts/dev check` is the entry point.
   There is no Python toolchain on the host and none is required.
2. **Never hand-edit generated code** — `src/qbo/entities.py` or `src/qbo/models/`.
   Run the generators. The hand-written registry invented entities that do not
   exist. The tests regenerate and fail on any difference.
3. **Never take one source as the API's definition.** Coverage is defined by the
   documentation; the XSDs are a cross-check. Changes go through the audit.
4. **Never hardcode a git branch when vendoring.** Pulling the Java SDK from
   `master` instead of its default `develop` silently yielded a schema 20 types
   stale and manufactured a disagreement that did not exist. The refresh script
   resolves default branches from the API and records what it used.
5. pyright runs in **strict** mode. Decoded JSON is `Any`; narrow it through the
   helpers in `tools/docsource.py` rather than indexing raw values.
6. Every error path goes through `redact()`. Tokens must never reach a log, an
   exception message or a response.

## Critical rules

- **A rotated refresh token must be persisted before it is returned.** Intuit has
  already invalidated the previous one by that point, so an unpersisted rotation
  strands the integration at the next restart. `QboTokenPersistenceError` exists
  for this and must not be softened into a warning.
- **One 401 triggers exactly one forced refresh and one retry.** A second 401 is
  an authorization problem; retrying burns refresh tokens for nothing.
- **Never infer an account's role from its name.** Use `AcctNum` and
  `AccountType`. QuickBooks files routinely name bank accounts "Receivable".
- **Report names are not route names.** Use `report_route()`; nine reports are
  documented under a name the URL rejects.
- **Money is `Decimal`, never `float`.** Responses decode with
  `parse_float=Decimal`; do not reintroduce `response.json()`.
- **Query results are capped at 1,000 rows** regardless of `MAXRESULTS`.
  `QboClient.query` pages transparently; do not reimplement that at call sites.
