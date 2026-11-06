import pytest
import yaml

from meta_coder import results
from meta_coder.manual import (
    BASE_COLUMNS,
    NOTES_FIELD_NAME,
    ManualError,
    manual_from_editor_payload,
    manual_to_editor_payload,
    manual_to_yaml_text,
    parse_coding_manual,
)


MANUAL_YAML = """
name: stroop_test
effect_definition: The difference in RT between compatible and incompatible trials.
effects:
  Condition:
    type: string
    required: true
    levels:
      - {value: "No", description: Not incompatible.}
      - {value: "Yes", description: Incompatible.}
  ResponseTimeMs:
    type: number
    evidence_required: false
"""


def test_manual_to_editor_payload_is_json_safe_round_trip():
    manual = parse_coding_manual(MANUAL_YAML)
    payload = manual_to_editor_payload(manual)
    # Must survive an actual JSON round-trip, since this is exactly what the
    # browser does (JSON.stringify -> POST -> json.loads server-side).
    import json

    round_tripped = json.loads(json.dumps(payload))
    rebuilt = manual_from_editor_payload(round_tripped)

    assert "required" not in payload["effects"][0]
    assert rebuilt.effect_definition == manual.effect_definition
    assert set(rebuilt.effects) == {"Condition", "ResponseTimeMs", "notes"}
    assert [level.value for level in rebuilt.effects["Condition"].levels] == ["No", "Yes"]
    assert rebuilt.effects["ResponseTimeMs"].evidence_required is False


def test_legacy_required_flag_imports_but_is_not_saved():
    manual = parse_coding_manual(MANUAL_YAML)
    import yaml

    saved = yaml.safe_load(manual_to_yaml_text(manual))
    assert "required" not in saved["effects"]["Condition"]


def test_manual_from_editor_payload_rejects_duplicate_field_names():
    payload = {
        "effect_definition": "x",
        "effects": [
            {"name": "Age", "type": "string"},
            {"name": "Age", "type": "number"},
        ],
    }
    try:
        manual_from_editor_payload(payload)
        assert False, "expected ManualError"
    except Exception as exc:
        assert "duplicate" in str(exc).lower()


def test_notes_field_is_always_added():
    manual = parse_coding_manual(MANUAL_YAML)
    notes = manual.effects[NOTES_FIELD_NAME]
    assert notes.evidence_required is False


def test_notes_field_cannot_be_redefined_or_removed():
    # An attempt to redefine `notes` (wrong type, not required, with levels) is
    # silently overridden back to the canonical spec rather than honored — the
    # field's whole point is that it's always present in the same shape.
    payload = {
        "effect_definition": "x",
        "effects": [
            {"name": "Age", "type": "string"},
            {"name": NOTES_FIELD_NAME, "type": "boolean", "required": False},
        ],
    }
    manual = manual_from_editor_payload(payload)
    notes = manual.effects[NOTES_FIELD_NAME]
    assert notes.type == "string"
    assert notes.evidence_required is False

    # Omitting it entirely still results in it being present.
    payload_without_notes = {
        "effect_definition": "x",
        "effects": [{"name": "Age", "type": "string"}],
    }
    manual = manual_from_editor_payload(payload_without_notes)
    assert NOTES_FIELD_NAME in manual.effects


def test_manual_to_yaml_text_produces_reparseable_yaml_with_yes_no_levels():
    # "No"/"Yes" are YAML 1.1 boolean words — this is the exact case that broke a
    # hand-rolled emitter would need to get right; PyYAML's dump handles it.
    manual = parse_coding_manual(MANUAL_YAML)
    text = manual_to_yaml_text(manual)
    reparsed = parse_coding_manual(text)
    assert [level.value for level in reparsed.effects["Condition"].levels] == ["No", "Yes"]
    assert reparsed.effect_definition == manual.effect_definition


# --- Reserved and colliding field names --------------------------------------

def _yaml_manual(effects: str) -> str:
    return f"effect_definition: comparison\neffects:\n{effects}"


def test_export_columns_have_a_single_source_of_truth():
    assert results.BASE_COLUMNS is BASE_COLUMNS


@pytest.mark.parametrize(("name", "suggestion"), [
    ("row_id", "study_row_id"),
    ("year", "publication_year"),
    (" Year ", "publication_year"),
    ("STATUS", "publication_status"),
    ("Source_PDF", "study_source_pdf"),
    ("authors", "study_authors"),
    ("locator", "study_locator"),
])
def test_reserved_field_names_are_rejected_in_yaml_and_editor(name, suggestion):
    with pytest.raises(ManualError, match=rf"is reserved .*Rename the field, e.g. to `{suggestion}`"):
        parse_coding_manual(_yaml_manual(f"  {name!r}: {{type: string}}\n"))
    with pytest.raises(ManualError, match="is reserved"):
        manual_from_editor_payload({"effect_definition": "x", "effects": [{"name": name, "type": "string"}]})


