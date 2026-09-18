"""Exceptions and secret redaction.

Every error raised by this package carries enough context to diagnose the
failure -- what was attempted, what came back -- and nothing that could leak a
credential. ``redact`` is applied on every path that formats a message, and the
tests assert that tokens never survive into ``str(exc)``.
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping, Sequence, cast

__all__ = [
    "QboError",
    "QboAuthError",
    "QboReauthorizationRequired",
    "QboTokenPersistenceError",
    "QboApiError",
    "QboRateLimitError",
    "QboInvariantError",
    "redact",
]

#: Substrings that mark a mapping key as secret-bearing, compared case-folded.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "id_token",
        "code",
        "password",
        "secret",
        "x-api-key",
    }
)

#: Free-text shapes worth scrubbing even when they appear outside a known key:
#: bearer headers, basic-auth headers, and Intuit's token formats.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(
        r"(?i)\b(access_token|refresh_token|client_secret)[\"'\s:=]+[A-Za-z0-9._\-*/=]{8,}"
    ),
    # Intuit access tokens are long opaque strings; refresh tokens are shorter
    # but still well over any realistic identifier length.
    re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}"),
)

_MASK: Final[str] = "[redacted]"


def redact(value: Any) -> Any:
    """Return ``value`` with anything credential-shaped replaced by a mask.

    Recurses into mappings and sequences so an entire request or response can be
    passed through before it reaches a log or an exception message. Keys are
    matched by substring, so ``QBO_CLIENT_SECRET`` and ``client_secret`` are both
    caught. Non-string scalars are returned unchanged.
    """
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        out: dict[str, Any] = {}
        for key, item in mapping.items():
            name = str(key)
            if any(marker in name.casefold() for marker in _SECRET_KEYS):
                out[name] = _MASK
            else:
                out[name] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        sequence = cast(Sequence[Any], value)
        rebuilt: list[Any] = [redact(item) for item in sequence]
        return tuple(rebuilt) if isinstance(value, tuple) else rebuilt
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(_MASK, text)
        return text
    return value


class QboError(Exception):
    """Base for every error this package raises."""


class QboAuthError(QboError):
    """A token could not be obtained or refreshed."""


class QboReauthorizationRequired(QboAuthError):
    """The refresh token is dead and no automated recovery is possible.

    Intuit invalidates a refresh token after roughly 100 days without use, and
    immediately if it is reused after rotation. Both surface as ``invalid_grant``
    and both require a human to walk the authorization-code flow again.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "QuickBooks refresh token is no longer valid. A human must "
            "re-authorize the app and store a new refresh token -- see "
            "scripts/get_refresh_token.py. "
            f"Intuit said: {redact(detail) or '(no detail)'}"
        )


class QboTokenPersistenceError(QboAuthError):
    """A rotated refresh token could not be written to the token store.

    Raised instead of returning, because Intuit has already invalidated the
    previous refresh token by this point. Continuing would use a token that only
    exists in memory and would strand the integration on the next restart.
    """


class QboApiError(QboError):
    """A non-2xx response from the QuickBooks API."""

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        body: Any = None,
        intuit_tid: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = redact(url)
        self.body = redact(body)
        self.intuit_tid = intuit_tid
        detail = f"{method} {self.url} -> HTTP {status_code}"
        if intuit_tid:
            detail += f" (intuit_tid={intuit_tid})"
        if self.body is not None:
            detail += f"\nResponse: {self.body}"
        super().__init__(detail)


class QboRateLimitError(QboApiError):
    """HTTP 429. Carries the server's retry hint when one was supplied."""

    def __init__(self, *, retry_after: float | None = None, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(**kwargs)


class QboInvariantError(QboError):
    """A financial document failed an arithmetic check.

    This is an error rather than a warning on purpose. A report whose stated
    total contradicts its own rows is not a report with a caveat -- it is a
    wrong number, and returning it is how bad findings get built.
    """
