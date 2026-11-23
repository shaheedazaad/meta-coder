"""Extractors: named, saved setups a run uses to reach a model.

An extractor holds a provider, model, endpoint and request settings. Its API
key is kept in the OS credential store's key vault (credentials.KeyVault) by
extractor id, never in `extractors.json`. A project chooses one extractor; the
coding-manual and coding-sheet drafts use the one chosen in Settings.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import credentials
from .app_settings import load_app_settings, read_raw_app_settings, save_app_settings
from .gemini import DEFAULT_SERVICE_TIER
from .openai_compatible import normalize_base_url
from .paths import app_data_dir
from .projects import Project, list_projects
from .provenance import atomic_write
from .providers import DEFAULT_TIMEOUT_SEC_BY_PROVIDER, PROVIDER_LABELS, PROVIDERS, default_model
from .settings import (
    MAX_PARALLEL_REQUESTS,
    MAX_REQUEST_DELAY_SEC,
    MAX_REQUEST_TIMEOUT_SEC,
    MIN_REQUEST_TIMEOUT_SEC,
    REASONING_EFFORTS,
    RESPONSE_FORMATS,
    RunSettings,
    load_project_extractor_id,
    load_run_settings,
    run_settings_saved,
    save_project_extractor_id,
)


RESPONSE_FORMAT_LABELS = {
    "json_schema": "Strict JSON schema",
    "json_object": "JSON object",
    "none": "Prompt only",
}
SERVICE_TIERS = ("flex", "standard")
COMMON_FIELDS = ("name", "provider", "model", "parallel_requests", "request_delay_sec", "request_timeout_sec")
PROVIDER_FIELDS = {
    "gemini": ("service_tier",),
    "openrouter": ("reasoning_effort",),
    "openai_compatible": ("base_url", "response_format", "reasoning_effort"),
}
NAME_MAX_LENGTH = 60


class ExtractorError(ValueError):
    """An extractor definition is invalid or cannot be found."""


class ExtractorsUnreadable(RuntimeError):
    """The extractors file exists but can't be read, so nothing may overwrite it."""


@dataclass(frozen=True)
class Extractor:
    id: str
    name: str
    provider: str
    model: str
    parallel_requests: int = 1
    request_delay_sec: int = 0
    request_timeout_sec: int = 600
    service_tier: str = DEFAULT_SERVICE_TIER
    reasoning_effort: str = ""
    base_url: str = ""
    response_format: str = "json_schema"
    # Whether the key vault holds a key for this extractor. Recorded here so
    # pages can show it without opening the credential store.
    key_saved: bool = False
    # Set only on extractors created by migration until their old
    # per-provider key moves into the vault: the old item's name.
    legacy_credential: str = ""

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    @property
    def requires_key(self) -> bool:
        return self.provider != "openai_compatible"

    @property
    def has_saved_key(self) -> bool:
        """Whether the vault holds a key for this extractor, or will once its old key moves."""
        return self.key_saved or bool(self.legacy_credential)

    def snapshot(self) -> dict[str, object]:
        """Every non-secret setting a run uses, for run settings and provenance."""
        values = asdict(self)
        return {name: values[name] for name in COMMON_FIELDS + PROVIDER_FIELDS[self.provider]}

    def settings_key(self) -> tuple:
        """The settings that change what a run does, without the name."""
        return tuple(sorted((k, v) for k, v in self.snapshot().items() if k != "name"))

    def summary(self) -> list[tuple[str, str]]:
        """Label/value rows describing the settings, for display."""
        rows = [("Provider", self.provider_label), ("Model", self.model or "Not set")]
        if self.provider == "gemini":
            rows.append(("Service tier", self.service_tier))
        if self.provider == "openai_compatible":
            rows.append(("API base URL", self.base_url or "Not set"))
            rows.append(("JSON output mode", RESPONSE_FORMAT_LABELS[self.response_format]))
        if self.provider != "gemini":
            rows.append(("Reasoning effort", self.reasoning_effort or "Model default"))
        rows.append(("Parallel requests", str(self.parallel_requests)))
        rows.append(("Delay between requests", f"{self.request_delay_sec} s"))
        rows.append(("Request timeout", f"{self.request_timeout_sec} s"))
        return rows


