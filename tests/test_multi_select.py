"""Covers categorical fields with `multiple: true`: parsing and saving the setting in the
manual (YAML and editor payload), the response schema in both dialects, validation,
the coded_data.csv cell order, and the prompt wording and version.
"""

import json

import pytest
import yaml

from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import (
    NOTES_FIELD_NAME,
    ManualError,
    manual_from_editor_payload,
    manual_to_editor_payload,
    manual_to_yaml_text,
    parse_coding_manual,
)
from meta_coder.mechanism import build_response_schema, validate_response
from meta_coder.prompts import PROMPT_VERSION, build_extraction_prompt, render_codebook
from meta_coder.results import collate_results


MANUAL_YAML = """
name: t
effect_definition: x
effects:
  Outcomes:
    type: string
    multiple: true
    levels:
      - {value: Accuracy, description: Accuracy was measured.}
      - {value: Speed, description: Response time was measured.}
      - {value: Confidence, description: Confidence was rated.}
  Condition:
    type: string
    levels:
      - {value: A, description: First condition.}
      - {value: B, description: Second condition.}
  estimate: {type: number}
"""


def _manual():
    return parse_coding_manual(MANUAL_YAML)


def _row(row_id="r1", pdf="paper.pdf", locator="Exp 1"):
    return CodingSheetRow(row_id, pdf, locator, "Smith", "2020")


def _field(value, missing=None, evidence="p1"):
    return {"value": value, "missing": missing, "evidence": evidence}


def _response(outcomes, *, condition=None, estimate=None):
    return {
        "effects": [
            {
                "row_id": "r1",
                "Outcomes": outcomes,
                "Condition": condition or _field("A"),
                "estimate": estimate or _field(1.5),
                "notes": {"value": None},
            }
        ]
    }


def _validate(outcomes, **kwargs):
    return validate_response(_response(outcomes, **kwargs), {"r1"}, _manual().effects)


# Parsing and saving the setting


def test_yaml_multiple_flag_is_parsed_and_other_fields_default_to_single_select():
    manual = _manual()
    assert manual.effects["Outcomes"].multiple is True
    assert manual.effects["Condition"].multiple is False
    assert manual.effects["estimate"].multiple is False
    assert manual.effects[NOTES_FIELD_NAME].multiple is False


@pytest.mark.parametrize('flag', ['"yes"', '1', '[]'])
def test_multiple_must_be_a_real_boolean(flag):
    text = MANUAL_YAML.replace("multiple: true", f"multiple: {flag}")
    with pytest.raises(ManualError, match=r"effects\.Outcomes\.multiple must be true or false\."):
        parse_coding_manual(text)


def test_explicit_multiple_false_is_accepted():
    text = MANUAL_YAML.replace("multiple: true", "multiple: false")
    assert parse_coding_manual(text).effects["Outcomes"].multiple is False


def test_multiple_without_levels_is_rejected():
    text = """
effect_definition: x
effects:
  Outcomes:
    type: string
    multiple: true
"""
    with pytest.raises(ManualError, match="multiple: true` but no `levels`"):
        parse_coding_manual(text)


def test_multiple_number_field_is_rejected_for_lacking_levels():
    text = """
effect_definition: x
effects:
  estimate:
    type: number
    multiple: true
"""
    with pytest.raises(ManualError, match="multiple: true` but no `levels`"):
        parse_coding_manual(text)


def test_multiple_level_containing_a_semicolon_is_rejected():
    text = MANUAL_YAML.replace("value: Speed", "value: Speed; fast")
    with pytest.raises(ManualError, match="cannot contain a semicolon"):
        parse_coding_manual(text)


def test_semicolon_in_a_level_of_a_single_select_field_is_allowed():
    text = MANUAL_YAML.replace("value: Speed", "value: Speed; fast").replace(
        "    multiple: true\n", ""
    )
    manual = parse_coding_manual(text)
    assert manual.effects["Outcomes"].multiple is False
    assert "Speed; fast" in [level.value for level in manual.effects["Outcomes"].levels]


# Round trips


def test_yaml_round_trip_keeps_multiple_and_writes_the_flag_only_when_set():
    first = _manual()
    text = manual_to_yaml_text(first)
    saved = yaml.safe_load(text)
    assert saved["effects"]["Outcomes"]["multiple"] is True
    assert "multiple" not in saved["effects"]["Condition"]
    assert "multiple" not in saved["effects"]["estimate"]

    second = parse_coding_manual(text)
    assert second.effects["Outcomes"].multiple is True
    assert second.effects["Condition"].multiple is False
    assert [level.value for level in second.effects["Outcomes"].levels] == [
        "Accuracy", "Speed", "Confidence",
    ]
    assert manual_to_yaml_text(second) == text


def test_editor_payload_round_trip_keeps_multiple():
    manual = _manual()
    payload = manual_to_editor_payload(manual)
    fields = {field["name"]: field for field in payload["effects"]}
    assert fields["Outcomes"]["multiple"] is True
    assert fields["Condition"]["multiple"] is False

    rebuilt = manual_from_editor_payload(json.loads(json.dumps(payload)))
    assert rebuilt.effects["Outcomes"].multiple is True
    assert rebuilt.effects["Condition"].multiple is False
    assert rebuilt.effects["estimate"].multiple is False


def test_editor_payload_without_multiple_key_is_single_select():
    payload = manual_to_editor_payload(_manual())
    for field in payload["effects"]:
        if field["name"] == "Outcomes":
            del field["multiple"]
    rebuilt = manual_from_editor_payload(payload)
    assert rebuilt.effects["Outcomes"].multiple is False


