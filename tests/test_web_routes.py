"""Exercise real ASGI routes, multipart uploads and downloadable artifacts."""
import io
import json
import re
import zipfile
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from meta_coder import extractors, pdf_matching, web
from meta_coder.app_settings import AppSettings, save_app_settings
from meta_coder.settings import load_run_settings, save_project_extractor_id
from meta_coder.manual import manual_to_editor_payload, parse_coding_manual
from meta_coder.projects import create_project


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
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
    for attempt in ['/audit/..%2Fraw%2Fbad.json', '/audit/%2E%2E%2F%2E%2E%2Fcoding_manual.yml']:
        assert client.get(url(project, attempt)).status_code == 404


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


def test_existing_manual_with_reserved_field_name_loads_with_repairable_error(site):
    # Manuals saved before reserved names were rejected must still open: the
    # page shows the error and the import/reset recovery, and keeps the file.
    client, project, _ = site
    legacy = 'effect_definition: comparison\neffects:\n  year: {type: integer}\n'
    project.manual_path.write_text(legacy)
    page = client.get(url(project))
    assert page.status_code == 200
    assert 'is reserved for a column MetaCoder adds' in page.text
    assert 'publication_year' in page.text
    assert 'Reset to a blank manual' in page.text and '/manual/import' in page.text
    assert project.manual_path.read_text() == legacy
    rejected = client.post(url(project, '/manual'), data={'manual_json': json.dumps(
        {'effect_definition': 'comparison', 'effects': [{'name': 'Year', 'type': 'integer'}]}
    )})
    assert rejected.status_code == 400 and 'is reserved' in rejected.text
    assert project.manual_path.read_text() == legacy


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
    give_key(runtime, choose_extractor(project))


def choose_extractor(project, **values):
    """The project's extractor, creating and choosing a Gemini one if it has none."""
    chosen = extractors.project_extractor(project).extractor
    if chosen is None:
        chosen = extractors.add_extractor(extractors.validate_new_extractor(
            {'name': 'Test ' + project.project_id, 'provider': 'gemini', 'model': 'test-model', **values}))
        save_project_extractor_id(project, chosen.id)
    return chosen


def give_key(runtime, extractor, key='fake-test-key'):
    """Hold a key in the session's vault, as if it had already been read."""
    runtime.vault._keys = {**(runtime.vault._keys or {}), extractor.id: key}


def drafting_ready(runtime, key='fake'):
    extractor = extractors.add_extractor(extractors.validate_new_extractor(
        {'name': 'Drafting', 'provider': 'gemini', 'model': 'draft-model'}))
    save_app_settings(AppSettings(drafting_extractor_id=extractor.id))
    give_key(runtime, extractor, key)
    return extractor


def test_manual_draft_returns_unsaved_candidate_and_expected_errors(site, monkeypatch):
    client, project, runtime = site
    original = project.manual_path.read_text()
    target = url(project, '/manual/draft')
    uploaded = {'file': ('manual.md', b'Human coding instructions')}
    assert 'Choose an extractor for drafting' in client.post(target, files=uploaded).json()['error']
    drafting_ready(runtime)
    draft = parse_coding_manual('effect_definition: draft comparison\neffects:\n  age: {type: number}\n')
    generate = Mock(return_value=draft)
    monkeypatch.setattr(web, 'draft_coding_manual', generate)
    response = client.post(target, files=uploaded)
    assert response.status_code == 200
    assert response.json()['manual']['effect_definition'] == 'draft comparison'
    assert response.json()['filename'] == 'manual.md'
    assert project.manual_path.read_text() == original
    assert generate.call_args.kwargs['document_text'] == 'Human coding instructions'
    assert generate.call_args.kwargs['model'] == 'draft-model'
    assert generate.call_args.kwargs['api_key'] == 'fake'
    assert response.json()['extractor'] == 'Drafting'
    assert client.post(target, files={'file': ('manual.exe', b'bad')}).status_code == 400
    generate.side_effect = web.ProviderError('offline')
    response = client.post(target, files=uploaded)
    assert response.status_code == 502 and response.json()['error'] == 'offline'