@pytest.mark.parametrize("name", ["Notes", "NOTES", " Notes "])
def test_case_variants_of_builtin_notes_field_are_rejected(name):
    with pytest.raises(ManualError, match="clashes with the built-in `notes` field"):
        parse_coding_manual(_yaml_manual(f"  {name!r}: {{type: string}}\n"))
    with pytest.raises(ManualError, match="clashes with the built-in `notes` field"):
        manual_from_editor_payload({"effect_definition": "x", "effects": [{"name": name}]})
    # Exact `notes` (after trimming) is still silently replaced by the built-in field.
    manual = parse_coding_manual(_yaml_manual("  ' notes ': {type: boolean}\n  age: {}\n"))
    assert list(manual.effects) == ["age", NOTES_FIELD_NAME]
    assert manual.effects[NOTES_FIELD_NAME].type == "string"


def test_reserved_name_lookalikes_are_allowed():
    manual = parse_coding_manual(_yaml_manual("  publication_year: {type: integer}\n  years_of_age: {}\n"))
    assert list(manual.effects) == ["publication_year", "years_of_age", NOTES_FIELD_NAME]


def test_field_names_colliding_after_trimming_or_case_are_rejected():
    with pytest.raises(ManualError, match="both `Age` and `Age`"):
        parse_coding_manual(_yaml_manual("  Age: {}\n  ' Age ': {}\n"))
    with pytest.raises(ManualError, match="both `Age` and `age`"):
        parse_coding_manual(_yaml_manual("  Age: {}\n  age: {}\n"))
    with pytest.raises(ManualError, match="both `Age` and `AGE`"):
        manual_from_editor_payload(
            {"effect_definition": "x", "effects": [{"name": "Age"}, {"name": "AGE "}]}
        )


# --- Faithful YAML parsing ----------------------------------------------------

@pytest.mark.parametrize(("text", "line", "key"), [
    ("effect_definition: a\neffect_definition: b\neffects:\n  x: {}\n", 2, "effect_definition"),
    (_yaml_manual("  age: {type: number}\n  age: {type: string}\n"), 4, "age"),
    (_yaml_manual("  age:\n    type: number\n    type: string\n"), 5, "type"),
    (_yaml_manual("  arm:\n    levels:\n      - {value: a, value: b}\n"), 5, "value"),
])
def test_duplicate_yaml_keys_are_rejected_with_location(text, line, key):
    with pytest.raises(ManualError, match=rf"Invalid YAML at line {line}, column \d+: duplicate key `{key}`"):
        parse_coding_manual(text)


def test_yaml_merge_keys_still_work_and_complex_keys_still_fail():
    manual = parse_coding_manual(
        "base: &base {type: string, evidence_required: false}\n"
        "effect_definition: comparison\n"
        "effects:\n  arm:\n    <<: *base\n    description: Arm.\n"
    )
    assert manual.effects["arm"].evidence_required is False
    with pytest.raises(ManualError, match="Invalid YAML"):
        parse_coding_manual("? [a, b]\n: c\n")


def test_category_level_values_keep_their_original_text():
    manual = parse_coding_manual(_yaml_manual(
        "  answer:\n    levels:\n"
        "      - value: yes\n      - value: No\n      - value: on\n      - value: off\n"
        "      - value: 01\n      - value: 1.50\n      - value: 2024-01-01\n"
        "      - value: true\n      - value: False\n      - value: 'null'\n      - value: =\n"
        "  flag: {type: boolean, evidence_required: false, description: 1.0}\n"
        "  007: {}\n"
    ))
    values = [level.value for level in manual.effects["answer"].levels]
    assert values == ["yes", "No", "on", "off", "01", "1.50", "2024-01-01", "true", "false", "null", "="]
    assert manual.effects["flag"].evidence_required is False
    assert manual.effects["flag"].description == "1.0"
    assert "007" in manual.effects


@pytest.mark.parametrize("value", ["", " null", " ~", " Null"])
def test_null_level_values_are_rejected_not_stringified(value):
    with pytest.raises(ManualError, match=r"levels\[1\]\.value cannot be empty"):
        parse_coding_manual(_yaml_manual(f"  arm:\n    levels:\n      - value:{value}\n"))


def test_yaml_1_1_boolean_words_are_not_flags():
    with pytest.raises(ManualError, match="evidence_required must be true or false"):
        parse_coding_manual(_yaml_manual("  arm: {evidence_required: no}\n"))


def test_manual_loader_does_not_change_pyyaml_globals():
    parse_coding_manual(_yaml_manual("  arm: {}\n"))
    assert yaml.safe_load("[yes, 01, 1.50]") == [True, 1, 1.5]


def test_tricky_level_values_round_trip_through_saved_yaml():
    values = ["yes", "No", "on", "OFF", "01", "1.50", "null", "~", "true", "1e3", "=", "2024-01-01"]
    payload = {
        "effect_definition": "x",
        "effects": [{"name": "arm", "type": "string", "levels": [{"value": v} for v in values]}],
    }
    manual = manual_from_editor_payload(payload)
    reparsed = parse_coding_manual(manual_to_yaml_text(manual))
    assert [level.value for level in reparsed.effects["arm"].levels] == values
