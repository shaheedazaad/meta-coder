"""Provider-agnostic result/error types shared by every LLM adapter (gemini.py,
openrouter.py). Kept separate from either adapter so neither module has to
import from the other, and so `runner.py`/`providers.py` can depend on one
stable shape regardless of which provider actually ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import json
from typing import Any
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, raw_response: str | None = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class ExtractionCancelled(ProviderError):
    """Raised when a provider request is interrupted by the user."""


def parse_json_response(raw_text: str) -> tuple[object, str | None]:
    """Parse provider JSON, repairing common truncated/malformed responses."""

    try:
        return json.loads(raw_text), None
    except json.JSONDecodeError as original_exc:
        try:
            from json_repair import repair_json
            repaired_text = repair_json(raw_text)
            return json.loads(repaired_text), repaired_text
        except (ImportError, TypeError, ValueError, json.JSONDecodeError) as repair_exc:
            raise ProviderError(
                f"Provider returned invalid JSON and repair failed: {original_exc}",
                raw_response=raw_text,
            ) from repair_exc


# Finish reasons meaning the model ended its answer on its own. Gemini's "STOP"
# and OpenAI/OpenRouter's "stop" normalise to "stop"; "eos_token"/"stop_sequence"
# are what some self-hosted OpenAI-compatible servers (e.g. TGI) report instead.
NORMAL_FINISH_REASONS = {"stop", "eos_token", "stop_sequence"}
# Provider-specific spellings mapped onto the OpenAI vocabulary. Gemini's
# unspecified value carries no information, so it counts as absent.
FINISH_REASON_ALIASES = {"max_tokens": "length", "finish_reason_unspecified": None}


def normalize_finish_reason(value: object) -> str | None:
    """One lower-case vocabulary for Gemini `finishReason` and OpenAI-style
    `finish_reason` values; None when the provider did not report one."""

    if value is None:
        return None
    reason = str(value).strip().lower()
    if not reason:
        return None
    return FINISH_REASON_ALIASES.get(reason, reason)


def review_issues(*, repaired_response: str | None, finish_reason: str | None) -> list[str]:
    """Reasons to hold an otherwise valid response for human review.

    A response cut off at the token limit can still be repaired into JSON that
    passes validation (e.g. `0.125` truncated to `0.12`), so neither a repair
    nor an abnormal finish is ever accepted as "ok". A missing finish reason is
    not treated as abnormal: some OpenAI-compatible servers never report one.
    """

    issues = []
    if finish_reason is not None and finish_reason not in NORMAL_FINISH_REASONS:
        issues.append(
            f"Provider stopped generating early (finish reason: {finish_reason}); "
            "the response may be truncated or incomplete."
        )
    if repaired_response is not None:
        issues.append(
            "Provider returned malformed JSON that was automatically repaired; "
            "check the coded values against the raw response."
        )
    return issues


class ResponseBytes(bytes):
    """Bytes with a small, safe subset of transport metadata for the audit log."""
    def __new__(cls, data, response):
        value = super().__new__(cls, data)
        value.status = getattr(response, 'status', None)
        headers = getattr(response, 'headers', {}) or {}
        value.audit_headers = {key: str(val) for key, val in headers.items() if key.lower() in {
            'content-type', 'date', 'x-request-id', 'request-id', 'x-goog-request-id', 'retry-after',
        }}
        return value


def cancellable_urlopen(
    request: Request, *, timeout: int, cancel_event: threading.Event
) -> bytes:
    """Read one urllib response while allowing the runner to close it."""

    result: queue.Queue[tuple[bytes | None, BaseException | None]] = queue.Queue(maxsize=1)
    response_holder: list[Any] = []

    def fetch() -> None:
        try:
            response = urlopen(request, timeout=timeout)
            response_holder.append(response)
            try:
                result.put((ResponseBytes(response.read(), response), None))
            finally:
                response.close()
        except BaseException as exc:  # pass provider/network errors to caller
            result.put((None, exc))

    threading.Thread(target=fetch, daemon=True).start()
    while True:
        if cancel_event.is_set():
            if response_holder:
                response_holder[0].close()
            raise ExtractionCancelled("Extraction cancelled by user.")
        try:
            body, error = result.get(timeout=0.05)
        except queue.Empty:
            continue
        if error is not None:
            if cancel_event.is_set():
                raise ExtractionCancelled("Extraction cancelled by user.")
            raise error
        return body if body is not None else b""


@dataclass
class ExtractionResult:
    source_pdf: str
    status: str  # "ok" | "needs_review" | "error" | "cancelled"
    coded_by_row_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_ids: set[str] = field(default_factory=set)
    extra_ids: set[str] = field(default_factory=set)
    raw_response: str | None = None
    repaired_response: str | None = None
    error: str | None = None
    duration_sec: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    audit_operation_id: str | None = None
