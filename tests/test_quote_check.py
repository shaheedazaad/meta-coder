"""Unit tests for meta_coder.quote_check: normalising text, the quote search and
its four statuses, the PDF text layer, and annotating coded cells."""

from difflib import SequenceMatcher
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from meta_coder import quote_check as qc
from meta_coder.quote_check import (
    APPROXIMATE,
    MAX_TEXTLESS_SHARE,
    MIN_PAGE_CHARS,
    MIN_QUOTE_CHARS,
    NOT_CHECKED,
    NOT_FOUND,
    VERIFIED,
    PdfText,
    annotate_quote_checks,
    check_quote,
    normalize,
    read_pdf_text,
)


BODY = (
    "The intervention improved reading scores substantially across all three "
    "schools in the sample."
)
BODY_QUOTE = "The intervention improved reading scores substantially"
LONG = (
    "The mean difference in reading comprehension between the intervention and "
    "control groups was 4.2 points after one term."
)
DIGITS = (
    "The sample included 124 children across three schools in the district "
    "during the spring term."
)
FILLER = (
    "Participants were randomly allocated to two arms within each school and "
    "outcomes were assessed blind."
)


# normalize


@pytest.mark.parametrize(('text', 'expected'), [
    ("ﬁnal ﬂow", "finalflow"),
    ("The Effect WAS Large", "theeffectwaslarge"),
    ("a b\tc d-e–f—g 'h' “i” ‘j’ \"k\"", "abcdefghijk"),
    ("p = .45, n = 2.31.", "p=.45n=2.31"),
    ("Done. 2 more", "done2more"),
    ("a < b > c = d ≤ e ≥ f", "a<b>c=d≤e≥f"),
    ("50%", "50"),
])
def test_normalize_folds_and_strips_layout_characters(text, expected):
    assert normalize(text) == expected


def test_normalize_reads_end_of_line_hyphenation_as_one_word():
    assert normalize("ex-\nchange") == normalize("exchange") == "exchange"


# verified


def test_exact_quote_is_verified_on_its_page():
    assert check_quote(BODY_QUOTE, PdfText([BODY])) == (VERIFIED, 1)


def test_quote_differing_only_in_layout_is_verified():
    page = "The intervention improved\nreading scores substan-\ntially across all three schools."
    assert check_quote(BODY_QUOTE, PdfText([page])) == (VERIFIED, 1)


def test_curly_quotes_and_ligatures_match_straight_text():
    page = "The “strong” eﬀect was ﬂat across the whole sample of children here."
    quote = 'The "strong" effect was flat across the whole sample of children here'
    assert check_quote(quote, PdfText([page])) == (VERIFIED, 1)


def test_quote_reports_the_page_it_is_on():
    pdf = PdfText(["Introduction: background on the programme and its history.", FILLER,
                   "Results: the mean difference was 4.5 points across all groups."])
    assert check_quote("the mean difference was 4.5 points", pdf) == (VERIFIED, 3)
    assert check_quote("outcomes were assessed blind", pdf) == (VERIFIED, 2)


@pytest.mark.parametrize('quote', [
    "The intervention improved ... across all three schools",
    "The intervention improved … across all three schools",
    "The intervention improved [...] across all three schools",
])
def test_ellipsis_pieces_that_occur_in_order_are_verified(quote):
    assert check_quote(quote, PdfText([BODY])) == (VERIFIED, 1)


def test_ellipsis_pieces_in_the_wrong_order_are_not_verified():
    page = (
        "The first alpha phrase appears here in the text of this page. Later the "
        "beta phrase follows after a long stretch of unrelated words."
    )
    status, found = check_quote("beta phrase follows ... first alpha phrase", PdfText([page]))
    assert status != VERIFIED
    assert status == NOT_FOUND and found is None


# approximate


