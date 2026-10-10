"""Failure persistence, cancellation checkpoints and runner lifecycle contracts."""
import json
import shutil
import threading
from unittest.mock import Mock

import pytest
import yaml

from meta_coder import runner as module
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project
from meta_coder.results import render_pdf_audit_yaml


@pytest.fixture
def inputs(tmp_path):
    project = create_project('Runner', root=tmp_path)
    manual = parse_coding_manual('effect_definition: comparison\neffects:\n  estimate: {type: number}\n')
    sheet = CodingSheet(rows=[CodingSheetRow(row_id='r1', source_pdf='paper.pdf', locator='experiment')], issues=[])
    return project, manual, sheet


def run_sync(inputs, monkeypatch, **options):
    project, manual, sheet = inputs
    runner = module.Runner()
    state = module.RunState(status='running', total=1, pdfs=[module.PdfProgress('paper.pdf')])
    for key, value in options.pop('state', {}).items():
        setattr(state, key, value)
    defaults = dict(parallel_requests=1, request_delay_sec=0, request_timeout_sec=9, service_tier='standard', reasoning_effort='high', base_url='', response_format='json_schema')
    defaults.update(options)
    runner._run(project, manual, sheet, 'key', 'gemini', 'model', state, **defaults)
    return state


def test_missing_and_corrupt_persisted_results_are_ignored(inputs):
    project, _, _ = inputs
    for index, value in enumerate(['{broken', '[]', '{}']):
        (project.raw_dir / f'{index}.json').write_text(value)
    assert module.load_persisted_results(project) == {}
    path = project.raw_dir / 'missing.json'
    assert module._result_from_raw_json(path) is None
    path.write_text(json.dumps({'source_pdf': 'paper.pdf'}))
    result = module.load_persisted_results(project)['paper.pdf']
    assert result.status == 'error' and result.coded_by_row_id == {} and result.duration_sec == 0
    shutil.rmtree(project.raw_dir)
    assert module.load_persisted_results(project) == {}


def test_cancel_and_snapshot_lifecycle(inputs):
    project, _, _ = inputs
    runner = module.Runner()
    runner.cancel(project.project_id)
    assert runner.state(project.project_id) is None
    state = module.RunState(status='complete', pdfs=[module.PdfProgress('paper.pdf', status='ok', input_tokens=4)])
    runner._states[project.project_id] = state
    runner.cancel(project.project_id)
    assert state.status == 'complete' and not state.cancel_event.is_set()
    state.status = 'running'
    runner.cancel(project.project_id)
    assert state.status == 'cancelling' and state.cancel_event.is_set()
    assert state.snapshot()['pdfs'][0]['input_tokens'] == 4


def test_start_rejects_duplicate_and_empty_runs(inputs):
    project, manual, sheet = inputs
    runner = module.Runner()
    runner._states[project.project_id] = module.RunState(status='running')
    with pytest.raises(RuntimeError, match='already running'):
        runner.start(project=project, manual=manual, coding_sheet=sheet, api_key='')
    runner._states.clear()
    with pytest.raises(RuntimeError, match='No matched PDFs'):
        runner.start(project=project, manual=manual, coding_sheet=sheet, api_key='', only_pdfs=['not-in-sheet.pdf'])


def test_provider_exception_becomes_durable_per_pdf_error(inputs, monkeypatch):
    monkeypatch.setattr(module, 'extract_pdf_effects', Mock(side_effect=ValueError('bad provider response')))
    state = run_sync(inputs, monkeypatch)
    assert state.status == 'complete' and state.processed == 1 and state.finished_at
    result = module.load_persisted_results(inputs[0])['paper.pdf']
    assert result.status == 'error' and 'bad provider response' in result.error
    assert state.pdfs[0].error == result.error


def test_failed_persistence_still_reports_error_in_memory(inputs, monkeypatch):
    monkeypatch.setattr(module, 'extract_pdf_effects', Mock(return_value=ExtractionResult('paper.pdf', 'ok')))
    monkeypatch.setattr(module, 'write_raw_result', Mock(side_effect=OSError('disk full')))
    state = run_sync(inputs, monkeypatch)
    assert state.status == 'complete' and state.processed == 1
    assert state.pdfs[0].status == 'error' and 'disk full' in state.pdfs[0].error


