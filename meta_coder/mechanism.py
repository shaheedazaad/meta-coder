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

from .manual import NOTES_FIELD_NAME, CodingManual, FieldSpec


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

# Why a field has no value. Every coded field except the built-in `notes` field
# carries one of these in `missing` when its `value` is null, and null otherwise.
MISSING_CODES = ("not_reported", "not_applicable", "unclear")


# The model's own confidence in a coded field, requested only when the manual
# sets `confidence: true`. Never asked of the built-in `notes` field.
CONFIDENCE_LEVELS = ("high", "medium", "low")


def _nullable(kind: str, type_map: dict[str, str], description: str) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": type_map[kind], "description": description}
    if type_map is JSON_SCHEMA_TYPE:
        schema["type"] = [type_map[kind], "null"]
    else:
        schema["nullable"] = True
    return schema


def _missing_schema(type_map: dict[str, str]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": type_map["string"],
        "description": "Why `value` is null; null when a value is given.",
        "enum": list(MISSING_CODES),
    }
    if type_map is JSON_SCHEMA_TYPE:
        schema["type"] = [type_map["string"], "null"]
        schema["enum"].append(None)
    else:
        schema["nullable"] = True
    return schema


def _value_schema(spec: FieldSpec, type_map: dict[str, str]) -> dict[str, Any]:
    if spec.multiple:
        # A list of levels. The enum sits on the items, and "nothing applies" is
        # a null value with a `missing` code, never an empty list.
        schema: dict[str, Any] = {
            "type": type_map["array"],
            "items": {
                "type": type_map["string"],
                "enum": [level.value for level in spec.levels],
            },
        }
        if type_map is JSON_SCHEMA_TYPE:
            schema["type"] = [type_map["array"], "null"]
        else:
            schema["nullable"] = True
        return schema
    schema = {"type": type_map[spec.type]}
    if type_map is JSON_SCHEMA_TYPE:
        schema["type"] = [type_map[spec.type], "null"]
    else:
        schema["nullable"] = True
    if spec.levels:
        schema["enum"] = [level.value for level in spec.levels]
        if type_map is JSON_SCHEMA_TYPE:
            schema["enum"].append(None)
    return schema


def _coded_field_schema(
    name: str, spec: FieldSpec, type_map: dict[str, str], *, confidence: bool = False
) -> dict[str, Any]:
    properties: dict[str, Any] = {"value": _value_schema(spec, type_map)}
    required = ["value"]
    if name != NOTES_FIELD_NAME:
        properties["missing"] = _missing_schema(type_map)
        required.append("missing")
    if confidence and name != NOTES_FIELD_NAME:
        properties["confidence"] = {
            "type": type_map["string"],
            "description": "How confident you are in this field's value or missing reason.",
            "enum": list(CONFIDENCE_LEVELS),
        }
        required.append("confidence")
    if spec.evidence_required:
        properties["evidence"] = {
            "type": type_map["string"],
            "description": "Where in the article this value comes from and how it was derived.",
        }
        properties["quote"] = _nullable(
            "string",
            type_map,
            "Passage supporting this value, copied exactly from the article; null if "
            "the value comes from a table or figure or nothing can be quoted.",
        )
        properties["page"] = _nullable(
            "integer",
            type_map,
            "Page of the PDF file the passage or value is on, counting its first page "
            "as 1; null if unknown.",
        )
        required.extend(["evidence", "quote", "page"])
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

    Every manual field is required in each row. `value: null` means no value
    could be coded, with `missing` giving the reason (see MISSING_CODES);
    omitting the field is never valid. The JSON
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
        effect_properties[name] = _coded_field_schema(
            name, spec, type_map, confidence=manual.confidence
        )
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
    if spec.multiple:
        # At least one level, each listed in the manual, none repeated.
        allowed = {level.value for level in spec.levels}
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item in allowed for item in value)
            and len(set(value)) == len(value)
        )
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


def _valid_missing_code(field: dict) -> bool:
    """A null value needs a reason; a coded value must not carry one."""

    code = field.get("missing")
    if field["value"] is None:
        return isinstance(code, str) and code in MISSING_CODES
    return code is None


def _valid_quote(field: dict) -> bool:
    """`quote` is text or null and `page` a whole number or null. Either may be
    left out by an endpoint that does not enforce the schema."""

    quote = field.get("quote")
    page = field.get("page")
    return (quote is None or isinstance(quote, str)) and (
        page is None or (isinstance(page, int) and not isinstance(page, bool))
    )


def validate_response(
    parsed: object,
    requested_row_ids: set[str],
    expected_fields: dict[str, FieldSpec] | set[str] | None = None,
    *,
    confidence: bool = False,
) -> ValidationResult:
    """Hard-validate a parsed response against the coding-sheet rows that were
    requested for one PDF.

    Deliberately does NOT trust positional alignment: the returned row_id set must
    equal the requested set exactly, or the whole PDF is flagged needs_review. A
    mismatch is never partially accepted. When `expected_fields` is supplied,
    every returned row must also include every manual field. `confidence` is
    the manual's setting: when true, every field except `notes` must carry one
    of CONFIDENCE_LEVELS.
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
                elif (
                    isinstance(expected_fields, dict)
                    and name != NOTES_FIELD_NAME
                    and not _valid_missing_code(field)
                ):
                    names.append(name)
                elif (
                    confidence
                    and name != NOTES_FIELD_NAME
                    and field.get("confidence") not in CONFIDENCE_LEVELS
                ):
                    names.append(name)
                elif isinstance(expected_fields, dict) and expected_fields[name].evidence_required:
                    if (
                        not isinstance(field.get("evidence"), str)
                        or not field["evidence"].strip()
                        or not _valid_quote(field)
                    ):
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
