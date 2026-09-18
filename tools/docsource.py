"""Shared access to the vendored documentation JSON.

Both the registry generator and the coverage audit read the same files and must
normalise field names identically -- when they did not, the audit reported 42
false divergences because the docs write ``Line [0..n]`` where the wire key is
``Line``.

Decoded JSON is ``Any``, and this package type-checks in pyright strict mode, so
every traversal goes through the narrowing helpers below rather than indexing
raw values. They are deliberately total: a missing or wrongly-typed node yields
an empty result instead of raising, because a documentation file that changes
shape should surface as a coverage failure with a useful diff, not a traceback
from three frames deep in a dict lookup.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, cast

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOCS: Final[Path] = REPO_ROOT / "refs" / "docs"

#: The docs embed cardinality in field labels: "Line [0..n]", "TxnDate [0..1]".
_CARDINALITY_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*\[([0-9]+)\.\.([0-9n]+)\]\s*$"
)


def field_name(raw: str) -> str:
    """Normalise a documented field label to the JSON key sent on the wire."""
    return _CARDINALITY_RE.sub("", str(raw)).strip()


def is_collection(raw: str) -> bool:
    """Whether a documented field label denotes a repeating field.

    The cardinality suffix is the only place the docs record this: ``Line
    [0..n]`` is a JSON array, ``CustomerRef [0..1]`` is a single object. Losing
    it produces a model that rejects every real transaction, because the line
    items arrive as a list.
    """
    found = _CARDINALITY_RE.search(str(raw))
    if not found:
        return False
    upper = found.group(0).strip().strip("[]").split("..")[-1]
    return upper == "n" or (upper.isdigit() and int(upper) > 1)


def as_dict(value: Any) -> dict[str, Any]:
    """Narrow a decoded JSON value to an object, or an empty one."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in cast("dict[Any, Any]", value).items()}


def as_list(value: Any) -> list[Any]:
    """Narrow a decoded JSON value to an array, or an empty one."""
    if not isinstance(value, list):
        return []
    return list(cast("list[Any]", value))


def as_str(value: Any) -> str:
    """Narrow a decoded JSON value to a string, treating None as empty."""
    return "" if value is None else str(value)


def as_str_list(value: Any) -> list[str]:
    """Narrow a decoded JSON value to a list of normalised field names."""
    return [
        name for name in (field_name(as_str(item)) for item in as_list(value)) if name
    ]


def load(filename: str) -> dict[str, Any]:
    """Load one vendored documentation file."""
    path = DOCS / filename
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run scripts/refresh_sources.py first.")
    return as_dict(json.loads(path.read_text(encoding="utf-8")))


def qbo_entities() -> dict[str, Any]:
    """The documented QBO objects: entities, reports and special endpoints."""
    return as_dict(as_dict(load("EntityJsonObject_v1.json").get("entities")).get("qbo"))


def qbo_models() -> dict[str, Any]:
    """The documented request/response models, keyed by model ref."""
    return as_dict(
        as_dict(load("CodesModelsJsonObjects_v2.json").get("models")).get("qbo")
    )
