import asyncio
import io
import json
import threading
import time
import urllib.error
from urllib.parse import urlencode

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from meta_coder import app_settings, openai_compatible as adapter, providers
from meta_coder.app_settings import AppSettings, load_app_settings, save_app_settings
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionCancelled, ProviderError
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project
from meta_coder.runner import Runner, load_persisted_results
from meta_coder.settings import RunSettings, load_run_settings, save_run_settings
from meta_coder.web import Runtime, create_app


MANUAL = parse_coding_manual('effect_definition: Test effect\neffects:\n  estimate: {type: number}\n')
REPLY = {"effects": [{"row_id": "r1", "estimate": {"value": 42, "evidence": "Page 1"}, "notes": {"value": "", "evidence": ""}}]}


def completion(value=REPLY):
    return json.dumps({"choices": [{"message": {"content": json.dumps(value)}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 8}}).encode()


def make_pdf(path, text="The estimate is 42."):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, 'app_data_dir', lambda: tmp_path)
    monkeypatch.setattr('meta_coder.web.credentials.saved_key_configured', lambda p: False)
    monkeypatch.setattr('meta_coder.web.credentials.keyring_available', lambda: False)
    return tmp_path


@pytest.mark.parametrize('mode', ['json_schema', 'json_object', 'none'])
@pytest.mark.parametrize('key', ['', 'secret'])
def test_chat_request_modes_and_optional_auth(mode, key, monkeypatch):
    captured = {}

    def respond(request, **kwargs):
        captured.update(request=request, payload=json.loads(request.data), **kwargs)
        return completion()

    monkeypatch.setattr(adapter, 'cancellable_urlopen', respond)
    text = adapter.generate_structured_text(prompt='Code this', response_schema={'type': 'object'},
        api_key=key, model='local/model', base_url='http://localhost:8000/custom/v1/',
        response_format=mode, timeout_sec=123)
    assert json.loads(text) == REPLY
    request, payload = captured['request'], captured['payload']
    assert request.full_url == 'http://localhost:8000/custom/v1/chat/completions'
    assert request.method == 'POST'
    assert request.get_header('Authorization') == (f'Bearer {key}' if key else None)
    assert captured['timeout'] == 123
    assert payload['model'] == 'local/model'
    assert 'Return only JSON' in payload['messages'][0]['content']
    assert not {'reasoning', 'temperature', 'plugins'} & payload.keys()
    if mode == 'none':
        assert 'response_format' not in payload
    elif mode == 'json_schema':
        assert payload['response_format']['json_schema']['strict'] is True
    else:
        assert payload['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('url', ['', 'file:///tmp/a', 'https://user:pass@host/v1',
    'https://host/v1?key=secret', 'https://host/v1#fragment', 'https://host:abc/v1',
    'https://host/v1/chat/completions', 'http://host/a b'])
def test_invalid_url_rejected(url):
    with pytest.raises(ValueError):
        adapter.normalize_base_url(url)
    assert providers.check_model('openai_compatible', 'model', api_key='', base_url=url)


@pytest.mark.parametrize('body', [b'bad json', b'[]', b'{"choices": ["bad"]}',
    b'{"choices":[{"message":{"refusal":"No"}}]}'])
def test_invalid_responses_are_provider_errors(body, monkeypatch):
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: body)
    with pytest.raises(ProviderError):
        adapter.generate_structured_text(prompt='x', response_schema={}, api_key='', model='m', base_url='http://localhost/v1')


def test_transient_retry_redaction_and_cancellation(monkeypatch):
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.HTTPError('http://localhost/v1', 429, 'busy', {}, io.BytesIO(b'secret busy'))
    monkeypatch.setattr(adapter, 'cancellable_urlopen', fail)
    monkeypatch.setattr(adapter, 'RETRY_BACKOFF_BASE_SEC', 0)
    with pytest.raises(ProviderError, match=r'\[redacted\] busy'):
        adapter.generate_structured_text(prompt='x', response_schema={}, api_key='secret', model='m', base_url='http://localhost/v1')
    assert len(attempts) == 3
    event = threading.Event()
    event.set()
    with pytest.raises(ExtractionCancelled):
        adapter._call(prompt='x', response_schema={}, api_key='', model='m', base_url='http://localhost/v1', cancel_event=event)
    assert len(attempts) == 3


