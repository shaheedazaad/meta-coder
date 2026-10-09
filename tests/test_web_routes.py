"""Exercise real ASGI routes, multipart uploads and downloadable artifacts."""
import io
import json
import zipfile
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from meta_coder import web
from meta_coder.manual import manual_to_editor_payload, parse_coding_manual
from meta_coder.projects import create_project


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Route study', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def url(project, suffix=''):
    return f'/test/projects/{project.project_id}{suffix}'


def test_root_and_static_files(site):
    client, _, _ = site
    assert client.get('/').status_code == 404
    assert client.get('/wrong/').status_code == 404
    assert client.get('/test/static/app.js').status_code == 200
    assert client.get('/test/static/missing').status_code == 404
    assert client.get('/test/static/%2e%2e/web.py').status_code == 404
    template = client.get('/test/coding-sheet-template.csv')
    assert template.status_code == 200
    assert template.text.startswith('row_id,source_pdf,locator')
    assert 'attachment' in template.headers['content-disposition']


@pytest.mark.parametrize(('headers', 'status'), [
    ({'host': 'evil.test'}, 400), ({'origin': 'https://evil.test'}, 403),
    ({'sec-fetch-site': 'cross-site'}, 403), ({'origin': 'http://localhost/'}, 303),
])
def test_local_security_checks_mutations(site, headers, status):
    client, _, _ = site
    assert client.post('/test/projects', data={'name': 'New'}, headers=headers).status_code == status


def test_create_and_delete_routes(site, monkeypatch):
    client, project, runtime = site
    assert client.post('/test/projects', data={'name': ' '}).status_code == 400
    created = client.post('/test/projects', data={'name': 'Created'})
    assert created.status_code == 303
    assert client.get(created.headers['location']).status_code == 200
    assert client.post('/test/projects/invalid/delete').status_code == 404
    assert client.get('/test/projects/invalid').status_code == 404
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert client.post(url(project, '/delete')).status_code == 409
    assert project.path.exists()
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: False)
    assert client.post(url(project, '/delete')).status_code == 303
    assert not project.path.exists()


def test_clear_output_blocks_active_run_and_preserves_inputs(site, monkeypatch):
    client, project, runtime = site
    result = project.output_dir / 'coded_data.csv'
    result.write_text('results')
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert client.post(url(project, '/clear-output')).status_code == 409
    assert result.exists()
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: False)
    assert client.post(url(project, '/clear-output')).status_code == 303
    assert not result.exists() and project.manual_path.exists()


@pytest.mark.parametrize(('platform', 'opener'), [('darwin', 'open'), ('win32', 'explorer'), ('linux', 'xdg-open')])
def test_open_folder_uses_native_command_and_reports_failure(site, monkeypatch, platform, opener):
    client, project, _ = site
    launch = Mock()
    monkeypatch.setattr(web.sys, 'platform', platform)
    monkeypatch.setattr(web.subprocess, 'Popen', launch)
    assert client.post(url(project, '/open-folder')).status_code == 303
    launch.assert_called_once_with([opener, str(project.path)])
    launch.side_effect = OSError('no file manager')
    response = client.post(url(project, '/open-folder'))
    assert response.status_code == 303 and 'error=' in response.headers['location']


def test_export_zip_and_csv_downloads(site):
    client, project, _ = site
    for kind, filename in [('coded', 'coded_data.csv'), ('evidence', 'evidence.csv')]:
        assert client.get(url(project, '/download/' + kind)).status_code == 404
        (project.output_dir / filename).write_bytes(b'row_id,value\nr1,1\n')
        response = client.get(url(project, '/download/' + kind))
        assert response.status_code == 200 and response.text == 'row_id,value\nr1,1\n'
        assert filename in response.headers['content-disposition']
    response = client.get(url(project, '/download/zip'))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert 'coding_manual.yml' in archive.namelist()
        assert 'output/coded_data.csv' in archive.namelist()
        assert not any('.meta_coder' in name for name in archive.namelist())


def test_raw_and_audit_artifacts_reject_escape_and_bad_data(site, tmp_path):
    client, project, _ = site
    outside = tmp_path / 'outside'
    outside.write_text('private')
    for directory, route in [(project.raw_dir, 'raw'), (project.audit_dir, 'audit')]:
        (directory / 'escape').symlink_to(outside)
        assert client.get(url(project, f'/{route}/escape')).status_code == 404
        assert client.get(url(project, f'/{route}/missing')).status_code == 404
    (project.raw_dir / 'bad.json').write_text('{broken')
    assert client.get(url(project, '/raw/bad.json')).status_code == 422
    (project.audit_dir / 'paper.yml').write_text('estimate: 1')
    response = client.get(url(project, '/audit/paper.yml'))
    assert response.status_code == 200 and response.text == 'estimate: 1'
    assert response.headers['content-type'].startswith('text/plain')


