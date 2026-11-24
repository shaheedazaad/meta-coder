"""OpenRouter provider adapter (plan.md Part A4, todo.md step 17) — a backup path
to Gemini's, routing through whatever underlying model the user names.

Uses the OpenAI-compatible chat completions shape OpenRouter exposes: PDFs go in
as a `file` content part (base64 data URL), XML sources as plain text, and structured output is requested via
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
import re
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
    normalize_finish_reason,
    parse_json_response,
    review_issues,
    XML_ARTICLE_HEADING,
    xml_article_text,
)
import threading
from .manual import CodingManual
from .mechanism import ValidationResult, build_response_schema, validate_response
from .prompts import build_extraction_prompt
from .sources import source_format


API_BASE = "https://openrouter.ai/api/v1"
# The model field is free text; use `list_models`/`check_model` to verify a
# different model ID before saving it.
DEFAULT_MODEL = "google/gemini-3.8-flash"
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
            "This model has no native file/PDF input — OpenRouter will send each PDF to its "
            "default parser, the third-party Mistral OCR service, which is charged per page "
            "on top of the model's token cost. Information conveyed only visually — figures "
            "or unusually laid-out tables — may be lost in the conversion."
        )
    return problems


def _completion_payload(
    *,
    model: str,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any],
    reasoning_effort: str = "",
) -> dict[str, Any]:
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
    return payload


def _post_completion(
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout_sec: int,
    cancel_event: threading.Event | None,
) -> tuple[Any, str]:
    """POST one chat completion through the audited transport, retrying transient
    failures; returns the decoded body and the raw response text."""

    last_error: ProviderError | None = None
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
                return json.loads(raw_response), raw_response
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"OpenRouter returned invalid response JSON: {exc}", raw_response=raw_response
                ) from exc
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
    raise last_error or ProviderError(  # pragma: no cover - loop always returns or raises above
        "OpenRouter request failed for an unknown reason."
    )


def _usage(body: Any) -> dict[str, Any]:
    usage = body.get("usage") if isinstance(body, dict) else None
    return usage if isinstance(usage, dict) else {}


def _completion_text(body: Any, raw_response: str) -> tuple[str, dict[str, Any]]:
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

    usage = _usage(body)
    # OpenAI-style completion_tokens already includes any reasoning tokens
    # (completion_tokens_details.reasoning_tokens is only a breakdown).
    served_model = body.get("model")
    tokens = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "served_model": served_model if isinstance(served_model, str) and served_model else None,
        "finish_reason": normalize_finish_reason(choice.get("finish_reason")),
    }
    return text, tokens


def _call_openrouter_content(
    *,
    model: str,
    api_key: str,
    content: list[dict[str, Any]],
    response_schema: dict[str, Any],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    reasoning_effort: str = "",
    cancel_event: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = _completion_payload(
        model=model, content=content, response_schema=response_schema, reasoning_effort=reasoning_effort
    )
    body, raw_response = _post_completion(
        payload, api_key=api_key, timeout_sec=timeout_sec, cancel_event=cancel_event
    )
    return _completion_text(body, raw_response)


# When a model has no native file input, OpenRouter converts the PDF with its
# file-parser plugin (by default the paid Mistral OCR engine) and returns the
# text it produced as a `file` annotation on the message. The parser can succeed
# yet return only the PDF's metadata and empty `### Page N` headings, or text for
# the first page only, so the model codes from the abstract or from nothing.
# Such a parse is retried once with this engine, which converts the PDF with
# Cloudflare Workers AI and has no parser charge.
FALLBACK_PDF_ENGINE = "cloudflare-ai"
# A parsed page with fewer non-whitespace characters than this counts as blank.
MIN_PARSED_PAGE_CHARS = 20
_PARSED_FILE_TAG = re.compile(r"^</?file\b[^>]*>$")
_PARSED_PAGE_HEADING = re.compile(r"^#{1,6}\s*Page\s+\d+$", re.IGNORECASE)


def _non_space(line: str) -> int:
    return len("".join(line.split()))


def parsed_page_chars(body: Any) -> list[int] | None:
    """Non-whitespace characters on each page of the PDF text OpenRouter's
    file-parser plugin returned, or None when the response carries no parser
    annotation (a model that read the PDF natively gets none).

    Pages are split on `### Page N` headings; parsed text without headings
    counts as one page. Text before the page contents is the PDF's metadata.
    """

    choices = body.get("choices") if isinstance(body, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    annotations = message.get("annotations")
    parts: list[str] = []
    found = False
    for annotation in annotations if isinstance(annotations, list) else []:
        file = annotation.get("file") if isinstance(annotation, dict) and annotation.get("type") == "file" else None
        content = file.get("content") if isinstance(file, dict) else None
        if not isinstance(content, list):
            continue
        found = True
        parts.extend(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    if not found:
        return None
    text = "\n".join(parts)
    _, contents_heading, contents = text.partition("## Contents")
    lines = [line.strip() for line in (contents if contents_heading else text).splitlines()]
    lines = [line for line in lines if line and not _PARSED_FILE_TAG.match(line)]
    if not any(_PARSED_PAGE_HEADING.match(line) for line in lines):
        return [sum(_non_space(line) for line in lines)]
    pages: list[int] = []
    for line in lines:
        if _PARSED_PAGE_HEADING.match(line):
            pages.append(0)
        elif pages:
            pages[-1] += _non_space(line)
    return pages


def _blank_pages(pages: list[int]) -> int:
    return sum(chars < MIN_PARSED_PAGE_CHARS for chars in pages)


def _poor_parse(pages: list[int] | None) -> bool:
    """No text at all, or no text for more than half of a multi-page PDF."""

    if pages is None:
        return False
    return sum(pages) == 0 or (len(pages) > 1 and _blank_pages(pages) * 2 > len(pages))


def _sum_tokens(*values: Any) -> int | None:
    counts = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return sum(counts) if counts else None


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
) -> tuple[str, dict[str, Any]]:
    """Structured generation with a PDF file part, kept as the extraction seam.

    A blank or mostly blank parse is sent once more with the fallback parser;
    that request goes through the same audited transport as the first. A parse
    that is still mostly blank adds a `parse_issue` to the returned token info,
    and one with no text at all is an error.
    """

    payload = _completion_payload(
        model=model,
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
        reasoning_effort=reasoning_effort,
    )
    send: dict[str, Any] = {"api_key": api_key, "timeout_sec": timeout_sec, "cancel_event": cancel_event}
    body, raw_response = _post_completion(payload, **send)
    pages = parsed_page_chars(body)
    first_usage: dict[str, Any] | None = None
    if _poor_parse(pages):
        first_usage = _usage(body)
        payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": FALLBACK_PDF_ENGINE}}]
        body, raw_response = _post_completion(payload, **send)
        pages = parsed_page_chars(body)
        if pages is not None and sum(pages) == 0:
            raise ProviderError(
                "OpenRouter's PDF parser returned no text for this PDF, including on a retry "
                f"with the {FALLBACK_PDF_ENGINE} parser, so the model could not read it.",
                raw_response=raw_response,
            )

    text, tokens = _completion_text(body, raw_response)
    if first_usage is not None:
        # Both requests are billed, so both count towards the reported usage.
        tokens["input_tokens"] = _sum_tokens(first_usage.get("prompt_tokens"), tokens["input_tokens"])
        tokens["output_tokens"] = _sum_tokens(first_usage.get("completion_tokens"), tokens["output_tokens"])
    if pages is not None and _poor_parse(pages):
        tokens["parse_issue"] = (
            f"PDF parser returned no text for {_blank_pages(pages)} of {len(pages)} pages; "
            "the model may have coded from part of the paper only."
        )
    return text, tokens


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
    """Code one PDF or XML source. A PDF is sent as a file part; an XML source
    as one text part after the prompt, so no file parser is involved."""

    source_pdf = pdf_path.name
    requested_ids = {row.row_id for row in rows}
    started = time.monotonic()
    response_schema = build_response_schema(manual, dialect="json_schema")
    is_xml = source_format(pdf_path) == "xml"
    prompt = build_extraction_prompt(
        manual, rows, article="article XML below" if is_xml else "attached PDF",
        source_format="xml" if is_xml else "pdf",
    )

    try:
        if is_xml:
            raw_text, tokens = _call_openrouter_content(
                model=model,
                api_key=api_key,
                content=[{"type": "text", "text": prompt + XML_ARTICLE_HEADING + xml_article_text(pdf_path)}],
                response_schema=response_schema,
                timeout_sec=timeout_sec,
                reasoning_effort=reasoning_effort,
                cancel_event=cancel_event,
            )
        else:
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

    result: ValidationResult = validate_response(
        parsed, requested_ids, manual.effects, confidence=manual.confidence
    )
    issues = review_issues(
        repaired_response=repaired_response, finish_reason=tokens.get("finish_reason")
    )
    if tokens.get("parse_issue"):
        issues.append(tokens["parse_issue"])
    duration = time.monotonic() - started
    if not result.ok or issues:
        return ExtractionResult(
            source_pdf=source_pdf,
            status="needs_review",
            coded_by_row_id=result.coded_by_row_id,
            missing_ids=result.missing_ids,
            extra_ids=result.extra_ids,
            error=" ".join(filter(None, [*issues, result.error])),
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
