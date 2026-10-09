"""Saved results are reused only while the inputs that produced them are
unchanged, and a failed retry never replaces a successful result."""
import os
import time
from dataclasses import replace

import pytest

import meta_coder.runner as runner_module
from meta_coder import fingerprints
from meta_coder.coding_sheet import CodingSheet, CodingSheetRow
from meta_coder.extraction import ExtractionResult
from meta_coder.fingerprints import CHANGED_INPUTS_ERROR, MISSING_PDF_ERROR, InputChecker
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project
from meta_coder.runner import (
    PdfProgress,
    Runner,
    RunState,
    attempt_json_path,
    audit_yaml_path,
    load_latest_attempts,
    load_persisted_results,
    raw_json_path,
    write_raw_result,
)

MANUAL = 'effect_definition: comparison\neffects:\n  estimate: {type: number}\n'


@pytest.fixture
def context(tmp_path):
    project = create_project('Freshness', root=tmp_path)
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF-original')
    manual = parse_coding_manual(MANUAL)
    sheet = CodingSheet([CodingSheetRow('r1', 'paper.pdf', 'Table 1', 'Smith', '2020'),
                         CodingSheetRow('r2', 'paper.pdf', 'Table 2', 'Smith', '2020')], [])
    return project, manual, sheet


def checker(context, **settings):
    project, manual, sheet = context
    return InputChecker(project, manual, sheet, **{'provider': 'gemini', 'model': 'model', **settings})


def fingerprint(context, **settings):
    return checker(context, **settings).fingerprint('paper.pdf')


def ok_result(context, **changes):
    fields = {'coded_by_row_id': {'r1': {'estimate': {'value': 7}}}, 'input_fingerprint': fingerprint(context)}
    return ExtractionResult('paper.pdf', 'ok', **{**fields, **changes})


def test_fingerprint_ignores_formatting_bookkeeping_columns_and_row_order(context):
    project, manual, sheet = context
    first = fingerprint(context)
    assert first
    manual.raw_text = '# formatting only'
    sheet.rows.reverse()
    sheet.rows[0].authors, sheet.rows[0].year, sheet.rows[0].title = 'Jones', '1999', 'Retitled'
    assert fingerprint(context) == first
    # Settings that only affect how a request is sent don't change it either.
    assert fingerprint(context, reasoning_effort='high', base_url='http://x') == first
    assert checker(context).check({'paper.pdf': ok_result(context)})['paper.pdf'].status == 'ok'