def test_manual_save_validation_import_and_reset(site):
    client, project, _ = site
    original = project.manual_path.read_text()
    assert client.post(url(project, '/manual'), data={'manual_json': '{'}).status_code == 400
    payload = {'effect_definition': '', 'effects': [{'name': 'age', 'type': 'number'}]}
    rejected = client.post(url(project, '/manual'), data={'manual_json': json.dumps(payload)})
    assert rejected.status_code == 400 and 'effect_definition' in rejected.text
    assert project.manual_path.read_text() == original
    manual = parse_coding_manual('effect_definition: comparison\neffects:\n  age: {type: number}\n')
    assert client.post(url(project, '/manual'), data={'manual_json': json.dumps(manual_to_editor_payload(manual))}).status_code == 303
    assert 'comparison' in project.manual_path.read_text()
    response = client.post(url(project, '/manual/import'), files={'file': ('manual.yml', b'bad')})
    assert 'error=' in response.headers['location']
    response = client.post(url(project, '/manual/import'), files={'file': ('manual.yml', b'effect_definition: new comparison\neffects:\n  age: {type: number}\n')})
    assert response.status_code == 303 and 'new comparison' in project.manual_path.read_text()
    assert client.post(url(project, '/manual/reset')).status_code == 303
    assert project.manual_path.read_text() == original


def test_upload_routes_and_scanner_dispatch(site):
    client, project, runtime = site
    response = client.post(url(project, '/uploads'), files=[('files', ('ok.PDF', b'%PDF-good')), ('files', ('bad.pdf', b'bad'))])
    assert response.status_code == 303 and 'error=' in response.headers['location']
    assert (project.sources_dir / 'ok.PDF').read_bytes() == b'%PDF-good'
    runtime.pdf_scanner.scan_async.assert_called_once()
    assert client.post(url(project, '/uploads'), files={'files': ('other.pdf', b'%PDF-ok')}).status_code == 303
    assert client.post(url(project, '/uploads/delete'), data={'filename': 'ok.PDF'}).status_code == 303
    assert not (project.sources_dir / 'ok.PDF').exists()
    note = project.sources_dir / 'notes.txt'
    note.write_text('keep')
    assert client.post(url(project, '/uploads/delete'), data={'filename': 'notes.txt'}).status_code == 303
    assert note.exists()
    response = client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', b'row_id,source_pdf,locator,authors,year\nr1,other.pdf,exp1,Smith,2024\n')})
    assert response.status_code == 303 and 'r1,other.pdf' in project.coding_sheet_path.read_text()


def test_idle_and_running_status_and_cancel(site, monkeypatch):
    client, project, runtime = site
    assert client.get(url(project, '/status')).json() == {'status': 'idle'}
    assert client.get(url(project, '/pdf-scan/status')).json() == {'status': 'idle', 'total': 0, 'processed': 0}
    for service, suffix in [(runtime.runner, '/status'), (runtime.pdf_scanner, '/pdf-scan/status')]:
        state = Mock()
        state.snapshot.return_value = {'status': 'running', 'processed': 1}
        monkeypatch.setattr(service, 'state', lambda _, state=state: state)
        assert client.get(url(project, suffix)).json() == state.snapshot.return_value
    cancel = Mock()
    monkeypatch.setattr(runtime.runner, 'cancel', cancel)
    assert client.post(url(project, '/run/cancel')).status_code == 303
    cancel.assert_called_once_with(project.project_id)


def ready_project(project, runtime):
    from meta_coder.projects import write_manual
    write_manual(project, parse_coding_manual('effect_definition: comparison\neffects:\n  estimate: {type: number}\n'))
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,paper.pdf,exp1,Smith,2024\n')
    runtime._session_keys['gemini'] = 'fake-test-key'


