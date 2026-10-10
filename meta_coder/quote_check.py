"""Check that the quotes a model returns actually appear in the PDF.

Each coded field may carry a `quote`: the passage the model says supports its
value. After a response arrives, every quote is searched for in the text layer
pypdf reads from the same PDF, and the outcome is stored beside the quote as
`quote_check`:

- `verified`: the quote is in the text once layout differences are ignored.
- `approximate`: a passage matches nearly all of it (OCR noise, a dropped word)
  and every digit in the quote.
- `not_found`: the PDF has a text layer, and nothing in it matches.
- `not_checked`: the quote could not be checked, because it is too short to be
  meaningful or the PDF has no usable text layer where it should be.

The check never changes a PDF's status. Gemini and OpenRouter read the PDF
itself, so their reading can differ from pypdf's text layer; that is why a
missing text layer yields `not_checked`, never `not_found`.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


VERIFIED = "verified"
APPROXIMATE = "approximate"
NOT_FOUND = "not_found"
NOT_CHECKED = "not_checked"

# A normalised quote shorter than this (e.g. "N = 40") would match by chance.
MIN_QUOTE_CHARS = 12
# Share of a quote's characters that must line up, in order, with one passage.
FUZZY_THRESHOLD = 0.9
# A page with less normalised text than this has no usable text layer.
MIN_PAGE_CHARS = 50
# Above this share of such pages, a quote that is not found may simply sit on
# a scanned page, so it is reported as not checked.
MAX_TEXTLESS_SHARE = 0.2

_SEED_CHARS = 8
_MAX_CANDIDATES = 40
_MIN_BLOCK_CHARS = 3
_KEPT_SYMBOLS = frozenset("<>=≤≥")
_DECIMAL_POINT_RE = re.compile(r"\.(?=\d)")
_DECIMAL_POINT = "\x00"
# A hyphen, minus sign or en dash directly before a number is a sign (or a
# range), unlike the hyphens that line breaks add and remove.
_MINUS_RE = re.compile(r"[-−–](?=\.?\d)")
_MINUS = "\x01"
_ELLIPSIS_RE = re.compile(r"\[?(?:\.\s?){3,}\]?|\[?…\]?")


def normalize(text: str) -> str:
    """Reduce text to what a quote and a PDF text layer should agree on.

    NFKC folds ligatures (`ﬁ`), full-width forms and non-breaking spaces; case
    is folded; then only letters, digits, decimal points, minus signs before
    a number and comparison signs are kept. Dropping spaces, hyphens and punctuation makes the comparison
    blind to line breaks, end-of-line hyphenation, missing or doubled spaces
    and typographic quotes and dashes, all of which PDF extraction changes.
    """

    text = unicodedata.normalize("NFKC", text).casefold()
    text = _MINUS_RE.sub(_MINUS, text)
    text = _DECIMAL_POINT_RE.sub(_DECIMAL_POINT, text)
    kept = {_DECIMAL_POINT: ".", _MINUS: "-"}
    return "".join(
        kept.get(char, char)
        for char in text
        if char.isalnum() or char in _KEPT_SYMBOLS or char in kept
    )


class PdfText:
    """The normalised text of a PDF, searchable as one string across pages."""

    def __init__(self, pages: list[str]) -> None:
        self.pages = [normalize(page) for page in pages]
        self.text = "".join(self.pages)
        self._starts: list[int] = []
        offset = 0
        for page in self.pages:
            self._starts.append(offset)
            offset += len(page)

    def page_at(self, offset: int) -> int:
        """The 1-based page containing the character at `offset`."""

        return bisect_right(self._starts, offset)

    def may_hide(self, page: int | None) -> bool:
        """True if a quote could be in the PDF without being in this text: the
        page the model named has no text layer, or many pages have none."""

        textless = [len(text) < MIN_PAGE_CHARS for text in self.pages]
        if page is not None and 1 <= page <= len(textless) and textless[page - 1]:
            return True
        return sum(textless) / len(textless) > MAX_TEXTLESS_SHARE


def read_pdf_text(path: Path) -> PdfText | None:
    """The PDF's text layer, or None when it cannot be read at all."""

    from pypdf import PdfReader

    try:
        return PdfText([page.extract_text() or "" for page in PdfReader(path).pages])
    except Exception:  # noqa: BLE001 - an unreadable PDF means "not checked"
        return None


