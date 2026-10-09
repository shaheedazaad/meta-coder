"""Verify provenance at the actual provider wire boundary, including failures."""
import hashlib
import http.client
import io
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from unittest.mock import Mock
import zipfile

import pytest
from fastapi.testclient import TestClient

from meta_coder import gemini, openrouter, openai_compatible, provenance, providers, runner, web
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.coding_sheet_drafting import CodingSheetDraftError
from meta_coder.manual_drafting import ManualDraftError
from meta_coder.extraction import ExtractionResult, ProviderError, ResponseBytes
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import clear_output, create_project, write_manual


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    p = create_project('Audit project', root=tmp_path / 'projects')
    write_manual(p, parse_coding_manual('effect_definition: Treatment versus control\neffects:\n  count: {type: number}\n'))
    (p.sources_dir / 'paper.pdf').write_bytes(b'%PDF-1.4 example')
    return p


def records(project):
    return [json.loads(path.read_text()) for path in sorted((project.path / 'audit/operations').glob('*/operation.json'))]


def exchanges(project, operation):
    return [json.loads(path.read_text()) for path in sorted((project.path / 'audit/operations' / operation['operation_id']).glob('exchange-*.json'))]


def blob(project, ref):
    data = (project.path / ref['path']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == ref['sha256']
    return data


@pytest.mark.parametrize('adapter', [gemini, openrouter, openai_compatible])
def test_exact_wire_prompts_schema_envelope_and_versions(project, monkeypatch, adapter):
    envelope = {'model': 'resolved-model-2026-01', 'modelVersion': 'revision-42', 'id': 'request-1',
                'system_fingerprint': 'fp-123', 'usage': {'prompt_tokens': 7},
                'candidates': [{'content': {'parts': [{'text': '{"answer": 1}'}]}}],
                'choices': [{'message': {'content': '{"answer": 1}'}}]}
    raw = json.dumps(envelope, separators=(',', ':')).encode()
    transport = Mock(return_value=raw)
    monkeypatch.setattr(adapter, 'cancellable_urlopen', transport)
    kwargs = {'base_url': 'http://localhost:1234/v1'} if adapter is openai_compatible else {}
    text = provenance.audited_call(
        project, 'manual_draft', adapter.generate_structured_text,
        api_key='secret-api-key', model='alias', prompt='Exact instructions.\nSecond line.',
        response_schema={'type': 'object'}, **kwargs)
    assert json.loads(text) == {'answer': 1}
    operation = records(project)[0]
    assert operation['application']['app_version']
    assert operation['application']['source_sha256']
    assert operation['application']['dependencies']['fastapi']
    with zipfile.ZipFile(io.BytesIO(blob(project, operation['application_source']))) as source:
        assert 'meta_coder/provenance.py' in source.namelist()
        assert 'meta_coder/documentation/index.html' in source.namelist()
    exchange = exchanges(project, operation)[0]
    request = transport.call_args.args[0]
    assert blob(project, exchange['request']['body']) == request.data
    assert blob(project, exchange['response']['body']) == raw
    assert exchange['response']['reported_model'] == 'revision-42'
    assert exchange['response']['system_fingerprint'] == 'fp-123'
    assert exchange['started_at_utc'] <= exchange['finished_at_utc']
    assert operation['started_at_utc'] <= operation['finished_at_utc']
    for path in (project.path / 'audit').rglob('*.json'):
        assert b'secret-api-key' not in path.read_bytes()


def test_retry_errors_and_invalid_response_are_retained(project, monkeypatch):
    error = HTTPError('https://example.test?key=secret-api-key', 429, 'limited', {}, io.BytesIO(b'{"error":"secret-api-key limited"}'))
    transport = Mock(side_effect=[error, b'not JSON'])
    monkeypatch.setattr(openai_compatible, 'cancellable_urlopen', transport)
    monkeypatch.setattr(openai_compatible, 'RETRY_BACKOFF_BASE_SEC', 0)
    with pytest.raises(ProviderError, match='invalid JSON'):
        provenance.audited_call(project, 'sheet_conversion', openai_compatible.generate_structured_text,
            api_key='secret-api-key', model='alias', base_url='http://localhost/v1',
            prompt='convert', response_schema={})
    operation = records(project)[0]
    attempts = exchanges(project, operation)
    assert operation['status'] == 'error'
    assert len(attempts) == 2
    assert attempts[0]['response']['http_status'] == 429
    assert blob(project, attempts[0]['response']['body']) == b'{"error":"[REDACTED] limited"}'
    assert blob(project, attempts[1]['response']['body']) == b'not JSON'
    assert attempts[1]['response']['reported_model'] is None


def test_request_saved_before_network_and_transport_failure(project, monkeypatch):
    def fail(request, **kwargs):
        op = records(project)[0]
        attempt = exchanges(project, op)[0]
        assert attempt['status'] == 'in_progress'
        assert blob(project, attempt['request']['body']) == request.data
        raise URLError('offline')
    monkeypatch.setattr(gemini, 'cancellable_urlopen', fail)
    with pytest.raises(ProviderError):
        provenance.audited_call(project, 'manual_draft', gemini.generate_structured_text,
            api_key='secret-api-key', model='alias', prompt='draft', response_schema={})
    operation = records(project)[0]
    assert exchanges(project, operation)[0]['status'] == 'transport_error'
    assert operation['status'] == 'error'


def test_history_survives_retries_and_output_reset_with_input_snapshots(project, monkeypatch):
    manual = parse_coding_manual(project.manual_path.read_text())
    sheet = CodingSheet([CodingSheetRow('r1', 'paper.pdf', 'Table 1')], [])
    monkeypatch.setattr(runner, 'extract_pdf_effects', lambda **kwargs: ExtractionResult('paper.pdf', 'ok', raw_response='original'))
    run = runner.Runner()
    for _ in range(2):
        state = run.start(project=project, manual=manual, coding_sheet=sheet, api_key='secret-api-key', only_pdfs=['paper.pdf'])
        deadline = time.monotonic() + 10
        while run.is_running(project.project_id) and time.monotonic() < deadline:
            time.sleep(.01)
        assert state.status == 'complete'
    history = records(project)
    assert len(history) == 4  # two runs and two independent extraction operations
    operations = [op for op in history if op['operation'] == 'extraction']
    for op in operations:
        assert blob(project, op['inputs']['source/paper.pdf']) == b'%PDF-1.4 example'
        assert op['settings']['run_id'] in {record['operation_id'] for record in history}
    latest = runner.load_persisted_results(project)['paper.pdf']
    assert latest.audit_operation_id in {op['operation_id'] for op in operations}
    clear_output(project)
    write_manual(project, manual)
    (project.sources_dir / 'paper.pdf').unlink()
    assert records(project) == history
    assert blob(project, operations[0]['inputs']['source/paper.pdf']) == b'%PDF-1.4 example'


def test_draft_uploads_and_results_are_exported_without_credentials(project, monkeypatch):
    monkeypatch.setattr(web.Runtime, 'prepare_provider', lambda *args: None)
    monkeypatch.setattr(web, 'draft_coding_manual', lambda **kwargs: parse_coding_manual(project.manual_path.read_text()))
    app = web.create_app(token='test', projects_root=project.path.parent)
    with TestClient(app, base_url='http://localhost') as client:
        response = client.post(f'/test/projects/{project.project_id}/manual/draft', files={'file': ('manual.md', b'Original manual text', 'text/markdown')})
        assert response.status_code == 200
        op = records(project)[0]
        assert blob(project, op['inputs']['uploaded/manual.md']) == b'Original manual text'
        assert json.loads(blob(project, op['inputs']['document_text.json'])) == 'Original manual text'
        response = client.get(f'/test/projects/{project.project_id}/download/zip')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('export_manifest.json'))
        assert manifest['project']['name'] == project.name
        assert 'run_settings.json' in manifest['files']
        for name, entry in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == entry['sha256']
        assert any('operation.json' in name for name in manifest['files'])
        assert any('before audit recording' in note for note in manifest['limitations'])
        assert not any('.meta_coder' in name for name in archive.namelist())