def test_key_routes_validate_save_clear_and_surface_store_errors(site, monkeypatch):
    client, _, runtime = site
    save = Mock()
    clear = Mock()
    monkeypatch.setattr(web.credentials, 'save_key', save)
    monkeypatch.setattr(web.credentials, 'clear_key', clear)
    assert client.post('/test/settings/api-key', data={'provider': 'unknown', 'api_key': 'x'}).status_code == 400
    assert client.post('/test/settings/api-key', data={'provider': 'gemini', 'api_key': ' '}).status_code == 400
    assert client.post('/test/settings/api-key/clear', data={'provider': 'unknown'}).status_code == 400
    assert client.post('/test/settings/api-key', data={'provider': 'gemini', 'api_key': ' key '}).status_code == 303
    save.assert_called_once_with('gemini', 'key')
    assert runtime.api_key('gemini') == 'key'
    assert client.post('/test/settings/api-key/clear', data={'provider': 'gemini'}).status_code == 303
    assert runtime.api_key('gemini') is None and not runtime.has_saved_key('gemini')
    save.side_effect = web.credentials.CredentialStoreError('locked')
    response = client.post('/test/settings/api-key', data={'provider': 'gemini', 'api_key': 'key'})
    assert 'error=locked' in response.headers['location']
    clear.side_effect = web.credentials.CredentialStoreError('locked')
    response = client.post('/test/settings/api-key/clear', data={'provider': 'gemini'})
    assert 'error=locked' in response.headers['location']
    response = client.post('/test/settings/openai-compatible', data={'openai_base_url': 'http://localhost/v1', 'api_key': 'key'})
    assert 'error=locked' in response.headers['location']
    response = client.post('/test/settings/openai-compatible', data={'openai_base_url': 'http://localhost/v1', 'openai_response_format': 'xml'})
    assert 'error=' in response.headers['location']


def test_manual_draft_returns_unsaved_candidate_and_expected_errors(site, monkeypatch):
    client, project, runtime = site
    original = project.manual_path.read_text()
    target = url(project, '/manual/draft')
    uploaded = {'file': ('manual.md', b'Human coding instructions')}
    assert client.post(target, files=uploaded).status_code == 400
    runtime._session_keys['gemini'] = 'fake'
    draft = parse_coding_manual('effect_definition: draft comparison\neffects:\n  age: {type: number}\n')
    generate = Mock(return_value=draft)
    monkeypatch.setattr(web, 'draft_coding_manual', generate)
    response = client.post(target, files=uploaded)
    assert response.status_code == 200
    assert response.json()['manual']['effect_definition'] == 'draft comparison'
    assert response.json()['filename'] == 'manual.md'
    assert project.manual_path.read_text() == original
    assert generate.call_args.kwargs['document_text'] == 'Human coding instructions'
    assert client.post(target, files={'file': ('manual.exe', b'bad')}).status_code == 400
    generate.side_effect = web.ProviderError('offline')
    response = client.post(target, files=uploaded)
    assert response.status_code == 502 and response.json()['error'] == 'offline'


def test_sheet_draft_setup_validation_and_provider_errors(site, monkeypatch):
    client, project, runtime = site
    target = url(project, '/coding-sheet/draft')
    uploaded = {'file': ('sheet.csv', b'citation\nSmith 2024')}
    assert client.post(target, files=uploaded).status_code == 400
    runtime._session_keys['gemini'] = 'fake'
    assert client.post(target, files=uploaded).status_code == 400  # incomplete starter manual
    ready_project(project, runtime)
    monkeypatch.setattr(web, 'draft_coding_sheet', Mock(side_effect=web.ProviderError('offline')))
    response = client.post(target, files=uploaded)
    assert response.status_code == 502 and response.json()['error'] == 'offline'


def test_match_and_unmatch_routes_only_accept_uploaded_files(site):
    client, project, _ = site
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,missing.pdf,exp,Smith,2024\n')
    target = url(project, '/coding-sheet/match-pdfs')
    assert client.post(target, data={'paper_key': 'missing.pdf', 'filename': '../outside.pdf'}).status_code == 303
    assert 'missing.pdf' in project.coding_sheet_path.read_text()
    assert client.post(target, data={'paper_key': 'missing.pdf', 'filename': 'paper.pdf', 'save_key': 'missing.pdf'}).status_code == 303
    assert 'r1,paper.pdf,' in project.coding_sheet_path.read_text()
    assert client.post(url(project, '/coding-sheet/unmatch-pdf'), data={'source_pdf': 'absent.pdf'}).status_code == 303
    assert 'r1,paper.pdf,' in project.coding_sheet_path.read_text()
    assert client.post(url(project, '/coding-sheet/unmatch-pdf'), data={'source_pdf': 'paper.pdf'}).status_code == 303
    assert 'r1,,exp' in project.coding_sheet_path.read_text()
    assert client.post(target, data={'paper_key': 'missing.pdf', 'filename': 'paper.pdf', 'save_key': 'different'}).status_code == 303


