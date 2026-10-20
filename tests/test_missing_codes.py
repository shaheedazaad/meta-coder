"""Covers the `missing` code that explains a null value: the response schema in both
dialects, validation, the collated coded_data.csv label, cells_to_check, the per-PDF
audit YAML, the prompt rules, and the run/results surfaces that show the count.
"""

import time
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from meta_coder import web
from meta_coder import runner as runner_module
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import parse_coding_manual
from meta_coder.mechanism import MISSING_CODES, build_response_schema, validate_response
from meta_coder.projects import Project, create_project, write_manual
from meta_coder.prompts import PROMPT_VERSION, build_extraction_prompt
from meta_coder.results import MISSING_LABELS, cells_to_check, collate_results, render_pdf_audit_yaml
from meta_coder.runner import PdfProgress, RunState, Runner, write_raw_result


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


def _manual():
    return parse_coding_manual(MANUAL_YAML)


def _row(row_id="r1", pdf="paper.pdf", locator="Exp 1"):
    return CodingSheetRow(row_id, pdf, locator, "Smith", "2020")


# Schema


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_every_coded_field_requires_missing_except_notes(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    item = schema["properties"]["effects"]["items"]
    for name in ("Condition", "estimate"):
        assert "missing" in item["properties"][name]["properties"]
        assert "missing" in item["properties"][name]["required"]
    notes = item["properties"]["notes"]
    assert "missing" not in notes["properties"]
    assert "missing" not in notes["required"]


def test_gemini_missing_schema_is_nullable_string_enum():
    schema = build_response_schema(_manual(), dialect="gemini")
    missing = schema["properties"]["effects"]["items"]["properties"]["estimate"]["properties"]["missing"]
    assert missing["type"] == "STRING"
    assert missing["nullable"] is True
    assert missing["enum"] == list(MISSING_CODES)


def test_json_schema_missing_schema_allows_null_and_closes_object():
    schema = build_response_schema(_manual(), dialect="json_schema")
    coded = schema["properties"]["effects"]["items"]["properties"]["estimate"]
    assert coded["additionalProperties"] is False
    assert coded["properties"]["missing"]["type"] == ["string", "null"]
    assert coded["properties"]["missing"]["enum"] == [*MISSING_CODES, None]


# Validation


def _response(estimate, condition=None):
    return {
        "effects": [
            {
                "row_id": "r1",
                "Condition": condition or {"value": "A", "missing": None, "evidence": "p1"},
                "estimate": estimate,
                "notes": {"value": None},
            }
        ]
    }


def _validate(estimate, condition=None):
    return validate_response(_response(estimate, condition), {"r1"}, _manual().effects)


@pytest.mark.parametrize('code', MISSING_CODES)
def test_null_value_accepts_each_missing_code(code):
    assert _validate({"value": None, "missing": code, "evidence": "p1"}).ok


@pytest.mark.parametrize('field', [
    {"value": None, "evidence": "p1"},
    {"value": None, "missing": None, "evidence": "p1"},
    {"value": None, "missing": "maybe", "evidence": "p1"},
    {"value": None, "missing": 3, "evidence": "p1"},
])
def test_null_value_rejects_missing_absent_unknown_or_non_string_code(field):
    result = _validate(field)
    assert not result.ok
    assert "invalid field value" in result.error
    assert "`r1`: estimate" in result.error


@pytest.mark.parametrize('field', [
    {"value": 1.5, "missing": "unclear", "evidence": "p1"},
    {"value": 1.5, "missing": "", "evidence": "p1"},
])
def test_real_value_rejects_a_missing_code(field):
    result = _validate(field)
    assert not result.ok
    assert "`r1`: estimate" in result.error


@pytest.mark.parametrize('field', [
    {"value": 1.5, "missing": None, "evidence": "p1"},
    {"value": 1.5, "evidence": "p1"},
])
def test_real_value_accepts_missing_none_or_absent(field):
    assert _validate(field).ok


def test_notes_field_is_exempt_from_missing_codes():
    result = validate_response(
        {"effects": [{"row_id": "r1", "Condition": {"value": "A", "missing": None, "evidence": "p1"},
                      "estimate": {"value": 1.0, "missing": None, "evidence": "p1"},
                      "notes": {"value": None}}]},
        {"r1"},
        _manual().effects,
    )
    assert result.ok


def test_missing_codes_are_only_checked_when_fields_are_described_by_spec():
    # A bare set of field names carries no FieldSpec, so no missing rule applies.
    parsed = {"effects": [{"row_id": "r1", "estimate": {"value": None}}]}
    assert validate_response(parsed, {"r1"}, {"estimate"}).ok


# Collation labels


@pytest.mark.parametrize(('code', 'label'), [
    ('not_reported', 'Not Reported'),
    ('not_applicable', 'Not Applicable'),
    ('unclear', 'Unclear'),
])
def test_collate_labels_each_missing_code(code, label):
    assert MISSING_LABELS[code] == label
    coded_rows = _collate({"value": None, "missing": code, "evidence": "p1"})
    assert coded_rows["estimate"] == label


@pytest.mark.parametrize('field', [
    {"value": None, "evidence": "Not reported."},
    {"value": None, "missing": "maybe", "evidence": "p1"},
    {"value": None, "missing": 7, "evidence": "p1"},
    {"value": None, "missing": None, "evidence": "p1"},
])
def test_collate_falls_back_to_not_reported(field):
    assert _collate(field)["estimate"] == "Not Reported"


def test_collate_shows_real_value_even_with_missing_key_none():
    assert _collate({"value": 2.5, "missing": None, "evidence": "p1"})["estimate"] == "2.5"


def _collate(estimate):
    manual = _manual()
    sheet = CodingSheet(rows=[_row()], issues=[])
    result = ExtractionResult(
        source_pdf="paper.pdf",
        status="ok",
        coded_by_row_id={"r1": {
            "Condition": {"value": "A", "missing": None, "evidence": "p1"},
            "estimate": estimate,
        }},
    )
    coded_rows, _ = collate_results(manual=manual, coding_sheet=sheet, results_by_pdf={"paper.pdf": result})
    return coded_rows[0]


# cells_to_check


@pytest.mark.parametrize('value', [None, [], "text", 3])
def test_cells_to_check_is_zero_for_non_dict_input(value):
    assert cells_to_check(value) == 0


def test_cells_to_check_counts_only_null_unclear_cells():
    coded = {
        "r1": {
            "a": {"value": None, "missing": "unclear"},          # counted
            "b": {"value": None, "missing": "not_reported"},     # other code
            "c": {"value": 1, "missing": "unclear"},             # value given
            "d": {"value": None},                                # legacy, no code
            "e": {"value": None, "missing": "unclear"},          # counted
        },
        "r2": {"a": {"value": None, "missing": "unclear"}},      # counted
    }
    assert cells_to_check(coded) == 3


def test_cells_to_check_skips_non_dict_rows_and_cells():
    coded = {
        "r1": "malformed row",
        "r2": {"a": "malformed cell", "b": None, "c": {"value": None, "missing": "unclear"}},
    }
    assert cells_to_check(coded) == 1


def test_cells_to_check_is_zero_when_nothing_is_unclear():
    assert cells_to_check({}) == 0
    assert cells_to_check({"r1": {"a": {"value": None, "missing": "not_applicable"}}}) == 0


# Per-PDF audit YAML


def _audit(coded_by_row_id):
    result = ExtractionResult(source_pdf="paper.pdf", status="ok", coded_by_row_id=coded_by_row_id)
    text = render_pdf_audit_yaml(manual=_manual(), source_pdf="paper.pdf", rows=[_row()], result=result)
    return yaml.safe_load(text)


def test_audit_yaml_includes_missing_only_for_null_values_and_counts_cells_to_check():
    parsed = _audit({"r1": {
        "Condition": {"value": "A", "missing": None, "evidence": "p1"},
        "estimate": {"value": None, "missing": "unclear", "evidence": "p3"},
        "notes": {"value": None},
    }})
    fields = parsed["effects"]["r1"]["fields"]
    assert fields["estimate"] == {"value": None, "missing": "unclear", "evidence": "p3"}
    assert "missing" not in fields["Condition"]
    assert "missing" not in fields["notes"]
    assert parsed["cells_to_check"] == 1


def test_audit_yaml_omits_cells_to_check_when_none_are_unclear():
    parsed = _audit({"r1": {
        "Condition": {"value": None, "missing": "not_applicable", "evidence": "p1"},
        "estimate": {"value": 1.5, "evidence": "p2"},
        "notes": {"value": None},
    }})
    assert "cells_to_check" not in parsed
    assert parsed["effects"]["r1"]["fields"]["Condition"]["missing"] == "not_applicable"
    assert "missing" not in parsed["effects"]["r1"]["fields"]["estimate"]


# Prompt and version


def test_prompt_defines_the_three_missing_codes():
    prompt = build_extraction_prompt(_manual(), [_row()])
    for code in ("not_reported", "not_applicable", "unclear"):
        assert code in prompt
    assert '"missing"' in prompt
    assert int(PROMPT_VERSION) >= 3


# Runner surfaces


def _make_project(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "output" / "raw").mkdir(parents=True)
    (tmp_path / "output" / "coded").mkdir(parents=True)
    return Project(project_id="0" * 16, name="t", path=tmp_path, created_at="")


def test_runner_records_cells_to_check_on_progress_and_snapshot(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    sheet = CodingSheet(rows=[_row()], issues=[])

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={"r1": {
                "Condition": {"value": "A", "missing": None, "evidence": "p1"},
                "estimate": {"value": None, "missing": "unclear", "evidence": "p2"},
                "notes": {"value": None},
            }},
        )

    (tmp_path / "sources" / "paper.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)
    runner = Runner()
    state = runner.start(
        project=project, manual=_manual(), coding_sheet=sheet, api_key="fake",
        parallel_requests=1, request_delay_sec=0,
    )
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state.status == "complete"
    assert state.pdfs[0].cells_to_check == 1
    assert state.snapshot()["pdfs"][0]["cells_to_check"] == 1


# Web surfaces


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Missing codes', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def test_run_table_rows_count_cells_to_check_from_progress_or_persisted_result(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    project = create_project('Rows', root=tmp_path / 'projects')
    sheet = CodingSheet(rows=[_row("r1", "a.pdf"), _row("r2", "b.pdf"), _row("r3", "c.pdf")], issues=[])
    persisted = ExtractionResult(
        source_pdf="b.pdf",
        status="ok",
        coded_by_row_id={"r2": {"estimate": {"value": None, "missing": "unclear", "evidence": "p1"}}},
    )
    progress = PdfProgress("a.pdf", status="ok", cells_to_check=3)
    rows = {row["source_pdf"]: row for row in web._run_table_rows([progress, persisted], sheet, project)}
    assert rows["a.pdf"]["cells_to_check"] == 3
    assert rows["b.pdf"]["cells_to_check"] == 1


def test_project_page_shows_cells_to_check_for_persisted_and_live_results(site):
    client, project, runtime = site
    write_manual(project, _manual())
    (project.sources_dir / "paper.pdf").write_bytes(b"%PDF")
    project.coding_sheet_path.write_text(
        "row_id,source_pdf,locator,authors,year\nr1,paper.pdf,exp1,Smith,2024\n"
    )
    runtime._session_keys['gemini'] = 'fake-test-key'
    write_raw_result(
        project,
        ExtractionResult(
            source_pdf="paper.pdf",
            status="ok",
            coded_by_row_id={"r1": {
                "Condition": {"value": "A", "missing": None, "evidence": "p1"},
                "estimate": {"value": None, "missing": "unclear", "evidence": "p2"},
                "notes": {"value": None},
            }},
        ),
        provider="gemini",
        model="test",
    )
    page = client.get(f'/test/projects/{project.project_id}')
    assert page.status_code == 200
    assert 'cells to check: 1' in page.text

    runtime.runner._states[project.project_id] = RunState(
        status="complete", pdfs=[PdfProgress("paper.pdf", status="ok", cells_to_check=2)]
    )
    assert 'cells to check: 2' in client.get(f'/test/projects/{project.project_id}').text
