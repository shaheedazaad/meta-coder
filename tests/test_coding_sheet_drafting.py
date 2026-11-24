import asyncio
import csv
import io
import json
from urllib.parse import urlencode

import pytest

from meta_coder import providers
from meta_coder.coding_sheet_drafting import (
    COLUMNS, CodingSheetDraftError, build_sheet_draft_prompt,
    parse_sheet_draft_response, read_source_csv, validate_sheet_csv,
)
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual
from meta_coder.web import create_app


def model_reply(**changes):
    row = dict.fromkeys(COLUMNS, '')
    row.update(row_id='effect-1', authors='Smith, Lee', year='2024', locator='Experiment 1', source_rows=[1])
    row.update(changes)
    return json.dumps({'rows': [row], 'warnings': ['Moderator columns omitted.']})


def test_source_csv_retains_quoted_cells_bom_and_semicolon_delimiter():
    source = read_source_csv(io.BytesIO('\ufeffCitation;Location\n"Smith; Lee";"Experiment 1"\n'.encode()), 'sheet.CSV', max_bytes=1024)
    assert source['headers'] == ['Citation', 'Location']
    assert source['rows'] == [{'source_row': 1, 'cells': ['Smith; Lee', 'Experiment 1']}]


@pytest.mark.parametrize('data,name,message', [
    (b'x', 'sheet.xlsx', 'CSV'),
    (b'header\n', 'sheet.csv', 'at least one'),
    (b'\xff', 'sheet.csv', 'UTF-8'),
    (b'header\n"unclosed', 'sheet.csv', 'could not be read'),
])
def test_rejects_unreadable_sources(data, name, message):
    with pytest.raises(CodingSheetDraftError, match=message):
        read_source_csv(io.BytesIO(data), name, max_bytes=1024)


def test_source_upload_limit():
    with pytest.raises(CodingSheetDraftError, match='upload limit'):
        read_source_csv(io.BytesIO(b'header\nvalue'), 'sheet.csv', max_bytes=5)


def test_prompt_includes_saved_manual_notes_and_source_without_losing_unicode():
    prompt = build_sheet_draft_prompt({'rows': []}, manual='Compare Δ RT', notes='Split experiments', filenames=['Smith.pdf'])
    assert 'Compare Δ RT' in prompt and 'Split experiments' in prompt and 'Smith.pdf' in prompt
    with pytest.raises(CodingSheetDraftError, match='notes'):
        build_sheet_draft_prompt({}, manual='', notes='x' * 10001, filenames=[])


def test_draft_csv_quoting_and_missing_pdf_warning():
    result = parse_sheet_draft_response(model_reply(), source_row_count=1)
    rows = list(csv.DictReader(io.StringIO(result['csv'])))
    assert rows[0]['authors'] == 'Smith, Lee'
    assert len(rows) == 1
    assert result['row_count'] == result['source_row_count'] == 1
    assert any('source matching' in warning for warning in result['warnings'])
    validate_sheet_csv(result['csv'], require_complete=True)


@pytest.mark.parametrize('changes,message', [
    ({'source_rows': []}, 'traced'),
    ({'source_rows': [2]}, 'traced'),
    ({'source_rows': [True]}, 'traced'),
    ({'year': 2024}, 'cells'),
    ({'row_id': ''}, 'unique'),
])
def test_rejects_bad_model_rows(changes, message):
    with pytest.raises(CodingSheetDraftError, match=message):
        parse_sheet_draft_response(model_reply(**changes), source_row_count=1)


def test_rejects_dropped_rows_and_duplicate_ids():
    with pytest.raises(CodingSheetDraftError, match='omitted source rows'):
        parse_sheet_draft_response(model_reply(), source_row_count=2)
    payload = json.loads(model_reply())
    payload['rows'].append({**payload['rows'][0], 'source_rows': [2]})
    with pytest.raises(CodingSheetDraftError, match='unique'):
        parse_sheet_draft_response(json.dumps(payload), source_row_count=2)


def test_missing_bibliography_is_reviewable_but_cannot_be_saved():
    draft = parse_sheet_draft_response(model_reply(authors=''), source_row_count=1)
    assert any('missing authors' in warning for warning in draft['warnings'])
    with pytest.raises(CodingSheetDraftError, match='Fill it in'):
        validate_sheet_csv(draft['csv'], require_complete=True)


@pytest.mark.parametrize('provider', providers.PROVIDERS)
def test_sheet_uses_shared_drafting_provider(provider, monkeypatch):
    captured = {}
    def generate(**kwargs):
        captured.update(kwargs)
        return model_reply()
    monkeypatch.setattr(getattr(providers, provider), 'generate_structured_text', generate)
    draft = providers.draft_coding_sheet(provider=provider, source={'rows': [{'source_row': 1}]},
        manual='Effect definition', notes='Use Citation', filenames=[], api_key='key', model='manual-model',
        base_url='http://localhost:8080/v1', response_format='json_object')
    assert draft['row_count'] == 1
    assert captured['model'] == 'manual-model'
    assert 'Use Citation' in captured['prompt']
    assert captured['response_schema']['type'] == ('OBJECT' if provider == 'gemini' else 'object')
    if provider == 'openai_compatible':
        assert captured['base_url'] == 'http://localhost:8080/v1'
        assert captured['response_format'] == 'json_object'


def request(app, path, *, body=b'', content_type=b'application/x-www-form-urlencoded', method='POST'):
    async def run():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path,
            'query_string': b'', 'root_path': '', 'headers': [(b'host', b'localhost'),
                (b'content-type', content_type)], 'server': ('localhost', 80)}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start'), b''.join(m.get('body', b'') for m in messages).decode()
    return asyncio.run(run())


