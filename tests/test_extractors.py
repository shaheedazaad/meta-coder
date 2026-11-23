"""Extractor definitions, their file, and migration from per-provider settings."""

import json
import os
import sys

import pytest

from meta_coder import credentials, extractors
from meta_coder.app_settings import load_app_settings, read_raw_app_settings
from meta_coder.credentials import KeyVault, _SERVICE_NAME
from meta_coder.extractors import Extractor, ExtractorError, ExtractorsUnreadable
from meta_coder.projects import create_project
from meta_coder.settings import (
    RunSettings, load_project_extractor_id, load_run_settings, save_project_extractor_id, save_run_settings,
)


def new(**values):
    return extractors.add_extractor(extractors.validate_new_extractor(
        {'name': 'Lab', 'provider': 'gemini', 'model': 'gemini-test', **values}))


def test_build_keeps_only_the_providers_fields_and_validates():
    gemini = new(service_tier='STANDARD', reasoning_effort='high', base_url='http://ignored/v1', parallel_requests='4')
    assert gemini.service_tier == 'standard' and gemini.reasoning_effort == '' and gemini.base_url == ''
    assert gemini.parallel_requests == 4 and gemini.request_timeout_sec == 600
    assert gemini.snapshot() == {
        'name': 'Lab', 'provider': 'gemini', 'model': 'gemini-test', 'parallel_requests': 4,
        'request_delay_sec': 0, 'request_timeout_sec': 600, 'service_tier': 'standard'}
    endpoint = extractors.validate_new_extractor({
        'name': '  Local   server ', 'provider': 'openai_compatible', 'model': 'm',
        'base_url': 'http://localhost:8000/v1/', 'response_format': 'none', 'reasoning_effort': 'LOW'})
    assert endpoint.name == 'Local server' and endpoint.base_url == 'http://localhost:8000/v1'
    assert endpoint.reasoning_effort == 'low' and not endpoint.requires_key
    assert ('API base URL', 'http://localhost:8000/v1') in endpoint.summary()
    assert ('JSON output mode', 'Prompt only') in endpoint.summary()
    router = extractors.validate_new_extractor({'name': 'Router', 'provider': 'openrouter', 'model': 'a/b'})
    assert ('Reasoning effort', 'Model default') in router.summary() and router.requires_key
    assert ('Service tier', 'standard') in gemini.summary()


@pytest.mark.parametrize(('values', 'message'), [
    ({'provider': 'bogus'}, 'Choose a provider'),
    ({'name': ' '}, 'Give the extractor a name'),
    ({'name': 'x' * 61}, 'at most 60'),
    ({'model': ''}, 'Enter a model ID'),
    ({'parallel_requests': 'many'}, 'whole number'),
    ({'parallel_requests': '0'}, 'between 1 and 32'),
    ({'request_timeout_sec': '5'}, 'between 30 and 3600'),
    ({'service_tier': 'gold'}, 'service tier'),
    ({'provider': 'openrouter', 'reasoning_effort': 'huge'}, 'reasoning effort'),
    ({'provider': 'openai_compatible'}, 'API base URL without'),
    ({'provider': 'openai_compatible', 'base_url': 'http://x/v1', 'response_format': 'xml'}, 'JSON output mode'),
])
def test_invalid_values_are_explained(values, message):
    with pytest.raises(ExtractorError, match=message):
        extractors.validate_new_extractor({'name': 'Lab', 'provider': 'gemini', 'model': 'm', **values})


def test_names_are_unique_ignoring_case():
    new()
    with pytest.raises(ExtractorError, match='already exists'):
        new(name='LAB')
    with pytest.raises(ExtractorError, match='already exists'):
        extractors.add_extractor(Extractor(id='other', name='lab', provider='gemini', model='m'))


