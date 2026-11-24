"""Keep every test away from the developer's real app data and credential store."""

import keyring
import keyring.backend
import keyring.errors
import pytest


class MemoryKeyring(keyring.backend.KeyringBackend):
    """An in-memory credential store that counts reads and writes."""

    priority = 1  # type: ignore[assignment]

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self.reads = 0
        self.writes = 0

    def set_password(self, service, username, password):
        self.writes += 1
        self.store[(service, username)] = password

    def get_password(self, service, username):
        self.reads += 1
        return self.store.get((service, username))

    def delete_password(self, service, username):
        try:
            del self.store[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError("not found") from exc


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("META_CODER_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture
def drafting_extractor():
    """A Gemini extractor chosen for drafting (in whichever app data folder is current)."""
    from meta_coder import extractors
    from meta_coder.app_settings import AppSettings, save_app_settings

    extractor = extractors.add_extractor(
        extractors.Extractor(id="drafting", name="Drafting", provider="gemini", model="draft-model"))
    save_app_settings(AppSettings(drafting_extractor_id=extractor.id))
    return extractor
