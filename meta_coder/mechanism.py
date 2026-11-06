"""The core mechanism under test (plan.md "Problem 2"): compile a coding manual into
a provider structured-output schema, and hard-validate that a parsed response's
coding-sheet row IDs exactly match what was requested.

Both functions here are pure — no network, no filesystem — so the mechanism can be
proven correct with hand-written fake responses before spending a single API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .manual import CodingManual, FieldSpec


# Two response-schema dialects share this same builder: Gemini's `responseSchema`
# (uppercase type names, e.g. "STRING"/"OBJECT"/"ARRAY") and standard JSON Schema
# (lowercase, "string"/"object"/"array") used for OpenRouter/OpenAI-style
# `response_format.json_schema.schema`. Everything else about the shape — which
# fields are required, evidence-per-field — is identical across both; only the
# type-name casing differs, so one builder parameterized by dialect serves both
# rather than duplicating the whole schema-construction logic per provider.
GEMINI_TYPE = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}
JSON_SCHEMA_TYPE = {
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}
DIALECTS = {"gemini": GEMINI_TYPE, "json_schema": JSON_SCHEMA_TYPE}


def _value_schema(spec: FieldSpec, type_map: dict[str, str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": type_map[spec.type]}
    if type_map is JSON_SCHEMA_TYPE:
        schema["type"] = [type_map[spec.type], "null"]
    else:
        schema["nullable"] = True
    if spec.levels:
        schema["enum"] = [level.value for level in spec.levels]
        if type_map is JSON_SCHEMA_TYPE:
            schema["enum"].append(None)
    return schema


def _coded_field_schema(name: str, spec: FieldSpec, type_map: dict[str, str]) -> dict[str, Any]:
    properties: dict[str, Any] = {"value": _value_schema(spec, type_map)}
    required = ["value"]
    if spec.evidence_required:
        properties["evidence"] = {
            "type": type_map["string"],
            "description": "Page number and/or short quotation supporting this value.",
        }
        required.append("evidence")
    schema = {
        "type": type_map["object"],
        "description": spec.description or f"Coded value for `{name}`.",
        "properties": properties,
        "required": required,
    }
    if type_map is JSON_SCHEMA_TYPE:
        schema["additionalProperties"] = False
    return schema


def build_response_schema(manual: CodingManual, *, dialect: str = "gemini") -> dict[str, Any]:
    """Compile `manual.effects` into a structured-output response schema.

    The coding sheet's own columns (paper identification, effect ID, effect
    location) are deliberately excluded — they are never requested of the model
    and are rejoined from the coding sheet at collation time.

    Every manual field is required in each row. `value: null` means the article
    does not report that value; omitting the field is never valid. The JSON
    Schema dialect is closed (`additionalProperties: false`) so OpenRouter can
    enforce it with strict structured output.
    """

    if dialect not in DIALECTS:
        raise ValueError(f"Unknown schema dialect: {dialect!r} (use one of {sorted(DIALECTS)}).")
    type_map = DIALECTS[dialect]

    if not manual.effects:
        raise ValueError("The coding manual must define at least one effect field.")

    effect_properties: dict[str, Any] = {
        "row_id": {
            "type": type_map["string"],
            "description": (
                "Must exactly match one of the requested coding-sheet row IDs. "
                "Never invent, rename, or omit this value."
            ),
        }
    }
    required = ["row_id"]
    for name, spec in manual.effects.items():
        effect_properties[name] = _coded_field_schema(name, spec, type_map)
        required.append(name)

    item_schema: dict[str, Any] = {
        "type": type_map["object"],
        "properties": effect_properties,
        "required": required,
    }
    schema: dict[str, Any] = {
        "type": type_map["object"],
        "properties": {
            "effects": {
                "type": type_map["array"],
                "description": "One entry per requested coding-sheet row.",
                "items": item_schema,
            }
        },
        "required": ["effects"],
    }
    if type_map is JSON_SCHEMA_TYPE:
        item_schema["additionalProperties"] = False
        schema["additionalProperties"] = False
    return schema


@dataclass
class ValidationResult:
    ok: bool
    coded_by_row_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_ids: set[str] = field(default_factory=set)
    extra_ids: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def needs_review(self) -> bool:
        return not self.ok


def _valid_field_value(value: object, spec: FieldSpec) -> bool:
    if value is None:
        return True
    if spec.type == "string":
        return isinstance(value, str) and (not spec.levels or value in {level.value for level in spec.levels})
    if spec.type == "number":
        # Python's json module accepts NaN/Infinity, which no paper reports.
        # Ints are always finite (and huge ones would overflow math.isfinite).
        if isinstance(value, float):
            return math.isfinite(value)
        return isinstance(value, int) and not isinstance(value, bool)
    if spec.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, bool)


def validate_response(
    parsed: object,
    requested_row_ids: set[str],
    expected_fields: dict[str, FieldSpec] | set[str] | None = None,
) -> ValidationResult:
    """Hard-validate a parsed response against the coding-sheet rows that were
    requested for one PDF.

    Deliberately does NOT trust positional alignment: the returned row_id set must
    equal the requested set exactly, or the whole PDF is flagged needs_review. A
    mismatch is never partially accepted. When `expected_fields` is supplied,
    every returned row must also include every manual field.
    """

    if not isinstance(parsed, dict):
        return ValidationResult(ok=False, error="Response is not a JSON object.")
    effects = parsed.get("effects")
    if not isinstance(effects, list):
        return ValidationResult(ok=False, error="Response is missing an `effects` array.")

    coded_by_row_id: dict[str, dict[str, Any]] = {}
    returned_ids: set[str] = set()
    for index, item in enumerate(effects, start=1):
        # row_id must be the exact requested string: a number or a padded copy
        # is never coerced into a match.
        row_id = item.get("row_id") if isinstance(item, dict) else None
        if not isinstance(row_id, str) or not row_id.strip():
            return ValidationResult(
                ok=False, error=f"effects[{index}] is missing a non-empty string `row_id`."
            )
        if row_id in returned_ids:
            return ValidationResult(ok=False, error=f"Duplicate row_id in response: `{row_id}`.")
        returned_ids.add(row_id)
        coded_by_row_id[row_id] = {k: v for k, v in item.items() if k != "row_id"}

    if expected_fields:
        expected_names = set(expected_fields)
        incomplete = {
            row_id: expected_names - set(fields)
            for row_id, fields in coded_by_row_id.items()
            if expected_names - set(fields)
        }
        if incomplete:
            details = "; ".join(
                f"`{row_id}`: {', '.join(sorted(fields))}" for row_id, fields in incomplete.items()
            )
            return ValidationResult(
                ok=False,
                coded_by_row_id=coded_by_row_id,
                error=f"Response omitted required field(s): {details}.",
            )
        malformed = {
            row_id: sorted(name for name in expected_names if not isinstance(fields[name], dict))
            for row_id, fields in coded_by_row_id.items()
        }
        malformed = {row_id: names for row_id, names in malformed.items() if names}
        if malformed:
            details = "; ".join(
                f"`{row_id}`: {', '.join(names)}" for row_id, names in malformed.items()
            )
            return ValidationResult(
                ok=False,
                coded_by_row_id=coded_by_row_id,
                error=f"Response returned malformed field value(s): {details}.",
            )
        invalid = {}
        for row_id, fields in coded_by_row_id.items():
            names = []
            for name in expected_names:
                field = fields[name]
                if "value" not in field or (
                    isinstance(expected_fields, dict)
                    and not _valid_field_value(field["value"], expected_fields[name])
                ):
                    names.append(name)
                elif isinstance(expected_fields, dict) and expected_fields[name].evidence_required:
                    if not isinstance(field.get("evidence"), str) or not field["evidence"].strip():
                        names.append(name)
            if names:
                invalid[row_id] = sorted(names)
        if invalid:
            details = "; ".join(
                f"`{row_id}`: {', '.join(names)}" for row_id, names in invalid.items()
            )
            return ValidationResult(
                ok=False,
                coded_by_row_id=coded_by_row_id,
                error=f"Response returned invalid field value(s): {details}.",
            )

    if returned_ids != requested_row_ids:
        return ValidationResult(
            ok=False,
            coded_by_row_id=coded_by_row_id,
            missing_ids=requested_row_ids - returned_ids,
            extra_ids=returned_ids - requested_row_ids,
            error="Returned row_id set does not match the requested set.",
        )

    return ValidationResult(ok=True, coded_by_row_id=coded_by_row_id)
