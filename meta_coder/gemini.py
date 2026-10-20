"""Gemini provider adapter (plan.md Part A4 / Problem 2).

Sends the original PDF bytes natively, batches every coding-sheet row for one PDF
into a single request, and relies on `mechanism.validate_response` for the hard
row_id check. Uses the plain REST API via `urllib` rather than the SDK, to keep the
dependency footprint minimal and the request/response shape fully visible.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .coding_sheet import CodingSheetRow
from .provenance import audited_transport
from .extraction import (
    ExtractionCancelled,
    ExtractionResult,
    ProviderError,
    cancellable_urlopen,
    parse_json_response,
)
import threading
from .manual import CodingManual
from .mechanism import ValidationResult, build_response_schema, validate_response


API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT_SEC = 600
DEFAULT_SERVICE_TIER = "flex"  # 50% lower cost, variable latency/best-effort availability

BASELINE_RULES = """You are a research assistant coding effects for a meta-analysis.

Rules:
1. Only use information present in the article. No outside knowledge, no inference
   or best-guessing, unless a field's description explicitly says otherwise.
2. If the article does not report a value for a field, set "value" to null and
   say "Not reported" in "evidence" rather than guessing. A missing field is invalid.
3. Code values must follow the exact formatting the field description specifies
   (units, decimal places, category label text).
4. "evidence" must be concise but specific: include a page number and/or a short
   quotation, not a vague paraphrase.
5. For a categorical field, report exactly one level unless its description states
   that multiple levels are valid.
