"""XML (JATS) article sources: upload checks, how each provider receives them,
the XML prompt note, identity matching from JATS front matter, and provenance.

Fixtures are bytes, never text written in text mode, so Windows keeps their LF
line endings and the bytes the tests compare are the bytes on disk.
"""
import io
import json
import threading
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from meta_coder import gemini, openai_compatible, openrouter, pdf_matching, web
from meta_coder.coding_sheet import CodingSheetRow, parse_coding_sheet_csv
from meta_coder.extraction import ProviderError, xml_article_text
from meta_coder.manual import parse_coding_manual
from meta_coder.pdf_matching import (
    PdfSignal,
    UnmatchedPaper,
    _cache_entry,
    _cache_source,
    cached_signal,
    pdf_signal,
    score_pair,
    suggest_matches,
)
from meta_coder.projects import ProjectError, create_project, write_manual
from meta_coder.prompts import XML_SOURCE_NOTE, build_extraction_prompt
from meta_coder.results import render_pdf_audit_yaml
from meta_coder.runner import Runner, raw_json_path
from meta_coder.sources import (
    JatsFront,
    SourceError,
    XmlChecker,
    decode_xml,
    jats_front,
    normalize_doi,
    source_format,
)
from meta_coder.uploads import list_uploaded_sources, safe_source_name, save_source_upload


JATS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD v1.2 20190208//EN" "JATS-archivearticle1.dtd">\n'
    b'<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">\n'
    b'<front>\n'
    b'<journal-meta><journal-id>psych</journal-id>'
    b'<article-id pub-id-type="doi">10.9999/journal.only</article-id></journal-meta>\n'
    b'<article-meta>\n'
    b'<article-id pub-id-type="pmid">123</article-id>\n'
    b'<article-id pub-id-type="doi">https://doi.org/10.1037/XGE0001234</article-id>\n'
    b'<title-group><article-title>Ego depletion &amp; the <italic>Stroop</italic> task</article-title></title-group>\n'
    b'<contrib-group><contrib><name><surname>M\xc3\xbcller</surname><given-names>A</given-names></name></contrib>'
    b'<contrib><name><surname>Nakamura</surname></name></contrib><contrib><name><surname> </surname></name></contrib></contrib-group>\n'
    b'<pub-date pub-type="collection"><season>Spring</season></pub-date>\n'
    b'<pub-date pub-type="epub"><day>3</day><year>2021</year></pub-date>\n'
    b'</article-meta>\n'
    b'</front>\n'
    b'<body><sec><title>Results</title><p>Depletion reduced accuracy, d = 0.42.</p>'
    b'<table-wrap><label>Table 2</label></table-wrap></sec></body>\n'
    b'</article>\n'
)
# Another publisher's schema: no JATS <front>, so matching reads the opening text.
NON_JATS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response><coredata><dc:title>Ostracism and aggression</dc:title>'
    b'<dc:creator>Williams</dc:creator><prism:coverDate>2018-04-01</prism:coverDate></coredata>'
    b'<originalText>Results: ostracism increased aggression.</originalText></full-text-retrieval-response>\n'
)

MANUAL = parse_coding_manual('effect_definition: treatment versus control\neffects:\n  estimate:\n    type: number\n')
ROWS = [CodingSheetRow(row_id='r1', source_pdf='study.xml', locator='Study 1')]


def reply(row_id='r1'):
    return json.dumps({'effects': [{'row_id': row_id, 'estimate': {'value': 0.42, 'evidence': 'Results > Study 1'},
                                    'notes': {'value': '', 'evidence': ''}}]})


# --- sources.py -----------------------------------------------------------------

def test_source_format_by_suffix():
    assert [source_format(name) for name in ('a.pdf', 'b.XML', 'c.Pdf', 'd.txt', 'xml')] == ['pdf', 'xml', 'pdf', None, None]


