"""Provider wire contracts, retries, and extraction outcomes without live APIs."""
import base64
import io
import json
import threading
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

from meta_coder import gemini, openrouter
from meta_coder.coding_sheet import CodingSheetRow
from meta_coder.extraction import ExtractionCancelled, ProviderError
from meta_coder.manual import parse_coding_manual


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())


def wire(body):
    return json.dumps(body).encode()


def call(adapter, **kwargs):
    function = gemini._call_gemini_parts if adapter is gemini else openrouter._call_openrouter_content
    content = {'parts': [{'text': 'input'}]} if adapter is gemini else {'content': [{'type': 'text', 'text': 'input'}]}
    return function(model='model', api_key='secret', response_schema={'type': 'object'}, **content, **kwargs)


def test_gemini_model_validation_encodes_url_and_checks_capability(monkeypatch):
    request = Mock(return_value=Response({'supportedGenerationMethods': ['generateContent']}))
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', request)
    assert gemini.check_model(' model/name ', api_key='a&b', timeout_sec=3) == []
    assert request.call_args.args[0].endswith('/models/model%2Fname?key=a%26b')
    assert request.call_args.kwargs == {'timeout': 3}
    request.return_value = Response({'supportedGenerationMethods': ['embedContent']})
    assert 'does not support' in gemini.check_model('m', api_key='key')[0]
    request.return_value = Response({})
    assert gemini.check_model('m', api_key='key') == []
    assert 'API key' in gemini.check_model('m', api_key='')[0]
    assert 'model name' in gemini.check_model(' ', api_key='key')[0]


@pytest.mark.parametrize('error', [HTTPError('url', 404, 'missing', {}, None), URLError('secret unavailable')])
def test_gemini_model_validation_reports_network_errors_without_key(monkeypatch, error):
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', Mock(side_effect=error))
    errors = gemini.check_model('m', api_key='secret')
    assert errors and 'secret' not in errors[0]


def test_openrouter_model_catalog_and_capability_checks(monkeypatch):
    model = {'id': 'good', 'supported_parameters': ['structured_outputs'], 'architecture': {'input_modalities': ['file']}}
    request = Mock(side_effect=lambda *a, **k: Response({'data': [model]}))
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', request)
    assert openrouter.list_models(api_key='secret') == [model]
    assert request.call_args.args[0].get_header('Authorization') == 'Bearer secret'
    assert openrouter.check_model('good') == []
    assert 'not found' in openrouter.check_model('missing')[0]
    model['architecture'] = {}
    assert 'no native file' in openrouter.check_model('good')[0]
    request.side_effect = lambda *a, **k: Response({})
    assert openrouter.list_models() == []
    request.side_effect = URLError('offline')
    with pytest.raises(ProviderError, match='reach OpenRouter'):
        openrouter.list_models()
    assert 'Could not check' in openrouter.check_model('m')[0]


def test_gemini_pdf_wire_payload_and_usage(monkeypatch):
    request = Mock(return_value=wire({'candidates': [{'content': {'parts': [{'text': 'a'}, {'inline_data': {}}, {'text': 'b'}]}}], 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2}}))
    monkeypatch.setattr(gemini, 'cancellable_urlopen', request)
    event = threading.Event()
    assert gemini._call_gemini(model='m', api_key='secret', prompt='input', pdf_bytes=b'%PDF', response_schema={'type': 'OBJECT'}, service_tier='standard', timeout_sec=12, cancel_event=event) == ('ab', {'input_tokens': 10, 'output_tokens': 2, 'finish_reason': None})
    payload = json.loads(request.call_args.args[0].data)
    assert payload['contents'][0]['parts'][1]['inline_data'] == {'mime_type': 'application/pdf', 'data': base64.b64encode(b'%PDF').decode()}
    assert payload['service_tier'] == 'standard'
    assert payload['generationConfig']['responseSchema'] == {'type': 'OBJECT'}
    assert request.call_args.kwargs == {'timeout': 12, 'cancel_event': event}


