"""The key vault, against an in-memory credential store (never the real one)."""

import json

import keyring
import keyring.errors
import pytest

from meta_coder import credentials
from meta_coder.credentials import KeyVault, VAULT_USERNAME, _SERVICE_NAME


def vault_item(memory_keyring):
    raw = memory_keyring.store.get((_SERVICE_NAME, VAULT_USERNAME))
    return json.loads(raw) if raw else None


def test_keys_round_trip_in_one_item_read_once(memory_keyring):
    vault = KeyVault()
    vault.set('a', ' key-a ')
    vault.set('b', 'key-b')
    assert vault_item(memory_keyring) == {'version': 1, 'keys': {'a': 'key-a', 'b': 'key-b'}}
    restarted = KeyVault()
    assert restarted.cached('a') is None and not restarted.loaded
    reads = memory_keyring.reads
    assert restarted.get('a') == 'key-a' and restarted.get('b') == 'key-b'
    assert memory_keyring.reads == reads + 1
    assert restarted.cached('b') == 'key-b'
    assert sorted(restarted.secrets()) == ['key-a', 'key-b']
    restarted.delete('a')
    assert vault_item(memory_keyring)['keys'] == {'b': 'key-b'}
    writes = memory_keyring.writes
    restarted.delete('missing')  # nothing to remove: no write
    assert memory_keyring.writes == writes


def test_empty_key_is_rejected_without_store_access(memory_keyring):
    with pytest.raises(ValueError, match='empty'):
        KeyVault().set('a', '  ')
    assert memory_keyring.reads == memory_keyring.writes == 0


@pytest.mark.parametrize('content', ['not json', '[]', '{"keys": []}'])
def test_an_unreadable_vault_is_reported_and_never_overwritten(memory_keyring, content):
    memory_keyring.store[(_SERVICE_NAME, VAULT_USERNAME)] = content
    vault = KeyVault()
    with pytest.raises(credentials.VaultUnavailable, match='Allow access'):
        vault.get('a')
    with pytest.raises(credentials.VaultUnavailable):
        vault.set('a', 'new key')
    with pytest.raises(credentials.VaultUnavailable):
        vault.delete('a')
    assert memory_keyring.writes == 0
    assert memory_keyring.store[(_SERVICE_NAME, VAULT_USERNAME)] == content


def test_denied_access_is_retried_on_the_next_action(memory_keyring, monkeypatch):
    KeyVault().set('a', 'key')
    vault = KeyVault()
    monkeypatch.setattr(keyring, 'get_password', lambda *a: (_ for _ in ()).throw(keyring.errors.KeyringError('denied')))
    with pytest.raises(credentials.VaultUnavailable):
        vault.get('a')
    monkeypatch.setattr(keyring, 'get_password', memory_keyring.get_password)
    assert vault.get('a') == 'key'


def test_missing_store_refuses_to_save_or_read(monkeypatch):
    monkeypatch.setattr(credentials, 'keyring_available', lambda: False)
    with pytest.raises(credentials.CredentialStoreError, match='No operating-system'):
        KeyVault().set('a', 'key')
    with pytest.raises(credentials.CredentialStoreError):
        KeyVault().get('a')