@pytest.mark.parametrize('data, message', [
    (b'', 'is empty'),
    (b'  \n\t', 'is empty'),
    (b'\xef\xbb\xbf', 'is empty'),
    (b'<article>\xff</article>', 'not UTF-8'),
    (b'<a>\x00</a>', 'not a text file'),
    (b'%PDF-1.7 <xml>', "should start with '<'"),
])
def test_unusable_xml_is_rejected_with_a_fix(data, message):
    with pytest.raises(SourceError, match=message):
        decode_xml(data, 'study.xml')


def test_xml_is_decoded_unchanged_except_for_a_bom():
    assert decode_xml(b'\xef\xbb\xbf\n <a>\xc3\xa9</a>\n', 'x.xml') == '\n <a>\xe9</a>\n'


def test_xml_checker_handles_characters_split_across_chunks():
    checker = XmlChecker('x.xml')
    data = '<a>Müller</a>'.encode('utf-8')
    split = data.index(b'\xc3') + 1
    assert checker.feed(b'') == ''
    text = checker.feed(data[:split]) + checker.feed(data[split:]) + checker.feed(b'', final=True)
    assert text == '<a>Müller</a>'


def test_jats_front_reads_article_meta_not_journal_meta():
    front = jats_front(JATS.decode('utf-8'))
    assert front == JatsFront(
        title='Ego depletion & the Stroop task', doi='10.1037/xge0001234', year='2021', surnames=('Müller', 'Nakamura'),
    )


def test_jats_front_without_article_meta_or_front():
    bare = '<article><front><article-title>Only a title</article-title></front></article>'
    assert jats_front(bare) == JatsFront(title='Only a title')
    assert jats_front('<article><body/></article>') == JatsFront()


@pytest.mark.parametrize('value, expected', [
    (' https://doi.org/10.1/ABC ', '10.1/abc'), ('http://dx.doi.org/10.1/x', '10.1/x'),
    ('doi: 10.1/Y', '10.1/y'), ('10.1/z', '10.1/z'), ('', ''),
])
def test_normalize_doi(value, expected):
    assert normalize_doi(value) == expected


def test_xml_article_text_turns_bad_files_into_provider_errors(tmp_path):
    good = tmp_path / 'good.xml'
    good.write_bytes(JATS)
    assert xml_article_text(good) == JATS.decode('utf-8')
    bad = tmp_path / 'bad.xml'
    bad.write_bytes(b'\xff\xfe<\x00')
    with pytest.raises(ProviderError, match='not UTF-8'):
        xml_article_text(bad)


# --- uploads --------------------------------------------------------------------

def test_xml_upload_is_saved_and_listed_with_pdfs(tmp_path):
    saved = save_source_upload(tmp_path, 'Study.XML', io.BytesIO(JATS))
    assert saved.name == 'Study.XML' and saved.read_bytes() == JATS
    (tmp_path / 'a.pdf').write_bytes(b'%PDF')
    (tmp_path / 'notes.txt').write_bytes(b'x')
    # Windows sorts paths case-insensitively, so compare names, not order.
    assert {path.name for path in list_uploaded_sources(tmp_path)} == {'Study.XML', 'a.pdf'}
    assert safe_source_name('dir/paper.xml') == 'paper.xml'
    with pytest.raises(ProjectError, match='not a PDF or XML file'):
        safe_source_name('notes.txt')
    with pytest.raises(ProjectError, match='PDF or XML filename is required'):
        safe_source_name(' . ')


@pytest.mark.parametrize('data, message', [
    (b'', 'is empty'), (b'not xml', "start with '<'"), (b'<a>\xff</a>', 'not UTF-8'), (b'<a>\x00</a>', 'not a text file'),
])
def test_bad_xml_upload_is_rejected_and_leaves_nothing_behind(tmp_path, data, message):
    with pytest.raises(ProjectError, match=message):
        save_source_upload(tmp_path, 'study.xml', io.BytesIO(data))
    assert list(tmp_path.iterdir()) == []


