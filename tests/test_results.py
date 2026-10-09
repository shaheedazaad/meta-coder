import pytest
import yaml

from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import parse_coding_manual
from meta_coder.results import collate_results, render_pdf_audit_yaml


MANUAL_YAML = """
name: t
effect_definition: x
effects:
  Condition: {type: string}
  ResponseTimeMs: {type: number}
"""


def test_collate_joins_coding_sheet_fields_and_marks_status():
    manual = parse_coding_manual(MANUAL_YAML)
    sheet = CodingSheet(
        rows=[
            CodingSheetRow("r1", "paper.pdf", "Exp 1", "Smith", "2020"),
            CodingSheetRow("r2", "paper.pdf", "Exp 2", "Smith", "2020"),
            CodingSheetRow("r3", "other.pdf", "Exp 1", "Jones", "2019"),
        ],
        issues=[],
    )
    results_by_pdf = {
        "paper.pdf": ExtractionResult(
            source_pdf="paper.pdf",
            status="ok",
            coded_by_row_id={
                "r1": {
                    "Condition": {"value": "Incompatible", "evidence": "p3"},
                    "ResponseTimeMs": {"value": 512, "evidence": "p3"},
                },
                "r2": {
                    "Condition": {"value": "Compatible", "evidence": "p4"},
                    "ResponseTimeMs": {"value": 480, "evidence": "p4"},
                },
            },
        )
    }
    coded_rows, evidence_rows = collate_results(
        manual=manual, coding_sheet=sheet, results_by_pdf=results_by_pdf
    )
    by_id = {row["row_id"]: row for row in coded_rows}
    assert by_id["r1"]["authors"] == "Smith"
    assert by_id["r1"]["Condition"] == "Incompatible"
    assert by_id["r1"]["status"] == "ok"
    assert by_id["r3"]["status"] == "not_run"  # other.pdf was never processed

    evidence_by_id = {row["row_id"]: row for row in evidence_rows}
    assert evidence_by_id["r1"]["Condition"] == "p3"


def test_audit_yaml_is_readable_and_shows_missing_rows():
    manual = parse_coding_manual(MANUAL_YAML)
    rows = [
        CodingSheetRow("r1", "paper.pdf", "Exp 1", "Smith", "2020"),
        CodingSheetRow("r2", "paper.pdf", "Exp 2", "Smith", "2020"),
    ]
    # r2 was requested but the model never returned it — needs_review.
    result = ExtractionResult(
        source_pdf="paper.pdf",
        status="needs_review",
        coded_by_row_id={
            "r1": {"Condition": {"value": "Incompatible", "evidence": "p3, 'RT was slower...'"}}
        },
        missing_ids={"r2"},
    )
    text = render_pdf_audit_yaml(manual=manual, source_pdf="paper.pdf", rows=rows, result=result)

    parsed = yaml.safe_load(text)
    assert parsed["status"] == "needs_review"
    assert parsed["missing_row_ids"] == ["r2"]
    assert parsed["effects"]["r1"]["locator"] == "Exp 1"
    assert parsed["effects"]["r1"]["fields"]["Condition"]["value"] == "Incompatible"
    assert parsed["effects"]["r1"]["fields"]["Condition"]["evidence"] == "p3, 'RT was slower...'"
    assert parsed["effects"]["r2"]["status"] == "not returned by the model"


def test_collate_renders_null_as_not_reported():
    manual = parse_coding_manual(MANUAL_YAML)
    sheet = CodingSheet(rows=[CodingSheetRow("r1", "paper.pdf", "Exp 1", "Smith", "2020")], issues=[])
    result = ExtractionResult(
        source_pdf="paper.pdf",
        status="ok",
        coded_by_row_id={
            "r1": {
                "Condition": {"value": None, "evidence": "Not reported in the article."},
                "ResponseTimeMs": {"value": None, "evidence": "Not reported in the article."},
                "notes": {"value": None},
            }
        },
    )
    coded_rows, _ = collate_results(manual=manual, coding_sheet=sheet, results_by_pdf={"paper.pdf": result})
    assert coded_rows[0]["Condition"] == "Not Reported"
    assert coded_rows[0]["ResponseTimeMs"] == "Not Reported"


@pytest.mark.parametrize('field', ['malformed', 7, [1], True])
def test_malformed_review_fields_do_not_break_exports(field):
    from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
    from meta_coder.extraction import ExtractionResult
    from meta_coder.manual import parse_coding_manual
    from meta_coder.results import collate_results, render_pdf_audit_yaml
    manual = parse_coding_manual('effect_definition: comparison\neffects:\n  estimate: {type: number}\n')
    rows = [CodingSheetRow('r1', 'p.pdf', '')]
    result = ExtractionResult('p.pdf', 'needs_review', coded_by_row_id={'r1': {'estimate': field}}, raw_response='original malformed response')
    coded, evidence = collate_results(manual=manual, coding_sheet=CodingSheet(rows, []), results_by_pdf={'p.pdf': result})
    assert coded[0]['estimate'] == '' and evidence[0]['estimate'] == ''
    audit = render_pdf_audit_yaml(manual=manual, source_pdf='p.pdf', rows=rows, result=result)
    assert 'needs_review' in audit
    assert result.coded_by_row_id['r1']['estimate'] == field
