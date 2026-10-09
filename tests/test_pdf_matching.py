import time
from pathlib import Path
from unittest.mock import patch

from meta_coder.pdf_matching import (
    PdfScanner,
    _cache_source,
    apply_source_pdf_matches,
    cached_signal,
    grobid_signal,
    load_match_score_cache,
    load_signal_cache,
    matched_paper_ids,
    save_match_score_cache,
    score_pair,
    suggest_matches_from_score_cache,
    suggest_matches,
    unmatched_paper_ids,
    PdfSignal,
    UnmatchedPaper,
)
from meta_coder.projects import Project, create_project


def test_unmatched_paper_ids_groups_by_source_pdf():
    csv_text = (
        "row_id,source_pdf,locator,authors,year\n"
        "r1,smith2020.pdf,Exp 1,Smith,2020\n"
        "r2,smith2020.pdf,Exp 2,Smith,2020\n"
        "r3,ok.pdf,Exp 1,Jones,2019\n"
    )
    papers = unmatched_paper_ids(csv_text, uploaded_filenames={"ok.pdf"})
    assert len(papers) == 1
    assert papers[0].key == "smith2020.pdf"
    assert papers[0].source_pdf == "smith2020.pdf"
    assert papers[0].row_count == 2
    assert papers[0].authors == "Smith"


def test_unmatched_paper_ids_groups_blank_source_pdf_by_doi_then_title_then_authors():
    csv_text = (
        "row_id,source_pdf,locator,authors,year,title,doi\n"
        "r1,,Exp 1,Smith,2020,,10.1/x\n"
        "r2,,Exp 2,Smith,2020,,10.1/x\n"
        "r3,,Exp 1,Jones,2019,Some Title,\n"
        "r4,,Exp 1,Doe,2018,,\n"
    )
    papers = unmatched_paper_ids(csv_text, uploaded_filenames=set())
    keys = {p.key: p.row_count for p in papers}
    assert keys == {"doi:10.1/x": 2, "title:Some Title": 1, "authors:Doe|2018": 1}


def test_unmatched_paper_ids_skips_rows_with_no_identifying_field_at_all():
    csv_text = "row_id,source_pdf,locator,authors,year\nr1,,Exp 1,,\n"
    papers = unmatched_paper_ids(csv_text, uploaded_filenames=set())
    assert papers == []


def test_score_pair_prefers_matching_filename_and_year():
    paper = UnmatchedPaper(
        key="smith2020.pdf", source_pdf="smith2020.pdf", authors="Smith", year="2020", locator=""
    )
    good = PdfSignal(tokens="smith 2020 effects of x", year="2020")
    bad = PdfSignal(tokens="jones 2019 unrelated topic", year="2019")
    assert score_pair(paper, good) > score_pair(paper, bad)


def test_score_pair_matches_on_title_and_doi_when_source_pdf_is_blank():
    # The generically-named-PDF case with source_pdf blank too: nothing but
    # title/DOI to go on, exactly like a coding sheet where filenames were
    # never filled in.
    paper = UnmatchedPaper(
        key="doi:10.1037/a0017606",
        source_pdf="",
        authors="Bub",
        year="2010",
        locator="",
        title="Grasping beer mugs on the mechanics of natural attention",
        doi="https://doi.org/10.1037/a0017606",
    )
    right_signal = PdfSignal(
        tokens="bub masson 2010 grasping beer mugs mechanics natural attention "
        "doi 10 1037 a0017606",
        year="2010",
    )
    wrong_signal = PdfSignal(tokens="cho proctor 2011 unrelated spatial task", year="2011")
    assert score_pair(paper, right_signal) > score_pair(paper, wrong_signal)
    assert score_pair(paper, right_signal) >= 0.5


def test_pairwise_match_scores_persist_when_inputs_are_unchanged(tmp_path):
    project = create_project("Test", root=tmp_path)
    paper = UnmatchedPaper(
        key="smith2020.pdf", source_pdf="smith2020.pdf", authors="Smith", year="2020", locator="Exp 1"
    )
    pdf_path = project.sources_dir / "orphan.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    signal_cache = {
        pdf_path.name: {"mtime": int(pdf_path.stat().st_mtime), "size": pdf_path.stat().st_size, "tokens": "smith 2020", "year": "2020", "source": _cache_source("")}
    }
    score_cache = {}
    suggestions, changed = suggest_matches_from_score_cache(
        [paper], [pdf_path], signal_cache=signal_cache, score_cache=score_cache
    )
    assert changed
    save_match_score_cache(project, score_cache)

    restored_cache = load_match_score_cache(project)
    restored_suggestions, changed = suggest_matches_from_score_cache(
        [paper], [pdf_path], signal_cache=signal_cache, score_cache=restored_cache
    )
    assert restored_suggestions == suggestions
    assert not changed