def test_start_run_validates_readiness_and_forwards_settings(site, monkeypatch):
    client, project, runtime = site
    target = url(project, '/run')
    response = client.post(target)
    assert response.status_code == 400 and 'API key' in response.text
    ready_project(project, runtime)
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    assert client.post(target).status_code == 303
    assert start.call_args.kwargs['api_key'] == 'fake-test-key'
    assert start.call_args.kwargs['coding_sheet'].rows[0].row_id == 'r1'
    monkeypatch.setattr(runtime, 'prepare_provider', lambda *a: 'Keychain locked')
    assert client.post(target).json()['detail'] == 'Keychain locked'
    project.manual_path.write_text('broken')
    assert 'manual has a validation error' in client.post(target).text
    project.coding_sheet_path.write_text('wrong,header\n')
    assert 'coding sheet has validation errors' in client.post(target).text
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert 'already in progress' in client.post(target).text


def test_retry_eligibility_and_explicit_or_all_failed_targets(site, monkeypatch):
    from meta_coder.runner import PdfProgress, RunState
    client, project, runtime = site
    target = url(project, '/run/retry')
    assert client.post(target).status_code == 400  # missing key
    runtime._session_keys['gemini'] = 'fake'
    assert 'Fix the coding manual' in client.post(target).text
    ready_project(project, runtime)
    assert 'Nothing to retry' in client.post(target).text
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('paper.pdf', status='error'), PdfProgress('done.pdf', status='ok')])
    assert client.post(target).status_code == 303
    assert start.call_args.kwargs['only_pdfs'] == ['paper.pdf']
    assert client.post(target, data={'source_pdf': 'paper.pdf'}).status_code == 303
    monkeypatch.setattr(runtime, 'prepare_provider', lambda *a: 'locked')
    assert client.post(target, data={'source_pdf': 'paper.pdf'}).json()['detail'] == 'locked'
    project.coding_sheet_path.write_text('wrong,header\n')
    assert 'Fix the coding sheet' in client.post(target).text
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert client.post(target).status_code == 409


def test_endpoint_readiness_rejects_missing_base_url_and_model(site):
    from meta_coder.settings import RunSettings, save_run_settings
    client, project, runtime = site
    ready_project(project, runtime)
    save_run_settings(project, RunSettings(provider='openai_compatible'))
    assert 'configure the OpenAI-compatible' in client.post(url(project, '/run')).text
    assert 'Configure the OpenAI-compatible' in client.post(url(project, '/run/retry')).text


def test_runtime_setup_rejects_empty_model_and_stale_saved_key(site, monkeypatch):
    _, _, runtime = site
    assert 'Enter a model ID' in runtime.provider_setup_error('gemini', '')
    runtime._saved_keys.add('gemini')
    monkeypatch.setattr(web.credentials, 'load_key', lambda _: None)
    monkeypatch.setattr(web.credentials, 'mark_saved_key_configured', lambda *a: True)
    assert 'no longer in the credential store' in runtime.prepare_provider('gemini', 'model')


def test_launch_binds_loopback_and_optionally_opens_browser(monkeypatch, capsys):
    import uvicorn
    fake_app = object()
    monkeypatch.setattr(web, 'create_app', lambda **kw: fake_app)
    monkeypatch.setattr(web.secrets, 'token_urlsafe', lambda _: 'token')
    server = Mock()
    timer = Mock()
    monkeypatch.setattr(uvicorn, 'run', server)
    monkeypatch.setattr(web.threading, 'Timer', timer)
    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 4567)
    socket_context = Mock()
    socket_context.__enter__ = Mock(return_value=sock)
    socket_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(web.socket, 'socket', Mock(return_value=socket_context))
    assert web.launch_web() == 0
    sock.bind.assert_called_once_with(('127.0.0.1', 0))
    server.assert_called_once_with(fake_app, host='127.0.0.1', port=4567, log_level='warning', access_log=False)
    assert timer.call_args.kwargs['args'] == ('http://127.0.0.1:4567/token/',)
    assert 'http://127.0.0.1:4567/token/' in capsys.readouterr().out
    timer.reset_mock()
    assert web.launch_web(open_browser=False, port=8765) == 0
    timer.assert_not_called()


def test_run_table_sorting_and_yaml_highlighting_are_safe():
    rows = [{'source_pdf': 'Z.pdf', 'input_tokens': 12}, {'source_pdf': 'a.pdf', 'input_tokens': None}]
    assert web._sort_run_rows(rows, 'invalid', 'asc')[0]['source_pdf'] == 'a.pdf'
    assert web._sort_run_rows(rows, 'input_tokens', 'asc')[0]['input_tokens'] is None
    assert web._sort_run_rows(rows, 'input_tokens', 'desc')[0]['input_tokens'] == 12
    rendered = str(web._highlight_yaml('<script>\nkey: value # comment\nempty:\n'))
    assert '<script>' not in rendered and '&lt;script&gt;' in rendered
    assert 'yaml-comment' in rendered and 'yaml-key' in rendered


