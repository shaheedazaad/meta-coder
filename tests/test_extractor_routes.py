"""Settings routes that add, edit, copy and delete extractors, and their keys."""
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import keyring
import pytest
from fastapi.testclient import TestClient

from meta_coder import extractors, web
from meta_coder.app_settings import AppSettings, load_app_settings, save_app_settings
from meta_coder.credentials import VAULT_USERNAME, _SERVICE_NAME
from meta_coder.projects import create_project
from meta_coder.settings import save_project_extractor_id

KEY = 'sk-test-SENTINEL-0123456789'


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'check_model', Mock(return_value=[]))
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, app.state.runtime, tmp_path / 'projects'


def query(response):
    location = urlsplit(response.headers['location'])
    return {name: values[0] for name, values in parse_qs(location.query).items()}, location.fragment


def add(client, **values):
    return client.post('/test/extractors', data={'name': 'Lab', 'provider': 'gemini', 'model': 'gemini-test', **values})


def only():
    [extractor] = extractors.load_extractors()
    return extractor


def test_settings_page_lists_extractors_without_opening_the_store(site, memory_keyring):
    client, _, _ = site
    page = client.get('/test/settings').text
    assert 'No extractors yet' in page and 'id="add-extractor" open' in page
    extractors.add_extractor(extractors.Extractor(id='lab', name='Lab', provider='gemini', model='m', key_saved=True))
    extractors.add_extractor(extractors.Extractor(id='local', name='Local', provider='openai_compatible', model='m',
                                                  base_url='http://localhost/v1'))
    extractors.add_extractor(extractors.Extractor(id='bare', name='Bare', provider='openrouter', model=''))
    page = client.get('/test/settings').text
    assert 'id="extractor-lab"' in page and 'Key saved' in page and 'Ready' in page
    assert 'Setup needed' in page and 'Choose a model for the “Bare” extractor' in page
    assert 'Saved — leave blank to keep it' in page
    assert memory_keyring.reads == 0


def test_create_saves_the_key_in_the_vault_never_in_files_or_pages(site, memory_keyring, isolated_home):
    client, runtime, _ = site
    response = add(client, api_key=f' {KEY} ', parallel_requests='3')
    params, anchor = query(response)
    extractor = only()
    assert params == {'notice': 'Saved the “Lab” extractor.'} and anchor == f'extractor-{extractor.id}'
    assert extractor.key_saved and extractor.parallel_requests == 3
    web.check_model.assert_called_once_with('gemini', 'gemini-test', api_key=KEY, base_url='')
    vault = json.loads(memory_keyring.store[(_SERVICE_NAME, VAULT_USERNAME)])
    assert vault['keys'] == {extractor.id: KEY}
    for path in isolated_home.rglob('*'):
        assert not path.is_file() or KEY not in path.read_text(errors='ignore')
    assert KEY not in client.get('/test/settings').text
    assert KEY not in client.get(f'/test/api/extractors/{extractor.id}/projects').text


def test_create_without_a_key_and_with_model_warnings(site):
    client, _, _ = site
    params, _ = query(add(client))
    assert 'The model will be checked when it runs.' in params['notice']
    web.check_model.return_value = [f'Model `x` failed with {KEY}']
    params, _ = query(add(client, name='Router', provider='openrouter', model='a/b', api_key=KEY))
    assert 'Model `x` failed with [redacted]' in params['notice'] and KEY not in params['notice']


def test_create_errors_are_reported_and_save_nothing(site, monkeypatch):
    client, _, _ = site
    params, anchor = query(add(client, model=''))
    assert params['error'] == 'Enter a model ID.' and anchor == 'add-extractor'
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    params, _ = query(add(client, api_key=KEY))
    assert 'No operating-system credential store' in params['error']
    assert extractors.load_extractors() == []


def test_a_key_the_store_refuses_leaves_the_extractor_without_it(site, monkeypatch):
    client, _, _ = site
    monkeypatch.setattr(keyring, 'set_password', Mock(side_effect=RuntimeError('locked')))
    params, _ = query(add(client, api_key=KEY))
    assert 'saved without its key' in params['notice']
    assert not only().key_saved


def test_edit_checks_a_changed_model_with_the_saved_key(site, memory_keyring):
    client, runtime, _ = site
    add(client, api_key=KEY)
    extractor = only()
    restarted = web.create_app(token='test', projects_root=runtime.projects_root)
    with TestClient(restarted, base_url='http://localhost', follow_redirects=False) as fresh:
        response = fresh.post(f'/test/extractors/{extractor.id}', data={'name': 'Lab', 'model': 'gemini-new', 'api_key': ''})
    assert query(response)[0] == {'notice': 'Saved the “Lab” extractor.'}
    web.check_model.assert_called_with('gemini', 'gemini-new', api_key=KEY, base_url='')
    assert only().model == 'gemini-new' and only().key_saved
    # Renaming alone asks no provider.
    calls = web.check_model.call_count
    client.post(f'/test/extractors/{extractor.id}', data={'name': 'Renamed', 'model': 'gemini-new'})
    assert web.check_model.call_count == calls and only().name == 'Renamed'