def test_pairwise_match_scores_recompute_only_for_changed_inputs(tmp_path):
    project = create_project("Test", root=tmp_path)
    paper = UnmatchedPaper(
        key="smith2020.pdf", source_pdf="smith2020.pdf", authors="Smith", year="2020", locator="Exp 1"
    )
    pdf_path = project.sources_dir / "orphan.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    signal_cache = {
        pdf_path.name: {"mtime": int(pdf_path.stat().st_mtime), "size": pdf_path.stat().st_size, "tokens": "smith 2020", "year": "2020", "source": _cache_source("")}
    }
    score_cache = {}
    suggest_matches_from_score_cache([paper], [pdf_path], signal_cache=signal_cache, score_cache=score_cache)
    changed_paper = UnmatchedPaper(
        key="smith2020.pdf", source_pdf="smith2020.pdf", authors="Jones", year="2020", locator="Exp 1"
    )

    _, changed = suggest_matches_from_score_cache(
        [changed_paper], [pdf_path], signal_cache=signal_cache, score_cache=score_cache
    )
    assert changed


def test_suggest_matches_never_suggests_same_orphan_twice(tmp_path):
    a = tmp_path / "Smith_2020_effects.pdf"
    a.write_bytes(b"%PDF-1.4\n")
    b = tmp_path / "Jones_2019_other.pdf"
    b.write_bytes(b"%PDF-1.4\n")

    papers = [
        UnmatchedPaper(key="smith.pdf", source_pdf="smith.pdf", authors="Smith", year="2020", locator=""),
        UnmatchedPaper(
            key="smith_dup.pdf", source_pdf="smith_dup.pdf", authors="Smith", year="2020", locator=""
        ),
    ]
    suggestions = suggest_matches(papers, [a, b])
    filenames = [s.filename for s in suggestions if s.filename]
    assert len(filenames) == len(set(filenames))


def test_suggest_matches_returns_none_confidence_below_threshold(tmp_path):
    orphan = tmp_path / "zzz_unrelated_qqq.pdf"
    orphan.write_bytes(b"%PDF-1.4\n")
    papers = [
        UnmatchedPaper(
            key="totally_different_paper",
            source_pdf="totally_different_paper",
            authors="Nobody",
            year="1899",
            locator="",
        )
    ]
    suggestions = suggest_matches(papers, [orphan])
    assert suggestions[0].confidence == "none"
    assert suggestions[0].filename is None


def test_apply_source_pdf_matches_rewrites_only_mapped_rows(tmp_path):
    path = tmp_path / "coding_sheet.csv"
    path.write_text(
        "row_id,source_pdf,locator,authors,year\n"
        "r1,smith2020.pdf,Exp 1,Smith,2020\n"
        "r2,ok.pdf,Exp 1,Jones,2019\n",
        encoding="utf-8",
    )
    apply_source_pdf_matches(path, {"smith2020.pdf": "Smith_2020_effects.pdf"})
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[1] == "r1,Smith_2020_effects.pdf,Exp 1,Smith,2020"
    assert rows[2] == "r2,ok.pdf,Exp 1,Jones,2019"


def test_apply_source_pdf_matches_rewrites_blank_source_pdf_rows_by_doi(tmp_path):
    path = tmp_path / "coding_sheet.csv"
    path.write_text(
        "row_id,source_pdf,locator,authors,year,title,doi\n"
        "r1,,Exp 1,Smith,2020,,10.1/x\n"
        "r2,,Exp 1,Jones,2019,,10.1/y\n",
        encoding="utf-8",
    )
    apply_source_pdf_matches(path, {"doi:10.1/x": "Smith_2020.pdf"})
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[1] == "r1,Smith_2020.pdf,Exp 1,Smith,2020,,10.1/x"
    assert rows[2] == "r2,,Exp 1,Jones,2019,,10.1/y"


def test_matched_paper_ids_groups_saved_matches_and_unmatch_clears_them(tmp_path):
    path = tmp_path / "coding_sheet.csv"
    path.write_text(
        "row_id,source_pdf,locator,authors,year,title\n"
        "r1,Smith_2020.pdf,Exp 1,Smith,2020,Effects\n"
        "r2,Smith_2020.pdf,Exp 2,Smith,2020,Effects\n",
        encoding="utf-8",
    )

    match = matched_paper_ids(path.read_text(encoding="utf-8"), uploaded_filenames={"Smith_2020.pdf"})[0]
    assert (match.source_pdf, match.authors, match.year, match.row_count) == ("Smith_2020.pdf", "Smith", "2020", 2)

    apply_source_pdf_matches(path, {"Smith_2020.pdf": ""})

    assert matched_paper_ids(path.read_text(encoding="utf-8"), uploaded_filenames={"Smith_2020.pdf"}) == []


