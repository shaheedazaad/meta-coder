"""Secure API-key persistence in the operating system credential store.

Every extractor's key lives in one credential-store item, the key vault:
`{"version": 1, "keys": {extractor id: key}}`, saved through python-keyring
(macOS Keychain / Windows Credential Locker / Linux Secret Service).

- Nothing reads the store when the app starts or a page loads. The vault is
  read once, the first time an action needs a key, and then served from memory.
- Adding, replacing or removing a key writes the whole item.
- A vault that can't be read fails the action and is never overwritten.
- There is deliberately no session-only or plaintext-file fallback: if secure
  persistence is unavailable, saving a key fails visibly.

Versions before extractors kept one item per provider, plus a file of
saved-key flags. Those items move into the vault the first time it is opened
(see `KeyVault`), and are deleted only after the vault has been saved.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import keyring
import keyring.errors

from .paths import APP_NAME, app_data_dir


_SERVICE_NAME = f"{APP_NAME} API key"
VAULT_USERNAME = "extractor_keys"


class CredentialStoreError(RuntimeError):
    """The operating-system credential store could not complete an operation."""


class VaultUnavailable(CredentialStoreError):
    """Saved keys exist but the vault could not be read or written."""


UNLOCK_MESSAGE = (
    "Couldn't open your saved API keys. Allow access when your computer asks, then try again."
)


def keyring_available() -> bool:
    """Inspect the backend without touching Keychain or triggering authorization.

    Actual read/write permissions are checked only when the user saves a key or runs an action that needs it.
    A probe entry can require separate access permissions from the saved key.
    """
    try:
        backend = keyring.get_keyring()
        return float(getattr(backend, "priority", 0)) > 0
    except Exception:
        return False


def legacy_saved_keys_path() -> Path:
    """Saved-key flags written by versions before extractors."""
    return app_data_dir() / "saved_api_keys.json"


def legacy_saved_key_flags() -> dict[str, bool] | None:
    """The old saved-key flags: {} when there is no file, None when it can't be read."""
    path = legacy_saved_keys_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {str(name): value is True for name, value in data.items()} if isinstance(data, dict) else None


def _delete_item(username: str) -> bool:
    """Delete a credential item. True once it is gone, including when it never existed."""
    try:
        keyring.delete_password(_SERVICE_NAME, username)
        return True
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception:
        return False
    # Deleting a missing item fails too; that still counts as removed.
    try:
        return not keyring.get_password(_SERVICE_NAME, username)
    except Exception:
        return False


class KeyVault:
    """Every extractor's API key, kept in one credential-store item."""

    def __init__(
        self,
        *,
        legacy_claims: Callable[[], dict[str, str]] | None = None,
        on_legacy_adopted: Callable[[dict[str, bool]], None] | None = None,
    ) -> None:
        """`legacy_claims` maps extractor ids to old per-provider credential items.

        When the vault is first opened those items are copied into it, and
        deleted only after the vault has been saved; `on_legacy_adopted` is
        then told which extractors received a key.
        """
        self._keys: dict[str, str] | None = None
        self._legacy_claims = legacy_claims
        self._on_legacy_adopted = on_legacy_adopted
        # Each load-modify-write holds this, so concurrent saves can't drop keys.
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._keys is not None

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._keys is not None:
                return self._keys
            if not keyring_available():
                raise CredentialStoreError("No operating-system credential store is available.")
            try:
                raw = keyring.get_password(_SERVICE_NAME, VAULT_USERNAME)
                payload = json.loads(raw) if raw else {}
                keys = payload.get("keys") if isinstance(payload, dict) else None
                if raw and not isinstance(keys, dict):
                    raise ValueError("The saved key vault is malformed.")
            except Exception as exc:
                # Not cached: the next action asks again, and nothing writes
                # over an item that could not be read.
                raise VaultUnavailable(UNLOCK_MESSAGE) from exc
            loaded = {str(name): str(value) for name, value in (keys or {}).items() if str(value).strip()}
            # Publish only once old keys have moved in, so `cached` never sees
            # a vault that is still missing them.
            self._adopt_legacy_keys(loaded)
            self._keys = loaded
            return self._keys

    def _adopt_legacy_keys(self, keys: dict[str, str]) -> None:
        claims = self._legacy_claims() if self._legacy_claims else {}
        if not claims:
            return
        for extractor_id, username in claims.items():
            if extractor_id in keys:
                continue
            try:
                value = str(keyring.get_password(_SERVICE_NAME, username) or "").strip()
            except Exception as exc:
                # Every old item stays in place, so a store that is briefly
                # locked never loses a key; the next action tries again.
                raise VaultUnavailable(UNLOCK_MESSAGE) from exc
            if value:
                keys[extractor_id] = value
        self._write(keys)
        removed = {username: _delete_item(username) for username in set(claims.values())}
        if self._on_legacy_adopted:
            # A claim whose old item is still there stays pending, so its
            # removal is retried next session.
            self._on_legacy_adopted({
                extractor_id: extractor_id in keys
                for extractor_id, username in claims.items()
                if removed[username]
            })

    def _write(self, keys: dict[str, str]) -> None:
        try:
            keyring.set_password(_SERVICE_NAME, VAULT_USERNAME, json.dumps({"version": 1, "keys": keys}))
        except Exception as exc:
            raise CredentialStoreError(
                "The OS credential store could not save the key. Check that Keychain or your "
                "system credential service is available, then try again."
            ) from exc

    def get(self, extractor_id: str) -> str | None:
        """Blocking: may open the credential store once per session."""
        return self._load().get(extractor_id)

    def cached(self, extractor_id: str) -> str | None:
        """A key only if the vault is already in memory; never reads the store."""
        return (self._keys or {}).get(extractor_id)

    def set(self, extractor_id: str, api_key: str) -> None:
        """Save a key, writing the whole vault. Nothing is kept if the write fails."""
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("Cannot save an empty API key.")
        with self._lock:
            keys = self._load()
            previous = keys.get(extractor_id)
            keys[extractor_id] = api_key
            try:
                self._write(keys)
            except CredentialStoreError:
                if previous is None:
                    keys.pop(extractor_id)
                else:
                    keys[extractor_id] = previous
                raise

    def delete(self, extractor_id: str, legacy_credential: str = "") -> None:
        """Remove a key, and an old credential item that hasn't moved yet.

        Raises CredentialStoreError when either removal can't be completed.
        """
        with self._lock:
            keys = self._load()
            removed = keys.pop(extractor_id, None)
            if removed is not None:
                try:
                    self._write(keys)
                except CredentialStoreError:
                    keys[extractor_id] = removed
                    raise
            # An old item that couldn't be removed yet would otherwise bring
            # the key back next session.
            if legacy_credential and not _delete_item(legacy_credential):
                raise CredentialStoreError("The OS credential store could not remove the key.")

    def secrets(self) -> list[str]:
        """Every key held in memory, for redacting text shown or saved."""
        return list((self._keys or {}).values())