@pytest.mark.parametrize('body, reason', [({}, 'unknown'), ({'candidates': [{'finishReason': 'SAFETY'}]}, 'SAFETY')])
def test_gemini_empty_response_reports_finish_reason(monkeypatch, body, reason):
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(return_value=wire(body)))
    with pytest.raises(ProviderError, match=reason):
        call(gemini)


@pytest.mark.parametrize('adapter', [gemini, openrouter])
@pytest.mark.parametrize('http', [True, False])
def test_transport_failure_redacts_secrets(monkeypatch, adapter, http):
    def fail(*a, **k):
        if http:
            raise HTTPError('url', 400, 'bad', {}, io.BytesIO(b'secret rejected'))
        raise URLError('secret offline')
    monkeypatch.setattr(adapter, 'cancellable_urlopen', fail)
    with pytest.raises(ProviderError) as error:
        call(adapter)
    assert 'secret' not in str(error.value)
    assert '[redacted]' in str(error.value)


@pytest.mark.parametrize('adapter', [gemini, openrouter])
def test_cancellation_during_network_failure(monkeypatch, adapter):
    event = threading.Event()
    def fail(*a, **k):
        event.set()
        raise URLError('connection closed')
    monkeypatch.setattr(adapter, 'cancellable_urlopen', fail)
    with pytest.raises(ExtractionCancelled):
        call(adapter, cancel_event=event)


@pytest.mark.parametrize('body, message', [
    ([], 'invalid response object'), ({}, 'no choices'), ({'choices': 'bad'}, 'no choices'),
    ({'choices': [1]}, 'invalid choice'), ({'choices': [{'message': 'bad'}]}, 'invalid message'),
    ({'choices': [{'message': {'content': '  '}}]}, 'no message content'),
])
def test_openrouter_invalid_response_retains_raw_body(monkeypatch, body, message):
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', Mock(return_value=wire(body)))
    with pytest.raises(ProviderError, match=message) as error:
        call(openrouter)
    assert json.loads(error.value.raw_response) == body


def test_openrouter_retries_with_exponential_backoff_and_preserves_request(monkeypatch):
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = False
    requests = Mock(side_effect=[HTTPError('url', 503, 'busy', {}, io.BytesIO(b'busy')), URLError('offline'), wire({'choices': [{'message': {'content': '{}'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}})])
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', requests)
    assert call(openrouter, reasoning_effort='high', cancel_event=event) == ('{}', {'input_tokens': 5, 'output_tokens': 3, 'finish_reason': None})
    assert [args.args[0] for args in event.wait.call_args_list] == [2, 4]
    assert requests.call_count == 3
    bodies = [json.loads(args.args[0].data) for args in requests.call_args_list]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]['reasoning'] == {'effort': 'high'}


def test_openrouter_retry_exhaustion_and_cancellation(monkeypatch):
    requests = Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(HTTPError('url', 503, 'busy', {}, io.BytesIO(b'busy'))))
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', requests)
    with pytest.raises(ProviderError, match='503'):
        call(openrouter)
    assert requests.call_count == 3
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = True
    with pytest.raises(ExtractionCancelled):
        call(openrouter, cancel_event=event)
    event.is_set.return_value = True
    with pytest.raises(ExtractionCancelled):
        call(openrouter, cancel_event=event)


def test_openrouter_nonobject_usage_is_ignored(monkeypatch):
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', Mock(return_value=wire({'choices': [{'message': {'content': '{}'}}], 'usage': 'bad'})))
    assert call(openrouter) == ('{}', {'input_tokens': None, 'output_tokens': None, 'finish_reason': None})


@pytest.fixture
def extraction_input(tmp_path):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF')
    manual = parse_coding_manual('effect_definition: treatment versus control\neffects:\n  estimate:\n    type: number\n')
    return {'pdf_path': pdf, 'manual': manual, 'rows': [CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='experiment 1')], 'api_key': 'secret'}