def test_editor_payload_rejects_multiple_on_a_field_without_levels():
    payload = {
        "effect_definition": "x",
        "effects": [{"name": "estimate", "type": "number", "multiple": True}],
    }
    with pytest.raises(ManualError, match="multiple: true` but no `levels`"):
        manual_from_editor_payload(payload)


# Response schema


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_multiple_field_schema_is_an_array_of_enum_strings(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    item = schema["properties"]["effects"]["items"]
    outcomes = item["properties"]["Outcomes"]["properties"]["value"]
    levels = ["Accuracy", "Speed", "Confidence"]
    if dialect == "gemini":
        assert outcomes["type"] == "ARRAY"
        assert outcomes["nullable"] is True
        assert outcomes["items"] == {"type": "STRING", "enum": levels}
        assert "enum" not in outcomes
    else:
        assert outcomes["type"] == ["array", "null"]
        assert outcomes["items"] == {"type": "string", "enum": levels}
        assert None not in outcomes["items"]["enum"]
        assert "nullable" not in outcomes


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_multiple_field_still_requires_missing_and_evidence(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    coded = schema["properties"]["effects"]["items"]["properties"]["Outcomes"]
    assert "missing" in coded["properties"]
    assert {"value", "missing", "evidence"} <= set(coded["required"])


def test_single_select_schema_is_unchanged_by_the_setting():
    schema = build_response_schema(_manual(), dialect="json_schema")
    value = schema["properties"]["effects"]["items"]["properties"]["Condition"]["properties"]["value"]
    assert value["type"] == ["string", "null"]
    assert value["enum"] == ["A", "B", None]
    assert "items" not in value


# Validation


@pytest.mark.parametrize('outcomes', [
    ["Speed"],
    ["Speed", "Accuracy"],
    ["Confidence", "Accuracy", "Speed"],
])
def test_multiple_field_accepts_a_non_empty_list_of_distinct_levels(outcomes):
    assert _validate(_field(outcomes)).ok


def test_multiple_field_accepts_null_with_each_missing_code():
    assert _validate(_field(None, missing="not_applicable")).ok
    assert _validate(_field(None, missing="unclear")).ok


@pytest.mark.parametrize('value', [
    "Speed",       # a plain string is a single select
    [],            # nothing applies is null with a code, never []
    ["Colour"],    # not a level in the manual
    ["Speed", "Colour"],
    [1],           # non-string item
    [None],
    ["Speed", "Speed"],  # repeated level
])
def test_multiple_field_rejects_invalid_lists(value):
    result = _validate(_field(value))
    assert not result.ok
    assert "invalid field value" in result.error
    assert "`r1`: Outcomes" in result.error


def test_multiple_field_rejects_a_missing_code_alongside_a_list():
    result = _validate(_field(["Speed"], missing="unclear"))
    assert not result.ok
    assert "`r1`: Outcomes" in result.error


def test_multiple_field_null_without_missing_code_is_rejected():
    result = _validate({"value": None, "evidence": "p1"})
    assert not result.ok
    assert "`r1`: Outcomes" in result.error


def test_single_select_field_still_rejects_a_list():
    result = _validate(_field(["Speed"]), condition=_field(["A"]))
    assert not result.ok
    assert "`r1`: Condition" in result.error


def test_single_select_field_still_accepts_one_level():
    assert _validate(_field(["Speed"]), condition=_field("B")).ok


# Collation


def _collate(outcomes):
    manual = _manual()
    sheet = CodingSheet(rows=[_row()], issues=[])
    result = ExtractionResult(
        source_pdf="paper.pdf",
        status="ok",
        coded_by_row_id={"r1": {
            "Outcomes": outcomes,
            "Condition": _field("A"),
            "estimate": _field(1.5),
        }},
    )
    coded_rows, evidence_rows = collate_results(
        manual=manual, coding_sheet=sheet, results_by_pdf={"paper.pdf": result},
    )
    return coded_rows[0], evidence_rows[0]


def test_collate_joins_selected_levels_in_manual_order_whatever_the_model_order():
    coded, _ = _collate(_field(["Confidence", "Accuracy"]))
    assert coded["Outcomes"] == "Accuracy; Confidence"


def test_collate_puts_unknown_items_last_in_their_given_order():
    coded, _ = _collate(_field(["Colour", "Speed", "Accuracy"]))
    assert coded["Outcomes"] == "Accuracy; Speed; Colour"


def test_collate_single_selected_level_has_no_separator():
    coded, _ = _collate(_field(["Speed"]))
    assert coded["Outcomes"] == "Speed"


def test_collate_null_multiple_field_shows_the_missing_label():
    coded, _ = _collate(_field(None, missing="not_applicable"))
    assert coded["Outcomes"] == "Not Applicable"


def test_collate_keeps_the_list_in_evidence_output_and_single_select_unchanged():
    _, evidence = _collate(_field(["Speed"], evidence="p3"))
    assert evidence["Outcomes"] == "p3"
    coded, _ = _collate(_field(["Speed"]))
    assert coded["Condition"] == "A"


# Prompt and version


def test_codebook_labels_only_multiple_fields_as_select_all_that_apply():
    codebook = render_codebook(_manual())
    assert "- `Outcomes` (categorical, select all that apply)" in codebook
    assert "- `Condition` (categorical)" in codebook
    assert "select all that apply" not in codebook.split("- `Condition`")[1].split("- `estimate`")[0]


def test_extraction_prompt_explains_selecting_several_levels():
    # The rule wraps across lines in the prompt, so compare on single spaces.
    prompt = " ".join(build_extraction_prompt(_manual(), [_row()]).split())
    assert "Only for a field marked \"select all that apply\", set \"value\" to a list of every level that applies" in prompt
    assert "Never return an empty list" in prompt


def test_prompt_version_is_at_least_four():
    assert int(PROMPT_VERSION) >= 4
