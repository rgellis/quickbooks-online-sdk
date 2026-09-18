"""OAuth 2.0 token handling for QuickBooks Online.

Intuit issues a **new refresh token on every refresh** and invalidates the old
one. That single fact drives the design here:

* the rotated token is persisted **before** the refresh result is returned, and
  a persistence failure raises rather than returning a token that exists only in
  memory (:class:`QboTokenPersistenceError`);
* refreshes are single-flighted behind a lock, so concurrent callers produce
  exactly one token request -- two racing refreshes would have the second
  invalidate the first's token;
* writes are atomic (temp file, fsync, rename) and hold an advisory lock, so a
  crash mid-write cannot truncate the store.

Access tokens last about an hour and are cached in memory. Refresh tokens expire
after roughly 100 days of disuse; recovery from that is a human walking the
authorization-code flow, which is what :class:`QboReauthorizationRequired` says.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Protocol, cast

import httpx
from pydantic import BaseModel, Field

from qbo.errors import (
    QboAuthError,
    QboReauthorizationRequired,
    QboTokenPersistenceError,
    redact,
)

__all__ = ["TokenSet", "TokenStore", "FileTokenStore", "MemoryTokenStore", "AuthClient"]

TOKEN_ENDPOINT: Final[str] = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_ENDPOINT: Final[str] = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

#: Refresh this many seconds before the access token actually expires, so a
#: request never sets off with a token that dies in flight.
_EXPIRY_SKEW = timedelta(seconds=120)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TokenSet(BaseModel):
    """The credential state that must survive a restart."""

    access_token: str
    refresh_token: str
    realm_id: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    rotated_at: datetime = Field(default_factory=_utcnow)

    @property
    def access_token_valid(self) -> bool:
        """True when the cached access token is safe to use right now."""
        if not self.access_token or self.access_token_expires_at is None:
            return False
        return _utcnow() + _EXPIRY_SKEW < self.access_token_expires_at

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"TokenSet(realm_id={self.realm_id!r}, "
            f"access_token='[redacted]', refresh_token='[redacted]', "
            f"access_token_expires_at={self.access_token_expires_at!r})"
        )

    __str__ = __repr__


class TokenStore(Protocol):
    """Somewhere a :class:`TokenSet` survives process restarts."""

    async def load(self) -> TokenSet | None: ...

    async def save(self, tokens: TokenSet) -> None: ...


class MemoryTokenStore:
    """In-process store. For tests -- rotations are lost on exit."""

    def __init__(self, tokens: TokenSet | None = None) -> None:
        self._tokens = tokens
        self.save_count = 0

    async def load(self) -> TokenSet | None:
        return self._tokens

    async def save(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        self.save_count += 1


class FileTokenStore:
    """A JSON file, written atomically under an advisory lock.

    ``save`` writes to a temp file in the same directory, fsyncs it, then
    ``os.replace``s it over the target -- an atomic operation on POSIX. The
    directory is fsynced afterwards so the rename itself is durable. A sidecar
    ``.lock`` file serialises writers across processes, which matters when more
    than one worker shares a token store.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    async def load(self) -> TokenSet | None:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return TokenSet.model_validate_json(raw)

    async def save(self, tokens: TokenSet) -> None:
        await asyncio.to_thread(self._save_sync, tokens)

    def _save_sync(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = tokens.model_dump_json(indent=2)
        # The sidecar carries no content, but it sits beside a credential and
        # should not be the one file in that directory anyone can read. Opened
        # with an explicit mode rather than left to the umask.
        lock_fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self.path.parent), prefix=".qbo-token-", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(tmp_name, 0o600)
                    os.replace(tmp_name, self.path)
                except BaseException:
                    # Never leave a partial file behind for the next reader.
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                    raise
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class AuthClient:
    """Produces valid access tokens, refreshing and rotating as needed."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        store: TokenStore,
        http: httpx.AsyncClient | None = None,
        token_endpoint: str = TOKEN_ENDPOINT,
    ) -> None:
        if not client_id or not client_secret:
            raise QboAuthError("QBO_CLIENT_ID and QBO_CLIENT_SECRET must both be set.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = store
        self._token_endpoint = token_endpoint
        self._http = http
        self._owns_http = http is None
        self._tokens: TokenSet | None = None
        self._lock = asyncio.Lock()
        #: Counts token-endpoint round trips. Tests assert concurrent callers
        #: produce exactly one.
        self.refresh_count = 0

    @property
    def _basic_auth(self) -> str:
        pair = f"{self._client_id}:{self._client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(pair).decode("ascii")

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, refreshing only when necessary.

        Concurrent callers arriving while a refresh is in flight wait on the
        same refresh rather than starting their own: the winner does the work,
        the losers re-check under the lock and find a fresh token waiting.
        """
        if not force_refresh:
            cached = await self._current()
            if cached is not None and cached.access_token_valid:
                return cached.access_token

        async with self._lock:
            # Double-checked: another coroutine may have refreshed while this
            # one waited for the lock. Re-reading here is what makes the refresh
            # single-flight rather than merely serialised.
            current = await self._current()
            if not force_refresh and current is not None and current.access_token_valid:
                return current.access_token
            if current is None:
                raise QboAuthError(
                    "No QuickBooks tokens found in the token store. Run "
                    "scripts/get_refresh_token.py to complete the one-time "
                    "authorization and seed it."
                )
            refreshed = await self._refresh_locked(current)
            return refreshed.access_token

    async def _current(self) -> TokenSet | None:
        if self._tokens is None:
            self._tokens = await self._store.load()
        return self._tokens

    async def _refresh_locked(self, current: TokenSet) -> TokenSet:
        """Exchange the refresh token. Caller must hold ``self._lock``."""
        client = await self._client()
        try:
            response = await client.post(
                self._token_endpoint,
                headers={
                    "Authorization": self._basic_auth,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            raise QboAuthError(
                f"Token refresh could not reach Intuit: {redact(str(exc))}"
            ) from exc

        self.refresh_count += 1
        payload = self._decode(response)

        if response.status_code != 200:
            error = str(payload.get("error", "")) if payload else ""
            if error == "invalid_grant" or response.status_code == 400:
                raise QboReauthorizationRequired(
                    str(payload.get("error_description") or error or response.text)
                )
            raise QboAuthError(
                f"Token refresh failed: HTTP {response.status_code} "
                f"{redact(payload or response.text)}"
            )

        rotated = self._token_set_from(payload, previous=current)

        # Persist before returning. Intuit has already invalidated
        # current.refresh_token, so an unpersisted rotation is a token that
        # exists only in this process -- a restart would strand the integration.
        try:
            await self._store.save(rotated)
        except Exception as exc:
            raise QboTokenPersistenceError(
                "Refreshed the QuickBooks token but could not persist the "
                "rotated refresh token. The previous refresh token is now "
                "invalid, so this must be resolved before the process exits or "
                "the app will need re-authorizing by hand. "
                f"Store error: {redact(str(exc))}"
            ) from exc

        self._tokens = rotated
        return rotated

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            decoded: Any = response.json()
        except ValueError:
            return {}
        return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else {}

    @staticmethod
    def _token_set_from(payload: dict[str, Any], *, previous: TokenSet) -> TokenSet:
        now = _utcnow()
        access = str(payload.get("access_token") or "")
        if not access:
            raise QboAuthError(
                f"Token endpoint returned 200 with no access_token: {redact(payload)}"
            )
        # Intuit returns a rotated refresh token on every successful refresh.
        # Falling back to the previous value would be wrong if it were ever
        # absent, so treat absence as the anomaly it is rather than silently
        # reusing a token Intuit has just invalidated.
        refresh = str(payload.get("refresh_token") or "")
        if not refresh:
            raise QboAuthError(
                "Token endpoint returned no refresh_token. Intuit rotates the "
                "refresh token on every refresh, so this response cannot be "
                "trusted: " + str(redact(payload))
            )

        def _expiry(key: str) -> datetime | None:
            seconds = payload.get(key)
            if isinstance(seconds, (int, float)):
                return now + timedelta(seconds=float(seconds))
            return None

        return TokenSet(
            access_token=access,
            refresh_token=refresh,
            realm_id=previous.realm_id,
            access_token_expires_at=_expiry("expires_in"),
            refresh_token_expires_at=_expiry("x_refresh_token_expires_in"),
            rotated_at=now,
        )