def test_extraction_through_runner_preserves_validation_and_usage(tmp_path, monkeypatch):
    project = create_project('Compatible', root=tmp_path)
    make_pdf(project.sources_dir / 'paper.pdf')
    captured = []
    def respond(request, **kwargs):
        captured.append(json.loads(request.data))
        return completion()
    monkeypatch.setattr(adapter, 'cancellable_urlopen', respond)
    runner = Runner()
    state = runner.start(project=project, manual=MANUAL,
        coding_sheet=CodingSheet(rows=[CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='')], issues=[]),
        api_key='', provider='openai_compatible', model='local/model',
        base_url='http://localhost:8000/v1', response_format='json_object')
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(.01)
    assert state.status == 'complete'
    result = load_persisted_results(project)['paper.pdf']
    assert result.status == 'ok'
    assert result.input_tokens == 12 and result.output_tokens == 8
    assert result.coded_by_row_id['r1']['estimate']['value'] == 42
    prompt = captured[0]['messages'][0]['content']
    assert '[Page 1]' in prompt and 'The estimate is 42.' in prompt
    assert captured[0]['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('choice_extra, expected', [
    ({}, 'ok'),  # local servers that omit finish metadata are not penalised
    ({'finish_reason': 'stop'}, 'ok'), ({'finish_reason': 'eos_token'}, 'ok'),
    ({'finish_reason': 'length'}, 'needs_review'), ({'finish_reason': 'content_filter'}, 'needs_review'),
])
def test_finish_reason_decides_whether_valid_reply_needs_review(tmp_path, monkeypatch, choice_extra, expected):
    make_pdf(tmp_path / 'paper.pdf')
    body = {'choices': [{'message': {'content': json.dumps(REPLY)}, **choice_extra}]}
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: json.dumps(body).encode())
    result = adapter.extract_pdf_effects(pdf_path=tmp_path / 'paper.pdf', manual=MANUAL, api_key='', model='m',
        rows=[CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='')], base_url='http://localhost/v1')
    assert result.status == expected
    assert result.coded_by_row_id['r1']['estimate']['value'] == 42
    if expected == 'needs_review':
        assert f"finish reason: {choice_extra['finish_reason']}" in result.error


def test_repaired_reply_needs_review_but_keeps_values(tmp_path, monkeypatch):
    make_pdf(tmp_path / 'paper.pdf')
    body = {'choices': [{'message': {'content': json.dumps(REPLY)[:-1] + ',}'}, 'finish_reason': 'stop'}]}
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: json.dumps(body).encode())
    result = adapter.extract_pdf_effects(pdf_path=tmp_path / 'paper.pdf', manual=MANUAL, api_key='', model='m',
        rows=[CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='')], base_url='http://localhost/v1')
    assert result.status == 'needs_review' and 'automatically repaired' in result.error
    assert result.coded_by_row_id == {'r1': {k: v for k, v in REPLY['effects'][0].items() if k != 'row_id'}}


def test_blank_pdf_and_cancelled_pdf_fail_without_network(tmp_path, monkeypatch):
    path = tmp_path / 'blank.pdf'
    make_pdf(path, '')
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: pytest.fail('Unexpected request'))
    kwargs = dict(provider='openai_compatible', pdf_path=path, manual=MANUAL, rows=[], api_key='',
                  model='m', base_url='http://localhost/v1')
    result = providers.extract_pdf_effects(**kwargs)
    assert result.status == 'error' and 'OCR' in result.error
    event = threading.Event()
    event.set()
    assert providers.extract_pdf_effects(**kwargs, cancel_event=event).status == 'cancelled'