def test_file_is_private_never_holds_keys_and_keeps_unloadable_records():
    lab = new()
    extractors.set_key_saved(lab.id, True)
    path = extractors.extractors_path()
    if sys.platform != 'win32':
        assert os.stat(path).st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    data['extractors'].append({'id': 'broken', 'provider': 'unknown', 'name': 'From the future'})
    data['extractors'].append({'name': 'no id'})
    data['extractors'].append('junk')
    path.write_text(json.dumps(data))
    assert [item.id for item in extractors.load_extractors()] == [lab.id]
    extractors.update_extractor(lab.id, {'name': 'Renamed', 'model': 'm2'})
    records = json.loads(path.read_text())['extractors']
    assert records[0]['name'] == 'Renamed' and records[0]['key_saved'] is True
    assert {'id': 'broken', 'provider': 'unknown', 'name': 'From the future'} in records


@pytest.mark.parametrize('content', ['{broken', '[]', '{"extractors": {}}'])
def test_an_unreadable_file_is_never_overwritten(content):
    path = extractors.extractors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(ExtractorsUnreadable, match='Fix or move the file'):
        extractors.load_extractors()
    with pytest.raises(ExtractorsUnreadable):
        new()
    assert path.read_text() == content


def test_update_duplicate_delete_and_key_status():
    lab = new()
    assert extractors.get_extractor('') is None and extractors.get_extractor('missing') is None
    updated = extractors.update_extractor(lab.id, {'name': 'Lab', 'provider': 'openrouter', 'model': 'new'})
    assert updated.provider == 'gemini' and updated.model == 'new'  # the provider never changes
    with pytest.raises(ExtractorError, match='no longer exists'):
        extractors.update_extractor('missing', {'name': 'x', 'model': 'y'})
    extractors.set_key_saved(lab.id, True)
    copy = extractors.duplicate_extractor(lab.id)
    assert copy.name == 'Lab copy' and copy.model == 'new' and not copy.key_saved and copy.id != lab.id
    assert extractors.duplicate_extractor(lab.id).name == 'Lab copy 2'
    long = new(name='x' * 60)
    assert extractors.duplicate_extractor(long.id).name == 'x' * 55 + ' copy'
    with pytest.raises(ExtractorError, match='already exists'):
        extractors.update_extractor(copy.id, {'name': 'lab', 'model': 'm'})
    assert extractors.delete_extractor(copy.id).id == copy.id
    assert extractors.get_extractor(copy.id) is None


def test_legacy_claims_and_adoption_are_recorded():
    first = extractors.add_extractor(Extractor(id='a', name='A', provider='gemini', model='m', key_saved=True, legacy_credential='gemini'))
    second = extractors.add_extractor(Extractor(id='b', name='B', provider='gemini', model='m', key_saved=True, legacy_credential='gemini'))
    assert first.has_saved_key and extractors.legacy_claims() == {'a': 'gemini', 'b': 'gemini'}
    assert extractors.other_claims(first)
    extractors.record_legacy_adoption({'a': True})
    assert extractors.get_extractor('a').legacy_credential == '' and extractors.get_extractor('a').key_saved
    assert not extractors.other_claims(extractors.get_extractor('b'))
    extractors.set_key_saved('b', False)
    assert extractors.get_extractor('b') == Extractor(id='b', name='B', provider='gemini', model='m')
    assert second.has_saved_key and not extractors.get_extractor('b').has_saved_key


def test_run_settings_snapshot_the_extractor():
    lab = new(parallel_requests=3)
    settings = extractors.run_settings_for(lab)
    assert settings.extractor_id == lab.id and settings.extractor_name == 'Lab'
    assert settings.extractor == lab.snapshot() and settings.parallel_requests == 3
    assert extractors.setup_problem(lab) is None
    assert 'Choose a model' in extractors.setup_problem(Extractor(id='x', name='X', provider='gemini', model=''))
    assert 'API base URL' in extractors.setup_problem(Extractor(id='x', name='X', provider='openai_compatible', model='m'))


def test_project_extractor_uses_the_choice_or_the_last_run(tmp_path):
    lab = new()
    other = new(name='Other')
    project = create_project('Study', root=tmp_path)
    assert extractors.project_extractor(project) == extractors.ProjectExtractor(None)
    save_run_settings(project, extractors.run_settings_for(other))
    assert extractors.project_extractor(project).extractor == other
    save_project_extractor_id(project, lab.id)
    assert extractors.project_extractor(project).extractor == lab
    assert extractors.projects_using(lab.id, root=tmp_path) == ['Study']
    assert extractors.projects_using(other.id, root=tmp_path) == []
    extractors.delete_extractor(lab.id)
    assert extractors.project_extractor(project) == extractors.ProjectExtractor(None, deleted=True)


