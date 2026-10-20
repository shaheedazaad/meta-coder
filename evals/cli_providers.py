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

Each CLI gets the article the way its vendor's API would take a PDF, and no
tools:

- Claude Code gets the PDF itself, as a document block in a `stream-json`
  input message. That is the block the Claude API uses for PDFs.
- Codex accepts images but no PDF. An API that takes a PDF gives the model
  each page's text and an image of the page, so Codex gets the text layer in
  the prompt (when the PDF has one) and the pages as images, rendered with
  poppler's `pdftoppm`.

Codex needs more than settings to send nothing but our text: it runs in a
temporary home that holds only a link to the login, because it reads the
user's global AGENTS.md from its home, and with a copy of the model's catalog
entry that lists no tools. Each result records the CLI version, since what a
CLI adds can change between versions.
"""

from __future__ import annotations

import base64
import json
import os
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

PAGE_IMAGE_DPI = 150
TEXT_LAYER_HEADING = "\n\nText layer of the article (the pages show the same content with its layout):\n"
CLAUDE_SYSTEM_PROMPT = (
    "You code research articles for a meta-analysis. Work only from the "
    "attached PDF and return only the requested structured output."
)
# Without this, Claude Code sends the prompt a second time, to a small model
# that names the session.
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
CODEX_INSTRUCTIONS = (
    "You code research articles for a meta-analysis. Work only from the "
    "attached page images and the article text in the message, and return "
    "only the requested JSON."
)
# Codex has no single switch for "no agent prompt"; each part it adds around
# ours has its own. These settings remove the instruction blocks, and
# `_codex_home` and `_codex_catalog` remove what no setting covers.
CODEX_OVERRIDES = (
    "approval_policy='never'",
    "mcp_servers={}",
    "agents.enabled=false",
    "web_search='disabled'",
    "project_doc_max_bytes=0",
    "memories.generate_memories=false",
    "memories.use_memories=false",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "include_collaboration_mode_instructions=false",
    "tools.experimental_request_user_input.enabled=false",
)
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "code_mode", "code_mode_only", "plugins", "apps",
    "recommended_plugins", "skill_search", "tool_suggest", "remote_plugin", "plugin_sharing",
    "goals", "multi_agent", "multi_agent_v2", "image_generation", "view_image", "sleep_tool",
    "current_time_reminder", "browser_use", "browser_use_external", "computer_use",
    "in_app_browser", "collaboration_modes", "hooks", "workspace_dependencies", "worktrees",
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


def _text_layer(pdf_path: Path) -> str:
    """The article section of the prompt; empty for a PDF without a text layer."""

    try:
        return TEXT_LAYER_HEADING + pdf_text(pdf_path, None)
    except ProviderError:
        return ""


def _page_images(pdf_path: Path, workspace: Path) -> list[Path]:
    try:
        done = subprocess.run(
            ["pdftoppm", "-r", str(PAGE_IMAGE_DPI), "-jpeg", str(pdf_path), "page"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=workspace,
        )
    except FileNotFoundError as exc:
        raise ProviderError(
            "codex_cli needs `pdftoppm` (part of poppler) on PATH to turn PDF pages into images."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("`pdftoppm` did not finish rendering the PDF.") from exc
    pages = sorted(workspace.glob("page-*.jpg"))
    if done.returncode != 0 or not pages:
        raise ProviderError(f"`pdftoppm` could not render the PDF: {done.stderr.strip()[-300:]}")
    return pages


def _codex_home(workspace: Path) -> Path:
    """A Codex home that holds nothing but the login.

    Codex reads the user's global AGENTS.md from its home folder and has no
    setting to leave it out. The login is linked, not copied: Codex rewrites
    `auth.json` in place when it renews the login, so the renewed one lands
    in the user's real file.
    """

    login = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not login.is_file():
        raise ProviderError(
            f"codex_cli needs a Codex login stored in {login} (run `codex login`; "
            "a login kept in the system keyring cannot be used)."
        )
    home = workspace / "codex-home"
    home.mkdir()
    try:
        (home / "auth.json").symlink_to(login)
    except OSError as exc:
        raise ProviderError(f"Could not link the Codex login into a temporary folder: {exc}") from exc
    return home


def _codex_catalog(model: str, workspace: Path) -> Path:
    """The model's own catalog entry with its tools removed.

    The catalog, not a setting, decides which tool definitions Codex sends
    with every request, so the entry is copied from `codex debug models` and
    only its three tool fields are changed.
    """

    try:
        catalog = json.loads(_run(["codex", "debug", "models"], "", workspace))
    except json.JSONDecodeError as exc:
        raise ProviderError("`codex debug models` did not return JSON.") from exc
    models = catalog.get("models") if isinstance(catalog, dict) else catalog
    entry = next(
        (item for item in models or [] if isinstance(item, dict) and item.get("slug") == model), None,
    )
    if entry is None:
        raise ProviderError(f"Codex does not list a model named {model!r}.")
    entry = {**entry, "tool_mode": "direct", "apply_patch_tool_type": None, "experimental_supported_tools": []}
    path = workspace / "catalog.json"
    path.write_text(json.dumps({"models": [entry]}), encoding="utf-8")
    return path


def _run(command: list[str], prompt: str, workspace: Path, env: dict[str, str] | None = None) -> str:
    name = command[0]
    try:
        done = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_SEC, env={**clean_env(), **(env or {})}, cwd=workspace,
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
    message = {"type": "user", "message": {"role": "user", "content": [
        {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
        }},
        {"type": "text", "text": prompt},
    ]}}
    stdout = _run([
        "claude", "-p", "--safe-mode", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "", "--no-session-persistence",
        "--system-prompt", CLAUDE_SYSTEM_PROMPT, "--model", model,
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(schema),
    ], json.dumps(message) + "\n", workspace, env=CLAUDE_ENV)
    reply = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            reply = event
    if reply is None or reply.get("is_error") or reply.get("structured_output") is None:
        raise ProviderError("`claude` returned no structured output.", raw_response=stdout[-2000:])
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
    prompt = prompt + _text_layer(pdf_path)
    pages = _page_images(pdf_path, workspace)
    (workspace / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (workspace / "instructions.md").write_text(CODEX_INSTRUCTIONS, encoding="utf-8")
    command = [
        "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--strict-config", "--sandbox", "read-only", "--model", model,
    ]
    home = _codex_home(workspace)
    catalog = _codex_catalog(model, workspace)
    for override in (
        *CODEX_OVERRIDES, "model_instructions_file='instructions.md'", f"model_catalog_json='{catalog.name}'",
    ):
        command += ["-c", override]
    for feature in CODEX_DISABLED_FEATURES:
        command += ["--disable", feature]
    for page in pages:
        command += ["--image", page.name]
    command += ["--output-schema", "schema.json", "--output-last-message", "reply.json", "--json", "-"]
    events = _run(command, prompt, workspace, env={"CODEX_HOME": str(home)})
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
                call, article = _call_claude, "attached PDF"
            else:
                call, article = _call_codex, "the attached images, one per page of the article"
            raw_text, tokens = call(
                pdf_path, build_extraction_prompt(manual, rows, article=article), schema, model, Path(folder),
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