def test_suggests_generically_named_pdf_by_first_page_text(tmp_path):
    # A real-world case: the uploaded PDF's filename ("1.pdf.pdf") carries no
    # author/year info at all, so the match has to come from scanning the
    # first page's text (title, byline, citation) rather than the filename.
    orphan = tmp_path / "1.pdf.pdf"
    orphan.write_bytes(b"%PDF-synthetic")
    papers = unmatched_paper_ids(
        "row_id,source_pdf,locator,authors,year\n"
        "1_1_A,Ambrosecchia 2015.pdf,Exp 1,Ambrosecchia et al.,2015\n",
        uploaded_filenames={orphan.name},
    )
    assert len(papers) == 1

    with patch(
        "meta_coder.pdf_matching.pdf_text",
        return_value="[Page 1]\nAmbrosecchia et al. 2015 original research",
    ):
        suggestions = suggest_matches(papers, [orphan])
    assert suggestions[0].filename == "1.pdf.pdf"
    assert suggestions[0].confidence == "high"


def test_cached_signal_from_real_pdf_survives_round_trip_without_reparsing(tmp_path):
    # Proves the actual claim behind caching: a real first-page-text signal,
    # once scanned, is enough on its own for a correct high-confidence match —
    # with `pdf_text` never called again, i.e. the expensive parse really is
    # skipped, not just skipped-looking.
    project = _make_project(tmp_path)
    orphan = tmp_path / "1.pdf.pdf"
    orphan.write_bytes(b"%PDF-synthetic")

    scanner = PdfScanner()
    with patch(
        "meta_coder.pdf_matching.pdf_text",
        return_value="[Page 1]\nAmbrosecchia et al. 2015 original research",
    ):
        scanner.scan_async(project, [orphan])
        for _ in range(500):
            if not scanner.is_running(project.project_id):
                break
            time.sleep(0.01)
    assert not scanner.is_running(project.project_id)
    cache = load_signal_cache(project)

    papers = unmatched_paper_ids(
        "row_id,source_pdf,locator,authors,year\n"
        "1_1_A,Ambrosecchia 2015.pdf,Exp 1,Ambrosecchia et al.,2015\n",
        uploaded_filenames={orphan.name},
    )
    with patch("meta_coder.pdf_matching.pdf_text", side_effect=AssertionError("should not reparse")):
        suggestions = suggest_matches(papers, [orphan], cache=cache)
    assert suggestions[0].filename == "1.pdf.pdf"
    assert suggestions[0].confidence == "high"


def test_grobid_signal_falls_back_cleanly_on_connection_error(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    with patch("meta_coder.pdf_matching.urllib.request.urlopen", side_effect=OSError("no route")):
        assert grobid_signal(path, "http://localhost:8070") is None


def _make_project(tmp_path: Path) -> Project:
    path = tmp_path / "proj"
    path.mkdir()
    return Project(project_id="a" * 16, name="Test", path=path, created_at="2024-01-01T00:00:00")


def test_pdf_scanner_populates_cache_and_suggest_matches_skips_reparsing(tmp_path):
    project = _make_project(tmp_path)
    pdf = tmp_path / "smith2020.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    scanner = PdfScanner()
    scanner.scan_async(project, [pdf])
    for _ in range(200):
        if not scanner.is_running(project.project_id):
            break
        time.sleep(0.01)
    assert not scanner.is_running(project.project_id)

    state = scanner.state(project.project_id)
    assert state.status == "complete"
    assert state.processed == 1

    cache = load_signal_cache(project)
    assert "smith2020.pdf" in cache

    with patch("meta_coder.pdf_matching.pdf_text") as mock_pdf_text:
        signal = cached_signal(pdf, cache)
        assert signal is not None
        mock_pdf_text.assert_not_called()


def test_cached_signal_misses_when_file_changes(tmp_path):
    project = _make_project(tmp_path)
    pdf = tmp_path / "smith2020.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    scanner = PdfScanner()
    scanner.scan_async(project, [pdf])
    for _ in range(200):
        if not scanner.is_running(project.project_id):
            break
        time.sleep(0.01)

    cache = load_signal_cache(project)
    assert cached_signal(pdf, cache) is not None

    time.sleep(1.05)  # cache stores integer-second mtimes
    pdf.write_bytes(b"%PDF-1.4\nmore\n")
    assert cached_signal(pdf, cache) is None


def test_suggest_matches_uses_cache_when_provided(tmp_path):
    project = _make_project(tmp_path)
    pdf = tmp_path / "Smith_2020_effects.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    scanner = PdfScanner()
    scanner.scan_async(project, [pdf])
    for _ in range(200):
        if not scanner.is_running(project.project_id):
            break
        time.sleep(0.01)
    cache = load_signal_cache(project)

    papers = [
        UnmatchedPaper(key="smith.pdf", source_pdf="smith.pdf", authors="Smith", year="2020", locator="")
    ]
    with patch("meta_coder.pdf_matching.pdf_text") as mock_pdf_text:
        suggestions = suggest_matches(papers, [pdf], cache=cache)
        mock_pdf_text.assert_not_called()
    assert suggestions[0].filename == "Smith_2020_effects.pdf"
