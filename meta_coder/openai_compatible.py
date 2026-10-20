"""Chat Completions adapter for configurable hosted and local endpoints."""

from __future__ import annotations

import json
import time
import threading
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
from .manual import CodingManual
from .mechanism import ValidationResult, build_response_schema, validate_response
from .prompts import build_extraction_prompt

DEFAULT_MODEL = ""
DEFAULT_TIMEOUT_SEC = 600
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 2.0
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def normalize_base_url(value: str) -> str:
    from urllib.parse import urlsplit

    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in value):
        raise ValueError("Enter an HTTP(S) API base URL without credentials, query, or fragment.")
    if parsed.path.endswith("/chat/completions"):
        raise ValueError("Enter the API base URL (for example /v1), without /chat/completions.")
    return value


def check_model(model: str, *, api_key: str = "", base_url: str = "") -> list[str]:
    # Model catalog endpoints are optional; never assume OpenRouter metadata.
    try:
        normalize_base_url(base_url)
    except ValueError as exc:
        return [str(exc)]
    return [] if model.strip() else ["Enter the model ID served by your endpoint."]


def pdf_text(path: Path, cancel_event: threading.Event | None) -> str:
    from pypdf import PdfReader

    pages = []
    try:
        for number, page in enumerate(PdfReader(path).pages, 1):
            if cancel_event and cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.")
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[Page {number}]\n{text}")
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Could not extract PDF text: {exc}") from exc
    if not pages:
        raise ProviderError("PDF has no extractable text. Run OCR first or use a provider with native PDF input.")
    return "\n\n".join(pages)


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "[redacted]") if api_key else text


def _request(url: str, *, api_key: str, body: dict[str, Any] | None = None, method: str = "GET"):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def _call(
    *,
    model: str,
    api_key: str,
    prompt: str,
    base_url: str,
    response_format: str = "json_schema",
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
                "content": prompt + "\n\nReturn only JSON matching this schema:\n" + json.dumps(response_schema),
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "meta_coder_effects", "strict": True, "schema": response_schema},
        },
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    if response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif response_format == "none":
        payload.pop("response_format")
    elif response_format != "json_schema":
        raise ProviderError("Unknown JSON output mode.")
    try:
        base_url = normalize_base_url(base_url)
    except ValueError as exc:
        raise ProviderError(str(exc)) from exc
    if not model.strip():
        raise ProviderError("Enter the model ID served by your OpenAI-compatible endpoint.")
    cancel_event = cancel_event or threading.Event()
    last_error: ProviderError | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if cancel_event.is_set():
            raise ExtractionCancelled("Extraction cancelled by user.")
        request = _request(
            f"{base_url}/chat/completions", api_key=api_key, body=payload, method="POST"
        )
        try:
            raw_body = audited_transport(cancellable_urlopen,
                request, timeout=timeout_sec, cancel_event=cancel_event
            )
            body = json.loads(raw_body.decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if cancel_event and cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.") from exc
            detail = _redact(exc.read().decode("utf-8", errors="replace"), api_key)
            last_error = ProviderError(f"OpenAI-compatible endpoint API error ({exc.code}): {detail}")
            if exc.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES:
                raise last_error from exc
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("Endpoint returned invalid JSON.") from exc
        except (urllib.error.URLError, OSError) as exc:
            if cancel_event and cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.") from exc
            last_error = ProviderError(f"Could not reach OpenAI-compatible endpoint: {_redact(str(exc), api_key)}")
            if attempt == MAX_RETRIES or (cancel_event and cancel_event.is_set()):
                raise last_error from exc
        if cancel_event and cancel_event.wait(RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))):
            raise ExtractionCancelled("Extraction cancelled by user.")
    else:  # pragma: no cover - loop always breaks or raises above
        raise last_error or ProviderError("OpenAI-compatible endpoint request failed for an unknown reason.")

    raw_response = json.dumps(body)
    if not isinstance(body, dict):
        raise ProviderError("OpenAI-compatible endpoint returned an invalid response object.", raw_response=raw_response)
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise ProviderError("OpenAI-compatible endpoint returned no choices.", raw_response=raw_response)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderError("OpenAI-compatible endpoint returned an invalid choice.", raw_response=raw_response)
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ProviderError("OpenAI-compatible endpoint returned an invalid message.", raw_response=raw_response)
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("OpenAI-compatible endpoint returned no message content.", raw_response=raw_response)

    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    tokens = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }
    return text, tokens


def generate_structured_text(*, prompt: str, response_schema: dict[str, Any],
                             api_key: str, model: str, base_url: str,
                             timeout_sec: int = DEFAULT_TIMEOUT_SEC,
                             response_format: str = "json_schema", reasoning_effort: str = "") -> str:
    text, _ = _call(prompt=prompt, response_schema=response_schema, api_key=api_key,
                    model=model, base_url=base_url, timeout_sec=timeout_sec,
                    response_format=response_format, reasoning_effort=reasoning_effort)
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
    base_url: str,
    response_format: str = "json_schema",
    cancel_event: threading.Event | None = None,
) -> ExtractionResult:
    source_pdf = pdf_path.name
    requested_ids = {row.row_id for row in rows}
    started = time.monotonic()
    response_schema = build_response_schema(manual, dialect="json_schema")
    prompt = build_extraction_prompt(manual, rows, article="article text below")

    try:
        raw_text, tokens = _call(
            model=model,
            api_key=api_key,
            prompt=prompt + "\n\nArticle text:\n" + pdf_text(pdf_path, cancel_event),
            base_url=base_url,
            response_format=response_format,
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
