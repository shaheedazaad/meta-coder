"""Run execution: a worker pool sized to the project's configured
`parallel_requests`, plus a shared pacer enforcing a minimum interval between
request *starts* across all workers (plan.md Part A7) — "1 request every N
seconds" means that globally, not per worker. Defaults to parallel_requests=1,
request_delay_sec=0 (plain sequential, no artificial pacing) unless the user
configures otherwise on the project's Setup tab.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .coding_sheet import CodingSheet
from .extraction import ExtractionResult
from .manual import CodingManual
from .projects import Project
from .prompts import PROMPT_VERSION
from .provenance import AuditOperation, audited_call, json_bytes
from .providers import DEFAULT_PROVIDER, default_model, extract_pdf_effects
from .results import cells_to_check, collate_field_key, collate_results, render_pdf_audit_yaml, rows_to_csv


_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_stem(source_pdf: str) -> str:
    return _SAFE_STEM_RE.sub("_", Path(source_pdf).stem)


def raw_json_path(project: Project, source_pdf: str) -> Path:
    """Return the collision-resistant path for a PDF's raw result."""
    digest = hashlib.sha256(source_pdf.encode("utf-8")).hexdigest()[:16]
    return project.raw_dir / f"{_safe_stem(source_pdf)}-{digest}.json"


def audit_yaml_path(project: Project, source_pdf: str) -> Path:
    digest = hashlib.sha256(source_pdf.encode("utf-8")).hexdigest()[:16]
    return project.audit_dir / f"{_safe_stem(source_pdf)}-{digest}.yaml"


def raw_json_path_for_read(project: Project, source_pdf: str) -> Path:
    """Resolve the new path first and fall back to pre-hash persisted data."""
    canonical = raw_json_path(project, source_pdf)
    if canonical.is_file():
        return canonical
    return project.raw_dir / f"{_safe_stem(source_pdf)}.json"