def test_wrong_row_ids_need_review(tmp_path, monkeypatch):
    path = tmp_path / 'paper.pdf'
    make_pdf(path)
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: completion())
    result = providers.extract_pdf_effects(provider='openai_compatible', pdf_path=path,
        manual=MANUAL, rows=[CodingSheetRow(row_id='different', source_pdf='paper.pdf', locator='')],
        api_key='', model='m', base_url='http://localhost/v1')
    assert result.status == 'needs_review'
    assert result.missing_ids == {'different'} and result.extra_ids == {'r1'}


def test_manual_drafting_dispatch(monkeypatch):
    draft = {'name': 'Test', 'description': '', 'effect_definition': 'Difference',
        'effects': [{'name': 'estimate', 'type': 'number', 'description': 'Estimate', 'evidence_required': True, 'levels': []}]}
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: completion(draft))
    manual = providers.draft_coding_manual(provider='openai_compatible', document_text='Manual text',
        api_key='', model='m', base_url='http://localhost/v1', response_format='none')
    assert set(manual.effects) == {'estimate', 'notes'}


def request(app, method, path, data=None):
    async def run():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': urlencode(data or {}).encode(), 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path,
            'query_string': b'', 'root_path': '', 'headers': [(b'host', b'localhost'),
                (b'content-type', b'application/x-www-form-urlencoded')], 'server': ('localhost', 80)}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start'), b''.join(m.get('body', b'') for m in messages).decode()
    return asyncio.run(run())


def test_settings_ui_persistence_and_keyless_readiness(settings_dir):
    app = create_app(token='test', projects_root=settings_dir / 'projects')
    status, body = request(app, 'GET', '/test/settings')
    assert status == 200 and 'openai_base_url' in body and 'openai_compatible' in body
    # Headings include decorative icon markup, so assert on the stable heading
    # text rather than the exact opening tag.
    provider_panel, manual_panel = body.split('Drafting model</h2>')
    assert all(f'id="config-{provider}"' in provider_panel for provider in providers.PROVIDERS)
    assert 'name="openai_base_url"' in provider_panel
    assert 'name="openai_base_url"' not in manual_panel
    assert 'name="manual_generator_provider"' in manual_panel
    assert 'name="manual_generator_model"' in manual_panel
    values = dict(upload_size_cap_mb=128, manual_generator_provider='openai_compatible',
                  manual_generator_model='my-model', openai_base_url='http://localhost:8000/v1/', openai_response_format='none')
    assert request(app, 'POST', '/test/settings/app', values)[0] == 303
    assert request(app, 'POST', '/test/settings/openai-compatible', values)[0] == 303
    settings = load_app_settings()
    assert settings.openai_base_url == 'http://localhost:8000/v1'
    assert settings.openai_response_format == 'none'
    assert settings.manual_generator_model == 'my-model'
    runtime = Runtime(projects_root=settings_dir / 'projects')
    assert runtime.provider_ready('openai_compatible')
    runtime._saved_keys.add('openai_compatible')
    assert runtime.provider_ready('openai_compatible')
    runtime._session_keys['openai_compatible'] = 'secret'
    assert runtime.provider_ready('openai_compatible')
    values['openai_base_url'] = 'file:///tmp/test'
    assert request(app, 'POST', '/test/settings/openai-compatible', values)[0] == 303
    assert load_app_settings().openai_base_url == settings.openai_base_url
    assert request(app, 'POST', '/test/settings/app', values)[0] == 303
    assert load_app_settings().openai_base_url == settings.openai_base_url
    assert request(app, 'POST', '/test/settings/openai-compatible', {'openai_base_url': ''})[0] == 303
    assert load_app_settings().openai_base_url == ''


