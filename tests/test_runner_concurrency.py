"""Proves the runner's worker pool actually parallelizes, and that the request
pacer's minimum interval is GLOBAL across workers, not per-worker — a per-worker
pacer would let N workers all start at once every interval, defeating the whole
point of "1 request every N seconds" as a rate-limit safeguard.
"""

import threading
import time

import meta_coder.runner as runner_module
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import Project
from meta_coder.runner import Runner, load_persisted_results


MANUAL_YAML = """
name: t
effect_definition: x
effects:
  Condition: {type: string}
"""


def _make_project(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "output" / "raw").mkdir(parents=True)
    (tmp_path / "output" / "coded").mkdir(parents=True)
    return Project(project_id="0" * 16, name="t", path=tmp_path, created_at="")


def _make_sheet(pdf_names):
    rows = [
        CodingSheetRow(row_id=f"r{i}", source_pdf=name, locator=f"Exp {i}")
        for i, name in enumerate(pdf_names)
    ]
    return CodingSheet(rows=rows, issues=[])


def test_concurrent_run_attributes_results_to_the_correct_pdf(tmp_path, monkeypatch):
    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    pdf_names = [f"paper{i}.pdf" for i in range(5)]
    sheet = _make_sheet(pdf_names)

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        time.sleep(0.05)  # encourage real interleaving between workers
        row = rows[0]
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={row.row_id: {"Condition": {"value": pdf_path.name, "evidence": "p1"}}},
        )

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)

    runner = Runner()
    state = runner.start(
        project=project,
        manual=manual,
        coding_sheet=sheet,
        api_key="fake",
        parallel_requests=5,
        request_delay_sec=0,
    )
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.02)

    assert state.status == "complete"
    assert state.processed == 5
    for progress in state.pdfs:
        assert progress.status == "ok"

    coded_csv = (project.output_dir / "coded_data.csv").read_text()
    for name in ("coded_data.csv", "evidence.csv"):
        # csv's own CRLF must not be translated again on Windows (\r\r\n = blank rows)
        assert b"\r\r\n" not in (project.output_dir / name).read_bytes()
    for name in pdf_names:
        # each row's Condition value was set to its OWN pdf_path.name by the fake —
        # if results ever got cross-attributed under concurrency, this would fail.
        assert name in coded_csv


def test_request_pacer_delay_is_global_not_per_worker(tmp_path, monkeypatch):
    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    pdf_names = [f"paper{i}.pdf" for i in range(3)]
    sheet = _make_sheet(pdf_names)
    lock = threading.Lock()
    start_times = []

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        with lock:
            start_times.append(time.monotonic())
        row = rows[0]
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={row.row_id: {"Condition": {"value": "x", "evidence": "p1"}}},
        )

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)

    runner = Runner()
    # request_delay_sec is deliberately whole seconds (matches RunSettings/the
    # Setup-tab number input) — the pacer truncates via int(), so a sub-1s value
    # here would test nothing.
    delay = 1
    runner.start(
        project=project,
        manual=manual,
        coding_sheet=sheet,
        api_key="fake",
        parallel_requests=3,  # all 3 workers could start at once without a global pacer
        request_delay_sec=delay,
    )
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.02)

    assert len(start_times) == 3
    start_times.sort()
    # A per-worker (not global) pacer would let all 3 start together at t=0.
    # A correctly-global pacer spaces every start by >= delay.
    assert start_times[1] - start_times[0] >= delay * 0.8
    assert start_times[2] - start_times[1] >= delay * 0.8


