"""OpenRouter provider routing: settings, request payload, endpoint listing,
web routes and provenance. All provider traffic is faked."""
import io
import json
import time
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from meta_coder import openrouter, providers, runner, web
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult, ProviderError
from meta_coder.fingerprints import InputChecker
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual
from meta_coder.settings import (
    RunSettings, load_run_settings, normalize_openrouter_provider, save_run_settings,
)

MANUAL = 'effect_definition: comparison\neffects:\n  estimate: {type: number}\n'


class FakeProject:
    def __init__(self, path):
        self.path = path


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())


# Settings ------------------------------------------------------------------

def test_routing_defaults_round_trip_and_invalid_saved_values(tmp_path):
    project = FakeProject(tmp_path)
    assert (load_run_settings(project).openrouter_provider, load_run_settings(project).openrouter_allow_fallbacks) == ('', True)
    save_run_settings(project, RunSettings(provider='openrouter', openrouter_provider=' DeepInfra/FP8 ', openrouter_allow_fallbacks=False))
    loaded = load_run_settings(project)
    assert (loaded.openrouter_provider, loaded.openrouter_allow_fallbacks) == ('deepinfra/fp8', False)
    path = tmp_path / '.meta_coder' / 'run_settings.json'
    stored = json.loads(path.read_text(encoding='utf-8'))
    assert stored['openrouter_provider'] == 'deepinfra/fp8' and stored['openrouter_allow_fallbacks'] is False
    # Only an explicit false disables fallbacks; a hand-edited bad slug means automatic.
    path.write_text(json.dumps({'openrouter_provider': '../etc', 'openrouter_allow_fallbacks': 0}), encoding='utf-8')
    loaded = load_run_settings(project)
    assert (loaded.openrouter_provider, loaded.openrouter_allow_fallbacks) == ('', True)


@pytest.mark.parametrize('value, expected', [
    (None, ''), ('', ''), ('  ', ''), ('Groq', 'groq'), ('deepinfra/turbo', 'deepinfra/turbo'),
    ('google-vertex/global', 'google-vertex/global'), ('a.b_c-d', 'a.b_c-d'),
])
def test_valid_provider_slugs(value, expected):
    assert normalize_openrouter_provider(value) == expected


@pytest.mark.parametrize('value', ['bad slug', '../etc', 'a/b/c', '/fp8', 'deepinfra/', '-x', 'a/-b', 'déjà'])
def test_invalid_provider_slugs(value):
    with pytest.raises(ValueError, match='Invalid OpenRouter provider'):
        normalize_openrouter_provider(value)


def test_routing_does_not_change_input_fingerprints(tmp_path):
    project = create_project('Fingerprint', root=tmp_path)
    write_manual(project, parse_coding_manual(MANUAL))
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    sheet = CodingSheet([CodingSheetRow('r1', 'paper.pdf', 'exp')], [])
    checker = InputChecker(project, parse_coding_manual(MANUAL), sheet, provider='openrouter', model='vendor/model')
    # The checker has no routing input at all: routing picks who serves the
    # same model, so changing it never marks a result outdated.
    assert 'openrouter_provider' not in checker._settings
    assert checker.fingerprint('paper.pdf') is not None


# Request payload -------------------------------------------------------------

@pytest.mark.parametrize('preferred, allow, expected', [
    ('', True, None),
    ('', False, {'allow_fallbacks': False}),
    ('deepinfra/fp8', True, {'order': ['deepinfra/fp8'], 'allow_fallbacks': True}),
    ('deepinfra/fp8', False, {'order': ['deepinfra/fp8'], 'allow_fallbacks': False}),
])
def test_extraction_request_carries_provider_preferences(tmp_path, monkeypatch, preferred, allow, expected):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF')
    sent = []

    def transport(request, **kwargs):
        sent.append(json.loads(request.data))
        return json.dumps({'choices': [{'message': {'content': '{"effects": []}'}}]}).encode()

    monkeypatch.setattr(openrouter, 'cancellable_urlopen', transport)
    providers.extract_pdf_effects(
        provider='openrouter', pdf_path=pdf, manual=parse_coding_manual(MANUAL),
        rows=[CodingSheetRow('r1', 'paper.pdf', 'exp')], api_key='key', model='vendor/model',
        openrouter_provider=preferred, openrouter_allow_fallbacks=allow,
    )
    assert sent[0].get('provider') == expected


