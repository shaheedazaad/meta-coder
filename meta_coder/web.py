from __future__ import annotations

from dataclasses import replace
from html import escape
import json
import hashlib
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from .updates import UpdateChecker
from . import credentials
from .provenance import atomic_write, audited_call, export_manifest, json_bytes
from .app_settings import AppSettings, load_app_settings, save_app_settings
from . import extractors
from .extractors import (
    Extractor,
    ExtractorError,
    ExtractorsUnreadable,
    add_extractor,
    delete_extractor,
    duplicate_extractor,
    get_extractor,
    load_extractors,
    migrate_legacy_settings,
    other_claims,
    project_extractor,
    projects_using,
    run_settings_for,
    set_key_saved,
    setup_problem,
    update_extractor,
    updated_extractor,
    validate_new_extractor,
)
from .coding_sheet import coding_sheet_template_csv, parse_coding_sheet_csv, read_coding_sheet
from .coding_sheet_drafting import CodingSheetDraftError, read_source_csv, validate_sheet_csv
from .pdf_matching import (
    PdfScanner,
    apply_source_pdf_matches,
    cached_signal,
    load_match_score_cache,
    load_signal_cache,
    matched_paper_ids,
    save_match_score_cache,
    suggest_matches_from_score_cache,
    unmatched_paper_ids,
)
from .extraction import ProviderError, redact_secrets
from .manual import (
    ManualError,
    manual_from_editor_payload,
    manual_to_editor_payload,
    manual_to_yaml_text,
    parse_coding_manual,
)
from .manual_drafting import ManualDraftError, extract_manual_document_text
from .projects import (
    Project,
    ProjectError,
    clear_output,
    create_project,
    delete_project,
    get_project,
    list_projects,
    project_archive_files,
    read_manual_text,
    reset_manual_to_default,
    write_manual,
)
from .providers import (
    PROVIDER_LABELS, PROVIDERS, check_model, default_model, draft_coding_manual, draft_coding_sheet, list_models,
)
from .fingerprints import STALE_STATUS, InputChecker
from .runner import (
    Runner,
    attempt_json_path,
    audit_yaml_path,
    keeps_success,
    load_latest_attempts,
    load_persisted_results,
    raw_json_path_for_read,
)
from .results import CONFIDENCE_CSV, low_confidence_cells, provenance_csv, rated_fields, spreadsheet_csv
from .settings import (
    REASONING_EFFORTS, RunSettings, load_run_settings, save_project_extractor_id, save_run_settings,
)
from .uploads import ProjectError as UploadProjectError  # re-export alias, same type
from .uploads import list_uploaded_pdfs, save_pdf_upload
import yaml


# Canonical result files under output/, keyed by their download route name.
RESULT_CSV_FILES = {"coded": "coded_data.csv", "evidence": "evidence.csv", "confidence": CONFIDENCE_CSV}


def _embeddable_json(data: object) -> str:
    """JSON for embedding inside a <script type="application/json"> tag. Escaping
    every `<` (not just `</script>`) is the standard, always-safe way to prevent
    the string from ever being interpreted as breaking out of the tag."""

    return json.dumps(data).replace("<", "\\u003c")


_YAML_KEY_LINE = re.compile(r"^(\s*(?:-\s+)?)([^:#][^:]*)(:)(.*)$")


def _highlight_yaml(text: str) -> Markup:
    """Render locally generated YAML safely with lightweight, dependency-free syntax colour."""

    rendered = []
    for line in text.splitlines():
        match = _YAML_KEY_LINE.match(line)
        if not match:
            rendered.append(escape(line))
            continue
        indent, key, colon, value = match.groups()
        value_html = escape(value)
        if " #" in value_html:
            scalar, comment = value_html.split(" #", 1)
            value_html = f'<span class="yaml-scalar">{scalar}</span> <span class="yaml-comment">#{comment}</span>'
        elif value_html:
            value_html = f'<span class="yaml-scalar">{value_html}</span>'
        rendered.append(
            f'{escape(indent)}<span class="yaml-key">{escape(key)}</span>{colon}{value_html}'
        )
    return Markup("\n".join(rendered))


PAGE_SIZE = 10
RUN_SORT_COLUMNS = {"source_pdf", "authors", "year", "status", "input_tokens", "output_tokens"}

# GROBID support remains implemented for future use, but its server setting is
# not ready to expose in the regular Settings UI.
GROBID_SETTINGS_ENABLED = False


RETRYABLE_STATUSES = {"error", "needs_review", "cancelled", STALE_STATUS}
RUN_ACTIVE_DETAIL = "Cancel or wait for the active run first."


def _run_table_rows(results, coding_sheet, project: Project, kept_success=frozenset(), rated=()) -> list[dict]:
    """One row per processed article, joined with its paper-identification columns.

    ``results`` may be the in-memory progress entries during a run or persisted
    extraction results after a restart. Both represent the same result fields.
    ``kept_success`` names PDFs whose latest attempt failed while an earlier
    successful result for the same inputs is still used. ``rated`` names the
    fields whose low confidence ratings are counted (see results.rated_fields).
    """

    first_row_by_pdf = {}
    for row in coding_sheet.rows:
        first_row_by_pdf.setdefault(row.source_pdf, row)

    rows = []
    for pdf in results:
        sheet_row = first_row_by_pdf.get(pdf.source_pdf)
        raw_path = attempt_json_path(project, pdf.source_pdf)
        if not raw_path.is_file():
            raw_path = raw_json_path_for_read(project, pdf.source_pdf)
        audit_path = audit_yaml_path(project, pdf.source_pdf)
        rows.append(
            {
                "source_pdf": pdf.source_pdf,
                "authors": sheet_row.authors if sheet_row else "",
                "year": sheet_row.year if sheet_row else "",
                "status": pdf.status,
                "error": pdf.error,
                "missing_ids": pdf.missing_ids,
                "extra_ids": pdf.extra_ids,
                "input_tokens": pdf.input_tokens,
                "output_tokens": pdf.output_tokens,
                "json_repaired": getattr(pdf, "json_repaired", False)
                or getattr(pdf, "repaired_response", None) is not None,
                "raw_available": raw_path.is_file(),
                "raw_filename": raw_path.name,
                "audit_available": audit_path.is_file(),
                "audit_filename": audit_path.name,
                "retryable": pdf.status in RETRYABLE_STATUSES,
                "kept_success": pdf.source_pdf in kept_success,
                # Live progress carries the count; a persisted result is counted here.
                "low_confidence": pdf.low_confidence
                if hasattr(pdf, "low_confidence")
                else low_confidence_cells(
                    pdf.coded_by_row_id,
                    row_ids=[row.row_id for row in coding_sheet.rows_for_pdf(pdf.source_pdf)],
                    fields=list(rated),
                ),
                # Saved before input fingerprints were recorded.
                "unverified": pdf.status == "ok" and getattr(pdf, "input_fingerprint", "") is None,
            }
        )
    return rows


def _sort_run_rows(rows: list[dict], sort_key: str | None, direction: str | None) -> list[dict]:
    if sort_key not in RUN_SORT_COLUMNS:
        sort_key = "source_pdf"

    def sort_value(row: dict):
        value = row[sort_key]
        if isinstance(value, str):
            return value.lower()
        # Only the token columns are ever None (not yet processed) — sort those
        # to the low end regardless of direction, rather than crashing on a
        # None-vs-int comparison.
        return value if value is not None else -1

    return sorted(rows, key=sort_value, reverse=(direction == "desc"))


