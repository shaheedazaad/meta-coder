"""Per-project run settings: the project's chosen extractor and its last run.

A project chooses one extractor (see extractors.py), saved in
`.meta_coder/extractor.json`. When a run starts, the concrete settings it uses
are written to `.meta_coder/run_settings.json`, together with the extractor's
id, name and a snapshot of its settings, so provenance shows exactly what ran
even after the extractor is edited or deleted.

API keys are never here: they live in the OS credential store (credentials.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .gemini import DEFAULT_SERVICE_TIER
from .projects import Project
from .provenance import atomic_write
from .providers import DEFAULT_PROVIDER, DEFAULT_TIMEOUT_SEC_BY_PROVIDER, PROVIDERS, default_model


MAX_PARALLEL_REQUESTS = 32
MAX_REQUEST_DELAY_SEC = 3600
MIN_REQUEST_TIMEOUT_SEC = 30
MAX_REQUEST_TIMEOUT_SEC = 3600
# OpenRouter's supported effort vocabulary; the blank value delegates to the
# selected model's default. Gemini ignores this provider-specific setting.
REASONING_EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh", "max")
RESPONSE_FORMATS = ("json_schema", "json_object", "none")
MAX_OUTPUT_TOKENS = 100_000_000


def output_token_limit(value: object) -> int:
    """A maximum output from a form or saved settings; 0 (the provider's own
    limit) when it is empty, malformed or not positive."""

    # bool is an int subclass, so a JSON `true` would otherwise load as 1.
    if isinstance(value, bool):
        return 0
    try:
        number = int(str(value).strip() or 0)
    except ValueError:
        return 0
    return max(0, min(number, MAX_OUTPUT_TOKENS))


# An OpenRouter provider slug, optionally with an endpoint variant, as listed by
# /models/{model}/endpoints: `deepinfra` or `deepinfra/fp8`.
OPENROUTER_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?")


def normalize_openrouter_provider(value: object) -> str:
    """Return a lower-case OpenRouter provider slug, or "" for automatic routing.

    Raises ValueError for anything that is not a `vendor` or `vendor/variant` slug."""

    slug = str(value or "").strip().lower()
    if slug and not OPENROUTER_PROVIDER_PATTERN.fullmatch(slug):
        raise ValueError(f"Invalid OpenRouter provider: {value}")
    return slug


@dataclass
class RunSettings:
    provider: str = DEFAULT_PROVIDER
    model: str = ""
    parallel_requests: int = 1
    request_delay_sec: int = 0
    request_timeout_sec: int = 0  # 0 means "provider default" — see clamped()
    service_tier: str = DEFAULT_SERVICE_TIER  # Gemini only; ignored by other providers
    reasoning_effort: str = ""  # OpenRouter/OpenAI-compatible; "" means "provider/model default"
    base_url: str = ""  # OpenAI-compatible only
    response_format: str = "json_schema"  # OpenAI-compatible only
    extractor_id: str = ""
    extractor_name: str = ""
    # Every non-secret setting of the extractor the run used.
    extractor: dict = field(default_factory=dict)
    # 0 means the provider's own limit; see providers.resolve_max_output_tokens.
    max_output_tokens: int = 0
    # OpenRouter only: the backend provider to route to ("" lets OpenRouter
    # choose) and whether requests may fall back to other providers.
    openrouter_provider: str = ""
    openrouter_allow_fallbacks: bool = True

    def clamped(self) -> "RunSettings":
        provider = self.provider if self.provider in PROVIDERS else DEFAULT_PROVIDER
        timeout_default = DEFAULT_TIMEOUT_SEC_BY_PROVIDER.get(provider, 600)
        timeout = int(self.request_timeout_sec or 0)
        effort = (self.reasoning_effort or "").strip().lower()
        return RunSettings(
            provider=provider,
            model=(self.model or "").strip() or default_model(provider),
            parallel_requests=max(1, min(int(self.parallel_requests), MAX_PARALLEL_REQUESTS)),
            request_delay_sec=max(0, min(int(self.request_delay_sec), MAX_REQUEST_DELAY_SEC)),
            request_timeout_sec=(
                timeout_default
                if timeout <= 0
                else max(MIN_REQUEST_TIMEOUT_SEC, min(timeout, MAX_REQUEST_TIMEOUT_SEC))
            ),
            service_tier=(self.service_tier or "").strip() or DEFAULT_SERVICE_TIER,
            reasoning_effort=effort if effort in REASONING_EFFORTS else "",
            base_url=(self.base_url or "").strip().rstrip("/"),
            response_format=self.response_format if self.response_format in RESPONSE_FORMATS else "json_schema",
            extractor_id=str(self.extractor_id or ""),
            extractor_name=str(self.extractor_name or ""),
            extractor=dict(self.extractor) if isinstance(self.extractor, dict) else {},
            max_output_tokens=output_token_limit(self.max_output_tokens),
            openrouter_provider=_valid_openrouter_provider(self.openrouter_provider),
            openrouter_allow_fallbacks=self.openrouter_allow_fallbacks is not False,
        )


def _valid_openrouter_provider(value: object) -> str:
    try:
        return normalize_openrouter_provider(value)
    except ValueError:
        return ""


def _meta_dir(project: Project) -> Path:
    return project.path / ".meta_coder"


def _settings_path(project: Project) -> Path:
    return _meta_dir(project) / "run_settings.json"


def _extractor_choice_path(project: Project) -> Path:
    return _meta_dir(project) / "extractor.json"


def run_settings_saved(project: Project) -> bool:
    return _settings_path(project).is_file()


def load_run_settings(project: Project) -> RunSettings:
    """The settings of the project's last run (or, for a project from before
    extractors, its saved provider settings)."""
    path = _settings_path(project)
    if not path.is_file():
        return RunSettings().clamped()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RunSettings().clamped()
        return RunSettings(
            provider=str(raw.get("provider") or DEFAULT_PROVIDER),
            model=str(raw.get("model") or ""),
            parallel_requests=int(raw.get("parallel_requests") or 1),
            request_delay_sec=int(raw.get("request_delay_sec") or 0),
            request_timeout_sec=int(raw.get("request_timeout_sec") or 0),
            service_tier=str(raw.get("service_tier") or DEFAULT_SERVICE_TIER),
            reasoning_effort=str(raw.get("reasoning_effort") or ""),
            base_url=str(raw.get("base_url") or ""),
            response_format=str(raw.get("response_format") or "json_schema"),
            extractor_id=str(raw.get("extractor_id") or ""),
            extractor_name=str(raw.get("extractor_name") or ""),
            extractor=raw.get("extractor") if isinstance(raw.get("extractor"), dict) else {},
            max_output_tokens=output_token_limit(raw.get("max_output_tokens") or 0),
            openrouter_provider=str(raw.get("openrouter_provider") or ""),
            openrouter_allow_fallbacks=raw.get("openrouter_allow_fallbacks", True) is not False,
        ).clamped()
    except (OSError, ValueError, TypeError):
        return RunSettings().clamped()


def save_run_settings(project: Project, settings: RunSettings) -> RunSettings:
    normalized = settings.clamped()
    atomic_write(_settings_path(project), (json.dumps(asdict(normalized), indent=2) + "\n").encode("utf-8"))
    return normalized


def load_project_extractor_id(project: Project) -> str:
    """The id of the extractor chosen for this project, or "" when none is."""
    try:
        raw = json.loads(_extractor_choice_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("extractor_id") or "") if isinstance(raw, dict) else ""


def save_project_extractor_id(project: Project, extractor_id: str) -> None:
    atomic_write(
        _extractor_choice_path(project),
        (json.dumps({"extractor_id": extractor_id}, indent=2) + "\n").encode("utf-8"),
    )
