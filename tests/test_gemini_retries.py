"""Bounded retries of transient Gemini HTTP errors."""
import io
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from meta_coder import gemini, provenance, web
from meta_coder.extraction import ExtractionCancelled, ProviderError
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual


def error(status=503, retry_after=None, body=b'error'):
    headers = {} if retry_after is None else {'Retry-After': retry_after}
    return HTTPError('https://example.test', status, 'temporary', headers, io.BytesIO(body))


def idle_event(cancelled_during_wait=False):
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = cancelled_during_wait
    return event


def send(event):
    return gemini._gemini_transport(Request('https://example.test'), timeout_sec=9, cancel_event=event)


def test_retry_after_is_honored_and_exchange_is_retried(monkeypatch):
    event = idle_event()
    transport = Mock(side_effect=[error(429, '5'), b'ok'])
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    assert send(event) == b'ok'
    event.wait.assert_called_once_with(5.)
    assert transport.call_count == 2


def test_backoff_is_exponential_and_never_shorter_than_retry_after(monkeypatch):
    event = idle_event()
    transport = Mock(side_effect=[error(500, '1'), error(502), b'ok'])
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    assert send(event) == b'ok'
    assert [call.args[0] for call in event.wait.call_args_list] == [2., 4.]


@pytest.mark.parametrize('status,attempts', [(400, 1), (401, 1), (404, 1), (503, 3)])
def test_retry_count_is_bounded_and_permanent_errors_are_not_retried(monkeypatch, status, attempts):
    transport = Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(error(status)))
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    with pytest.raises(HTTPError):
        send(idle_event())
    assert transport.call_count == attempts


def test_network_errors_are_not_retried(monkeypatch):
    transport = Mock(side_effect=URLError('offline'))
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    with pytest.raises(URLError):
        send(idle_event())
    assert transport.call_count == 1


@pytest.mark.parametrize('retry_after', ['abc', '-3', '1.5', 'nan', 'inf', '²', ''])
def test_invalid_retry_after_falls_back_to_backoff(monkeypatch, retry_after):
    event = idle_event()
    monkeypatch.setattr(gemini, 'audited_transport', Mock(side_effect=[error(503, retry_after), b'ok']))
    assert send(event) == b'ok'
    event.wait.assert_called_once_with(gemini.RETRY_BACKOFF_BASE_SEC)


@pytest.mark.parametrize('retry_after', ['31', '3600'])
def test_long_retry_after_returns_the_error_instead_of_retrying_early(monkeypatch, retry_after):
    event = idle_event()
    transport = Mock(side_effect=error(429, retry_after, b'quota exhausted'))
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    with pytest.raises(HTTPError) as caught:
        send(event)
    assert caught.value.read() == b'quota exhausted'  # body remains readable for the error message
    assert transport.call_count == 1
    event.wait.assert_not_called()


def test_http_date_retry_after_is_supported():
    now = datetime.now(timezone.utc)
    assert 9 < gemini._retry_after_sec({'Retry-After': format_datetime(now + timedelta(seconds=11), usegmt=True)}) <= 11
    assert gemini._retry_after_sec({'Retry-After': format_datetime(now - timedelta(minutes=5), usegmt=True)}) == 0
    naive = (now + timedelta(hours=1)).replace(tzinfo=None)
    assert gemini._retry_after_sec({'Retry-After': format_datetime(naive)}) > gemini.MAX_RETRY_AFTER_SEC
    assert gemini._retry_after_sec({'Retry-After': '12'}) == 12
    assert gemini._retry_after_sec({}) is None
    assert gemini._retry_after_sec(None) is None


def test_past_http_date_retry_after_uses_backoff(monkeypatch):
    event = idle_event()
    past = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=1), usegmt=True)
    monkeypatch.setattr(gemini, 'audited_transport', Mock(side_effect=[error(503, past), b'ok']))
    assert send(event) == b'ok'
    event.wait.assert_called_once_with(gemini.RETRY_BACKOFF_BASE_SEC)


def test_cancel_during_backoff_stops_further_requests(monkeypatch):
    transport = Mock(side_effect=error())
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    with pytest.raises(ExtractionCancelled):
        send(idle_event(cancelled_during_wait=True))
    assert transport.call_count == 1


def test_cancel_before_first_attempt_sends_nothing(monkeypatch):
    event = idle_event()
    event.is_set.return_value = True
    transport = Mock(return_value=b'ok')
    monkeypatch.setattr(gemini, 'audited_transport', transport)
    with pytest.raises(ExtractionCancelled):
        send(event)
    transport.assert_not_called()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    p = create_project('Retry project', root=tmp_path / 'projects')
    write_manual(p, parse_coding_manual('effect_definition: Treatment versus control\neffects:\n  count: {type: number}\n'))
    return p


def audited_exchanges(project):
    [operation] = (project.path / 'audit/operations').iterdir()
    return [json.loads(path.read_text()) for path in sorted(operation.glob('exchange-*.json'))]


def test_every_failed_attempt_is_recorded_by_the_real_audit_transport(project, monkeypatch):
    monkeypatch.setattr(gemini, 'RETRY_BACKOFF_BASE_SEC', 0)
    ok = json.dumps({'candidates': [{'content': {'parts': [{'text': '{"answer": 1}'}]}}]}).encode()
    transport = Mock(side_effect=[error(503, '0', b'overloaded secret-api-key'), error(500, None, b'internal'), ok])
    monkeypatch.setattr(gemini, 'cancellable_urlopen', transport)
    text = provenance.audited_call(project, 'test', gemini.generate_structured_text,
        api_key='secret-api-key', model='alias', prompt='test', response_schema={})
    assert json.loads(text) == {'answer': 1}
    recorded = audited_exchanges(project)
    assert [x['status'] for x in recorded] == ['http_error', 'http_error', 'received']
    assert [x['response']['http_status'] for x in recorded[:2]] == [503, 500]
    assert recorded[0]['response']['headers'] == {'Retry-After': '0'}
    first_body = (project.path / recorded[0]['response']['body']['path']).read_bytes()
    assert b'overloaded' in first_body and b'secret-api-key' not in first_body


def test_final_retryable_error_is_redacted_after_audited_attempts(project, monkeypatch):
    monkeypatch.setattr(gemini, 'RETRY_BACKOFF_BASE_SEC', 0)
    monkeypatch.setattr(gemini, 'cancellable_urlopen',
                        Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(error(503, None, b'busy secret-api-key'))))
    with pytest.raises(ProviderError) as caught:
        provenance.audited_call(project, 'test', gemini.generate_structured_text,
            api_key='secret-api-key', model='alias', prompt='test', response_schema={})
    assert str(caught.value) == 'Gemini API error (503): busy [redacted]'
    assert [x['status'] for x in audited_exchanges(project)] == ['http_error'] * gemini.MAX_ATTEMPTS