def test_only_openrouter_receives_routing_options(tmp_path, monkeypatch):
    for name in providers.PROVIDERS:
        adapter = getattr(providers, name)
        extract = Mock(return_value=ExtractionResult(source_pdf='paper.pdf', status='ok'))
        monkeypatch.setattr(adapter, 'extract_pdf_effects', extract)
        providers.extract_pdf_effects(provider=name, pdf_path=tmp_path / 'paper.pdf', manual=None, rows=[],
                                      api_key='key', model='m', openrouter_provider='groq', openrouter_allow_fallbacks=False)
        args = extract.call_args.kwargs
        assert ('openrouter_provider' in args) == (name == 'openrouter')
        if name == 'openrouter':
            assert (args['openrouter_provider'], args['allow_fallbacks']) == ('groq', False)


# Endpoint listing --------------------------------------------------------------

def test_endpoint_listing_keeps_structured_output_providers(monkeypatch):
    payload = {'data': {'endpoints': [
        {'tag': 'deepinfra/turbo', 'provider_name': 'DeepInfra', 'quantization': 'fp8', 'supported_parameters': ['structured_outputs']},
        {'tag': 'groq', 'provider_name': 'Groq', 'supported_parameters': ['tools']},
        {'tag': 'google-vertex', 'provider_name': 'Google Vertex', 'quantization': 'unknown', 'supported_parameters': ['Structured_Outputs']},
        {'tag': 'deepinfra/turbo', 'provider_name': 'Duplicate', 'supported_parameters': ['structured_outputs']},
        {'tag': 'akashml/fp8', 'provider_name': 'AkashML', 'quantization': 'fp8', 'supported_parameters': ['structured_outputs']},
        {'tag': 'nameless', 'supported_parameters': ['structured_outputs']},
        {'provider_name': 'No tag', 'supported_parameters': ['structured_outputs']},
        {'tag': 'no-parameters'},
        'not an endpoint',
    ]}}
    request = Mock(side_effect=lambda *args, **kwargs: Response(payload))
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', request)
    endpoints = openrouter.list_model_endpoints(' meta-llama/llama 3:free ', api_key='secret', timeout_sec=5)
    sent = request.call_args.args[0]
    assert sent.full_url == 'https://openrouter.ai/api/v1/models/meta-llama/llama%203/endpoints'
    assert sent.get_header('Authorization') == 'Bearer secret'
    assert request.call_args.kwargs == {'timeout': 5}
    assert endpoints == [
        {'tag': 'akashml/fp8', 'name': 'AkashML (fp8)'},
        {'tag': 'deepinfra/turbo', 'name': 'DeepInfra (turbo, fp8)'},
        {'tag': 'google-vertex', 'name': 'Google Vertex'},
        {'tag': 'nameless', 'name': 'nameless'},
    ]
    openrouter.list_model_endpoints('vendor/model')
    assert request.call_args.args[0].get_header('Authorization') is None


@pytest.mark.parametrize('model', ['', 'model', 'a/b/c', '/model', 'vendor/'])
def test_endpoint_listing_rejects_malformed_model_ids(model):
    with pytest.raises(ValueError, match='not an OpenRouter model ID'):
        openrouter.list_model_endpoints(model)