def test_sheet_draft_setup_validation_and_provider_errors(site, monkeypatch):
    client, project, runtime = site
    target = url(project, '/coding-sheet/draft')
    uploaded = {'file': ('sheet.csv', b'citation\nSmith 2024')}
    assert client.post(target, files=uploaded).status_code == 400
    drafting_ready(runtime)
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
    assert response.status_code == 400 and 'Choose an extractor for this project' in response.text
    ready_project(project, runtime)
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    assert client.post(target).status_code == 303
    assert start.call_args.kwargs['api_key'] == 'fake-test-key'
    assert start.call_args.kwargs['coding_sheet'].rows[0].row_id == 'r1'
    assert start.call_args.kwargs['model'] == 'test-model'
    assert start.call_args.kwargs['extractor']['name'] == 'Test ' + project.project_id
    saved = load_run_settings(project)
    assert saved.extractor_id == start.call_args.kwargs['extractor']['id']
    assert saved.extractor['model'] == 'test-model'
    monkeypatch.setattr(runtime, 'prepare', lambda *a: ('', 'Keychain locked'))
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
    assert client.post(target).status_code == 400  # no extractor
    give_key(runtime, choose_extractor(project))
    assert 'Fix the coding manual' in client.post(target).text
    ready_project(project, runtime)
    assert 'Nothing to retry' in client.post(target).text
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('paper.pdf', status='error'), PdfProgress('done.pdf', status='ok')])
    assert client.post(target).status_code == 303
    assert start.call_args.kwargs['only_pdfs'] == ['paper.pdf']
    assert client.post(target, data={'source_pdf': 'paper.pdf'}).status_code == 303
    monkeypatch.setattr(runtime, 'prepare', lambda *a: ('', 'locked'))
    assert client.post(target, data={'source_pdf': 'paper.pdf'}).json()['detail'] == 'locked'
    project.coding_sheet_path.write_text('wrong,header\n')
    assert 'Fix the coding sheet' in client.post(target).text
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert client.post(target).status_code == 409


def test_endpoint_readiness_rejects_missing_base_url_and_model(site):
    client, project, runtime = site
    ready_project(project, runtime)
    incomplete = extractors.add_extractor(extractors.Extractor(
        id='endpoint', name='Endpoint', provider='openai_compatible', model='local'))
    save_project_extractor_id(project, incomplete.id)
    assert 'Enter the API base URL for the “Endpoint” extractor' in client.post(url(project, '/run')).text
    assert 'Enter the API base URL' in client.post(url(project, '/run/retry')).text
    extractors.update_extractor('endpoint', {'name': 'Endpoint', 'model': 'local', 'base_url': 'http://localhost/v1'})
    assert web._project_view(runtime, project)['run_setup_error'] is None  # no key needed
    nameless = extractors.add_extractor(extractors.Extractor(id='nameless', name='No model', provider='gemini', model=''))
    save_project_extractor_id(project, nameless.id)
    assert 'Choose a model for the “No model” extractor' in client.post(url(project, '/run')).text
    extractors.delete_extractor('nameless')
    assert 'chosen for this project was deleted' in client.post(url(project, '/run')).text


def test_runtime_reports_and_repairs_a_stale_saved_key(site):
    _, project, runtime = site
    extractor = choose_extractor(project)
    extractors.set_key_saved(extractor.id, True)
    extractor = extractors.get_extractor(extractor.id)
    assert runtime.setup_error(extractor) is None  # judged without opening the store
    key, error = runtime.prepare(extractor)
    assert key == '' and 'no longer in the credential store' in error
    assert extractors.get_extractor(extractor.id).key_saved is False
    assert 'Add an API key' in runtime.setup_error(extractors.get_extractor(extractor.id))


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


def test_results_table_links_per_pdf_yaml_only_when_written(site):
    from meta_coder.extraction import ExtractionResult
    from meta_coder.runner import audit_yaml_path, write_raw_result
    client, project, runtime = site
    ready_project(project, runtime)
    write_raw_result(project, ExtractionResult('paper.pdf', 'ok'), provider='gemini', model='test')
    audit_path = audit_yaml_path(project, 'paper.pdf')
    link = url(project, f'/audit/{audit_path.name}')
    page = client.get(url(project, '?tab=results')).text
    assert 'view raw output (YAML)' in page
    assert link not in page and 'view coded effects (YAML)' not in page
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_bytes(b'source_pdf: paper.pdf\n')
    page = client.get(url(project, '?tab=results')).text
    assert f'href="{link}"' in page and 'view coded effects (YAML)' in page
    response = client.get(link)
    assert response.status_code == 200 and response.text == 'source_pdf: paper.pdf\n'
    assert response.headers['content-type'].startswith('text/plain')


