"""Selecting PDFs on the Results tab: the status filter, Retry selected and
Delete output (re-collated CSVs, audit history, run guard)."""
import json
import re
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from meta_coder import runner as runner_module
from meta_coder import web
from meta_coder.extraction import ExtractionResult
from meta_coder.fingerprints import InputChecker
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual
from meta_coder.runner import (
    PdfProgress,
    RunState,
    attempt_json_path,
    audit_yaml_path,
    delete_pdf_outputs,
    raw_json_path,
    write_raw_result,
)
from meta_coder.coding_sheet import read_coding_sheet
from meta_coder.settings import load_run_settings

MANUAL = 'effect_definition: comparison\neffects:\n  estimate: {type: number}\n'
RATED_MANUAL = 'effect_definition: comparison\nconfidence: true\neffects:\n  estimate: {type: number}\n'
PDFS = ['a.pdf', 'b.pdf', 'c.pdf']


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Selection study', root=tmp_path / 'projects')
    monkeypatch.setattr(app.state.runtime.pdf_scanner, 'scan_async', Mock())
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project, app.state.runtime


def url(project, suffix=''):
    return f'/test/projects/{project.project_id}{suffix}'


def coded(value, confidence=None):
    cell = {'value': value, 'evidence': f'p. {value}'}
    if confidence:
        cell['confidence'] = confidence
    return {'estimate': cell}


def setup_results(project, manual_text=MANUAL, statuses=('ok', 'ok', 'error')):
    """A project whose three PDFs were coded with the given statuses."""
    manual = parse_coding_manual(manual_text)
    write_manual(project, manual)
    rows = ['row_id,source_pdf,locator,authors,year']
    for index, name in enumerate(PDFS):
        (project.sources_dir / name).write_bytes(b'%PDF-' + name.encode())
        rows.append(f'r{index},{name},exp,Author{index},202{index}')
    project.coding_sheet_path.write_text('\n'.join(rows) + '\n')
    sheet = read_coding_sheet(project.coding_sheet_path, uploaded_filenames=set(PDFS))
    settings = load_run_settings(project)
    checker = InputChecker(project, manual, sheet, provider=settings.provider, model=settings.model)
    for index, (name, status) in enumerate(zip(PDFS, statuses)):
        result = ExtractionResult(name, status, coded_by_row_id={f'r{index}': coded(index + 1, 'low')} if status == 'ok' else {},
                                  input_fingerprint=checker.fingerprint(name), error=None if status == 'ok' else 'Timed out')
        write_raw_result(project, result, provider=settings.provider, model=settings.model)
        audit_yaml_path(project, name).parent.mkdir(parents=True, exist_ok=True)
        audit_yaml_path(project, name).write_text(f'source_pdf: {name}\n')
    return manual, sheet, checker


def operations(project, kind):
    found = [json.loads(path.read_text()) for path in (project.path / 'audit/operations').glob('*/operation.json')]
    return [op for op in found if op['operation'] == kind]


def csv_rows(project, name='coded_data.csv'):
    lines = (project.output_dir / name).read_text().splitlines()
    return [line.split(',') for line in lines[1:]]


def test_delete_removes_a_pdfs_output_recollates_and_records_audit(site):
    _, project, _ = site
    manual, sheet, checker = setup_results(project, RATED_MANUAL)
    # Another PDF's legacy result shares b.pdf's lossy stem and must survive.
    other_legacy = project.raw_dir / 'b.json'
    other_legacy.write_text(json.dumps({'source_pdf': 'b?.pdf', 'status': 'ok'}))
    # a.pdf also has a pre-hash legacy result.
    a_legacy = project.raw_dir / 'a.json'
    a_legacy.write_text(json.dumps({'source_pdf': 'a.pdf', 'status': 'ok'}))

    deleted = delete_pdf_outputs(project, ['a.pdf', 'b.pdf', 'missing.pdf'], manual=manual, coding_sheet=sheet, checker=checker)

    assert deleted == ['a.pdf', 'b.pdf']
    for name in ['a.pdf', 'b.pdf']:
        assert not raw_json_path(project, name).exists()
        assert not attempt_json_path(project, name).exists()
        assert not audit_yaml_path(project, name).exists()
    assert not a_legacy.exists() and other_legacy.exists()
    assert raw_json_path(project, 'c.pdf').exists()
    statuses = {row[1]: row[5] for row in csv_rows(project)}
    assert statuses == {'a.pdf': 'not_run', 'b.pdf': 'not_run', 'c.pdf': 'error'}
    assert (project.output_dir / 'evidence.csv').is_file()
    assert {row[1]: row[6] for row in csv_rows(project, 'confidence.csv')}['b.pdf'] == ''

    [operation] = operations(project, 'delete_output')
    assert operation['status'] == 'complete'
    assert operation['settings'] == {'source_pdfs': ['a.pdf', 'b.pdf']}
    deleted_inputs = [name for name in operation['inputs'] if name.startswith('deleted/')]
    assert f'deleted/output/raw/{raw_json_path(project, "a.pdf").name}' in deleted_inputs
    assert 'deleted/output/raw/a.json' in deleted_inputs
    assert f'deleted/output/coded/{audit_yaml_path(project, "b.pdf").name}' in deleted_inputs
    assert {'coded_data.csv', 'evidence.csv', 'confidence.csv'} <= set(operation['inputs'])
    # The audit copy of the deleted result is the file as it was.
    blob = operation['inputs'][f'deleted/output/raw/{raw_json_path(project, "a.pdf").name}']
    assert json.loads((project.path / blob['path']).read_text())['source_pdf'] == 'a.pdf'