@pytest.mark.parametrize('change', ['pdf', 'manual', 'locator', 'row_id', 'removed_row', 'model', 'provider'])
def test_changed_inputs_mark_results_stale_without_discarding_them(context, change):
    project, manual, sheet = context
    result = ok_result(context)
    settings = {}
    if change == 'pdf':
        (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF-replaced')
    if change == 'manual':
        manual.effects['estimate'].description = 'Different instructions'
    if change == 'locator':
        sheet.rows[0].locator = 'Table 3'
    if change == 'row_id':
        sheet.rows[0].row_id = 'r3'
    if change == 'removed_row':
        sheet.rows.pop()
    if change == 'model':
        settings = {'model': 'other'}
    if change == 'provider':
        settings = {'provider': 'openrouter'}
    current = checker(context, **settings).check({'paper.pdf': result})['paper.pdf']
    assert current.status == 'stale' and current.coded_by_row_id == {}
    assert current.error == CHANGED_INPUTS_ERROR
    assert result.status == 'ok' and result.coded_by_row_id  # the saved result is untouched


def test_provider_specific_settings_count_only_where_they_are_sent(context):
    assert fingerprint(context, provider='openrouter', reasoning_effort='high') != fingerprint(context, provider='openrouter')
    assert fingerprint(context, provider='openai_compatible', base_url='http://a') != \
        fingerprint(context, provider='openai_compatible', base_url='http://b')
    assert fingerprint(context, provider='openai_compatible', response_format='json_object') != \
        fingerprint(context, provider='openai_compatible')


def test_restoring_inputs_makes_a_stale_result_current_again(context):
    project, _, _ = context
    result = ok_result(context)
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF-replaced')
    assert checker(context).check({'paper.pdf': result})['paper.pdf'].status == 'stale'
    (project.sources_dir / 'paper.pdf').write_bytes(b'%PDF-original')
    assert checker(context).check({'paper.pdf': result})['paper.pdf'] is result


def test_missing_pdf_and_unmatched_results_cannot_be_used(context):
    project, _, sheet = context
    result = ok_result(context)
    (project.sources_dir / 'paper.pdf').unlink()
    current = checker(context).check({'paper.pdf': result})['paper.pdf']
    assert current.status == 'stale' and current.error == MISSING_PDF_ERROR
    sheet.rows.clear()
    assert checker(context).check({'paper.pdf': result})['paper.pdf'].error == CHANGED_INPUTS_ERROR


def test_legacy_results_and_an_invalid_manual_leave_results_unchanged(context):
    project, _, sheet = context
    legacy = ExtractionResult('paper.pdf', 'ok', coded_by_row_id={'r1': {'estimate': {'value': 7}}})
    assert checker(context).check({'paper.pdf': legacy})['paper.pdf'] is legacy
    result = ok_result(context)
    unchecked = InputChecker(project, None, sheet, provider='gemini', model='model')
    assert unchecked.fingerprint('paper.pdf') is None
    assert unchecked.check({'paper.pdf': result})['paper.pdf'] is result


def test_pdf_digest_is_cached_until_the_file_changes(context, monkeypatch):
    project, _, _ = context
    path = project.sources_dir / 'paper.pdf'
    first = fingerprints.pdf_digest(path)
    opened = []
    real_open = type(path).open
    monkeypatch.setattr(type(path), 'open', lambda self, *a, **k: opened.append(self) or real_open(self, *a, **k))
    assert fingerprints.pdf_digest(path) == first and opened == []
    path.write_bytes(b'%PDF-other')
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    opened.clear()
    assert fingerprints.pdf_digest(path) != first and opened == [path]
    assert fingerprints.pdf_digest(project.sources_dir / 'missing.pdf') is None


@pytest.mark.parametrize('status', ['error', 'cancelled', 'needs_review'])
def test_failed_retry_is_recorded_without_replacing_a_success(context, status):
    project, _, _ = context
    write_raw_result(project, ok_result(context, raw_response='good'), provider='gemini', model='model')
    attempt = ExtractionResult('paper.pdf', status, error='latest attempt failed', input_fingerprint=fingerprint(context))
    write_raw_result(project, attempt, provider='gemini', model='model')
    assert load_persisted_results(project)['paper.pdf'].raw_response == 'good'
    assert load_latest_attempts(project)['paper.pdf'].status == status
    assert list(load_persisted_results(project)) == ['paper.pdf']  # the attempt file is not a second result


def test_success_replaces_earlier_results_and_stale_successes_are_kept_but_not_used(context):
    project, _, _ = context
    write_raw_result(project, ExtractionResult('paper.pdf', 'error', input_fingerprint='old'), provider='gemini', model='model')
    write_raw_result(project, ok_result(context, input_fingerprint='old'), provider='gemini', model='model')
    assert load_persisted_results(project)['paper.pdf'].status == 'ok'
    write_raw_result(project, ExtractionResult('paper.pdf', 'error', input_fingerprint=fingerprint(context)),
                     provider='gemini', model='model')
    kept = load_persisted_results(project)
    assert kept['paper.pdf'].status == 'ok'  # kept, so restoring the old inputs recovers it
    assert checker(context).check(kept)['paper.pdf'].status == 'stale'  # but never used for these inputs
    write_raw_result(project, ok_result(context, raw_response='new'), provider='gemini', model='model')
    assert load_persisted_results(project)['paper.pdf'].raw_response == 'new'


def test_failed_attempt_keeps_a_legacy_success_at_its_pre_hash_path(context):
    project, _, _ = context
    legacy_path = project.raw_dir / 'paper.json'
    legacy_path.write_text('{"source_pdf": "paper.pdf", "status": "ok"}')
    write_raw_result(project, ExtractionResult('paper.pdf', 'error'), provider='gemini', model='model')
    assert not raw_json_path(project, 'paper.pdf').exists()
    assert load_persisted_results(project)['paper.pdf'].status == 'ok'
    assert attempt_json_path(project, 'paper.pdf').is_file()


def test_unrelated_and_unreadable_attempt_files_are_ignored(context):
    project, _, _ = context
    assert load_latest_attempts(replace(project, path=project.path / 'missing')) == {}
    (project.raw_dir / 'broken.attempt.json').write_text('not json')
    (project.raw_dir / 'renamed.attempt.json').write_text('{"source_pdf": "paper.pdf", "status": "error"}')
    assert load_latest_attempts(project) == {}


def test_discarding_finished_progress_keeps_an_active_run():
    runner = Runner()
    runner._states['project'] = RunState(status='complete', processed=4)
    runner.discard_finished_state('project')
    assert runner.state('project') is None
    runner._states['project'] = RunState(status='cancelling')
    runner.discard_finished_state('project')
    assert runner.is_running('project')


def _run(runner, project, manual, sheet, **kwargs):
    runner.start(project=project, manual=manual, coding_sheet=sheet, api_key='fake', model='model', **kwargs)
    deadline = time.monotonic() + 5
    while runner.is_running(project.project_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    return runner.state(project.project_id)


def test_runs_recode_changed_inputs_and_keep_successes_after_failed_retries(context, monkeypatch):
    project, manual, sheet = context
    outcomes = ['ok', 'error', 'ok']
    calls = []

    def fake_extract(*, pdf_path, rows, **_kwargs):
        calls.append(pdf_path.name)
        status = outcomes.pop(0)
        value = {'value': len(calls), 'evidence': 'p1'} if status == 'ok' else {}
        return ExtractionResult(pdf_path.name, status, coded_by_row_id={'r1': {'estimate': value}} if value else {},
                                error=None if status == 'ok' else 'provider failed')

    monkeypatch.setattr(runner_module, 'extract_pdf_effects', fake_extract)
    runner = Runner()
    _run(runner, project, manual, sheet)
    assert load_persisted_results(project)['paper.pdf'].input_fingerprint == fingerprint(context)
    yaml_path = audit_yaml_path(project, 'paper.pdf')
    first_yaml = yaml_path.read_text()
    with pytest.raises(RuntimeError, match='No matched PDFs'):
        runner.start(project=project, manual=manual, coding_sheet=sheet, api_key='fake', model='model')

    state = _run(runner, project, manual, sheet, only_pdfs=['paper.pdf'])
    assert state.pdfs[0].status == 'error'
    assert load_persisted_results(project)['paper.pdf'].status == 'ok'
    assert ',1,' in (project.output_dir / 'coded_data.csv').read_text()  # still collated
    assert yaml_path.read_text() == first_yaml

    sheet.rows[0].locator = 'Table 9'  # the saved success no longer matches
    _run(runner, project, manual, sheet)
    assert calls == ['paper.pdf'] * 3
    assert ',3,' in (project.output_dir / 'coded_data.csv').read_text()


def test_stale_results_are_not_collated_when_another_pdf_runs(context, monkeypatch):
    project, manual, sheet = context
    (project.sources_dir / 'other.pdf').write_bytes(b'%PDF-other')
    write_raw_result(project, ok_result(context), provider='gemini', model='model')
    manual.effects['estimate'].description = 'Changed'
    monkeypatch.setattr(runner_module, 'extract_pdf_effects',
                        lambda *, pdf_path, **_: ExtractionResult(pdf_path.name, 'error', error='failed'))
    sheet.rows.append(CodingSheetRow('r3', 'other.pdf', 'Table 1'))
    _run(Runner(), project, manual, sheet, only_pdfs=['other.pdf'])
    coded = (project.output_dir / 'coded_data.csv').read_text()
    assert ',stale,' in coded and ',7,' not in coded


def test_progress_reports_the_attempt_even_when_a_success_is_kept(context, monkeypatch):
    project, manual, sheet = context
    write_raw_result(project, ok_result(context), provider='gemini', model='model')
    monkeypatch.setattr(runner_module, 'extract_pdf_effects',
                        lambda *, pdf_path, **_: ExtractionResult(pdf_path.name, 'needs_review', error='check'))
    state = _run(Runner(), project, manual, sheet, only_pdfs=['paper.pdf'])
    assert state.pdfs == [PdfProgress('paper.pdf', status='needs_review', error='check')]
