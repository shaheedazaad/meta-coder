"""The run's maximum output setting: resolution, wire payloads and provenance, with no live API."""
import io
import json
import time
from unittest.mock import Mock
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from meta_coder import gemini, openai_compatible, openrouter, providers, web
from meta_coder import runner as runner_module
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ProviderError
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual
from meta_coder.settings import MAX_OUTPUT_TOKENS, RunSettings, load_run_settings, output_token_limit, save_run_settings

MANUAL = parse_coding_manual('effect_definition: comparison\neffects:\n  estimate: {type: number}\n')
SHEET = CodingSheet(rows=[CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='exp')], issues=[])
COMPLETION = json.dumps({'choices': [{'message': {'content': '{"rows": []}'}, 'finish_reason': 'stop'}]}).encode()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def endpoints(*items):
    return Response(json.dumps({'data': {'endpoints': list(items)}}).encode())


def endpoint(tag, max_completion=None, context=None, structured=True):
    return {'tag': tag, 'max_completion_tokens': max_completion, 'context_length': context,
            'supported_parameters': ['Structured_Outputs'] if structured else ['tools']}


# --- Settings -------------------------------------------------------------

@pytest.mark.parametrize(('value', 'expected'), [
    ('', 0), ('  ', 0), (None, 0), (0, 0), ('-5', 0), ('abc', 0), ('1.5', 0), (True, 0),
    ('65536', 65_536), (' 8000 ', 8000), (8000, 8000), (MAX_OUTPUT_TOKENS * 2, MAX_OUTPUT_TOKENS),
])
def test_output_token_limit_parses_forms_and_saved_values(value, expected):
    assert output_token_limit(value) == expected


def test_maximum_output_is_saved_with_the_run_settings(tmp_path):
    class Project:
        path = tmp_path
    assert RunSettings().clamped().max_output_tokens == 0
    save_run_settings(Project, RunSettings(max_output_tokens=65_536))
    assert load_run_settings(Project).max_output_tokens == 65_536
    path = tmp_path / '.meta_coder' / 'run_settings.json'
    assert json.loads(path.read_text(encoding='utf-8'))['max_output_tokens'] == 65_536
    # A JSON true must not load as a one-token cap, and older files have no value.
    path.write_text(json.dumps({'provider': 'openrouter', 'max_output_tokens': True}), encoding='utf-8')
    assert load_run_settings(Project).max_output_tokens == 0
    path.write_text(json.dumps({'provider': 'openrouter'}), encoding='utf-8')
    assert load_run_settings(Project).max_output_tokens == 0


# --- OpenRouter endpoint lookup --------------------------------------------

def test_endpoint_limit_is_the_lowest_structured_output_endpoint(monkeypatch):
    request = Mock(return_value=endpoints(
        endpoint('deepinfra/fp8', 393_216, 1_048_576),  # min(393,216, 1,048,576 - 262,144)
        endpoint('cheap', 16_384),  # no context stated: its own maximum
        endpoint('tools-only', 1_000, structured=False),  # can't serve strict JSON
        endpoint('tiny', 8_000, 40_000),  # 40,000 - 32,768 leaves 7,232
        endpoint('too-small', 8_000, 30_000),  # no room for the article: left out
        endpoint('unstated', None, 200_000),
        endpoint('malformed', 'lots', True),
        'not an endpoint',
    ))
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', request)
    assert openrouter.endpoint_output_limit('vendor/model name:free', api_key='secret', timeout_sec=7) == 7_232
    sent = request.call_args.args[0]
    # A `:free` variant has its own endpoints, so it is looked up as itself.
    assert sent.full_url == 'https://openrouter.ai/api/v1/models/vendor/model%20name:free/endpoints'
    assert sent.get_header('Authorization') == 'Bearer secret'
    assert request.call_args.kwargs == {'timeout': 7}