def test_one_or_two_misspelled_letters_in_a_long_quote_are_approximate():
    quote = (
        "The mean difference in reading comprehenslon between the intervention and "
        "contral groups was 4.2 points after one term."
    )
    assert check_quote(quote, PdfText([LONG])) == (APPROXIMATE, 1)


def test_a_dropped_word_is_approximate_and_reports_its_page():
    quote = (
        "The mean difference in comprehension between the intervention and control "
        "groups was 4.2 points after one term."
    )
    pdf = PdfText([FILLER, LONG])
    assert check_quote(quote, pdf) == (APPROXIMATE, 2)


# not found


def test_paraphrase_is_not_found():
    quote = "Reading gains were larger for pupils who received the programme"
    assert check_quote(quote, PdfText([BODY])) == (NOT_FOUND, None)


def test_invented_sentence_is_not_found():
    quote = "Participants reported high satisfaction with the tutoring sessions"
    assert check_quote(quote, PdfText([BODY, FILLER])) == (NOT_FOUND, None)


def test_transposed_number_is_not_found():
    quote = DIGITS.replace("124", "142")
    assert check_quote(quote, PdfText([DIGITS])) == (NOT_FOUND, None)


# not checked


def test_missing_document_is_not_checked():
    assert check_quote(BODY_QUOTE, None) == (NOT_CHECKED, None)


@pytest.mark.parametrize('pages', [[], ["", "   "]])
def test_document_without_text_is_not_checked(pages):
    assert check_quote(BODY_QUOTE, PdfText(pages)) == (NOT_CHECKED, None)


def test_quote_shorter_than_the_minimum_after_normalising_is_not_checked():
    short = "N = 40"
    assert len(normalize(short)) < MIN_QUOTE_CHARS
    assert check_quote(short, PdfText([BODY + " N = 40 children."])) == (NOT_CHECKED, None)


def _ten_pages(*textless_indices):
    pages = [FILLER] * 10
    for index in textless_indices:
        pages[index] = "Short."
    return PdfText(pages)


UNRELATED = "A sentence that appears nowhere in this particular document at all"


def test_unmatched_quote_on_a_textless_named_page_is_not_checked():
    pdf = _ten_pages(1)  # 10% textless, so only the named page can explain a miss
    assert check_quote(UNRELATED, pdf, page=2) == (NOT_CHECKED, None)


def test_unmatched_quote_on_a_text_rich_named_page_is_not_found():
    pdf = _ten_pages(1)
    assert check_quote(UNRELATED, pdf, page=3) == (NOT_FOUND, None)


def test_unmatched_quote_when_many_pages_have_no_text_is_not_checked():
    pdf = _ten_pages(0, 1, 2)  # 30% textless, above MAX_TEXTLESS_SHARE
    assert 0.3 > MAX_TEXTLESS_SHARE
    assert check_quote(UNRELATED, pdf) == (NOT_CHECKED, None)


def test_unmatched_quote_with_few_textless_pages_is_not_found():
    assert check_quote(UNRELATED, _ten_pages(4)) == (NOT_FOUND, None)


@pytest.mark.parametrize('page', [0, 99])
def test_out_of_range_page_falls_back_to_the_share_rule(page):
    assert check_quote(UNRELATED, _ten_pages(1), page=page) == (NOT_FOUND, None)
    assert check_quote(UNRELATED, _ten_pages(0, 1, 2), page=page) == (NOT_CHECKED, None)


def test_page_text_threshold_decides_what_counts_as_textless():
    rich = "x" * MIN_PAGE_CHARS
    thin = "x" * (MIN_PAGE_CHARS - 1)
    assert PdfText([rich, rich]).may_hide(1) is False
    assert PdfText([thin, rich, rich, rich, rich]).may_hide(1) is True


# PdfText


def test_page_at_maps_offsets_to_one_based_pages_and_skips_empty_pages():
    pdf = PdfText(["alpha beta gamma", "", "delta epsilon"])
    assert pdf.page_at(0) == 1
    assert pdf.page_at(len(pdf.pages[0]) - 1) == 1
    assert pdf.page_at(len(pdf.pages[0])) == 3
    assert pdf.page_at(len(pdf.text) - 1) == 3