@pytest.fixture
def project_app(tmp_path, monkeypatch, drafting_extractor):
    project = create_project('Sheet conversion', root=tmp_path / 'projects')
    write_manual(project, parse_coding_manual('effect_definition: Compare RT\neffects:\n  age: {type: number}\n'))
    project.coding_sheet_path.write_text('original sheet')
    (project.output_dir / 'coded_data.csv').write_text('original results')
    app = create_app(token='test', projects_root=tmp_path / 'projects')
    app.state.runtime.vault._keys = {drafting_extractor.id: 'test-key'}
    return project, app


def test_upload_draft_preserves_saved_files_and_passes_notes_and_manual(project_app, monkeypatch):
    project, app = project_app
    captured = {}
    def draft(**kwargs):
        captured.update(kwargs)
        return parse_sheet_draft_response(model_reply(), source_row_count=1)
    monkeypatch.setattr('meta_coder.web.draft_coding_sheet', draft)
    body = (b'--boundary\r\nContent-Disposition: form-data; name="notes"\r\n\r\nUse Citation\r\n'
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="input.csv"\r\n'
            b'Content-Type: text/csv\r\n\r\nCitation,Experiment\nSmith 2024,1\r\n--boundary--\r\n')
    status, result = request(app, f'/test/projects/{project.project_id}/coding-sheet/draft', body=body,
                             content_type=b'multipart/form-data; boundary=boundary')
    assert status == 200, result
    assert json.loads(result)['row_count'] == 1
    assert captured['notes'] == 'Use Citation' and 'Compare RT' in captured['manual']
    assert captured['source']['rows'][0]['cells'] == ['Smith 2024', '1']
    assert project.coding_sheet_path.read_text() == 'original sheet'
    assert (project.output_dir / 'coded_data.csv').read_text() == 'original results'


def test_save_validates_before_replacing_and_blocks_active_runs(project_app, monkeypatch):
    project, app = project_app
    url = f'/test/projects/{project.project_id}/coding-sheet/draft/save'
    status, _ = request(app, url, body=urlencode({'csv_text': 'bad csv'}).encode())
    assert status == 400
    assert project.coding_sheet_path.read_text() == 'original sheet'
    text = parse_sheet_draft_response(model_reply(), source_row_count=1)['csv']
    monkeypatch.setattr('meta_coder.web.Runner.is_running', lambda *a: True)
    status, _ = request(app, url, body=urlencode({'csv_text': text}).encode())
    assert status == 409
    assert project.coding_sheet_path.read_text() == 'original sheet'
    monkeypatch.setattr('meta_coder.web.Runner.is_running', lambda *a: False)
    status, result = request(app, url, body=urlencode({'csv_text': text}).encode())
    assert status == 200, result
    assert project.coding_sheet_path.read_text() == text
    assert not (project.output_dir / 'coded_data.csv').exists()


def test_project_page_exposes_notes_and_explicit_conversion_and_save(project_app):
    project, app = project_app
    status, body = request(app, f'/test/projects/{project.project_id}', method='GET')
    assert status == 200
    assert 'id="sheet-draft-notes"' in body
    assert 'Convert to a draft' in body and 'Save converted sheet' in body


def test_binary_and_oversized_csv_text_are_rejected(monkeypatch):
    from meta_coder import coding_sheet_drafting as drafting
    with pytest.raises(CodingSheetDraftError, match='binary data'):
        read_source_csv(io.BytesIO(b'header\n\x00'), 'sheet.csv', max_bytes=100)
    monkeypatch.setattr(drafting, 'MAX_TEXT_CHARS', 10)
    with pytest.raises(CodingSheetDraftError, match='too large'):
        read_source_csv(io.BytesIO(b'header\nvalue'), 'sheet.csv', max_bytes=100)
    with pytest.raises(CodingSheetDraftError, match='converted CSV is too large'):
        validate_sheet_csv('x' * 11)


def test_source_row_limit(monkeypatch):
    from meta_coder import coding_sheet_drafting as drafting
    monkeypatch.setattr(drafting, 'MAX_ROWS', 1)
    with pytest.raises(CodingSheetDraftError, match='at most 1'):
        read_source_csv(io.BytesIO(b'name\na\nb'), 'sheet.csv', max_bytes=100)


@pytest.mark.parametrize('body', ['', 'r1,paper.pdf', 'r1,paper.pdf,here,Smith,2024,title,doi,extra', '"unclosed'])
def test_preview_rejects_missing_rows_wrong_cell_count_and_bad_quotes(body):
    with pytest.raises(CodingSheetDraftError):
        validate_sheet_csv(','.join(COLUMNS) + '\n' + body)


@pytest.mark.parametrize('payload', [[], {}, {'rows': []}, {'rows': [], 'warnings': 'bad'}, {'rows': [], 'warnings': [1]}])
def test_invalid_draft_envelopes(payload):
    with pytest.raises(CodingSheetDraftError):
        parse_sheet_draft_response(json.dumps(payload), source_row_count=1)


def test_unrepairable_draft_json_is_reported(monkeypatch):
    from meta_coder import coding_sheet_drafting as drafting
    from meta_coder.extraction import ProviderError
    def fail(_):
        raise ProviderError('invalid JSON')
    monkeypatch.setattr(drafting, 'parse_json_response', fail)
    with pytest.raises(CodingSheetDraftError, match='unreadable coding sheet'):
        parse_sheet_draft_response('{', source_row_count=1)


def test_complete_draft_with_pdf_needs_no_extra_warnings():
    draft = parse_sheet_draft_response(model_reply(source_pdf='paper.pdf'), source_row_count=1)
    assert draft['warnings'] == ['Moderator columns omitted.']
