"""The subscription CLI providers (`claude -p`, `codex exec`) code a PDF through
a fake `subprocess.run`, so no CLI is started and no network is used. The
saved Kearney response stands in for what the CLIs return."""

import base64
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import cli_providers  # noqa: E402
import run_eval  # noqa: E402
from meta_coder.extraction import ExtractionResult  # noqa: E402
from meta_coder.manual import read_coding_manual  # noqa: E402

BASELINE = EVALS / "baselines" / "gemini-3.7-flash-small" / "kearney_2022.json"
USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 7}


def _baseline_response():
    return json.loads(json.loads(BASELINE.read_text(encoding="utf-8"))["raw_response"])


@pytest.fixture
def manual():
    return read_coding_manual(EVALS / "manual.yml")


@pytest.fixture
def kearney(manual):
    pdf = EVALS / "pdfs" / "kearney_2022.pdf"
    return pdf, run_eval.rows_by_pdf("small")["kearney_2022.pdf"]


def _fake_run(monkeypatch, respond):
    calls = []

    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return respond(command, kwargs)

    monkeypatch.setattr(cli_providers.subprocess, "run", fake)
    return calls


def _claude_stdout(structured=None, usage=USAGE):
    body = {"type": "result", "is_error": False,
            "structured_output": _baseline_response() if structured is None else structured}
    if usage is not None:
        body["usage"] = usage
    return "\n".join([json.dumps({"type": "system"}), "this line is not JSON", json.dumps(body)])


def _codex_events(usage=None):
    lines = [json.dumps({"type": "thread.started"}), "this line is not JSON"]
    lines.append(json.dumps({"type": "turn.completed", "usage": usage or {"input_tokens": 11, "output_tokens": 5}}))
    return "\n".join(lines)


def test_clean_env_drops_billing_and_host_variables_and_keeps_the_rest(monkeypatch):
    dropped = cli_providers.BILLING_ENV + cli_providers.HOST_ENV
    for name in dropped:
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("MC_EVAL_KEEP_ME", "kept")
    env = cli_providers.clean_env()
    assert not set(env) & set(dropped)
    assert env["MC_EVAL_KEEP_ME"] == "kept"
    assert env["PATH"] == os.environ["PATH"]


def test_cli_version_returns_the_stripped_stdout(monkeypatch):
    _fake_run(monkeypatch, lambda command, kwargs: subprocess.CompletedProcess(command, 0, "  2.1.0 (Claude Code)\n", ""))
    assert cli_providers.cli_version("claude_cli") == "2.1.0 (Claude Code)"


def test_cli_version_is_empty_when_the_cli_cannot_run(monkeypatch):
    def missing(command, kwargs):
        raise OSError("no such file")

    _fake_run(monkeypatch, missing)
    assert cli_providers.cli_version("codex_cli") == ""