def test_project_chooses_an_existing_extractor(site, monkeypatch):
    from meta_coder.settings import load_project_extractor_id
    client, project, runtime = site
    other = extractors.add_extractor(extractors.validate_new_extractor(
        {'name': 'Other', 'provider': 'openrouter', 'model': 'vendor/model', 'parallel_requests': 3}))
    response = client.post(url(project, '/settings'), data={'extractor_id': other.id})
    assert response.headers['location'] == url(project, '?tab=run')
    assert load_project_extractor_id(project) == other.id
    assert web._project_view(runtime, project)['run_settings'].parallel_requests == 3
    response = client.post(url(project, '/settings'), data={'extractor_id': 'missing'})
    assert 'error=' in response.headers['location']
    assert load_project_extractor_id(project) == other.id
    monkeypatch.setattr(runtime.runner, 'is_running', lambda _: True)
    assert client.post(url(project, '/settings'), data={'extractor_id': other.id}).status_code == 409


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


def test_results_table_shows_latest_attempts_outdated_and_unverified_results(site):
    from meta_coder.extraction import ExtractionResult
    from meta_coder.fingerprints import InputChecker
    from meta_coder.runner import PdfProgress, RunState, write_raw_result
    client, project, runtime = site
    ready_project(project, runtime)
    (project.sources_dir / 'old.pdf').write_bytes(b'%PDF-old')
    (project.sources_dir / 'legacy.pdf').write_bytes(b'%PDF-legacy')
    project.coding_sheet_path.write_text(project.coding_sheet_path.read_text()
                                         + 'r2,old.pdf,exp1,Lee,2021\nr3,legacy.pdf,exp1,Kim,2022\n')
    view = web._project_view(runtime, project)
    settings = view['run_settings']
    checker = InputChecker(project, view['manual'], view['coding_sheet'], provider=settings.provider, model=settings.model)
    for name in ['paper.pdf', 'old.pdf']:
        write_raw_result(project, ExtractionResult(name, 'ok', input_fingerprint=checker.fingerprint(name)),
                         provider='gemini', model='model')
    write_raw_result(project, ExtractionResult('paper.pdf', 'error', error='Timed out', input_fingerprint=checker.fingerprint('paper.pdf')),
                     provider='gemini', model='model')
    write_raw_result(project, ExtractionResult('legacy.pdf', 'ok'), provider='gemini', model='model')
    (project.output_dir / 'coded_data.csv').write_text('row_id\n')
    (project.output_dir / 'evidence.csv').write_text('row_id\n')
    (project.sources_dir / 'old.pdf').write_bytes(b'%PDF-replaced')
    # The last run's progress can't hide that old.pdf's inputs changed since.
    runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('old.pdf', status='ok')])

    view = web._project_view(runtime, project)
    assert {article['source_pdf'] for article in view['completed_articles']} == {'paper.pdf', 'legacy.pdf'}
    assert view['pending_pdf_count'] == 1 and view['stale_result_count'] == 1
    page = client.get(url(project, '?tab=results')).text
    assert 'Timed out' in page and 'The earlier successful result is still used.' in page
    assert '>outdated<' in page and '1 result is outdated' in page
    assert "Coded before MetaCoder recorded each result" in page
    assert '.attempt.json' in page  # the raw link opens the latest attempt
    retry_targets = re.findall(r'run/retry">\s*<input type="hidden" name="source_pdf" value="([^"]+)"', page)
    assert sorted(retry_targets) == ['old.pdf', 'paper.pdf']  # the failed attempt and the outdated result