# _find_in_order and _fuzzy_find


def test_find_in_order_returns_first_offset_only_when_all_fragments_follow():
    text = "abcdefghijkl"
    assert qc._find_in_order(["cd", "ij"], text) == 2
    assert qc._find_in_order(["ij", "cd"], text) is None
    assert qc._find_in_order(["zz"], text) is None


def test_fuzzy_find_returns_none_for_unrelated_text():
    assert qc._fuzzy_find("the mean difference was four points", "zzzz" * 40) is None


def test_fuzzy_find_locates_a_passage_near_the_start_of_the_text():
    needle = "the mean difference was four points"
    assert qc._fuzzy_find(needle, needle + " and more text follows here") == 0


def test_fuzzy_find_checks_each_passage_only_once_for_repeated_seeds(monkeypatch):
    calls = []
    real = SequenceMatcher

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(qc, "SequenceMatcher", spy)
    needle = "the mean difference was four points"
    assert qc._fuzzy_find(needle, "xxx" + needle + "yyy") == 3
    assert len(calls) == 1


def test_fuzzy_find_caps_the_number_of_candidate_passages(monkeypatch):
    calls = []
    real = SequenceMatcher

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(qc, "SequenceMatcher", spy)
    # The seed occurs at many well-separated places, none of which matches.
    text = ("abcdefgh" + "-" * 20) * 100
    needle = "abcdefgh" + "qrstuvwxyz0123456789"
    assert qc._fuzzy_find(needle, text) is None
    assert len(calls) == qc._MAX_CANDIDATES


# read_pdf_text


def _write_pdf(path: Path, pages: list[str]) -> None:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


def test_read_pdf_text_reads_the_text_layer_page_by_page(tmp_path):
    path = tmp_path / "paper.pdf"
    _write_pdf(path, ["The estimate is 42 for reading.", "Second page holds the result here."])
    pdf = read_pdf_text(path)
    assert pdf is not None
    assert pdf.pages == [normalize("The estimate is 42 for reading."),
                         normalize("Second page holds the result here.")]
    assert check_quote("second page holds the result here", pdf) == (VERIFIED, 2)


def test_read_pdf_text_of_a_pdf_without_pages_has_no_text(tmp_path):
    path = tmp_path / "empty.pdf"
    _write_pdf(path, [])
    pdf = read_pdf_text(path)
    assert pdf is not None and pdf.text == ""
    assert check_quote(BODY_QUOTE, pdf) == (NOT_CHECKED, None)


def test_unreadable_file_gives_no_text_layer(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    assert read_pdf_text(broken) is None
    assert read_pdf_text(tmp_path / "missing.pdf") is None


def test_pages_without_extractable_text_count_as_empty(monkeypatch):
    class FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        def __init__(self, path):
            self.pages = [FakePage(BODY), FakePage(None)]

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)
    pdf = read_pdf_text(Path("paper.pdf"))
    assert pdf is not None
    assert pdf.pages == [normalize(BODY), ""]
    assert pdf.may_hide(2) is True


# annotate_quote_checks


def _doc_for(monkeypatch, pdf):
    monkeypatch.setattr(qc, "read_pdf_text", lambda path: pdf)


def test_annotate_ignores_input_that_is_not_a_dict(monkeypatch):
    read = Mock()
    monkeypatch.setattr(qc, "read_pdf_text", read)
    annotate_quote_checks(["not", "a", "dict"], Path("paper.pdf"))
    annotate_quote_checks(None, Path("paper.pdf"))
    read.assert_not_called()


