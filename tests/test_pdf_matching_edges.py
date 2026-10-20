"""Optional metadata services, corrupt caches, and background scan recovery."""
import io
import json
import threading
import time
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from meta_coder import pdf_matching as matching
from meta_coder.projects import create_project


@pytest.mark.parametrize('text', [
    '{broken', '[]', '{"version": 0}', '{"version": 1, "scores": {"k": 0.5}}',
    json.dumps({'version': matching._SCORE_CACHE_VERSION, 'scores': []}),
])
def test_corrupt_caches_are_discarded(tmp_path, text):
    project = create_project('Cache', root=tmp_path)
    matching._signal_cache_path(project).write_text(text)
    if text in ('{broken', '[]'):
        assert matching.load_signal_cache(project) == {}
    matching._score_cache_path(project).write_text(text)
    assert matching.load_match_score_cache(project) == {}


def test_deleted_file_invalidates_signal_cache(tmp_path):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'%PDF')
    entry = matching._cache_entry(path, matching.PdfSignal('smith', '2020'), '')
    path.unlink()
    assert matching.cached_signal(path, {path.name: entry}) is None


def test_grobid_header_extracts_title_authors_and_publication_year(tmp_path, monkeypatch):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'%PDF')
    tei = b'''<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Attention and memory</title></titleStmt><sourceDesc><biblStruct><analytic><author><persName><surname>Smith</surname></persName></author></analytic><monogr><imprint><date when="2020-01-01">2021</date></imprint></monogr></biblStruct></sourceDesc></fileDesc></teiHeader></TEI>'''
    request = Mock(return_value=io.BytesIO(tei))
    monkeypatch.setattr(matching.urllib.request, 'urlopen', request)
    signal = matching.pdf_signal(path, grobid_url='http://localhost:8070/')
    assert signal == matching.PdfSignal('attention and memory smith', '2020')
    wire = request.call_args.args[0]
    assert wire.full_url == 'http://localhost:8070/api/processHeaderDocument'
    assert b'filename="paper.pdf"' in wire.data and b'%PDF' in wire.data
    assert request.call_args.kwargs['timeout'] == 20


@pytest.mark.parametrize('body', [b'<broken', b'<TEI/>'])
def test_unusable_grobid_output_falls_back_to_local_signal(tmp_path, monkeypatch, body):
    path = tmp_path / 'smith2020.pdf'
    path.write_bytes(b'%PDF')
    monkeypatch.setattr(matching.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(body))
    expected = matching.PdfSignal('local smith', '2020')
    monkeypatch.setattr(matching, '_local_pdf_signal', lambda _: expected)
    assert matching.pdf_signal(path, grobid_url='http://localhost') == expected


def test_grobid_network_failure_is_optional(tmp_path, monkeypatch):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(b'%PDF')
    monkeypatch.setattr(matching.urllib.request, 'urlopen', Mock(side_effect=URLError('offline')))
    assert matching.grobid_signal(path, 'http://localhost') is None


def test_scoring_empty_identity_and_confidence_boundaries():
    paper = matching.UnmatchedPaper('', '', '', '', '')
    assert matching.score_pair(paper, matching.PdfSignal('smith', '2020')) == 0
    for value, band in [(1, 'high'), (.85, 'high'), (.5, 'medium'), (.25, 'low'), (0, 'none')]:
        assert matching._confidence_band(value) == band


def test_scanner_handles_disappearing_files_and_empty_queue(tmp_path, monkeypatch):
    project = create_project('Scan', root=tmp_path)
    scanner = matching.PdfScanner()
    scanner.scan_async(project, [])
    assert scanner.state(project.project_id) is None
    path = project.sources_dir / 'gone.pdf'
    monkeypatch.setattr(matching, 'pdf_signal', Mock(side_effect=OSError('deleted')))
    scanner.scan_async(project, [path])
    deadline = time.monotonic() + 2
    while scanner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(.005)
    assert not scanner.is_running(project.project_id)
    assert scanner.state(project.project_id).snapshot() == {'status': 'complete', 'total': 1, 'processed': 1}
    assert matching.load_signal_cache(project) == {}


def test_scanner_includes_inflight_work_when_queue_grows(tmp_path, monkeypatch):
    project = create_project('Scan', root=tmp_path)
    scanner = matching.PdfScanner()
    paths = [project.sources_dir / name for name in ['first.pdf', 'second.pdf']]
    for path in paths:
        path.write_bytes(b'%PDF')
    started, release = threading.Event(), threading.Event()
    def signal(path, **kwargs):
        if path == paths[0]:
            started.set()
            assert release.wait(2)
        return matching.PdfSignal(path.stem, None)
    monkeypatch.setattr(matching, 'pdf_signal', signal)
    scanner.scan_async(project, paths[:1])
    try:
        assert started.wait(2)
        scanner.scan_async(project, paths[1:])
        assert scanner.state(project.project_id).total == 2
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while scanner.is_running(project.project_id) and time.monotonic() < deadline:
            time.sleep(.005)
    assert scanner.state(project.project_id).snapshot() == {'status': 'complete', 'total': 2, 'processed': 2}
    assert set(matching.load_signal_cache(project)) == {'first.pdf', 'second.pdf'}


def test_scan_persistence_failure_releases_running_marker(tmp_path, monkeypatch):
    project = create_project('Failure', root=tmp_path)
    scanner = matching.PdfScanner()
    path = project.sources_dir / 'paper.pdf'
    path.write_bytes(b'%PDF')
    state = matching.ScanState(status='running', total=1)
    scanner._running.add(project.project_id)
    scanner._pending[project.project_id] = {path.name: path}
    monkeypatch.setattr(matching, 'pdf_signal', lambda *a, **k: matching.PdfSignal('paper', None))
    monkeypatch.setattr(matching, 'save_signal_cache', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        scanner._scan_loop(project, '', state)
    assert not scanner.is_running(project.project_id)
    assert project.project_id not in scanner._inflight
    assert state.processed == 1


def test_queue_update_is_retained_even_if_progress_state_is_unavailable(tmp_path):
    project = create_project('Queue', root=tmp_path)
    scanner = matching.PdfScanner()
    scanner._running.add(project.project_id)
    path = project.sources_dir / 'paper.pdf'
    scanner.scan_async(project, [path])
    assert scanner._pending[project.project_id] == {'paper.pdf': path}
    assert scanner.state(project.project_id) is None