def test_provider_switch_preserves_custom_model(settings_dir):
    project = create_project('Test', root=settings_dir / 'projects')
    save_app_settings(AppSettings(openai_base_url='http://localhost/v1'))
    app = create_app(token='test', projects_root=settings_dir / 'projects')
    status, _ = request(app, 'POST', f'/test/projects/{project.project_id}/settings',
        dict(provider='openai_compatible', previous_provider='gemini', model='custom/model'))
    assert status == 303
    settings = load_run_settings(project)
    assert settings.provider == 'openai_compatible' and settings.model == 'custom/model'


def test_authenticated_web_run_and_retry(settings_dir, monkeypatch):
    project = create_project('Authenticated endpoint', root=settings_dir / 'projects')
    project.manual_path.write_text('effect_definition: Test effect\neffects:\n  estimate: {type: number}\n')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,paper.pdf,,Smith,2020\n')
    make_pdf(project.sources_dir / 'paper.pdf')
    save_run_settings(project, RunSettings(provider='openai_compatible', model='private/model'))
    save_app_settings(AppSettings(openai_base_url='https://example.test/api/v1', openai_response_format='none'))
    saved = {}
    monkeypatch.setattr('meta_coder.web.credentials.save_key', lambda p, k: saved.update({p: k}))
    monkeypatch.setattr('meta_coder.web.credentials.load_key', lambda p: saved.get(p))
    monkeypatch.setattr('meta_coder.web.credentials.mark_saved_key_configured', lambda *a: True)
    captured = []
    def respond(req, **kwargs):
        captured.append(req)
        return completion()
    monkeypatch.setattr(adapter, 'cancellable_urlopen', respond)
    app = create_app(token='test', projects_root=project.path.parent)
    assert request(app, 'POST', '/test/settings/api-key', {'provider': 'openai_compatible', 'api_key': 'private-key'})[0] == 303
    assert saved == {'openai_compatible': 'private-key'}
    # Running after restart loads the saved key without a separate unlock action.
    monkeypatch.setattr('meta_coder.web.credentials.saved_key_configured', lambda p: p in saved)
    app = create_app(token='test', projects_root=project.path.parent)
    run_path = f'/test/projects/{project.project_id}/run'
    start_status, start_body = request(app, 'POST', run_path)
    assert start_status == 303, start_body
    deadline = time.monotonic() + 5
    while not load_persisted_results(project) and time.monotonic() < deadline:
        time.sleep(.01)
    assert load_persisted_results(project)['paper.pdf'].status == 'ok'
    assert captured[0].full_url == 'https://example.test/api/v1/chat/completions'
    assert captured[0].get_header('Authorization') == 'Bearer private-key'
    assert json.loads(captured[0].data)['model'] == 'private/model'
    # Wait for the worker's final state, then explicitly retry the same PDF.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _ = request(app, 'POST', run_path + '/retry', {'source_pdf': 'paper.pdf'})
        if status != 409:
            break
        time.sleep(.01)
    assert status == 303
    while len(captured) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(captured) == 2
    assert captured[1].get_header('Authorization') == 'Bearer private-key'