def test_transport_headers_allowlist():
    response = Mock(status=200, headers={'Date': 'today', 'X-Request-ID': 'id', 'Set-Cookie': 'secret', 'Authorization': 'secret'})
    value = ResponseBytes(b'raw', response)
    assert value == b'raw' and value.status == 200
    assert value.audit_headers == {'Date': 'today', 'X-Request-ID': 'id'}


def test_binary_input_and_source_archive_are_not_corrupted_by_redaction(project):
    original = b'%PDF-1.4 secret-api-key binary\x00\xff'
    op = provenance.AuditOperation(project, 'test', {}, secrets=('secret-api-key',))
    op.input('original.pdf', original)
    assert blob(project, op.data['inputs']['original.pdf']) == original
    with zipfile.ZipFile(io.BytesIO(blob(project, op.data['application_source']))) as archive:
        assert archive.testzip() is None


def test_missing_model_revision_and_redacted_echo(project, monkeypatch):
    envelope = b'{"choices":[{"message":{"content":"secret-api-key"}}]}'
    monkeypatch.setattr(openai_compatible, 'cancellable_urlopen', Mock(return_value=envelope))
    provenance.audited_call(project, 'test', openai_compatible.generate_structured_text,
        api_key='secret-api-key', model='alias', base_url='http://localhost/v1', prompt='test', response_schema={})
    op = records(project)[0]
    exchange = exchanges(project, op)[0]
    assert exchange['response']['reported_model'] is None
    assert b'secret-api-key' not in blob(project, exchange['response']['body'])
    assert b'secret-api-key' not in blob(project, op['result'])


