#!/usr/bin/env python
"""Vendor Intuit's published API sources into ``refs/``.

Three independent sources, because no single one is trustworthy alone.

**Documentation JSON** (``static.developer.intuit.com/JSONObjects``) is what
developer.intuit.com itself renders. It is the authoritative statement of which
entities exist, which operations each accepts, and which fields are required --
none of which the XSD can tell you. This is the primary source.

**XSD from the .NET and Java SDKs** is the cross-check. Both are parsed because
they disagree with each other: at the time of writing the .NET schema carried 20
complexTypes the Java one lacked, and none the other way round. The XSD also
over-reports, marking QuickBooks *Desktop* types as entities that the REST API
never exposes.

``tools/audit_coverage.py`` reconciles all three and reports disagreements
rather than picking a winner silently.

Usage (from the host):

    ./scripts/dev run python scripts/refresh_sources.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

import httpx

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REFS: Final[Path] = REPO_ROOT / "refs"

SCHEMA_FILES: Final[tuple[str, ...]] = (
    "Finance.xsd",
    "IntuitBaseTypes.xsd",
    "IntuitNamesTypes.xsd",
    "IntuitRestServiceDef.xsd",
    "Report.xsd",
)


class Source(NamedTuple):
    """One upstream schema location.

    ``branch`` is resolved from the repository's default branch at fetch time
    rather than hardcoded. Assuming ``master`` was wrong and quietly costly:
    the Java SDK defaults to ``develop``, and its ``master`` is 20 complexTypes
    behind -- enough to make Intuit's two SDKs look like they disagree when
    their current branches are byte-identical.
    """

    key: str
    repo: str
    directory: str

    def raw_url(self, filename: str, branch: str) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.repo}/"
            f"{branch}/{self.directory}/{filename}"
        )


#: The JSON the documentation site itself consumes. These are served gzipped;
#: httpx transparently decompresses. They are the primary source of truth for
#: entities, operations and field requirements.
DOC_FILES: Final[tuple[str, ...]] = (
    "EntityJsonObject_v1.json",
    "CodesModelsJsonObjects_v2.json",
    "TocJsonObject_v1.json",
    "QboMinorVersions.json",
)

DOCS_BASE: Final[str] = "https://static.developer.intuit.com/JSONObjects"

#: Top-level keys dropped from a vendored doc file before it is written.
#:
#: CodesModelsJsonObjects carries two sections. "models" is the field and type
#: data everything here is generated from. "codes" is sample request/response
#: payloads, and Intuit's samples embed presigned S3 URLs containing an AWS
#: access key id belonging to their preprod account -- 16 of them, expired in
#: 2021. Nothing in this package reads "codes", and committing someone else's
#: key material into a public repository trips secret scanners over a
#: credential we neither own nor can rotate. So it is dropped at vendor time,
#: which also halves the file.
#:
#: Report fixtures under tests/fixtures/ were taken from "codes" before this
#: filter existed; they contain no URLs and are checked in directly.
DROPPED_SECTIONS: Final[dict[str, tuple[str, ...]]] = {
    "CodesModelsJsonObjects_v2.json": ("codes",),
}


SOURCES: Final[tuple[Source, ...]] = (
    Source(
        key="dotnet",
        repo="intuit/QuickBooks-V3-DotNET-SDK",
        directory=(
            "IPPDotNetDevKitCSV3/Tools/XsdExtension/Intuit.Ipp.XsdExtension/Schema"
        ),
    ),
    Source(
        key="java",
        repo="intuit/QuickBooks-V3-Java-SDK",
        directory="ipp-v3-java-data/src/main/xsd",
    ),
)


_GH_HEADERS: Final[dict[str, str]] = {"Accept": "application/vnd.github+json"}


async def _default_branch(client: httpx.AsyncClient, source: Source) -> str:
    """The repository's own default branch.

    Falls back to ``master`` only when the API is unreachable, and says so --
    a silent fallback is how the stale-branch problem arose in the first place.
    """
    try:
        response = await client.get(
            f"https://api.github.com/repos/{source.repo}", headers=_GH_HEADERS
        )
        if response.status_code == 200:
            payload: Any = response.json()
            branch = str(payload.get("default_branch") or "")
            if branch:
                return branch
    except httpx.HTTPError:
        pass
    print(f"  ! {source.key}: could not resolve default branch; assuming master")
    return "master"


async def _head_commit(
    client: httpx.AsyncClient, source: Source, branch: str
) -> str | None:
    """The commit the vendored copy was taken from, for traceability."""
    url = f"https://api.github.com/repos/{source.repo}/commits/{branch}"
    try:
        response = await client.get(url, headers=_GH_HEADERS)
        if response.status_code != 200:
            return None
        payload: Any = response.json()
        return str(payload.get("sha", ""))[:12] or None
    except httpx.HTTPError:
        return None


async def _fetch(client: httpx.AsyncClient, source: Source) -> dict[str, Any]:
    target = REFS / source.key
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    branch = await _default_branch(client, source)

    for filename in SCHEMA_FILES:
        url = source.raw_url(filename, branch)
        response = await client.get(url)
        if response.status_code != 200:
            print(f"  ! {source.key}/{filename}: HTTP {response.status_code}")
            files[filename] = {"status": response.status_code}
            continue
        body = response.content
        (target / filename).write_bytes(body)
        files[filename] = {"bytes": len(body), "url": url}
        print(f"  + {source.key}/{filename}  ({len(body):,} bytes)")

    return {
        "repo": source.repo,
        "branch": branch,
        "resolved": "default branch, from the GitHub API",
        "commit": await _head_commit(client, source, branch),
        "files": files,
    }


async def _fetch_docs(client: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch the documentation JSON the developer portal renders from."""
    target = REFS / "docs"
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}

    for filename in DOC_FILES:
        url = f"{DOCS_BASE}/{filename}"
        response = await client.get(url, headers={"Accept-Encoding": "gzip"})
        if response.status_code != 200:
            print(f"  ! docs/{filename}: HTTP {response.status_code}")
            files[filename] = {"status": response.status_code}
            continue
        body = response.content
        dropped = DROPPED_SECTIONS.get(filename, ())
        note: str | None = None
        if dropped:
            try:
                parsed: Any = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                removed = [key for key in dropped if key in parsed]
                for key in removed:
                    del parsed[key]
                body = (json.dumps(parsed, indent=2) + "\n").encode("utf-8")
                note = f"dropped section(s): {', '.join(removed)}"

        (target / filename).write_bytes(body)
        files[filename] = {"bytes": len(body), "url": url}
        if note:
            files[filename]["filtered"] = note
        print(
            f"  + docs/{filename}  ({len(body):,} bytes)"
            + (f"  [{note}]" if note else "")
        )

    return {"base_url": DOCS_BASE, "files": files}


async def main() -> int:
    REFS.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "Intuit publishes no OpenAPI/discovery document for the Accounting "
            "API v3. 'docs' is the JSON the developer portal itself renders and "
            "is the primary source; the XSDs come from Intuit's own SDK "
            "repositories and are used as a cross-check. All three disagree in "
            "places -- see API_COVERAGE.md."
        ),
        "sources": {},
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        print("Fetching developer.intuit.com documentation JSON (primary source)")
        manifest["sources"]["docs"] = await _fetch_docs(client)
        for source in SOURCES:
            print(f"Fetching {source.repo} ({source.key})")
            manifest["sources"][source.key] = await _fetch(client, source)

    (REFS / "SOURCES.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nWrote {REFS / 'SOURCES.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
