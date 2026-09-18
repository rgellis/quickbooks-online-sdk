"""Token lifecycle: rotation, persistence, single-flight refresh, redaction."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from qbo.auth import (
    TOKEN_ENDPOINT,
    AuthClient,
    FileTokenStore,
    MemoryTokenStore,
    TokenSet,
)
from qbo.errors import (
    QboAuthError,
    QboReauthorizationRequired,
    QboTokenPersistenceError,
    redact,
)


def _expired_tokens() -> TokenSet:
    return TokenSet(
        access_token="stale-access",
        refresh_token="refresh-v1",
        realm_id="9130347",
        access_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


def _fresh_tokens() -> TokenSet:
    return TokenSet(
        access_token="good-access",
        refresh_token="refresh-v1",
        realm_id="9130347",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=55),
    )


def _token_response(n: int) -> dict[str, Any]:
    return {
        "access_token": f"access-v{n}",
        "refresh_token": f"refresh-v{n}",
        "expires_in": 3600,
        "x_refresh_token_expires_in": 8726400,
        "token_type": "bearer",
    }


def _auth(store: Any, http: httpx.AsyncClient | None = None) -> AuthClient:
    return AuthClient(
        client_id="ABC123", client_secret="shh-secret", store=store, http=http
    )


class TestRotation:
    @respx.mock
    async def test_rotated_refresh_token_is_persisted_before_returning(
        self, tmp_path: Path
    ) -> None:
        """Intuit invalidates the old refresh token, so the new one must land on
        disk before the caller is allowed to proceed."""
        route = respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_response(2))
        )
        store = FileTokenStore(tmp_path / "tokens.json")
        await store.save(_expired_tokens())

        auth = _auth(store)
        token = await auth.access_token()

        assert token == "access-v2"
        assert route.called
        on_disk = json.loads((tmp_path / "tokens.json").read_text())
        assert on_disk["refresh_token"] == "refresh-v2", "rotation not persisted"
        assert on_disk["realm_id"] == "9130347", "realm carried across rotation"

    @respx.mock
    async def test_persisted_rotation_is_reused_by_a_new_client(
        self, tmp_path: Path
    ) -> None:
        """A restart must pick up the rotated token, not the original."""
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_response(2))
        )
        path = tmp_path / "tokens.json"
        await FileTokenStore(path).save(_expired_tokens())
        await _auth(FileTokenStore(path)).access_token()

        reloaded = await FileTokenStore(path).load()
        assert reloaded is not None
        assert reloaded.refresh_token == "refresh-v2"
        assert reloaded.access_token_valid

    @respx.mock
    async def test_persistence_failure_raises_rather_than_returning(self) -> None:
        """A rotation that cannot be saved has already invalidated the old
        token. Returning would strand the integration at the next restart."""
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_response(2))
        )

        class BrokenStore(MemoryTokenStore):
            async def save(self, tokens: TokenSet) -> None:
                raise OSError("disk full")

        auth = _auth(BrokenStore(_expired_tokens()))
        with pytest.raises(QboTokenPersistenceError) as exc:
            await auth.access_token()
        assert "re-authorizing" in str(exc.value)

    @respx.mock
    async def test_missing_refresh_token_in_response_is_an_error(self) -> None:
        """Intuit rotates on every refresh. A response without a new refresh
        token is malformed, and silently reusing the old one would be wrong."""
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200, json={"access_token": "a", "expires_in": 3600}
            )
        )
        with pytest.raises(QboAuthError, match="rotates the refresh token"):
            await _auth(MemoryTokenStore(_expired_tokens())).access_token()


class TestSingleFlight:
    @respx.mock
    async def test_concurrent_callers_trigger_exactly_one_refresh(self) -> None:
        """Two racing refreshes would have the second invalidate the first."""
        calls = 0

        async def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)  # widen the race window
            return httpx.Response(200, json=_token_response(2))

        respx.post(TOKEN_ENDPOINT).mock(side_effect=handler)
        auth = _auth(MemoryTokenStore(_expired_tokens()))

        tokens = await asyncio.gather(*(auth.access_token() for _ in range(12)))

        assert calls == 1, f"expected one token request, made {calls}"
        assert auth.refresh_count == 1
        assert set(tokens) == {"access-v2"}, "losers must see the winner's token"

    @respx.mock
    async def test_valid_cached_token_makes_no_request(self) -> None:
        route = respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(200, json=_token_response(2))
        )
        auth = _auth(MemoryTokenStore(_fresh_tokens()))
        assert await auth.access_token() == "good-access"
        assert not route.called


class TestFailureModes:
    @respx.mock
    async def test_invalid_grant_asks_for_human_reauthorization(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Token invalid or expired",
                },
            )
        )
        with pytest.raises(QboReauthorizationRequired) as exc:
            await _auth(MemoryTokenStore(_expired_tokens())).access_token()
        message = str(exc.value)
        assert "human must re-authorize" in message
        assert "get_refresh_token.py" in message

    async def test_empty_store_explains_the_bootstrap_step(self) -> None:
        with pytest.raises(QboAuthError, match="get_refresh_token.py"):
            await _auth(MemoryTokenStore(None)).access_token()

    def test_missing_credentials_fail_at_construction(self) -> None:
        with pytest.raises(QboAuthError, match="QBO_CLIENT_ID"):
            AuthClient(client_id="", client_secret="x", store=MemoryTokenStore())


class TestSecretHandling:
    def test_token_set_never_prints_its_secrets(self) -> None:
        rendered = f"{_fresh_tokens()!r} {_fresh_tokens()!s}"
        assert "good-access" not in rendered
        assert "refresh-v1" not in rendered
        assert "9130347" in rendered, "non-secret context should survive"

    @pytest.mark.parametrize(
        "payload",
        [
            {"access_token": "secret-value", "realm": "9130347"},
            {"Authorization": "Bearer abcdefghijklmnop"},
            {"nested": [{"client_secret": "hunter2"}]},
            "Authorization: Bearer abcdefghijklmnopqrstuv",
        ],
    )
    def test_redaction_removes_credentials(self, payload: Any) -> None:
        rendered = json.dumps(redact(payload))
        for leaked in ("secret-value", "abcdefghijklmnop", "hunter2"):
            assert leaked not in rendered
        assert "[redacted]" in rendered


class TestFileStore:
    async def test_write_is_atomic_and_leaves_no_temp_files(
        self, tmp_path: Path
    ) -> None:
        store = FileTokenStore(tmp_path / "tokens.json")
        await store.save(_fresh_tokens())
        await store.save(_expired_tokens())
        leftovers = [
            p.name for p in tmp_path.iterdir() if p.name.startswith(".qbo-token-")
        ]
        assert leftovers == [], f"temp files left behind: {leftovers}"
        loaded = await store.load()
        assert loaded is not None and loaded.access_token == "stale-access"

    async def test_missing_and_empty_files_load_as_none(self, tmp_path: Path) -> None:
        assert await FileTokenStore(tmp_path / "absent.json").load() is None
        empty = tmp_path / "empty.json"
        empty.write_text("")
        assert await FileTokenStore(empty).load() is None

    async def test_token_file_is_not_world_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        await FileTokenStore(path).save(_fresh_tokens())
        assert path.stat().st_mode & 0o077 == 0, "token file must be owner-only"

    async def test_the_lock_sidecar_is_not_world_readable(self, tmp_path: Path) -> None:
        """It holds nothing, but it sits next to a credential and should not be
        the one readable file in that directory."""
        store = FileTokenStore(tmp_path / "tokens.json")
        await store.save(_fresh_tokens())
        lock = tmp_path / "tokens.json.lock"
        assert lock.exists()
        assert lock.stat().st_mode & 0o077 == 0