def test_run_explains_empty_sheet_and_already_completed_results(site):
    from meta_coder.extraction import ExtractionResult
    from meta_coder.runner import write_raw_result
    client, project, runtime = site
    ready_project(project, runtime)
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\n')
    assert 'no matched rows' in client.post(url(project, '/run')).text
    ready_project(project, runtime)
    write_raw_result(project, ExtractionResult('paper.pdf', 'ok'), provider='gemini', model='test')
    assert 'already coded' in client.post(url(project, '/run')).text


def test_valid_project_settings_save_without_warning(site, monkeypatch):
    from meta_coder.settings import load_run_settings
    client, project, runtime = site
    ready_project(project, runtime)
    monkeypatch.setattr(web, 'check_model', Mock(return_value=[]))
    response = client.post(url(project, '/settings'), data={'provider': 'gemini', 'previous_provider': 'gemini', 'model': 'valid-model', 'parallel_requests': 3})
    assert response.headers['location'] == url(project, '?tab=run')
    settings = load_run_settings(project)
    assert settings.model == 'valid-model' and settings.parallel_requests == 3


def test_saving_identical_converted_sheet_keeps_results(site):
    client, project, _ = site
    text = 'row_id,source_pdf,locator,authors,year,title,doi\nr1,paper.pdf,exp,Smith,2024,,\n'
    project.coding_sheet_path.write_text(text)
    result = project.output_dir / 'coded_data.csv'
    result.write_text('keep')
    assert client.post(url(project, '/coding-sheet/draft/save'), data={'csv_text': text}).status_code == 200
    assert result.read_text() == 'keep'


def test_all_invalid_uploads_do_not_trigger_scanning(site):
    client, project, runtime = site
    response = client.post(url(project, '/uploads'), files={'files': ('bad.txt', b'not pdf')})
    assert response.status_code == 303 and 'error=' in response.headers['location']
    runtime.pdf_scanner.scan_async.assert_not_called()


def test_model_warning_preserves_saved_settings(site, monkeypatch):
    from meta_coder.settings import load_run_settings
    client, project, runtime = site
    ready_project(project, runtime)
    monkeypatch.setattr(web, 'check_model', Mock(return_value=['Model lacks structured output']))
    response = client.post(url(project, '/settings'), data={'provider': 'gemini', 'previous_provider': 'gemini', 'model': 'model'})
    assert response.status_code == 303 and 'warning=' in response.headers['location']
    assert load_run_settings(project).model == 'model'


@pytest.mark.parametrize('raw,status', [
    (b'row_id,source_pdf,locator,authors,year\nr1,p.pdf,,Smith,2020,extra\n', 400),
    (b'row_id,source_pdf,locator,authors,year\nr1,p.pdf,,M\xfcller,2020\n', 400),
    (b'x' * 25, 413),
])
def test_direct_sheet_rejection_preserves_inputs_and_results(site, monkeypatch, raw, status):
    client, project, _ = site
    if status == 413:
        monkeypatch.setattr(web, 'load_app_settings', lambda: Mock(upload_size_cap_bytes=24))
    previous = project.coding_sheet_path.read_bytes()
    output = project.raw_dir / 'result.json'
    output.write_text('valuable')
    response = client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', raw)})
    assert response.status_code == status
    assert project.coding_sheet_path.read_bytes() == previous
    assert output.read_text() == 'valuable'


def test_direct_sheet_bom_and_unchanged_save(site):
    client, project, _ = site
    text = 'row_id,source_pdf,locator,authors,year\nr1,missing.pdf,,Müller,2020\n'
    response = client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', ('\ufeff' + text).encode())})
    assert response.status_code == 303
    assert project.coding_sheet_path.read_text() == text
    output = project.raw_dir / 'result.json'
    output.write_text('valuable')
    assert client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', text.encode())}).status_code == 303
    assert output.read_text() == 'valuable'


def test_direct_sheet_upload_refuses_active_run(site, monkeypatch):
    client, project, runtime = site
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    previous = project.coding_sheet_path.read_bytes()
    raw = b'row_id,source_pdf,locator,authors,year\nr1,p.pdf,,Smith,2020\n'
    assert client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', raw)}).status_code == 409
    assert project.coding_sheet_path.read_bytes() == previous
