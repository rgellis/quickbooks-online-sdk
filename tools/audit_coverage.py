#!/usr/bin/env python
"""Reconcile the entity registry against every published source.

Writes ``API_COVERAGE.md`` and ``refs/coverage.json``.
``tests/test_api_coverage.py`` asserts the result, so an upstream change this
package has not accounted for fails the suite rather than passing unnoticed.

Three sources, because none is trustworthy alone:

* **docs** (``refs/docs/``) -- the JSON developer.intuit.com renders. Primary.
  It is the only source that states which operations an endpoint accepts and
  which fields are required.
* **dotnet** / **java** XSD -- Intuit's own SDK schemas, used as a cross-check.
  They disagree with each other, they over-report (marking QuickBooks *Desktop*
  types as entities), and they under-report relative to the docs. All three
  behaviours are reported rather than silently resolved.

Usage:

    ./scripts/dev run python tools/audit_coverage.py
    ./scripts/dev run python tools/audit_coverage.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from docsource import as_dict, as_str, field_name  # noqa: E402

from qbo.entities import ENTITIES, REPORTS  # noqa: E402

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REFS: Final[Path] = REPO_ROOT / "refs"
XS: Final[str] = "{http://www.w3.org/2001/XMLSchema}"
XSD_SOURCES: Final[tuple[str, ...]] = ("dotnet", "java")
SCHEMA_FILES: Final[tuple[str, ...]] = (
    "Finance.xsd",
    "IntuitBaseTypes.xsd",
    "IntuitNamesTypes.xsd",
    "IntuitRestServiceDef.xsd",
    "Report.xsd",
)

#: Entities the docs spell differently from the XSD. Recorded rather than
#: normalised away, because the difference is itself worth knowing: the wire
#: name is the documented one.
KNOWN_ALIASES: Final[dict[str, str]] = {
    "CreditCardPayment": "CreditCardPaymentTxn",
    "Exchangerate": "ExchangeRate",
}


@dataclass
class XsdSource:
    """One vendored XSD set, parsed."""

    key: str
    entities: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]]
    )
    missing_files: list[str] = field(default_factory=list[str])


def parse_xsd(key: str) -> XsdSource:
    """Extract entity -> {field: type}, following xs:extension chains."""
    source = XsdSource(key=key)
    directory = REFS / key
    complex_types: dict[str, ET.Element] = {}
    names: set[str] = set()

    for filename in SCHEMA_FILES:
        path = directory / filename
        if not path.exists():
            source.missing_files.append(filename)
            continue
        root = ET.parse(path).getroot()
        for node in root.iter(f"{XS}complexType"):
            name = node.get("name")
            if name:
                complex_types[name] = node
        for node in root.iter(f"{XS}element"):
            name = node.get("name")
            if (
                name
                and (node.get("substitutionGroup") or "").split(":")[-1]
                == "IntuitObject"
            ):
                names.add(name)

    def fields_of(type_name: str, seen: set[str] | None = None) -> dict[str, str]:
        seen = seen or set()
        if type_name in seen or type_name not in complex_types:
            return {}
        seen.add(type_name)
        node = complex_types[type_name]
        collected: dict[str, str] = {}
        for extension in node.iter(f"{XS}extension"):
            collected.update(
                fields_of((extension.get("base") or "").split(":")[-1], seen)
            )
        for element in node.iter(f"{XS}element"):
            element_name = element.get("name")
            if element_name:
                collected[element_name] = (element.get("type") or "anyType").split(":")[
                    -1
                ]
        return collected

    for name in sorted(names):
        source.entities[name] = fields_of(name)
    return source


def docs_fields() -> dict[str, dict[str, dict[str, Any]]]:
    """Documented properties per entity, from the request/response models."""
    entity_file = REFS / "docs" / "EntityJsonObject_v1.json"
    model_file = REFS / "docs" / "CodesModelsJsonObjects_v2.json"
    if not entity_file.exists() or not model_file.exists():
        return {}

    models = as_dict(
        as_dict(json.loads(model_file.read_text(encoding="utf-8")).get("models")).get(
            "qbo"
        )
    )
    qbo = as_dict(
        as_dict(
            json.loads(entity_file.read_text(encoding="utf-8")).get("entities")
        ).get("qbo")
    )

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, raw in qbo.items():
        if name not in ENTITIES:
            continue
        body = as_dict(raw)
        ref = as_str(as_dict(body.get("model")).get("$ref"))
        properties = as_dict(as_dict(models.get(ref)).get("properties"))
        resolved: dict[str, dict[str, Any]] = {}
        for key, value in properties.items():
            normalised = field_name(key)
            if normalised:
                resolved[normalised] = as_dict(value)
        out[name] = resolved
    return out


@dataclass
class Audit:
    xsd: dict[str, XsdSource]
    docs_properties: dict[str, dict[str, dict[str, Any]]]
    xsd_union: set[str]
    documented: set[str]
    xsd_only: set[str]
    docs_only: set[str]
    xsd_disagreements: dict[str, list[str]]
    field_gaps: dict[str, dict[str, list[str]]]


def run_audit() -> Audit:
    xsd = {key: parse_xsd(key) for key in XSD_SOURCES if (REFS / key).exists()}
    if not xsd:
        raise SystemExit("No vendored XSDs in refs/. Run scripts/refresh_sources.py.")

    union: set[str] = set()
    for source in xsd.values():
        union |= set(source.entities)

    documented = set(ENTITIES)
    aliased = {KNOWN_ALIASES.get(name, name) for name in documented}

    xsd_only = union - aliased
    docs_only = {n for n in documented if KNOWN_ALIASES.get(n, n) not in union}

    disagreements: dict[str, list[str]] = {}
    if len(xsd) > 1:
        for name in sorted(union):
            absent = sorted(k for k, s in xsd.items() if name not in s.entities)
            if absent:
                disagreements[name] = absent

    properties = docs_fields()
    field_gaps: dict[str, dict[str, list[str]]] = {}
    primary = xsd.get("dotnet") or next(iter(xsd.values()))
    for name in sorted(documented):
        schema_name = KNOWN_ALIASES.get(name, name)
        schema_fields = set(primary.entities.get(schema_name, {}))
        doc_fields = set(properties.get(name, {}))
        if not schema_fields or not doc_fields:
            continue
        # Only one direction is a finding. Fields in the XSD but absent from
        # the documented model are routine -- the docs list a curated subset,
        # not the full wire shape. Fields the docs describe that no schema
        # models are the real signal: the SDKs are behind the API.
        only_docs = sorted(doc_fields - schema_fields)
        if only_docs:
            field_gaps[name] = {"documented_but_absent_from_xsd": only_docs}

    return Audit(
        xsd=xsd,
        docs_properties=properties,
        xsd_union=union,
        documented=documented,
        xsd_only=xsd_only,
        docs_only=docs_only,
        xsd_disagreements=disagreements,
        field_gaps=field_gaps,
    )


def to_json(audit: Audit) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "documented_entities": sorted(audit.documented),
        "documented_reports": {
            name: {"route": spec.route, "variants": list(spec.variants)}
            for name, spec in sorted(REPORTS.items())
        },
        "xsd_entities": sorted(audit.xsd_union),
        "xsd_only": sorted(audit.xsd_only),
        "docs_only": sorted(audit.docs_only),
        "aliases": KNOWN_ALIASES,
        "xsd_source_disagreements": audit.xsd_disagreements,
        "field_gaps": audit.field_gaps,
        "sources": {
            key: {
                "entity_count": len(source.entities),
                "missing_files": source.missing_files,
            }
            for key, source in audit.xsd.items()
        },
    }


def to_markdown(audit: Audit) -> str:
    lines: list[str] = []
    add = lines.append
    add("# API Coverage")
    add("")
    add("Generated by `tools/audit_coverage.py`. **Do not hand-edit.**")
    add("Re-run after `scripts/refresh_sources.py`.")
    add("")
    add("## Why this file exists")
    add("")
    add(
        "Intuit publishes no OpenAPI or discovery document for the Accounting "
        "API v3, and their own SDKs disagree both with each other and with the "
        "documentation. This report reconciles all three so that coverage is a "
        "verified fact rather than a claim."
    )
    add("")
    add("## Sources")
    add("")
    add("| Source | Role | Entities |")
    add("|---|---|---:|")
    add(
        f"| `refs/docs/` | **primary** — the JSON developer.intuit.com renders; "
        f"the only source stating operations and field requirements | "
        f"{len(audit.documented)} |"
    )
    for key, source in sorted(audit.xsd.items()):
        add(
            f"| `refs/{key}/` | cross-check — XSD from Intuit's {key} SDK | "
            f"{len(source.entities)} |"
        )
    add("")
    add("## Summary")
    add("")
    add(f"- Documented entities covered: **{len(audit.documented)}**")
    add(f"- Documented reports covered: **{len(REPORTS)}**")
    add(
        f"- Types the XSD declares but the docs do not expose: **{len(audit.xsd_only)}**"
    )
    add(f"- Documented entities absent from the XSD: **{len(audit.docs_only)}**")
    add(f"- Entities where the two XSDs disagree: **{len(audit.xsd_disagreements)}**")
    add(f"- Entities with field-level divergence: **{len(audit.field_gaps)}**")
    add("")
    add(
        "Coverage is defined against the documentation. The XSD columns exist to "
        "catch the case where Intuit ships a field or entity the docs have not "
        "caught up with."
    )
    add("")

    add("## Documented entities")
    add("")
    add("| Entity | Path | Operations | Required | Required for update |")
    add("|---|---|---|---|---|")
    for name in sorted(audit.documented):
        spec = ENTITIES[name]
        ops = ", ".join(sorted(str(o) for o in spec.operations))
        required = ", ".join(f"`{f}`" for f in spec.required) or "—"
        for_update = ", ".join(f"`{f}`" for f in spec.required_for_update) or "—"
        add(f"| `{name}` | `{spec.path}` | {ops} | {required} | {for_update} |")
    add("")

    add("## Documented reports")
    add("")
    add("| Report | Route | Locale variants |")
    add("|---|---|---|")
    for name in sorted(REPORTS):
        spec = REPORTS[name]
        flag = "" if spec.route == name else " ⚠️"
        variants = ", ".join(f"`{v}`" for v in spec.variants) or "—"
        add(f"| `{name}` | `{spec.route}`{flag} | {variants} |")
    add("")
    add(
        "⚠️ marks a report whose URL differs from the name the documentation "
        "files it under. Calling the documented name returns HTTP 400."
    )
    add("")

    if audit.docs_only:
        add("## Documented but absent from every XSD")
        add("")
        add(
            "Endpoints the documentation describes that Intuit's SDK schemas do "
            "not model. Covered on the documentation's authority."
        )
        add("")
        for name in sorted(audit.docs_only):
            add(f"- `{name}`")
        add("")

    if audit.xsd_only:
        add("## Declared in XSD, not exposed by the Accounting API")
        add("")
        add(
            'The XSD marks these `substitutionGroup="IntuitObject"`, but the '
            "documentation gives them no endpoint. Mostly QuickBooks Desktop "
            "concepts and internal types. Not coverage gaps."
        )
        add("")
        add(", ".join(f"`{n}`" for n in sorted(audit.xsd_only)))
        add("")

    if KNOWN_ALIASES:
        add("## Naming differences")
        add("")
        add("| Documented name (wire) | XSD name |")
        add("|---|---|")
        for docs_name, xsd_name in sorted(KNOWN_ALIASES.items()):
            add(f"| `{docs_name}` | `{xsd_name}` |")
        add("")

    if audit.xsd_disagreements:
        add("## Disagreement between Intuit's own SDKs")
        add("")
        add(
            "Entities one SDK schema declares and the other does not. Evidence "
            "that no single SDK can be taken as the API's definition."
        )
        add("")
        add("| Entity | Absent from |")
        add("|---|---|")
        for name, absent in sorted(audit.xsd_disagreements.items()):
            add(f"| `{name}` | {', '.join(f'`{a}`' for a in absent)} |")
        add("")

    if audit.field_gaps:
        add("## Field-level divergence")
        add("")
        add(
            "Where the documented model and the .NET XSD describe different "
            "fields for the same entity. Fields documented but absent from the "
            "schema are the interesting direction: the schema is behind."
        )
        add("")
        add("| Entity | Direction | Fields |")
        add("|---|---|---|")
        for name, detail in sorted(audit.field_gaps.items()):
            for direction, fields in sorted(detail.items()):
                shown = ", ".join(f"`{f}`" for f in fields[:8])
                more = f" (+{len(fields) - 8} more)" if len(fields) > 8 else ""
                add(f"| `{name}` | {direction.replace('_', ' ')} | {shown}{more} |")
        add("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit API coverage.")
    parser.add_argument("--check", action="store_true", help="Fail if out of date.")
    args = parser.parse_args()

    audit = run_audit()
    data = to_json(audit)
    markdown = to_markdown(audit)
    json_path = REFS / "coverage.json"
    md_path = REPO_ROOT / "API_COVERAGE.md"

    if args.check:
        stale: list[str] = []
        if not md_path.exists() or md_path.read_text() != markdown:
            stale.append("API_COVERAGE.md")
        if not json_path.exists():
            stale.append("refs/coverage.json")
        else:
            previous: Any = json.loads(json_path.read_text())
            volatile = {"generated_at"}
            if {k: v for k, v in previous.items() if k not in volatile} != {
                k: v for k, v in data.items() if k not in volatile
            }:
                stale.append("refs/coverage.json")
        if stale:
            print(f"Out of date: {', '.join(stale)}. Re-run tools/audit_coverage.py.")
            return 1
        print("Coverage artefacts are current.")
        return 0

    json_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    md_path.write_text(markdown)

    print(f"Documented entities      : {len(audit.documented)}")
    print(f"Documented reports       : {len(REPORTS)}")
    print(f"XSD-only (not exposed)   : {len(audit.xsd_only)}")
    print(
        f"Docs-only (not in XSD)   : {len(audit.docs_only)}  {sorted(audit.docs_only)}"
    )
    print(f"XSD source disagreements : {len(audit.xsd_disagreements)}")
    print(f"Field-level divergence   : {len(audit.field_gaps)} entities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