@pytest.mark.parametrize('outcome, message', [
    (HTTPError('u', 404, 'missing', {}, io.BytesIO(b'')), 'was not found'),
    (HTTPError('u', 500, 'broken', {}, io.BytesIO(b'')), r'API error \(500\)'),
    (URLError('offline'), 'Could not reach OpenRouter: offline'),
    (Response(b'not json'), 'invalid endpoint list'),
    (Response([]), 'invalid endpoint list'),
    (Response({'data': []}), 'invalid endpoint list'),
    (Response({'data': {'endpoints': {}}}), 'invalid endpoint list'),
])
def test_endpoint_listing_failures(monkeypatch, outcome, message):
    def urlopen(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', urlopen)
    with pytest.raises(ProviderError, match=message):
        openrouter.list_model_endpoints('vendor/model')


# Web routes ----------------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Routing study', root=tmp_path / 'projects')
    write_manual(project, parse_coding_manual(MANUAL))
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,paper.pdf,exp1,Smith,2024\n')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    runtime = app.state.runtime
    runtime._session_keys['openrouter'] = 'session-key'
    monkeypatch.setattr(web, 'check_model', Mock(return_value=[]))
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, runtime


def test_settings_route_saves_keeps_and_validates_routing(site):
    client, project, _ = site
    target = f'/test/projects/{project.project_id}/settings'
    base = {'provider': 'openrouter', 'previous_provider': 'openrouter', 'model': 'vendor/model'}
    assert client.post(target, data={**base, 'openrouter_provider': 'DeepInfra/FP8', 'openrouter_fallbacks': 'deny'}).status_code == 303
    settings = load_run_settings(project)
    assert (settings.openrouter_provider, settings.openrouter_allow_fallbacks) == ('deepinfra/fp8', False)
    for bad in ({'openrouter_fallbacks': 'maybe'}, {'openrouter_provider': 'bad slug!'}):
        assert client.post(target, data={**base, **bad}).status_code == 400
    assert load_run_settings(project).openrouter_provider == 'deepinfra/fp8'
    page = client.get(f'/test/projects/{project.project_id}?tab=run').text
    assert '<option value="deepinfra/fp8" selected>deepinfra/fp8</option>' in page
    assert '<option value="deny" selected>' in page
    # The form always sends both fields; leaving them out means automatic routing with fallbacks.
    assert client.post(target, data=base).status_code == 303
    settings = load_run_settings(project)
    assert (settings.openrouter_provider, settings.openrouter_allow_fallbacks) == ('', True)
    page = client.get(f'/test/projects/{project.project_id}?tab=run').text
    assert '<option value="allow" selected>' in page and 'value="deepinfra/fp8"' not in page


def test_run_forwards_saved_routing(site, monkeypatch):
    client, project, runtime = site
    save_run_settings(project, RunSettings(provider='openrouter', model='vendor/model', openrouter_provider='groq', openrouter_allow_fallbacks=False))
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    assert client.post(f'/test/projects/{project.project_id}/run').status_code == 303
    kwargs = start.call_args.kwargs
    assert (kwargs['openrouter_provider'], kwargs['openrouter_allow_fallbacks']) == ('groq', False)


def test_endpoint_api_uses_only_a_session_key_and_maps_errors(site, monkeypatch):
    client, _, runtime = site
    listing = [{'tag': 'groq', 'name': 'Groq'}]
    store = Mock()
    monkeypatch.setattr(runtime, 'prepare_provider', store)
    endpoints = Mock(return_value=listing)
    monkeypatch.setattr(web, 'list_model_endpoints', endpoints)
    response = client.get('/test/api/openrouter/endpoints', params={'model': 'vendor/model'})
    assert (response.status_code, response.json()) == (200, listing)
    endpoints.assert_called_once_with('vendor/model', api_key='session-key')
    runtime._session_keys.clear()
    client.get('/test/api/openrouter/endpoints', params={'model': 'vendor/model'})
    endpoints.assert_called_with('vendor/model', api_key='')
    store.assert_not_called()
    endpoints.side_effect = ProviderError('OpenRouter is down')
    response = client.get('/test/api/openrouter/endpoints', params={'model': 'vendor/model'})
    assert (response.status_code, response.json()['detail']) == (502, 'OpenRouter is down')
    endpoints.side_effect = ValueError('not a model')
    assert client.get('/test/api/openrouter/endpoints', params={'model': 'x'}).status_code == 400


# Provenance ------------------------------------------------------------------

def run_and_wait(project, **options):
    run = runner.Runner()
    state = run.start(project=project, manual=parse_coding_manual(MANUAL),
                      coding_sheet=CodingSheet([CodingSheetRow('r1', 'paper.pdf', 'exp')], []),
                      api_key='secret', model='vendor/model', only_pdfs=['paper.pdf'], **options)
    deadline = time.monotonic() + 10
    while run.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(.01)
    assert state.status == 'complete'
    operations = [json.loads(path.read_text()) for path in (project.path / 'audit/operations').glob('*/operation.json')]
    return {op['operation']: op for op in operations}


def test_run_provenance_records_openrouter_routing(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    project = create_project('Audit', root=tmp_path / 'projects')
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    sent = []

    def transport(request, **kwargs):
        sent.append(json.loads(request.data))
        return json.dumps({'provider': 'Groq', 'choices': [{'message': {'content': '{"effects": []}'}}]}).encode()

    monkeypatch.setattr(openrouter, 'cancellable_urlopen', transport)
    operations = run_and_wait(project, provider='openrouter', openrouter_provider='groq', openrouter_allow_fallbacks=False)
    expected = {'openrouter_provider': 'groq', 'openrouter_allow_fallbacks': False}
    for name in ('extraction_run', 'extraction'):
        assert {key: operations[name]['settings'][key] for key in expected} == expected
    assert sent[0]['provider'] == {'order': ['groq'], 'allow_fallbacks': False}


def test_other_providers_record_no_routing(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    project = create_project('Audit', root=tmp_path / 'projects')
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    extract = Mock(return_value=ExtractionResult('paper.pdf', 'ok'))
    monkeypatch.setattr(runner, 'extract_pdf_effects', extract)
    operations = run_and_wait(project, provider='gemini', openrouter_provider='groq')
    assert 'openrouter_provider' not in operations['extraction_run']['settings']
    assert 'openrouter_provider' not in operations['extraction']['settings']
    assert 'openrouter_provider' not in extract.call_args.kwargs
