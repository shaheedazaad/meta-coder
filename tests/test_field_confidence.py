"""Covers the optional per-field confidence rating (`confidence: true` in a manual):
manual parsing and editor round-trip, the response schema in both dialects,
validation, each provider adapter's enforcement, the prompt rule, cells_to_check,
the per-PDF audit YAML, collate_field_key, the runner's confidence.csv, and the
web download route and page link.
"""

import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from meta_coder import gemini, openai_compatible, openrouter, web
from meta_coder import runner as runner_module
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import (
    ManualError,
    manual_from_editor_payload,
    manual_to_editor_payload,
    manual_to_yaml_text,
    parse_coding_manual,
)
from meta_coder.mechanism import CONFIDENCE_LEVELS, build_response_schema, validate_response
from meta_coder.projects import Project, create_project, write_manual
from meta_coder.prompts import BASELINE_RULES, CONFIDENCE_RULE, PROMPT_VERSION, build_extraction_prompt
from meta_coder.results import cells_to_check, collate_field_key, render_pdf_audit_yaml
from meta_coder.runner import Runner


MANUAL_YAML = """
name: t
effect_definition: x
effects:
  Condition:
    type: string
    levels:
      - {value: A, description: First condition.}
      - {value: B, description: Second condition.}
  estimate: {type: number}
"""


def _manual(confidence=None):
    if confidence is None:
        text = MANUAL_YAML
    else:
        value = {True: "true", False: "false"}.get(confidence, confidence)
        text = f"confidence: {value}\n" + MANUAL_YAML
    return parse_coding_manual(text)


def _row(row_id="r1", pdf="paper.pdf", locator="Exp 1"):
    return CodingSheetRow(row_id, pdf, locator, "Smith", "2020")


# Manual parsing and editor round-trip


def test_confidence_defaults_to_off_and_is_not_written_to_yaml():
    manual = _manual()
    assert manual.confidence is False
    assert "confidence" not in yaml.safe_load(manual_to_yaml_text(manual))


def test_confidence_true_parses_and_survives_yaml_round_trip():
    manual = _manual(True)
    assert manual.confidence is True
    saved = yaml.safe_load(manual_to_yaml_text(manual))
    assert saved["confidence"] is True
    assert parse_coding_manual(manual_to_yaml_text(manual)).confidence is True


@pytest.mark.parametrize('value', ['null', 'false'])
def test_confidence_null_or_false_means_off(value):
    assert _manual(value).confidence is False


@pytest.mark.parametrize('value', ['yes', '1', '"true"', '[]', '{}'])
def test_confidence_must_be_a_boolean(value):
    with pytest.raises(ManualError, match="`confidence` must be true or false."):
        _manual(value)


def test_editor_payload_carries_confidence_both_ways():
    manual = _manual(True)
    payload = json.loads(json.dumps(manual_to_editor_payload(manual)))
    assert payload["confidence"] is True
    assert manual_from_editor_payload(payload).confidence is True
    payload["confidence"] = False
    assert manual_from_editor_payload(payload).confidence is False


def test_editor_payload_without_confidence_key_or_null_is_off():
    payload = manual_to_editor_payload(_manual())
    del payload["confidence"]
    assert manual_from_editor_payload(payload).confidence is False
    payload["confidence"] = None
    assert manual_from_editor_payload(payload).confidence is False


def test_editor_payload_rejects_a_non_boolean_confidence():
    payload = manual_to_editor_payload(_manual())
    payload["confidence"] = "yes"
    with pytest.raises(ManualError, match="must be true or false"):
        manual_from_editor_payload(payload)


def test_editor_confidence_true_is_written_to_yaml():
    payload = manual_to_editor_payload(_manual())
    payload["confidence"] = True
    assert yaml.safe_load(manual_to_yaml_text(manual_from_editor_payload(payload)))["confidence"] is True


# Response schema


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_schema_requires_confidence_on_every_field_except_notes(dialect):
    schema = build_response_schema(_manual(True), dialect=dialect)
    item = schema["properties"]["effects"]["items"]
    for name in ("Condition", "estimate"):
        field = item["properties"][name]
        assert field["properties"]["confidence"]["enum"] == list(CONFIDENCE_LEVELS)
        assert "confidence" in field["required"]
    notes = item["properties"]["notes"]
    assert "confidence" not in notes["properties"]
    assert "confidence" not in notes["required"]


def test_gemini_confidence_schema_is_not_nullable():
    schema = build_response_schema(_manual(True), dialect="gemini")
    confidence = schema["properties"]["effects"]["items"]["properties"]["estimate"]["properties"]["confidence"]
    assert confidence["type"] == "STRING"
    assert "nullable" not in confidence