def test_claude_cli_sends_the_pdf_as_a_document_block_with_no_tools(monkeypatch, manual, kearney):
    pdf, rows = kearney
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = {}

    def respond(command, kwargs):
        seen["command"] = command
        seen["files"] = sorted(path.name for path in Path(kwargs["cwd"]).iterdir())
        seen["env"] = kwargs["env"]
        seen["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, _claude_stdout(), "")

    _fake_run(monkeypatch, respond)
    result = cli_providers.extract_pdf_effects(provider="claude_cli", pdf_path=pdf, manual=manual, rows=rows)

    assert result.status == "ok"
    assert result.input_tokens == 152
    assert result.output_tokens == 7
    quoted = [cell for coded in result.coded_by_row_id.values() for cell in coded.values()
              if isinstance(cell, dict) and cell.get("quote")]
    assert quoted and all("quote_check" in cell for cell in quoted)

    command = seen["command"]
    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert "--system-prompt" in command
    assert "--json-schema" in command
    assert seen["files"] == []
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["env"]["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    document, text = json.loads(seen["input"])["message"]["content"]
    assert document["type"] == "document"
    assert base64.b64decode(document["source"]["data"]) == pdf.read_bytes()
    assert "attached PDF" in text["text"]


@pytest.mark.parametrize(("reply", "expected"), [
    (subprocess.CompletedProcess(["claude"], 1, "", "boom: not signed in"), "not signed in"),
    (subprocess.CompletedProcess(["claude"], 0, "<html>not json</html>", ""), "returned no structured output"),
    (subprocess.CompletedProcess(["claude"], 0, json.dumps(
        {"type": "result", "is_error": True, "structured_output": _baseline_response(), "usage": USAGE}), ""),
     "returned no structured output"),
    (subprocess.CompletedProcess(["claude"], 0, json.dumps({"type": "result", "is_error": False, "usage": USAGE}), ""),
     "returned no structured output"),
    (FileNotFoundError("claude"), "not installed or not on PATH"),
    (subprocess.TimeoutExpired(["claude"], 900), "did not finish within"),
], ids=["nonzero-exit", "not-json", "is-error", "no-structured-output", "not-installed", "timeout"])
def test_claude_cli_failures_become_errors_with_a_reason(monkeypatch, manual, kearney, reply, expected):
    pdf, rows = kearney

    def respond(command, kwargs):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    _fake_run(monkeypatch, respond)
    result = cli_providers.extract_pdf_effects(provider="claude_cli", pdf_path=pdf, manual=manual, rows=rows)
    assert result.status == "error"
    assert expected in result.error


def test_claude_cli_usage_without_token_counts_gives_none(monkeypatch, manual, kearney):
    pdf, rows = kearney
    _fake_run(monkeypatch, lambda command, kwargs: subprocess.CompletedProcess(
        command, 0, _claude_stdout(usage={}), ""))
    result = cli_providers.extract_pdf_effects(provider="claude_cli", pdf_path=pdf, manual=manual, rows=rows)
    assert result.status == "ok"
    assert result.input_tokens is None


def test_claude_cli_response_missing_a_requested_row_needs_review(monkeypatch, manual, kearney):
    pdf, rows = kearney
    response = _baseline_response()
    dropped = response["effects"].pop()["row_id"]
    _fake_run(monkeypatch, lambda command, kwargs: subprocess.CompletedProcess(
        command, 0, _claude_stdout(structured=response), ""))
    result = cli_providers.extract_pdf_effects(provider="claude_cli", pdf_path=pdf, manual=manual, rows=rows)
    assert result.status == "needs_review"
    assert dropped in result.missing_ids


CATALOG = {"models": [
    {"slug": "gpt-6.1-sol", "tool_mode": "code_mode_only", "apply_patch_tool_type": "freeform",
     "experimental_supported_tools": ["clock"], "context_window": 1000},
    {"slug": "X", "tool_mode": "code_mode_only"},
]}


@pytest.fixture
def codex_login(monkeypatch, tmp_path):
    (tmp_path / "auth.json").write_text('{"login": true}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return tmp_path / "auth.json"


def _codex_run(monkeypatch, manual, rows, pdf, model="", write_reply=True, pages=2):
    seen = {}

    def respond(command, kwargs):
        workspace = Path(kwargs["cwd"])
        if command[:3] == ["codex", "debug", "models"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(CATALOG), "")
        if command[0] == "pdftoppm":
            seen["pdftoppm"] = command
            for number in range(1, pages + 1):
                (workspace / f"page-{number}.jpg").write_bytes(b"jpeg")
            return subprocess.CompletedProcess(command, 0, "", "no pages" if not pages else "")
        seen["command"] = command
        seen["files"] = sorted(path.name for path in workspace.iterdir())
        seen["input"] = kwargs["input"]
        home = Path(kwargs["env"]["CODEX_HOME"])
        seen["home"] = {path.name: path.resolve() for path in home.iterdir()}
        seen["catalog"] = json.loads((workspace / "catalog.json").read_text(encoding="utf-8"))
        if write_reply:
            (workspace / "reply.json").write_text(json.dumps(_baseline_response()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, _codex_events(), "")

    _fake_run(monkeypatch, respond)
    result = cli_providers.extract_pdf_effects(
        provider="codex_cli", pdf_path=pdf, manual=manual, rows=rows, model=model,
    )
    return result, seen


def test_codex_cli_reads_the_reply_file_and_counts_tokens(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    result, seen = _codex_run(monkeypatch, manual, rows, pdf)

    assert result.status == "ok"
    assert result.input_tokens == 11
    assert result.output_tokens == 5

    command = seen["command"]
    assert command[:2] == ["codex", "exec"]
    assert "--output-schema" in command
    overrides = [command[i + 1] for i, part in enumerate(command) if part == "-c"]
    assert "model_instructions_file='instructions.md'" in overrides
    assert {"schema.json", "instructions.md"} <= set(seen["files"])
    assert seen["pdftoppm"][:4] == ["pdftoppm", "-r", "150", "-jpeg"]
    assert [command[i + 1] for i, part in enumerate(command) if part == "--image"] == ["page-1.jpg", "page-2.jpg"]
    assert "Text layer of the article" in seen["input"]


def test_codex_cli_runs_in_a_home_that_holds_only_the_login(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    _, seen = _codex_run(monkeypatch, manual, rows, pdf)
    assert seen["home"] == {"auth.json": codex_login}
    overrides = [seen["command"][i + 1] for i, part in enumerate(seen["command"]) if part == "-c"]
    assert "skills.include_instructions=false" in overrides


def test_codex_cli_sends_the_models_catalog_entry_without_tools(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    _, seen = _codex_run(monkeypatch, manual, rows, pdf)
    assert seen["catalog"] == {"models": [{
        "slug": "gpt-6.1-sol", "tool_mode": "direct", "apply_patch_tool_type": None,
        "experimental_supported_tools": [], "context_window": 1000,
    }]}
    overrides = [seen["command"][i + 1] for i, part in enumerate(seen["command"]) if part == "-c"]
    assert "model_catalog_json='catalog.json'" in overrides


def test_codex_cli_is_an_error_for_a_model_codex_does_not_list(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    result, seen = _codex_run(monkeypatch, manual, rows, pdf, model="no-such-model")
    assert result.status == "error"
    assert "does not list a model" in result.error
    assert "command" not in seen


def test_codex_cli_is_an_error_without_a_login_file(monkeypatch, manual, kearney, tmp_path):
    pdf, rows = kearney
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    result, seen = _codex_run(monkeypatch, manual, rows, pdf)
    assert result.status == "error"
    assert "codex login" in result.error
    assert "command" not in seen


def test_codex_cli_always_names_a_model(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    _, seen = _codex_run(monkeypatch, manual, rows, pdf, model="")
    assert seen["command"][seen["command"].index("--model") + 1] == "gpt-6.1-sol"


def test_codex_cli_passes_the_requested_model(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    _, seen = _codex_run(monkeypatch, manual, rows, pdf, model="X")
    assert seen["command"][seen["command"].index("--model") + 1] == "X"


def test_codex_cli_without_a_reply_file_is_an_error(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    result, _ = _codex_run(monkeypatch, manual, rows, pdf, write_reply=False)
    assert result.status == "error"
    assert "returned no final message" in result.error


def test_codex_cli_sends_only_page_images_for_a_pdf_without_a_text_layer(monkeypatch, manual, codex_login):
    pdf = EVALS / "pdfs" / "xia_2022_scanned.pdf"
    rows = run_eval.rows_by_pdf("small")["xia_2022_scanned.pdf"]
    _, seen = _codex_run(monkeypatch, manual, rows, pdf)
    assert "--image" in seen["command"]
    assert "Text layer of the article" not in seen["input"]


def test_codex_cli_is_an_error_when_no_page_image_is_rendered(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney
    result, seen = _codex_run(monkeypatch, manual, rows, pdf, pages=0)
    assert result.status == "error"
    assert "could not render" in result.error
    assert "command" not in seen


def test_codex_cli_is_an_error_without_pdftoppm(monkeypatch, manual, kearney, codex_login):
    pdf, rows = kearney

    def respond(command, kwargs):
        raise FileNotFoundError(command[0])

    _fake_run(monkeypatch, respond)
    result = cli_providers.extract_pdf_effects(provider="codex_cli", pdf_path=pdf, manual=manual, rows=rows)
    assert result.status == "error"
    assert "poppler" in result.error


def test_unknown_provider_is_a_value_error(manual, kearney):
    pdf, rows = kearney
    with pytest.raises(ValueError, match="Unknown provider"):
        cli_providers.extract_pdf_effects(provider="gemini", pdf_path=pdf, manual=manual, rows=rows)


def test_run_eval_run_with_claude_cli_records_the_cli_version_and_needs_no_key(monkeypatch):
    for name in run_eval.KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli_providers, "cli_version", lambda provider: "9.9.9")
    monkeypatch.setattr(
        cli_providers, "extract_pdf_effects",
        lambda **kwargs: ExtractionResult(source_pdf=kwargs["pdf_path"].name, status="ok"),
    )
    name = f"pytest-cli-{uuid.uuid4().hex[:8]}"
    run_dir = run_eval.ROOT / "runs" / name
    try:
        code = run_eval.main(["run", "--set", "small", "--provider", "claude_cli", "--name", name])
        saved = json.loads((run_dir / "kearney_2022.json").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    assert code == 0
    assert saved["cli_version"] == "9.9.9"
    assert saved["provider"] == "claude_cli"