def test_endpoint_limit_without_stated_limits_is_none(monkeypatch):
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', Mock(return_value=endpoints(endpoint('a'))))
    assert openrouter.endpoint_output_limit('v/m') is None
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', Mock(return_value=endpoints()))
    assert openrouter.endpoint_output_limit('v/m') is None


@pytest.mark.parametrize('outcome', [
    URLError('offline secret'), TimeoutError('timed out'), Response(b'not json'),
    Response(b'[]'), Response(b'{"data": []}'), Response(b'{"data": {"endpoints": {}}}'),
])
def test_unreadable_endpoint_list_is_a_provider_error(monkeypatch, outcome):
    if isinstance(outcome, Exception):
        request = Mock(side_effect=outcome)
    else:
        request = Mock(return_value=outcome)
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', request)
    with pytest.raises(ProviderError) as raised:
        openrouter.endpoint_output_limit('v/m', api_key='secret')
    assert 'secret' not in str(raised.value)


def test_resolution_prefers_the_setting_then_openrouter_endpoints(monkeypatch):
    lookup = Mock(return_value=131_072)
    monkeypatch.setattr(openrouter, 'endpoint_output_limit', lookup)
    assert providers.resolve_max_output_tokens('openrouter', 'v/m', 8000, api_key='k') == 8000
    assert providers.resolve_max_output_tokens('gemini', 'm', 0, api_key='k') is None
    assert providers.resolve_max_output_tokens('openai_compatible', 'm', 0, api_key='') is None
    lookup.assert_not_called()
    assert providers.resolve_max_output_tokens('openrouter', 'v/m', 0, api_key='k', timeout_sec=9) == 131_072
    lookup.assert_called_once_with('v/m', api_key='k', timeout_sec=9, only_provider='')
    lookup.reset_mock()
    providers.resolve_max_output_tokens(
        'openrouter', 'v/m', 0, api_key='k', openrouter_provider='groq', openrouter_allow_fallbacks=False,
    )
    lookup.assert_called_once_with('v/m', api_key='k', timeout_sec=20, only_provider='groq')
    lookup.reset_mock()
    providers.resolve_max_output_tokens('openrouter', 'v/m', 0, api_key='k', openrouter_provider='groq')
    lookup.assert_called_once_with('v/m', api_key='k', timeout_sec=20, only_provider='')


def test_a_pinned_provider_without_fallbacks_uses_only_its_endpoints(monkeypatch):
    listed = [endpoint('deepinfra/fp8', 65_536), endpoint('deepinfra/turbo', 32_768), endpoint('cheap', 4_096)]
    request = Mock(side_effect=lambda *args, **kwargs: endpoints(*listed))
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', request)
    assert openrouter.endpoint_output_limit('v/m', only_provider='deepinfra') == 32_768
    assert openrouter.endpoint_output_limit('v/m', only_provider='deepinfra/fp8') == 65_536
    assert openrouter.endpoint_output_limit('v/m', only_provider='absent') is None


def test_a_malformed_model_id_is_a_provider_error_for_the_lookup():
    with pytest.raises(ProviderError, match='not an OpenRouter model ID'):
        openrouter.endpoint_output_limit('no-author')


# --- Wire payloads ------------------------------------------------------------

def test_openrouter_sends_max_tokens_only_when_set(monkeypatch):
    request = Mock(side_effect=lambda *a, **k: COMPLETION)
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', request)
    openrouter._call_openrouter(model='m', api_key='k', prompt='p', pdf_bytes=b'%PDF', filename='a.pdf',
                                response_schema={}, max_output_tokens=65_536)
    assert json.loads(request.call_args.args[0].data)['max_tokens'] == 65_536
    openrouter.generate_structured_text(prompt='p', response_schema={}, api_key='k')
    assert 'max_tokens' not in json.loads(request.call_args.args[0].data)


