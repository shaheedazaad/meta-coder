"""Identity evidence, contradictory years and ambiguity in PDF match scoring."""
import pytest

from meta_coder import pdf_matching as matching


def paper(authors='Li', year='2020', key='p', title=''):
    return matching.UnmatchedPaper(key=key, source_pdf='', authors=authors, year=year, locator='', title=title, row_count=1)


def suggest(papers, scores):
    signals = {name: matching.PdfSignal('', None) for _, name in scores}
    return {s.paper.key: s for s in matching._suggestions_from_scores(papers, signals, scores)}


def test_short_and_unicode_surnames_are_identity_tokens():
    assert matching._short_name_tokens('Li, Wu & 王 Müller') == {'li', 'wu', '王'}
    assert 'müller' in matching._significant_tokens('Müller')
    assert matching.score_pair(paper(authors='Li'), matching.PdfSignal('li 2020', '2020')) == 1


@pytest.mark.parametrize('authors', ['Smith JA, Jones MB', 'Smith Jr', 'De Vries', 'Smith, α', 'Smith et Al'])
def test_initials_particles_and_symbols_are_not_short_names(authors):
    assert matching._short_name_tokens(authors) == set()


def test_short_title_words_are_not_evidence():
    title_only = paper(authors='', year='', title='Do we go')
    assert matching.score_pair(title_only, matching.PdfSignal('do we go', None)) == 0
    assert matching._signal_tokens('Do we see 王 α et al 2020') == {'do', 'we', 'see', '王', '2020'}


def test_short_names_get_no_fuzzy_credit():
    li = paper(authors='Li Smith', year='')
    assert matching.score_pair(li, matching.PdfSignal('lin smith', None)) == 0.5


def test_unicode_names_split_from_years():
    assert matching._normalize('Müller2020') == 'müller 2020'
    assert matching._find_year('x12019 ٢٠٢٠ 2020a') == '2020'


def test_year_alone_cannot_identify_a_pdf():
    assert matching.score_pair(paper(authors=''), matching.PdfSignal('2020', '2020')) == 0


def test_contradictory_year_prevents_high_confidence():
    assert matching.score_pair(paper(), matching.PdfSignal('li 2020 2019', '2019')) == matching.CONTRADICTORY_YEAR_CAP


def test_year_suffix_is_not_a_contradiction():
    assert matching.score_pair(paper(year='2020a'), matching.PdfSignal('li 2020', '2020')) == 1
    assert matching.score_pair(paper(year='in press'), matching.PdfSignal('li press', '2019')) == 1


def test_equal_candidates_are_ambiguous():
    suggestions = suggest([paper()], {('p', 'a.pdf'): 1., ('p', 'b.pdf'): 1.})
    assert suggestions['p'].confidence == 'low' and suggestions['p'].filename == 'a.pdf'


def test_clear_lead_is_not_ambiguous():
    suggestions = suggest([paper()], {('p', 'a.pdf'): .95, ('p', 'b.pdf'): .85})
    assert suggestions['p'].confidence == 'high'


def test_pdf_claimed_by_a_better_fitting_paper_is_not_a_competitor():
    # Same author and year; B's title pins b.pdf, which leaves a.pdf for A.
    scores = {('a', 'a.pdf'): .9, ('a', 'b.pdf'): .9, ('b', 'a.pdf'): .6, ('b', 'b.pdf'): 1.}
    suggestions = suggest([paper(key='a'), paper(key='b')], scores)
    assert (suggestions['a'].filename, suggestions['a'].confidence) == ('a.pdf', 'high')
    assert (suggestions['b'].filename, suggestions['b'].confidence) == ('b.pdf', 'high')


def test_two_papers_fitting_two_pdfs_equally_are_both_ambiguous():
    scores = {(key, name): 1. for key in 'ab' for name in ('a.pdf', 'b.pdf')}
    suggestions = suggest([paper(key='a'), paper(key='b')], scores)
    assert {s.confidence for s in suggestions.values()} == {'low'}


def test_two_papers_competing_for_one_pdf_are_ambiguous():
    suggestions = suggest([paper(key='a'), paper(key='b')], {('a', 'x.pdf'): 1., ('b', 'x.pdf'): .95})
    assert suggestions['a'].confidence == 'low' and suggestions['b'].confidence == 'none'


def test_low_and_none_suggestions_are_not_upgraded_or_checked():
    suggestions = suggest([paper(key='a'), paper(key='b')], {('a', 'x.pdf'): .3, ('b', 'x.pdf'): .3})
    assert suggestions['a'].confidence == 'low' and suggestions['b'].confidence == 'none'


def test_signals_cached_before_the_identity_rules_are_rescanned(tmp_path):
    path = tmp_path / 'li2020.pdf'
    path.write_bytes(b'%PDF')
    stat = path.stat()
    old = {'mtime': int(stat.st_mtime), 'size': stat.st_size, 'tokens': '2020', 'year': '2020'}
    for source in ('local', 'grobid'):
        assert matching.cached_signal(path, {path.name: {**old, 'source': source}}) is None
    current = matching._cache_entry(path, matching.PdfSignal('li 2020', '2020'), '')
    assert matching.cached_signal(path, {path.name: current}) == matching.PdfSignal('li 2020', '2020')
