"""The one-time authorization helper.

Only the parsing is tested here: the exchange itself is an interactive call to
Intuit that cannot be exercised without a browser and a real company. What can
go wrong offline is the paste step -- people paste the whole redirect URL, just
the query string, or only the code, and all three have to work, because this
runs at the worst possible moment: the integration is already broken.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    """Import the script by path; scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "get_refresh_token", REPO_ROOT / "scripts" / "get_refresh_token.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load()


class TestExtract:
    def test_full_redirect_url(self) -> None:
        code, realm = bootstrap._extract(
            "http://localhost:8000/callback?code=AB11&state=xyz&realmId=9130347"
        )
        assert code == "AB11"
        assert realm == "9130347"

    def test_https_redirect_url(self) -> None:
        code, realm = bootstrap._extract(
            "https://books.example.com/quickbooks/callback?code=AB11&realmId=9130347"
        )
        assert (code, realm) == ("AB11", "9130347")

    def test_bare_query_string(self) -> None:
        assert bootstrap._extract("code=AB11&realmId=9130347") == ("AB11", "9130347")

    def test_bare_code_from_the_oauth_playground(self) -> None:
        """The Playground shows the code alone; the realm comes from env."""
        assert bootstrap._extract("AB11xyz") == ("AB11xyz", "")

    @pytest.mark.parametrize(
        "pasted",
        [
            '"http://localhost:8000/callback?code=AB11&realmId=9130347"',
            "  http://localhost:8000/callback?code=AB11&realmId=9130347  ",
            "'http://localhost:8000/callback?code=AB11&realmId=9130347'",
        ],
    )
    def test_surrounding_quotes_and_whitespace_are_tolerated(self, pasted: str) -> None:
        assert bootstrap._extract(pasted) == ("AB11", "9130347")

    def test_lowercase_realmid_spelling(self) -> None:
        assert bootstrap._extract("code=AB11&realmid=9130347")[1] == "9130347"

    def test_empty_paste_is_rejected(self) -> None:
        with pytest.raises(SystemExit, match="nothing pasted"):
            bootstrap._extract("   ")

    def test_url_without_a_code_is_rejected(self) -> None:
        with pytest.raises(SystemExit, match="no 'code' parameter"):
            bootstrap._extract(
                "http://localhost:8000/callback?error=access_denied&code="
            )


class TestEnvironment:
    def test_missing_credentials_name_what_to_do(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicitly unset rather than assuming the variable is absent.

        docker-compose loads .env into the container, so once real credentials
        exist this passes for the wrong reason -- or fails, which is how the
        dependency on ambient environment was found.
        """
        monkeypatch.delenv("QBO_CLIENT_ID", raising=False)
        with pytest.raises(SystemExit, match="QBO_CLIENT_ID is not set"):
            bootstrap._require_env("QBO_CLIENT_ID", None)

    def test_an_override_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QBO_CLIENT_ID", "from-env")
        assert bootstrap._require_env("QBO_CLIENT_ID", "from-flag") == "from-flag"

    def test_the_environment_is_used_when_no_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QBO_CLIENT_ID", "from-env")
        assert bootstrap._require_env("QBO_CLIENT_ID") == "from-env"