def test_delete_without_saved_output_changes_and_records_nothing(site):
    _, project, _ = site
    manual, sheet, checker = setup_results(project)
    before = (project.output_dir / 'raw').stat().st_mtime_ns
    assert delete_pdf_outputs(project, ['missing.pdf'], manual=manual, coding_sheet=sheet, checker=checker) == []
    assert not (project.path / 'audit/operations').exists() or not operations(project, 'delete_output')
    assert (project.output_dir / 'raw').stat().st_mtime_ns == before


def test_deleting_every_result_removes_the_result_csvs(site):
    _, project, _ = site
    manual, sheet, checker = setup_results(project, RATED_MANUAL)
    for name in ['coded_data.csv', 'evidence.csv', 'confidence.csv']:
        (project.output_dir / name).write_text('stale')
    delete_pdf_outputs(project, PDFS, manual=manual, coding_sheet=sheet, checker=checker)
    assert not any((project.output_dir / name).exists() for name in ['coded_data.csv', 'evidence.csv', 'confidence.csv'])
    assert operations(project, 'delete_output')[0]['status'] == 'complete'


def test_a_failed_recollation_is_recorded_as_an_error(site, monkeypatch):
    _, project, _ = site
    manual, sheet, checker = setup_results(project)
    monkeypatch.setattr(runner_module, 'write_collated_outputs', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        delete_pdf_outputs(project, ['a.pdf'], manual=manual, coding_sheet=sheet, checker=checker)
    [operation] = operations(project, 'delete_output')
    assert operation['status'] == 'error' and operation['error'] == 'disk full'


def test_delete_route_redirects_forgets_finished_progress_and_validates(site):
    client, project, runtime = site
    setup_results(project)
    runtime.runner._states[project.project_id] = RunState(status='complete', pdfs=[PdfProgress('a.pdf', status='ok')])
    target = url(project, '/results/delete')

    response = client.post(target, data={'source_pdfs': json.dumps(['a.pdf', 'c.pdf'])})
    assert response.status_code == 303 and response.headers['location'].endswith('?tab=results')
    assert runtime.runner.state(project.project_id) is None
    view = web._project_view(runtime, project)
    assert view['run_all_pdfs'] == ['b.pdf']

    for data in [{}, {'source_pdfs': '[]'}, {'source_pdfs': 'not json'}, {'source_pdfs': '{"a": 1}'}, {'source_pdfs': '[""]'}]:
        response = client.post(target, data=data)
        assert response.status_code == 400 and response.json()['detail'] == 'Select at least one PDF.'
    response = client.post(target, data={'source_pdf': 'a.pdf'})
    assert response.status_code == 400 and 'None of the selected PDFs' in response.json()['detail']

    project.manual_path.write_text('effects: [')
    response = client.post(target, data={'source_pdf': 'b.pdf'})
    assert response.status_code == 400 and 'Fix the coding manual' in response.json()['detail']
    assert raw_json_path(project, 'b.pdf').exists()


@pytest.mark.parametrize('status', ['running', 'cancelling'])
def test_delete_route_is_blocked_during_a_run(site, status):
    client, project, runtime = site
    setup_results(project)
    runtime.runner._states[project.project_id] = RunState(status=status)
    response = client.post(url(project, '/results/delete'), data={'source_pdf': 'a.pdf'})
    assert response.status_code == 409
    assert raw_json_path(project, 'a.pdf').exists()


def test_retry_selected_recodes_exactly_the_chosen_pdfs_whatever_their_status(site, monkeypatch):
    client, project, runtime = site
    setup_results(project)
    runtime._session_keys['gemini'] = 'fake-test-key'
    start = Mock()
    monkeypatch.setattr(runtime.runner, 'start', start)
    response = client.post(url(project, '/run/retry'), data={'source_pdfs': json.dumps(['a.pdf', 'b.pdf', 'a.pdf'])})
    assert response.status_code == 303
    assert start.call_args.kwargs['only_pdfs'] == ['a.pdf', 'b.pdf']
    # Repeated fields from a single-PDF form and the JSON selection combine.
    client.post(url(project, '/run/retry'), data={'source_pdf': 'c.pdf', 'source_pdfs': json.dumps(['a.pdf'])})
    assert start.call_args.kwargs['only_pdfs'] == ['c.pdf', 'a.pdf']
    # An empty selection is an error, never "retry every failed PDF".
    start.reset_mock()
    response = client.post(url(project, '/run/retry'), data={'source_pdfs': '[]'})
    assert response.status_code == 400 and not start.called


def test_results_filter_counts_statuses_and_keeps_the_filter_while_paging(site):
    client, project, runtime = site
    setup_results(project, statuses=('ok', 'needs_review', 'error'))
    runtime._session_keys['gemini'] = 'fake-test-key'

    view = web._project_view(runtime, project, run_status='error')
    assert view['run_status'] == 'error'
    assert view['run_filtered_pdfs'] == ['c.pdf'] and view['run_filtered_count'] == 1
    assert sorted(view['run_all_pdfs']) == PDFS
    assert view['run_status_options'] == [('ok', 'OK', 1), ('needs_review', 'Needs review', 1), ('error', 'Failed', 1)]
    assert view['can_retry_selected'] is True

    # A filter with no matching PDF stays selectable so it can be seen and left.
    view = web._project_view(runtime, project, run_status='cancelled')
    assert view['run_filtered_pdfs'] == [] and ('cancelled', 'Cancelled', 0) in view['run_status_options']
    page = client.get(url(project, '?tab=results&run_status=cancelled')).text
    assert 'No PDFs have this status.' in page

    assert web._project_view(runtime, project, run_status='bogus')['run_status'] == ''

    page = client.get(url(project, '?tab=results&run_status=error')).text
    assert '<option value="error" selected>Failed (1)</option>' in page
    assert 'run_sort=authors&run_dir=asc&run_status=error' in page
    assert re.search(r'data-select-pdf value="c\.pdf"', page) and 'data-select-pdf value="a.pdf"' not in page
    assert '"filtered": ["c.pdf"]' in page
    assert 'Select all 1 PDF shown by this filter, on every page' in page


def test_results_pagination_carries_the_filter(site):
    client, project, runtime = site
    manual = parse_coding_manual(MANUAL)
    write_manual(project, manual)
    rows = ['row_id,source_pdf,locator,authors,year']
    for index in range(12):
        name = f'p{index:02d}.pdf'
        (project.sources_dir / name).write_bytes(b'%PDF')
        rows.append(f'r{index},{name},exp,A,2020')
        write_raw_result(project, ExtractionResult(name, 'error', error='Timed out'), provider='gemini', model='m')
    project.coding_sheet_path.write_text('\n'.join(rows) + '\n')
    page = client.get(url(project, '?tab=results&run_status=error&run_page=2')).text
    assert 'Page 2 of 2 (12 articles)' in page
    assert 'run_status=error&run_page=1' in page
    assert 'Select all 12 PDFs shown by this filter' in page
    # Links to other tabs' pages (here, the coding sheet's) keep the filter too.
    assert '&run_status=error&id_issues_page=' in page
    page = client.get(url(project, '?tab=results')).text
    assert 'Select all 12 PDFs in the table, on every page' in page


def test_selection_actions_reflect_run_and_setup_state(site):
    client, project, runtime = site
    setup_results(project)
    page = client.get(url(project, '?tab=results')).text
    # No provider key yet: deleting works, retrying needs setup.
    assert re.search(r'data-selection-retry[^>]*\s+disabled>Retry selected', page)
    assert 'To retry PDFs, set up the provider' in page
    runtime._session_keys['gemini'] = 'fake-test-key'
    page = client.get(url(project, '?tab=results')).text
    assert not re.search(r'data-selection-retry[^>]*\s+disabled>', page)
    assert 'To retry PDFs' not in page
    runtime.runner._states[project.project_id] = RunState(status='running', pdfs=[PdfProgress('a.pdf', status='running')])
    page = client.get(url(project, '?tab=results')).text
    assert re.search(r'data-selection-delete[^>]*\s+disabled>Delete output', page)
    assert 'available once the active run finishes' in page