# Every read-modify-write of the extractors file holds this lock, so requests
# handled on worker threads can't overwrite each other's changes.
_LOCK = threading.RLock()


def _serialized(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with _LOCK:
            return function(*args, **kwargs)
    return locked


def extractors_path() -> Path:
    return app_data_dir() / "extractors.json"


def _int_field(values: Mapping[str, object], name: str, label: str, low: int, high: int, default: int) -> int:
    raw = values.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        number = int(str(raw).strip())
    except ValueError:
        raise ExtractorError(f"{label} must be a whole number.") from None
    if not low <= number <= high:
        raise ExtractorError(f"{label} must be between {low} and {high}.")
    return number


def _build(values: Mapping[str, object], *, extractor_id: str, complete: bool = True) -> Extractor:
    """Validate raw values into an extractor, keeping only its provider's fields.

    `complete=False` accepts a saved or migrated extractor that still lacks
    its model or, for OpenAI-compatible, its base URL; it can't run until
    they are set.
    """
    provider = str(values.get("provider") or "")
    if provider not in PROVIDERS:
        raise ExtractorError("Choose a provider.")
    name = " ".join(str(values.get("name") or "").split())
    if not name:
        raise ExtractorError("Give the extractor a name.")
    if len(name) > NAME_MAX_LENGTH:
        raise ExtractorError(f"Extractor names can be at most {NAME_MAX_LENGTH} characters.")
    model = str(values.get("model") or "").strip()
    if not model and complete:
        raise ExtractorError("Enter a model ID.")
    extractor = Extractor(
        id=extractor_id,
        name=name,
        provider=provider,
        model=model,
        parallel_requests=_int_field(values, "parallel_requests", "Parallel requests", 1, MAX_PARALLEL_REQUESTS, 1),
        request_delay_sec=_int_field(values, "request_delay_sec", "Delay between requests", 0, MAX_REQUEST_DELAY_SEC, 0),
        request_timeout_sec=_int_field(
            values, "request_timeout_sec", "Request timeout", MIN_REQUEST_TIMEOUT_SEC, MAX_REQUEST_TIMEOUT_SEC,
            DEFAULT_TIMEOUT_SEC_BY_PROVIDER[provider],
        ),
        key_saved=values.get("key_saved") is True,
        legacy_credential=str(values.get("legacy_credential") or ""),
    )
    if provider == "gemini":
        tier = str(values.get("service_tier") or "").strip().lower() or DEFAULT_SERVICE_TIER
        if tier not in SERVICE_TIERS:
            raise ExtractorError("Choose a service tier.")
        return replace(extractor, service_tier=tier)
    effort = str(values.get("reasoning_effort") or "").strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ExtractorError("Choose a reasoning effort.")
    extractor = replace(extractor, reasoning_effort=effort)
    if provider == "openrouter":
        return extractor
    base_url = str(values.get("base_url") or "").strip()
    if base_url or complete:
        try:
            base_url = normalize_base_url(base_url)
        except ValueError as exc:
            raise ExtractorError(str(exc)) from None
    response_format = str(values.get("response_format") or "json_schema")
    if response_format not in RESPONSE_FORMATS:
        raise ExtractorError("Choose a JSON output mode.")
    return replace(extractor, base_url=base_url, response_format=response_format)


def _stored(extractor: Extractor) -> dict[str, object]:
    record: dict[str, object] = {"id": extractor.id, **extractor.snapshot()}
    if extractor.key_saved:
        record["key_saved"] = True
    if extractor.legacy_credential:
        record["legacy_credential"] = extractor.legacy_credential
    return record


def _write(extractors: list[Extractor], kept: list[dict]) -> None:
    path = extractors_path()
    data = json.dumps({"version": 1, "extractors": [*(_stored(item) for item in extractors), *kept]}, indent=2)
    atomic_write(path, (data + "\n").encode("utf-8"))
    try:
        os.chmod(path, 0o600)
    except OSError:  # permissions are best effort on unusual filesystems
        pass


def _read_records(path: Path) -> tuple[list[Extractor], list[dict]]:
    """The extractors in the file, and the records that couldn't be loaded.

    Raises ExtractorsUnreadable when the file can't be read or isn't an
    extractors file, so a later save never replaces what couldn't be read.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExtractorsUnreadable(
            f"Couldn't read {path}: {exc}. Fix or move the file, then try again."
        ) from exc
    records = payload.get("extractors") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ExtractorsUnreadable(f"{path} has no extractor list. Fix or move the file, then try again.")
    extractors: list[Extractor] = []
    unloadable: list[dict] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("id"):
            continue
        try:
            extractors.append(_build(record, extractor_id=str(record["id"]), complete=False))
        except ExtractorError:
            # Kept in the file (see save_extractors), so it isn't lost.
            unloadable.append(record)
    return extractors, unloadable


@_serialized
def save_extractors(extractors: list[Extractor]) -> None:
    """Write every extractor. Records that can't be loaded are written back unchanged."""
    path = extractors_path()
    ids = {item.id for item in extractors}
    kept = [record for record in (_read_records(path)[1] if path.is_file() else []) if record.get("id") not in ids]
    _write(extractors, kept)


@_serialized
def load_extractors() -> list[Extractor]:
    """Every loadable extractor in creation order."""
    path = extractors_path()
    return _read_records(path)[0] if path.is_file() else []


def get_extractor(extractor_id: str) -> Extractor | None:
    if not extractor_id:
        return None
    return next((item for item in load_extractors() if item.id == extractor_id), None)


def _check_unique_name(extractors: list[Extractor], name: str, extractor_id: str) -> None:
    if any(item.name.casefold() == name.casefold() and item.id != extractor_id for item in extractors):
        raise ExtractorError(f"An extractor called “{name}” already exists.")


def _index(extractors: list[Extractor], extractor_id: str) -> int:
    for index, item in enumerate(extractors):
        if item.id == extractor_id:
            return index
    raise ExtractorError("That extractor no longer exists.")


def validate_new_extractor(values: Mapping[str, object]) -> Extractor:
    """Validate a new extractor without saving it."""
    extractor = _build({**values, "key_saved": False, "legacy_credential": ""}, extractor_id=str(uuid.uuid4()))
    _check_unique_name(load_extractors(), extractor.name, extractor.id)
    return extractor


@_serialized
def add_extractor(extractor: Extractor) -> Extractor:
    extractors = load_extractors()
    _check_unique_name(extractors, extractor.name, extractor.id)
    save_extractors([*extractors, extractor])
    return extractor


def updated_extractor(current: Extractor, values: Mapping[str, object]) -> Extractor:
    """Validate new settings for an extractor. Its provider and key status never change here."""
    return _build(
        {**values, "provider": current.provider, "key_saved": current.key_saved,
         "legacy_credential": current.legacy_credential},
        extractor_id=current.id,
    )


@_serialized
def update_extractor(extractor_id: str, values: Mapping[str, object]) -> Extractor:
    extractors = load_extractors()
    index = _index(extractors, extractor_id)
    updated = updated_extractor(extractors[index], values)
    _check_unique_name(extractors, updated.name, updated.id)
    extractors[index] = updated
    save_extractors(extractors)
    return updated


def _unique_name(base: str, taken: set[str], suffix_for: Callable[[int], str]) -> str:
    counter = 1
    while True:
        suffix = suffix_for(counter)
        # Shorten the base so the suffix always fits the limit.
        name = base[:NAME_MAX_LENGTH - len(suffix)].rstrip() + suffix
        if name.casefold() not in taken:
            return name
        counter += 1


@_serialized
def duplicate_extractor(extractor_id: str) -> Extractor:
    """Copy every setting except the key, under a new unused name."""
    extractors = load_extractors()
    original = extractors[_index(extractors, extractor_id)]
    name = _unique_name(
        original.name, {item.name.casefold() for item in extractors},
        lambda counter: " copy" if counter == 1 else f" copy {counter}",
    )
    copy = replace(original, id=str(uuid.uuid4()), name=name, key_saved=False, legacy_credential="")
    save_extractors([*extractors, copy])
    return copy


@_serialized
def delete_extractor(extractor_id: str) -> Extractor:
    extractors = load_extractors()
    removed = extractors.pop(_index(extractors, extractor_id))
    save_extractors(extractors)
    return removed


@_serialized
def set_key_saved(extractor_id: str, saved: bool) -> None:
    """Record whether the vault holds this extractor's key; a removal also drops any old key claim."""
    extractors = load_extractors()
    index = _index(extractors, extractor_id)
    changes: dict[str, object] = {"key_saved": saved}
    if not saved:
        changes["legacy_credential"] = ""
    extractors[index] = replace(extractors[index], **changes)
    save_extractors(extractors)


def legacy_claims() -> dict[str, str]:
    """Old credential items still waiting to move into the key vault, by extractor id."""
    return {item.id: item.legacy_credential for item in load_extractors() if item.legacy_credential}


@_serialized
def record_legacy_adoption(result: Mapping[str, bool]) -> None:
    """Record which migrated extractors received their old key."""
    extractors = load_extractors()
    for index, item in enumerate(extractors):
        if item.id in result:
            extractors[index] = replace(item, key_saved=bool(result[item.id]), legacy_credential="")
    save_extractors(extractors)


def other_claims(extractor: Extractor) -> bool:
    """Whether another extractor still waits for the same old credential item."""
    return any(
        item.legacy_credential == extractor.legacy_credential and item.id != extractor.id
        for item in load_extractors()
    )


def setup_problem(extractor: Extractor) -> str | None:
    """What must be set before this extractor can run, or None (the key aside)."""
    if not extractor.model:
        return f"Choose a model for the “{extractor.name}” extractor in Settings."
    if extractor.provider == "openai_compatible" and not extractor.base_url:
        return f"Enter the API base URL for the “{extractor.name}” extractor in Settings."
    return None


def run_settings_for(extractor: Extractor) -> RunSettings:
    """The concrete settings a run, retry or draft uses with this extractor."""
    return RunSettings(
        provider=extractor.provider,
        model=extractor.model,
        parallel_requests=extractor.parallel_requests,
        request_delay_sec=extractor.request_delay_sec,
        request_timeout_sec=extractor.request_timeout_sec,
        service_tier=extractor.service_tier,
        reasoning_effort=extractor.reasoning_effort,
        base_url=extractor.base_url,
        response_format=extractor.response_format,
        extractor_id=extractor.id,
        extractor_name=extractor.name,
        extractor=extractor.snapshot(),
    ).clamped()


@dataclass(frozen=True)
class ProjectExtractor:
    """The extractor a project will use next, if any."""

    extractor: Extractor | None
    # True when the project's extractor has since been deleted.
    deleted: bool = False


def project_extractor(project: Project, extractors: list[Extractor] | None = None) -> ProjectExtractor:
    """The extractor chosen for a project, or the one its last run used."""
    by_id = {item.id: item for item in (load_extractors() if extractors is None else extractors)}
    chosen = load_project_extractor_id(project) or load_run_settings(project).extractor_id
    if not chosen:
        return ProjectExtractor(None)
    return ProjectExtractor(by_id.get(chosen), deleted=chosen not in by_id)


def projects_using(extractor_id: str, *, root: Path | None = None) -> list[str]:
    """Names of projects whose next run would use this extractor."""
    extractors = load_extractors()
    return [
        project.name for project in list_projects(root=root)
        if (chosen := project_extractor(project, extractors).extractor) is not None and chosen.id == extractor_id
    ]


# --- Migration from versions before extractors --------------------------------

def _connected(provider: str, flags: Mapping[str, bool], base_url: str) -> bool:
    if flags.get(provider) is True:
        return True
    return provider == "openai_compatible" and bool(base_url)


@_serialized
def migrate_legacy_settings(*, projects_root: Path | None = None) -> bool:
    """Turn the provider setup of versions before extractors into extractors.

    Runs once, when `extractors.json` does not exist, and never reads the
    credential store: each migrated extractor names its provider's old
    credential item, and the key vault moves that key the first time it is
    opened.

    A provider is migrated when it had a saved key or, for OpenAI-compatible,
    a base URL. Settings left at the defaults with no key are skipped. Each
    connected provider gets an extractor named after it; the drafting model
    and each project's run settings then reuse an extractor with the same
    settings, or get a new one sharing the provider's key. A saved key whose
    settings are missing or unusable is still kept, on an extractor that asks
    for its model or base URL before it can run.

    Project choices and the drafting choice are written before the
    extractors file, and old settings are removed only after it, so an
    interrupted migration runs again from the start and loses nothing.
    """
    if extractors_path().exists():
        return False
    raw_app = read_raw_app_settings()
    app = raw_app or {}
    flags = credentials.legacy_saved_key_flags()
    flags = flags or {}
    try:
        base_url = normalize_base_url(str(app.get("openai_base_url") or ""))
    except ValueError:
        base_url = ""
    endpoint = {"base_url": base_url, "response_format": app.get("openai_response_format")}

    extractors: list[Extractor] = []
    untouched: dict[str, int] = {}  # provider -> index of its unclaimed base extractor
    key_fields: dict[str, dict[str, object]] = {}

    def build(values: Mapping[str, object], name: str) -> Extractor:
        provider = str(values["provider"])
        merged = {**values, "name": name, **key_fields[provider]}
        try:
            return _build(merged, extractor_id=str(uuid.uuid4()), complete=False)
        except ExtractorError:
            # Keep the key with an extractor that asks for its settings again.
            return _build({"provider": provider, "name": name, **key_fields[provider]},
                          extractor_id=str(uuid.uuid4()), complete=False)

    for provider in PROVIDERS:
        if not _connected(provider, flags, base_url):
            continue
        has_key = flags.get(provider) is True
        key_fields[provider] = {"key_saved": has_key, "legacy_credential": provider if has_key else ""}
        base = build({"provider": provider, "model": default_model(provider),
                      **(endpoint if provider == "openai_compatible" else {})},
                     PROVIDER_LABELS[provider])
        untouched[provider] = len(extractors)
        extractors.append(base)

    def claim(values: Mapping[str, object], origin: str) -> str | None:
        provider = str(values.get("provider") or "")
        if provider not in key_fields:
            return None
        values = {**values, **(endpoint if provider == "openai_compatible" else {})}
        candidate = build(values, "candidate")
        for index, item in enumerate(extractors):
            if item.provider == provider and item.settings_key() == candidate.settings_key():
                if untouched.get(provider) == index:
                    del untouched[provider]
                return item.id
        if provider in untouched:
            index = untouched.pop(provider)
            extractors[index] = replace(candidate, id=extractors[index].id, name=extractors[index].name)
            return extractors[index].id
        name = _unique_name(
            f"{PROVIDER_LABELS[provider]} ({origin})",
            {item.name.casefold() for item in extractors},
            lambda counter: "" if counter == 1 else f" {counter}",
        )
        extractors.append(replace(candidate, name=name))
        return extractors[-1].id

    drafting_provider = str(app.get("manual_generator_provider") or "gemini")
    drafting_id = claim({
        "provider": drafting_provider,
        "model": str(app.get("manual_generator_model") or "") or default_model(drafting_provider),
    }, "drafting") or ""

    choices: list[tuple[Project, str]] = []
    for project in sorted(list_projects(root=projects_root), key=lambda item: (item.created_at, item.name)):
        # No extractors file exists yet, so a choice already saved here comes
        # from an interrupted migration and names an extractor that was never
        # written. Rebuild it from the project's last run settings.
        if not run_settings_saved(project):
            continue
        previous = load_run_settings(project)
        chosen = claim({
            "provider": previous.provider, "model": previous.model,
            "parallel_requests": previous.parallel_requests, "request_delay_sec": previous.request_delay_sec,
            "request_timeout_sec": previous.request_timeout_sec, "service_tier": previous.service_tier,
            "reasoning_effort": previous.reasoning_effort,
        }, project.name)
        if chosen:
            choices.append((project, chosen))

    for project, chosen in choices:
        save_project_extractor_id(project, chosen)
    if raw_app is not None and drafting_id:
        atomic_write(
            app_data_dir() / "app_settings.json",
            (json.dumps({**app, "drafting_extractor_id": drafting_id}, indent=2) + "\n").encode("utf-8"),
        )
    _write(extractors, [])
    # Old provider settings and flags are now held by the extractors.
    if raw_app:
        save_app_settings(load_app_settings())
    if credentials.legacy_saved_key_flags() is not None:
        credentials.legacy_saved_keys_path().unlink(missing_ok=True)
    return True
