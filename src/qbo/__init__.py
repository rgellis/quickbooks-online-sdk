"""Typed Python client for the QuickBooks Online Accounting API v3.

Intuit ships V3 SDKs for .NET, Java and PHP only; Python gets an OAuth helper
and nothing else. This package is the missing accounting client, written
directly against the REST API so that new ``minorversion`` fields are available
without waiting for an SDK release.

Design commitments, in order of importance:

1. **Every number is traceable.** Values come from a raw API object or a
   documented report row. Derived figures say so.
2. **Arithmetic that must hold is asserted.** A report whose stated total
   contradicts its own rows raises rather than returning a plausible number.
3. **Coverage is mechanically verified**, not claimed. See ``API_COVERAGE.md``
   and ``tests/test_api_coverage.py``.
"""

from __future__ import annotations

from qbo.auth import AuthClient, FileTokenStore, MemoryTokenStore, TokenSet, TokenStore
from qbo.client import (
    MAX_PAGE_SIZE,
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    QboClient,
)
from qbo.errors import (
    QboApiError,
    QboAuthError,
    QboError,
    QboInvariantError,
    QboRateLimitError,
    QboReauthorizationRequired,
    QboTokenPersistenceError,
    redact,
)
from qbo.reports import (
    Report,
    ReportColumn,
    ReportRow,
    check_report,
    parse_report,
)

__version__ = "0.1.0"

__all__ = [
    "AuthClient",
    "FileTokenStore",
    "MemoryTokenStore",
    "TokenSet",
    "TokenStore",
    "QboClient",
    "Report",
    "ReportColumn",
    "ReportRow",
    "check_report",
    "parse_report",
    "MAX_PAGE_SIZE",
    "PRODUCTION_BASE_URL",
    "SANDBOX_BASE_URL",
    "QboApiError",
    "QboAuthError",
    "QboError",
    "QboInvariantError",
    "QboRateLimitError",
    "QboReauthorizationRequired",
    "QboTokenPersistenceError",
    "redact",
    "__version__",
]