def test_clearing_output_or_manual_forgets_finished_progress(site):
    from meta_coder.runner import PdfProgress, RunState
    client, project, runtime = site
    ready_project(project, runtime)
    for route in ['/clear-output', '/manual/reset']:
        runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('paper.pdf', status='error')])
        assert client.post(url(project, route)).status_code == 303
        assert runtime.runner.state(project.project_id) is None
    sheet = 'row_id,source_pdf,locator,authors,year\nr2,paper.pdf,exp2,Jones,2025\n'
    for route, request in [('/coding-sheet', {'files': {'file': ('sheet.csv', sheet.encode())}}),
                           ('/coding-sheet/draft/save', {'data': {'csv_text': 'row_id,source_pdf,locator,authors,year,title,doi\nr3,paper.pdf,exp,Smith,2024,,\n'}})]:
        runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('paper.pdf', status='error')])
        response = client.post(url(project, route), **request)
        assert response.status_code in (200, 303), (route, response.status_code, response.text)
        assert runtime.runner.state(project.project_id) is None


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
    assert project.coding_sheet_path.read_text(encoding='utf-8') == text
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


def project_files(project):
    return {path: path.read_bytes() for path in sorted(project.path.rglob('*')) if path.is_file()}


VALID_MANUAL = 'effect_definition: new comparison\neffects:\n  age: {type: number}\n'
NEW_SHEET = 'row_id,source_pdf,locator,authors,year\nr2,other.pdf,exp2,Jones,2025\n'
GUARDED_EDITS = {
    'save manual': ('/manual', {'data': {'manual_json': json.dumps(manual_to_editor_payload(parse_coding_manual(VALID_MANUAL)))}}),
    'reset manual': ('/manual/reset', {}),
    'import manual': ('/manual/import', {'files': {'file': ('manual.yml', VALID_MANUAL.encode())}}),
    'upload sheet': ('/coding-sheet', {'files': {'file': ('sheet.csv', NEW_SHEET.encode())}}),
    'save converted sheet': ('/coding-sheet/draft/save', {'data': {'csv_text': NEW_SHEET}}),
    'match PDF': ('/coding-sheet/match-pdfs', {'data': {'paper_key': 'paper.pdf', 'filename': 'other.pdf', 'save_key': 'paper.pdf'}}),
    'unmatch PDF': ('/coding-sheet/unmatch-pdf', {'data': {'source_pdf': 'paper.pdf'}}),
    'delete PDF': ('/uploads/delete', {'data': {'filename': 'paper.pdf'}}),
    'clear output': ('/clear-output', {}),
    'delete project': ('/delete', {}),
}


@pytest.mark.parametrize('status', ['running', 'cancelling'])
@pytest.mark.parametrize('edit', GUARDED_EDITS)
def test_input_edits_are_rejected_while_a_run_is_active(site, status, edit):
    from meta_coder.runner import RunState
    client, project, runtime = site
    ready_project(project, runtime)
    (project.sources_dir / 'other.pdf').write_bytes(b'%PDF')
    (project.output_dir / 'coded_data.csv').write_text('results')
    runtime.runner._states[project.project_id] = RunState(status=status)
    before = project_files(project)
    suffix, request = GUARDED_EDITS[edit]
    assert client.post(url(project, suffix), **request).status_code == 409
    assert project_files(project) == before


def test_project_deletion_waits_for_an_active_pdf_scan(site, monkeypatch):
    client, project, runtime = site
    monkeypatch.setattr(runtime.pdf_scanner, 'is_running', lambda _: True)
    response = client.post(url(project, '/delete'))
    assert response.status_code == 409 and 'PDF scan' in response.json()['detail']
    assert project.path.exists()


def test_second_run_click_during_key_loading_gets_409(site, monkeypatch):
    from meta_coder.runner import RunState
    client, project, runtime = site
    ready_project(project, runtime)
    started = []

    def start(**kwargs):
        started.append(kwargs)
        runtime.runner._states[project.project_id] = RunState(status='running')

    second_click = []

    def prepare(*args):
        # Runs in a worker thread, so the event loop handles the whole second
        # click while the first one is paused here waiting for its key.
        if not second_click:
            second_click.append(None)
            second_click[0] = client.post(url(project, '/run'))
        return 'fake-test-key', None

    monkeypatch.setattr(runtime.runner, 'start', start)
    monkeypatch.setattr(runtime, 'prepare', prepare)
    first_click = client.post(url(project, '/run'))
    assert second_click[0].status_code == 303
    assert first_click.status_code == 409
    assert first_click.json()['detail'] == 'A run is already in progress.'
    assert len(started) == 1


