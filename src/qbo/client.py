"""HTTP client for the QuickBooks Online Accounting API v3.

Everything that talks to Intuit goes through :meth:`QboClient.request`, which
owns the cross-cutting behaviour:

* ``minorversion`` on every request -- the API is versioned by it, and omitting
  it silently pins you to an old field set;
* exactly one retry after a 401, following exactly one forced token refresh
  (a second 401 means the problem is not token staleness);
* exponential backoff with full jitter on 429 and 5xx;
* the ``intuit_tid`` response header preserved on every error, because it is
  what Intuit support asks for.

Query paging is transparent. The API caps any query at 1,000 rows regardless of
what ``MAXRESULTS`` asks for, so :meth:`query` walks ``STARTPOSITION`` until a
short page arrives and returns the concatenation.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from decimal import Decimal
from typing import Any, Final, Literal, Mapping, Sequence, TypeVar, cast

import httpx

from qbo.auth import AuthClient
from qbo.entities import report_route
from qbo.errors import QboApiError, QboError, QboRateLimitError, redact
from qbo.models import QboModel, model_for
from qbo.reports import Report, check_report, parse_report

__all__ = ["QboClient", "PRODUCTION_BASE_URL", "SANDBOX_BASE_URL", "MAX_PAGE_SIZE"]

M = TypeVar("M", bound=QboModel)

PRODUCTION_BASE_URL: Final[str] = "https://quickbooks.api.intuit.com"
SANDBOX_BASE_URL: Final[str] = "https://sandbox-quickbooks.api.intuit.com"

#: The API will not return more than this many rows from one query, whatever
#: MAXRESULTS says. Paging past it is the caller's problem, so we do it here.
MAX_PAGE_SIZE: Final[int] = 1000

DEFAULT_MINOR_VERSION: Final[int] = 75

_RETRY_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_PAGING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(STARTPOSITION|MAXRESULTS)\b", re.IGNORECASE
)
_WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _decode_json(text: str) -> Any:
    """Decode a response body with money kept exact.

    ``parse_float=Decimal`` keeps every JSON number out of binary floating point
    entirely. Pydantic happens to round-trip floats correctly for realistic
    amounts, but relying on that would make correctness of the ledger depend on
    an implementation detail of a third-party conversion.
    """
    return json.loads(text, parse_float=Decimal)


def _json_objects(value: Any) -> list[dict[str, Any]]:
    """Narrow an arbitrary decoded JSON value to a list of JSON objects."""
    if not isinstance(value, list):
        return []
    return [
        cast(dict[str, Any], item)
        for item in cast(list[Any], value)
        if isinstance(item, dict)
    ]


class QboClient:
    """A typed, paging, retrying client bound to one company (realm)."""

    def __init__(
        self,
        *,
        realm_id: str,
        auth: AuthClient,
        minor_version: int = DEFAULT_MINOR_VERSION,
        environment: Literal["production", "sandbox"] = "production",
        http: httpx.AsyncClient | None = None,
        read_only: bool = False,
        max_retries: int = 4,
        timeout: float = 60.0,
    ) -> None:
        if not realm_id:
            raise QboError("realm_id is required -- it identifies the company file.")
        self.realm_id = realm_id
        self.minor_version = minor_version
        self.environment = environment
        self.read_only = read_only
        self.base_url = (
            PRODUCTION_BASE_URL if environment == "production" else SANDBOX_BASE_URL
        )
        self._auth = auth
        self._http = http
        self._owns_http = http is None
        self._max_retries = max_retries
        self._timeout = timeout

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        await self._auth.aclose()

    async def __aenter__(self) -> QboClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- request

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Issue one authenticated request and return the decoded JSON body."""
        method = method.upper()
        if self.read_only and method in _WRITE_METHODS:
            raise QboError(
                f"Refusing {method} {path}: this client is in read-only mode. "
                "Set read_only=False (QBO_READ_ONLY=false) to permit writes."
            )

        url = f"{self.base_url}/v3/company/{self.realm_id}/{path.lstrip('/')}"
        query: dict[str, Any] = {"minorversion": str(self.minor_version)}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})

        client = await self._client()
        attempt = 0
        refreshed = False

        while True:
            token = await self._auth.access_token(force_refresh=refreshed)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if content is not None:
                headers["Content-Type"] = content_type or "application/text"
            elif json_body is not None:
                headers["Content-Type"] = "application/json"

            try:
                response = await client.request(
                    method,
                    url,
                    params=query,
                    json=json_body,
                    content=content,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise QboError(
                        f"{method} {redact(url)} failed after {attempt + 1} "
                        f"attempts: {redact(str(exc))}"
                    ) from exc
                await self._sleep_backoff(attempt)
                attempt += 1
                continue

            # One 401 means the cached access token went stale; force exactly
            # one refresh and retry. A second 401 is a real authorization
            # problem and retrying would only burn refresh tokens.
            if response.status_code == 401 and not refreshed:
                refreshed = True
                continue

            if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                await self._sleep_backoff(attempt, response)
                attempt += 1
                continue

            if response.status_code >= 400:
                raise self._error_for(response, method, url)

            if not response.content:
                return {}
            decoded = _decode_json(response.text)
            if isinstance(decoded, dict):
                return cast(dict[str, Any], decoded)
            return {"value": decoded}

    def _error_for(
        self, response: httpx.Response, method: str, url: str
    ) -> QboApiError:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text[:2000]
        tid = response.headers.get("intuit_tid")
        if response.status_code == 429:
            hint = response.headers.get("Retry-After")
            return QboRateLimitError(
                retry_after=float(hint) if hint and hint.isdigit() else None,
                status_code=response.status_code,
                method=method,
                url=url,
                body=body,
                intuit_tid=tid,
            )
        return QboApiError(
            status_code=response.status_code,
            method=method,
            url=url,
            body=body,
            intuit_tid=tid,
        )

    async def _sleep_backoff(
        self, attempt: int, response: httpx.Response | None = None
    ) -> None:
        """Exponential backoff with full jitter, honouring Retry-After."""
        if response is not None:
            hint = response.headers.get("Retry-After")
            if hint and hint.isdigit():
                await asyncio.sleep(min(float(hint), 60.0))
                return
        ceiling = min(2.0**attempt, 30.0)
        await asyncio.sleep(random.uniform(0.0, ceiling))

    # ------------------------------------------------------------------ query

    async def query(
        self, statement: str, *, page: bool = True, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Run a QBO SQL statement, paging past the 1,000-row cap.

        Args:
            statement: e.g. ``SELECT * FROM Invoice WHERE TxnDate >= '2026-08-01'``.
            page: When False, issue exactly one request and return that page.
            limit: Stop once this many rows have been collected.

        Returns:
            The concatenated entity rows. An empty list when nothing matched --
            QuickBooks omits the entity key entirely rather than returning [].
        """
        statement = statement.strip().rstrip(";")
        if _PAGING_RE.search(statement):
            # The caller is driving paging themselves; respect that exactly.
            return self._rows(
                await self.request("GET", "query", params={"query": statement})
            )

        rows: list[dict[str, Any]] = []
        start = 1
        while True:
            size = MAX_PAGE_SIZE
            if limit is not None:
                size = min(size, limit - len(rows))
                if size <= 0:
                    break
            paged = f"{statement} STARTPOSITION {start} MAXRESULTS {size}"
            payload = await self.request("GET", "query", params={"query": paged})
            batch = self._rows(payload)
            rows.extend(batch)
            if not page or len(batch) < size:
                break
            start += len(batch)
        return rows

    @staticmethod
    def _rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Pull the entity array out of a QueryResponse envelope.

        QuickBooks keys the array by entity name (``QueryResponse.Invoice``) and
        omits the key entirely when there are no matches, so the absence of a
        key is a legitimate empty result rather than a malformed response.
        """
        envelope = payload.get("QueryResponse")
        if not isinstance(envelope, Mapping):
            return []
        for key, value in cast(Mapping[str, Any], envelope).items():
            if key in {"startPosition", "maxResults", "totalCount"}:
                continue
            if isinstance(value, list):
                return _json_objects(value)
        return []

    # ----------------------------------------------------------------- entity

    async def get(self, entity: str, entity_id: str) -> dict[str, Any]:
        """Read one entity by id."""
        payload = await self.request("GET", f"{entity.lower()}/{entity_id}")
        return self._unwrap(payload, entity)

    async def create(self, entity: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Create an entity."""
        payload = await self.request("POST", entity.lower(), json_body=dict(body))
        return self._unwrap(payload, entity)

    async def update(
        self, entity: str, body: Mapping[str, Any], *, sparse: bool = False
    ) -> dict[str, Any]:
        """Update an entity.

        A full update replaces every field, so ``Id`` and the current
        ``SyncToken`` are both required and any field omitted is cleared.
        ``sparse=True`` sends a partial update instead, changing only the fields
        present in ``body``.
        """
        data = dict(body)
        missing = [k for k in ("Id", "SyncToken") if not data.get(k)]
        if missing:
            raise QboError(
                f"Updating {entity} requires {' and '.join(missing)}. "
                "Read the current record first -- SyncToken is QuickBooks' "
                "optimistic-concurrency check and a stale one is rejected."
            )
        if sparse:
            data["sparse"] = True
        payload = await self.request("POST", entity.lower(), json_body=data)
        return self._unwrap(payload, entity)

    async def delete(self, entity: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Delete an entity. Requires ``Id`` and ``SyncToken``."""
        payload = await self.request(
            "POST", entity.lower(), params={"operation": "delete"}, json_body=dict(body)
        )
        return self._unwrap(payload, entity)

    async def void(self, entity: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Void a transaction, keeping the record with zeroed amounts."""
        payload = await self.request(
            "POST", entity.lower(), params={"operation": "void"}, json_body=dict(body)
        )
        return self._unwrap(payload, entity)

    @staticmethod
    def _unwrap(payload: Mapping[str, Any], entity: str) -> dict[str, Any]:
        value = payload.get(entity) or payload.get(entity.capitalize())
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        return dict(payload)

    async def get_as(self, model: type[M], entity: str, entity_id: str) -> M:
        """Read one entity and parse it into ``model``.

        The raw dictionary remains available through :meth:`get`; this is the
        parsed path, and it validates rather than casts.
        """
        return model.model_validate(await self.get(entity, entity_id))

    async def get_model(self, entity: str, entity_id: str) -> QboModel:
        """Read one entity, parsed into the model the docs define for it."""
        return await self.get_as(model_for(entity), entity, entity_id)

    async def query_as(
        self,
        model: type[M],
        statement: str,
        *,
        page: bool = True,
        limit: int | None = None,
    ) -> list[M]:
        """Run a query and parse every row into ``model``."""
        rows = await self.query(statement, page=page, limit=limit)
        return [model.model_validate(row) for row in rows]

    async def query_models(
        self, entity: str, statement: str | None = None, *, limit: int | None = None
    ) -> list[QboModel]:
        """Query one entity, parsed. Defaults to selecting everything."""
        return await self.query_as(
            model_for(entity), statement or f"SELECT * FROM {entity}", limit=limit
        )

    # ----------------------------------------------------------------- report

    async def report(self, name: str, **params: Any) -> dict[str, Any]:
        """Fetch a report, accepting either its documented or route name.

        Nine reports are filed in the documentation under a name the URL does
        not take -- ``APAgingDetail`` is fetched from ``/reports/AgedPayableDetail``
        -- so the name is resolved rather than passed through. Using the
        documented name directly returns HTTP 400.
        """
        return await self.request("GET", f"reports/{report_route(name)}", params=params)

    async def report_as(
        self, name: str, *, validate: bool = True, **params: Any
    ) -> Report:
        """Fetch a report, flatten it, and check its arithmetic.

        QuickBooks returns reports as a nested tree in which every section
        restates its own totals. This returns flat rows instead, and by default
        refuses to hand back a report that contradicts itself: a section whose
        stated total its own line items disprove raises
        :class:`~qbo.errors.QboInvariantError` rather than being returned.

        Args:
            name: Documented report name or its route name; either resolves.
            validate: Setting this False returns the report unchecked. Only
                reasonable when the goal is to inspect one already known to be
                inconsistent.
            **params: Report parameters, passed through untouched.
        """
        parsed = parse_report(await self.report(name, **params))
        return check_report(parsed) if validate else parsed

    # ------------------------------------------------------------ batch / cdc

    async def batch(self, items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Submit a batch. The API accepts at most 30 operations per call."""
        if len(items) > 30:
            raise QboError(
                f"Batch accepts at most 30 operations, got {len(items)}. "
                "Split the work across calls."
            )
        payload = await self.request(
            "POST", "batch", json_body={"BatchItemRequest": list(items)}
        )
        return _json_objects(payload.get("BatchItemResponse"))

    async def cdc(self, entities: Sequence[str], changed_since: str) -> dict[str, Any]:
        """Change data capture: what changed since a timestamp."""
        return await self.request(
            "GET",
            "cdc",
            params={"entities": ",".join(entities), "changedSince": changed_since},
        )