def test_failed_writes_keep_the_previous_keys(memory_keyring, monkeypatch):
    vault = KeyVault()
    vault.set('a', 'old')
    monkeypatch.setattr(keyring, 'set_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    with pytest.raises(credentials.CredentialStoreError, match='could not save'):
        vault.set('a', 'new')
    with pytest.raises(credentials.CredentialStoreError):
        vault.set('b', 'new')
    with pytest.raises(credentials.CredentialStoreError):
        vault.delete('a')
    assert vault.cached('a') == 'old' and vault.cached('b') is None
    assert vault_item(memory_keyring)['keys'] == {'a': 'old'}


def test_keyring_available_inspects_the_backend_without_access(memory_keyring, monkeypatch):
    assert credentials.keyring_available() is True
    assert memory_keyring.reads == memory_keyring.writes == 0
    monkeypatch.setattr(keyring, 'get_keyring', lambda: type('Unavailable', (), {'priority': 0})())
    assert credentials.keyring_available() is False
    monkeypatch.setattr(keyring, 'get_keyring', lambda: (_ for _ in ()).throw(RuntimeError('setup failed')))
    assert credentials.keyring_available() is False


def test_old_per_provider_keys_move_in_and_are_then_deleted(memory_keyring):
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = ' old-gemini '
    adopted = []
    vault = KeyVault(legacy_claims=lambda: {'x1': 'gemini', 'x2': 'gemini', 'x3': 'openrouter'},
                     on_legacy_adopted=adopted.append)
    assert vault.get('x1') == 'old-gemini' and vault.get('x2') == 'old-gemini'
    assert vault.get('x3') is None  # its old item no longer existed
    assert (_SERVICE_NAME, 'gemini') not in memory_keyring.store
    assert vault_item(memory_keyring)['keys'] == {'x1': 'old-gemini', 'x2': 'old-gemini'}
    assert adopted == [{'x1': True, 'x2': True, 'x3': False}]


def test_a_key_already_in_the_vault_is_not_replaced_by_an_old_item(memory_keyring):
    KeyVault().set('x1', 'newer')
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'older'
    vault = KeyVault(legacy_claims=lambda: {'x1': 'gemini'})
    assert vault.get('x1') == 'newer'
    assert (_SERVICE_NAME, 'gemini') not in memory_keyring.store


def test_no_claims_means_no_adoption_writes(memory_keyring):
    vault = KeyVault(legacy_claims=lambda: {})
    assert vault.get('x') is None
    assert memory_keyring.writes == 0


def test_an_unreadable_old_item_keeps_every_old_item(memory_keyring, monkeypatch):
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'old'
    real_get = memory_keyring.get_password

    def get(service, username):
        if username == 'gemini':
            raise keyring.errors.KeyringError('denied')
        return real_get(service, username)

    monkeypatch.setattr(keyring, 'get_password', get)
    vault = KeyVault(legacy_claims=lambda: {'x1': 'gemini'})
    with pytest.raises(credentials.VaultUnavailable):
        vault.get('x1')
    assert not vault.loaded and memory_keyring.writes == 0
    assert memory_keyring.store[(_SERVICE_NAME, 'gemini')] == 'old'


def test_an_unsaved_vault_keeps_the_old_items(memory_keyring, monkeypatch):
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'old'
    monkeypatch.setattr(keyring, 'set_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    vault = KeyVault(legacy_claims=lambda: {'x1': 'gemini'})
    with pytest.raises(credentials.CredentialStoreError):
        vault.get('x1')
    assert memory_keyring.store[(_SERVICE_NAME, 'gemini')] == 'old'


def test_an_old_item_that_cannot_be_deleted_stays_claimed(memory_keyring, monkeypatch):
    memory_keyring.store[(_SERVICE_NAME, 'gemini')] = 'old'
    monkeypatch.setattr(keyring, 'delete_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    adopted = []
    vault = KeyVault(legacy_claims=lambda: {'x1': 'gemini'}, on_legacy_adopted=adopted.append)
    assert vault.get('x1') == 'old'
    assert adopted == [{}]  # retried next session


def test_delete_removes_an_unmoved_old_item(memory_keyring, monkeypatch):
    vault = KeyVault()
    memory_keyring.store[(_SERVICE_NAME, 'openrouter')] = 'old'
    vault.delete('x1', 'openrouter')
    assert (_SERVICE_NAME, 'openrouter') not in memory_keyring.store
    vault.delete('x1', 'openrouter')  # already gone: still counts as removed
    memory_keyring.store[(_SERVICE_NAME, 'openrouter')] = 'old'
    monkeypatch.setattr(keyring, 'delete_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    with pytest.raises(credentials.CredentialStoreError, match='could not remove'):
        vault.delete('x1', 'openrouter')


def test_a_missing_item_that_cannot_be_checked_is_not_counted_removed(memory_keyring, monkeypatch):
    vault = KeyVault()
    vault.get('x')
    monkeypatch.setattr(keyring, 'get_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    with pytest.raises(credentials.CredentialStoreError):
        vault.delete('x', 'gemini')


def test_legacy_saved_key_flags(isolated_home):
    assert credentials.legacy_saved_key_flags() == {}
    path = credentials.legacy_saved_keys_path()
    path.write_text('{"gemini": true, "openrouter": "yes"}', encoding='utf-8')
    assert credentials.legacy_saved_key_flags() == {'gemini': True, 'openrouter': False}
    path.write_text('[]', encoding='utf-8')
    assert credentials.legacy_saved_key_flags() is None
    path.write_text('{broken', encoding='utf-8')
    assert credentials.legacy_saved_key_flags() is None