def test_input_edit_during_key_loading_is_used_by_the_run(site, monkeypatch):
    client, project, runtime = site
    ready_project(project, runtime)
    (project.sources_dir / 'other.pdf').write_bytes(b'%PDF')
    start = Mock()
    edits = []

    def prepare(*args):
        edits.append(client.post(url(project, '/coding-sheet'), files={'file': ('sheet.csv', NEW_SHEET.encode())}))
        edits.append(client.post(url(project, '/manual/import'), files={'file': ('manual.yml', VALID_MANUAL.encode())}))
        return 'fake-test-key', None

    monkeypatch.setattr(runtime.runner, 'start', start)
    monkeypatch.setattr(runtime, 'prepare', prepare)
    assert client.post(url(project, '/run')).status_code == 303
    assert [edit.status_code for edit in edits] == [303, 303]
    assert [row.row_id for row in start.call_args.kwargs['coding_sheet'].rows] == ['r2']
    assert start.call_args.kwargs['manual'].effect_definition == 'new comparison'


def test_edit_during_key_loading_that_blocks_the_run_rejects_it(site, monkeypatch):
    client, project, runtime = site
    ready_project(project, runtime)
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    monkeypatch.setattr(runtime, 'prepare', lambda *a: (client.post(url(project, '/manual/reset')), ('k', None))[1])
    response = client.post(url(project, '/run'))
    assert response.status_code == 400 and 'Cannot run' in response.json()['detail']
    start.assert_not_called()


def test_extractor_edit_during_key_loading_rejects_the_start(site, monkeypatch):
    client, project, runtime = site
    extractor = choose_extractor(project)
    ready_project(project, runtime)
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)

    def edit(*args):
        extractors.update_extractor(extractor.id, {'name': extractor.name, 'model': 'test-model', 'parallel_requests': 4})
        return 'k', None

    monkeypatch.setattr(runtime, 'prepare', edit)
    response = client.post(url(project, '/run'))
    assert response.status_code == 409 and 'extractor changed' in response.json()['detail']
    start.assert_not_called()


def test_runner_refusal_is_a_409_not_a_server_error(site, monkeypatch):
    client, project, runtime = site
    ready_project(project, runtime)
    # The real runner refuses a retry that targets no PDF in the sheet.
    response = client.post(url(project, '/run/retry'), data={'source_pdf': 'absent.pdf'})
    assert response.status_code == 409
    assert response.json()['detail'] == 'No matched PDFs still need coding.'
    monkeypatch.setattr(runtime.runner, 'start', Mock(side_effect=RuntimeError('This project is already running.')))
    response = client.post(url(project, '/run'))
    assert response.status_code == 409 and response.json()['detail'] == 'This project is already running.'


def test_retry_all_uses_durable_failures_after_restart_and_subset_retry(site, monkeypatch):
    from meta_coder.runner import PdfProgress, RunState, write_raw_result
    from meta_coder.extraction import ExtractionResult
    client, project, runtime = site
    ready_project(project, runtime)
    for name in ['older.pdf', 'removed.pdf']:
        (project.sources_dir / name).write_bytes(b'%PDF')
    project.coding_sheet_path.write_text(project.coding_sheet_path.read_text() + 'r2,older.pdf,,Smith,2020\n')
    for name in ['paper.pdf', 'older.pdf', 'removed.pdf']:
        write_raw_result(project, ExtractionResult(name, 'error'), provider='gemini', model='model')
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    assert web._project_view(runtime, project)['can_retry'] is True
    assert client.post(url(project, '/run/retry')).status_code == 303
    assert set(start.call_args.kwargs['only_pdfs']) == {'paper.pdf', 'older.pdf'}
    runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('paper.pdf', status='ok')])
    assert client.post(url(project, '/run/retry')).status_code == 303
    assert start.call_args.kwargs['only_pdfs'] == ['older.pdf']


