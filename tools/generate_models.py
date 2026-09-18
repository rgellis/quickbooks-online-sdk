#!/usr/bin/env python
"""Generate Pydantic models for every documented entity.

Each source is used for what it is actually good at:

* **docs** (``refs/docs/CodesModelsJsonObjects_v2.json``) give the object graph:
  which fields exist, their types, which are required, which are read-only, and
  how polymorphic fields resolve (``Invoice.Line`` is a union over five line
  shapes discriminated by ``DetailType``).
* **XSD** (``refs/dotnet/``) gives enum *values*. The docs name a type
  ``LineDetailTypeEnum`` but never list its members; the schema enumerates all
  17. Neither source can generate these models alone.

Only models reachable from an entity are emitted, walking ``$ref`` transitively
from the 43 documented entities.

Usage:

    ./scripts/dev run python tools/generate_models.py
    ./scripts/dev run python tools/generate_models.py --check
"""

from __future__ import annotations

import argparse
import keyword
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docsource import (  # noqa: E402
    as_dict,
    as_list,
    as_str,
    field_name,
    is_collection,
    load,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REFS: Final[Path] = REPO_ROOT / "refs"
TARGET: Final[Path] = REPO_ROOT / "src" / "qbo" / "models" / "_generated.py"
XS: Final[str] = "{http://www.w3.org/2001/XMLSchema}"

SCHEMA_FILES: Final[tuple[str, ...]] = (
    "Finance.xsd",
    "IntuitBaseTypes.xsd",
    "IntuitNamesTypes.xsd",
    "IntuitRestServiceDef.xsd",
    "Report.xsd",
)

#: Documented scalar type -> Python annotation. QuickBooks writes the same
#: concept several ways ("String" and "string"), hence the duplicates.
SCALARS: Final[dict[str, str]] = {
    "String": "str",
    "string": "str",
    "IdType": "str",
    "Decimal": "Decimal",
    "BigDecimal": "Decimal",
    "Integer": "int",
    "Boolean": "bool",
    "Date": "date",
    "DateTime": "datetime",
    "None": "str",
}


def xsd_enums() -> dict[str, list[str]]:
    """Enum name -> members, from the XSD. The docs name types but not values."""
    found: dict[str, list[str]] = {}
    for filename in SCHEMA_FILES:
        path = REFS / "dotnet" / filename
        if not path.exists():
            continue
        for node in ET.parse(path).getroot().iter(f"{XS}simpleType"):
            name = node.get("name")
            if not name:
                continue
            values = [
                value
                for value in (e.get("value") for e in node.iter(f"{XS}enumeration"))
                if value
            ]
            if values:
                found[name] = values
    return found


def _identifier(raw: str) -> str:
    """A valid Python attribute name, or empty when the label is unusable.

    The documentation contains at least one malformed label
    (``"BreakHours BreakMinutes"`` on TimeActivity). Those are skipped and
    reported rather than silently mangled into a field that does not exist.
    """
    name = field_name(raw)
    if not name or not name.isidentifier() or keyword.iskeyword(name):
        return ""
    return name


def _enum_class(name: str) -> str:
    return name if name.endswith("Enum") else f"{name}Enum"


def _member(value: str) -> str:
    """A Python enum member name for an API value like 'Accounts Receivable'."""
    cleaned = "".join(c if c.isalnum() else "_" for c in value).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"VALUE_{cleaned}"
    return cleaned.upper()


class Generator:
    def __init__(self) -> None:
        self.models = as_dict(
            as_dict(as_dict(load("CodesModelsJsonObjects_v2.json")).get("models")).get(
                "qbo"
            )
        )
        self.entities = as_dict(
            as_dict(load("EntityJsonObject_v1.json").get("entities")).get("qbo")
        )
        self.enums = xsd_enums()
        #: Model ref -> the PascalCase name Intuit actually uses. The models are
        #: keyed lowercase ("salesitemline") but every $ref that points at one
        #: carries its proper spelling ("SalesItemLine"), so the casing is
        #: recovered from the references rather than guessed from the key.
        self.proper: dict[str, str] = self._proper_names()
        #: Proper name -> every ref claiming it. Several models share a name:
        #: "accountbasedexpenseline" and "accountbasedexpenselinebill" both call
        #: themselves AccountBasedExpenseLine, and they are different shapes.
        self.claimants: dict[str, list[str]] = {}
        for ref, name in self.proper.items():
            self.claimants.setdefault(name, []).append(ref)
        #: Entity response models are named after their entity: the model ref
        #: "invoiceresponse" becomes Invoice, not Invoiceresponse.
        self.entity_class: dict[str, str] = {}
        self.needed_models: set[str] = set()
        self.needed_enums: set[str] = set()
        self.skipped: list[str] = []
        #: Field labels that collapse onto an attribute already emitted. The
        #: documentation contains genuine duplicates -- Entitlements declares
        #: both "Entitlement [0..n]" (with no definition at all) and
        #: "Entitlement" (typed TelephoneNumber, which it plainly is not). The
        #: first wins and the conflict is reported rather than resolved in
        #: silence.
        self.duplicates: list[str] = []

    def _proper_names(self) -> dict[str, str]:
        """Recover each model's PascalCase name from the references to it."""
        found: dict[str, str] = {}
        for model in self.models.values():
            for prop in as_dict(as_dict(model).get("properties")).values():
                raw = as_dict(prop).get("$ref")
                for item in as_list(raw) if isinstance(raw, list) else [raw]:
                    for key, value in as_dict(item).items():
                        if isinstance(value, str) and value:
                            found[key] = value
        return found

    def is_scalar_ref(self, ref: str) -> str:
        """The Python scalar a pseudo-model ref stands for, if it is one.

        The documentation models scalars as referenceable objects: ``ShipDate``
        points at a model called ``date`` whose proper name is ``Date``.
        Emitting a class for that would be wrong -- it is a plain date.
        """
        return SCALARS.get(self.proper.get(ref, ""), "")

    # -- discovery ---------------------------------------------------------

    def entity_refs(self) -> dict[str, str]:
        """Entity name -> its documented response model ref."""
        from qbo.entities import ENTITIES  # local: generated module

        refs: dict[str, str] = {}
        for name, raw in self.entities.items():
            if name not in ENTITIES:
                continue
            ref = as_str(as_dict(as_dict(raw).get("model")).get("$ref"))
            if ref and ref in self.models:
                refs[name] = ref
        return refs

    def walk(self, ref: str) -> None:
        """Mark a model and everything it references as needed."""
        if ref in self.needed_models or ref not in self.models:
            return
        if self.is_scalar_ref(ref):
            return
        self.needed_models.add(ref)
        for prop in as_dict(as_dict(self.models[ref]).get("properties")).values():
            self._walk_property(as_dict(prop))

    def _walk_property(self, prop: dict[str, Any]) -> None:
        declared = as_str(prop.get("type"))
        if declared.endswith("Enum") and declared in self.enums:
            self.needed_enums.add(declared)
        for target in self._refs(prop):
            self.walk(target)

    def _refs(self, prop: dict[str, Any]) -> list[str]:
        """The model refs a property points at; a list means a union."""
        raw = prop.get("$ref")
        out: list[str] = []
        if isinstance(raw, str):
            out.append(raw)
        for item in as_list(raw):
            if isinstance(item, str):
                out.append(item)
            else:
                out.extend(as_dict(item))
        return [r for r in out if r in self.models]

    # -- rendering ---------------------------------------------------------

    def annotation(self, prop: dict[str, Any]) -> str:
        refs = self._refs(prop)
        if refs:
            scalars = {self.is_scalar_ref(r) for r in refs if self.is_scalar_ref(r)}
            models = [r for r in refs if not self.is_scalar_ref(r)]
            names = sorted({self.internal_name(r) for r in models}) + sorted(scalars)
            inner = " | ".join(names) if names else "str"
        else:
            declared = as_str(prop.get("type"))
            if declared.endswith("Enum") and declared in self.enums:
                inner = _enum_class(declared)
            else:
                inner = SCALARS.get(declared, "")
                if not inner:
                    # An unmapped complex type: fall back to a permissive
                    # object rather than inventing a shape we cannot verify.
                    inner = "dict[str, Any]"
        return inner

    def internal_name(self, ref: str) -> str:
        """The name the class is *defined* under.

        QuickBooks routinely names a field after its own type -- ``Invoice`` has
        a ``CurrencyRef`` field of type ``CurrencyRef``. Since PEP 563 makes
        annotations strings, Pydantic resolves them against a namespace that
        includes the field defaults, so a bare ``CurrencyRef | None`` evaluates
        to ``None | None`` and raises. Defining every class with a trailing
        underscore and exporting an alias sidesteps that entirely: no QuickBooks
        field name ends in one, so an annotation can never be shadowed.
        """
        return f"{self.class_name(ref)}_"

    @staticmethod
    def _capitalise(value: str) -> str:
        return value[:1].upper() + value[1:] if value else ""

    def class_name(self, ref: str) -> str:
        """The public Python class name for a model ref.

        Names have to be unique, and Intuit's are not: five distinct models call
        themselves ``CustomField``. When a name is contested, the ref is split
        around it so the variants keep the shared stem and gain their
        distinguishing part -- ``accountbasedexpenselinebill`` becomes
        ``AccountBasedExpenseLineBill``, ``pcesalesitemline`` becomes
        ``PceSalesItemLine``. The canonical ref keeps the plain name.
        """
        entity = self.entity_class.get(ref)
        if entity:
            return entity

        proper = self.proper.get(ref, "")
        contested = len(self.claimants.get(proper, [])) > 1

        # A nested value object can share an entity's name while being a wholly
        # different shape: the `creditcardpayment` model is the embedded charge
        # block on Payment and SalesReceipt, not the CreditCardPayment entity.
        # The entity keeps the plain name; the embedded block takes a `Detail`
        # suffix, which is what QuickBooks itself calls such blocks elsewhere
        # (SalesItemLineDetail, JournalEntryLineDetail).
        if proper and proper in set(self.entity_class.values()):
            return f"{proper}Detail"
        # A "proper" name that is entirely lowercase is not one; two report-row
        # models share such a label, so fall back to the ref for both.
        if proper and not proper.islower() and not contested:
            return proper

        if proper and not proper.islower() and proper.lower() in ref:
            index = ref.index(proper.lower())
            prefix = ref[:index]
            suffix = ref[index + len(proper) :]
            return self._capitalise(prefix) + proper + self._capitalise(suffix)

        return self._capitalise(ref) or "Unknown"

    def render(self) -> str:
        entity_refs = self.entity_refs()
        # Name entity response models after the entity before anything is
        # rendered, so references to them resolve to the same class name.
        for entity, ref in entity_refs.items():
            self.entity_class.setdefault(ref, entity)
        for ref in entity_refs.values():
            self.walk(ref)

        lines: list[str] = []
        add = lines.append
        add('"""Pydantic models for the QuickBooks Online Accounting API.')
        add("")
        add("GENERATED by ``tools/generate_models.py``. Do not edit by hand.")
        add("")
        add("Structure and field requirements come from the documentation JSON;")
        add("enum members come from the XSD, which is the only source that lists")
        add("them. See ``API_COVERAGE.md``.")
        add("")
        add("Every field is optional. A response model that refuses to parse a")
        add("response the API legitimately returned is worse than one that admits")
        add("a field was absent -- partial reads, sparse updates and locale")
        add("differences all produce valid responses missing documented fields.")
        add("Request-side requirements are declared on ``EntitySpec`` in")
        add("``qbo.entities`` and enforced there instead.")
        add('"""')
        add("")
        add("from __future__ import annotations")
        add("")
        add("from datetime import date, datetime")
        add("from decimal import Decimal")
        add("from enum import StrEnum")
        add("from typing import Any")
        add("")
        add("from pydantic import BaseModel, ConfigDict")
        add("")
        add("")
        add("class QboModel(BaseModel):")
        add('    """Base for every generated model.')
        add("")
        add('    ``extra="allow"`` is deliberate: QuickBooks adds fields in new')
        add("    minorversions, and dropping them silently is how a field that")
        add("    exists on the wire becomes invisible to callers. Unknown fields")
        add("    are kept and reachable through ``model_extra``.")
        add('    """')
        add("")
        add("    model_config = ConfigDict(")
        add('        extra="allow",')
        add("        populate_by_name=True,")
        add("        str_strip_whitespace=True,")
        add("    )")
        add("")
        add("")

        for name in sorted(self.needed_enums):
            add(f"class {_enum_class(name)}(StrEnum):")
            add(f'    """{name}, values from the XSD."""')
            add("")
            seen: set[str] = set()
            for value in self.enums[name]:
                member = _member(value)
                while member in seen:
                    member += "_"
                seen.add(member)
                add(f'    {member} = "{value}"')
            add("")
            add("")

        for ref in sorted(self.needed_models, key=self.class_name):
            model = as_dict(self.models[ref])
            properties = as_dict(model.get("properties"))
            add(f"class {self.internal_name(ref)}(QboModel):")
            add(f'    """{self.class_name(ref)}."""')
            add("")
            emitted = 0
            seen_attributes: set[str] = set()
            for raw_name, raw_prop in properties.items():
                attribute = _identifier(raw_name)
                if not attribute:
                    self.skipped.append(f"{self.class_name(ref)}.{raw_name!r}")
                    continue
                if attribute in seen_attributes:
                    self.duplicates.append(f"{self.class_name(ref)}.{raw_name!r}")
                    continue
                seen_attributes.add(attribute)
                annotation = self.annotation(as_dict(raw_prop))
                if is_collection(raw_name):
                    annotation = f"list[{annotation}]"
                elif " | " in annotation:
                    annotation = f"({annotation})"
                add(f"    {attribute}: {annotation} | None = None")
                emitted += 1
            if not emitted:
                add("    pass")
            add("")
            add("")

        add("# Public aliases. Classes are defined with a trailing underscore so")
        add("# that a field named after its own type cannot shadow it; these are")
        add("# the names callers use.")
        for ref in sorted(self.needed_models, key=self.class_name):
            public, internal = self.class_name(ref), self.internal_name(ref)
            add(f"{internal}.__name__ = {public!r}")
            add(f"{internal}.__qualname__ = {public!r}")
            add(f"{public} = {internal}")
        add("")
        add("#: Documented entity name -> its response model.")
        add("ENTITY_MODELS: dict[str, type[QboModel]] = {")
        for entity, ref in sorted(entity_refs.items()):
            add(f'    "{entity}": {self.class_name(ref)},')
        add("}")
        add("")
        add("__all__ = [")
        add('    "QboModel",')
        add('    "ENTITY_MODELS",')
        for name in sorted(self.needed_enums):
            add(f'    "{_enum_class(name)}",')
        for ref in sorted(self.needed_models, key=self.class_name):
            add(f'    "{self.class_name(ref)}",')
        add("]")
        return "\n".join(lines) + "\n"


def render_init(generator: Generator, entity_refs: dict[str, str]) -> str:
    """Render the package __init__ with a static export list.

    Re-exporting through ``__getattr__`` works at runtime but leaves type
    checkers unable to see what the package exports, so the imports are written
    out explicitly.
    """
    names = sorted({generator.class_name(r) for r in generator.needed_models})
    enums = sorted(_enum_class(e) for e in generator.needed_enums)
    exported = ["QboModel", "ENTITY_MODELS", *enums, *names]

    lines: list[str] = []
    add = lines.append
    add('"""Typed models for QuickBooks Online entities.')
    add("")
    add("GENERATED by ``tools/generate_models.py``. Do not edit by hand.")
    add("")
    add("Import an entity model directly (``from qbo.models import Invoice``) or")
    add("look one up by API name with :func:`model_for`.")
    add('"""')
    add("")
    add("from __future__ import annotations")
    add("")
    add("from qbo.models._generated import (")
    for name in exported:
        add(f"    {name},")
    add(")")
    add("")
    add("__all__ = [")
    for name in [*exported, "model_for"]:
        add(f'    "{name}",')
    add("]")
    add("")
    add("")
    add("def model_for(entity: str) -> type[QboModel]:")
    add('    """The model class for a documented entity name.')
    add("")
    add("    Raises:")
    add("        KeyError: if the entity is not one the API documents.")
    add('    """')
    add("    model = ENTITY_MODELS.get(entity)")
    add("    if model is None:")
    add("        raise KeyError(")
    add('            f"Unknown entity {entity!r}. Documented entities: "')
    add('            + ", ".join(sorted(ENTITY_MODELS))')
    add("        )")
    add("    return model")
    return "\n".join(lines) + "\n"


def _ruff_format(path: Path) -> None:
    try:
        subprocess.run(
            ["ruff", "format", "--quiet", str(path)], check=True, capture_output=True
        )
        subprocess.run(
            ["ruff", "check", "--fix", "--quiet", str(path)], capture_output=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"warning: ruff unavailable ({exc}); output may be unformatted")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Pydantic models.")
    parser.add_argument("--check", action="store_true", help="Fail if out of date.")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "src"))
    generator = Generator()
    rendered = generator.render()
    TARGET.parent.mkdir(parents=True, exist_ok=True)

    if args.check:
        scratch = TARGET.with_suffix(".tmp.py")
        scratch.write_text(rendered)
        _ruff_format(scratch)
        current = scratch.read_text()
        scratch.unlink()
        if not TARGET.exists() or TARGET.read_text() != current:
            print("src/qbo/models/_generated.py is out of date.")
            return 1
        print("Models are current.")
        return 0

    TARGET.write_text(rendered)
    _ruff_format(TARGET)
    init = TARGET.parent / "__init__.py"
    init.write_text(render_init(generator, generator.entity_refs()))
    _ruff_format(init)
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)} and {init.relative_to(REPO_ROOT)}")
    print(f"  models : {len(generator.needed_models)}")
    print(f"  enums  : {len(generator.needed_enums)}")
    if generator.skipped:
        print(f"  skipped unusable field labels : {generator.skipped}")
    if generator.duplicates:
        print(f"  duplicate field labels dropped: {generator.duplicates}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
