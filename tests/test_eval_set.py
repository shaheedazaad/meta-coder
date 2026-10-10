"""The regression set in `evals/` stays consistent, and replaying its saved
provider responses through the current validation and quote check reproduces
the committed scores. No provider is called."""

import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import run_eval  # noqa: E402
from meta_coder.manual import read_coding_manual  # noqa: E402

BASELINE = EVALS / "baselines" / "gemini-3.7-flash-small"


def _rows(name):
    with (EVALS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_every_row_has_a_valid_human_code_for_every_scored_field():
    manual = read_coding_manual(EVALS / "manual.yml")
    scored = set(manual.effects) - {"notes"}
    truth = _rows("ground_truth.csv")
    by_row = {}
    for cell in truth:
        by_row.setdefault(cell["row_id"], set()).add(cell["field"])
        spec = manual.effects[cell["field"]]
        assert bool(cell["value"]) != bool(cell["missing"]), cell
        assert cell["missing"] in ("", "not_reported", "not_applicable", "unclear")
        if spec.levels and cell["value"]:
            assert cell["value"] in {level.value for level in spec.levels}, cell
    sheet = _rows("coding_sheet.csv")
    assert {row["row_id"] for row in sheet} == set(by_row)
    assert all(fields == scored for fields in by_row.values())


def test_pdfs_match_the_manifest_and_the_scanned_copy_has_no_text():
    articles = _rows("articles.csv")
    assert {row["source_pdf"] for row in _rows("coding_sheet.csv")} == {a["source_pdf"] for a in articles}
    for article in articles:
        data = (EVALS / "pdfs" / article["source_pdf"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == article["sha256"], article["source_pdf"]
    assert len(run_eval.rows_by_pdf("small")) == 5
    assert len(run_eval.rows_by_pdf("broad")) == 10
    scanned = PdfReader(EVALS / "pdfs" / "xia_2022_scanned.pdf")
    assert not "".join(page.extract_text() or "" for page in scanned.pages).strip()


@pytest.mark.parametrize(("coded", "truth", "compare", "expected"), [
    ("daily  Work", "Daily work", "exact", True),
    ("Lab", "Daily work", "exact", False),
    (True, "true", "bool", True),
    (False, "true", "bool", False),
    ("true", "true", "bool", False),
    (0.3, "0.3", "number:0.011", True),
    (0.32, "0.3", "number:0.011", False),
    (-0.55, "0.55", "number:0.011", False),
    (-0.55, "0.55", "number_abs:0.011", True),
    ("high", "0.3", "number:0.011", False),
    (True, "1", "number:0.5", False),
    ("2012–2013", "2012-2013", "years", True),
    ("2013", "2012-2013", "years", False),
])
def test_values_match(coded, truth, compare, expected):
    assert run_eval.values_match(coded, truth, compare) is expected


def test_regressions_report_an_accuracy_drop_and_more_pdfs_needing_review():
    baseline = {"accuracy": 0.9, "statuses": {"ok": 5}}
    assert run_eval.regressions({"accuracy": 0.87, "statuses": {"ok": 5}}, baseline) == []
    problems = run_eval.regressions({"accuracy": 0.8, "statuses": {"ok": 4, "needs_review": 1}}, baseline)
    assert len(problems) == 2


def test_replaying_the_saved_responses_reproduces_the_committed_scores(tmp_path, capsys):
    run_dir = tmp_path / "run"
    shutil.copytree(BASELINE, run_dir)
    committed = json.loads((BASELINE / "summary.json").read_text(encoding="utf-8"))
    code = run_eval.main(["replay", str(run_dir), "--baseline", str(BASELINE / "summary.json")])
    replayed = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert code == 0
    assert replayed == committed
    assert "Cells correct" in capsys.readouterr().out
