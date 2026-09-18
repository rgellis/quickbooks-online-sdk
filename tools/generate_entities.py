#!/usr/bin/env python
"""Generate ``src/qbo/entities.py`` from the vendored documentation JSON.

The entity registry used to be hand-written, and hand-writing it was wrong about
roughly ten things: it invented ``CreditCardPaymentTxn`` (the API calls it
``CreditCardPayment``), capitalised ``Exchangerate`` incorrectly, marked
``Budget`` read-only when it accepts full CRUD, excluded three entities the docs
document, and included ``JournalCode``, which the docs do not list at all.

So the registry is generated. Every entity, every operation and every field
requirement traces to ``refs/docs/EntityJsonObject_v1.json`` and
``refs/docs/CodesModelsJsonObjects_v2.json`` -- the same JSON the developer
portal renders. ``tests/test_api_coverage.py`` regenerates and diffs, so drift
fails the suite.

Usage:

    ./scripts/dev run python tools/generate_entities.py
    ./scripts/dev run python tools/generate_entities.py --check
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from docsource import as_dict, as_list, as_str, as_str_list, load

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOCS: Final[Path] = REPO_ROOT / "refs" / "docs"
TARGET: Final[Path] = REPO_ROOT / "src" / "qbo" / "entities.py"

#: Route segment -> classification. Reports and the two special endpoints are
#: not entities and are modelled separately.
_REPORT_RE: Final[re.Pattern[str]] = re.compile(r"/reports/([A-Za-z]+)")
_PATH_RE: Final[re.Pattern[str]] = re.compile(r"<realmID>/([A-Za-z0-9]+)")
_SPECIAL: Final[frozenset[str]] = frozenset({"Batch", "ChangeDataCapture"})

#: Locale-specific report route suffixes, e.g. TrialBalanceFR for France.
_LOCALE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"(FR|IN|UK|AU|CA)$")


@dataclass
class Doc:
    """One documented API object, before classification."""

    name: str
    operations: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    model_ref: str = ""
    title: str = ""
    description: str = ""

    @property
    def routes(self) -> list[str]:
        return [route for routes in self.operations.values() for route in routes]

    @property
    def is_report(self) -> bool:
        return any(_REPORT_RE.search(route) for route in self.routes)

    @property
    def report_routes(self) -> list[str]:
        """Every ``/reports/<Name>`` route this object documents.

        One Operation string can carry several, because Intuit documents locale
        variants inline: TrialBalance's single QUERY entry names both
        ``TrialBalanceFR`` (France) and ``TrialBalance`` (everywhere else).
        """
        found: list[str] = []
        for route in self.routes:
            for match in _REPORT_RE.finditer(route):
                if match.group(1) not in found:
                    found.append(match.group(1))
        return found

    @property
    def report_route(self) -> str:
        """The route to call by default.

        Ten of the 29 reports are invoked under a different name from the one
        the docs file them under -- ``APAgingDetail`` is fetched from
        ``/reports/AgedPayableDetail``. Using the documented name returns 400,
        so the route is what gets recorded.
        """
        routes = self.report_routes
        if not routes:
            return self.name
        general = [r for r in routes if not _LOCALE_SUFFIX_RE.search(r)]
        return general[0] if general else routes[0]

    @property
    def report_variants(self) -> list[str]:
        """Locale-specific routes other than the default."""
        return [r for r in self.report_routes if r != self.report_route]

    @property
    def path(self) -> str:
        for route in self.routes:
            found = _PATH_RE.search(route)
            if found and found.group(1).lower() not in {"reports", "batch", "cdc"}:
                return found.group(1)
        return self.name.lower()


def read_docs() -> tuple[list[Doc], dict[str, Any], str]:
    """Parse the vendored documentation into Doc records."""
    qbo = as_dict(as_dict(load("EntityJsonObject_v1.json")).get("entities")).get("qbo")
    models = as_dict(
        as_dict(as_dict(load("CodesModelsJsonObjects_v2.json")).get("models")).get(
            "qbo"
        )
    )
    minor = as_str(as_dict(load("QboMinorVersions.json")).get("defaultMinorVersion"))

    docs: list[Doc] = []
    for name, raw in sorted(as_dict(qbo).items()):
        body = as_dict(raw)
        if not body:
            continue
        entry = Doc(name=name)
        entry.title = as_str(body.get("title")) or name
        entry.description = as_str(body.get("description"))
        entry.model_ref = as_str(as_dict(body.get("model")).get("$ref"))
        for verb, items in as_dict(body.get("operations")).items():
            routes: list[str] = []
            for item in as_list(items):
                definition = as_dict(as_dict(item).get("definition"))
                route = as_str(definition.get("Operation"))
                if route:
                    routes.append(route)
            entry.operations[verb] = routes
        docs.append(entry)
    return docs, models, minor or "75"


def _requirements(models: dict[str, Any], ref: str) -> dict[str, tuple[str, ...]]:
    """Documented field requirements for one model reference."""
    model = as_dict(models.get(ref))
    return {
        attr: tuple(as_str_list(model.get(key)))
        for key, attr in (
            ("Required", "required"),
            ("RequiredForUpdate", "required_for_update"),
            ("ConditionallyRequired", "conditionally_required"),
        )
    }


def _fmt(values: tuple[str, ...], indent: str = "        ") -> str:
    """Render a tuple literal, wrapping so ruff format leaves it alone."""
    if not values:
        return "()"
    single = "(" + ", ".join(f'"{v}"' for v in values) + ",)"
    if len(indent) + len(single) <= 84:
        return single
    body = "".join(f'\n{indent}    "{v}",' for v in values)
    return f"({body}\n{indent})"


def _fmt_ops(operations: list[str], indent: str = "        ") -> str:
    """Render the operations frozenset, wrapped for the same reason."""
    single = "frozenset({" + ", ".join(f"Operation.{v}" for v in operations) + "})"
    if len(indent) + len("operations=") + len(single) <= 84:
        return single
    body = "".join(f"\n{indent}        Operation.{v}," for v in operations)
    return f"frozenset(\n{indent}    {{{body}\n{indent}    }}\n{indent})"


def render(docs: list[Doc], models: dict[str, Any], minor: str) -> str:
    entities = [d for d in docs if not d.is_report and d.name not in _SPECIAL]
    reports = sorted((d for d in docs if d.is_report), key=lambda d: d.name)
    verbs = sorted({v for d in docs for v in d.operations})

    lines: list[str] = []
    add = lines.append
    add('"""The entity and report registry -- GENERATED, do not edit by hand.')
    add("")
    add("Generated by ``tools/generate_entities.py`` from the documentation JSON")
    add("vendored in ``refs/docs/``, which is the same data developer.intuit.com")
    add("renders. Re-generate after ``scripts/refresh_sources.py``.")
    add("")
    add("Operations come from the documented HTTP routes, not from the XSD -- the")
    add("schema describes shape and says nothing about which verbs an endpoint")
    add("accepts. Field requirements come from the documented request models,")
    add("which is the only source that distinguishes required from optional at")
    add("all (every XSD field reports ``minOccurs=0``).")
    add('"""')
    add("")
    add("from __future__ import annotations")
    add("")
    add("from enum import StrEnum")
    add("from typing import Final, Mapping, NamedTuple")
    add("")
    add("__all__ = [")
    add('    "Operation",')
    add('    "EntitySpec",')
    add('    "ENTITIES",')
    add('    "ReportSpec",')
    add('    "REPORTS",')
    add('    "REPORT_ROUTES",')
    add('    "DEFAULT_MINOR_VERSION",')
    add('    "entity_names",')
    add('    "report_names",')
    add('    "report_route",')
    add('    "supports",')
    add("]")
    add("")
    add("#: The minorversion the documentation currently defaults to.")
    add(f"DEFAULT_MINOR_VERSION: Final[int] = {int(minor)}")
    add("")
    add("")
    add("class Operation(StrEnum):")
    add('    """An operation the Accounting API documents for an endpoint."""')
    add("")
    for verb in verbs:
        add(f'    {verb} = "{verb}"')
    add("")
    add("")
    add("class ReportSpec(NamedTuple):")
    add('    """One documented report.')
    add("")
    add("    ``name`` is how the documentation files it; ``route`` is what the URL")
    add("    actually takes. They differ for ten of the reports -- calling the")
    add("    documented name returns HTTP 400.")
    add('    """')
    add("")
    add("    name: str")
    add("    route: str")
    add("    variants: tuple[str, ...]")
    add("")
    add("")
    add("class EntitySpec(NamedTuple):")
    add('    """One documented entity."""')
    add("")
    add("    name: str")
    add("    path: str")
    add("    operations: frozenset[Operation]")
    add("    required: tuple[str, ...]")
    add("    required_for_update: tuple[str, ...]")
    add("    conditionally_required: tuple[str, ...]")
    add("")
    add("")
    add("#: Every entity the Accounting API documents, keyed by its API name.")
    add("ENTITIES: Final[Mapping[str, EntitySpec]] = {")
    for entry in sorted(entities, key=lambda d: d.name):
        req = _requirements(models, entry.model_ref)
        ops = _fmt_ops(sorted(entry.operations))
        add(f'    "{entry.name}": EntitySpec(')
        add(f'        name="{entry.name}",')
        add(f'        path="{entry.path}",')
        add(f"        operations={ops},")
        add(f"        required={_fmt(req.get('required', ()))},")
        add(f"        required_for_update={_fmt(req.get('required_for_update', ()))},")
        add(
            f"        conditionally_required="
            f"{_fmt(req.get('conditionally_required', ()))},"
        )
        add("    ),")
    add("}")
    add("")
    add("#: Every documented report, keyed by its documentation name.")
    add("REPORTS: Final[Mapping[str, ReportSpec]] = {")
    for entry in reports:
        variants = tuple(entry.report_variants)
        add(f'    "{entry.name}": ReportSpec(')
        add(f'        name="{entry.name}",')
        add(f'        route="{entry.report_route}",')
        add(f"        variants={_fmt(variants)},")
        add("    ),")
    add("}")
    add("")
    add("#: Reverse lookup: every callable route -> the documentation name.")
    add("REPORT_ROUTES: Final[Mapping[str, str]] = {")
    for entry in reports:
        for route in entry.report_routes:
            add(f'    "{route}": "{entry.name}",')
    add("}")
    add("")
    add("")
    add("def entity_names() -> frozenset[str]:")
    add('    """Every documented entity name."""')
    add("    return frozenset(ENTITIES)")
    add("")
    add("")
    add("def report_names() -> frozenset[str]:")
    add('    """Every documented report name."""')
    add("    return frozenset(REPORTS)")
    add("")
    add("")
    add("def report_route(name: str) -> str:")
    add('    """Resolve a report name to the route the API actually takes.')
    add("")
    add("    Accepts either the documentation name or the route itself, because")
    add("    callers reasonably use whichever they read first.")
    add('    """')
    add("    spec = REPORTS.get(name)")
    add("    if spec is not None:")
    add("        return spec.route")
    add("    documented = REPORT_ROUTES.get(name)")
    add("    if documented is not None:")
    add("        return REPORTS[documented].route")
    add("    raise KeyError(")
    add('        f"Unknown report {name!r}. Known reports: "')
    add('        + ", ".join(sorted(REPORTS))')
    add("    )")
    add("")
    add("")
    add("def supports(entity: str, operation: Operation) -> bool:")
    add('    """Whether ``entity`` documents ``operation``."""')
    add("    spec = ENTITIES.get(entity)")
    add("    return spec is not None and operation in spec.operations")
    return "\n".join(lines) + "\n"