def test_edit_replaces_the_key_and_reports_errors(site, memory_keyring, monkeypatch):
    client, runtime, _ = site
    add(client)
    extractor = only()
    client.post(f'/test/extractors/{extractor.id}', data={'name': 'Lab', 'model': 'gemini-test', 'api_key': 'second'})
    assert runtime.vault.cached(extractor.id) == 'second' and only().key_saved
    params, anchor = query(client.post(f'/test/extractors/{extractor.id}', data={'name': '', 'model': 'm'}))
    assert params['error'] == 'Give the extractor a name.' and anchor == f'extractor-{extractor.id}'
    monkeypatch.setattr(keyring, 'set_password', Mock(side_effect=RuntimeError(f'locked {KEY}')))
    params, _ = query(client.post(f'/test/extractors/{extractor.id}', data={'name': 'Lab', 'model': 'm', 'api_key': KEY}))
    assert 'could not save' in params['error']
    assert client.post('/test/extractors/missing', data={'name': 'x', 'model': 'y'}).status_code == 404


def test_an_endpoint_without_a_key_is_checked_without_one(site):
    client, _, _ = site
    add(client, name='Local', provider='openai_compatible', model='m', base_url='http://localhost/v1')
    extractor = only()
    client.post(f'/test/extractors/{extractor.id}', data={'name': 'Local', 'model': 'm', 'base_url': 'http://other/v1'})
    web.check_model.assert_called_with('openai_compatible', 'm', api_key='', base_url='http://other/v1')


def test_duplicate_copies_settings_but_not_the_key(site):
    client, runtime, _ = site
    add(client, api_key=KEY)
    original = only()
    params, _ = query(client.post(f'/test/extractors/{original.id}/duplicate'))
    assert 'Keys are not copied' in params['notice']
    copy = next(item for item in extractors.load_extractors() if item.id != original.id)
    assert copy.name == 'Lab copy' and not copy.key_saved and runtime.vault.cached(copy.id) is None
    assert client.post('/test/extractors/missing/duplicate').status_code == 404


def test_delete_names_affected_projects_first_and_removes_the_key(site, memory_keyring):
    client, runtime, root = site
    add(client, api_key=KEY)
    extractor = only()
    study = create_project('Study A', root=root)
    create_project('Unrelated', root=root)
    save_project_extractor_id(study, extractor.id)
    save_app_settings(AppSettings(drafting_extractor_id=extractor.id))
    uses = client.get(f'/test/api/extractors/{extractor.id}/projects').json()
    assert uses == {'projects': ['Study A'], 'drafting': True}
    params, _ = query(client.post(f'/test/extractors/{extractor.id}/delete'))
    assert params['notice'] == ('Deleted the “Lab” extractor. Choose another extractor for: Study A. '
                                'Choose another extractor for drafting.')
    assert extractors.load_extractors() == []
    assert json.loads(memory_keyring.store[(_SERVICE_NAME, VAULT_USERNAME)])['keys'] == {}
    assert client.get(f'/test/api/extractors/{extractor.id}/projects').status_code == 404
    assert client.post(f'/test/extractors/{extractor.id}/delete').status_code == 404
    add(client, name='Keyless', provider='openai_compatible', model='m', base_url='http://localhost/v1')
    assert query(client.post(f'/test/extractors/{only().id}/delete'))[0] == {'notice': 'Deleted the “Keyless” extractor.'}


def test_a_key_that_cannot_be_removed_keeps_the_extractor(site, monkeypatch):
    client, _, _ = site
    add(client, api_key=KEY)
    extractor = only()
    monkeypatch.setattr(keyring, 'set_password', Mock(side_effect=RuntimeError('locked')))
    params, _ = query(client.post(f'/test/extractors/{extractor.id}/delete'))
    assert 'was not deleted' in params['error']
    params, _ = query(client.post(f'/test/extractors/{extractor.id}/key/delete'))
    assert 'could not save' in params['error']
    assert only().key_saved


def test_delete_racing_another_delete_is_a_404(site, monkeypatch):
    client, _, _ = site
    add(client, name='Local', provider='openai_compatible', model='m', base_url='http://localhost/v1')
    monkeypatch.setattr(web, 'delete_extractor', Mock(side_effect=extractors.ExtractorError('gone')))
    assert client.post(f'/test/extractors/{only().id}/delete').status_code == 404


