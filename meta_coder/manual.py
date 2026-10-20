"""Coding manual = extraction schema, parsed from YAML.

See plan.md "Problem 1". A coding manual is the single source of truth for what
gets coded: an `effect_definition` (what comparison counts as "the effect" for this
meta-analysis) and a set of `effects` fields (LLM-coded, each optionally categorical
via `levels`). The coding sheet's own columns (paper identification, effect ID,
effect location — see coding_sheet.py) are a fixed schema, not manual-configurable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_TYPES = {"string", "number", "integer", "boolean"}

# Columns MetaCoder itself writes at the start of every coded_data.csv /
# evidence.csv row (see results.py); `row_id` is also the key of each coded
# effect in the model's response schema (see mechanism.py). A manual field with
# one of these names would overwrite that column or response key, so they're
# rejected — case-insensitively, since `Year` next to `year` in a spreadsheet
# header is just as confusing (and some tools, e.g. SPSS, ignore case).
BASE_COLUMNS = ("row_id", "source_pdf", "locator", "authors", "year", "status")
_RESERVED_NAME_SUGGESTIONS = {"year": "publication_year", "status": "publication_status"}

# Joins the selected levels of a `multiple` field into one coded_data.csv cell
# (see results.py), so a level of such a field cannot contain the separator.
MULTIPLE_SEPARATOR = "; "

# Every manual gets this effect field forced onto it — see `_build_manual_from_raw`,
# which injects it after parsing and discards whatever the caller supplied for it.
# Not sourced from the PDF text itself, so `evidence_required` is False: there's
# nothing to cite a page/quote for.
NOTES_FIELD_NAME = "notes"
NOTES_FIELD_DESCRIPTION = (
    "Explain any issues coding this effect for this row: the value could not be "
    "located, had to be inferred or approximated, was ambiguous between multiple "
    "candidates, or came from outside the main text (e.g. supplementary materials, "
    "an appendix). Leave empty only if nothing about coding this row was noteworthy."
)


class ManualError(ValueError):
    """Raised for any structurally invalid coding manual."""


@dataclass
class Level:
    value: str
    description: str | None = None


@dataclass
class FieldSpec:
    type: str
    description: str | None = None
    levels: list[Level] = field(default_factory=list)
    evidence_required: bool = True
    # Categorical fields only: the model may select several levels, not one.
    multiple: bool = False

    @property
    def is_categorical(self) -> bool:
        return bool(self.levels)


def _notes_field_spec() -> FieldSpec:
    return FieldSpec(
        type="string",
        description=NOTES_FIELD_DESCRIPTION,
        levels=[],
        evidence_required=False,
    )


@dataclass
class CodingManual:
    name: str
    description: str | None
    effect_definition: str
    effects: dict[str, FieldSpec]
    raw_text: str = ""

    @property
    def is_complete(self) -> bool:
        """True once the manual has real content, not just the unedited starter
        manual's blank `effect_definition` (see `parse_coding_manual`'s
        `require_effect_definition=False` display path — that path lets an
        incomplete manual still parse so the structured editor can render it,
        but callers must check this before treating it as run-ready)."""
        return bool(self.effect_definition)


def _require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ManualError(f"`{label}` must be a mapping.")
    return value


def _parse_levels(raw: object, field_name: str) -> list[Level]:
    if raw is None or raw == []:
        # An empty list means "no levels" (the structured editor always sends
        # `levels: []` for non-categorical fields) — only a non-empty non-list
        # value is a real error.
        return []
    if not isinstance(raw, list):
        raise ManualError(f"effects.{field_name}.levels must be a list.")
    levels: list[Level] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or "value" not in entry:
            raise ManualError(
                f"effects.{field_name}.levels[{index}] must be a mapping with a `value` key."
            )
        raw_value = entry["value"]
        if raw_value is None:
            # Unquoted `value:` / `value: null` in YAML — reject rather than
            # turning it into a level called "None".
            raise ManualError(
                f"effects.{field_name}.levels[{index}].value cannot be empty "
                "(put quotes around it if the level is literally called `null`)."
            )
        if isinstance(raw_value, bool):
            # Only `true`/`false` resolve to booleans (see _ManualLoader); keep
            # that spelling rather than Python's `True`/`False`.
            raw_value = "true" if raw_value else "false"
        value = str(raw_value).strip()
        if not value:
            raise ManualError(f"effects.{field_name}.levels[{index}].value cannot be empty.")
        if value in seen:
            raise ManualError(f"effects.{field_name}.levels has a duplicate value: `{value}`.")
        seen.add(value)
        description = entry.get("description")
        levels.append(Level(value=value, description=str(description) if description else None))
    return levels


def _parse_field(name: str, raw: object, section: str) -> FieldSpec:
    if not isinstance(raw, dict):
        raise ManualError(f"{section}.{name} must be a mapping.")
    field_type = str(raw.get("type") or "string").strip().lower()
    if field_type not in SUPPORTED_TYPES:
        raise ManualError(
            f"{section}.{name}.type `{field_type}` is not supported "
            f"(use one of: {', '.join(sorted(SUPPORTED_TYPES))})."
        )
    # `required` was configurable in older manuals. Keep accepting it so those
    # manuals import cleanly, but field inclusion is now unconditional.
    legacy_required = raw.get("required", False)
    if not isinstance(legacy_required, bool):
        raise ManualError(f"{section}.{name}.required must be true or false.")
    evidence_required = raw.get("evidence_required", True)
    if not isinstance(evidence_required, bool):
        raise ManualError(f"{section}.{name}.evidence_required must be true or false.")
    levels = _parse_levels(raw.get("levels"), name) if section == "effects" else []
    if levels and field_type != "string":
        raise ManualError(f"effects.{name} has `levels` but type is not `string`.")
    multiple = raw.get("multiple", False)
    if not isinstance(multiple, bool):
        raise ManualError(f"{section}.{name}.multiple must be true or false.")
    if multiple and not levels:
        raise ManualError(
            f"{section}.{name} has `multiple: true` but no `levels`; only a categorical "
            "field can allow several levels."
        )
    if multiple:
        for level in levels:
            if MULTIPLE_SEPARATOR.strip() in level.value:
                raise ManualError(
                    f"{section}.{name}.levels has the value `{level.value}`, but a level of a "
                    "`multiple` field cannot contain a semicolon: selected levels are "
                    "separated by semicolons in the export."
                )
    description = raw.get("description")
    return FieldSpec(
        type=field_type,
        description=str(description).strip() if description else None,
        levels=levels,
        evidence_required=evidence_required,
        multiple=multiple,
    )


def _parse_section(raw: object, section: str, *, allow_empty: bool) -> dict[str, FieldSpec]:
    if raw is None:
        if allow_empty:
            return {}
        raise ManualError(f"`{section}` must contain at least one field.")
    mapping = _require_mapping(raw, section)
    if not mapping and not allow_empty:
        raise ManualError(f"`{section}` must contain at least one field.")
    out: dict[str, FieldSpec] = {}
    seen: dict[str, str] = {}
    for name, spec in mapping.items():
        field_name = str(name or "").strip()
        if not field_name:
            raise ManualError(f"Every field in `{section}` must be named.")
        key = field_name.casefold()
        if key in BASE_COLUMNS:
            suggestion = _RESERVED_NAME_SUGGESTIONS.get(key, f"study_{key}")
            raise ManualError(
                f"{section}.{field_name}: `{field_name}` is reserved for a column MetaCoder "
                f"adds to every export ({', '.join(BASE_COLUMNS)}). Rename the field, "
                f"e.g. to `{suggestion}`."
            )
        if key == NOTES_FIELD_NAME and field_name != NOTES_FIELD_NAME:
            # Exact `notes` is silently replaced by the built-in field (see
            # _build_manual_from_raw); a case variant would sit beside it.
            raise ManualError(
                f"{section}.{field_name}: `{field_name}` clashes with the built-in "
                f"`{NOTES_FIELD_NAME}` field, which MetaCoder adds automatically. Remove "
                "this field or give it a more specific name."
            )
        if key in seen:
            raise ManualError(
                f"`{section}` has fields named both `{seen[key]}` and `{field_name}`; "
                "field names must differ by more than spaces or capitalization."
            )
        seen[key] = field_name
        out[field_name] = _parse_field(field_name, spec, section)
    return out


def _build_manual_from_raw(
    raw: object, *, raw_text: str = "", require_effect_definition: bool = True
) -> CodingManual:
    """Shared validation core: `raw` is the plain dict/list structure produced by
    either _ManualLoader (text path) or json.loads (structured-editor path) — both
    parse into the same basic Python types, so one validator serves both.
    """

    if not isinstance(raw, dict):
        raise ManualError("The coding manual must contain a top-level mapping.")

    effect_definition = str(raw.get("effect_definition") or "").strip()
    if require_effect_definition and not effect_definition:
        raise ManualError(
            "`effect_definition` is required: state what comparison counts as "
            "\"the effect\" for this meta-analysis, e.g. \"the difference in response "
            "times between compatible and incompatible trials.\""
        )

    name = str(raw.get("name") or "untitled_meta_analysis").strip()
    description = raw.get("description")

    effects = _parse_section(raw.get("effects"), "effects", allow_empty=False)

    # Force this onto every manual, discarding whatever the caller supplied for
    # it (if anything) — see NOTES_FIELD_NAME above. Popped first so it always
    # lands last regardless of where it appeared in the input.
    effects.pop(NOTES_FIELD_NAME, None)
    effects[NOTES_FIELD_NAME] = _notes_field_spec()

    return CodingManual(
        name=name,
        description=str(description).strip() if description else None,
        effect_definition=effect_definition,
        effects=effects,
        raw_text=raw_text,
    )


class _ManualLoader(yaml.SafeLoader):
    """SafeLoader tuned for coding manuals, without touching PyYAML's globals.

    - Duplicate mapping keys are an error (PyYAML silently keeps the last one,
      which would quietly drop a field, level, or instruction).
    - Only `true`/`false` are booleans (YAML 1.2 style), so category labels
      such as `yes`/`no`/`on`/`off` stay strings.
    - No implicit int/float/timestamp resolution: nothing in a manual is
      numeric, so unquoted `01` or `1.50` keep their exact text instead of
      becoming `1` / `1.5`. `null`, `~` and empty values still mean "no value".
    """

    def construct_mapping(self, node, deep=False):
        seen: set[object] = set()
        for key_node, _value_node in node.value:
            # Merge keys (`<<: *anchor`) and complex keys are left to PyYAML.
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key `{key}` (each key may appear only once)",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_KEPT_YAML_RESOLVERS = {"tag:yaml.org,2002:null", "tag:yaml.org,2002:merge"}
# Assigning a fresh dict on the subclass leaves SafeLoader's own resolvers intact.
_ManualLoader.yaml_implicit_resolvers = {
    first: kept
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    if (kept := [(tag, regexp) for tag, regexp in resolvers if tag in _KEPT_YAML_RESOLVERS])
}
_ManualLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def parse_coding_manual(text: str, *, require_effect_definition: bool = True) -> CodingManual:
    try:
        raw = yaml.load(text, Loader=_ManualLoader) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = str(getattr(exc, "problem", "") or "The YAML could not be parsed.")
        if mark is not None:
            raise ManualError(
                f"Invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"
            ) from exc
        raise ManualError(f"Invalid YAML: {problem}") from exc
    return _build_manual_from_raw(
        raw, raw_text=text, require_effect_definition=require_effect_definition
    )


def read_coding_manual(path: Path, *, require_effect_definition: bool = True) -> CodingManual:
    if not path.is_file():
        raise FileNotFoundError(f"Coding manual not found: {path}")
    return parse_coding_manual(
        path.read_text(encoding="utf-8"), require_effect_definition=require_effect_definition
    )


# --- Structured-editor payload <-> CodingManual -----------------------------
# The GUI editor (project.html) is the only user-facing way to edit a manual — see
# plan.md "Problem 1". It works with a JSON-friendly, list-based shape (arrays are
# natural for add/remove/reorder in JS) rather than the YAML file's dict-keyed
# shape; these functions convert between the two. The YAML file on disk stays the
# storage format (plan.md: "the manual *is* the schema"), just never hand-edited.

def _payload_list_to_mapping(items: object, section: str) -> dict:
    if not isinstance(items, list):
        raise ManualError(f"`{section}` must be a list.")
    out: dict[str, dict] = {}
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ManualError(f"{section}[{index}] must be an object.")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ManualError(f"{section}[{index}] is missing a field name.")
        if name in out:
            raise ManualError(f"`{section}` has a duplicate field name: `{name}`.")
        out[name] = {k: v for k, v in item.items() if k != "name"}
    return out


def manual_from_editor_payload(payload: object) -> CodingManual:
    if not isinstance(payload, dict):
        raise ManualError("Invalid manual data.")
    raw = {
        "name": payload.get("name"),
        "description": payload.get("description"),
        "effect_definition": payload.get("effect_definition"),
        "effects": _payload_list_to_mapping(payload.get("effects") or [], "effects"),
    }
    return _build_manual_from_raw(raw)


def _field_to_payload(name: str, spec: FieldSpec) -> dict[str, Any]:
    return {
        "name": name,
        "type": spec.type,
        "description": spec.description or "",
        "evidence_required": spec.evidence_required,
        "multiple": spec.multiple,
        "levels": [{"value": level.value, "description": level.description or ""} for level in spec.levels],
    }


def manual_to_editor_payload(manual: CodingManual) -> dict[str, Any]:
    return {
        "name": manual.name,
        "description": manual.description or "",
        "effect_definition": manual.effect_definition,
        "effects": [_field_to_payload(name, spec) for name, spec in manual.effects.items()],
    }


def manual_to_yaml_text(manual: CodingManual) -> str:
    """Serialize a CodingManual to YAML for on-disk storage, via PyYAML (not a
    hand-rolled emitter) so quoting/escaping is always correct. The default
    dumper quotes any string YAML 1.1 would read as another type (`yes`, `01`,
    `1.50`, `null`) — a superset of what _ManualLoader resolves — so every
    value re-imports unchanged."""

    def field_dict(spec: FieldSpec) -> dict[str, Any]:
        out: dict[str, Any] = {"type": spec.type}
        if spec.description:
            out["description"] = spec.description
        if not spec.evidence_required:
            out["evidence_required"] = False
        if spec.multiple:
            out["multiple"] = True
        if spec.levels:
            out["levels"] = [
                ({"value": level.value, "description": level.description} if level.description else {"value": level.value})
                for level in spec.levels
            ]
        return out

    data: dict[str, Any] = {"name": manual.name}
    if manual.description:
        data["description"] = manual.description
    data["effect_definition"] = manual.effect_definition
    data["effects"] = {name: field_dict(spec) for name, spec in manual.effects.items()}

    return yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)