def test_authentication_error_is_reported_without_retry(monkeypatch):
    attempts = []
    def unauthorized(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.HTTPError('https://example.test/v1', 401, 'Unauthorized', {}, io.BytesIO(b'Invalid key secret'))
    monkeypatch.setattr(adapter, 'cancellable_urlopen', unauthorized)
    with pytest.raises(ProviderError, match=r'401.*Invalid key \[redacted\]'):
        adapter.generate_structured_text(prompt='x', response_schema={}, api_key='secret',
                                         model='m', base_url='https://example.test/v1')
    assert len(attempts) == 1


def test_endpoint_save_also_saves_api_key_and_survives_restart(settings_dir, monkeypatch):
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self.current = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'form':
                self.current = {'action': attrs.get('action'), 'fields': set()}
                self.forms.append(self.current)
            if self.current is not None and 'name' in attrs:
                self.current['fields'].add(attrs['name'])
        def handle_endtag(self, tag):
            if tag == 'form':
                self.current = None
    saved = {}
    monkeypatch.setattr('meta_coder.web.credentials.save_key', lambda p, k: saved.update({p: k}))
    monkeypatch.setattr('meta_coder.web.credentials.saved_key_configured', lambda p: p in saved)
    app = create_app(token='test', projects_root=settings_dir / 'projects')
    forms = Forms()
    forms.feed(request(app, 'GET', '/test/settings')[1])
    endpoint_form = next(f for f in forms.forms if f['action'] == '/test/settings/openai-compatible')
    assert {'api_key', 'openai_base_url', 'openai_response_format'} <= endpoint_form['fields']
    assert request(app, 'POST', endpoint_form['action'], {
        'api_key': 'test-key', 'openai_base_url': 'https://example.test/v1',
        'openai_response_format': 'json_schema'})[0] == 303
    assert saved == {'openai_compatible': 'test-key'}
    restarted = create_app(token='test', projects_root=settings_dir / 'projects')
    body = request(restarted, 'GET', '/test/settings')[1]
    assert 'value="https://example.test/v1"' in body
    assert 'Saved in Keychain' in body
    assert 'test-key' not in body
    # Saving an endpoint without replacing its key preserves the saved key.
    request(restarted, 'POST', endpoint_form['action'], {
        'api_key': '', 'openai_base_url': 'https://example.test/v1'})
    assert saved == {'openai_compatible': 'test-key'}


def test_manual_generator_reports_missing_url_instead_of_locked_key(settings_dir, monkeypatch):
    project = create_project('Manual UI', root=settings_dir / 'projects')
    save_app_settings(AppSettings(manual_generator_provider='openai_compatible', manual_generator_model='Qwen3.8-27B'))
    saved = {}
    monkeypatch.setattr('meta_coder.web.credentials.save_key', lambda p, k: saved.update({p: k}))
    monkeypatch.setattr('meta_coder.web.credentials.saved_key_configured', lambda p: p in saved)
    app = create_app(token='test', projects_root=project.path.parent)
    request(app, 'POST', '/test/settings/api-key', {'provider': 'openai_compatible', 'api_key': 'test-key'})
    path = f'/test/projects/{project.project_id}'
    body = request(app, 'GET', path)[1]
    assert 'Set a valid OpenAI-compatible API base URL' in body
    from html.parser import HTMLParser
    class UploadInput(HTMLParser):
        attrs = {}
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get('id') == 'manual-draft-file':
                self.attrs = attrs
    upload = UploadInput()
    upload.feed(body)
    assert upload.attrs['type'] == 'file'
    assert 'u-visually-hidden' not in upload.attrs.get('class', '')
    assert 'disabled' in upload.attrs
    assert 'Unlock the saved OpenAI-compatible API key' not in body
    request(app, 'POST', '/test/settings/openai-compatible', {'openai_base_url': 'https://example.test/v1'})
    body = request(app, 'GET', path)[1]
    assert 'id="manual-draft-form"' in body
    upload.feed(body)
    assert 'disabled' not in upload.attrs
    assert 'Set a valid OpenAI-compatible API base URL' not in body
    restarted = create_app(token='test', projects_root=project.path.parent)
    body = request(restarted, 'GET', path)[1]
    assert 'Unlock the saved OpenAI-compatible API key' not in body
    upload.feed(body)
    assert 'disabled' not in upload.attrs


def test_launch_and_page_views_never_read_keychain(settings_dir, monkeypatch):
    monkeypatch.setattr('meta_coder.web.credentials.saved_key_configured', lambda p: True)
    def unexpected_read(*args):
        pytest.fail('Page views must never request keychain access')
    monkeypatch.setattr('meta_coder.web.credentials.load_key', unexpected_read)
    project = create_project('No launch prompt', root=settings_dir / 'projects')
    app = create_app(token='test', projects_root=project.path.parent)
    for path in ['/test/', '/test/settings', f'/test/projects/{project.project_id}']:
        status, body = request(app, 'GET', path)
        assert status == 200
        assert '/api-key/unlock' not in body
        assert '/run/unlock' not in body


def endpoint_call(**changes):
    options = dict(prompt='code', response_schema={}, api_key='secret', model='model', base_url='http://localhost/v1')
    options.update(changes)
    return adapter._call(**options)


@pytest.mark.parametrize('changes', [{'base_url': 'bad'}, {'model': ' '}, {'response_format': 'xml'}])
def test_invalid_call_configuration_never_sends_request(monkeypatch, changes):
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: pytest.fail('Unexpected request'))
    with pytest.raises(ProviderError):
        endpoint_call(**changes)