6. Every object in "effects" MUST include a "row_id" that exactly matches one of the
   requested row IDs below. Never invent, rename, or omit a row_id. Return exactly
   one object per requested row, no more, no fewer."""


def _build_prompt(manual: CodingManual, rows: list[CodingSheetRow]) -> str:
    row_lines = "\n".join(
        f"- row_id: {row.row_id}\n  locator: {row.locator or '(none)'}" for row in rows
    )
    return (
        f"{BASELINE_RULES}\n\n"
        f"Effect definition for this meta-analysis:\n{manual.effect_definition}\n\n"
        f"Code the following {len(rows)} row(s) from the attached PDF. Each row is one "
        "study/experiment/condition; use its locator to find the right one. A row with no "
        "locator means this manuscript has only one effect of interest: identify and code it. "
        "If it is not clear which effect that is, say so in the row's `notes` field rather "
        "than guessing:\n"
        f"{row_lines}"
    )


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "[redacted]") if api_key else text


def check_model(model: str, *, api_key: str, timeout_sec: int = 20) -> list[str]:
    """Confirm a Gemini model exists and accepts generateContent before saving it."""

    if not api_key:
        return ["Save a Gemini API key before validating a model."]
    name = model.strip()
    if not name:
        return ["Enter a Gemini model name."]
    url = f"{API_BASE}/models/{urllib.parse.quote(name, safe='-_.')}?key={urllib.parse.quote(api_key, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [f"Gemini could not validate `{name}` ({exc.code})."]
    except urllib.error.URLError as exc:
        return [f"Could not reach Gemini to validate the model: {_redact(str(exc.reason), api_key)}"]
    methods = {str(method).lower() for method in body.get("supportedGenerationMethods") or []}
    if methods and "generatecontent" not in methods:
        return [f"Gemini model `{name}` does not support generateContent."]
    return []


def _call_gemini_parts(
    *,
    model: str,
    api_key: str,
    parts: list[dict[str, Any]],
    response_schema: dict[str, Any],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    url = f"{API_BASE}/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": 0,
        },
        # Flex tier: ~50% lower cost in exchange for variable latency and
        # best-effort availability (per Gemini API docs). Omit/"standard" for the
        # default tier.
        "service_tier": service_tier,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        raw_body = audited_transport(cancellable_urlopen,
            request, timeout=timeout_sec, cancel_event=cancel_event or threading.Event()
        )
        raw_response = raw_body.decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Gemini returned invalid response JSON: {exc}", raw_response=raw_response
            ) from exc
    except urllib.error.HTTPError as exc:
        detail = _redact(exc.read().decode("utf-8", errors="replace"), api_key)
        raise ProviderError(f"Gemini API error ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        if cancel_event and cancel_event.is_set():
            raise ExtractionCancelled("Extraction cancelled by user.") from exc
        raise ProviderError(f"Could not reach Gemini: {_redact(str(exc.reason), api_key)}") from exc

    if not isinstance(body, dict):
        raise ProviderError("Gemini returned an invalid response object.", raw_response=raw_response)
    candidates = body.get("candidates") or []
    if not isinstance(candidates, list):
        raise ProviderError("Gemini returned invalid candidates.", raw_response=raw_response)
    parts = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ProviderError("Gemini returned an invalid candidate.", raw_response=raw_response)
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            raise ProviderError("Gemini returned invalid candidate content.", raw_response=raw_response)
        candidate_parts = content.get("parts") or []
        if not isinstance(candidate_parts, list):
            raise ProviderError("Gemini returned invalid content parts.", raw_response=raw_response)
        for part in candidate_parts:
            if not isinstance(part, dict):
                raise ProviderError("Gemini returned an invalid content part.", raw_response=raw_response)
            if "text" in part:
                if not isinstance(part["text"], str):
                    raise ProviderError("Gemini returned invalid text content.", raw_response=raw_response)
                parts.append(part["text"])
    text = "".join(parts)
    if not text.strip():
        finish_reason = (candidates[0].get("finishReason") if candidates else None) or "unknown"
        raise ProviderError(f"Gemini returned no text (finishReason: {finish_reason}).")

    usage = body.get("usageMetadata") or {}
    if not isinstance(usage, dict):
        raise ProviderError("Gemini returned invalid usage metadata.", raw_response=raw_response)
    for field in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount"):
        value = usage.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ProviderError("Gemini returned invalid usage metadata.", raw_response=raw_response)
    # Thinking tokens are billed as output but reported separately from the
    # candidate (answer) tokens, so output is their sum when either is present.
    output_counts = [usage[f] for f in ("candidatesTokenCount", "thoughtsTokenCount") if usage.get(f) is not None]
    served_model = body.get("modelVersion")
    tokens = {
        "input_tokens": usage.get("promptTokenCount"),
        "output_tokens": sum(output_counts) if output_counts else None,
        "served_model": served_model if isinstance(served_model, str) and served_model else None,
    }
    return text, tokens


def _call_gemini(
    *,
    model: str,
    api_key: str,
    prompt: str,
    pdf_bytes: bytes,
    response_schema: dict[str, Any],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    """Structured generation with a native PDF, kept as the extraction seam."""

    return _call_gemini_parts(
        model=model,
        api_key=api_key,
        parts=[
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": "application/pdf",
                    "data": base64.b64encode(pdf_bytes).decode("ascii"),
                }
            },
        ],
        response_schema=response_schema,
        timeout_sec=timeout_sec,
        service_tier=service_tier,
        cancel_event=cancel_event,
    )


def generate_structured_text(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    service_tier: str = DEFAULT_SERVICE_TIER,
) -> str:
    """Generate schema-constrained JSON from text-only input."""

    text, _tokens = _call_gemini_parts(
        model=model,
        api_key=api_key,
        parts=[{"text": prompt}],
        response_schema=response_schema,
        timeout_sec=timeout_sec,
        service_tier=service_tier,
    )
    return text


def extract_pdf_effects(
    *,
    pdf_path: Path,
    manual: CodingManual,
    rows: list[CodingSheetRow],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cancel_event: threading.Event | None = None,
) -> ExtractionResult:
    source_pdf = pdf_path.name
    requested_ids = {row.row_id for row in rows}
    started = time.monotonic()
    response_schema = build_response_schema(manual)
    prompt = _build_prompt(manual, rows)

    try:
        raw_text, tokens = _call_gemini(
            model=model,
            api_key=api_key,
            prompt=prompt,
            pdf_bytes=pdf_path.read_bytes(),
            response_schema=response_schema,
            timeout_sec=timeout_sec,
            service_tier=service_tier,
            cancel_event=cancel_event,
        )
    except ProviderError as exc:
        return ExtractionResult(
            source_pdf=source_pdf,
            status="cancelled" if isinstance(exc, ExtractionCancelled) else "error",
            error=str(exc),
            raw_response=exc.raw_response,
            duration_sec=time.monotonic() - started,
        )

    try:
        parsed, repaired_response = parse_json_response(raw_text)
    except ProviderError as exc:
        return ExtractionResult(
            source_pdf=source_pdf,
            status="error",
            error=str(exc),
            raw_response=raw_text,
            duration_sec=time.monotonic() - started,
        )

    result: ValidationResult = validate_response(parsed, requested_ids, manual.effects)
    duration = time.monotonic() - started
    if not result.ok:
        return ExtractionResult(
            source_pdf=source_pdf,
            status="needs_review",
            coded_by_row_id=result.coded_by_row_id,
            missing_ids=result.missing_ids,
            extra_ids=result.extra_ids,
            error=result.error,
            raw_response=raw_text,
            repaired_response=repaired_response,
            duration_sec=duration,
            input_tokens=tokens.get("input_tokens"),
            output_tokens=tokens.get("output_tokens"),
            served_model=tokens.get("served_model"),
        )

    return ExtractionResult(
        source_pdf=source_pdf,
        status="ok",
        coded_by_row_id=result.coded_by_row_id,
        raw_response=raw_text,
        repaired_response=repaired_response,
        duration_sec=duration,
        input_tokens=tokens.get("input_tokens"),
        output_tokens=tokens.get("output_tokens"),
        served_model=tokens.get("served_model"),
    )