def test_annotate_skips_malformed_rows_and_cells(monkeypatch):
    _doc_for(monkeypatch, PdfText([BODY]))
    coded = {
        "r1": "malformed row",
        "r2": {"a": "malformed cell", "b": None, "c": {"quote": BODY_QUOTE}},
    }
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert coded == {
        "r1": "malformed row",
        "r2": {
            "a": "malformed cell",
            "b": None,
            "c": {"quote": BODY_QUOTE, "quote_check": VERIFIED, "quote_found_page": 1},
        },
    }


def test_cells_without_a_usable_quote_get_no_check_and_the_pdf_is_not_read(monkeypatch):
    read = Mock(return_value=PdfText([BODY]))
    monkeypatch.setattr(qc, "read_pdf_text", read)
    coded = {"r1": {
        "none": {"value": 1},
        "absent_quote": {"quote": None},
        "blank": {"quote": ""},
        "spaces": {"quote": "   "},
        "number": {"quote": 5},
        "list": {"quote": ["text"]},
    }}
    annotate_quote_checks(coded, Path("paper.pdf"))
    for cell in coded["r1"].values():
        assert "quote_check" not in cell
        assert "quote_found_page" not in cell
    read.assert_not_called()


def test_model_supplied_check_keys_are_removed_even_without_a_quote(monkeypatch):
    read = Mock(return_value=PdfText([BODY]))
    monkeypatch.setattr(qc, "read_pdf_text", read)
    coded = {"r1": {"a": {"value": 1, "quote_check": "verified", "quote_found_page": 3}}}
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert coded == {"r1": {"a": {"value": 1}}}
    read.assert_not_called()


def test_model_supplied_check_keys_are_replaced_when_there_is_a_quote(monkeypatch):
    _doc_for(monkeypatch, PdfText([BODY]))
    coded = {"r1": {"a": {"quote": UNRELATED, "quote_check": "verified", "quote_found_page": 3}}}
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert coded["r1"]["a"] == {"quote": UNRELATED, "quote_check": NOT_FOUND}


@pytest.mark.parametrize(('page', 'expected_named_page'), [
    (True, None),
    ("2", None),
    (2, 2),
])
def test_bool_or_string_page_is_ignored(monkeypatch, page, expected_named_page):
    seen = []
    real = qc.check_quote

    def spy(quote, document, named_page=None):
        seen.append(named_page)
        return real(quote, document, named_page)

    monkeypatch.setattr(qc, "check_quote", spy)
    _doc_for(monkeypatch, PdfText([BODY]))
    coded = {"r1": {"a": {"quote": BODY_QUOTE, "page": page}}}
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert seen == [expected_named_page]


def test_found_page_is_set_only_when_the_quote_is_found(monkeypatch):
    _doc_for(monkeypatch, PdfText([BODY]))
    coded = {"r1": {
        "found": {"quote": BODY_QUOTE},
        "missing": {"quote": UNRELATED},
    }}
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert coded["r1"]["found"] == {"quote": BODY_QUOTE, "quote_check": VERIFIED, "quote_found_page": 1}
    assert coded["r1"]["missing"] == {"quote": UNRELATED, "quote_check": NOT_FOUND}


def test_unreadable_pdf_marks_quoted_cells_not_checked(monkeypatch):
    _doc_for(monkeypatch, None)
    coded = {"r1": {"a": {"quote": BODY_QUOTE}}}
    annotate_quote_checks(coded, Path("paper.pdf"))
    assert coded["r1"]["a"] == {"quote": BODY_QUOTE, "quote_check": NOT_CHECKED}


def test_a_dropped_minus_sign_is_not_a_verified_quote():
    document = PdfText(["The effect was \u22120.5 points in the sample of first-year students."])
    assert normalize("\u22120.5") == normalize("-0.5") == normalize("\u2013.5".replace(".5", "0.5")) == "-0.5"
    assert check_quote("The effect was -0.5 points in the sample of first-year students", document) == ("verified", 1)
    status, _page = check_quote("The effect was 0.5 points in the sample of first-year students", document)
    assert status != "verified"
    assert normalize("self-\nreport") == normalize("selfreport")