def test_gemini_sends_max_output_tokens_only_when_set(monkeypatch):
    body = json.dumps({'candidates': [{'content': {'parts': [{'text': '{}'}]}}]}).encode()
    request = Mock(side_effect=lambda *a, **k: body)
    monkeypatch.setattr(gemini, 'cancellable_urlopen', request)
    gemini._call_gemini(model='m', api_key='k', prompt='p', pdf_bytes=b'%PDF', response_schema={},
                        max_output_tokens=32_000)
    assert json.loads(request.call_args.args[0].data)['generationConfig']['maxOutputTokens'] == 32_000
    gemini.generate_structured_text(prompt='p', response_schema={}, api_key='k')
    assert 'maxOutputTokens' not in json.loads(request.call_args.args[0].data)['generationConfig']


@pytest.mark.parametrize(('base_url', 'key'), [
    ('http://localhost:8000/v1', 'max_tokens'),
    ('https://api.openai.com/v1', 'max_completion_tokens'),
])
def test_openai_compatible_sends_the_servers_output_field(monkeypatch, base_url, key):
    request = Mock(side_effect=lambda *a, **k: COMPLETION)
    monkeypatch.setattr(openai_compatible, 'cancellable_urlopen', request)
    openai_compatible._call(model='m', api_key='', prompt='p', base_url=base_url, response_schema={},
                            max_output_tokens=8192)
    payload = json.loads(request.call_args.args[0].data)
    assert payload[key] == 8192
    assert not ({'max_tokens', 'max_completion_tokens'} - {key}) & set(payload)
    openai_compatible._call(model='m', api_key='', prompt='p', base_url=base_url, response_schema={})
    payload = json.loads(request.call_args.args[0].data)
    assert 'max_tokens' not in payload and 'max_completion_tokens' not in payload


@pytest.mark.parametrize('provider', providers.PROVIDERS)
def test_extraction_forwards_the_cap_to_every_adapter(provider, monkeypatch, tmp_path):
    adapter = getattr(providers, provider)
    extract = Mock(return_value='result')
    monkeypatch.setattr(adapter, 'extract_pdf_effects', extract)
    assert providers.extract_pdf_effects(provider=provider, pdf_path=tmp_path / 'a.pdf', manual=None, rows=[],
                                         api_key='k', model='m', max_output_tokens=4096) == 'result'
    assert extract.call_args.kwargs['max_output_tokens'] == 4096


@pytest.mark.parametrize('adapter', [gemini, openrouter, openai_compatible])
def test_adapters_pass_the_cap_to_their_request(adapter, monkeypatch, tmp_path):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF')
    seam = {gemini: '_call_gemini', openrouter: '_call_openrouter', openai_compatible: '_call'}[adapter]
    call = Mock(side_effect=ProviderError('stop'))
    monkeypatch.setattr(adapter, seam, call)
    if adapter is openai_compatible:
        monkeypatch.setattr(adapter, 'pdf_text', lambda *a: 'text')
    options = {'base_url': 'http://localhost/v1'} if adapter is openai_compatible else {}
    result = adapter.extract_pdf_effects(pdf_path=pdf, manual=MANUAL, rows=SHEET.rows, api_key='k', model='m',
                                         max_output_tokens=1234, **options)
    assert result.status == 'error'
    assert call.call_args.kwargs['max_output_tokens'] == 1234


# --- Runner and provenance ------------------------------------------------------