def _paginate(rows: list, page: int, *, page_size: int = PAGE_SIZE):
    total = len(rows)
    total_pages = max(1, -(-total // page_size))  # ceil division
    page = max(1, min(page or 1, total_pages))
    start = (page - 1) * page_size
    return rows[start : start + page_size], page, total_pages


def _home_view(projects: list[Project], *, q: str = "", sort: str = "newest", page: int = 1) -> dict:
    q = q.strip()
    sort = sort if sort in {"newest", "oldest", "name"} else "newest"
    matching = [project for project in projects if q.casefold() in project.name.casefold()]
    if sort == "name":
        matching.sort(key=lambda project: (project.name.casefold(), project.project_id))
    else:
        matching.sort(key=lambda project: (project.created_at, project.project_id), reverse=sort == "newest")
    page_projects, page, total_pages = _paginate(matching, page)
    pdf_counts = {project.project_id: len(list_uploaded_pdfs(project.sources_dir)) for project in page_projects}
    return {
        "projects": page_projects, "total_projects": len(projects), "matching_count": len(matching),
        "pdf_counts": pdf_counts, "q": q, "sort": sort,
        "page": page, "total_pages": total_pages,
        "first_project": (page - 1) * PAGE_SIZE + 1 if matching else 0,
        "last_project": min(page * PAGE_SIZE, len(matching)),
    }


PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Invalid Host header."}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                host_header = request.headers.get("host", "")
                if origin.rstrip("/") not in {f"http://{host_header}", f"https://{host_header}"}:
                    return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse({"detail": "Cross-site request rejected."}, status_code=403)
        return await call_next(request)


class Runtime:
    """Per-session state: the runner, the PDF scanner and the key vault.

    Saved keys are read from the credential store at most once per session,
    the first time an action needs one, never when the app starts or a page
    loads (see credentials.KeyVault)."""

    def __init__(self, projects_root: Path | None = None) -> None:
        self.projects_root = projects_root
        self.runner = Runner()
        self.pdf_scanner = PdfScanner()
        migrate_legacy_settings(projects_root=projects_root)
        self.vault = credentials.KeyVault(
            legacy_claims=extractors.legacy_claims, on_legacy_adopted=extractors.record_legacy_adoption,
        )

    def project(self, project_id: str) -> Project:
        try:
            return get_project(project_id, root=self.projects_root)
        except ProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def has_key(self, extractor: Extractor) -> bool:
        """Whether a key is saved for this extractor, without opening the credential store."""
        return extractor.has_saved_key or self.vault.cached(extractor.id) is not None

    def setup_error(self, extractor: Extractor | None, *, deleted: bool = False, purpose: str = "this project") -> str | None:
        """Why this extractor can't be used yet, without opening the credential store."""
        if extractor is None:
            if deleted:
                return f"The extractor chosen for {purpose} was deleted. Choose another."
            return f"Choose an extractor for {purpose}."
        problem = setup_problem(extractor)
        if problem:
            return problem
        if extractor.requires_key and not self.has_key(extractor):
            return f"Add an API key for the “{extractor.name}” extractor in Settings."
        return None

    def key_for(self, extractor: Extractor) -> str:
        """Blocking: may open the credential store once. Call via a threadpool from async routes.

        Raises CredentialStoreError when a saved key can't be read or has gone."""
        if not extractor.has_saved_key:
            return self.vault.cached(extractor.id) or ""
        key = self.vault.get(extractor.id)
        if key:
            return key
        if get_extractor(extractor.id) is not None and not other_claims(extractor):
            # Repair a stale marker (for example, a vault edited outside the
            # app) so the next page no longer claims a key is saved.
            set_key_saved(extractor.id, False)
        raise credentials.CredentialStoreError(
            f"The saved API key for the “{extractor.name}” extractor is no longer in the credential "
            "store. Add it again in Settings."
        )

    def prepare(self, extractor: Extractor | None, **kwargs) -> tuple[str, str | None]:
        """Blocking: the extractor's key for an explicit action, and any reason it can't run."""
        error = self.setup_error(extractor, **kwargs)
        if error:
            return "", error
        try:
            return self.key_for(extractor), None
        except credentials.CredentialStoreError as exc:
            return "", str(exc)

    def secrets(self, *extra: str) -> list[str]:
        """Every key known this session, for redacting text shown to the user."""
        return [*self.vault.secrets(), *(value for value in extra if value)]

    def redact(self, text: object, *extra: str) -> str:
        return redact_secrets(str(text), self.secrets(*extra))

    def has_any_api_key(self) -> bool:
        """For the global nav badge (base.html): is any extractor ready to use?"""
        return any(self.setup_error(item) is None for item in load_extractors())


def _extractor_view(runtime: Runtime, extractor: Extractor) -> dict[str, object]:
    """What the pages need to show an extractor. Never includes the key."""
    return {
        "id": extractor.id,
        "name": extractor.name,
        "provider": extractor.provider,
        "provider_label": extractor.provider_label,
        "model": extractor.model,
        "summary": extractor.summary(),
        "settings": extractor.snapshot(),
        "fields": extractor,
        "has_key": runtime.has_key(extractor),
        "requires_key": extractor.requires_key,
        "setup_error": runtime.setup_error(extractor),
    }


def _extractor_record(settings: RunSettings) -> dict[str, object]:
    """The extractor behind a run or draft, for provenance: id, name and a settings snapshot."""
    return {"id": settings.extractor_id, "name": settings.extractor_name, "settings": settings.extractor}


def _drafting_extractor(app_settings: AppSettings, all_extractors: list[Extractor]) -> Extractor | None:
    return next((item for item in all_extractors if item.id == app_settings.drafting_extractor_id), None)


def _project_view(
    runtime: Runtime,
    project: Project,
    *,
    run_sort: str | None = None,
    run_dir: str | None = None,
    run_page: int = 1,
    id_issues_page: int = 1,
    id_matches_page: int = 1,
    id_saved_page: int = 1,
    id_orphans_page: int = 1,
    sources_page: int = 1,
    sheet_page: int = 1,
) -> dict:
    """Everything the project page template needs, recomputed fresh on every
    render — including coding-sheet validation, which must reflect the CURRENT
    set of uploaded PDFs, not the set at the time the sheet was last saved."""

    manual_text = read_manual_text(project)
    manual = None
    manual_error = None
    try:
        # Lenient here: a blank `effect_definition` (the unedited starter manual's
        # state) must still parse so the structured editor can render it, rather
        # than falling back to the "unreadable, reset it" recovery screen. Actual
        # run-readiness is gated on `manual.is_complete` below, not on this parse
        # succeeding — see manual.py's `require_effect_definition`.
        manual = parse_coding_manual(manual_text, require_effect_definition=False)
    except ManualError as exc:
        manual_error = str(exc)

    # The manual is only ever edited through the structured GUI (see save_manual
    # below) — manual_editor_json seeds that editor's in-browser state. If the
    # on-disk file is somehow invalid (e.g. edited outside the app), there's
    # nothing valid to seed the editor with; the template falls back to a
    # "reset to default" recovery action instead of rendering the editor.
    manual_editor_json = _embeddable_json(manual_to_editor_payload(manual)) if manual else None

    uploaded = list_uploaded_pdfs(project.sources_dir)
    uploaded_names = {p.name for p in uploaded}

    # No longer depends on `manual` — the coding sheet's columns are a fixed
    # schema (see coding_sheet.py), not manual-configurable.
    coding_sheet = read_coding_sheet(project.coding_sheet_path, uploaded_filenames=uploaded_names)

    run_state = runtime.runner.state(project.project_id)
    is_running = runtime.runner.is_running(project.project_id)

    orphan_pdfs = sorted(uploaded_names - {row.source_pdf for row in coding_sheet.rows})
    app_settings = load_app_settings()
    all_extractors = load_extractors()
    chosen = project_extractor(project, all_extractors)
    selected = chosen.extractor
    # What the next run would use; without a usable extractor, the last run's
    # settings, so saved results are not reported as outdated.
    run_settings = (
        run_settings_for(selected) if selected is not None and setup_problem(selected) is None
        else load_run_settings(project)
    )
    run_setup_error = runtime.setup_error(selected, deleted=chosen.deleted)
    drafting = _drafting_extractor(app_settings, all_extractors)
    drafting_setup_error = runtime.setup_error(
        drafting, deleted=bool(app_settings.drafting_extractor_id), purpose="drafting",
    )

    # Only worth matching when there's actually an unmatched (or blank)
    # source_pdf to help with — the common case, once a project is set up, is
    # none. unmatched_paper_ids is the source of truth for "is there anything
    # to suggest a match for" (not coding_sheet.issues' message text — a row
    # can need a suggestion via title/DOI/authors+year even with no
    # source_pdf at all, which is a different issue message).
    #
    # Parsing a PDF for its match signal (full-document text extraction) is
    # too slow to do inline on every page render, so this only ever reads
    # `PdfScanner`'s on-disk cache (see pdf_matching.py). Any orphan not yet
    # in the cache — freshly uploaded, or a project from before this cache
    # existed — is handed to a background scan instead of parsed here; the
    # page renders a scanning progress bar in its place until the cache
    # catches up.
    sheet_text = (
        project.coding_sheet_path.read_text(encoding="utf-8")
        if project.coding_sheet_path.is_file()
        else ""
    )
    unmatched_papers = unmatched_paper_ids(sheet_text, uploaded_filenames=uploaded_names)
    matched_papers = matched_paper_ids(sheet_text, uploaded_filenames=uploaded_names)
    pdf_match_suggestions = []
    signal_cache = load_signal_cache(project)
    if unmatched_papers:
        orphan_paths = [p for p in uploaded if p.name in orphan_pdfs]
        unscanned = [
            p
            for p in orphan_paths
            if cached_signal(p, signal_cache, grobid_url=app_settings.grobid_url) is None
        ]
        if unscanned:
            # Fixed True here rather than re-reading `is_running` after
            # kicking off the scan: a fast scan can finish between starting
            # it and checking, which would otherwise render neither the
            # progress bar nor the (not-yet-available) suggestions panel —
            # leaving the user with nothing to look at until they refresh.
            pdf_scanning = True
            runtime.pdf_scanner.scan_async(project, unscanned, grobid_url=app_settings.grobid_url)
        else:
            pdf_scanning = False
            # Pairwise scores are cached independently, so accepting a match
            # only filters the current score matrix. Fuzzy matching runs again
            # only for a paper/PDF signal pair not seen before.
            score_cache = load_match_score_cache(project)
            pdf_match_suggestions, score_cache_changed = suggest_matches_from_score_cache(
                unmatched_papers,
                orphan_paths,
                signal_cache=signal_cache,
                grobid_url=app_settings.grobid_url,
                score_cache=score_cache,
            )
            if score_cache_changed:
                save_match_score_cache(project, score_cache)
    else:
        pdf_scanning = False

    # Each of these four lists paginates independently (distinct query
    # params) so paging one doesn't reset the others.
    id_issues_page_rows, id_issues_page, id_issues_total_pages = _paginate(
        coding_sheet.pdf_issues, id_issues_page
    )
    id_matches_page_rows, id_matches_page, id_matches_total_pages = _paginate(
        pdf_match_suggestions, id_matches_page
    )
    id_saved_page_rows, id_saved_page, id_saved_total_pages = _paginate(matched_papers, id_saved_page)
    id_orphans_page_rows, id_orphans_page, id_orphans_total_pages = _paginate(
        orphan_pdfs, id_orphans_page
    )
    sources_page_rows, sources_page, sources_total_pages = _paginate(uploaded, sources_page)
    sheet_rows, sheet_page, sheet_total_pages = _paginate(coding_sheet.rows, sheet_page)

    pdf_scan_state = runtime.pdf_scanner.state(project.project_id)
    api_key_set = run_setup_error is None
    # Saved results only count while their inputs are unchanged; see
    # fingerprints.py.
    input_checker = InputChecker(
        project, manual, coding_sheet, provider=run_settings.provider, model=run_settings.model,
        reasoning_effort=run_settings.reasoning_effort, base_url=run_settings.base_url,
        response_format=run_settings.response_format,
    )
    persisted_results = input_checker.check(load_persisted_results(project))
    completed_pdf_names = {
        source_pdf for source_pdf, result in persisted_results.items() if result.status == "ok"
    }
    matched_pdf_names = {row.source_pdf for row in coding_sheet.rows}
    pending_pdf_names = matched_pdf_names - completed_pdf_names
    first_row_by_pdf = {}
    for row in coding_sheet.rows:
        first_row_by_pdf.setdefault(row.source_pdf, row)
    completed_articles = [
        {
            "source_pdf": source_pdf,
            "authors": first_row_by_pdf.get(source_pdf).authors if source_pdf in first_row_by_pdf else "",
            "year": first_row_by_pdf.get(source_pdf).year if source_pdf in first_row_by_pdf else "",
        }
        for source_pdf in sorted(completed_pdf_names)
    ]

    can_run = bool(
        api_key_set
        and manual is not None
        and manual.is_complete
        and not coding_sheet.sheet_issues
        and coding_sheet.rows
        and pending_pdf_names
        and not is_running
    )

    coded_csv = project.output_dir / "coded_data.csv"
    evidence_csv = project.output_dir / "evidence.csv"
    audit_files = sorted(project.audit_dir.glob("*.yaml")) if project.audit_dir.is_dir() else []

    run_sort = run_sort if run_sort in RUN_SORT_COLUMNS else "source_pdf"
    run_dir = run_dir if run_dir in ("asc", "desc") else "asc"
    # The table shows each PDF's latest attempt, which can be a failed retry
    # of a PDF whose earlier success is still used.
    table_results = dict(persisted_results)
    latest_attempts = input_checker.check(load_latest_attempts(project))
    kept_success = {
        name for name, attempt in latest_attempts.items()
        if keeps_success(persisted_results.get(name), attempt)
    }
    table_results.update(latest_attempts)
    if run_state:
        # A retry's in-memory state includes only its target PDFs. Overlay it
        # on the persisted history rather than hiding earlier outcomes. After
        # the run, an input change since then takes precedence: the result
        # is outdated whatever the run reported.
        table_results.update({
            progress.source_pdf: progress for progress in run_state.pdfs
            if is_running or getattr(table_results.get(progress.source_pdf), "status", None) != STALE_STATUS
        })
    run_rows_all = _run_table_rows(table_results.values(), coding_sheet, project, kept_success, rated_fields(manual))
    run_rows_sorted = _sort_run_rows(run_rows_all, run_sort, run_dir)
    run_rows_page, run_page, run_total_pages = _paginate(run_rows_sorted, run_page)
    failed_result_count = sum(row["status"] == "error" for row in run_rows_all)
    stale_result_count = sum(result.status == STALE_STATUS for result in persisted_results.values())
    can_retry = bool(
        not is_running
        and api_key_set
        and manual is not None
        and manual.is_complete
        and not coding_sheet.sheet_issues
        and any(row["retryable"] for row in run_rows_all)
    )

    return {
        "project": project,
        "manual_text": manual_text,
        "manual": manual,
        "manual_error": manual_error,
        "manual_editor_json": manual_editor_json,
        "uploaded_pdfs": sources_page_rows,
        "uploaded_pdfs_count": len(uploaded),
        "sources_page": sources_page,
        "sources_total_pages": sources_total_pages,
        "coding_sheet": coding_sheet,
        "sheet_rows": sheet_rows,
        "sheet_page": sheet_page,
        "sheet_total_pages": sheet_total_pages,
        "orphan_pdfs": id_orphans_page_rows,
        "orphan_pdfs_count": len(orphan_pdfs),
        "id_orphans_page": id_orphans_page,
        "id_orphans_total_pages": id_orphans_total_pages,
        "pdf_issues": id_issues_page_rows,
        "pdf_issues_count": len(coding_sheet.pdf_issues),
        "id_issues_page": id_issues_page,
        "id_issues_total_pages": id_issues_total_pages,
        "pdf_match_suggestions": id_matches_page_rows,
        "pdf_match_suggestions_count": len(pdf_match_suggestions),
        "id_matches_page": id_matches_page,
        "id_matches_total_pages": id_matches_total_pages,
        "matched_papers": id_saved_page_rows,
        "matched_papers_count": len(matched_papers),
        "id_saved_page": id_saved_page,
        "id_saved_total_pages": id_saved_total_pages,
        "pdf_scanning": pdf_scanning,
        "pdf_scan_state": pdf_scan_state,
        "run_state": run_state,
        "is_running": is_running,
        "can_run": can_run,
        "matched_pdf_count": len(matched_pdf_names),
        "pending_pdf_count": len(pending_pdf_names),
        "completed_articles": completed_articles,
        "failed_result_count": failed_result_count,
        "stale_result_count": stale_result_count,
        "api_key_set": api_key_set,
        "run_setup_error": run_setup_error,
        "extractors": [_extractor_view(runtime, item) for item in all_extractors],
        "selected_extractor_id": selected.id if selected else "",
        "selected_extractor_deleted": chosen.deleted,
        "has_results": bool(run_state) or bool(completed_articles) or (coded_csv.is_file() and evidence_csv.is_file()),
        "result_files_available": coded_csv.is_file() and evidence_csv.is_file(),
        "confidence_file_available": (project.output_dir / CONFIDENCE_CSV).is_file(),
        "audit_files": audit_files,
        "run_settings": run_settings,
        "drafting_extractor": _extractor_view(runtime, drafting) if drafting else None,
        "drafting_ready": drafting_setup_error is None,
        "drafting_setup_error": drafting_setup_error,
        "run_rows": run_rows_page,
        "run_rows_all": run_rows_all,
        "run_row_count": len(run_rows_all),
        "run_sort": run_sort,
        "run_dir": run_dir,
        "run_page": run_page,
        "run_total_pages": run_total_pages,
        "can_retry": can_retry,
    }


def create_app(*, token: str, projects_root: Path | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(LocalSecurityMiddleware)
    app.mount(
        f"/{token}/docs",
        StaticFiles(directory=PACKAGE_DIR / "documentation", html=True),
        name="documentation",
    )
    runtime = Runtime(projects_root=projects_root)
    app.state.runtime = runtime
    updates = UpdateChecker()

    def _reject_if_busy(
        project_id: str, detail: str = RUN_ACTIVE_DETAIL, *, include_pdf_scan: bool = False
    ) -> None:
        """409 for a change to a project's inputs or output while a run is
        active or cancelling. Its workers keep coding from the manual and
        coding sheet they started with and write into output/ until they stop,
        so an edit now would be mixed with, or overwritten by, stale results."""

        if runtime.runner.is_running(project_id):
            raise HTTPException(status_code=409, detail=detail)
        if include_pdf_scan and runtime.pdf_scanner.is_running(project_id):
            raise HTTPException(status_code=409, detail="Wait for the PDF scan to finish first.")

    @app.get(f"/{token}/updates")
    def update_status():
        return {"version": updates.check()}

    @app.get("/", include_in_schema=False)
    async def root_without_token() -> PlainTextResponse:
        return PlainTextResponse(
            "MetaCoder is running. Use the URL printed in your terminal.", status_code=404
        )

    @app.get(f"/{token}/static/{{filename:path}}", include_in_schema=False)
    async def static_file(filename: str):
        path = (STATIC_DIR / filename).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        return FileResponse(path)

    @app.get(f"/{token}/coding-sheet-template.csv", include_in_schema=False)
    async def download_coding_sheet_template():
        return PlainTextResponse(
            coding_sheet_template_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="coding-sheet-template.csv"'},
        )

    @app.get(f"/{token}/", response_class=HTMLResponse)
    async def home(request: Request, q: str = "", sort: str = "newest", page: int = 1):
        view = _home_view(list_projects(root=runtime.projects_root), q=q, sort=sort, page=page)
        def page_url(number: int) -> str:
            return f"/{token}/?" + urlencode({"q": view["q"], "sort": view["sort"], "page": number})
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "token": token,
                **view,
                "previous_url": page_url(view["page"] - 1),
                "next_url": page_url(view["page"] + 1),
                "api_key_set": runtime.has_any_api_key(),
            },
        )

    @app.post(f"/{token}/projects")
    async def new_project(name: str = Form(...)):
        try:
            project = create_project(name, root=runtime.projects_root)
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/{token}/projects/{project.project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/delete")
    async def delete_project_route(project_id: str):
        project = runtime.project(project_id)
        # An active PDF scan would recreate the deleted folder when it saves
        # its signal cache.
        _reject_if_busy(project_id, include_pdf_scan=True)
        delete_project(project)
        return RedirectResponse(f"/{token}/", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/clear-output")
    async def clear_output_route(project_id: str):
        project = runtime.project(project_id)
        _reject_if_busy(project_id)
        clear_output(project)
        runtime.runner.discard_finished_state(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=manage", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/open-folder")
    async def open_project_folder(project_id: str):
        project = runtime.project(project_id)
        # Local-only app (LocalSecurityMiddleware pins the host to 127.0.0.1/
        # localhost, and the server itself only ever binds 127.0.0.1) — the
        # browser and this process are always on the same machine, so opening
        # a native file-manager window here is opening it for the same person
        # looking at the page, not some remote party.
        opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([opener, str(project.path)])
        except OSError as exc:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?tab=manage&error={quote(f'Could not open the folder: {exc}')}",
                status_code=303,
            )
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=manage", status_code=303)

    @app.get(f"/{token}/projects/{{project_id}}/download/zip")
    def download_project_zip(project_id: str):
        project = runtime.project(project_id)
        files = project_archive_files(project)
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = Path(handle.name)
        handle.close()
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                checksums = {}
                for abs_path, arcname in files:
                    digest = hashlib.sha256()
                    size = 0
                    with abs_path.open("rb") as source, archive.open(arcname, "w", force_zip64=True) as destination:
                        while chunk := source.read(1024 * 1024):
                            destination.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    checksums[arcname] = {"sha256": digest.hexdigest(), "bytes": size}
                settings_content = json_bytes(load_run_settings(project))
                archive.writestr("run_settings.json", settings_content)
                checksums["run_settings.json"] = {"sha256": hashlib.sha256(settings_content).hexdigest(), "bytes": len(settings_content)}
                archive.writestr("export_manifest.json", json_bytes(export_manifest(project, checksums)))
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename=f"{project.name}.zip",
            background=BackgroundTask(tmp_path.unlink, missing_ok=True),
        )

    @app.get(f"/{token}/projects/{{project_id}}", response_class=HTMLResponse)
    async def project_page(
        request: Request,
        project_id: str,
        error: str | None = None,
        warning: str | None = None,
        tab: str | None = None,
        run_sort: str | None = None,
        run_dir: str | None = None,
        run_page: int = 1,
        id_issues_page: int = 1,
        id_matches_page: int = 1,
        id_saved_page: int = 1,
        id_orphans_page: int = 1,
        sources_page: int = 1,
        sheet_page: int = 1,
    ):
        project = runtime.project(project_id)
        # `tab` lets a redirect (e.g. a Manage-tab action's error) jump the sidebar
        # straight to a specific tab; `error` with no explicit `tab` historically
        # only ever came from a redirected PDF-upload failure, so it still defaults
        # to "sources" for backward compatibility.
        context = {
            "token": token,
            "error": error,
            "warning": warning,
            "force_tab": tab or ("sources" if error else None),
            **_project_view(
                runtime,
                project,
                run_sort=run_sort,
                run_dir=run_dir,
                run_page=run_page,
                id_issues_page=id_issues_page,
                id_matches_page=id_matches_page,
                id_saved_page=id_saved_page,
                id_orphans_page=id_orphans_page,
                sources_page=sources_page,
                sheet_page=sheet_page,
            ),
        }
        return TEMPLATES.TemplateResponse(request, "project.html", context)

    @app.exception_handler(ExtractorsUnreadable)
    async def extractors_unreadable(request: Request, exc: ExtractorsUnreadable):
        # Every page and action that needs extractors stops here, so nothing
        # overwrites the file before it is fixed.
        return PlainTextResponse(str(exc), status_code=503)

    def _settings_redirect(*, error: str | None = None, notice: str | None = None, anchor: str = "") -> RedirectResponse:
        query = {key: value for key, value in (("error", error), ("notice", notice)) if value}
        return RedirectResponse(
            f"/{token}/settings" + (f"?{urlencode(query)}" if query else "") + (f"#{anchor}" if anchor else ""),
            status_code=303,
        )

    @app.get(f"/{token}/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, error: str | None = None, notice: str | None = None):
        all_extractors = await run_in_threadpool(load_extractors)
        app_settings = load_app_settings()
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "token": token,
                "api_key_set": runtime.has_any_api_key(),
                "extractors": [_extractor_view(runtime, item) for item in all_extractors],
                "providers": PROVIDERS,
                "provider_labels": PROVIDER_LABELS,
                "provider_default_models": {p: default_model(p) for p in PROVIDERS},
                "reasoning_efforts": REASONING_EFFORTS,
                "service_tiers": extractors.SERVICE_TIERS,
                "response_format_labels": extractors.RESPONSE_FORMAT_LABELS,
                "new_extractor": Extractor(id="", name="", provider="gemini", model=default_model("gemini")),
                "keyring_available": credentials.keyring_available(),
                "app_settings": app_settings,
                "drafting_extractor": _drafting_extractor(app_settings, all_extractors),
                "grobid_settings_enabled": GROBID_SETTINGS_ENABLED,
                "error": error,
                "notice": notice,
            },
        )

    def _save_key(extractor: Extractor, api_key: str) -> None:
        """Blocking: store a key for an extractor in the vault."""
        runtime.vault.set(extractor.id, api_key)
        set_key_saved(extractor.id, True)

    def _model_warning(extractor: Extractor, api_key: str) -> str | None:
        """Blocking: ask the provider whether the model works, redacting any key."""
        if extractor.requires_key and not api_key:
            return "The model will be checked when it runs."
        problems = check_model(extractor.provider, extractor.model, api_key=api_key, base_url=extractor.base_url)
        return runtime.redact("; ".join(problems), api_key) if problems else None

    def _extractor_values(form) -> dict[str, object]:
        """Extractor settings from a submitted form, without the key."""
        return {name: value for name, value in form.items() if name != "api_key"}

    def _require_store(api_key: str) -> None:
        if api_key and not credentials.keyring_available():
            raise credentials.CredentialStoreError("No operating-system credential store is available.")

    @app.post(f"/{token}/extractors")
    async def new_extractor(request: Request):
        form = await request.form()
        api_key = str(form.get("api_key") or "").strip()

        def create() -> tuple[Extractor, str | None]:
            extractor = validate_new_extractor(_extractor_values(form))
            _require_store(api_key)
            warning = _model_warning(extractor, api_key)
            add_extractor(extractor)
            if api_key:
                try:
                    _save_key(extractor, api_key)
                except credentials.CredentialStoreError as exc:
                    return extractor, f"The extractor was saved without its key. {exc}"
            return extractor, warning

        try:
            extractor, warning = await run_in_threadpool(create)
        except (ExtractorError, credentials.CredentialStoreError) as exc:
            return _settings_redirect(error=runtime.redact(exc, api_key), anchor="add-extractor")
        return _settings_redirect(
            notice=f"Saved the “{extractor.name}” extractor." + (f" {warning}" if warning else ""),
            anchor=f"extractor-{extractor.id}",
        )

    async def _existing_extractor(extractor_id: str) -> Extractor:
        extractor = await run_in_threadpool(get_extractor, extractor_id)
        if extractor is None:
            raise HTTPException(status_code=404, detail="That extractor no longer exists.")
        return extractor

    @app.post(f"/{token}/extractors/{{extractor_id}}")
    async def edit_extractor(extractor_id: str, request: Request):
        current = await _existing_extractor(extractor_id)
        form = await request.form()
        api_key = str(form.get("api_key") or "").strip()

        def edit() -> tuple[Extractor, str | None]:
            updated = updated_extractor(current, _extractor_values(form))
            _require_store(api_key)
            warning = None
            if api_key or updated.model != current.model or updated.base_url != current.base_url:
                try:
                    key = api_key or (runtime.key_for(current) if current.has_saved_key else "")
                except credentials.CredentialStoreError:
                    key = ""  # an unreadable key must not block the edit; the run checks again
                warning = _model_warning(updated, key)
            updated = update_extractor(extractor_id, _extractor_values(form))
            if api_key:
                _save_key(updated, api_key)
            return updated, warning

        try:
            updated, warning = await run_in_threadpool(edit)
        except (ExtractorError, credentials.CredentialStoreError) as exc:
            return _settings_redirect(error=runtime.redact(exc, api_key), anchor=f"extractor-{extractor_id}")
        return _settings_redirect(
            notice=f"Saved the “{updated.name}” extractor." + (f" {warning}" if warning else ""),
            anchor=f"extractor-{updated.id}",
        )

    @app.post(f"/{token}/extractors/{{extractor_id}}/duplicate")
    async def copy_extractor(extractor_id: str):
        try:
            copy = await run_in_threadpool(duplicate_extractor, extractor_id)
        except ExtractorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _settings_redirect(
            notice=f"Created “{copy.name}”. Keys are not copied: add one before running.",
            anchor=f"extractor-{copy.id}",
        )

    def _uses(extractor_id: str) -> dict[str, object]:
        return {
            "projects": projects_using(extractor_id, root=runtime.projects_root),
            "drafting": load_app_settings().drafting_extractor_id == extractor_id,
        }

    @app.get(f"/{token}/api/extractors/{{extractor_id}}/projects")
    async def extractor_projects(extractor_id: str):
        """What would be left without this extractor if it were deleted. Never opens the key vault."""
        await _existing_extractor(extractor_id)
        return await run_in_threadpool(_uses, extractor_id)

    def _remove_key(extractor: Extractor) -> None:
        """Blocking: remove an extractor's key and any old item no other extractor waits for."""
        legacy = extractor.legacy_credential if extractor.legacy_credential and not other_claims(extractor) else ""
        runtime.vault.delete(extractor.id, legacy)

    @app.post(f"/{token}/extractors/{{extractor_id}}/delete")
    async def remove_extractor(extractor_id: str):
        # Check first so a stale request returns 404 without scanning projects.
        extractor = await _existing_extractor(extractor_id)
        uses = await run_in_threadpool(_uses, extractor_id)
        # The key goes first: if it can't be removed, the extractor stays so
        # its removal can be retried from Settings.
        if runtime.has_key(extractor):
            try:
                await run_in_threadpool(_remove_key, extractor)
            except credentials.CredentialStoreError as exc:
                return _settings_redirect(
                    error=f"The extractor was not deleted because its saved key could not be removed. {exc}",
                    anchor=f"extractor-{extractor_id}",
                )
        try:
            await run_in_threadpool(delete_extractor, extractor_id)
        except ExtractorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        notice = f"Deleted the “{extractor.name}” extractor."
        if uses["projects"]:
            notice += " Choose another extractor for: " + ", ".join(uses["projects"]) + "."
        if uses["drafting"]:
            notice += " Choose another extractor for drafting."
        return _settings_redirect(notice=notice)

    @app.post(f"/{token}/extractors/{{extractor_id}}/key/delete")
    async def remove_extractor_key(extractor_id: str):
        extractor = await _existing_extractor(extractor_id)
        try:
            await run_in_threadpool(_remove_key, extractor)
        except credentials.CredentialStoreError as exc:
            return _settings_redirect(error=str(exc), anchor=f"extractor-{extractor_id}")
        await run_in_threadpool(set_key_saved, extractor.id, False)
        return _settings_redirect(notice=f"Removed the key for “{extractor.name}”.", anchor=f"extractor-{extractor_id}")

    @app.post(f"/{token}/api/models")
    async def model_suggestions(
        provider: str = Form(...), api_key: str = Form(""), base_url: str = Form(""), extractor_id: str = Form(""),
    ):
        """Model IDs for the form's provider: with the key typed into the form, or
        a saved extractor's key (which may open the credential store once). A
        typed key is sent to the provider for this request only: never stored,
        logged, or included in the response or an error message."""
        key = api_key.strip()
        if provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider.")
        if not key and extractor_id:
            extractor = await _existing_extractor(extractor_id)
            try:
                key = await run_in_threadpool(runtime.key_for, extractor)
            except credentials.CredentialStoreError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            models = await run_in_threadpool(list_models, provider, api_key=key, base_url=base_url)
        except Exception as exc:  # Provider errors vary; any of them must reach the user redacted.
            raise HTTPException(status_code=502, detail=runtime.redact(exc, key) or "Models could not be loaded.") from None
        return {"models": models}

    @app.post(f"/{token}/settings/app")
    async def save_app_settings_route(
        upload_size_cap_mb: int = Form(...),
        drafting_extractor_id: str = Form(""),
        grobid_url: str = Form(""),
    ):
        if drafting_extractor_id and await run_in_threadpool(get_extractor, drafting_extractor_id) is None:
            return _settings_redirect(error="That extractor no longer exists. Choose another.", anchor="drafting")
        current_settings = load_app_settings()
        save_app_settings(replace(
            current_settings,
            upload_size_cap_mb=upload_size_cap_mb,
            drafting_extractor_id=drafting_extractor_id,
            # A hidden field must never clear an existing advanced setting.
            grobid_url=grobid_url if GROBID_SETTINGS_ENABLED else current_settings.grobid_url,
        ))
        return _settings_redirect(notice="Saved drafting preferences.", anchor="drafting")

    @app.post(f"/{token}/projects/{{project_id}}/settings")
    async def save_project_settings(project_id: str, extractor_id: str = Form(...)):
        project = runtime.project(project_id)
        _reject_if_busy(project_id, "Wait for the active run to finish before changing its extractor.")
        if await run_in_threadpool(get_extractor, extractor_id) is None:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?tab=run&error={quote('That extractor no longer exists. Choose another.')}",
                status_code=303,
            )
        save_project_extractor_id(project, extractor_id)
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual")
    async def save_manual(request: Request, project_id: str, manual_json: str = Form(...)):
        project = runtime.project(project_id)
        try:
            payload = json.loads(manual_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Malformed manual data: {exc}") from exc
        try:
            manual = manual_from_editor_payload(payload)
        except ManualError as exc:
            # Redisplay the user's just-submitted (invalid) edit, not the last
            # successfully saved manual — losing in-progress edits on a rejected
            # save would be a bad enough experience that it's worth the extra
            # context override here.
            context = {
                "token": token,
                "error": None,
                "force_tab": "manual",
                **_project_view(runtime, project),
            }
            context["manual_error"] = str(exc)
            context["manual_editor_json"] = _embeddable_json(payload)
            return TEMPLATES.TemplateResponse(request, "project.html", context, status_code=400)
        _reject_if_busy(project_id)
        write_manual(project, manual)
        runtime.runner.discard_finished_state(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual/reset")
    async def reset_manual(project_id: str):
        project = runtime.project(project_id)
        _reject_if_busy(project_id)
        reset_manual_to_default(project)
        runtime.runner.discard_finished_state(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual/import")
    async def import_manual(project_id: str, file: UploadFile = File(...)):
        # A one-time import of an existing manual.yml — e.g. one drafted from an
        # uploaded coding-manual PDF, or reused from another project — seeds the
        # structured editor. Still never hand-edited as text after this: any
        # further change goes through save_manual like everything else.
        project = runtime.project(project_id)
        limit = load_app_settings().upload_size_cap_bytes
        raw = await file.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail="The manual exceeds the upload limit.")
        _reject_if_busy(project_id)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Save the manual as UTF-8 and try again.") from exc
        try:
            manual = parse_coding_manual(text)
        except ManualError as exc:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?error={quote(str(exc))}", status_code=303
            )
        write_manual(project, manual)
        runtime.runner.discard_finished_state(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    async def _prepare_drafting(settings: AppSettings) -> tuple[Extractor | None, str, str | None]:
        """The drafting extractor, its key and any reason it can't be used."""
        drafting = await run_in_threadpool(get_extractor, settings.drafting_extractor_id)
        key, error = await run_in_threadpool(
            lambda: runtime.prepare(drafting, deleted=bool(settings.drafting_extractor_id), purpose="drafting")
        )
        return drafting, key, error

    def _drafting_kwargs(drafting: Extractor, key: str) -> dict[str, object]:
        """Provider arguments for a draft, plus the extractor's snapshot for provenance."""
        settings = run_settings_for(drafting)
        return {
            "provider": settings.provider, "model": settings.model, "api_key": key,
            "timeout_sec": settings.request_timeout_sec, "service_tier": settings.service_tier,
            "reasoning_effort": settings.reasoning_effort, "base_url": settings.base_url,
            "response_format": settings.response_format,
            "audit_settings": {"extractor": _extractor_record(settings)},
        }

    @app.post(f"/{token}/projects/{{project_id}}/manual/draft", response_class=JSONResponse)
    async def draft_manual(project_id: str, file: UploadFile = File(...)):
        """Return an unsaved editor draft; the browser updates in place."""

        project = runtime.project(project_id)
        settings = load_app_settings()
        drafting, key, setup_error = await _prepare_drafting(settings)
        if setup_error:
            return JSONResponse({"error": setup_error}, status_code=400)

        try:
            document_text = await run_in_threadpool(
                extract_manual_document_text,
                file.file,
                file.filename or "",
                max_bytes=settings.upload_size_cap_bytes,
            )
            await file.seek(0)
            source_bytes = await file.read()
            draft = await run_in_threadpool(
                audited_call, project, "manual_draft", draft_coding_manual,
                audit_inputs={"uploaded/" + Path(file.filename or "manual").name: source_bytes},
                document_text=document_text,
                **_drafting_kwargs(drafting, key),
            )
        except ManualDraftError as exc:
            return JSONResponse({"error": runtime.redact(exc, key)}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": runtime.redact(exc, key)}, status_code=502)

        return JSONResponse(
            {
                "manual": manual_to_editor_payload(draft),
                "yaml": manual_to_yaml_text(draft),
                "filename": Path(file.filename or "manual document").name,
                "provider": drafting.provider,
                "model": drafting.model,
                "extractor": drafting.name,
            }
        )

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet")
    async def upload_coding_sheet(project_id: str, file: UploadFile = File(...)):
        project = runtime.project(project_id)
        limit = load_app_settings().upload_size_cap_bytes
        raw = await file.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail="The coding sheet exceeds the upload limit.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Save the CSV as UTF-8 and try again.") from exc
        sheet = parse_coding_sheet_csv(text, uploaded_filenames={p.name for p in list_uploaded_pdfs(project.sources_dir)})
        if sheet.sheet_issues:
            raise HTTPException(status_code=400, detail="; ".join(issue.message for issue in sheet.sheet_issues))
        _reject_if_busy(project_id, "Wait for the active run to finish before replacing the sheet.")
        previous = project.coding_sheet_path.read_text(encoding="utf-8") if project.coding_sheet_path.exists() else ""
        if text != previous:
            atomic_write(project.coding_sheet_path, text.encode("utf-8"))
            clear_output(project)
            runtime.runner.discard_finished_state(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/draft", response_class=JSONResponse)
    async def draft_sheet(project_id: str, file: UploadFile = File(...), notes: str = Form("")):
        project = runtime.project(project_id)
        settings = load_app_settings()
        drafting, key, setup_error = await _prepare_drafting(settings)
        if setup_error:
            return JSONResponse({"error": setup_error}, status_code=400)
        try:
            manual = parse_coding_manual(read_manual_text(project))
            source = await run_in_threadpool(
                read_source_csv, file.file, file.filename or "",
                max_bytes=settings.upload_size_cap_bytes,
            )
            await file.seek(0)
            source_bytes = await file.read()
            draft = await run_in_threadpool(
                audited_call, project, "sheet_conversion", draft_coding_sheet, source=source,
                audit_inputs={"uploaded/" + Path(file.filename or "sheet.csv").name: source_bytes},
                manual=manual_to_yaml_text(manual), notes=notes,
                filenames=[path.name for path in list_uploaded_pdfs(project.sources_dir)],
                **_drafting_kwargs(drafting, key),
            )
        except (CodingSheetDraftError, ManualError) as exc:
            return JSONResponse({"error": runtime.redact(exc, key)}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": runtime.redact(exc, key)}, status_code=502)
        return JSONResponse(draft)

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/draft/save", response_class=JSONResponse)
    async def save_sheet_draft(project_id: str, csv_text: str = Form(...)):
        project = runtime.project(project_id)
        if runtime.runner.is_running(project_id):
            return JSONResponse({"error": "Wait for the active run to finish before replacing the sheet."}, status_code=409)
        try:
            validate_sheet_csv(csv_text, require_complete=True)
        except CodingSheetDraftError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        previous = project.coding_sheet_path.read_text(encoding="utf-8") if project.coding_sheet_path.exists() else ""
        if csv_text != previous:
            project.coding_sheet_path.write_text(csv_text, encoding="utf-8")
            clear_output(project)
            runtime.runner.discard_finished_state(project_id)
        return JSONResponse({"redirect": f"/{token}/projects/{project_id}?tab=coding-sheet"})

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/match-pdfs")
    async def match_coding_sheet_pdfs(request: Request, project_id: str):
        project = runtime.project(project_id)
        form = await request.form()
        _reject_if_busy(project_id)
        paper_keys = form.getlist("paper_key")
        filenames = form.getlist("filename")
        uploaded_filenames = {path.name for path in list_uploaded_pdfs(project.sources_dir)}
        mapping = {
            paper_key: filename
            for paper_key, filename in zip(paper_keys, filenames)
            if paper_key and filename in uploaded_filenames
        }
        save_key = form.get("save_key")
        if save_key:
            mapping = {save_key: mapping[save_key]} if save_key in mapping else {}
        if mapping and project.coding_sheet_path.is_file():
            apply_source_pdf_matches(project.coding_sheet_path, mapping)
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=identify", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/unmatch-pdf")
    async def unmatch_coding_sheet_pdf(project_id: str, source_pdf: str = Form(...)):
        project = runtime.project(project_id)
        _reject_if_busy(project_id)
        uploaded_filenames = {path.name for path in list_uploaded_pdfs(project.sources_dir)}
        if source_pdf in uploaded_filenames and project.coding_sheet_path.is_file():
            apply_source_pdf_matches(project.coding_sheet_path, {source_pdf: ""})
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=identify", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/uploads")
    async def upload_pdfs(project_id: str, files: list[UploadFile] = File(...)):
        project = runtime.project(project_id)
        app_settings = load_app_settings()
        errors = []
        saved: list[Path] = []
        for upload in files:
            try:
                saved.append(
                    save_pdf_upload(
                        project.sources_dir,
                        upload.filename or "",
                        upload.file,
                        max_bytes=app_settings.upload_size_cap_bytes,
                    )
                )
            except UploadProjectError as exc:
                errors.append(str(exc))
        if saved:
            runtime.pdf_scanner.scan_async(project, saved, grobid_url=app_settings.grobid_url)
        if errors:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?error={quote('; '.join(errors))}", status_code=303
            )
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/uploads/delete")
    async def delete_pdf(project_id: str, filename: str = Form(...)):
        project = runtime.project(project_id)
        _reject_if_busy(project_id)
        safe = Path(filename).name
        target = project.sources_dir / safe
        if target.is_file() and target.suffix.lower() == ".pdf":
            target.unlink()
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    def _runnable_view(project: Project) -> dict:
        """A fresh view of the project's inputs, validated for a full run."""

        _reject_if_busy(project.project_id, "A run is already in progress.")
        view = _project_view(runtime, project)
        if not view["can_run"]:
            reasons = []
            if view["run_setup_error"]:
                reasons.append(view["run_setup_error"].rstrip("."))
            if view["manual_error"]:
                reasons.append("the coding manual has a validation error")
            if view["coding_sheet"].sheet_issues:
                reasons.append("the coding sheet has validation errors")
            elif not view["coding_sheet"].rows:
                reasons.append("the coding sheet has no matched rows")
            elif not view["pending_pdf_count"]:
                reasons.append("all matched PDFs were already coded")
            raise HTTPException(status_code=400, detail="Cannot run: " + "; ".join(reasons) + ".")
        return view

    def _retryable_view(project: Project, source_pdf: list[str]) -> tuple[dict, list[str]]:
        """A fresh view of the project's inputs plus the PDFs a retry targets."""

        _reject_if_busy(project.project_id, "A run is already in progress.")
        view = _project_view(runtime, project)
        if view["run_setup_error"]:
            raise HTTPException(status_code=400, detail=view["run_setup_error"])
        if view["manual_error"] or view["manual"] is None or not view["manual"].is_complete:
            raise HTTPException(status_code=400, detail="Fix the coding manual before retrying.")
        if view["coding_sheet"].sheet_issues:
            raise HTTPException(status_code=400, detail="Fix the coding sheet before retrying.")

        if source_pdf:
            targets = source_pdf
        else:
            eligible = {row.source_pdf for row in view["coding_sheet"].rows}
            targets = [row["source_pdf"] for row in view["run_rows_all"]
                       if row["retryable"] and row["source_pdf"] in eligible]

        if not targets:
            raise HTTPException(status_code=400, detail="Nothing to retry.")
        return view, targets

    async def _start_run(project: Project, validate) -> None:
        """Validate, load the extractor's key, then validate again and start.

        Loading a saved key runs in a worker thread, so other requests — a
        second click on Run, or an edit to the manual, coding sheet or
        extractor — can be handled while this one waits. Re-reading and
        re-validating the inputs afterwards, with no further await before
        `runner.start`, means the run either starts from the inputs as they
        are now or is rejected; it never codes a snapshot taken before an
        edit, and a request that lost the race gets a 409 instead of the
        runner's "already running" error."""

        view, only_pdfs = validate()
        settings = view["run_settings"]
        extractor = get_extractor(settings.extractor_id)
        key, setup_error = await run_in_threadpool(runtime.prepare, extractor)
        if setup_error:
            raise HTTPException(status_code=400, detail=setup_error)
        view, only_pdfs = validate()
        if view["run_settings"] != settings:
            raise HTTPException(status_code=409, detail="The extractor changed while the run was starting. Try again.")
        try:
            runtime.runner.start(
                project=project,
                manual=view["manual"],
                coding_sheet=view["coding_sheet"],
                api_key=key,
                provider=settings.provider,
                model=settings.model,
                parallel_requests=settings.parallel_requests,
                request_delay_sec=settings.request_delay_sec,
                request_timeout_sec=settings.request_timeout_sec,
                service_tier=settings.service_tier,
                reasoning_effort=settings.reasoning_effort,
                base_url=settings.base_url,
                response_format=settings.response_format,
                extractor=_extractor_record(settings),
                only_pdfs=only_pdfs,
            )
        except RuntimeError as exc:
            # The runner's own guards: already running, or nothing to code.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The last run's settings, with a snapshot of the extractor it used.
        save_run_settings(project, settings)

    @app.post(f"/{token}/projects/{{project_id}}/run")
    async def start_run(project_id: str):
        project = runtime.project(project_id)
        await _start_run(project, lambda: (_runnable_view(project), None))
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/run/cancel")
    async def cancel_run(project_id: str):
        runtime.project(project_id)
        runtime.runner.cancel(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/run/retry")
    async def retry_run(project_id: str, source_pdf: list[str] = Form([])):
        project = runtime.project(project_id)
        await _start_run(project, lambda: _retryable_view(project, source_pdf))
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.get(f"/{token}/projects/{{project_id}}/status")
    async def run_status(project_id: str):
        runtime.project(project_id)
        state = runtime.runner.state(project_id)
        if state is None:
            return {"status": "idle"}
        return state.snapshot()

    @app.get(f"/{token}/projects/{{project_id}}/pdf-scan/status")
    async def pdf_scan_status(project_id: str):
        runtime.project(project_id)
        state = runtime.pdf_scanner.state(project_id)
        if state is None:
            return {"status": "idle", "total": 0, "processed": 0}
        return state.snapshot()

    @app.get(f"/{token}/projects/{{project_id}}/raw/{{filename}}")
    async def view_raw_response(request: Request, project_id: str, filename: str):
        project = runtime.project(project_id)
        safe = Path(filename).name
        path = (project.raw_dir / safe).resolve()
        try:
            path.relative_to(project.raw_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
        except (OSError, json.JSONDecodeError, yaml.YAMLError):
            raise HTTPException(status_code=422, detail="The stored raw response is unreadable.")
        return TEMPLATES.TemplateResponse(
            request,
            "raw_response.html",
            {"token": token, "project": project, "raw_yaml": _highlight_yaml(yaml_text)},
        )

    @app.get(f"/{token}/projects/{{project_id}}/download/coded")
    async def download_coded(project_id: str):
        project = runtime.project(project_id)
        path = project.output_dir / "coded_data.csv"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No results yet.")
        return FileResponse(path, media_type="text/csv", filename=f"{project.name}-coded_data.csv")

    @app.get(f"/{token}/projects/{{project_id}}/download/evidence")
    async def download_evidence(project_id: str):
        project = runtime.project(project_id)
        path = project.output_dir / "evidence.csv"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No results yet.")
        return FileResponse(path, media_type="text/csv", filename=f"{project.name}-evidence.csv")

    @app.get(f"/{token}/projects/{{project_id}}/download/confidence")
    async def download_confidence(project_id: str):
        project = runtime.project(project_id)
        path = project.output_dir / CONFIDENCE_CSV
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No confidence ratings yet.")
        return FileResponse(path, media_type="text/csv", filename=f"{project.name}-confidence.csv")

    def _csv_attachment(data: bytes, filename: str) -> Response:
        # Same quoting as FileResponse: RFC 6266 filename*= for names that need escaping.
        quoted = quote(filename)
        disposition = f'attachment; filename="{filename}"' if quoted == filename else f"attachment; filename*=utf-8''{quoted}"
        return Response(data, media_type="text/csv", headers={"Content-Disposition": disposition})

    @app.get(f"/{token}/projects/{{project_id}}/download/spreadsheet/{{kind}}")
    async def download_spreadsheet(project_id: str, kind: str):
        project = runtime.project(project_id)
        filename = RESULT_CSV_FILES.get(kind)
        if filename is None:
            raise HTTPException(status_code=404, detail="Unknown export.")
        path = project.output_dir / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No results yet.")
        data = spreadsheet_csv((await run_in_threadpool(path.read_bytes)).decode("utf-8"))
        return _csv_attachment(data, f"{project.name}-{Path(filename).stem}-spreadsheet.csv")

    @app.get(f"/{token}/projects/{{project_id}}/download/provenance")
    async def download_provenance(project_id: str):
        project = runtime.project(project_id)
        path = project.output_dir / RESULT_CSV_FILES["coded"]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No results yet.")
        coded_csv = (await run_in_threadpool(path.read_bytes)).decode("utf-8")
        results = await run_in_threadpool(load_persisted_results, project)
        return _csv_attachment(provenance_csv(coded_csv, results).encode("utf-8"), f"{project.name}-provenance.csv")

    @app.get(f"/{token}/projects/{{project_id}}/audit/{{filename}}")
    async def view_audit(project_id: str, filename: str):
        project = runtime.project(project_id)
        safe = Path(filename).name
        path = (project.audit_dir / safe).resolve()
        try:
            path.relative_to(project.audit_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        # text/plain (not a YAML MIME type) so it renders inline in the browser for
        # quick auditing, rather than forcing a download.
        return FileResponse(path, media_type="text/plain")

    return app


def launch_web(*, open_browser: bool = True, port: int | None = None) -> int:
    token = secrets.token_urlsafe(32)
    if port is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
    app = create_app(token=token)
    url = f"http://127.0.0.1:{port}/{token}/"
    print(f"MetaCoder is running locally at {url}")
    print("Press Ctrl+C to stop it.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    return 0
