import json
import time
from pathlib import Path

import pytest

from meta_coder import gemini, openai_compatible, openrouter
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project
from meta_coder.prompts import PROMPT_VERSION, build_extraction_prompt, render_codebook
from meta_coder.runner import Runner, raw_json_path


MANUAL = parse_coding_manual(
    """\
effect_definition: The relevant effect.
effects:
  estimate:
    type: number
"""
)

CODEBOOK_MANUAL = parse_coding_manual(
    """\
name: publication_bias
description: |-
  Coding for a meta-analysis of publication bias.
  Code the main text only.
effect_definition: The relevant effect.
effects:
  Publication status:
    type: string
    description: Whether the report was published.
    levels:
      - value: "1"
        description: Published in a peer-reviewed journal
      - value: "2"
        description: |-
          Unpublished report.
          Includes theses and preprints.
      - value: "3"
  Sample size:
    type: integer
    description: Number of participants contributing to this effect.
  Preregistered:
    type: boolean
"""
)


def test_blank_locator_instructs_the_model_to_use_the_single_effect_and_note_ambiguity():
    rows = [CodingSheetRow(row_id="r1", source_pdf="paper.pdf", locator="")]
    prompt = build_extraction_prompt(MANUAL, rows)
    assert "locator: (none)" in prompt
    assert "only one effect of interest" in prompt
    assert "`notes` field" in prompt
    assert "from the attached PDF" in prompt
    assert "from the article text below" in build_extraction_prompt(MANUAL, rows, article="article text below")


def test_codebook_renders_manual_description_fields_and_level_descriptions():
    assert render_codebook(CODEBOOK_MANUAL) == (
        "Coding manual:\n"
        "About this coding manual: Coding for a meta-analysis of publication bias.\n"
        "  Code the main text only.\n"
        "\n"
        "Fields to code for each row:\n"
        "- `Publication status` (categorical)\n"
        "  Description: Whether the report was published.\n"
        "  Levels:\n"
        "  - `1`: Published in a peer-reviewed journal\n"
        "  - `2`: Unpublished report.\n"
        "      Includes theses and preprints.\n"
        "  - `3`\n"
        "- `Sample size` (integer)\n"
        "  Description: Number of participants contributing to this effect.\n"
        "- `Preregistered` (boolean)\n"
        "- `notes` (string)\n"
        f"  Description: {CODEBOOK_MANUAL.effects['notes'].description}"
    )


def test_codebook_omits_absent_manual_description():
    assert render_codebook(MANUAL).startswith("Coding manual:\n\nFields to code for each row:\n- `estimate` (number)\n")


def test_prompt_rules_do_not_promise_multiple_levels():
    prompt = build_extraction_prompt(MANUAL, [])
    assert "multiple levels are valid" not in prompt
    assert "exactly one of its listed levels" in prompt


GEMINI_REPLY = {"candidates": [{"content": {"parts": [{"text": json.dumps({"effects": []})}]}}]}
CHAT_REPLY = {"choices": [{"message": {"content": json.dumps({"effects": []})}}]}


def _request_text(adapter, payload):
    if adapter is gemini:
        return payload["contents"][0]["parts"][0]["text"]
    if adapter is openrouter:
        return payload["messages"][0]["content"][0]["text"]
    return payload["messages"][0]["content"]


@pytest.mark.parametrize(
    "adapter, reply, kwargs",
    [
        (gemini, GEMINI_REPLY, {}),
        (openrouter, CHAT_REPLY, {}),
        (openai_compatible, CHAT_REPLY, {"model": "m", "base_url": "http://localhost/v1"}),
    ],
)
def test_every_adapter_sends_the_shared_prompt_with_codebook(adapter, reply, kwargs, tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    sent = []

    def respond(request, **_):
        sent.append(json.loads(request.data))
        return json.dumps(reply).encode()

    monkeypatch.setattr(adapter, "cancellable_urlopen", respond)
    monkeypatch.setattr(openai_compatible, "pdf_text", lambda path, event: "[Page 1]\nArticle body.")
    rows = [CodingSheetRow(row_id="r1", source_pdf="paper.pdf", locator="Study 1")]
    adapter.extract_pdf_effects(pdf_path=pdf, manual=CODEBOOK_MANUAL, rows=rows, api_key="k", **kwargs)

    text = _request_text(adapter, sent[0])
    assert "About this coding manual: Coding for a meta-analysis of publication bias." in text
    assert "- `1`: Published in a peer-reviewed journal" in text
    assert "- `2`: Unpublished report." in text
    if adapter is openai_compatible:
        expected = build_extraction_prompt(CODEBOOK_MANUAL, rows, article="article text below")
        assert text.startswith(expected + "\n\nArticle text:\n[Page 1]\nArticle body.")
    else:
        assert text == build_extraction_prompt(CODEBOOK_MANUAL, rows)


def test_prompt_version_is_recorded_in_raw_result_and_audit_settings(tmp_path, monkeypatch):
    project = create_project("Prompt version", root=tmp_path)
    (project.sources_dir / "paper.pdf").write_bytes(b"%PDF")
    reply = {"effects": [{"row_id": "r1", "estimate": {"value": 1, "evidence": "p. 1"}, "notes": {"value": ""}}]}
    monkeypatch.setattr(gemini, "cancellable_urlopen", lambda request, **_: json.dumps(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}).encode())
    runner = Runner()
    state = runner.start(
        project=project, manual=MANUAL, api_key="k", provider="gemini", model="m",
        coding_sheet=CodingSheet(rows=[CodingSheetRow(row_id="r1", source_pdf="paper.pdf", locator="")], issues=[]),
    )
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state.status == "complete"
    raw = json.loads(raw_json_path(project, "paper.pdf").read_text())
    assert raw["status"] == "ok" and raw["prompt_version"] == PROMPT_VERSION
    operations = [json.loads(path.read_text()) for path in Path(project.path, "audit/operations").glob("*/operation.json")]
    assert {op["operation"] for op in operations} == {"extraction_run", "extraction"}
    assert all(op["settings"]["prompt_version"] == PROMPT_VERSION for op in operations)
