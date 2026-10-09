"""Assisted matching between a coding sheet's `source_pdf` values and the
PDFs actually uploaded to a project.

`coding_sheet.py` treats a `source_pdf` that doesn't match an uploaded
filename exactly as a blocking issue — correct, since a run must never guess
which PDF a row means. This module is a separate, additive concern: given the
unmatched rows and the uploaded PDFs nobody claimed ("orphans"), suggest which
orphan each unmatched `source_pdf` probably refers to, so the user can confirm
(or repick) instead of manually editing the CSV. Nothing here is applied
automatically — suggestions only feed a UI the user must confirm.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .manual_drafting import ManualDraftError, pdf_text
from .coding_sheet import coding_sheet_reader
from .projects import Project


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[a-zA-Z])(?=[0-9])|(?<=[0-9])(?=[a-zA-Z])")

# Words too generic to count as evidence of a match on their own (every filename
# has "pdf" in it; "et al" is boilerplate, not a name; "effect"/"task"/"study" show
# up in nearly every psychology paper title and would inflate every candidate).
_STOPWORDS = {
    "pdf", "et", "al", "and", "the", "of", "in", "a", "an", "is", "are", "on", "for",
    "with", "to", "as", "by", "not", "same", "effect", "effects", "task", "study",
    "https", "http", "www", "doi", "org", "dx", "com",
}

HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.5
LOW_CONFIDENCE = 0.25


@dataclass
class UnmatchedPaper:
    # Stable identity for this paper group, used to write suggestions back
    # (`apply_source_pdf_matches`). Equal to `source_pdf` when the CSV has
    # one; when `source_pdf` is blank, a synthetic key from whatever
    # identification the row *does* have, since grouping every blank-filename
    # row under the empty string would merge unrelated papers together.
    key: str
    source_pdf: str
    authors: str
    year: str
    locator: str
    title: str = ""
    doi: str = ""
    row_count: int = 0


@dataclass
class MatchedPaper:
    """One uploaded PDF currently assigned by the coding sheet."""

    source_pdf: str
    authors: str
    year: str
    title: str = ""
    row_count: int = 0


@dataclass
class PdfSignal:
    tokens: str
    year: str | None


_SIGNAL_CACHE_NAME = "pdf_signals.json"
_SCORE_CACHE_NAME = "pdf_match_scores.json"
_SCORE_CACHE_VERSION = 1


def _signal_cache_path(project: Project) -> Path:
    return project.path / ".meta_coder" / _SIGNAL_CACHE_NAME


def _score_cache_path(project: Project) -> Path:
    return project.path / ".meta_coder" / _SCORE_CACHE_NAME


def load_signal_cache(project: Project) -> dict[str, dict[str, Any]]:
    """Per-project, on-disk cache of each uploaded PDF's matching signal
    (filename tokens + first-page text, or a GROBID header extraction),
    keyed by filename. Parsing a PDF for its signal is the expensive part of
    match-suggesting (full-document text extraction) — this cache is what
    lets `PdfScanner` do that once, on upload, instead of on every page
    load."""

    path = _signal_cache_path(project)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_signal_cache(project: Project, cache: dict[str, dict[str, Any]]) -> None:
    path = _signal_cache_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(path)


def load_match_score_cache(project: Project) -> dict[str, float]:
    """Load pairwise fuzzy scores calculated for this project.

    A cached entry is tied to both the paper identity and the PDF signal, so
    accepting a different match merely filters these scores; it does not make
    the remaining pairs stale.
    """

    try:
        data = json.loads(_score_cache_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCORE_CACHE_VERSION:
        return {}
    scores = data.get("scores")
    if not isinstance(scores, dict):
        return {}
    return {key: value for key, value in scores.items() if isinstance(key, str) and isinstance(value, float)}


def save_match_score_cache(project: Project, scores: dict[str, float]) -> None:
    """Persist pairwise fuzzy scores atomically for reuse after restarts."""

    path = _score_cache_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": _SCORE_CACHE_VERSION, "scores": scores}), encoding="utf-8")
    tmp.replace(path)


def _score_cache_key(paper: UnmatchedPaper, signal: PdfSignal) -> str:
    inputs = {
        "version": _SCORE_CACHE_VERSION,
        "paper": {
            "source_pdf": paper.source_pdf,
            "authors": paper.authors,
            "year": paper.year,
            "title": paper.title,
            "doi": paper.doi,
        },
        "signal": {"tokens": signal.tokens, "year": signal.year},
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cache_source(grobid_url: str) -> str:
    return "grobid" if grobid_url else "local"


def _cache_entry(path: Path, signal: PdfSignal, grobid_url: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "mtime": int(stat.st_mtime),
        "size": stat.st_size,
        "source": _cache_source(grobid_url),
        "tokens": signal.tokens,
        "year": signal.year,
    }


def cached_signal(
    path: Path, cache: dict[str, dict[str, Any]], *, grobid_url: str = ""
) -> PdfSignal | None:
    """The cached signal for `path`, or None if there's no entry, the file
    has changed since it was scanned (size/mtime mismatch), or it was scanned
    under a different GROBID configuration than the one in effect now."""

    entry = cache.get(path.name)
    if not entry:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if (
        entry.get("size") != stat.st_size
        or entry.get("mtime") != int(stat.st_mtime)
        or entry.get("source") != _cache_source(grobid_url)
    ):
        return None
    return PdfSignal(tokens=entry.get("tokens", ""), year=entry.get("year"))


@dataclass
class MatchSuggestion:
    paper: UnmatchedPaper
    filename: str | None
    confidence: str
    candidates: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    # Split "smith2020" into "smith 2020" first, so a filename that jams a
    # surname and year together still tokenizes the same as "Smith, 2020".
    spaced = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", text)
    return " ".join(_TOKEN_RE.findall(spaced.lower()))


def _find_year(text: str) -> str | None:
    match = _YEAR_RE.search(text)
    return match.group(0) if match else None


def _significant_tokens(text: str) -> set[str]:
    """Tokens worth treating as evidence: real words/years, not boilerplate
    like "pdf" or "et al" that would match nearly every paper."""

    return {
        token
        for token in _normalize(text).split()
        if token not in _STOPWORDS and (len(token) >= 3 or token.isdigit())
    }


def _paper_key(record: dict) -> str | None:
    """A grouping/rewrite key for one CSV row's paper, in order of how
    precisely it identifies the paper. Returns None when the row has neither
    a `source_pdf` nor any other identifying field — nothing to group or
    suggest a match for."""

    source_pdf = (record.get("source_pdf") or "").strip()
    if source_pdf:
        return source_pdf
    doi = (record.get("doi") or "").strip()
    if doi:
        return f"doi:{doi}"
    title = (record.get("title") or "").strip()
    if title:
        return f"title:{title}"
    authors = (record.get("authors") or "").strip()
    year = (record.get("year") or "").strip()
    if authors or year:
        return f"authors:{authors}|{year}"
    return None


def unmatched_paper_ids(
    coding_sheet_text: str, *, uploaded_filenames: set[str]
) -> list[UnmatchedPaper]:
    """Group coding-sheet rows that don't already resolve to an uploaded
    filename, by `_paper_key` — usually the raw `source_pdf` string, but a
    row can be grouped (and later matched) purely on title/DOI/authors+year
    when `source_pdf` hasn't been filled in yet. Permissive on purpose —
    unlike `parse_coding_sheet_csv`, malformed rows are simply skipped rather
    than reported, since this only feeds match suggestions, not validation."""

    reader = coding_sheet_reader(coding_sheet_text)
    groups: dict[str, UnmatchedPaper] = {}
    for record in reader:
        source_pdf = (record.get("source_pdf") or "").strip()
        if source_pdf and source_pdf in uploaded_filenames:
            continue
        key = _paper_key(record)
        if key is None:
            continue
        if key not in groups:
            groups[key] = UnmatchedPaper(
                key=key,
                source_pdf=source_pdf,
                authors=(record.get("authors") or "").strip(),
                year=(record.get("year") or "").strip(),
                locator=(record.get("locator") or "").strip(),
                title=(record.get("title") or "").strip(),
                doi=(record.get("doi") or "").strip(),
            )
        groups[key].row_count += 1
    return list(groups.values())


def matched_paper_ids(coding_sheet_text: str, *, uploaded_filenames: set[str]) -> list[MatchedPaper]:
    """Group every coding-sheet assignment that resolves to an uploaded PDF.

    This intentionally reads the raw sheet rather than the validated
    ``CodingSheet``: a newly saved match should remain visible here even when
    another field on that row still needs attention.
    """

    groups: dict[str, MatchedPaper] = {}
    for record in coding_sheet_reader(coding_sheet_text):
        source_pdf = (record.get("source_pdf") or "").strip()
        if source_pdf not in uploaded_filenames:
            continue
        if source_pdf not in groups:
            groups[source_pdf] = MatchedPaper(
                source_pdf=source_pdf,
                authors=(record.get("authors") or "").strip(),
                year=(record.get("year") or "").strip(),
                title=(record.get("title") or "").strip(),
            )
        groups[source_pdf].row_count += 1
    return sorted(groups.values(), key=lambda paper: paper.source_pdf.lower())


def _local_pdf_signal(path: Path) -> PdfSignal:
    filename_tokens = _normalize(path.stem.replace("_", " ").replace("-", " "))
    filename_year = _find_year(path.stem)

    text_tokens = ""
    text_year = filename_year
    try:
        text = pdf_text(path.read_bytes())
    except (ManualDraftError, OSError):
        text = ""
    if text:
        # `pdf_text` joins pages as "[Page N]\n<text>" separated by "\n\n" —
        # split on the next page marker, not the first blank line, since a
        # page's own text commonly has blank lines in it (title page, byline,
        # abstract) well before the page actually ends.
        first_page = re.split(r"\n\n\[Page 2\]", text, maxsplit=1)[0]
        text_tokens = _normalize(first_page)
        text_year = text_year or _find_year(first_page)

    combined_tokens = " ".join(t for t in (filename_tokens, text_tokens) if t)
    # `score_pair`'s fuzzy-match step is O(unmatched paper tokens x signal
    # tokens); a first-page's raw token count (hundreds, mostly stopwords/
    # short filler) makes that the dominant cost once PDF parsing itself is
    # cached. Storing only the significant tokens — the same filter already
    # applied to the paper side — keeps every token that could plausibly be
    # evidence while shrinking the set scoring has to search.
    return PdfSignal(tokens=" ".join(sorted(_significant_tokens(combined_tokens))), year=text_year)


def grobid_signal(path: Path, grobid_url: str) -> PdfSignal | None:
    """Query a GROBID server's header-extraction endpoint for title/author/year.
    Any failure (unreachable server, timeout, bad response, unparseable TEI)
    returns None so the caller falls back to the local heuristic — matching
    must never hard-fail because GROBID is unset or down."""

    url = grobid_url.rstrip("/") + "/api/processHeaderDocument"
    boundary = "----metacoderpdfmatch"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="input"; filename="{path.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            tei = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    try:
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        root = ET.fromstring(tei)
        title = root.findtext(".//tei:titleStmt/tei:title", default="", namespaces=ns) or ""
        surnames = [
            el.text or ""
            for el in root.findall(".//tei:sourceDesc//tei:author//tei:surname", ns)
        ]
        date = root.findtext(".//tei:sourceDesc//tei:date", default="", namespaces=ns) or ""
        date_attr = root.find(".//tei:sourceDesc//tei:date", ns)
        when = date_attr.get("when", "") if date_attr is not None else ""
    except ET.ParseError:
        return None

    tokens = _normalize(" ".join([title, *surnames]))
    if not tokens:
        return None
    year = _find_year(when) or _find_year(date)
    return PdfSignal(tokens=tokens, year=year)


def pdf_signal(path: Path, *, grobid_url: str = "") -> PdfSignal:
    if grobid_url:
        signal = grobid_signal(path, grobid_url)
        if signal is not None:
            return signal
    return _local_pdf_signal(path)


def score_pair(paper: UnmatchedPaper, signal: PdfSignal) -> float:
    """How likely `signal` (an uploaded PDF's filename + first-page text) is
    the paper referenced by `paper`'s `source_pdf`/`authors`/`year`.

    Containment, not a global similarity ratio: `signal.tokens` is often a
    full first page of running prose, hundreds of tokens long, while the
    paper side is just a surname and a year (or, when available, a title and
    a DOI). A plain SequenceMatcher ratio between a short string and a long
    one is length-normalized and stays near zero even when the short string
    appears verbatim in the long one — so instead this checks what fraction
    of the paper's significant tokens (surname, year, title words, DOI)
    actually appear in the signal.
    """

    paper_tokens = _significant_tokens(
        f"{paper.authors} {paper.year} {paper.source_pdf} {paper.title} {paper.doi}"
    )
    if not paper_tokens:
        return 0.0

    signal_tokens = set(signal.tokens.split())
    matched = paper_tokens & signal_tokens
    containment = len(matched) / len(paper_tokens)

    # Partial credit for near-misses (typos, OCR noise, abbreviations) on the
    # tokens that didn't match exactly.
    fuzzy_credit = 0.0
    unmatched = paper_tokens - matched
    if unmatched and signal_tokens:
        best_ratios = [
            max(SequenceMatcher(None, token, candidate).ratio() for candidate in signal_tokens)
            for token in unmatched
        ]
        fuzzy_credit = sum(best_ratios) / len(paper_tokens) * 0.5

    year_bonus = 0.15 if paper.year and signal.year and paper.year == signal.year else 0.0
    return min(1.0, containment + fuzzy_credit + year_bonus)


def _confidence_band(score: float) -> str:
    if score >= HIGH_CONFIDENCE:
        return "high"
    if score >= MEDIUM_CONFIDENCE:
        return "medium"
    if score >= LOW_CONFIDENCE:
        return "low"
    return "none"


def suggest_matches(
    papers: list[UnmatchedPaper],
    orphan_paths: list[Path],
    *,
    grobid_url: str = "",
    cache: dict[str, dict[str, Any]] | None = None,
) -> list[MatchSuggestion]:
    """`cache`, when given, is a project's on-disk signal cache (see
    `load_signal_cache`) — a cache hit skips re-parsing that PDF entirely.
    Callers that care about request latency (the project page) should only
    call this once every orphan has a fresh cache entry (see `PdfScanner`);
    a miss here still falls back to parsing inline so the result stays
    correct either way."""

    signals = _signals_for_paths(orphan_paths, cache=cache, grobid_url=grobid_url)
    scores = {
        (paper.key, name): score_pair(paper, signal)
        for paper in papers
        for name, signal in signals.items()
    }
    return _suggestions_from_scores(papers, signals, scores)


def suggest_matches_from_score_cache(
    papers: list[UnmatchedPaper],
    orphan_paths: list[Path],
    *,
    grobid_url: str = "",
    signal_cache: dict[str, dict[str, Any]] | None = None,
    score_cache: dict[str, float],
) -> tuple[list[MatchSuggestion], bool]:
    """Build suggestions from cached pair scores, calculating only cache misses.

    The returned ``changed`` flag tells the caller whether the on-disk cache
    needs saving. Removing an accepted paper/PDF pair does not change any key
    for the remaining pairs, so this path does no fuzzy scoring after a save.
    """

    signals = _signals_for_paths(orphan_paths, cache=signal_cache, grobid_url=grobid_url)
    scores: dict[tuple[str, str], float] = {}
    changed = False
    for paper in papers:
        for name, signal in signals.items():
            cache_key = _score_cache_key(paper, signal)
            score = score_cache.get(cache_key)
            if score is None:
                score = score_pair(paper, signal)
                score_cache[cache_key] = score
                changed = True
            scores[(paper.key, name)] = score
    return _suggestions_from_scores(papers, signals, scores), changed


def _signals_for_paths(
    orphan_paths: list[Path], *, cache: dict[str, dict[str, Any]] | None, grobid_url: str
) -> dict[str, PdfSignal]:
    signals = {}
    for path in orphan_paths:
        signal = cached_signal(path, cache, grobid_url=grobid_url) if cache is not None else None
        if signal is None:
            signal = pdf_signal(path, grobid_url=grobid_url)
        signals[path.name] = signal
    return signals


def _suggestions_from_scores(
    papers: list[UnmatchedPaper],
    signals: dict[str, PdfSignal],
    scores: dict[tuple[str, str], float],
) -> list[MatchSuggestion]:
    orphan_names = sorted(signals)
    scored = [(score, paper_key, name) for (paper_key, name), score in scores.items()]
    scored.sort(key=lambda item: item[0], reverse=True)

    assigned_filename: dict[str, str] = {}
    assigned_score: dict[str, float] = {}
    claimed_orphans: set[str] = set()
    for score, key, name in scored:
        if key in assigned_filename or name in claimed_orphans:
            continue
        assigned_filename[key] = name
        assigned_score[key] = score
        claimed_orphans.add(name)

    suggestions = []
    for paper in papers:
        filename = assigned_filename.get(paper.key)
        score = assigned_score.get(paper.key, 0.0)
        confidence = _confidence_band(score) if filename else "none"
        if confidence == "none":
            filename = None
        suggestions.append(
            MatchSuggestion(
                paper=paper, filename=filename, confidence=confidence, candidates=orphan_names
            )
        )
    return suggestions


def apply_source_pdf_matches(path: Path, mapping: dict[str, str]) -> None:
    """Rewrite `coding_sheet.csv` in place: every row whose `_paper_key`
    (its `source_pdf`, or — when that's blank — a key derived from its
    title/DOI/authors+year) is in `mapping` gets that filename written into
    `source_pdf`. Every other row (and the header/column order) is left
    untouched."""

    text = path.read_text(encoding="utf-8")
    reader = coding_sheet_reader(text)
    fieldnames = reader.fieldnames or []
    rows = list(reader)
    for row in rows:
        key = _paper_key(row)
        if key is not None and key in mapping:
            row["source_pdf"] = mapping[key]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


@dataclass
class ScanState:
    status: str = "idle"  # idle | running | complete
    total: int = 0
    processed: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status, "total": self.total, "processed": self.processed}


class PdfScanner:
    """Runs `pdf_signal` for uploaded PDFs in a background thread and writes
    the results into the project's on-disk signal cache, so the project page
    (`_project_view`) never has to parse a PDF inline. One project scans at a
    time; PDFs queued while a scan is already running are folded into that
    same run rather than dropped or started as a second overlapping scan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, ScanState] = {}
        self._pending: dict[str, dict[str, Path]] = {}
        self._inflight: dict[str, int] = {}
        self._running: set[str] = set()

    def state(self, project_id: str) -> ScanState | None:
        return self._states.get(project_id)

    def is_running(self, project_id: str) -> bool:
        return project_id in self._running

    def scan_async(self, project: Project, paths: list[Path], *, grobid_url: str = "") -> None:
        if not paths:
            return
        project_id = project.project_id
        with self._lock:
            pending = self._pending.setdefault(project_id, {})
            for path in paths:
                pending[path.name] = path
            if project_id in self._running:
                state = self._states.get(project_id)
                if state:
                    state.total = state.processed + self._inflight.get(project_id, 0) + len(pending)
                return
            self._running.add(project_id)
            state = ScanState(status="running", total=len(pending))
            self._states[project_id] = state

        thread = threading.Thread(
            target=self._scan_loop, args=(project, grobid_url, state), daemon=True
        )
        thread.start()

    def _scan_loop(self, project: Project, grobid_url: str, state: ScanState) -> None:
        project_id = project.project_id
        cache = load_signal_cache(project)
        completed = False
        try:
            while True:
                with self._lock:
                    batch = self._pending.get(project_id, {})
                    self._pending[project_id] = {}
                    if not batch:
                        # Publish completion while holding the same lock used
                        # by scan_async, so a new queue is either included in
                        # this worker or starts a fresh worker.
                        self._running.discard(project_id)
                        self._inflight.pop(project_id, None)
                        state.status = "complete"
                        completed = True
                        return
                    self._inflight[project_id] = len(batch)
                    state.total = state.processed + len(batch) + len(self._pending.get(project_id, {}))
                for path in batch.values():
                    try:
                        cache[path.name] = _cache_entry(
                            path, pdf_signal(path, grobid_url=grobid_url), grobid_url
                        )
                    except OSError:
                        pass
                    with self._lock:
                        state.processed += 1
                        self._inflight[project_id] = max(0, self._inflight.get(project_id, 1) - 1)
                        state.total = state.processed + self._inflight[project_id] + len(self._pending.get(project_id, {}))
                save_signal_cache(project, cache)
        finally:
            if not completed:
                with self._lock:
                    self._running.discard(project_id)
                    self._inflight.pop(project_id, None)
                    state.status = "complete"
