"""Input fingerprints: tie each saved extraction result to the inputs that
produced it, so a result is reused only while those inputs are unchanged.

A fingerprint covers what determines the provider's answer: the effective
coding manual (canonical YAML, so formatting-only edits don't matter), the
row IDs and locators requested for the PDF, the PDF's bytes, and the
provider, model and semantic request settings. Pacing, timeouts and service
tier only affect how a request is sent, so they are excluded.

Results saved before fingerprints existed carry none. Their inputs cannot be
verified, but older versions cleared results whenever the manual changed, so
they are kept usable as before rather than hidden or recoded at the user's
expense. Re-running a PDF replaces its legacy result with a verified one.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import threading

from .manual import manual_to_yaml_text

FINGERPRINT_VERSION = 1
STALE_STATUS = "stale"
CHANGED_INPUTS_ERROR = (
    "Inputs changed since this PDF was coded (manual, its coding-sheet rows, the PDF or the model "
    "settings). The result is kept but not used. Run or retry this PDF to update it."
)
MISSING_PDF_ERROR = "The source PDF is missing or unreadable, so this result cannot be verified or used."

# Hashing every PDF on each page view would be slow for large projects, so
# digests are cached against the file's identity and modification time.
_pdf_digests: dict[str, tuple[tuple[int, int, int], str]] = {}
_pdf_digests_lock = threading.Lock()


def pdf_digest(path) -> str | None:
    """SHA-256 of a PDF's bytes, or None if it can't be read."""

    try:
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        with _pdf_digests_lock:
            cached = _pdf_digests.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    with _pdf_digests_lock:
        _pdf_digests[str(path)] = (key, digest.hexdigest())
    return digest.hexdigest()


class InputChecker:
    """Fingerprint the current inputs of a project's PDFs and check saved
    results against them. Build one per request or run: the manual and
    settings are fixed for its lifetime, and each PDF is fingerprinted once."""

    def __init__(
        self, project, manual, coding_sheet, *, provider: str, model: str,
        reasoning_effort: str = "", base_url: str = "", response_format: str = "json_schema",
    ) -> None:
        self.project = project
        self.manual = manual
        self.coding_sheet = coding_sheet
        self._manual_text = manual_to_yaml_text(manual) if manual is not None else None
        self._settings = {
            "provider": provider,
            "model": model,
            # Gemini ignores reasoning effort; base URL and response format
            # exist only for OpenAI-compatible endpoints.
            "reasoning_effort": "" if provider == "gemini" else reasoning_effort,
            "base_url": base_url if provider == "openai_compatible" else "",
            "response_format": response_format if provider == "openai_compatible" else "",
        }
        self._fingerprints: dict[str, str | None] = {}

    def fingerprint(self, source_pdf: str) -> str | None:
        """The PDF's input fingerprint; None if it has no rows, its PDF can't
        be read, or there is no valid manual."""

        if source_pdf not in self._fingerprints:
            self._fingerprints[source_pdf] = self._compute(source_pdf)
        return self._fingerprints[source_pdf]

    def _compute(self, source_pdf: str) -> str | None:
        rows = self.coding_sheet.rows_for_pdf(source_pdf)
        if self._manual_text is None or not rows:
            return None
        pdf_sha256 = pdf_digest(self.project.sources_dir / source_pdf)
        if pdf_sha256 is None:
            return None
        inputs = {
            "fingerprint_version": FINGERPRINT_VERSION,
            "manual": self._manual_text,
            # Only the row ID and locator are sent to the provider; other
            # sheet columns (authors, year, title, DOI) are bookkeeping, and
            # row order carries no meaning.
            "rows": sorted([row.row_id, row.locator] for row in rows),
            "pdf_sha256": pdf_sha256,
            **self._settings,
        }
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def check(self, results: dict) -> dict:
        """Return `results` with each result checked against the current inputs.

        A result whose fingerprint doesn't match is replaced by a `stale` copy
        without coded values, so it is neither reused nor collated; the record
        on disk is untouched and becomes current again if the inputs are
        restored. Legacy results without a fingerprint are returned unchanged.
        Without a valid manual nothing can be checked (or run), so results are
        returned unchanged.
        """

        if self._manual_text is None:
            return dict(results)
        checked = {}
        for name, result in results.items():
            fingerprint = self.fingerprint(name)
            if result.input_fingerprint is None or result.input_fingerprint == fingerprint:
                checked[name] = result
                continue
            missing_pdf = fingerprint is None and bool(self.coding_sheet.rows_for_pdf(name))
            checked[name] = replace(
                result, status=STALE_STATUS, coded_by_row_id={}, missing_ids=set(), extra_ids=set(),
                error=MISSING_PDF_ERROR if missing_pdf else CHANGED_INPUTS_ERROR,
            )
        return checked