# --- migration ------------------------------------------------------------------

def write_legacy(home, *, app=None, flags=None):
    if app is not None:
        (home / 'app_settings.json').write_text(app if isinstance(app, str) else json.dumps(app), encoding='utf-8')
    if flags is not None:
        (home / 'saved_api_keys.json').write_text(flags if isinstance(flags, str) else json.dumps(flags), encoding='utf-8')


def legacy_project(root, name, **settings):
    project = create_project(name, root=root)
    save_run_settings(project, RunSettings(**settings))
    return project


def test_a_fresh_install_migrates_to_no_extractors(tmp_path):
    assert extractors.migrate_legacy_settings(projects_root=tmp_path) is True
    assert extractors.load_extractors() == []
    assert extractors.migrate_legacy_settings(projects_root=tmp_path) is False


def test_default_only_settings_are_skipped(isolated_home, tmp_path):
    write_legacy(isolated_home, app={'manual_generator_provider': 'gemini', 'manual_generator_model': 'gemini-3.8-flash'})
    legacy_project(tmp_path, 'Untouched', provider='openrouter', model='a/b')
    extractors.migrate_legacy_settings(projects_root=tmp_path)
    assert extractors.load_extractors() == []
    assert load_app_settings().drafting_extractor_id == ''


def test_saved_keys_and_settings_become_extractors(isolated_home, tmp_path, memory_keyring):
    write_legacy(isolated_home, app={
        'upload_size_cap_mb': 64, 'manual_generator_provider': 'openai_compatible', 'manual_generator_model': 'qwen',
        'openai_base_url': 'http://localhost:8000/v1/', 'openai_response_format': 'none',
    }, flags={'gemini': True, 'openrouter': True})
    default_gemini = legacy_project(tmp_path, 'A default Gemini', provider='gemini', model='gemini-3.8-flash')
    fast = legacy_project(tmp_path, 'Fast Gemini', provider='gemini', model='gemini-3.8-flash', parallel_requests=8)
    fast_too = legacy_project(tmp_path, 'Fast too', provider='gemini', model='gemini-3.8-flash', parallel_requests=8)
    pro = legacy_project(tmp_path, 'Pro Gemini', provider='gemini', model='gemini-pro', service_tier='standard')
    router = legacy_project(tmp_path, 'Router', provider='openrouter', model='a/b', reasoning_effort='high')
    chosen = legacy_project(tmp_path, 'Already chosen', provider='gemini', model='x')
    save_project_extractor_id(chosen, 'kept')
    create_project('Never ran', root=tmp_path)
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'old-gemini-key'
    memory_keyring.store[(_SERVICE_NAME, 'openrouter')] = 'old-router-key'

    assert extractors.migrate_legacy_settings(projects_root=tmp_path) is True
    assert memory_keyring.reads == 0  # migration never opens the credential store
    by_name = {item.name: item for item in extractors.load_extractors()}
    assert set(by_name) == {'Gemini', 'OpenRouter', 'OpenAI-compatible', 'Gemini (Fast Gemini)',
                            'Gemini (Pro Gemini)'}
    gemini, endpoint = by_name['Gemini'], by_name['OpenAI-compatible']
    # The first project with a provider takes that provider's extractor; the
    # rest reuse an identical one or get their own, sharing the old key.
    assert load_project_extractor_id(default_gemini) == gemini.id and gemini.model == 'gemini-3.8-flash'
    assert load_project_extractor_id(fast) == load_project_extractor_id(fast_too) == by_name['Gemini (Fast Gemini)'].id
    assert by_name['Gemini (Fast Gemini)'].parallel_requests == 8
    assert load_project_extractor_id(pro) == by_name['Gemini (Pro Gemini)'].id
    assert by_name['Gemini (Pro Gemini)'].service_tier == 'standard'
    assert load_project_extractor_id(router) == by_name['OpenRouter'].id
    assert by_name['OpenRouter'].reasoning_effort == 'high'
    assert load_project_extractor_id(chosen) == 'kept'
    assert all(item.legacy_credential == item.provider and item.key_saved
               for item in by_name.values() if item.provider != 'openai_compatible')
    assert endpoint.base_url == 'http://localhost:8000/v1' and endpoint.response_format == 'none'
    assert endpoint.model == 'qwen' and not endpoint.has_saved_key
    # Old settings are replaced: drafting uses the endpoint extractor.
    assert read_raw_app_settings() == {'upload_size_cap_mb': 64, 'grobid_url': '', 'drafting_extractor_id': endpoint.id}
    assert not credentials.legacy_saved_keys_path().exists()

    # The first action that needs a key moves the old keys into the vault.
    vault = KeyVault(legacy_claims=extractors.legacy_claims, on_legacy_adopted=extractors.record_legacy_adoption)
    assert vault.get(by_name['Gemini (Pro Gemini)'].id) == 'old-gemini-key'
    assert vault.get(by_name['OpenRouter'].id) == 'old-router-key'
    assert (_SERVICE_NAME, 'gemini') not in memory_keyring.store
    assert extractors.legacy_claims() == {}
    assert all(item.key_saved for item in extractors.load_extractors() if item.provider != 'openai_compatible')


