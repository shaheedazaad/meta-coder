import json

from meta_coder.extraction import normalize_finish_reason, parse_json_response, review_issues


def test_parse_json_response_preserves_repaired_payload():
    parsed, repaired = parse_json_response('{"effects": [{"row_id": "r1",}]}')

    assert parsed == {"effects": [{"row_id": "r1"}]}
    assert repaired is not None
    assert json.loads(repaired) == parsed


def test_parse_json_response_does_not_rewrite_valid_json():
    parsed, repaired = parse_json_response('{"effects": []}')

    assert parsed == {"effects": []}
    assert repaired is None


import threading
from urllib.request import Request

import json_repair
import pytest

from meta_coder import extraction


def test_failed_repair_retains_original_response(monkeypatch):
    def fail(_):
        raise ValueError('unrepairable')
    monkeypatch.setattr(json_repair, 'repair_json', fail)
    with pytest.raises(extraction.ProviderError, match='repair failed') as raised:
        parse_json_response('{bad')
    assert raised.value.raw_response == '{bad'


@pytest.mark.parametrize('body', [b'payload', b''])
def test_http_read_closes_response(monkeypatch, body):
    class Response:
        closed = False
        def read(self):
            return body
        def close(self):
            self.closed = True
    response = Response()
    calls = []
    def open_request(request, timeout):
        calls.append((request.full_url, timeout))
        return response
    monkeypatch.setattr(extraction, 'urlopen', open_request)
    assert extraction.cancellable_urlopen(Request('https://example.test'), timeout=9, cancel_event=threading.Event()) == body
    assert response.closed
    assert calls == [('https://example.test', 9)]


def test_network_error_is_propagated(monkeypatch):
    error = OSError('offline')
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(extraction, 'urlopen', fail)
    with pytest.raises(OSError) as raised:
        extraction.cancellable_urlopen(Request('https://example.test'), timeout=1, cancel_event=threading.Event())
    assert raised.value is error


def test_cancel_closes_inflight_response(monkeypatch):
    reading = threading.Event()
    closed = threading.Event()
    cancel = threading.Event()
    class Response:
        def read(self):
            reading.set()
            assert closed.wait(2), 'cancel did not close the response'
            return b''
        def close(self):
            closed.set()
    monkeypatch.setattr(extraction, 'urlopen', lambda *a, **k: Response())
    def cancel_when_reading():
        assert reading.wait(2)
        cancel.set()
    canceller = threading.Thread(target=cancel_when_reading)
    canceller.start()
    try:
        with pytest.raises(extraction.ExtractionCancelled):
            extraction.cancellable_urlopen(Request('https://example.test'), timeout=1, cancel_event=cancel)
        assert closed.is_set()
    finally:
        closed.set()
        canceller.join(timeout=2)


def test_cancellation_before_response_opens_returns_without_waiting(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        try:
            assert release.wait(2)
            raise OSError('cancelled connection')
        finally:
            finished.set()
    monkeypatch.setattr(extraction, 'urlopen', blocked)
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(extraction.ExtractionCancelled):
            extraction.cancellable_urlopen(Request('https://example.test'), timeout=1, cancel_event=cancel)
    finally:
        release.set()
        assert finished.wait(2)


def test_cancellation_arriving_with_network_error_wins(monkeypatch):
    cancel = threading.Event()
    original_queue = extraction.queue.Queue
    class CancelOnDelivery(original_queue):
        def get(self, *args, **kwargs):
            item = super().get(*args, **kwargs)
            cancel.set()
            return item
    monkeypatch.setattr(extraction.queue, 'Queue', CancelOnDelivery)
    def fail(*args, **kwargs):
        raise OSError('connection closed')
    monkeypatch.setattr(extraction, 'urlopen', fail)
    with pytest.raises(extraction.ExtractionCancelled):
        extraction.cancellable_urlopen(Request('https://example.test'), timeout=1, cancel_event=cancel)


def test_waiting_for_http_response_survives_a_poll_timeout(monkeypatch):
    import io
    original_queue = extraction.queue.Queue
    class TimeoutOnce(original_queue):
        timed_out = False
        def get(self, *args, **kwargs):
            if not self.timed_out:
                self.timed_out = True
                raise extraction.queue.Empty
            return super().get(*args, **kwargs)
    monkeypatch.setattr(extraction.queue, 'Queue', TimeoutOnce)
    response = io.BytesIO(b'completed after polling')
    monkeypatch.setattr(extraction, 'urlopen', lambda *a, **k: response)
    result = extraction.cancellable_urlopen(Request('https://example.test'), timeout=1, cancel_event=threading.Event())
    assert result == b'completed after polling'
    assert response.closed


@pytest.mark.parametrize('value, expected', [
    (None, None), ('', None), ('  ', None), ('STOP', 'stop'), (' stop ', 'stop'),
    ('MAX_TOKENS', 'length'), ('length', 'length'), ('FINISH_REASON_UNSPECIFIED', None), ('SAFETY', 'safety'),
])
def test_normalize_finish_reason(value, expected):
    assert normalize_finish_reason(value) == expected


def test_review_issues_flag_repairs_and_abnormal_finishes_only():
    assert review_issues(repaired_response=None, finish_reason=None) == []
    for normal in ('stop', 'eos_token', 'stop_sequence'):
        assert review_issues(repaired_response=None, finish_reason=normal) == []
    issues = review_issues(repaired_response='{}', finish_reason='length')
    assert len(issues) == 2 and 'finish reason: length' in issues[0] and 'repaired' in issues[1]
