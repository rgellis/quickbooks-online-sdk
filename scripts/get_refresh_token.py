#!/usr/bin/env python
"""Complete the one-time QuickBooks authorization and seed the token store.

This is the script every ``QboReauthorizationRequired`` error points at, and the
only recovery path when a refresh token dies -- which happens when one is
reused after rotation, or goes ~100 days unused. Both surface as
``invalid_grant`` and neither can be recovered automatically.

Nothing here runs at server start. Authorization is a human action, once.

Usage, from the host:

    ./scripts/dev run python scripts/get_refresh_token.py

The script prints an authorization URL, you open it and approve, and Intuit
redirects to your registered redirect URI carrying ``code`` and ``realmId``.
Paste that whole redirect URL back in. Nothing listens on a port, which is
deliberate: this usually runs inside a container where binding the redirect URI
would mean publishing a port purely to receive one browser redirect.

To emit only the refresh token, for seeding a deployment:

    ./scripts/dev run python scripts/get_refresh_token.py --print-token

stdout is then the token and nothing else, so it can be piped:

    ... --print-token | aws ssm put-parameter --name /x/QBO_REFRESH_TOKEN \\
        --type SecureString --overwrite --value "$(cat)"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qbo.auth import FileTokenStore, TokenSet  # noqa: E402

DEFAULT_TOKEN_STORE = "/data/qbo-tokens.json"
DEFAULT_REDIRECT_URI = "http://localhost:8000/callback"


def _say(message: str = "") -> None:
    """Progress goes to stderr so --print-token leaves stdout clean."""
    print(message, file=sys.stderr)


def _require_env(name: str, override: str | None = None) -> str:
    value = override or os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"FATAL: {name} is not set. Copy .env.example to .env and fill it in, "
            "or pass the matching command-line option."
        )
    return value


def _extract(pasted: str) -> tuple[str, str]:
    """Pull ``code`` and ``realmId`` out of whatever the user pasted.

    Accepts the full redirect URL, a bare query string, or just the code. The
    first two are what a browser actually gives you; the third is what Intuit's
    OAuth Playground shows.
    """
    text = pasted.strip().strip('"').strip("'")
    if not text:
        raise SystemExit("FATAL: nothing pasted.")

    if "code=" in text:
        query = urlparse(text).query or text
        params = parse_qs(query)
        code = (params.get("code") or [""])[0]
        realm = (params.get("realmId") or params.get("realmid") or [""])[0]
        if not code:
            raise SystemExit(f"FATAL: no 'code' parameter found in: {text[:120]}")
        return code, realm
    return text, ""


async def _persist(store_path: str, tokens: TokenSet) -> None:
    await FileTokenStore(store_path).save(tokens)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize the app once and seed the QuickBooks token store."
    )
    parser.add_argument("--client-id", help="Overrides QBO_CLIENT_ID.")
    parser.add_argument("--client-secret", help="Overrides QBO_CLIENT_SECRET.")
    parser.add_argument(
        "--redirect-uri",
        help=(
            "Must exactly match one registered on the Intuit app. "
            f"Defaults to QBO_REDIRECT_URI, else {DEFAULT_REDIRECT_URI}."
        ),
    )
    parser.add_argument(
        "--token-store",
        help=f"Where to write. Defaults to QBO_TOKEN_STORE, else {DEFAULT_TOKEN_STORE}.",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print only the refresh token to stdout; do not write the store.",
    )
    args = parser.parse_args()

    client_id = _require_env("QBO_CLIENT_ID", args.client_id)
    client_secret = _require_env("QBO_CLIENT_SECRET", args.client_secret)
    redirect_uri = (
        args.redirect_uri or os.environ.get("QBO_REDIRECT_URI") or DEFAULT_REDIRECT_URI
    )
    store_path = (
        args.token_store or os.environ.get("QBO_TOKEN_STORE") or DEFAULT_TOKEN_STORE
    )

    try:
        from intuitlib.client import AuthClient
        from intuitlib.enums import Scopes
    except ImportError:
        raise SystemExit(
            "FATAL: intuit-oauth is not installed. It lives in the 'bootstrap' "
            "and 'dev' extras:  uv sync --extra bootstrap"
        ) from None

    state = secrets.token_urlsafe(24)
    # Production only. The sandbox is a different company with different data,
    # and a token issued there will not work against the real ledger.
    client: Any = AuthClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        environment="production",
        state_token=state,
    )
    url = str(client.get_authorization_url([Scopes.ACCOUNTING], state_token=state))

    _say("=" * 72)
    _say("1. Open this URL and approve access to the company:")
    _say()
    _say(f"   {url}")
    _say()
    _say(f"2. Intuit redirects to {redirect_uri} carrying ?code=...&realmId=...")
    _say("   Nothing is listening there, so the browser will show an error page.")
    _say("   That is expected -- the URL in the address bar is what matters.")
    _say()
    _say("3. Paste that whole URL here.")
    _say("=" * 72)
    _say()

    try:
        pasted = input("Redirect URL (or bare code): ")
    except (EOFError, KeyboardInterrupt):
        _say("\nAborted.")
        return 1

    code, realm_id = _extract(pasted)
    realm_id = realm_id or os.environ.get("QBO_REALM_ID", "")
    if not realm_id:
        raise SystemExit(
            "FATAL: no realmId in the redirect and QBO_REALM_ID is not set. "
            "The realm identifies which company file the tokens are for."
        )

    _say("\nExchanging the authorization code...")
    try:
        client.get_bearer_token(code, realm_id=realm_id)
    except Exception as exc:  # intuitlib raises its own exception types
        raise SystemExit(
            f"FATAL: token exchange failed: {exc}\n"
            "       An authorization code is single-use and expires in minutes. "
            "Re-run and paste a fresh one.\n"
            "       A redirect_uri mismatch also lands here: it must match the "
            "Intuit app registration exactly."
        ) from None

    refresh_token = str(client.refresh_token or "")
    if not refresh_token:
        raise SystemExit("FATAL: Intuit returned no refresh token.")

    if args.print_token:
        # stdout gets the token and nothing else, so this can be piped.
        print(refresh_token)
        _say("\nPrinted the refresh token. The token store was NOT written.")
        _say("This token rotates on first use -- seed with it, do not keep it.")
        return 0

    tokens = TokenSet(
        access_token=str(client.access_token or ""),
        refresh_token=refresh_token,
        realm_id=str(client.realm_id or realm_id),
    )
    asyncio.run(_persist(store_path, tokens))

    _say()
    _say("=" * 72)
    _say(f"Wrote {store_path}")
    _say(f"  realm_id      : {tokens.realm_id}")
    _say(f"  refresh_token : [redacted, {len(refresh_token)} chars]")
    _say()
    _say("Intuit rotates this token on every refresh and invalidates the old")
    _say("one, so this file is now the only live copy of the credential.")
    _say("Back it up, and do not re-seed over it from a stale value.")
    _say("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