def _run_to_completion(runner, project_id, timeout=5):
    deadline = time.monotonic() + timeout
    while runner.is_running(project_id) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_retry_of_a_subset_does_not_blank_out_other_pdfs_results(tmp_path, monkeypatch):
    """Regression test for the rehydration bug: a retry of just the failed PDF
    must not overwrite coded_data.csv with blank rows for the PDF that already
    succeeded in an earlier run and isn't part of this run at all."""

    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    sheet = _make_sheet(["ok.pdf", "bad.pdf"])

    calls = []

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        calls.append(pdf_path.name)
        row = rows[0]
        if pdf_path.name == "bad.pdf" and calls.count("bad.pdf") == 1:
            return ExtractionResult(source_pdf=pdf_path.name, status="error", error="boom")
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={row.row_id: {"Condition": {"value": pdf_path.name, "evidence": "p1"}}},
        )

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)

    runner = Runner()
    runner.start(project=project, manual=manual, coding_sheet=sheet, api_key="fake")
    _run_to_completion(runner, project.project_id)
    state = runner.state(project.project_id)
    assert state.status == "complete"
    assert {p.source_pdf: p.status for p in state.pdfs} == {"ok.pdf": "ok", "bad.pdf": "error"}

    # Retry only the failed PDF.
    runner.start(
        project=project, manual=manual, coding_sheet=sheet, api_key="fake", only_pdfs=["bad.pdf"]
    )
    _run_to_completion(runner, project.project_id)
    state = runner.state(project.project_id)
    assert state.status == "complete"
    assert [p.source_pdf for p in state.pdfs] == ["bad.pdf"]
    assert state.pdfs[0].status == "ok"

    coded_csv = (project.output_dir / "coded_data.csv").read_text()
    # ok.pdf's row must still show its prior successful code, not a blank
    # "not_run" row, even though this run never touched ok.pdf at all.
    assert ",ok.pdf," in coded_csv
    assert "not_run" not in coded_csv


def test_provider_exception_is_persisted_for_review_after_restart(tmp_path, monkeypatch):
    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    sheet = _make_sheet(["failed.pdf"])

    def fake_extract(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)

    runner = Runner()
    runner.start(project=project, manual=manual, coding_sheet=sheet, api_key="fake")
    _run_to_completion(runner, project.project_id)

    persisted = load_persisted_results(project)
    assert persisted["failed.pdf"].status == "error"
    assert "provider unavailable" in (persisted["failed.pdf"].error or "")


def test_resume_skips_already_successfully_coded_pdfs(tmp_path, monkeypatch):
    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    sheet = _make_sheet(["ok.pdf", "retry.pdf"])
    calls = []

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        calls.append(pdf_path.name)
        row = rows[0]
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok" if pdf_path.name == "ok.pdf" else "error",
            coded_by_row_id={row.row_id: {"Condition": {"value": "x", "evidence": "p1"}}},
        )

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)
    runner = Runner()
    runner.start(project=project, manual=manual, coding_sheet=sheet, api_key="fake")
    _run_to_completion(runner, project.project_id)
    runner.start(project=project, manual=manual, coding_sheet=sheet, api_key="fake")
    _run_to_completion(runner, project.project_id)

    assert calls.count("ok.pdf") == 1
    assert calls.count("retry.pdf") == 2


def test_cancel_skips_unstarted_pdfs_but_keeps_completed_ones(tmp_path, monkeypatch):
    manual = parse_coding_manual(MANUAL_YAML)
    project = _make_project(tmp_path)
    pdf_names = [f"paper{i}.pdf" for i in range(4)]
    sheet = _make_sheet(pdf_names)

    def fake_extract(*, pdf_path, manual, rows, api_key, model, **_kwargs):
        time.sleep(0.1)
        row = rows[0]
        return ExtractionResult(
            source_pdf=pdf_path.name,
            status="ok",
            coded_by_row_id={row.row_id: {"Condition": {"value": "x", "evidence": "p1"}}},
        )

    monkeypatch.setattr(runner_module, "extract_pdf_effects", fake_extract)

    runner = Runner()
    state = runner.start(
        project=project, manual=manual, coding_sheet=sheet, api_key="fake", parallel_requests=1
    )
    time.sleep(0.05)  # let the first PDF start
    runner.cancel(project.project_id)
    assert state.status == "cancelling"
    assert runner.is_running(project.project_id)  # cancelling still counts as "in progress"

    _run_to_completion(runner, project.project_id)
    assert state.status == "cancelled"
    statuses = {p.status for p in state.pdfs}
    assert "cancelled" in statuses  # at least one queued PDF never started
    assert "ok" in statuses  # the in-flight one was allowed to finish, not killed