def write_raw_result(project: Project, result: ExtractionResult, *, provider: str, model: str) -> None:
    """Persist every completed attempt, including provider failures, for retries and review."""

    raw_json_path(project, result.source_pdf).write_text(
        json.dumps(
            {
                "source_pdf": result.source_pdf,
                "audit_operation_id": result.audit_operation_id,
                "provider": provider,
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "status": result.status,
                "error": result.error,
                "coded_by_row_id": result.coded_by_row_id,
                "missing_ids": sorted(result.missing_ids),
                "extra_ids": sorted(result.extra_ids),
                "raw_response": result.raw_response,
                "repaired_response": result.repaired_response,
                "duration_sec": result.duration_sec,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _result_from_raw_json(path: Path) -> ExtractionResult | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("source_pdf"):
        return None
    return ExtractionResult(
        source_pdf=data["source_pdf"],
        audit_operation_id=data.get("audit_operation_id"),
        status=data.get("status") or "error",
        coded_by_row_id=data.get("coded_by_row_id") or {},
        missing_ids=set(data.get("missing_ids") or []),
        extra_ids=set(data.get("extra_ids") or []),
        raw_response=data.get("raw_response"),
        repaired_response=data.get("repaired_response"),
        error=data.get("error"),
        duration_sec=data.get("duration_sec") or 0.0,
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
    )


def load_persisted_results(project: Project) -> dict[str, ExtractionResult]:
    """Rehydrate prior attempts from `output/raw/*.json` so a partial run (a
    retry of just a few PDFs, or a cancelled run resumed later) doesn't
    overwrite `coded_data.csv`/`evidence.csv` with blank rows for every PDF
    this particular run didn't touch. Keyed off the `source_pdf` field stored
    *inside* each file, not the filename — `_safe_stem` is lossy, so two
    different source filenames can collide on the same stem."""

    results: dict[str, ExtractionResult] = {}
    if not project.raw_dir.is_dir():
        return results
    for path in project.raw_dir.glob("*.json"):
        result = _result_from_raw_json(path)
        if result is not None:
            # Prefer the canonical collision-resistant record when both it and
            # a legacy lossy-stem record exist for the same source PDF.
            if path == raw_json_path(project, result.source_pdf) or result.source_pdf not in results:
                results[result.source_pdf] = result
    return results


class _RequestPacer:
    """Enforces a minimum interval between request *starts*, shared across every
    worker thread — not a per-worker delay, a global one."""

    def __init__(self, delay_sec: int) -> None:
        self.delay_sec = max(0, int(delay_sec))
        self._last_started: float | None = None
        self._lock = threading.Lock()

    def wait(self, cancel_event: threading.Event | None = None) -> bool:
        if self.delay_sec <= 0:
            return not (cancel_event and cancel_event.is_set())
        while True:
            with self._lock:
                now = time.monotonic()
                remaining = 0.0 if self._last_started is None else self.delay_sec - (now - self._last_started)
                if remaining <= 0:
                    self._last_started = now
                    return True
            if cancel_event and cancel_event.wait(min(remaining, 0.1)):
                return False


@dataclass
class PdfProgress:
    source_pdf: str
    status: str = "pending"  # pending | running | ok | needs_review | error | cancelled
    error: str | None = None
    missing_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    json_repaired: bool = False
    cells_to_check: int = 0


@dataclass
class RunState:
    status: str = "idle"  # idle | running | cancelling | complete | failed | cancelled
    total: int = 0
    processed: int = 0
    pdfs: list[PdfProgress] = field(default_factory=list)
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    audit_run: AuditOperation | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "error": self.error,
            "pdfs": [
                {
                    "source_pdf": p.source_pdf,
                    "status": p.status,
                    "error": p.error,
                    "missing_ids": p.missing_ids,
                    "extra_ids": p.extra_ids,
                    "input_tokens": p.input_tokens,
                    "output_tokens": p.output_tokens,
                    "json_repaired": p.json_repaired,
                    "cells_to_check": p.cells_to_check,
                }
                for p in self.pdfs
            ],
        }


class Runner:
    """Holds one RunState per project in memory. A single-user local app doesn't
    need persistence for run progress across restarts."""

    def __init__(self) -> None:
        self._states: dict[str, RunState] = {}
        self._lock = threading.Lock()

    def state(self, project_id: str) -> RunState | None:
        return self._states.get(project_id)

    def is_running(self, project_id: str) -> bool:
        state = self._states.get(project_id)
        return bool(state and state.status in ("running", "cancelling"))

    def cancel(self, project_id: str) -> None:
        """Cooperative cancellation: flips a flag `process_one` checks before
        starting each PDF. A PDF already mid-request runs to completion rather
        than being killed — the "next safe checkpoint" plan.md A7 calls for."""

        state = self._states.get(project_id)
        if state and state.status == "running":
            state.cancel_requested = True
            state.cancel_event.set()
            state.status = "cancelling"

    def start(
        self,
        *,
        project: Project,
        manual: CodingManual,
        coding_sheet: CodingSheet,
        api_key: str,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        parallel_requests: int = 1,
        request_delay_sec: int = 0,
        request_timeout_sec: int | None = None,
        service_tier: str | None = None,
        reasoning_effort: str = "",
        only_pdfs: list[str] | None = None,
        base_url: str = "",
        response_format: str = "json_schema",
    ) -> RunState:
        """`only_pdfs`, when given, restricts the run to that subset of the
        coding sheet's PDFs (todo.md step 11's retry: reprocess only
        failed/needs_review PDFs, optionally narrowed to a selection) instead
        of the full set every PDF the coding sheet references."""

        if self.is_running(project.project_id):
            raise RuntimeError("This project is already running.")
        model = model or default_model(provider)

        all_pdf_names = sorted({row.source_pdf for row in coding_sheet.rows})
        if only_pdfs is not None:
            wanted = set(only_pdfs)
            pdf_names = [name for name in all_pdf_names if name in wanted]
        else:
            prior_results = load_persisted_results(project)
            # A normal run is resumable: a successfully coded PDF is durable
            # in output/raw and is never sent to the provider again. Explicit
            # retries intentionally bypass this filter.
            pdf_names = [
                name
                for name in all_pdf_names
                if name not in prior_results or prior_results[name].status != "ok"
            ]
        if not pdf_names:
            raise RuntimeError("No matched PDFs still need coding.")

        state = RunState(
            status="running",
            total=len(pdf_names),
            pdfs=[PdfProgress(source_pdf=name) for name in pdf_names],
            started_at=time.time(),
        )
        state.audit_run = AuditOperation(project, "extraction_run", {
            "provider": provider, "model": model, "prompt_version": PROMPT_VERSION,
            "parallel_requests": parallel_requests,
            "request_delay_sec": request_delay_sec, "timeout_sec": request_timeout_sec,
            "service_tier": service_tier, "reasoning_effort": reasoning_effort,
            "base_url": base_url, "response_format": response_format,
            "selected_pdfs": pdf_names, "retry_selection": only_pdfs,
        }, secrets=(api_key,))
        state.audit_run.input("effective_manual.json", json_bytes(manual))
        state.audit_run.input("effective_coding_sheet.json", json_bytes(coding_sheet))
        with self._lock:
            self._states[project.project_id] = state

        thread = threading.Thread(
            target=self._run,
            args=(
                project,
                manual,
                coding_sheet,
                api_key,
                provider,
                model,
                state,
                parallel_requests,
                request_delay_sec,
                request_timeout_sec,
                service_tier,
                reasoning_effort,
                base_url,
                response_format,
            ),
            daemon=True,
        )
        thread.start()
        return state

    def _run(
        self,
        project: Project,
        manual: CodingManual,
        coding_sheet: CodingSheet,
        api_key: str,
        provider: str,
        model: str,
        state: RunState,
        parallel_requests: int,
        request_delay_sec: int,
        request_timeout_sec: int | None,
        service_tier: str | None,
        reasoning_effort: str,
        base_url: str,
        response_format: str,
    ) -> None:
        # Seed from prior attempts (see `_load_existing_results`) so a partial run
        # — a retry of a few PDFs, or a run cancelled midway — upserts over the
        # existing record instead of collating as if every other PDF was never
        # processed.
        results_by_pdf: dict[str, ExtractionResult] = load_persisted_results(project)
        results_lock = threading.Lock()
        pacer = _RequestPacer(request_delay_sec)
        provider_kwargs: dict[str, object] = {}
        if provider == "openai_compatible":
            provider_kwargs.update(base_url=base_url, response_format=response_format)
        if service_tier:
            provider_kwargs["service_tier"] = service_tier
        if reasoning_effort:
            provider_kwargs["reasoning_effort"] = reasoning_effort

        def process_one(progress: PdfProgress) -> None:
            if state.cancel_requested:
                with results_lock:
                    progress.status = "cancelled"
                    state.processed += 1
                return

            progress.status = "running"
            pdf_path = project.sources_dir / progress.source_pdf
            rows = coding_sheet.rows_for_pdf(progress.source_pdf)
            try:
                if not pacer.wait(state.cancel_event):
                    with results_lock:
                        progress.status = "cancelled"
                        state.processed += 1
                    return
                result = audited_call(
                    project, "extraction", extract_pdf_effects,
                    audit_inputs={"run_coding_sheet.json": json_bytes(coding_sheet)},
                    audit_settings={"prompt_version": PROMPT_VERSION,
                                    "parallel_requests": parallel_requests, "request_delay_sec": request_delay_sec,
                                    "run_id": state.audit_run.id if state.audit_run else None},
                    provider=provider,
                    pdf_path=pdf_path,
                    manual=manual,
                    rows=rows,
                    api_key=api_key,
                    model=model,
                    timeout_sec=request_timeout_sec,
                    **provider_kwargs,
                    cancel_event=state.cancel_event,
                )
                write_raw_result(project, result, provider=provider, model=model)
                audit_yaml_path(project, progress.source_pdf).write_text(
                    render_pdf_audit_yaml(
                        manual=manual, source_pdf=progress.source_pdf, rows=rows, result=result
                    ),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001 - one PDF's failure (e.g. a
                # disk write error) must not abort the whole run and lose every
                # other already-completed PDF's results — see collate below.
                result = ExtractionResult(
                    source_pdf=progress.source_pdf, status="error", error=f"Unexpected error: {exc}"
                )
                try:
                    write_raw_result(project, result, provider=provider, model=model)
                except OSError:
                    # The original failure still appears in this run's state;
                    # a storage failure simply cannot survive a restart.
                    pass

            with results_lock:
                results_by_pdf[progress.source_pdf] = result
                progress.status = result.status
                progress.error = result.error
                progress.missing_ids = sorted(result.missing_ids)
                progress.extra_ids = sorted(result.extra_ids)
                progress.input_tokens = result.input_tokens
                progress.output_tokens = result.output_tokens
                progress.json_repaired = result.repaired_response is not None
                progress.cells_to_check = cells_to_check(result.coded_by_row_id)
                state.processed += 1

        try:
            project.audit_dir.mkdir(parents=True, exist_ok=True)
            workers = max(1, min(int(parallel_requests), len(state.pdfs) or 1))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(process_one, progress) for progress in state.pdfs]
                for future in futures:
                    future.result()  # process_one never raises; this only ever
                    # surfaces a genuine framework-level bug, not a per-PDF failure

            coded_rows, evidence_rows = collate_results(
                manual=manual, coding_sheet=coding_sheet, results_by_pdf=results_by_pdf
            )
            (project.output_dir / "coded_data.csv").write_text(
                rows_to_csv(coded_rows, manual), encoding="utf-8"
            )
            (project.output_dir / "evidence.csv").write_text(
                rows_to_csv(evidence_rows, manual), encoding="utf-8"
            )
            output_names = ["coded_data.csv", "evidence.csv"]
            if manual.confidence:
                confidence_rows = collate_field_key(
                    "confidence", manual=manual, coding_sheet=coding_sheet, results_by_pdf=results_by_pdf
                )
                (project.output_dir / "confidence.csv").write_text(
                    rows_to_csv(confidence_rows, manual), encoding="utf-8"
                )
                output_names.append("confidence.csv")
            if state.audit_run:
                for name in output_names:
                    state.audit_run.input(name, (project.output_dir / name).read_bytes())
                state.audit_run.input("final_results.json", json_bytes(results_by_pdf))
            terminal_status = "cancelled" if state.cancel_requested else "complete"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            terminal_status = "failed"
            state.error = str(exc)
        finally:
            state.finished_at = time.time()
            if state.audit_run:
                try:
                    state.audit_run.finish(result={**state.snapshot(), "status": terminal_status}, status=terminal_status)
                except OSError as exc:
                    terminal_status = "failed"
                    state.error = f"Could not finalize audit history: {exc}"
            state.status = terminal_status
