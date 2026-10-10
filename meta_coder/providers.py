"""Dispatch extraction and manual drafting to the selected provider."""

from __future__ import annotations

from pathlib import Path
import threading

from . import gemini, openrouter, openai_compatible
from .coding_sheet import CodingSheetRow
from .extraction import ExtractionResult
from .manual import CodingManual
from .quote_check import annotate_quote_checks
from .manual_drafting import (
    build_manual_draft_prompt,
    build_manual_draft_schema,
    parse_manual_draft_response,
)


PROVIDERS = ("gemini", "openrouter", "openai_compatible")
DEFAULT_PROVIDER = "gemini"

DEFAULT_MODEL_BY_PROVIDER = {
    "openai_compatible": openai_compatible.DEFAULT_MODEL,
    "gemini": gemini.DEFAULT_MODEL,
    "openrouter": openrouter.DEFAULT_MODEL,
}
DEFAULT_TIMEOUT_SEC_BY_PROVIDER = {
    "openai_compatible": openai_compatible.DEFAULT_TIMEOUT_SEC,
    "gemini": gemini.DEFAULT_TIMEOUT_SEC,
    "openrouter": openrouter.DEFAULT_TIMEOUT_SEC,
}
PROVIDER_LABELS = {"gemini": "Gemini", "openrouter": "OpenRouter", "openai_compatible": "OpenAI-compatible"}


def default_model(provider: str) -> str:
    return DEFAULT_MODEL_BY_PROVIDER.get(provider, gemini.DEFAULT_MODEL)


def check_model(provider: str, model: str, *, api_key: str, base_url: str = "") -> list[str]:
    if provider == "openai_compatible":
        return openai_compatible.check_model(model, api_key=api_key, base_url=base_url)
    if provider == "gemini":
        return gemini.check_model(model, api_key=api_key)
    if provider == "openrouter":
        return openrouter.check_model(model, api_key=api_key)
    return [f"Unknown provider: {provider!r}."]


def _generate_draft_text(
    *,
    provider: str,
    prompt: str,
    response_schema: dict,
    api_key: str,
    model: str,
    timeout_sec: int | None = None,
    service_tier: str = gemini.DEFAULT_SERVICE_TIER,
    reasoning_effort: str = "",
    base_url: str = "",
    response_format: str = "json_schema",
) -> str:
    """Shared text-only structured generation for reviewable import drafts."""
    if provider == "openai_compatible":
        raw_text = openai_compatible.generate_structured_text(
            prompt=prompt, response_schema=response_schema,
            api_key=api_key, model=model, base_url=base_url, response_format=response_format,
            timeout_sec=timeout_sec or openai_compatible.DEFAULT_TIMEOUT_SEC,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "openrouter":
        raw_text = openrouter.generate_structured_text(
            prompt=prompt,
            response_schema=response_schema,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec or openrouter.DEFAULT_TIMEOUT_SEC,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "gemini":
        raw_text = gemini.generate_structured_text(
            prompt=prompt,
            response_schema=response_schema,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec or gemini.DEFAULT_TIMEOUT_SEC,
            service_tier=service_tier,
        )
    else:
        raise ValueError(f"Unknown provider: {provider!r} (use one of {PROVIDERS}).")
    return raw_text


def draft_coding_manual(
    *,
    provider: str,
    document_text: str,
    api_key: str,
    model: str,
    timeout_sec: int | None = None,
    service_tier: str = gemini.DEFAULT_SERVICE_TIER,
    reasoning_effort: str = "",
    base_url: str = "",
    response_format: str = "json_schema",
) -> CodingManual:
    """Draft and validate a manual using the selected provider."""
    raw_text = _generate_draft_text(
        provider=provider, prompt=build_manual_draft_prompt(document_text),
        response_schema=build_manual_draft_schema(dialect="gemini" if provider == "gemini" else "json_schema"),
        api_key=api_key, model=model, timeout_sec=timeout_sec, service_tier=service_tier,
        reasoning_effort=reasoning_effort, base_url=base_url, response_format=response_format,
    )
    return parse_manual_draft_response(raw_text)


def draft_coding_sheet(
    *, provider: str, source: dict, manual: str, notes: str, filenames: list[str],
    api_key: str, model: str, base_url: str = "", response_format: str = "json_schema",
) -> dict:
    """Convert source rows without modifying the saved sheet or results."""
    from .coding_sheet_drafting import (
        build_sheet_draft_prompt, build_sheet_draft_schema, parse_sheet_draft_response,
    )

    raw_text = _generate_draft_text(
        provider=provider,
        prompt=build_sheet_draft_prompt(source, manual=manual, notes=notes, filenames=filenames),
        response_schema=build_sheet_draft_schema(dialect="gemini" if provider == "gemini" else "json_schema"),
        api_key=api_key, model=model, base_url=base_url, response_format=response_format,
    )
    return parse_sheet_draft_response(raw_text, source_row_count=len(source["rows"]))


def extract_pdf_effects(
    *,
    provider: str,
    pdf_path: Path,
    manual: CodingManual,
    rows: list[CodingSheetRow],
    api_key: str,
    model: str,
    timeout_sec: int | None = None,
    service_tier: str = gemini.DEFAULT_SERVICE_TIER,
    reasoning_effort: str = "",
    base_url: str = "",
    response_format: str = "json_schema",
    cancel_event: threading.Event | None = None,
) -> ExtractionResult:
    """Route to the right adapter. Each adapter keeps its own kwargs (Gemini's
    `service_tier`, OpenRouter's `reasoning_effort`) — this function picks out
    only the subset that provider actually accepts rather than forwarding both
    blindly, since neither adapter's signature accepts the other's kwarg."""

    if provider == "openai_compatible":
        result = openai_compatible.extract_pdf_effects(
            pdf_path=pdf_path, manual=manual, rows=rows, api_key=api_key, model=model,
            base_url=base_url, response_format=response_format,
            timeout_sec=timeout_sec or openai_compatible.DEFAULT_TIMEOUT_SEC,
            reasoning_effort=reasoning_effort, cancel_event=cancel_event,
        )
    elif provider == "openrouter":
        result = openrouter.extract_pdf_effects(
            pdf_path=pdf_path,
            manual=manual,
            rows=rows,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec or openrouter.DEFAULT_TIMEOUT_SEC,
            reasoning_effort=reasoning_effort,
            cancel_event=cancel_event,
        )
    elif provider == "gemini":
        result = gemini.extract_pdf_effects(
            pdf_path=pdf_path,
            manual=manual,
            rows=rows,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec or gemini.DEFAULT_TIMEOUT_SEC,
            service_tier=service_tier,
            cancel_event=cancel_event,
        )
    else:
        raise ValueError(f"Unknown provider: {provider!r} (use one of {PROVIDERS}).")
    # Every provider's quotes are checked against the same local text layer.
    annotate_quote_checks(result.coded_by_row_id, pdf_path)
    return result