@pytest.mark.parametrize(('raw', 'limit', 'status'), [(b'\xff', 1024, 400), (b'x' * 25, 24, 413)])
def test_manual_import_rejects_encoding_and_limit_without_data_loss(site, monkeypatch, raw, limit, status):
    client, project, _ = site
    monkeypatch.setattr(web, 'load_app_settings', lambda: Mock(upload_size_cap_bytes=limit))
    before = project.manual_path.read_bytes()
    result = project.raw_dir / 'result.json'
    result.write_text('valuable')
    response = client.post(url(project, '/manual/import'), files={'file': ('manual.yml', raw)})
    assert response.status_code == status
    assert project.manual_path.read_bytes() == before
    assert result.read_text() == 'valuable'


def test_manual_import_accepts_utf8_bom(site):
    client, project, _ = site
    raw = '\ufeffeffect_definition: comparison\neffects:\n  label: {description: Müller}\n'.encode()
    response = client.post(url(project, '/manual/import'), files={'file': ('manual.yml', raw)})
    assert response.status_code == 303
    assert 'Müller' in project.manual_path.read_text(encoding='utf-8')


def test_model_check_runs_outside_request_event_loop(site, monkeypatch):
    import threading
    client, project, runtime = site
    @client.app.get('/thread-id')
    async def thread_id():
        return threading.get_ident()
    event_thread = client.get('/thread-id').json()
    threads = []
    monkeypatch.setattr(web, 'check_model', lambda *a, **k: threads.append(threading.get_ident()) or [])
    response = client.post('/test/extractors', data={'name': 'Threaded', 'provider': 'gemini', 'model': 'm', 'api_key': 'k'})
    assert response.status_code == 303
    assert threads and threads[0] != event_thread


def test_failed_zip_creation_cleans_up_temp_file(site, monkeypatch, tmp_path):
    client, project, _ = site
    temporary = tmp_path / 'failed.zip'
    real_temporary = web.tempfile.NamedTemporaryFile
    monkeypatch.setattr(web.tempfile, 'NamedTemporaryFile', lambda **kwargs: real_temporary(dir=tmp_path, prefix='export-', **kwargs))
    monkeypatch.setattr(web, 'project_archive_files', lambda _: [(temporary, 'missing')])
    with pytest.raises(FileNotFoundError):
        client.get(url(project, '/download/zip'))
    assert not list(tmp_path.glob('export-*.zip'))


@pytest.mark.parametrize(('method', 'suffix', 'data'), [
    ('get', '/status', None), ('get', '/pdf-scan/status', None),
    ('get', '/download/zip', None), ('get', '/download/coded', None),
    ('get', '/download/evidence', None), ('get', '/raw/missing.json', None),
    ('post', '/clear-output', None), ('post', '/manual/reset', None),
    ('post', '/run/cancel', None), ('post', '/open-folder', None),
])
def test_missing_projects_return_404_across_routes(site, method, suffix, data):
    client, project, _ = site
    from meta_coder.projects import delete_project
    delete_project(project)
    response = client.request(method, url(project, suffix), data=data)
    assert response.status_code == 404


def test_only_high_confidence_suggestions_are_preselected(site, monkeypatch):
    client, project, _ = site
    for name in ('high.pdf', 'medium.pdf'):
        (project.sources_dir / name).write_bytes(b'%PDF')
    project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nr1,a.pdf,exp,Li,2020\nr2,b.pdf,exp,Wu,2021\n')
    papers = pdf_matching.unmatched_paper_ids(project.coding_sheet_path.read_text(), uploaded_filenames={'high.pdf', 'medium.pdf'})
    candidates = ['high.pdf', 'medium.pdf']
    suggestions = [
        pdf_matching.MatchSuggestion(papers[0], 'high.pdf', 'high', candidates),
        pdf_matching.MatchSuggestion(papers[1], 'medium.pdf', 'medium', candidates),
    ]
    monkeypatch.setattr(web, 'cached_signal', lambda *a, **k: pdf_matching.PdfSignal('', None))
    monkeypatch.setattr(web, 'suggest_matches_from_score_cache', lambda *a, **k: (suggestions, False))
    body = client.get(url(project, '?tab=identify')).text
    assert '<option value="high.pdf" selected>high.pdf</option>' in body
    assert '<option value="medium.pdf" >medium.pdf (suggested)</option>' in body
    assert 'value="medium.pdf" selected' not in body