def test_no_request_is_sent_if_audit_cannot_be_saved(project, monkeypatch):
    original = provenance.atomic_write
    def fail_exchange(path, data):
        if path.name.startswith('exchange-'):
            raise OSError('audit disk full')
        return original(path, data)
    monkeypatch.setattr(provenance, 'atomic_write', fail_exchange)
    transport = Mock(return_value=b'{}')
    monkeypatch.setattr(gemini, 'cancellable_urlopen', transport)
    with pytest.raises(OSError, match='audit disk full'):
        provenance.audited_call(project, 'test', gemini.generate_structured_text,
            api_key='secret-api-key', model='alias', prompt='test', response_schema={})
    transport.assert_not_called()
    assert records(project)[0]['status'] == 'error'


def test_parallel_operations_keep_requests_separate(project, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(3)
    def transport(request, **kwargs):
        barrier.wait(timeout=5)
        content = json.loads(request.data)['contents'][0]['parts'][0]['text']
        return json.dumps({'modelVersion': content, 'candidates': [{'content': {'parts': [{'text': content}]}}]}).encode()
    monkeypatch.setattr(gemini, 'cancellable_urlopen', transport)
    def call(index):
        return provenance.audited_call(project, 'test', gemini.generate_structured_text,
            api_key='secret-api-key', model='alias', prompt=str(index), response_schema={})
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(call, range(3))) == ['0', '1', '2']
    for op in records(project):
        exchange = exchanges(project, op)[0]
        request = json.loads(blob(project, exchange['request']['body']))
        assert request['contents'][0]['parts'][0]['text'] == exchange['response']['reported_model']


def test_sheet_conversion_records_original_csv_notes_and_generated_draft(project, monkeypatch):
    monkeypatch.setattr(web.Runtime, 'prepare_provider', lambda *args: None)
    monkeypatch.setattr(web, 'draft_coding_sheet', lambda **kwargs: {'csv_text': 'generated', 'warnings': []})
    app = web.create_app(token='test', projects_root=project.path.parent)
    with TestClient(app, base_url='http://localhost') as client:
        response = client.post(f'/test/projects/{project.project_id}/coding-sheet/draft',
            files={'file': ('original.csv', b'paper,effect\nDemo,Experiment 1\n', 'text/csv')},
            data={'notes': 'Each row is one experiment.'})
    assert response.status_code == 200
    op = records(project)[0]
    assert op['operation'] == 'sheet_conversion'
    assert blob(project, op['inputs']['uploaded/original.csv']) == b'paper,effect\nDemo,Experiment 1\n'
    assert json.loads(blob(project, op['inputs']['notes.json'])) == 'Each row is one experiment.'
    assert json.loads(blob(project, op['result']))['csv_text'] == 'generated'


def test_auditing_preserves_http_error_detail_for_adapter(project, monkeypatch):
    error = HTTPError('https://example.test', 400, 'bad request', {'X-Request-ID': 'failed-1'}, io.BytesIO(b'Invalid schema: missing properties'))
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(side_effect=error))
    with pytest.raises(ProviderError, match='Invalid schema: missing properties'):
        provenance.audited_call(project, 'test', gemini.generate_structured_text,
            api_key='secret-api-key', model='alias', prompt='test', response_schema={})
    assert exchanges(project, records(project)[0])[0]['response']['headers']['X-Request-ID'] == 'failed-1'