def test_a_saved_key_with_unusable_settings_is_kept(isolated_home, tmp_path):
    write_legacy(isolated_home, app={'openai_base_url': 'ftp://nowhere'}, flags={'openai_compatible': True})
    legacy_project(tmp_path, 'Odd', provider='openai_compatible', model='m', reasoning_effort='')
    extractors.migrate_legacy_settings(projects_root=tmp_path)
    [endpoint] = extractors.load_extractors()
    assert endpoint.base_url == '' and endpoint.legacy_credential == 'openai_compatible'
    assert 'API base URL' in extractors.setup_problem(endpoint)


def test_unreadable_old_files_are_left_in_place(isolated_home, tmp_path):
    write_legacy(isolated_home, app='{broken', flags='{broken')
    extractors.migrate_legacy_settings(projects_root=tmp_path)
    assert extractors.load_extractors() == []
    assert (isolated_home / 'app_settings.json').read_text() == '{broken'
    assert credentials.legacy_saved_keys_path().read_text() == '{broken'


def test_a_project_for_an_unconnected_provider_is_left_to_choose(isolated_home, tmp_path):
    write_legacy(isolated_home, flags={'gemini': True})
    project = legacy_project(tmp_path, 'Router', provider='openrouter', model='a/b')
    extractors.migrate_legacy_settings(projects_root=tmp_path)
    assert load_project_extractor_id(project) == ''
    assert load_run_settings(project).model == 'a/b'  # the record of its last settings stays
    assert [item.name for item in extractors.load_extractors()] == ['Gemini']


def test_a_broken_legacy_setting_falls_back_to_the_providers_bare_extractor(isolated_home, tmp_path):
    write_legacy(isolated_home, flags={'gemini': True})
    project = create_project('Odd', root=tmp_path)
    path = project.path / '.meta_coder' / 'run_settings.json'
    path.write_text(json.dumps({'provider': 'gemini', 'model': 'g', 'service_tier': 'priority'}))
    extractors.migrate_legacy_settings(projects_root=tmp_path)
    by_name = {item.name: item for item in extractors.load_extractors()}
    assert set(by_name) == {'Gemini', 'Gemini (Odd)'}  # drafting kept the default Gemini setup
    gemini = by_name['Gemini (Odd)']
    assert load_project_extractor_id(project) == gemini.id
    assert gemini.model == ''  # asks for its settings again


def test_a_filesystem_without_permissions_still_saves(monkeypatch):
    def refuse(*args):
        raise OSError('unsupported')

    monkeypatch.setattr(extractors.os, 'chmod', refuse)
    assert new().name == 'Lab'
    assert [item.name for item in extractors.load_extractors()] == ['Lab']
