"""Code a PDF through a signed-in coding-agent CLI instead of an API key.

`claude_cli` runs `claude -p` and `codex_cli` runs `codex exec`, so a run draws
on the Claude or ChatGPT plan the CLI is logged in to. Prompt, schema,
validation and quote check are the app's own; only the transport differs.

The approach follows coarse (https://github.com/Davidvandijcke/coarse), which
routes its review pipeline through the same CLIs: every call runs in an empty
temporary directory, with the CLI's tools, MCP servers, user configuration and
session saving turned off, and with API keys removed from the environment so
the CLI cannot switch to metered billing. Unlike coarse, the schema is passed
through each CLI's own structured-output option rather than in the prompt.

- Claude Code gets one tool, Read, confined to a directory that holds only the
  PDF, so it reads the article itself (scanned PDFs included).
- Codex has no tool that reads a PDF: with file access it improvises with
  whatever shell tools the machine has. So it gets the text layer in the
  prompt and no tools, and a PDF without a text layer ends as an error.

Both CLIs add their own prompt around ours, and that prompt changes between
CLI versions, so each result records the version that produced it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from meta_coder.coding_sheet import CodingSheetRow
from meta_coder.extraction import ExtractionResult, ProviderError, parse_json_response, review_issues
from meta_coder.manual import CodingManual
from meta_coder.mechanism import build_response_schema, validate_response
from meta_coder.openai_compatible import pdf_text
from meta_coder.prompts import build_extraction_prompt
from meta_coder.quote_check import annotate_quote_checks

PROVIDERS = ("claude_cli", "codex_cli")
BINARIES = {"claude_cli": "claude", "codex_cli": "codex"}
# Codex always gets a named model: left to the CLI's own default, a result
# would not say which model produced it.
DEFAULT_MODELS = {"claude_cli": "sonnet", "codex_cli": "gpt-6.1-sol"}
TIMEOUT_SEC = 900

# With one of these set, the CLI bills an API account or calls another
# endpoint instead of using the plan it is logged in to.
BILLING_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
)
# Set by a coding agent for its own session; a CLI started from inside one
# must not inherit them.
HOST_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID",
)

ARTICLE_NAME = "article.pdf"
CLAUDE_SYSTEM_PROMPT = (
    "You code research articles for a meta-analysis. Read article.pdf in full "
    "with the Read tool, then return only the requested structured output."
)
CODEX_INSTRUCTIONS = (
    "You code research articles for a meta-analysis. Work only from the "
    "article text in the message and return only the requested JSON."
)
CODEX_OVERRIDES = (
    "approval_policy='never'",
    "mcp_servers={}",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "agents.enabled=false",
    "web_search='disabled'",
    "project_doc_max_bytes=0",
    "memories.generate_memories=false",
    "memories.use_memories=false",
)


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in BILLING_ENV + HOST_ENV}


def cli_version(provider: str) -> str:
    """The installed CLI's version line, or "" when it cannot be run."""

    try:
        done = subprocess.run(
            [BINARIES[provider], "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, env=clean_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def _run(command: list[str], prompt: str, workspace: Path) -> str:
    name = command[0]
    try:
        done = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_SEC, env=clean_env(), cwd=workspace,
        )
    except FileNotFoundError as exc:
        raise ProviderError(f"`{name}` is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"`{name}` did not finish within {TIMEOUT_SEC} seconds.") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[-500:]
        raise ProviderError(
            f"`{name}` exited with status {done.returncode}: {detail} "
            f"(if this is a sign-in problem, log in with `{name} login`)."
        )
    return done.stdout


def _call_claude(
    pdf_path: Path, prompt: str, schema: dict, model: str, workspace: Path,
) -> tuple[str, dict[str, int | None]]:
    shutil.copyfile(pdf_path, workspace / ARTICLE_NAME)
    stdout = _run([
        "claude", "-p", "--safe-mode", "--restricted", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "Read", "--allowedTools", "Read", "--no-session-persistence",
        "--system-prompt", CLAUDE_SYSTEM_PROMPT, "--model", model,
        "--output-format", "json", "--json-schema", json.dumps(schema),
    ], prompt, workspace)
    try:
        reply = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError("`claude` did not return JSON.", raw_response=stdout) from exc
    if not isinstance(reply, dict) or reply.get("is_error") or reply.get("structured_output") is None:
        raise ProviderError("`claude` returned no structured output.", raw_response=stdout)
    usage = reply.get("usage") or {}
    read = [usage.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    tokens = {
        "input_tokens": None if all(count is None for count in read) else sum(count or 0 for count in read),
        "output_tokens": usage.get("output_tokens"),
    }
    return json.dumps(reply["structured_output"], ensure_ascii=False), tokens


def _call_codex(
    pdf_path: Path, prompt: str, schema: dict, model: str, workspace: Path,
) -> tuple[str, dict[str, int | None]]:
    prompt = prompt + "\n\nArticle text:\n" + pdf_text(pdf_path, None)
    (workspace / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (workspace / "instructions.md").write_text(CODEX_INSTRUCTIONS, encoding="utf-8")
    command = [
        "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--strict-config", "--sandbox", "read-only", "--model", model,
    ]
    for override in (*CODEX_OVERRIDES, "model_instructions_file='instructions.md'"):
        command += ["-c", override]
    command += ["--output-schema", "schema.json", "--output-last-message", "reply.json", "--json", "-"]
    events = _run(command, prompt, workspace)
    reply = workspace / "reply.json"
    if not reply.is_file():
        raise ProviderError("`codex` returned no final message.", raw_response=events)
    tokens: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            tokens = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
    return reply.read_text(encoding="utf-8"), tokens


def extract_pdf_effects(
    *, provider: str, pdf_path: Path, manual: CodingManual, rows: list[CodingSheetRow], model: str = "",
) -> ExtractionResult:
    """Counterpart of `meta_coder.providers.extract_pdf_effects` for the CLIs."""

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r} (use one of {PROVIDERS}).")
    source_pdf = pdf_path.name
    started = time.monotonic()
    schema = build_response_schema(manual, dialect="json_schema")
    model = model or DEFAULT_MODELS[provider]
    try:
        with tempfile.TemporaryDirectory(prefix="meta-coder-eval-") as folder:
            if provider == "claude_cli":
                article = f"the PDF file {ARTICLE_NAME} in the current directory"
                raw_text, tokens = _call_claude(
                    pdf_path, build_extraction_prompt(manual, rows, article=article), schema, model, Path(folder),
                )
            else:
                raw_text, tokens = _call_codex(
                    pdf_path, build_extraction_prompt(manual, rows, article="article text below"),
                    schema, model, Path(folder),
                )
        parsed, repaired = parse_json_response(raw_text)
    except ProviderError as exc:
        return ExtractionResult(
            source_pdf=source_pdf, status="error", error=str(exc),
            raw_response=exc.raw_response, duration_sec=time.monotonic() - started,
        )
    checked = validate_response(
        parsed, {row.row_id for row in rows}, manual.effects, confidence=manual.confidence
    )
    issues = review_issues(repaired_response=repaired, finish_reason=None)
    result = ExtractionResult(
        source_pdf=source_pdf,
        status="ok" if checked.ok and not issues else "needs_review",
        coded_by_row_id=checked.coded_by_row_id,
        missing_ids=checked.missing_ids,
        extra_ids=checked.extra_ids,
        raw_response=raw_text,
        repaired_response=repaired,
        error=" ".join(filter(None, [*issues, checked.error])) or None,
        duration_sec=time.monotonic() - started,
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
    )
    annotate_quote_checks(result.coded_by_row_id, pdf_path)
    return result