def _find_in_order(fragments: list[str], text: str) -> int | None:
    """Offset of the first fragment if every fragment occurs, in order."""

    first = None
    position = 0
    for fragment in fragments:
        found = text.find(fragment, position)
        if found == -1:
            return None
        if first is None:
            first = found
        position = found + len(fragment)
    return first


def _fuzzy_find(needle: str, text: str) -> int | None:
    """Offset of a passage matching at least FUZZY_THRESHOLD of `needle`.

    Short exact pieces of the quote locate candidate passages; each candidate
    is then aligned with the whole quote. Only runs of several characters
    count, so scattered single-letter coincidences cannot add up to a match,
    and a passage whose numbers differ from the quote's never matches.
    """

    length = len(needle)
    slack = max(_SEED_CHARS, length // 5)
    best_score = 0.0
    best_offset = None
    seen: set[int] = set()
    for seed_start in range(0, length - _SEED_CHARS + 1, _SEED_CHARS // 2):
        seed = needle[seed_start : seed_start + _SEED_CHARS]
        position = text.find(seed)
        while position != -1 and len(seen) < _MAX_CANDIDATES:
            start = max(0, position - seed_start - slack)
            if start // slack not in seen:
                seen.add(start // slack)
                window = text[start : position - seed_start + length + slack]
                blocks = [
                    block
                    for block in SequenceMatcher(None, needle, window, autojunk=False).get_matching_blocks()
                    if block.size >= _MIN_BLOCK_CHARS
                ]
                score = sum(block.size for block in blocks) / length
                matched = {
                    index for block in blocks for index in range(block.a, block.a + block.size)
                }
                # Letters may differ slightly, but every digit of the quote must
                # line up: `124` against `142` is a different number, not noise.
                if any(char.isdigit() and index not in matched for index, char in enumerate(needle)):
                    score = 0.0
                if score > best_score:
                    best_score = score
                    best_offset = start + blocks[0].b
            position = text.find(seed, position + 1)
    return best_offset if best_score >= FUZZY_THRESHOLD else None


def check_quote(
    quote: str, document: PdfText | None, page: int | None = None
) -> tuple[str, int | None]:
    """Return the check outcome and, when found, the 1-based page it is on.

    `page` is the page the model named for the quote. It never restricts the
    search (models often give the printed page number, not the PDF's); it only
    helps decide between `not_found` and `not_checked`.
    """

    # A quote with an ellipsis is matched piece by piece, in order.
    fragments = [part for part in map(normalize, _ELLIPSIS_RE.split(quote)) if part]
    needle = "".join(fragments)
    if document is None or not document.text or len(needle) < MIN_QUOTE_CHARS:
        return NOT_CHECKED, None
    offset = _find_in_order(fragments, document.text)
    if offset is not None:
        return VERIFIED, document.page_at(offset)
    offset = _fuzzy_find(needle, document.text)
    if offset is not None:
        return APPROXIMATE, document.page_at(offset)
    if document.may_hide(page):
        return NOT_CHECKED, None
    return NOT_FOUND, None


def annotate_quote_checks(coded_by_row_id: object, pdf_path: Path) -> None:
    """Add `quote_check` (and `quote_found_page`) to every coded cell that has a
    quote, in place. Tolerates the malformed rows and cells that a response
    held for review can contain. The PDF is only read if there is a quote."""

    if not isinstance(coded_by_row_id, dict):
        return
    cells: list[dict[str, Any]] = [
        cell
        for coded in coded_by_row_id.values()
        if isinstance(coded, dict)
        for cell in coded.values()
        if isinstance(cell, dict)
    ]
    quoted = []
    for cell in cells:
        # These keys are MetaCoder's own; a model must not be able to supply them.
        cell.pop("quote_check", None)
        cell.pop("quote_found_page", None)
        if isinstance(cell.get("quote"), str) and cell["quote"].strip():
            quoted.append(cell)
    if not quoted:
        return
    document = read_pdf_text(pdf_path)
    for cell in quoted:
        page = cell.get("page")
        named_page = page if isinstance(page, int) and not isinstance(page, bool) else None
        status, found_page = check_quote(cell["quote"], document, named_page)
        cell["quote_check"] = status
        if found_page is not None:
            cell["quote_found_page"] = found_page