# --- prompts --------------------------------------------------------------------

def test_xml_prompt_is_the_pdf_prompt_plus_the_location_note():
    pdf = build_extraction_prompt(MANUAL, ROWS)
    assert XML_SOURCE_NOTE not in pdf and 'page numbers' not in pdf
    xml = build_extraction_prompt(MANUAL, ROWS, source_format='xml')
    assert xml == pdf + '\n\n' + XML_SOURCE_NOTE
    assert 'section heading path' in XML_SOURCE_NOTE and 'not its XML markup' in XML_SOURCE_NOTE


# --- provider adapters ----------------------------------------------------------

@pytest.fixture
def xml_source(tmp_path):
    path = tmp_path / 'study.xml'
    path.write_bytes(JATS)
    return path


def capture(monkeypatch, module, body):
    sent = []

    def transport(request, *, timeout, cancel_event):
        sent.append(json.loads(request.data))
        return json.dumps(body).encode()

    monkeypatch.setattr(module, 'cancellable_urlopen', transport)
    return sent


def test_gemini_sends_xml_as_a_text_part_with_no_upload(monkeypatch, xml_source):
    sent = capture(monkeypatch, gemini, {'candidates': [{'content': {'parts': [{'text': reply()}]}, 'finishReason': 'STOP'}]})
    result = gemini.extract_pdf_effects(pdf_path=xml_source, manual=MANUAL, rows=ROWS, api_key='key')
    assert result.status == 'ok' and result.source_pdf == 'study.xml'
    parts = sent[0]['contents'][0]['parts']
    assert parts[1] == {'text': JATS.decode('utf-8')}
    assert len(parts) == 2 and 'inline_data' not in json.dumps(sent[0])
    assert 'article XML in the next part' in parts[0]['text'] and parts[0]['text'].endswith(XML_SOURCE_NOTE)


def test_openrouter_sends_xml_as_one_text_part(monkeypatch, xml_source):
    sent = capture(monkeypatch, openrouter, {'choices': [{'message': {'content': reply()}, 'finish_reason': 'stop'}]})
    result = openrouter.extract_pdf_effects(pdf_path=xml_source, manual=MANUAL, rows=ROWS, api_key='key')
    assert result.status == 'ok'
    content = sent[0]['messages'][0]['content']
    assert len(content) == 1 and content[0]['type'] == 'text'
    assert content[0]['text'].endswith('\n\nArticle XML:\n' + JATS.decode('utf-8'))
    assert XML_SOURCE_NOTE in content[0]['text'] and 'plugins' not in sent[0]
    assert 'Return only JSON matching this schema' in content[0]['text']