@pytest.mark.parametrize('adapter', [gemini, openrouter])
@pytest.mark.parametrize('status', ['ok', 'needs_review', 'error', 'cancelled', 'parse_error', 'repaired'])
def test_extraction_outcomes_preserve_evidence(monkeypatch, adapter, extraction_input, status):
    item = {'row_id': 'r1', 'estimate': {'value': 1.5, 'evidence': 'p. 2'}, 'notes': {'value': '', 'evidence': ''}}
    raw = json.dumps({'effects': [item] if status != 'needs_review' else []})
    if status == 'repaired':
        raw = raw[:-1] + ',}'
    transport = Mock(return_value=(raw, {'input_tokens': 11, 'output_tokens': 4}))
    if status in ('error', 'cancelled'):
        cls = ProviderError if status == 'error' else ExtractionCancelled
        transport.side_effect = cls('failure', raw_response='wire response')
    if status == 'parse_error':
        monkeypatch.setattr(adapter, 'parse_json_response', Mock(side_effect=ProviderError('invalid JSON')))
    monkeypatch.setattr(adapter, '_call_gemini' if adapter is gemini else '_call_openrouter', transport)
    result = adapter.extract_pdf_effects(**extraction_input)
    # Repaired JSON is never accepted silently: it may be a truncated response.
    expected = {'parse_error': 'error', 'repaired': 'needs_review'}.get(status, status)
    assert result.status == expected
    assert result.source_pdf == 'paper.pdf'
    assert result.duration_sec >= 0
    assert transport.call_args.kwargs['pdf_bytes'] == b'%PDF'
    assert 'experiment 1' in transport.call_args.kwargs['prompt']
    if status in ('error', 'cancelled'):
        assert result.raw_response == 'wire response' and result.error == 'failure'
    else:
        assert result.raw_response == raw
    if expected in ('ok', 'needs_review'):
        assert (result.input_tokens, result.output_tokens) == (11, 4)
    if status in ('ok', 'repaired'):
        assert result.coded_by_row_id['r1']['estimate'] == item['estimate']
    if status == 'needs_review':
        assert result.missing_ids == {'r1'} and result.error
    if status == 'repaired':
        assert json.loads(result.repaired_response) == {'effects': [item]}
        assert 'automatically repaired' in result.error and not result.missing_ids


@pytest.mark.parametrize('adapter', [gemini, openrouter])
@pytest.mark.parametrize('reason, expected', [(None, 'ok'), ('stop', 'ok'), ('length', 'needs_review'), ('content_filter', 'needs_review')])
def test_abnormal_finish_holds_valid_response_for_review(monkeypatch, adapter, extraction_input, reason, expected):
    item = {'row_id': 'r1', 'estimate': {'value': 0.12, 'evidence': 'p. 2'}, 'notes': {'value': '', 'evidence': ''}}
    transport = Mock(return_value=(json.dumps({'effects': [item]}), {'input_tokens': 1, 'output_tokens': 2, 'finish_reason': reason}))
    monkeypatch.setattr(adapter, '_call_gemini' if adapter is gemini else '_call_openrouter', transport)
    result = adapter.extract_pdf_effects(**extraction_input)
    assert result.status == expected
    assert result.coded_by_row_id['r1']['estimate'] == item['estimate']
    if expected == 'needs_review':
        assert f'finish reason: {reason}' in result.error and result.repaired_response is None


@pytest.mark.parametrize('adapter', [gemini, openrouter])
def test_truncated_and_repaired_response_lists_every_reason(monkeypatch, adapter, extraction_input):
    raw = '{"effects": [{"row_id": "r1", "estimate": {"value": 0.12, "evidence": "p. 2"'
    transport = Mock(return_value=(raw, {'finish_reason': 'length'}))
    monkeypatch.setattr(adapter, '_call_gemini' if adapter is gemini else '_call_openrouter', transport)
    result = adapter.extract_pdf_effects(**extraction_input)
    assert result.status == 'needs_review' and result.repaired_response is not None
    assert result.error.index('finish reason: length') < result.error.index('automatically repaired')
    # The repaired row lacks `notes`, so the validation error is reported too.
    assert 'omitted required field' in result.error


