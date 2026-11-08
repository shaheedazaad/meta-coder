"""The regression set in `evals/` stays consistent, and replaying its saved
provider responses through the current parsing, validation and scoring
reproduces the committed scores. No provider is called.

The PDFs are downloaded by `evals/fetch_pdfs.py` (CI does this); tests that
need them skip when they are absent.
"""

import csv
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import fetch_pdfs  # noqa: E402
import run_eval  # noqa: E402
from meta_coder.manual import read_coding_manual  # noqa: E402

BASELINES = sorted(path for path in (EVALS / "baselines").glob("*") if (path / "summary.json").is_file())


def _rows(name):
    with (EVALS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _pdfs_present():
    return all((EVALS / "pdfs" / a["source_pdf"]).is_file() for a in _rows("articles.csv"))


needs_pdfs = pytest.mark.skipif(not _pdfs_present(), reason="run python evals/fetch_pdfs.py")


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


def test_sets_and_manifest_agree():
    articles = _rows("articles.csv")
    assert {row["source_pdf"] for row in _rows("coding_sheet.csv")} == {a["source_pdf"] for a in articles}
    assert len(run_eval.rows_by_pdf("small")) == 5
    assert len(run_eval.rows_by_pdf("broad")) == 10


@needs_pdfs
def test_pdfs_match_the_manifest_and_the_scanned_copy_has_no_text():
    for article in _rows("articles.csv"):
        data = (EVALS / "pdfs" / article["source_pdf"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == article["sha256"], article["source_pdf"]
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


def test_a_missing_human_code_matches_any_empty_value():
    truth = {cell["row_id"]: cell for cell in _rows("ground_truth.csv") if cell["missing"]}
    row_id, cell = next(iter(truth.items()))
    pdf = next(row["source_pdf"] for row in _rows("coding_sheet.csv") if row["row_id"] == row_id)
    blank = {"source_pdf": pdf, "status": "ok", "coded_by_row_id": {row_id: {cell["field"]: {"value": None}}}}
    filled = {**blank, "coded_by_row_id": {row_id: {cell["field"]: {"value": "something"}}}}
    assert run_eval.score({pdf: blank})["missing_values"]["left_blank"] >= 1
    assert run_eval.score({pdf: filled})["missing_values"]["value_given"] >= 1


def test_regressions_report_an_accuracy_drop_and_more_pdfs_needing_review():
    baseline = {"accuracy": 0.9, "statuses": {"ok": 5}}
    assert run_eval.regressions({"accuracy": 0.87, "statuses": {"ok": 5}}, baseline) == []
    problems = run_eval.regressions({"accuracy": 0.8, "statuses": {"ok": 4, "needs_review": 1}}, baseline)
    assert len(problems) == 2


@pytest.mark.skipif(not BASELINES, reason="no baseline committed in evals/baselines")
@pytest.mark.parametrize("baseline", BASELINES, ids=lambda path: path.name)
def test_replaying_the_saved_responses_reproduces_the_committed_scores(baseline, tmp_path, capsys):
    run_dir = tmp_path / "run"
    shutil.copytree(baseline, run_dir)
    committed = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
    code = run_eval.main(["replay", str(run_dir), "--baseline", str(baseline / "summary.json")])
    replayed = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert code == 0
    assert replayed == committed
    assert "Cells correct" in capsys.readouterr().out


def test_run_stops_before_calling_a_provider_when_pdfs_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    for name in ("articles.csv", "coding_sheet.csv"):
        shutil.copy(EVALS / name, tmp_path / name)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(run_eval, "extract_pdf_effects", lambda **_: pytest.fail("provider called"))
    with pytest.raises(SystemExit, match="fetch_pdfs.py"):
        run_eval.main(["run", "--name", "x"])


def test_fetch_downloads_only_absent_or_wrong_pdfs_and_rejects_bad_checksums(tmp_path, monkeypatch):
    good = b"%PDF good"
    articles = [
        {"source_pdf": "kept.pdf", "sha256": hashlib.sha256(good).hexdigest()},
        {"source_pdf": "new.pdf", "sha256": hashlib.sha256(good).hexdigest()},
        {"source_pdf": "bad.pdf", "sha256": "0" * 64},
        {"source_pdf": "gone.pdf", "sha256": "0" * 64},
    ]
    (tmp_path / "kept.pdf").write_bytes(good)
    requested = []

    def fake_urlopen(url, timeout):
        requested.append(url.rsplit("/", 1)[1])
        if url.endswith("gone.pdf"):
            raise OSError("404")
        return io.BytesIO(good)

    monkeypatch.setattr(fetch_pdfs, "articles", lambda: articles)
    monkeypatch.setattr(fetch_pdfs.urllib.request, "urlopen", fake_urlopen)
    problems = fetch_pdfs.fetch(tmp_path, "https://example.test/")
    assert requested == ["new.pdf", "bad.pdf", "gone.pdf"]
    assert (tmp_path / "new.pdf").read_bytes() == good
    assert not (tmp_path / "bad.pdf").exists()
    assert len(problems) == 2 and "checksum" in problems[0] and "download failed" in problems[1]
    monkeypatch.setattr(fetch_pdfs, "fetch", lambda: problems)
    assert fetch_pdfs.main() == 1