def operations(project, kind):
    found = []
    for path in sorted((project.path / 'audit' / 'operations').glob('*/operation.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['operation'] == kind:
            found.append(data)
    return found


def run_openrouter(tmp_path, monkeypatch, lookup, **options):
    project = create_project('Output', root=tmp_path)
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', lookup)
    chat = Mock(side_effect=lambda *a, **k: COMPLETION)
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', chat)
    runner = runner_module.Runner()
    state = runner.start(project=project, manual=MANUAL, coding_sheet=SHEET, api_key='secret',
                         provider='openrouter', model='v/m', **options)
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(.01)
    assert state.status == 'complete'
    return project, state, json.loads(chat.call_args.args[0].data)


def test_run_sends_and_records_the_endpoint_limit(tmp_path, monkeypatch):
    lookup = Mock(return_value=endpoints(endpoint('a', 131_072), endpoint('b', 393_216)))
    project, state, payload = run_openrouter(tmp_path, monkeypatch, lookup)
    assert payload['max_tokens'] == 131_072
    assert state.warning is None and state.snapshot()['warning'] is None
    lookup.assert_called_once()
    [run] = operations(project, 'extraction_run')
    assert run['settings']['max_output_tokens'] == 0
    assert run['settings']['resolved_max_output_tokens'] == 131_072
    assert run['settings']['max_output_tokens_lookup_error'] is None
    [extraction] = operations(project, 'extraction')
    assert extraction['settings']['max_output_tokens'] == 131_072


def test_run_sends_a_configured_cap_without_a_lookup(tmp_path, monkeypatch):
    lookup = Mock(side_effect=AssertionError('no lookup when the cap is set'))
    project, _, payload = run_openrouter(tmp_path, monkeypatch, lookup, max_output_tokens=50_000)
    assert payload['max_tokens'] == 50_000
    [run] = operations(project, 'extraction_run')
    assert (run['settings']['max_output_tokens'], run['settings']['resolved_max_output_tokens']) == (50_000, 50_000)


def test_failed_lookup_warns_and_sends_no_cap(tmp_path, monkeypatch):
    project, state, payload = run_openrouter(tmp_path, monkeypatch, Mock(side_effect=URLError('down secret')))
    assert 'max_tokens' not in payload
    assert "Couldn't look up" in state.warning and 'secret' not in state.warning
    assert state.snapshot()['warning'] == state.warning
    [run] = operations(project, 'extraction_run')
    assert run['settings']['resolved_max_output_tokens'] is None
    assert 'down' in run['settings']['max_output_tokens_lookup_error']
    [extraction] = operations(project, 'extraction')
    assert 'max_output_tokens' not in extraction['settings']


def test_resolution_without_an_audit_record(monkeypatch):
    monkeypatch.setattr(runner_module, 'resolve_max_output_tokens', Mock(side_effect=ProviderError('down')))
    state = runner_module.RunState()
    assert runner_module._resolve_output_limit(state, 'openrouter', 'v/m', 0, 'k', None) is None
    assert 'down' in state.warning


# --- Web form and page ---------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    monkeypatch.setattr(web, 'check_model', Mock(return_value=[]))
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Route study', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def test_settings_form_saves_and_shows_the_maximum_output(site):
    client, project, _ = site
    path = f'/test/projects/{project.project_id}/settings'
    form = {'provider': 'openrouter', 'previous_provider': 'openrouter', 'model': 'v/m'}
    assert client.post(path, data={**form, 'max_output_tokens': '65536'}).status_code == 303
    assert load_run_settings(project).max_output_tokens == 65_536
    page = client.get(f'/test/projects/{project.project_id}').text
    assert 'name="max_output_tokens"' in page and 'value="65536"' in page
    assert client.post(path, data={**form, 'max_output_tokens': ''}).status_code == 303
    assert load_run_settings(project).max_output_tokens == 0
    assert client.post(path, data=form).status_code == 303
    assert load_run_settings(project).max_output_tokens == 0


def test_run_passes_the_setting_and_page_shows_the_lookup_warning(site, monkeypatch):
    client, project, runtime = site
    write_manual(project, MANUAL)
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,paper.pdf,exp1,Smith,2024\n')
    runtime._session_keys['gemini'] = 'fake-test-key'
    save_run_settings(project, RunSettings(max_output_tokens=12_345))
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    assert client.post(f'/test/projects/{project.project_id}/run').status_code == 303
    assert start.call_args.kwargs['max_output_tokens'] == 12_345
    runtime.runner._states[project.project_id] = runner_module.RunState(
        status='complete', warning="Couldn't look up the model's maximum output (offline).")
    assert "maximum output (offline)" in client.get(f'/test/projects/{project.project_id}').text