def test_openai_compatible_sends_raw_xml_text(monkeypatch, xml_source):
    sent = capture(monkeypatch, openai_compatible, {'choices': [{'message': {'content': reply()}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(openai_compatible, 'pdf_text', Mock(side_effect=AssertionError('PDF text extraction used')))
    result = openai_compatible.extract_pdf_effects(
        pdf_path=xml_source, manual=MANUAL, rows=ROWS, api_key='key', model='local', base_url='http://127.0.0.1:1/v1',
    )
    assert result.status == 'ok'
    message = sent[0]['messages'][0]['content']
    assert 'from the article XML below' in message and XML_SOURCE_NOTE in message
    assert '\n\nArticle XML:\n' + JATS.decode('utf-8') in message
    assert 'Return only JSON matching this schema' in message


@pytest.mark.parametrize('module, extra', [
    (gemini, {}), (openrouter, {}), (openai_compatible, {'model': 'm', 'base_url': 'http://127.0.0.1:1/v1'}),
])
def test_unusable_xml_fails_before_any_request(monkeypatch, tmp_path, module, extra):
    bad = tmp_path / 'bad.xml'
    bad.write_bytes(b'\x00\x01')
    request = Mock()
    monkeypatch.setattr(module, 'cancellable_urlopen', request)
    result = module.extract_pdf_effects(pdf_path=bad, manual=MANUAL, rows=ROWS, api_key='key', **extra)
    assert result.status == 'error' and 'not a text file' in result.error
    request.assert_not_called()


# --- identity matching ----------------------------------------------------------

def test_xml_signal_uses_jats_front_matter_and_never_grobid(monkeypatch, tmp_path):
    path = tmp_path / 'download-1.xml'
    path.write_bytes(JATS)
    grobid = Mock()
    monkeypatch.setattr(pdf_matching, 'grobid_signal', grobid)
    signal = pdf_signal(path, grobid_url='http://grobid.invalid')
    grobid.assert_not_called()
    assert signal.doi == '10.1037/xge0001234' and signal.year == '2021'
    assert {'müller', 'nakamura', 'stroop', 'depletion', 'download'} <= set(signal.tokens.split())
    # The article body is not part of the signal, only the front matter.
    assert 'accuracy' not in signal.tokens.split()


def test_non_jats_xml_falls_back_to_opening_text(tmp_path):
    path = tmp_path / 'paper.xml'
    path.write_bytes(NON_JATS)
    signal = pdf_signal(path)
    assert signal.doi == '' and signal.year == '2018'
    assert {'williams', 'ostracism', 'aggression'} <= set(signal.tokens.split())
    assert 'coredata' not in signal.tokens.split()


def test_unreadable_xml_signal_falls_back_to_the_filename(tmp_path):
    path = tmp_path / 'smith_2019.xml'
    path.write_bytes(b'\xff\xfe')
    assert pdf_signal(path) == PdfSignal(tokens='2019 smith', year='2019', doi='')


def test_same_doi_is_a_certain_match_and_a_different_doi_rules_it_out():
    signal = PdfSignal(tokens='depletion stroop smith 2021', year='2021', doi='10.1037/xge0001234')
    same = UnmatchedPaper(key='k', source_pdf='', authors='Someone Else', year='1999', locator='',
                          doi='https://doi.org/10.1037/XGE0001234')
    assert score_pair(same, signal) == 1.0
    other = UnmatchedPaper(key='k', source_pdf='smith2021.pdf', authors='Smith', year='2021', locator='',
                           title='Depletion Stroop', doi='10.1037/other')
    assert score_pair(other, signal) == pdf_matching.CONTRADICTORY_DOI_CAP
    no_doi = UnmatchedPaper(key='k', source_pdf='smith2021.pdf', authors='Smith', year='2021', locator='')
    assert score_pair(no_doi, signal) > pdf_matching.HIGH_CONFIDENCE


def test_doi_from_jats_matches_an_unhelpfully_named_xml(tmp_path):
    xml = tmp_path / 'download-1.xml'
    xml.write_bytes(JATS)
    decoy = tmp_path / 'download-2.xml'
    decoy.write_bytes(NON_JATS)
    paper = UnmatchedPaper(key='doi:10.1037/xge0001234', source_pdf='', authors='', year='', locator='',
                           doi='10.1037/xge0001234')
    [suggestion] = suggest_matches([paper], [xml, decoy])
    assert (suggestion.filename, suggestion.confidence) == ('download-1.xml', 'high')


def test_xml_cache_entries_do_not_depend_on_grobid(tmp_path):
    path = tmp_path / 'study.xml'
    path.write_bytes(JATS)
    signal = pdf_signal(path)
    cache = {path.name: _cache_entry(path, signal, 'http://grobid')}
    assert cache[path.name]['source'] == _cache_source('', 'xml') == 'identity-v2:jats'
    assert cached_signal(path, cache) == cached_signal(path, cache, grobid_url='http://grobid') == signal
    # Entries written before DOIs were cached still load, with no DOI.
    del cache[path.name]['doi']
    assert cached_signal(path, cache).doi == ''


# --- runner and provenance ------------------------------------------------------

def test_mixed_run_codes_pdf_and_xml_and_records_the_source_format(tmp_path, monkeypatch):
    project = create_project('Mixed', root=tmp_path)
    write_manual(project, MANUAL)
    (project.sources_dir / 'a.pdf').write_bytes(b'%PDF-1.4 example')
    (project.sources_dir / 'b.xml').write_bytes(JATS)
    sheet_text = 'row_id,source_pdf,locator,authors,year\nr1,a.pdf,Study 1,Smith,2020\nr2,b.xml,Study 1,Müller,2021\n'
    project.coding_sheet_path.write_bytes(sheet_text.encode('utf-8'))
    sheet = parse_coding_sheet_csv(sheet_text, uploaded_filenames={'a.pdf', 'b.xml'})
    lock = threading.Lock()
    sent = []

    def transport(request, *, timeout, cancel_event):
        body = json.loads(request.data)
        with lock:
            sent.append(body)
        row_id = 'r1' if 'inline_data' in json.dumps(body) else 'r2'
        return json.dumps({'candidates': [{'content': {'parts': [{'text': reply(row_id)}]}, 'finishReason': 'STOP'}]}).encode()

    monkeypatch.setattr(gemini, 'cancellable_urlopen', transport)
    runner = Runner()
    runner.start(project=project, manual=MANUAL, coding_sheet=sheet, api_key='secret', provider='gemini', model='m')
    deadline = time.monotonic() + 10
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.01)
    state = runner.state(project.project_id)
    assert state.status == 'complete' and [p.status for p in state.pdfs] == ['ok', 'ok']
    assert len(sent) == 2

    raw = json.loads(raw_json_path(project, 'b.xml').read_bytes())
    assert (raw['source_pdf'], raw['source_format']) == ('b.xml', 'xml')
    assert json.loads(raw_json_path(project, 'a.pdf').read_bytes())['source_format'] == 'pdf'

    operations = [json.loads(path.read_bytes()) for path in (project.path / 'audit' / 'operations').glob('*/operation.json')]
    xml_ops = [op for op in operations if op['operation'] == 'extraction' and op['settings']['source_format'] == 'xml']
    assert len(xml_ops) == 1 and 'source/b.xml' in xml_ops[0]['inputs']
    stored = project.path / xml_ops[0]['inputs']['source/b.xml']['path']
    assert stored.read_bytes() == JATS

    coded = (project.output_dir / 'coded_data.csv').read_bytes().decode('utf-8')
    assert 'r2,b.xml' in coded and '0.42' in coded
    audit_yaml = render_pdf_audit_yaml(manual=MANUAL, source_pdf='b.xml', rows=sheet.rows_for_pdf('b.xml'),
                                       result=runner_result(project, 'b.xml'))
    assert 'source_format: xml' in audit_yaml


def runner_result(project, name):
    from meta_coder.runner import load_persisted_results
    return load_persisted_results(project)[name]


# --- web ------------------------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('XML study', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def test_xml_upload_route_accepts_xml_rejects_bad_files_and_deletes(site):
    client, project, runtime = site
    base = f'/test/projects/{project.project_id}'
    page = client.get(base)
    assert 'accept=".pdf,.xml,application/pdf,application/xml,text/xml"' in page.text
    assert 'Drop PDF or XML files here' in page.text
    response = client.post(base + '/uploads', files=[('files', ('study.xml', JATS)), ('files', ('bad.xml', b'plain text'))])
    assert response.status_code == 303 and 'error=' in response.headers['location']
    assert (project.sources_dir / 'study.xml').read_bytes() == JATS
    assert not (project.sources_dir / 'bad.xml').exists()
    [saved] = runtime.pdf_scanner.scan_async.call_args.args[1]
    assert saved.name == 'study.xml'
    assert '1 source uploaded' in client.get(base).text
    assert client.post(base + '/uploads/delete', data={'filename': 'study.xml'}).status_code == 303
    assert not (project.sources_dir / 'study.xml').exists()
