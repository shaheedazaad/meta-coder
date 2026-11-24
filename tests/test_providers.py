import json

import pytest

from meta_coder import gemini, openrouter, providers
from meta_coder.extraction import ProviderError
from meta_coder.prompts import schema_prompt


def test_check_model_dispatches_to_selected_provider(monkeypatch):
    monkeypatch.setattr(providers.gemini, "check_model", lambda model, **kwargs: [f"gemini:{model}"])
    monkeypatch.setattr(
        providers.openrouter, "check_model", lambda model, **kwargs: [f"openrouter:{model}"]
    )

    assert providers.check_model("gemini", "gemini-model", api_key="key") == ["gemini:gemini-model"]
    assert providers.check_model("openrouter", "router-model", api_key="key") == [
        "openrouter:router-model"
    ]


def test_check_model_rejects_unknown_provider():
    assert providers.check_model("unknown", "model", api_key="key") == ["Unknown provider: 'unknown'."]


@pytest.mark.parametrize(
    ("provider", "adapter", "expected_schema_type"),
    [
        ("gemini", providers.gemini, "OBJECT"),
        ("openrouter", providers.openrouter, "object"),
    ],
)
def test_manual_drafting_dispatches_and_validates(provider, adapter, expected_schema_type, monkeypatch):
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "name": "draft",
                "description": "",
                "effect_definition": "Difference between intervention and control",
                "effects": [
                    {
                        "name": "sample_size",
                        "type": "integer",
                        "description": "Number of participants",
                        "evidence_required": True,
                        "levels": [],
                    }
                ],
            }
        )

    monkeypatch.setattr(adapter, "generate_structured_text", fake_generate)
    manual = providers.draft_coding_manual(
        provider=provider,
        document_text="manual source",
        api_key="key",
        model="model",
    )

    assert captured["response_schema"]["type"] == expected_schema_type
    assert captured["api_key"] == "key"
    assert set(manual.effects) == {"sample_size", "notes"}


def test_openrouter_model_without_structured_outputs_is_rejected(monkeypatch):
    monkeypatch.setattr(
        openrouter,
        "list_models",
        lambda **_kwargs: [
            {
                "id": "example/no-schema",
                "supported_parameters": ["temperature"],
                "architecture": {"input_modalities": ["file"]},
            }
        ],
    )

    problems = openrouter.check_model("example/no-schema")

    assert any("structured outputs" in problem for problem in problems)


def test_openrouter_malformed_choice_is_a_provider_error(monkeypatch):
    monkeypatch.setattr(openrouter, "cancellable_urlopen", lambda *_args, **_kwargs: json.dumps({"choices": ["bad"]}).encode())

    with pytest.raises(ProviderError, match="invalid choice"):
        openrouter._call_openrouter(
            model="example/model",
            api_key="key",
            prompt="prompt",
            pdf_bytes=b"%PDF",
            filename="paper.pdf",
            response_schema={},
        )


def test_openrouter_requests_strict_json_schema(monkeypatch):
    captured = {}

    def fake_urlopen(request, **_kwargs):
        captured.update(json.loads(request.data.decode()))
        return json.dumps(
            {"choices": [{"message": {"content": "{\"effects\": []}"}}], "usage": {}}
        ).encode()

    monkeypatch.setattr(openrouter, "cancellable_urlopen", fake_urlopen)
    openrouter._call_openrouter(
        model="example/model",
        api_key="key",
        prompt="prompt",
        pdf_bytes=b"%PDF",
        filename="paper.pdf",
        response_schema={"type": "object"},
    )

    assert captured["response_format"]["json_schema"]["strict"] is True


def test_gemini_manual_draft_request_is_text_only_and_structured(monkeypatch):
    captured = {}

    def fake_urlopen(request, **_kwargs):
        captured.update(json.loads(request.data.decode()))
        return json.dumps(
            {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                "usageMetadata": {},
            }
        ).encode()

    monkeypatch.setattr(gemini, "cancellable_urlopen", fake_urlopen)
    gemini.generate_structured_text(
        prompt="manual text", response_schema={"type": "OBJECT"}, api_key="key"
    )

    assert captured["contents"][0]["parts"] == [{"text": "manual text"}]
    assert captured["generationConfig"]["responseSchema"] == {"type": "OBJECT"}


def test_openrouter_manual_draft_request_is_text_only_and_structured(monkeypatch):
    captured = {}

    def fake_urlopen(request, **_kwargs):
        captured.update(json.loads(request.data.decode()))
        return json.dumps(
            {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        ).encode()

    monkeypatch.setattr(openrouter, "cancellable_urlopen", fake_urlopen)
    openrouter.generate_structured_text(
        prompt="manual text", response_schema={"type": "object"}, api_key="key"
    )

    assert captured["messages"][0]["content"] == [
        {"type": "text", "text": "manual text" + schema_prompt({"type": "object"})}
    ]
    assert captured["response_format"]["json_schema"]["schema"] == {"type": "object"}


@pytest.mark.parametrize('provider', providers.PROVIDERS)
def test_extraction_dispatches_only_supported_options(provider, monkeypatch, tmp_path):
    import threading
    from unittest.mock import Mock
    from meta_coder.extraction import ExtractionResult
    adapter = getattr(providers, provider)
    result = ExtractionResult(source_pdf='paper.pdf', status='ok')
    extract = Mock(return_value=result)
    monkeypatch.setattr(adapter, 'extract_pdf_effects', extract)
    cancel = threading.Event()
    assert providers.extract_pdf_effects(provider=provider, pdf_path=tmp_path / 'paper.pdf', manual=None, rows=[], api_key='key', model='model', timeout_sec=99, service_tier='standard', reasoning_effort='high', base_url='http://localhost/v1', response_format='none', cancel_event=cancel) is result
    args = extract.call_args.kwargs
    assert args['timeout_sec'] == 99 and args['cancel_event'] is cancel
    assert ('service_tier' in args) == (provider == 'gemini')
    assert ('reasoning_effort' in args) == (provider != 'gemini')
    assert ('base_url' in args) == (provider == 'openai_compatible')


def test_unknown_provider_rejected_before_extraction_or_drafting(tmp_path):
    with pytest.raises(ValueError, match='Unknown provider'):
        providers.extract_pdf_effects(provider='unknown', pdf_path=tmp_path / 'p.pdf', manual=None, rows=[], api_key='', model='m')
    with pytest.raises(ValueError, match='Unknown provider'):
        providers.draft_coding_manual(provider='unknown', document_text='manual', api_key='', model='m')