def test_remove_key_also_removes_an_unmoved_old_item(site, memory_keyring):
    client, runtime, _ = site
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'old'
    first = extractors.add_extractor(extractors.Extractor(id='a', name='A', provider='gemini', model='m',
                                                          key_saved=True, legacy_credential='gemini'))
    extractors.add_extractor(extractors.Extractor(id='b', name='B', provider='gemini', model='m',
                                                  key_saved=True, legacy_credential='gemini'))
    runtime.vault._keys = {}  # loaded already, before the old item moved
    params, _ = query(client.post(f'/test/extractors/{first.id}/key/delete'))
    assert params['notice'] == 'Removed the key for “A”.'
    assert (_SERVICE_NAME, 'gemini') in memory_keyring.store  # B still waits for it
    assert not extractors.get_extractor('a').has_saved_key
    client.post('/test/extractors/b/key/delete')
    assert (_SERVICE_NAME, 'gemini') not in memory_keyring.store
    assert client.post('/test/extractors/missing/key/delete').status_code == 404


def test_model_suggestions_use_the_typed_or_saved_key_and_never_echo_it(site, monkeypatch, memory_keyring):
    client, runtime, _ = site
    listed = Mock(return_value=['m1', 'm2'])
    monkeypatch.setattr(web, 'list_models', listed)
    response = client.post('/test/api/models', data={'provider': 'gemini', 'api_key': f' {KEY} '})
    assert response.json() == {'models': ['m1', 'm2']}
    listed.assert_called_with('gemini', api_key=KEY, base_url='')
    assert memory_keyring.reads == memory_keyring.writes == 0  # a typed key is never stored
    add(client, api_key='saved-key')
    extractor = only()
    runtime.vault._keys = None
    client.post('/test/api/models', data={'provider': 'gemini', 'extractor_id': extractor.id})
    listed.assert_called_with('gemini', api_key='saved-key', base_url='')
    client.post('/test/api/models', data={'provider': 'openrouter'})
    listed.assert_called_with('openrouter', api_key='', base_url='')
    listed.side_effect = RuntimeError(f'bad key {KEY}')
    response = client.post('/test/api/models', data={'provider': 'gemini', 'api_key': KEY})
    assert response.status_code == 502 and KEY not in response.text and '[redacted]' in response.text
    listed.side_effect = RuntimeError('')
    assert client.post('/test/api/models', data={'provider': 'gemini'}).json()['detail'] == 'Models could not be loaded.'
    assert client.post('/test/api/models', data={'provider': 'bogus'}).status_code == 400
    assert client.post('/test/api/models', data={'provider': 'gemini', 'extractor_id': 'missing'}).status_code == 404
    runtime.vault._keys = None
    monkeypatch.setattr(keyring, 'get_password', Mock(side_effect=RuntimeError('denied')))
    response = client.post('/test/api/models', data={'provider': 'gemini', 'extractor_id': extractor.id})
    assert response.status_code == 409 and 'Allow access' in response.json()['detail']


def test_drafting_choice_must_be_an_existing_extractor(site):
    client, _, _ = site
    add(client)
    params, anchor = query(client.post('/test/settings/app', data={'upload_size_cap_mb': 64, 'drafting_extractor_id': 'missing'}))
    assert 'no longer exists' in params['error'] and anchor == 'drafting'
    params, _ = query(client.post('/test/settings/app', data={'upload_size_cap_mb': 64, 'drafting_extractor_id': only().id}))
    assert params == {'notice': 'Saved drafting preferences.'}
    assert load_app_settings() == AppSettings(upload_size_cap_mb=64, drafting_extractor_id=only().id)
    assert f'<option value="{only().id}" selected>' in client.get('/test/settings').text


def test_an_unreadable_extractors_file_stops_every_page(site):
    client, _, _ = site
    extractors.extractors_path().write_text('{broken')
    for path in ['/test/', '/test/settings']:
        response = client.get(path)
        assert response.status_code == 503 and 'Fix or move the file' in response.text
    assert extractors.extractors_path().read_text() == '{broken'


def test_nav_badge_reflects_a_usable_extractor(site):
    client, runtime, _ = site
    assert runtime.has_any_api_key() is False
    add(client, name='Local', provider='openai_compatible', model='m', base_url='http://localhost/v1')
    assert runtime.has_any_api_key() is True


def test_a_saved_key_missing_for_a_deleted_extractor_changes_nothing(site):
    _, runtime, _ = site
    runtime.vault._keys = {}
    gone = extractors.Extractor(id='gone', name='Gone', provider='gemini', model='m', key_saved=True)
    key, error = runtime.prepare(gone)
    assert key == '' and 'no longer in the credential store' in error
    assert extractors.load_extractors() == []


def test_an_unreadable_saved_key_does_not_block_an_edit(site, monkeypatch):
    client, runtime, _ = site
    add(client, api_key=KEY)
    extractor = only()
    runtime.vault._keys = None
    monkeypatch.setattr(keyring, 'get_password', Mock(side_effect=RuntimeError('denied')))
    params, _ = query(client.post(f'/test/extractors/{extractor.id}', data={'name': 'Lab', 'model': 'other'}))
    assert 'The model will be checked when it runs.' in params['notice']
    assert only().model == 'other' and only().key_saved