def test_json_schema_confidence_schema_is_a_plain_string_enum():
    schema = build_response_schema(_manual(True), dialect="json_schema")
    confidence = schema["properties"]["effects"]["items"]["properties"]["estimate"]["properties"]["confidence"]
    assert confidence["type"] == "string"
    assert confidence["enum"] == ["high", "medium", "low"]


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_schema_has_no_confidence_when_off(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    item = schema["properties"]["effects"]["items"]
    for name in ("Condition", "estimate", "notes"):
        assert "confidence" not in item["properties"][name]["properties"]
        assert "confidence" not in item["properties"][name]["required"]


# Validation


def _confidence_response(estimate=None, condition=None, notes=None):
    return {
        "effects": [
            {
                "row_id": "r1",
                "Condition": condition or {"value": "A", "missing": None, "evidence": "p1", "confidence": "high"},
                "estimate": estimate or {"value": 1.5, "missing": None, "evidence": "p1", "confidence": "high"},
                "notes": notes or {"value": None},
            }
        ]
    }


def _validate_conf(estimate=None, condition=None, *, confidence=True, manual=None):
    manual = manual or _manual()
    return validate_response(
        _confidence_response(estimate, condition), {"r1"}, manual.effects, confidence=confidence
    )


@pytest.mark.parametrize('level', CONFIDENCE_LEVELS)
def test_each_confidence_level_is_accepted(level):
    estimate = {"value": 1.5, "missing": None, "evidence": "p1", "confidence": level}
    assert _validate_conf(estimate).ok


def test_null_value_with_missing_code_and_confidence_is_accepted():
    estimate = {"value": None, "missing": "unclear", "evidence": "p1", "confidence": "low"}
    assert _validate_conf(estimate).ok


@pytest.mark.parametrize('field', [
    {"value": 1.5, "missing": None, "evidence": "p1"},
    {"value": 1.5, "missing": None, "evidence": "p1", "confidence": None},
    {"value": 1.5, "missing": None, "evidence": "p1", "confidence": "certain"},
    {"value": 1.5, "missing": None, "evidence": "p1", "confidence": "High"},
    {"value": 1.5, "missing": None, "evidence": "p1", "confidence": 3},
])
def test_missing_unknown_or_none_confidence_is_rejected(field):
    result = _validate_conf(field)
    assert not result.ok
    assert "invalid field value" in result.error
    assert "`r1`: estimate" in result.error


def test_confidence_is_checked_on_condition_field_too():
    result = _validate_conf(condition={"value": "A", "missing": None, "evidence": "p1"})
    assert not result.ok
    assert "`r1`: Condition" in result.error


def test_notes_field_is_exempt_from_confidence():
    # _confidence_response gives notes no confidence key at all.
    assert _validate_conf().ok


def test_confidence_is_not_checked_when_off():
    estimate = {"value": 1.5, "missing": None, "evidence": "p1"}
    assert _validate_conf(estimate, confidence=False).ok
    assert _validate_conf({"value": 1.5, "missing": None, "evidence": "p1", "confidence": "certain"},
                          confidence=False).ok


def test_confidence_is_required_for_bare_field_name_sets_too():
    parsed = {"effects": [{"row_id": "r1", "estimate": {"value": 1.5}}]}
    result = validate_response(parsed, {"r1"}, {"estimate"}, confidence=True)
    assert not result.ok and "`r1`: estimate" in result.error


# Provider adapters enforce the manual's setting


def _wire(adapter, text):
    if adapter is gemini:
        return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


@pytest.mark.parametrize('adapter, kwargs', [
    (gemini, {}),
    (openrouter, {}),
    (openai_compatible, {"model": "m", "base_url": "http://localhost/v1"}),
])
@pytest.mark.parametrize('manual_flag, with_confidence, expected', [
    (True, False, 'needs_review'),
    (True, True, 'ok'),
    (False, False, 'ok'),
])
def test_adapter_enforces_confidence_only_when_manual_asks(
    adapter, kwargs, manual_flag, with_confidence, expected, tmp_path, monkeypatch
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    field = {"value": 1.5, "missing": None, "evidence": "p. 1"}
    if with_confidence:
        field["confidence"] = "medium"
    condition = {"value": "A", "missing": None, "evidence": "p. 1"}
    if with_confidence:
        condition["confidence"] = "high"
    reply = {"effects": [{"row_id": "r1", "Condition": condition, "estimate": field,
                          "notes": {"value": "", "evidence": ""}}]}
    monkeypatch.setattr(adapter, "cancellable_urlopen", lambda request, **_: _wire(adapter, json.dumps(reply)))
    monkeypatch.setattr(openai_compatible, "pdf_text", lambda path, event: "[Page 1]\nArticle body.")
    manual = _manual(manual_flag or None)
    result = adapter.extract_pdf_effects(
        pdf_path=pdf, manual=manual, rows=[CodingSheetRow(row_id="r1", source_pdf="paper.pdf", locator="")],
        api_key="k", **kwargs,
    )
    assert result.status == expected
    if expected == 'needs_review':
        assert "invalid field value" in result.error and "`r1`: Condition, estimate" in result.error


# Prompt


def test_prompt_adds_the_confidence_rule_only_when_asked():
    rows = [_row()]
    on = build_extraction_prompt(_manual(True), rows)
    off = build_extraction_prompt(_manual(), rows)
    assert f"{BASELINE_RULES}\n{CONFIDENCE_RULE}" in on
    assert "7. For every field except `notes`" in on
    assert CONFIDENCE_RULE not in off
    assert "7. For every field" not in off


def test_prompt_version_has_moved_to_at_least_five():
    assert int(PROMPT_VERSION) >= 5


# cells_to_check


def test_cells_to_check_counts_low_confidence_cells():
    coded = {
        "r1": {
            "a": {"value": 1, "confidence": "low"},                         # counted
            "b": {"value": 2, "confidence": "medium"},                      # not counted
            "c": {"value": 3, "confidence": "high"},                        # not counted
            "d": {"value": 4},                                              # no rating
        },
    }
    assert cells_to_check(coded) == 1


def test_cells_to_check_counts_an_unclear_and_low_cell_once():
    coded = {"r1": {"a": {"value": None, "missing": "unclear", "confidence": "low"}}}
    assert cells_to_check(coded) == 1


def test_cells_to_check_adds_unclear_and_low_cells_separately():
    coded = {"r1": {
        "a": {"value": None, "missing": "unclear", "confidence": "high"},
        "b": {"value": 2, "confidence": "low"},
    }}
    assert cells_to_check(coded) == 2


# Per-PDF audit YAML


def test_audit_yaml_shows_confidence_only_for_cells_that_have_it():
    manual = _manual(True)
    result = ExtractionResult(source_pdf="paper.pdf", status="ok", coded_by_row_id={"r1": {
        "Condition": {"value": "A", "missing": None, "evidence": "p1", "confidence": "high"},
        "estimate": {"value": 1.5, "evidence": "p2", "confidence": "low"},
        "notes": {"value": None},
    }})
    text = render_pdf_audit_yaml(manual=manual, source_pdf="paper.pdf", rows=[_row()], result=result)
    fields = yaml.safe_load(text)["effects"]["r1"]["fields"]
    assert fields["Condition"]["confidence"] == "high"
    assert fields["estimate"]["confidence"] == "low"
    assert "confidence" not in fields["notes"]
    assert yaml.safe_load(text)["cells_to_check"] == 1


def test_audit_yaml_without_confidence_keys_has_no_confidence_entries():
    result = ExtractionResult(source_pdf="paper.pdf", status="ok", coded_by_row_id={"r1": {
        "Condition": {"value": "A", "missing": None, "evidence": "p1"},
        "estimate": {"value": 1.5, "evidence": "p2"},
        "notes": {"value": None},
    }})
    text = render_pdf_audit_yaml(manual=_manual(), source_pdf="paper.pdf", rows=[_row()], result=result)
    assert "confidence" not in text


# collate_field_key


def _sheet():
    return CodingSheet(rows=[_row("r1", "a.pdf"), _row("r2", "a.pdf"), _row("r3", "b.pdf")], issues=[])


def test_collate_field_key_fills_cells_with_the_key_and_blanks_the_rest():
    manual = _manual(True)
    results = {"a.pdf": ExtractionResult(source_pdf="a.pdf", status="needs_review", coded_by_row_id={
        "r1": {
            "Condition": {"value": "A", "missing": None, "evidence": "p1", "confidence": "high"},
            "estimate": {"value": None, "missing": "unclear", "evidence": "p2"},  # no key -> blank
            "notes": {"value": "", "evidence": ""},                              # no key -> blank
        },
        # r2 missing from the model's output entirely: status from result, blank cells
    })}
    rows = collate_field_key("confidence", manual=manual, coding_sheet=_sheet(), results_by_pdf=results)
    assert [row["row_id"] for row in rows] == ["r1", "r2", "r3"]
    assert rows[0] == {
        "row_id": "r1", "source_pdf": "a.pdf", "locator": "Exp 1", "authors": "Smith", "year": "2020",
        "status": "needs_review", "Condition": "high", "estimate": "", "notes": "",
    }
    assert rows[1]["status"] == "needs_review"
    assert rows[1]["Condition"] == "" and rows[1]["estimate"] == ""


def test_collate_field_key_blanks_pdfs_without_a_result_and_malformed_cells():
    manual = _manual(True)
    results = {"a.pdf": ExtractionResult(source_pdf="a.pdf", status="ok", coded_by_row_id={
        "r1": {
            "Condition": "malformed cell",
            "estimate": {"value": 1.5, "confidence": None},
        },
    })}
    rows = collate_field_key("confidence", manual=manual, coding_sheet=_sheet(), results_by_pdf=results)
    assert rows[0]["Condition"] == "" and rows[0]["estimate"] == ""
    assert rows[2]["status"] == "not_run"
    assert rows[2]["Condition"] == "" and rows[2]["estimate"] == ""


def test_collate_field_key_stringifies_any_key_it_is_asked_for():
    manual = _manual()
    results = {"a.pdf": ExtractionResult(source_pdf="a.pdf", status="ok", coded_by_row_id={
        "r1": {"Condition": {"value": "A", "evidence": "p1"}, "estimate": {"value": 1, "evidence": 7}},
    })}
    rows = collate_field_key("evidence", manual=manual, coding_sheet=_sheet(), results_by_pdf=results)
    assert rows[0]["Condition"] == "p1" and rows[0]["estimate"] == "7"


# Runner: confidence.csv


def _make_project(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "output" / "raw").mkdir(parents=True)
    (tmp_path / "output" / "coded").mkdir(parents=True)
    return Project(project_id="0" * 16, name="t", path=tmp_path, created_at="")


def _run_with_fake_extraction(tmp_path, monkeypatch, manual):
    project = _make_project(tmp_path)
    sheet = CodingSheet(rows=[_row()], issues=[])

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={"r1": {
                "Condition": {"value": "A", "missing": None, "evidence": "p1", "confidence": "high"},
                "estimate": {"value": None, "missing": "unclear", "evidence": "p2", "confidence": "low"},
                "notes": {"value": None},
            }},
        )

    (tmp_path / "sources" / "paper.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)
    runner = Runner()
    state = runner.start(
        project=project, manual=manual, coding_sheet=sheet, api_key="fake",
        parallel_requests=1, request_delay_sec=0,
    )
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    return project, state


def test_runner_writes_confidence_csv_and_audits_it_when_asked(tmp_path, monkeypatch):
    project, state = _run_with_fake_extraction(tmp_path, monkeypatch, _manual(True))
    assert state.status == "complete"
    lines = (project.output_dir / "confidence.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "row_id,source_pdf,locator,authors,year,status,Condition,estimate,notes"
    assert lines[1] == "r1,paper.pdf,Exp 1,Smith,2020,ok,high,low,"
    operations = [json.loads(path.read_text()) for path in Path(project.path, "audit/operations").glob("*/operation.json")]
    extraction_run = next(op for op in operations if op["operation"] == "extraction_run")
    assert "confidence.csv" in extraction_run["inputs"]


def test_runner_writes_no_confidence_csv_when_off(tmp_path, monkeypatch):
    project, state = _run_with_fake_extraction(tmp_path, monkeypatch, _manual())
    assert state.status == "complete"
    assert not (project.output_dir / "confidence.csv").exists()
    assert (project.output_dir / "coded_data.csv").is_file()
    operations = [json.loads(path.read_text()) for path in Path(project.path, "audit/operations").glob("*/operation.json")]
    extraction_run = next(op for op in operations if op["operation"] == "extraction_run")
    assert "confidence.csv" not in extraction_run["inputs"]


# Web surfaces


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Field confidence', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def test_download_confidence_serves_the_file_or_404s(site):
    client, project, _ = site
    url = f'/test/projects/{project.project_id}/download/confidence'
    missing = client.get(url)
    assert missing.status_code == 404
    assert missing.json()['detail'] == 'No confidence ratings yet.'

    project.output_dir.mkdir(parents=True, exist_ok=True)
    (project.output_dir / 'confidence.csv').write_text('row_id,Condition\nr1,high\n', encoding='utf-8')
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    assert response.text == 'row_id,Condition\nr1,high\n'
    assert 'confidence.csv' in response.headers['content-disposition']


def test_project_page_offers_confidence_download_only_when_the_file_exists(site):
    client, project, _ = site
    write_manual(project, _manual(True))
    project.output_dir.mkdir(parents=True, exist_ok=True)
    (project.output_dir / 'coded_data.csv').write_text('row_id\n', encoding='utf-8')
    (project.output_dir / 'evidence.csv').write_text('row_id\n', encoding='utf-8')
    url = f'/test/projects/{project.project_id}'

    page = client.get(url)
    assert page.status_code == 200
    assert 'Download coded_data.csv' in page.text
    assert 'Download confidence.csv' not in page.text
    assert 'id="manual-confidence"' in page.text

    (project.output_dir / 'confidence.csv').write_text('row_id\n', encoding='utf-8')
    page = client.get(url)
    assert 'Download confidence.csv' in page.text
    assert f'/test/projects/{project.project_id}/download/confidence' in page.text

