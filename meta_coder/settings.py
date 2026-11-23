"""Per-project run settings — provider, model, parallelism, request pacing
(plan.md Part A6: "different models have very different sane concurrency/
pacing defaults").

The API key is deliberately NOT here: it is global per provider and persisted in
the OS credential store (see credentials.py), not per-project. Once unlocked, one
key per provider is used across every project in the running session.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .gemini import DEFAULT_SERVICE_TIER
from .projects import Project
from .providers import DEFAULT_PROVIDER, DEFAULT_TIMEOUT_SEC_BY_PROVIDER, PROVIDERS, default_model


MAX_PARALLEL_REQUESTS = 32
MAX_REQUEST_DELAY_SEC = 3600
MIN_REQUEST_TIMEOUT_SEC = 30
MAX_REQUEST_TIMEOUT_SEC = 3600
# OpenRouter's supported effort vocabulary; the blank value delegates to the
# selected model's default. Gemini ignores this provider-specific setting.
REASONING_EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh", "max")
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
    # OpenRouter only: the backend provider to route to ("" lets OpenRouter
    # choose) and whether requests may fall back to other providers.
    openrouter_provider: str = ""
    openrouter_allow_fallbacks: bool = True

    def clamped(self) -> "RunSettings":
        provider = self.provider if self.provider in PROVIDERS else DEFAULT_PROVIDER
        timeout_default = DEFAULT_TIMEOUT_SEC_BY_PROVIDER.get(provider, 600)
        timeout = int(self.request_timeout_sec or 0)
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
            reasoning_effort=(self.reasoning_effort or "").strip().lower()
            if (self.reasoning_effort or "").strip().lower() in REASONING_EFFORTS
            else "",
            openrouter_provider=_valid_openrouter_provider(self.openrouter_provider),
            openrouter_allow_fallbacks=self.openrouter_allow_fallbacks is not False,
        )


def _valid_openrouter_provider(value: object) -> str:
    try:
        return normalize_openrouter_provider(value)
    except ValueError:
        return ""


def _settings_path(project: Project) -> Path:
    return project.path / ".meta_coder" / "run_settings.json"


def load_run_settings(project: Project) -> RunSettings:
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
            openrouter_provider=str(raw.get("openrouter_provider") or ""),
            openrouter_allow_fallbacks=raw.get("openrouter_allow_fallbacks", True) is not False,
        ).clamped()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return RunSettings().clamped()


def save_run_settings(project: Project, settings: RunSettings) -> RunSettings:
    normalized = settings.clamped()
    path = _settings_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(normalized), indent=2) + "\n", encoding="utf-8")
    return normalized