def _ruff_format(path: Path) -> None:
    """Format generated output with the project's own formatter.

    Predicting ruff's line wrapping by hand is fragile -- the magic trailing
    comma alone re-explodes any tuple written on one line. Formatting here means
    generated code is byte-identical to what ``./scripts/dev check`` produces,
    so ``--check`` cannot fail for cosmetic reasons.
    """
    try:
        subprocess.run(
            ["ruff", "format", "--quiet", str(path)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"warning: could not run ruff format ({exc}); output may be unformatted")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the entity registry.")
    parser.add_argument("--check", action="store_true", help="Fail if out of date.")
    args = parser.parse_args()

    docs, models, minor = read_docs()
    rendered = render(docs, models, minor)

    if args.check:
        scratch = TARGET.with_suffix(".generated.tmp")
        scratch.write_text(rendered)
        _ruff_format(scratch)
        current = scratch.read_text()
        scratch.unlink()
        if not TARGET.exists() or TARGET.read_text() != current:
            print("src/qbo/entities.py is out of date. Run tools/generate_entities.py.")
            return 1
        print("Entity registry is current.")
        return 0

    TARGET.write_text(rendered)
    _ruff_format(TARGET)
    entities = [d for d in docs if not d.is_report and d.name not in _SPECIAL]
    report_docs = [d for d in docs if d.is_report]
    renamed = [d for d in report_docs if d.report_route != d.name]
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)}")
    print(f"  entities : {len(entities)}")
    print(
        f"  reports  : {len(report_docs)} ({len(renamed)} whose route differs from their docs name)"
    )
    print(f"  minorver : {minor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