SYNTHETIC_KEY = 'synthetic/key-123'


def run_gemini(project, monkeypatch, transport, model='gemini flash'):
    manual = parse_coding_manual(project.manual_path.read_text())
    sheet = CodingSheet([CodingSheetRow('r1', 'paper.pdf', 'Table 1')], [])
    monkeypatch.setattr(gemini, 'cancellable_urlopen', transport)
    run = runner.Runner()
    state = run.start(project=project, manual=manual, coding_sheet=sheet, api_key=SYNTHETIC_KEY,
                      provider='gemini', model=model, only_pdfs=['paper.pdf'])
    deadline = time.monotonic() + 10
    while run.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(.01)
    return state


def assert_key_absent(project, state):
    persisted = [*project.raw_dir.glob('*.json'), *(project.path / 'audit').rglob('*')]
    texts = [json.dumps(state.snapshot())]
    texts += [path.read_bytes().decode('utf-8', 'replace') for path in persisted if path.is_file()]
    for text in texts:
        assert SYNTHETIC_KEY not in text and quote(SYNTHETIC_KEY, safe='') not in text


def test_gemini_model_with_space_never_puts_key_in_url_or_records(project, monkeypatch):
    sent = []
    def transport(request, **kwargs):
        sent.append(request)
        if any(ord(char) <= 0x20 for char in request.full_url):  # what http.client rejects
            raise http.client.InvalidURL(f"URL can't contain control characters. {request.full_url!r}")
        return json.dumps({'candidates': [{'content': {'parts': [{'text': '{"effects": []}'}]}}]}).encode()
    state = run_gemini(project, monkeypatch, transport)
    assert state.status == 'complete'
    assert '/models/gemini%20flash:generateContent' in sent[0].full_url
    assert sent[0].get_header('X-goog-api-key') == SYNTHETIC_KEY
    extraction = [op for op in records(project) if op['operation'] == 'extraction'][0]
    assert exchanges(project, extraction)[0]['request']['headers'] == {'Content-Type': 'application/json'}
    assert_key_absent(project, state)


def test_unexpected_errors_echoing_the_key_are_redacted_everywhere(project, monkeypatch):
    def transport(request, **kwargs):
        raise http.client.InvalidURL(f'{request.full_url}?key={SYNTHETIC_KEY} or {quote(SYNTHETIC_KEY, safe="")}')
    state = run_gemini(project, monkeypatch, transport)
    error = runner.load_persisted_results(project)['paper.pdf'].error
    assert error.startswith('Unexpected error:') and '[redacted]' in error
    assert state.pdfs[0].error == error
    assert_key_absent(project, state)

    monkeypatch.setattr(runner, 'collate_results', Mock(side_effect=OSError(f'cannot write {SYNTHETIC_KEY}')))
    state = run_gemini(project, monkeypatch, transport)
    assert state.status == 'failed' and state.error == 'cannot write [redacted]'
    assert_key_absent(project, state)


def test_draft_errors_echoing_the_key_are_redacted(project, monkeypatch):
    monkeypatch.setattr(web.Runtime, 'prepare_provider', lambda *args: None)
    app = web.create_app(token='test', projects_root=project.path.parent)
    app.state.runtime._session_keys['gemini'] = SYNTHETIC_KEY
    routes = [('manual/draft', 'draft_coding_manual', ManualDraftError, 'manual.md'),
              ('coding-sheet/draft', 'draft_coding_sheet', CodingSheetDraftError, 'sheet.csv')]
    with TestClient(app, base_url='http://localhost') as client:
        for route, function, draft_error, filename in routes:
            for error, status in [(draft_error, 400), (ProviderError, 502)]:
                monkeypatch.setattr(web, function, Mock(side_effect=error(f'failed with {SYNTHETIC_KEY}')))
                response = client.post(f'/test/projects/{project.project_id}/{route}',
                                       files={'file': (filename, b'citation\nSmith 2024')})
                assert response.status_code == status
                assert response.json()['error'] == 'failed with [redacted]'