@pytest.mark.parametrize('finish, expected', [('STOP', 'stop'), ('MAX_TOKENS', 'length'), ('SAFETY', 'safety'), ('FINISH_REASON_UNSPECIFIED', None), (None, None)])
def test_gemini_reports_normalised_finish_reason(monkeypatch, finish, expected):
    candidate = {'content': {'parts': [{'text': '{}'}]}}
    if finish:
        candidate['finishReason'] = finish
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(return_value=wire({'candidates': [candidate]})))
    assert call(gemini)[1]['finish_reason'] == expected


@pytest.mark.parametrize('finish, expected', [('stop', 'stop'), ('length', 'length'), ('content_filter', 'content_filter'), (None, None), ('', None)])
def test_openrouter_reports_normalised_finish_reason(monkeypatch, finish, expected):
    choice = {'message': {'content': '{}'}, 'finish_reason': finish}
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', Mock(return_value=wire({'choices': [choice]})))
    assert call(openrouter)[1]['finish_reason'] == expected


@pytest.mark.parametrize('adapter', [gemini, openrouter])
def test_invalid_wire_json_is_a_provider_error(monkeypatch, adapter):
    monkeypatch.setattr(adapter, 'cancellable_urlopen', Mock(return_value=b'<html>upstream failed</html>'))
    with pytest.raises(ProviderError, match='(?i)invalid.*JSON') as error:
        call(adapter)
    assert error.value.raw_response == '<html>upstream failed</html>'


@pytest.mark.parametrize('body', [[], {'candidates': 'bad'}, {'candidates': [1]}, {'candidates': [{'content': 'bad'}]}, {'candidates': [{'content': {'parts': [1]}}]}, {'candidates': [{'content': {'parts': [{'text': 42}]}}]}])
def test_gemini_invalid_response_shape_is_a_provider_error(monkeypatch, body):
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(return_value=wire(body)))
    with pytest.raises(ProviderError) as error:
        call(gemini)
    assert json.loads(error.value.raw_response) == body


def test_gemini_invalid_parts_collection_retains_raw_response(monkeypatch):
    body = {'candidates': [{'content': {'parts': 'bad'}}]}
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(return_value=wire(body)))
    with pytest.raises(ProviderError, match='content parts') as error:
        call(gemini)
    assert json.loads(error.value.raw_response) == body


@pytest.mark.parametrize('usage', ['bad', {'promptTokenCount': True}, {'candidatesTokenCount': 'five'}])
def test_gemini_invalid_usage_retains_raw_response(monkeypatch, usage):
    body = {'candidates': [{'content': {'parts': [{'text': '{}'}]}}], 'usageMetadata': usage}
    monkeypatch.setattr(gemini, 'cancellable_urlopen', Mock(return_value=wire(body)))
    with pytest.raises(ProviderError, match='usage metadata') as error:
        call(gemini)
    assert json.loads(error.value.raw_response) == body


@pytest.mark.parametrize('adapter', [gemini, openrouter])
def test_redaction_without_key_preserves_diagnostic(adapter):
    assert adapter._redact('offline', '') == 'offline'


def test_openrouter_pdf_payload_encodes_native_file(monkeypatch):
    request = Mock(return_value=wire({'choices': [{'message': {'content': '{}'}}]}))
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', request)
    openrouter._call_openrouter(model='m', api_key='key', prompt='code', pdf_bytes=b'%PDF', filename='paper.pdf', response_schema={})
    content = json.loads(request.call_args.args[0].data)['messages'][0]['content']
    assert content == [{'type': 'text', 'text': 'code'}, {'type': 'file', 'file': {'filename': 'paper.pdf', 'file_data': 'data:application/pdf;base64,' + base64.b64encode(b'%PDF').decode()}}]
