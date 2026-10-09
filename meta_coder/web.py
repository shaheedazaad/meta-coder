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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from .updates import UpdateChecker
from . import credentials
from .provenance import audited_call, export_manifest, json_bytes
from .app_settings import AppSettings, endpoint_options, load_app_settings, save_app_settings
from .openai_compatible import normalize_base_url
from .coding_sheet import coding_sheet_template_csv, read_coding_sheet
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
from .extraction import ProviderError, redact_secret
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
from .providers import PROVIDER_LABELS, PROVIDERS, check_model, default_model, draft_coding_manual, draft_coding_sheet
from .runner import Runner, load_persisted_results, raw_json_path_for_read
from .settings import REASONING_EFFORTS, RunSettings, load_run_settings, save_run_settings
from .uploads import ProjectError as UploadProjectError  # re-export alias, same type
from .uploads import list_uploaded_pdfs, save_pdf_upload
import yaml


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


RETRYABLE_STATUSES = {"error", "needs_review", "cancelled"}


def _run_table_rows(results, coding_sheet, project: Project) -> list[dict]:
    """One row per processed article, joined with its paper-identification columns.

    ``results`` may be the in-memory progress entries during a run or persisted
    extraction results after a restart. Both represent the same result fields.
    """

    first_row_by_pdf = {}
    for row in coding_sheet.rows:
        first_row_by_pdf.setdefault(row.source_pdf, row)

    rows = []
    for pdf in results:
        sheet_row = first_row_by_pdf.get(pdf.source_pdf)
        raw_path = raw_json_path_for_read(project, pdf.source_pdf)
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
                "retryable": pdf.status in RETRYABLE_STATUSES,
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
    """One API key slot per provider (todo.md step 9: a second provider means a
    second key). Every accepted key is persisted in the OS credential store;
    saved keys are loaded only in response to a user action that needs them."""

    def __init__(self, projects_root: Path | None = None) -> None:
        self.projects_root = projects_root
        self.runner = Runner()
        self.pdf_scanner = PdfScanner()
        self._session_keys: dict[str, str] = {}
        self._saved_keys: set[str] = {
            provider for provider in PROVIDERS if credentials.saved_key_configured(provider)
        }

    def project(self, project_id: str) -> Project:
        return get_project(project_id, root=self.projects_root)

    def api_key(self, provider: str) -> str | None:
        return self._session_keys.get(provider)

    def provider_setup_error(self, provider: str, model: str | None = None) -> str | None:
        label = PROVIDER_LABELS.get(provider, provider)
        if provider == "openai_compatible":
            try:
                normalize_base_url(load_app_settings().openai_base_url)
            except ValueError:
                return "Set a valid OpenAI-compatible API base URL in global Settings."
        if model is not None and not model.strip():
            return f"Enter a model ID for {label}."
        if not self.api_key(provider) and not self.has_saved_key(provider):
            if provider != "openai_compatible":
                return f"Save a {label} API key in global Settings."
        return None

    def provider_ready(self, provider: str) -> bool:
        return self.provider_setup_error(provider) is None

    def has_saved_key(self, provider: str) -> bool:
        return provider in self._saved_keys

    def prepare_provider(self, provider: str, model: str) -> str | None:
        """Load saved keys for an explicit provider action, never for page rendering."""
        error = self.provider_setup_error(provider, model)
        if error:
            return error
        try:
            for saved_provider in PROVIDERS:
                if self.has_saved_key(saved_provider) and not self.api_key(saved_provider):
                    found = self.unlock_key(saved_provider)
                    if saved_provider == provider and not found:
                        return "The saved API key is no longer in the credential store. Save it again in Settings."
        except credentials.CredentialStoreError as exc:
            return str(exc)
        return self.provider_setup_error(provider, model)

    def unlock_key(self, provider: str) -> bool:
        key = credentials.load_key(provider)
        if not key:
            # Repair a stale UI marker (for example, a key deleted directly in
            # Keychain Access) so the next page no longer claims it is saved.
            credentials.mark_saved_key_configured(provider, False)
            self._saved_keys.discard(provider)
            return False
        self._session_keys[provider] = key
        credentials.mark_saved_key_configured(provider, True)
        self._saved_keys.add(provider)
        return True

    def set_api_key(self, provider: str, key: str) -> None:
        # Persist first. A key that cannot survive restart is never
        # silently accepted as session-only.
        credentials.save_key(provider, key)
        self._session_keys[provider] = key
        self._saved_keys.add(provider)

    def clear_api_key(self, provider: str) -> None:
        credentials.clear_key(provider)
        self._session_keys.pop(provider, None)
        self._saved_keys.discard(provider)

    def has_any_api_key(self) -> bool:
        """For the global nav badge (base.html), which just needs "is there
        something set" — per-project/per-provider detail lives on the pages
        that actually care (the project's Run tab, the Settings page)."""
        return bool(self._session_keys or self._saved_keys)


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
    run_settings = load_run_settings(project)
    app_settings = load_app_settings()
    generator_provider = app_settings.manual_generator_provider

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
    api_key_set = runtime.provider_ready(run_settings.provider)
    saved_key_available = runtime.has_saved_key(run_settings.provider)
    provider_key_status = {
        provider: (
            "unconfigured"
            if provider == "openai_compatible" and not load_app_settings().openai_base_url
            else "optional"
            if provider == "openai_compatible" and runtime.provider_ready(provider) and not (runtime.api_key(provider) or runtime.has_saved_key(provider))
            else "saved"
            if runtime.api_key(provider) or runtime.has_saved_key(provider)
            else "missing"
        )
        for provider in PROVIDERS
    }
    keyring_available = credentials.keyring_available()

    persisted_results = load_persisted_results(project)
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
        and run_settings.model.strip()
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
    table_results = dict(persisted_results)
    if run_state:
        # A retry's in-memory state includes only its target PDFs. Overlay it
        # on the persisted history rather than hiding earlier outcomes.
        table_results.update({progress.source_pdf: progress for progress in run_state.pdfs})
    run_rows_all = _run_table_rows(table_results.values(), coding_sheet, project)
    run_rows_sorted = _sort_run_rows(run_rows_all, run_sort, run_dir)
    run_rows_page, run_page, run_total_pages = _paginate(run_rows_sorted, run_page)
    failed_result_count = sum(row["status"] == "error" for row in run_rows_all)
    can_retry = bool(
        run_state
        and not is_running
        and api_key_set
        and run_settings.model.strip()
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
        "api_key_set": api_key_set,
        "run_provider_setup_error": runtime.provider_setup_error(run_settings.provider),
        "saved_key_available": saved_key_available,
        "provider_key_status": provider_key_status,
        "provider_default_models": {provider: default_model(provider) for provider in PROVIDERS},
        "keyring_available": keyring_available,
        "providers": PROVIDERS,
        "provider_labels": PROVIDER_LABELS,
        "reasoning_efforts": REASONING_EFFORTS,
        "has_results": bool(run_state) or bool(completed_articles) or (coded_csv.is_file() and evidence_csv.is_file()),
        "result_files_available": coded_csv.is_file() and evidence_csv.is_file(),
        "audit_files": audit_files,
        "run_settings": run_settings,
        "manual_generator_provider": generator_provider,
        "manual_generator_model": app_settings.manual_generator_model,
        "manual_generator_ready": runtime.provider_setup_error(generator_provider, app_settings.manual_generator_model) is None,
        "manual_generator_setup_error": runtime.provider_setup_error(generator_provider, app_settings.manual_generator_model),
        "manual_generator_saved_key_available": runtime.has_saved_key(generator_provider),
        "run_rows": run_rows_page,
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
        try:
            project = runtime.project(project_id)
        except ProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if runtime.runner.is_running(project_id):
            raise HTTPException(status_code=409, detail="Cancel or wait for the active run first.")
        delete_project(project)
        return RedirectResponse(f"/{token}/", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/clear-output")
    async def clear_output_route(project_id: str):
        project = runtime.project(project_id)
        if runtime.runner.is_running(project_id):
            raise HTTPException(status_code=409, detail="Cancel or wait for the active run first.")
        clear_output(project)
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
    async def download_project_zip(project_id: str):
        project = runtime.project(project_id)
        files = project_archive_files(project)
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = Path(handle.name)
        handle.close()
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
        try:
            project = runtime.project(project_id)
        except ProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
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

    @app.get(f"/{token}/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "token": token,
                "api_key_set": runtime.has_any_api_key(),
                "providers": PROVIDERS,
                "provider_labels": PROVIDER_LABELS,
                "keys": {p: bool(runtime.api_key(p)) for p in PROVIDERS},
                "saved_keys": {p: runtime.has_saved_key(p) for p in PROVIDERS},
                "keyring_available": credentials.keyring_available(),
                "app_settings": load_app_settings(),
                "provider_default_models": {p: default_model(p) for p in PROVIDERS},
                "grobid_settings_enabled": GROBID_SETTINGS_ENABLED,
                "error": error,
            },
        )

    @app.post(f"/{token}/settings/api-key")
    async def set_api_key(provider: str = Form(...), api_key: str = Form(...)):
        if provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider.")
        key = api_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="Enter an API key.")
        try:
            runtime.set_api_key(provider, key)
        except credentials.CredentialStoreError as exc:
            return RedirectResponse(f"/{token}/settings?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/{token}/settings", status_code=303)

    @app.post(f"/{token}/settings/api-key/clear")
    async def clear_api_key(provider: str = Form(...)):
        if provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider.")
        try:
            runtime.clear_api_key(provider)
        except credentials.CredentialStoreError as exc:
            return RedirectResponse(f"/{token}/settings?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/{token}/settings", status_code=303)

    @app.post(f"/{token}/settings/openai-compatible")
    async def save_endpoint_settings(
        openai_base_url: str = Form(""),
        openai_response_format: str = Form("json_schema"),
        api_key: str = Form(""),
    ):
        try:
            endpoint_url = normalize_base_url(openai_base_url) if openai_base_url.strip() else ""
            if openai_response_format not in {"json_schema", "json_object", "none"}:
                raise ValueError("Unknown JSON output mode.")
        except ValueError as exc:
            return RedirectResponse(f"/{token}/settings?error={quote(str(exc))}", status_code=303)
        if api_key.strip():
            try:
                runtime.set_api_key("openai_compatible", api_key.strip())
            except credentials.CredentialStoreError as exc:
                return RedirectResponse(f"/{token}/settings?error={quote(str(exc))}", status_code=303)
        save_app_settings(replace(
            load_app_settings(),
            openai_base_url=endpoint_url,
            openai_response_format=openai_response_format,
        ))
        return RedirectResponse(f"/{token}/settings", status_code=303)

    @app.post(f"/{token}/settings/app")
    async def save_app_settings_route(
        upload_size_cap_mb: int = Form(...),
        manual_generator_provider: str = Form(...),
        manual_generator_model: str = Form(""),
        grobid_url: str = Form(""),
    ):
        current_settings = load_app_settings()
        save_app_settings(replace(
            current_settings,
            upload_size_cap_mb=upload_size_cap_mb,
            manual_generator_provider=manual_generator_provider,
            manual_generator_model=manual_generator_model,
            # A hidden field must never clear an existing advanced setting.
            grobid_url=grobid_url if GROBID_SETTINGS_ENABLED else current_settings.grobid_url,
        ))
        return RedirectResponse(f"/{token}/settings", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/settings")
    async def save_project_settings(
        project_id: str,
        provider: str = Form(...),
        previous_provider: str = Form(""),
        model: str = Form(""),
        parallel_requests: int = Form(1),
        request_delay_sec: int = Form(0),
        request_timeout_sec: int = Form(0),
        service_tier: str = Form(""),
        reasoning_effort: str = Form(""),
    ):
        project = runtime.project(project_id)
        settings = RunSettings(
            provider=provider,
            model=model if provider == "openai_compatible" or provider == previous_provider else "",
            parallel_requests=parallel_requests,
            request_delay_sec=request_delay_sec,
            request_timeout_sec=request_timeout_sec,
            service_tier=service_tier,
            reasoning_effort=reasoning_effort,
        ).clamped()
        setup_error = await run_in_threadpool(runtime.prepare_provider, settings.provider, settings.model)
        problems = [setup_error] if setup_error else check_model(
            settings.provider, settings.model, api_key=runtime.api_key(settings.provider) or "",
            **({"base_url": load_app_settings().openai_base_url} if settings.provider == "openai_compatible" else {})
        )
        save_run_settings(project, settings)
        if problems:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?tab=run&warning={quote('; '.join(problems))}",
                status_code=303,
            )
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
        write_manual(project, manual)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual/reset")
    async def reset_manual(project_id: str):
        project = runtime.project(project_id)
        reset_manual_to_default(project)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual/import")
    async def import_manual(project_id: str, file: UploadFile = File(...)):
        # A one-time import of an existing manual.yml — e.g. one drafted from an
        # uploaded coding-manual PDF, or reused from another project — seeds the
        # structured editor. Still never hand-edited as text after this: any
        # further change goes through save_manual like everything else.
        project = runtime.project(project_id)
        raw = await file.read()
        try:
            manual = parse_coding_manual(raw.decode("utf-8", errors="replace"))
        except ManualError as exc:
            return RedirectResponse(
                f"/{token}/projects/{project_id}?error={quote(str(exc))}", status_code=303
            )
        write_manual(project, manual)
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/manual/draft", response_class=JSONResponse)
    async def draft_manual(project_id: str, file: UploadFile = File(...)):
        """Return an unsaved editor draft; the browser updates in place."""

        project = runtime.project(project_id)
        settings = load_app_settings()
        provider = settings.manual_generator_provider
        setup_error = await run_in_threadpool(runtime.prepare_provider, provider, settings.manual_generator_model)
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
                provider=provider,
                document_text=document_text,
                api_key=runtime.api_key(provider) or "",
                model=settings.manual_generator_model,
                **endpoint_options(provider, settings),
            )
        except ManualDraftError as exc:
            return JSONResponse({"error": redact_secret(str(exc), runtime.api_key(provider) or "")}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": redact_secret(str(exc), runtime.api_key(provider) or "")}, status_code=502)

        return JSONResponse(
            {
                "manual": manual_to_editor_payload(draft),
                "yaml": manual_to_yaml_text(draft),
                "filename": Path(file.filename or "manual document").name,
                "provider": provider,
                "model": settings.manual_generator_model,
            }
        )

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet")
    async def upload_coding_sheet(project_id: str, file: UploadFile = File(...)):
        project = runtime.project(project_id)
        raw = await file.read()
        project.coding_sheet_path.write_text(raw.decode("utf-8", errors="replace"), encoding="utf-8")
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/draft", response_class=JSONResponse)
    async def draft_sheet(project_id: str, file: UploadFile = File(...), notes: str = Form("")):
        project = runtime.project(project_id)
        settings = load_app_settings()
        provider = settings.manual_generator_provider
        setup_error = await run_in_threadpool(runtime.prepare_provider, provider, settings.manual_generator_model)
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
                audited_call, project, "sheet_conversion", draft_coding_sheet, provider=provider, source=source,
                audit_inputs={"uploaded/" + Path(file.filename or "sheet.csv").name: source_bytes},
                manual=manual_to_yaml_text(manual), notes=notes,
                filenames=[path.name for path in list_uploaded_pdfs(project.sources_dir)],
                api_key=runtime.api_key(provider) or "", model=settings.manual_generator_model,
                **endpoint_options(provider, settings),
            )
        except (CodingSheetDraftError, ManualError) as exc:
            return JSONResponse({"error": redact_secret(str(exc), runtime.api_key(provider) or "")}, status_code=400)
        except ProviderError as exc:
            return JSONResponse({"error": redact_secret(str(exc), runtime.api_key(provider) or "")}, status_code=502)
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
        return JSONResponse({"redirect": f"/{token}/projects/{project_id}?tab=coding-sheet"})

    @app.post(f"/{token}/projects/{{project_id}}/coding-sheet/match-pdfs")
    async def match_coding_sheet_pdfs(request: Request, project_id: str):
        project = runtime.project(project_id)
        form = await request.form()
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
        safe = Path(filename).name
        target = project.sources_dir / safe
        if target.is_file() and target.suffix.lower() == ".pdf":
            target.unlink()
        return RedirectResponse(f"/{token}/projects/{project_id}", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/run")
    async def start_run(project_id: str):
        project = runtime.project(project_id)
        view = _project_view(runtime, project)
        settings = view["run_settings"]
        if not view["can_run"]:
            reasons = []
            if settings.provider == "openai_compatible" and (not runtime.provider_ready(settings.provider) or not settings.model.strip()):
                reasons.append("configure the OpenAI-compatible base URL in Settings and enter a model")
            elif not runtime.provider_ready(settings.provider):
                reasons.append(
                    f"a {PROVIDER_LABELS.get(settings.provider, settings.provider)} API key "
                    "is required (set it in Settings)"
                )
            if view["manual_error"]:
                reasons.append("the coding manual has a validation error")
            if view["coding_sheet"].sheet_issues:
                reasons.append("the coding sheet has validation errors")
            elif not view["coding_sheet"].rows:
                reasons.append("the coding sheet has no matched rows")
            elif not view["pending_pdf_count"]:
                reasons.append("all matched PDFs were already coded")
            if runtime.runner.is_running(project_id):
                reasons.append("a run is already in progress")
            raise HTTPException(status_code=400, detail="Cannot run: " + "; ".join(reasons) + ".")

        setup_error = await run_in_threadpool(runtime.prepare_provider, settings.provider, settings.model)
        if setup_error:
            raise HTTPException(status_code=400, detail=setup_error)
        runtime.runner.start(
            project=project,
            manual=view["manual"],
            coding_sheet=view["coding_sheet"],
            api_key=runtime.api_key(settings.provider) or "",
            **endpoint_options(settings.provider),
            provider=settings.provider,
            model=settings.model,
            parallel_requests=settings.parallel_requests,
            request_delay_sec=settings.request_delay_sec,
            request_timeout_sec=settings.request_timeout_sec,
            service_tier=settings.service_tier,
            reasoning_effort=settings.reasoning_effort,
        )
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/run/cancel")
    async def cancel_run(project_id: str):
        runtime.project(project_id)
        runtime.runner.cancel(project_id)
        return RedirectResponse(f"/{token}/projects/{project_id}?tab=run", status_code=303)

    @app.post(f"/{token}/projects/{{project_id}}/run/retry")
    async def retry_run(project_id: str, source_pdf: list[str] = Form([])):
        project = runtime.project(project_id)
        view = _project_view(runtime, project)
        settings = view["run_settings"]
        if runtime.runner.is_running(project_id):
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        if settings.provider == "openai_compatible" and (not runtime.provider_ready(settings.provider) or not settings.model.strip()):
            raise HTTPException(status_code=400, detail="Configure the OpenAI-compatible base URL and model.")
        if not runtime.provider_ready(settings.provider):
            raise HTTPException(
                status_code=400,
                detail=f"A {PROVIDER_LABELS.get(settings.provider, settings.provider)} API key "
                "is required (set it in Settings).",
            )
        if view["manual_error"] or view["manual"] is None or not view["manual"].is_complete:
            raise HTTPException(status_code=400, detail="Fix the coding manual before retrying.")
        if view["coding_sheet"].sheet_issues:
            raise HTTPException(status_code=400, detail="Fix the coding sheet before retrying.")

        if source_pdf:
            targets = source_pdf
        else:
            # A bare "retry all failed" click means every failed/needs_review/
            # cancelled PDF from the last run, not just whichever page of the
            # (paginated) run table happens to be showing.
            prior_state = runtime.runner.state(project_id)
            targets = (
                [pdf.source_pdf for pdf in prior_state.pdfs if pdf.status in RETRYABLE_STATUSES]
                if prior_state
                else []
            )
        if not targets:
            raise HTTPException(status_code=400, detail="Nothing to retry.")

        setup_error = await run_in_threadpool(runtime.prepare_provider, settings.provider, settings.model)
        if setup_error:
            raise HTTPException(status_code=400, detail=setup_error)
        runtime.runner.start(
            project=project,
            manual=view["manual"],
            coding_sheet=view["coding_sheet"],
            api_key=runtime.api_key(settings.provider) or "",
            **endpoint_options(settings.provider),
            provider=settings.provider,
            model=settings.model,
            parallel_requests=settings.parallel_requests,
            request_delay_sec=settings.request_delay_sec,
            request_timeout_sec=settings.request_timeout_sec,
            service_tier=settings.service_tier,
            reasoning_effort=settings.reasoning_effort,
            only_pdfs=targets,
        )
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
