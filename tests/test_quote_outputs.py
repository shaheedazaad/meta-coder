"""Covers where the quote feature shows up outside quote_check.py: the response
schema in both dialects, validation of `quote` and `page`, the prompt rule, the
cells-to-check count, the per-PDF audit YAML, evidence.csv text, the runner's
quote_check.csv, the provider dispatcher, the download route and the project page.
"""

import csv
import io
import threading
import time
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from meta_coder import providers, runner as runner_module, web
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import parse_coding_manual
from meta_coder.mechanism import build_response_schema, validate_response
from meta_coder.projects import Project, create_project, write_manual
from meta_coder.prompts import PROMPT_VERSION, build_extraction_prompt
from meta_coder.quote_check import PdfText
from meta_coder.results import cells_to_check, collate_results, render_pdf_audit_yaml
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
  Source:
    type: string
    evidence_required: false
"""

BODY = (
    "The intervention improved reading scores substantially across all three "
    "schools in the sample."
)
BODY_QUOTE = "The intervention improved reading scores substantially"


def _manual():
    return parse_coding_manual(MANUAL_YAML)


def _row(row_id="r1", pdf="paper.pdf", locator="Exp 1"):
    return CodingSheetRow(row_id, pdf, locator, "Smith", "2020")


def _field(**extra):
    return {"value": 1.5, "missing": None, "evidence": "Table 2", **extra}


# Schema


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_evidence_fields_carry_a_nullable_quote_and_page_and_require_them(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    item = schema["properties"]["effects"]["items"]
    coded = item["properties"]["estimate"]
    assert {"evidence", "quote", "page"} <= set(coded["properties"])
    assert {"evidence", "quote", "page"} <= set(coded["required"])
    assert "quote" in item["properties"]["Condition"]["properties"]
    assert "quote" not in item["properties"]["notes"]["properties"]
    assert "page" not in item["properties"]["notes"]["properties"]


def test_gemini_quote_and_page_are_nullable_scalars():
    coded = build_response_schema(_manual(), dialect="gemini")["properties"]["effects"]["items"]["properties"]["estimate"]
    assert coded["properties"]["quote"]["type"] == "STRING"
    assert coded["properties"]["quote"]["nullable"] is True
    assert coded["properties"]["page"]["type"] == "INTEGER"
    assert coded["properties"]["page"]["nullable"] is True


def test_json_schema_quote_and_page_allow_null_and_stay_closed():
    coded = build_response_schema(_manual(), dialect="json_schema")["properties"]["effects"]["items"]["properties"]["estimate"]
    assert coded["properties"]["quote"]["type"] == ["string", "null"]
    assert coded["properties"]["page"]["type"] == ["integer", "null"]
    assert "nullable" not in coded["properties"]["quote"]
    assert coded["additionalProperties"] is False


@pytest.mark.parametrize('dialect', ['gemini', 'json_schema'])
def test_field_without_evidence_has_no_quote_or_page(dialect):
    schema = build_response_schema(_manual(), dialect=dialect)
    source = schema["properties"]["effects"]["items"]["properties"]["Source"]
    assert "quote" not in source["properties"]
    assert "page" not in source["properties"]
    assert "quote" not in source["required"]
    assert "page" not in source["required"]


# Validation


def _validate(field, source=None):
    response = {"effects": [{
        "row_id": "r1",
        "Condition": {"value": "A", "missing": None, "evidence": "p1", "quote": None, "page": None},
        "estimate": field,
        "Source": source or {"value": "registry", "missing": None},
        "notes": {"value": None},
    }]}
    return validate_response(response, {"r1"}, _manual().effects)


@pytest.mark.parametrize('extra', [
    {"quote": BODY_QUOTE, "page": 4},
    {"quote": None, "page": None},
    {},
])
def test_quote_and_page_accept_text_or_null_or_absent(extra):
    assert _validate({"value": 1.5, "missing": None, "evidence": "Table 2", **extra}).ok


@pytest.mark.parametrize('extra', [
    {"quote": 5},
    {"quote": ["text"]},
    {"page": "4"},
    {"page": 4.0},
    {"page": True},
])
def test_wrong_typed_quote_or_page_is_an_invalid_field_value(extra):
    result = _validate({"value": 1.5, "missing": None, "evidence": "Table 2", **extra})
    assert not result.ok
    assert "Response returned invalid field value(s): `r1`: estimate." in result.error


def test_quote_is_not_checked_on_fields_without_evidence():
    result = _validate(
        {"value": 1.5, "missing": None, "evidence": "Table 2", "quote": None, "page": None},
        source={"value": "registry", "missing": None, "quote": 5, "page": "x"},
    )
    assert result.ok


# Prompt


def test_prompt_asks_for_an_exact_quote_and_a_page():
    prompt = " ".join(build_extraction_prompt(_manual(), [_row()]).split())
    assert "copied exactly as it appears in the article" in prompt
    assert '"quote"' in prompt and '"page"' in prompt
    assert "counting the file's first page as 1" in prompt
    assert int(PROMPT_VERSION) >= 6


# Cells to check


def test_cells_to_check_counts_not_found_quotes_once_with_unclear_cells():
    coded = {
        "r1": {
            "a": {"value": 1, "quote_check": "not_found"},                       # counted
            "b": {"value": None, "missing": "unclear", "quote_check": "not_found"},  # once
            "c": {"value": 1, "quote_check": "verified"},
            "d": {"value": 1, "quote_check": "approximate"},
            "e": {"value": 1, "quote_check": "not_checked"},
        },
    }
    assert cells_to_check(coded) == 2


# Per-PDF audit YAML


def _audit(estimate):
    result = ExtractionResult(
        source_pdf="paper.pdf", status="ok",
        coded_by_row_id={"r1": {"Condition": {"value": "A", "missing": None, "evidence": "p1"},
                                "estimate": estimate, "Source": {"value": "x"}}},
    )
    text = render_pdf_audit_yaml(manual=_manual(), source_pdf="paper.pdf", rows=[_row()], result=result)
    return yaml.safe_load(text)["effects"]["r1"]["fields"]["estimate"]


def test_audit_yaml_shows_quote_page_and_check_when_given():
    entry = _audit({"value": 1.5, "missing": None, "evidence": "Table 2", "quote": BODY_QUOTE,
                    "page": 4, "quote_check": "approximate", "quote_found_page": 5})
    assert entry == {"value": 1.5, "evidence": "Table 2", "quote": BODY_QUOTE, "page": 4,
                     "quote_check": "approximate", "quote_found_page": 5}


def test_audit_yaml_omits_quote_keys_that_are_none_or_absent():
    entry = _audit({"value": 1.5, "missing": None, "evidence": "Table 2", "quote": None,
                    "page": None, "quote_check": "not_checked"})
    assert entry == {"value": 1.5, "evidence": "Table 2", "quote_check": "not_checked"}


# evidence.csv text


def _evidence_cell(estimate):
    sheet = CodingSheet(rows=[_row()], issues=[])
    result = ExtractionResult(
        source_pdf="paper.pdf", status="ok",
        coded_by_row_id={"r1": {"Condition": {"value": "A", "missing": None, "evidence": "p1"},
                                "estimate": estimate}},
    )
    _, evidence_rows = collate_results(manual=_manual(), coding_sheet=sheet, results_by_pdf={"paper.pdf": result})
    return evidence_rows[0]["estimate"]


@pytest.mark.parametrize(('estimate', 'expected'), [
    ({"value": 1.5, "evidence": "Table 2", "quote": "The effect was 1.5", "page": 4},
     'p. 4: "The effect was 1.5" — Table 2'),
    ({"value": 1.5, "evidence": "Table 2", "quote": "  The effect was 1.5  "},
     '"The effect was 1.5" — Table 2'),
    ({"value": 1.5, "evidence": "Table 2", "page": 4},
     "p. 4: Table 2"),
    ({"value": 1.5, "evidence": "", "page": 4},
     "p. 4"),
    ({"value": 1.5, "evidence": "", "quote": "The effect was 1.5"},
     '"The effect was 1.5"'),
    ({"value": 1.5, "evidence": "", "page": 4, "quote": "The effect was 1.5"},
     'p. 4: "The effect was 1.5"'),
    ({"value": 1.5, "evidence": "Table 2", "quote": "   "},
     "Table 2"),
    ({"value": 1.5, "evidence": "Table 2", "quote": 5, "page": True},
     "Table 2"),
    ({"value": 1.5, "evidence": "p1"},
     "p1"),
])
def test_evidence_cell_prefixes_page_and_quote_only_when_given(estimate, expected):
    assert _evidence_cell(estimate) == expected


# Runner


def _make_project(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "output" / "raw").mkdir(parents=True)
    (tmp_path / "output" / "coded").mkdir(parents=True)
    return Project(project_id="0" * 16, name="t", path=tmp_path, created_at="")


def test_runner_writes_quote_check_csv_with_each_cell_status(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    sheet = CodingSheet(rows=[_row()], issues=[])

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={"r1": {
                "Condition": {"value": "A", "missing": None, "evidence": "p1",
                              "quote": BODY_QUOTE, "page": 1, "quote_check": "verified"},
                "estimate": {"value": 1.5, "missing": None, "evidence": "Table 2",
                             "quote": "A passage that is not in the paper", "page": 3,
                             "quote_check": "not_found"},
                "Source": {"value": "registry", "missing": None},
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

    rows = list(csv.DictReader(io.StringIO((project.output_dir / "quote_check.csv").read_text(encoding="utf-8"))))
    assert len(rows) == 1
    assert rows[0]["row_id"] == "r1"
    assert rows[0]["Condition"] == "verified"
    assert rows[0]["estimate"] == "not_found"
    assert rows[0]["Source"] == ""
    assert rows[0]["notes"] == ""


# Provider dispatcher


@pytest.mark.parametrize('provider', providers.PROVIDERS)
def test_extraction_result_is_annotated_for_every_provider(provider, monkeypatch, tmp_path):
    adapter = getattr(providers, provider)
    result = ExtractionResult(source_pdf="paper.pdf", status="ok", coded_by_row_id={"r1": {}})
    monkeypatch.setattr(adapter, "extract_pdf_effects", Mock(return_value=result))
    annotate = Mock()
    monkeypatch.setattr(providers, "annotate_quote_checks", annotate)
    pdf_path = tmp_path / "paper.pdf"
    returned = providers.extract_pdf_effects(
        provider=provider, pdf_path=pdf_path, manual=None, rows=[], api_key="key", model="model",
        cancel_event=threading.Event(),
    )
    assert returned is result
    annotate.assert_called_once_with(result.coded_by_row_id, pdf_path)


@pytest.mark.parametrize('provider', providers.PROVIDERS)
def test_provider_quotes_are_checked_against_the_pdf_text(provider, monkeypatch, tmp_path):
    adapter = getattr(providers, provider)
    coded = {"r1": {"estimate": {"value": 1.5, "evidence": "p1", "quote": BODY_QUOTE, "page": 1}}}
    monkeypatch.setattr(adapter, "extract_pdf_effects",
                        Mock(return_value=ExtractionResult(source_pdf="paper.pdf", status="ok", coded_by_row_id=coded)))
    from meta_coder import quote_check
    monkeypatch.setattr(quote_check, "read_pdf_text", lambda path: PdfText([BODY]))
    providers.extract_pdf_effects(
        provider=provider, pdf_path=tmp_path / "paper.pdf", manual=None, rows=[], api_key="key", model="model",
    )
    assert coded["r1"]["estimate"]["quote_check"] == "verified"
    assert coded["r1"]["estimate"]["quote_found_page"] == 1


def test_unknown_provider_still_raises_and_skips_the_annotation(monkeypatch, tmp_path):
    annotate = Mock()
    monkeypatch.setattr(providers, "annotate_quote_checks", annotate)
    with pytest.raises(ValueError, match="Unknown provider"):
        providers.extract_pdf_effects(
            provider="unknown", pdf_path=tmp_path / "p.pdf", manual=None, rows=[], api_key="", model="m",
        )
    annotate.assert_not_called()


# Web surfaces


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Quotes', root=tmp_path / 'projects')
    write_manual(project, _manual())
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def test_quote_check_download_is_404_until_the_file_exists(site):
    client, project, _ = site
    url = f'/test/projects/{project.project_id}/download/quote-check'
    response = client.get(url)
    assert response.status_code == 404
    assert response.json()['detail'] == 'No quote checks yet.'
    (project.output_dir / 'quote_check.csv').write_bytes(b'row_id,Condition\nr1,verified\n')
    response = client.get(url)
    assert response.status_code == 200
    assert response.text == 'row_id,Condition\nr1,verified\n'
    assert 'quote_check.csv' in response.headers['content-disposition']


def test_project_page_links_quote_check_only_when_the_file_exists(site):
    client, project, _ = site
    link = f'/test/projects/{project.project_id}/download/quote-check'
    # The download buttons only render once the coded and evidence CSVs exist.
    (project.output_dir / 'coded_data.csv').write_text('row_id,Condition\n')
    (project.output_dir / 'evidence.csv').write_text('row_id,Condition\n')
    assert link not in client.get(f'/test/projects/{project.project_id}').text
    (project.output_dir / 'quote_check.csv').write_text('row_id,Condition\n')
    page = client.get(f'/test/projects/{project.project_id}')
    assert page.status_code == 200
    assert link in page.text
    assert 'Download quote_check.csv' in page.text