def test_collation_failure_marks_run_failed(inputs, monkeypatch):
    monkeypatch.setattr(module, 'extract_pdf_effects', Mock(return_value=ExtractionResult('paper.pdf', 'ok')))
    monkeypatch.setattr(module, 'collate_results', Mock(side_effect=OSError('cannot write results')))
    state = run_sync(inputs, monkeypatch)
    assert state.status == 'failed' and state.error == 'cannot write results' and state.finished_at
    assert module.load_persisted_results(inputs[0])['paper.pdf'].status == 'ok'


def test_cancel_while_pacing_skips_provider(inputs, monkeypatch):
    monkeypatch.setattr(module._RequestPacer, 'wait', lambda *a: False)
    monkeypatch.setattr(module, 'extract_pdf_effects', lambda **k: pytest.fail('request after cancellation'))
    state = run_sync(inputs, monkeypatch)
    assert state.processed == 1 and state.pdfs[0].status == 'cancelled'


def test_pacer_wait_is_interruptible(monkeypatch):
    pacer = module._RequestPacer(10)
    event = threading.Event()
    assert pacer.wait(event)
    event.set()
    assert not pacer.wait(event)
    assert not module._RequestPacer(0).wait(event)


def test_audit_reports_repair_and_unexpected_rows(inputs):
    _, manual, sheet = inputs
    result = ExtractionResult('paper.pdf', 'needs_review', missing_ids={'r1'}, extra_ids={'extra'}, repaired_response='{}', error='row mismatch')
    data = yaml.safe_load(render_pdf_audit_yaml(manual=manual, source_pdf='paper.pdf', rows=sheet.rows, result=result))
    assert data['json_repaired'] is True
    assert data['unexpected_row_ids'] == ['extra']
    assert data['missing_row_ids'] == ['r1']
    assert data['effects']['r1']['status'] == 'not returned by the model'


def test_distinct_pdf_names_cannot_overwrite_each_others_results(inputs):
    project, _, _ = inputs
    names = ['paper one.pdf', 'paper_one.pdf', 'paper.one.pdf', 'paper.one.PDF']
    for name in names:
        module.write_raw_result(project, ExtractionResult(name, 'ok', raw_response=name), provider='gemini', model='model')
    loaded = module.load_persisted_results(project)
    assert set(loaded) == set(names)
    assert len({module.audit_yaml_path(project, name) for name in names}) == len(names)
    assert all(loaded[name].raw_response == name for name in names)


def test_new_result_takes_precedence_over_legacy_filename(inputs):
    project, _, _ = inputs
    legacy = project.raw_dir / 'paper.json'
    legacy.write_text(json.dumps({'source_pdf': 'paper.pdf', 'status': 'ok', 'raw_response': 'old'}))
    module.write_raw_result(project, ExtractionResult('paper.pdf', 'ok', raw_response='new'), provider='gemini', model='model')
    assert module.load_persisted_results(project)['paper.pdf'].raw_response == 'new'
    assert module.raw_json_path_for_read(project, 'paper.pdf') == module.raw_json_path(project, 'paper.pdf')


def test_legacy_result_remains_readable(inputs):
    project, _, _ = inputs
    legacy = project.raw_dir / 'paper.json'
    legacy.write_text(json.dumps({'source_pdf': 'paper.pdf', 'status': 'ok', 'raw_response': 'legacy'}))
    assert module.load_persisted_results(project)['paper.pdf'].raw_response == 'legacy'
    assert module.raw_json_path_for_read(project, 'paper.pdf') == legacy


def test_audit_finalization_failure_is_visible(inputs, monkeypatch):
    monkeypatch.setattr(module, 'extract_pdf_effects', Mock(return_value=ExtractionResult('paper.pdf', 'ok')))
    audit = Mock()
    audit.id = "test-run"
    audit.finish.side_effect = OSError('audit disk full')
    state = run_sync(inputs, monkeypatch, state={'audit_run': audit})
    assert state.status == 'failed'
    assert state.error == 'Could not finalize audit history: audit disk full'
    assert module.load_persisted_results(inputs[0])['paper.pdf'].status == 'ok'
