"""Source formats: what each kind of article file needs.

A source is a PDF or an XML file (usually JATS, as served by PubMed Central and
many publishers). PDFs reach the model as PDFs, or as text extracted from them.
XML sources are never parsed for the model: their bytes are decoded as UTF-8 and
sent as article text, markup and all. Only identity matching (pdf_matching.py)
reads the JATS front matter, and it does so with regular expressions, so no DTD,
entity or network lookup ever happens.
"""

from __future__ import annotations

import codecs
import html
import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_FORMATS = {".pdf": "pdf", ".xml": "xml"}
SOURCE_SUFFIXES = frozenset(SOURCE_FORMATS)
# For <input accept>: extensions plus the MIME types browsers report for them.
SOURCE_ACCEPT = ".pdf,.xml,application/pdf,application/xml,text/xml"


class SourceError(ValueError):
    """A source file that cannot be used as it is."""


def source_format(name: Path | str) -> str | None:
    """'pdf' or 'xml' for a source filename (any case), else None."""

    return SOURCE_FORMATS.get(Path(name).suffix.lower())


class XmlChecker:
    """Check XML source bytes chunk by chunk, holding at most one chunk.

    The bytes are only decoded and sniffed, never parsed: empty, non-UTF-8,
    binary (NUL bytes) or non-markup content is rejected with a message saying
    how to fix it.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")()
        self._started = False

    def feed(self, chunk: bytes, final: bool = False) -> str:
        try:
            text = self._decoder.decode(chunk, final)
        except UnicodeDecodeError:
            raise SourceError(f"{self.name} is not UTF-8 text. Save the XML as UTF-8 and upload it again.") from None
        if "\x00" in text:
            raise SourceError(f"{self.name} is not a text file. Upload the article's XML file instead.")
        if not self._started and text.strip():
            if not text.lstrip().startswith("<"):
                raise SourceError(
                    f"{self.name} does not look like XML (it should start with '<'). "
                    "Upload the article's XML file instead."
                )
            self._started = True
        if final and not self._started:
            raise SourceError(f"{self.name} is empty. Upload the article's full XML file.")
        return text


def decode_xml(data: bytes, name: str) -> str:
    """An XML source's text, or SourceError saying how to fix the file."""

    return XmlChecker(name).feed(data, final=True)


def read_article_text(path: Path) -> str:
    """The article text of an XML source, exactly as stored (minus any BOM)."""

    return decode_xml(path.read_bytes(), path.name)


# --- JATS front matter, for identity matching only ---------------------------

# Front matter is at the start of the file; never scan a whole large article.
_FRONT_SCAN_CHARS = 512 * 1024
_FRONT_RE = re.compile(r"<front\b.*?</front\s*>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_ARTICLE_META_RE = re.compile(r"<article-meta\b.*?</article-meta\s*>", re.DOTALL)
_DOI_RE = re.compile(
    r"<article-id\b[^>]*\bpub-id-type\s*=\s*[\"']doi[\"'][^>]*>(.*?)</article-id\s*>", re.DOTALL
)
_TITLE_RE = re.compile(r"<article-title\b[^>]*>(.*?)</article-title\s*>", re.DOTALL)
_CONTRIB_GROUP_RE = re.compile(r"<contrib-group\b.*?</contrib-group\s*>", re.DOTALL)
_SURNAME_RE = re.compile(r"<surname\b[^>]*>(.*?)</surname\s*>", re.DOTALL)
_PUB_DATE_RE = re.compile(r"<pub-date\b[^>]*>(.*?)</pub-date\s*>", re.DOTALL)
_YEAR_RE = re.compile(r"<year\b[^>]*>\s*((?:19|20)[0-9]{2})\s*</year\s*>")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


@dataclass(frozen=True)
class JatsFront:
    title: str = ""
    doi: str = ""
    year: str | None = None
    surnames: tuple[str, ...] = ()


def _plain(fragment: str) -> str:
    """Inline markup removed, entities resolved, whitespace collapsed."""

    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def normalize_doi(value: str) -> str:
    """A DOI compared case-insensitively, without a resolver URL or `doi:` prefix."""

    return _DOI_PREFIX_RE.sub("", value.strip()).strip().lower()


def jats_front(text: str) -> JatsFront:
    """Title, DOI, publication year and author surnames from JATS front matter.

    Missing parts are left empty; an XML file without a `<front>` element
    (not JATS) yields an empty result rather than an error.
    """

    head = text[:_FRONT_SCAN_CHARS]
    front_match = _FRONT_RE.search(head)
    if not front_match:
        return JatsFront()
    front = front_match.group(0)
    # <journal-meta> can carry its own DOI; the article's lives in <article-meta>.
    meta_match = _ARTICLE_META_RE.search(front)
    meta = meta_match.group(0) if meta_match else front
    doi = _DOI_RE.search(meta)
    title = _TITLE_RE.search(meta)
    years = [_YEAR_RE.search(date) for date in _PUB_DATE_RE.findall(meta)]
    surnames = [
        _plain(name)
        for group in _CONTRIB_GROUP_RE.findall(meta)
        for name in _SURNAME_RE.findall(group)
    ]
    return JatsFront(
        title=_plain(title.group(1)) if title else "",
        doi=normalize_doi(_plain(doi.group(1))) if doi else "",
        year=next((year.group(1) for year in years if year), None),
        surnames=tuple(name for name in surnames if name),
    )