@pytest.mark.parametrize('body', [{}, {'choices': 'bad'}, {'choices': [{'message': 'bad'}]}])
def test_malformed_completion_envelopes_keep_raw_response(monkeypatch, body):
    monkeypatch.setattr(adapter, 'cancellable_urlopen', lambda *a, **k: json.dumps(body).encode())
    with pytest.raises(ProviderError) as error:
        endpoint_call()
    assert json.loads(error.value.raw_response) == body


def test_invalid_usage_is_ignored_and_reasoning_effort_forwarded(monkeypatch):
    captured = []
    def respond(request, **kwargs):
        captured.append(json.loads(request.data))
        return json.dumps({'choices': [{'message': {'content': '{}'}}], 'usage': 'bad'}).encode()
    monkeypatch.setattr(adapter, 'cancellable_urlopen', respond)
    assert endpoint_call(reasoning_effort='high') == ('{}', {'input_tokens': None, 'output_tokens': None, 'finish_reason': None})
    assert captured[0]['reasoning_effort'] == 'high'
    request = adapter._request('http://localhost/v1', api_key='')
    assert request.data is None and request.get_header('Authorization') is None
    assert adapter._redact('diagnostic', '') == 'diagnostic'


@pytest.mark.parametrize('http', [False, True])
def test_cancel_during_endpoint_failure(monkeypatch, http):
    event = threading.Event()
    def fail(*a, **k):
        event.set()
        if http:
            raise urllib.error.HTTPError('url', 503, 'busy', {}, io.BytesIO(b'busy'))
        raise OSError('connection closed')
    monkeypatch.setattr(adapter, 'cancellable_urlopen', fail)
    with pytest.raises(ExtractionCancelled):
        endpoint_call(cancel_event=event)


def test_connection_retries_and_cancel_during_backoff(monkeypatch):
    from unittest.mock import Mock
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = False
    request = Mock(side_effect=[OSError('secret offline'), completion()])
    monkeypatch.setattr(adapter, 'cancellable_urlopen', request)
    assert json.loads(endpoint_call(cancel_event=event)[0]) == REPLY
    event.wait.assert_called_once_with(2)
    request.side_effect = OSError('secret offline')
    with pytest.raises(ProviderError, match=r'\[redacted\] offline'):
        endpoint_call(cancel_event=event)
    event.wait.return_value = True
    with pytest.raises(ExtractionCancelled):
        endpoint_call(cancel_event=event)


def test_corrupt_pdf_and_unrepairable_model_json_are_reported(tmp_path, monkeypatch):
    from unittest.mock import Mock
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'broken')
    with pytest.raises(ProviderError, match='Could not extract PDF text'):
        adapter.pdf_text(path, None)
    make_pdf(path)
    monkeypatch.setattr(adapter, '_call', Mock(return_value=('bad JSON', {})))
    monkeypatch.setattr(adapter, 'parse_json_response', Mock(side_effect=ProviderError('repair failed')))
    result = adapter.extract_pdf_effects(pdf_path=path, manual=MANUAL, rows=[], api_key='', base_url='http://localhost/v1')
    assert result.status == 'error' and result.raw_response == 'bad JSON'
    assert result.error == 'repair failed'
