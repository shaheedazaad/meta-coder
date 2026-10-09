"""OpenRouter provider adapter (plan.md Part A4, todo.md step 17) — a backup path
to Gemini's, routing through whatever underlying model the user names.

Uses the OpenAI-compatible chat completions shape OpenRouter exposes: PDFs go in
as a `file` content part (base64 data URL), and structured output is requested via
`response_format: {type: "json_schema", ...}` — see `mechanism.build_response_schema`
called with `dialect="json_schema"` and `strict: true`. Every manual field is
required in each returned row; `value: null` explicitly means the paper did not
report it, rather than letting a provider silently omit the field.

Unlike Gemini's adapter, this one retries transient failures (connection errors,
429, 5xx) with bounded exponential backoff — OpenRouter fans a single request out
to one of several possible backend providers per model, so a transient failure at
one of them is more common here than hitting Gemini's API directly.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
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
from .prompts import build_extraction_prompt


API_BASE = "https://openrouter.ai/api/v1"
# The model field is free text; use `list_models`/`check_model` to verify a
# different model ID before saving it.
DEFAULT_MODEL = "google/gemini-3.7-flash"
DEFAULT_TIMEOUT_SEC = 600
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 2.0
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "[redacted]") if api_key else text


def _request(url: str, *, api_key: str, body: dict[str, Any] | None = None, method: str = "GET"):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def list_models(*, api_key: str = "", timeout_sec: int = 20) -> list[dict[str, Any]]:
    """GET /models — public, doesn't require a key, but one is sent if given (it's
    harmless and matches how every other request to this API is made)."""

    request = _request(f"{API_BASE}/models", api_key=api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach OpenRouter: {exc}") from exc
    return body.get("data") or []


def check_model(model: str, *, api_key: str = "", timeout_sec: int = 20) -> list[str]:
    """Live validation (todo.md step 17): fetch OpenRouter's model list and report
    anything about `model` that would undermine this app's mechanism — never
    raises for a network hiccup, since that shouldn't block using a model you
    already know works; it just can't confirm anything this time."""

    try:
        models = list_models(api_key=api_key, timeout_sec=timeout_sec)
    except ProviderError as exc:
        return [f"Could not check this model right now: {exc}"]

    entry = next((m for m in models if m.get("id") == model), None)
    if entry is None:
        return [f"`{model}` was not found in OpenRouter's current model list — check the exact slug."]

    problems = []
    supported = set(entry.get("supported_parameters") or [])
    if "structured_outputs" not in supported:
        problems.append(
            "This model isn't listed as supporting structured outputs — coded values may "
            "come back malformed or fail to parse, since this app relies on schema-constrained "
            "JSON, not prompt-only formatting instructions."
        )
    modalities = set((entry.get("architecture") or {}).get("input_modalities") or [])
    if "file" not in modalities:
        problems.append(
            "This model has no native file/PDF input — OpenRouter will use its free "
            "Cloudflare AI parser to convert the PDF to text/Markdown. Information conveyed "
            "only visually — figures or unusually laid-out tables — may be lost."
        )
    return problems


def _call_openrouter_content(
    *,
    model: str,
    api_key: str,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    reasoning_effort: str = "",
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, int | None]]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "meta_coder_effects", "strict": True, "schema": response_schema},
        },
        "temperature": 0,
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    last_error: ProviderError | None = None
    raw_response = ""
    for attempt in range(1, MAX_RETRIES + 1):
        request = _request(
            f"{API_BASE}/chat/completions", api_key=api_key, body=payload, method="POST"
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
                    f"OpenRouter returned invalid response JSON: {exc}", raw_response=raw_response
                ) from exc
            break
        except urllib.error.HTTPError as exc:
            if cancel_event and cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.") from exc
            detail = _redact(exc.read().decode("utf-8", errors="replace"), api_key)
            last_error = ProviderError(f"OpenRouter API error ({exc.code}): {detail}")
            if exc.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES:
                raise last_error from exc
        except urllib.error.URLError as exc:
            if cancel_event and cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.") from exc
            last_error = ProviderError(f"Could not reach OpenRouter: {_redact(str(exc.reason), api_key)}")
            if attempt == MAX_RETRIES or (cancel_event and cancel_event.is_set()):
                raise last_error from exc
        if cancel_event and cancel_event.wait(RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))):
            raise ExtractionCancelled("Extraction cancelled by user.")
    else:  # pragma: no cover - loop always breaks or raises above
        raise last_error or ProviderError("OpenRouter request failed for an unknown reason.")

    if not isinstance(body, dict):
        raise ProviderError("OpenRouter returned an invalid response object.", raw_response=raw_response)
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise ProviderError("OpenRouter returned no choices.", raw_response=raw_response)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError("OpenRouter returned an invalid choice.", raw_response=raw_response)
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ProviderError("OpenRouter returned an invalid message.", raw_response=raw_response)
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("OpenRouter returned no message content.", raw_response=raw_response)

    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    tokens = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    return text, tokens


def _call_openrouter(
    *,
    model: str,
    api_key: str,
    prompt: str,
    pdf_bytes: bytes,
    filename: str,
    response_schema: dict[str, Any],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    reasoning_effort: str = "",
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, int | None]]:
    """Structured generation with a PDF file part, kept as the extraction seam."""

    return _call_openrouter_content(
        model=model,
        api_key=api_key,
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "file",
                "file": {
                    "filename": filename,
                    "file_data": "data:application/pdf;base64,"
                    + base64.b64encode(pdf_bytes).decode("ascii"),
                },
            },
        ],
        response_schema=response_schema,
        timeout_sec=timeout_sec,
        reasoning_effort=reasoning_effort,
        cancel_event=cancel_event,
    )


def generate_structured_text(
    *,
    prompt: str,
    response_schema: dict[str, Any],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    reasoning_effort: str = "",
) -> str:
    """Generate schema-constrained JSON from text-only input."""

    text, _tokens = _call_openrouter_content(
        model=model,
        api_key=api_key,
        content=[{"type": "text", "text": prompt}],
        response_schema=response_schema,
        timeout_sec=timeout_sec,
        reasoning_effort=reasoning_effort,
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
    reasoning_effort: str = "",
    cancel_event: threading.Event | None = None,
) -> ExtractionResult:
    source_pdf = pdf_path.name
    requested_ids = {row.row_id for row in rows}
    started = time.monotonic()
    response_schema = build_response_schema(manual, dialect="json_schema")
    prompt = build_extraction_prompt(manual, rows)

    try:
        raw_text, tokens = _call_openrouter(
            model=model,
            api_key=api_key,
            prompt=prompt,
            pdf_bytes=pdf_path.read_bytes(),
            filename=source_pdf,
            response_schema=response_schema,
            timeout_sec=timeout_sec,
            reasoning_effort=reasoning_effort,
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
    )
