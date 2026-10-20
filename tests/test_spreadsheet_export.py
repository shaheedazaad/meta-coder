"""Spreadsheet-safe copies of the result CSVs and the per-row provenance export."""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from meta_coder import web
from meta_coder.extraction import ExtractionResult
from meta_coder.projects import create_project
from meta_coder.results import provenance_csv, spreadsheet_csv, spreadsheet_safe_cell
from meta_coder.runner import write_raw_result


@pytest.mark.parametrize(('cell', 'expected'), [
    ('=SUM(A1:A2)', "'=SUM(A1:A2)"),
    ('+danger', "'+danger"),
    ('-danger', "'-danger"),
    ('@danger', "'@danger"),
    ('\t@danger', "'\t@danger"),
    ('\tplain', "'\tplain"),
    ('\r=1', "'\r=1"),
    ('  =1+1', "'  =1+1"),
    ('\n@x', "'\n@x"),
    ('-1.25e-3', '-1.25e-3'),
    ('+4', '+4'),
    (' -.5', ' -.5'),
    ('1-2', '1-2'),
    ('a=b', 'a=b'),
    ('Not Reported', 'Not Reported'),
    ('', ''),
])
def test_spreadsheet_safe_cell_follows_owasp_prefixes_but_keeps_numbers(cell, expected):
    assert spreadsheet_safe_cell(cell) == expected


def test_spreadsheet_csv_escapes_headers_and_cells_and_adds_bom():
    canonical = 'row_id,=formula_header,estimate,quote\r\n=SUM(A1:A2),+danger,-1.25e-3,"Café\r\nline"\r\n'
    exported = spreadsheet_csv(canonical)
    assert exported.startswith(b'\xef\xbb\xbf') and b'\r\r\n' not in exported
    rows = list(csv.reader(io.StringIO(exported.decode('utf-8-sig'), newline='')))
    assert rows == [
        ['row_id', "'=formula_header", 'estimate', 'quote'],
        ["'=SUM(A1:A2)", "'+danger", '-1.25e-3', 'Café\r\nline'],
    ]


def test_spreadsheet_csv_drops_blank_lines_from_legacy_windows_files():
    exported = spreadsheet_csv('row_id,year\r\r\nr1,2020\r\r\n').decode('utf-8-sig')
    assert exported == 'row_id,year\r\nr1,2020\r\n'


def test_provenance_follows_coded_rows_with_blanks_for_unknown_results():
    coded = 'row_id,source_pdf,status\r\nr1,a.pdf,ok\r\nr2,b.pdf,ok\r\nr3,legacy.pdf,ok\r\nr4,missing.pdf,not_run\r\n'
    results = {
        'a.pdf': ExtractionResult('a.pdf', 'ok', provider='gemini', model='m-a', audit_operation_id='op1'),
        'b.pdf': ExtractionResult('b.pdf', 'ok', provider='openrouter', model='m-b'),
        'legacy.pdf': ExtractionResult('legacy.pdf', 'ok'),
    }
    rows = list(csv.reader(io.StringIO(provenance_csv(coded, results))))
    assert rows == [
        ['row_id', 'source_pdf', 'provider', 'model', 'audit_operation_id'],
        ['r1', 'a.pdf', 'gemini', 'm-a', 'op1'],
        ['r2', 'b.pdf', 'openrouter', 'm-b', ''],
        ['r3', 'legacy.pdf', '', '', ''],
        ['r4', 'missing.pdf', '', '', ''],
    ]


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(web.credentials, 'saved_key_configured', lambda _: False)
    monkeypatch.setattr(web.credentials, 'keyring_available', lambda: False)
    app = web.create_app(token='test', projects_root=tmp_path / 'projects')
    project = create_project('Export study', root=tmp_path / 'projects')
    with TestClient(app, base_url='http://localhost', follow_redirects=False) as client:
        yield client, project


def url(project, suffix):
    return f'/test/projects/{project.project_id}{suffix}'


def test_spreadsheet_download_routes(site):
    client, project = site
    for kind, filename in [('coded', 'coded_data.csv'), ('evidence', 'evidence.csv')]:
        missing = client.get(url(project, '/download/spreadsheet/' + kind))
        assert missing.status_code == 404 and missing.json()['detail'] == 'No results yet.'
        (project.output_dir / filename).write_bytes(b'row_id,value\r\n=1+1,-2\r\n')
        response = client.get(url(project, '/download/spreadsheet/' + kind))
        assert response.status_code == 200 and response.headers['content-type'].startswith('text/csv')
        assert response.content == b"\xef\xbb\xbfrow_id,value\r\n'=1+1,-2\r\n"
        stem = filename.removesuffix('.csv')
        assert response.headers['content-disposition'] == f"attachment; filename*=utf-8''Export%20study-{stem}-spreadsheet.csv"
        # The canonical file is untouched.
        assert (project.output_dir / filename).read_bytes() == b'row_id,value\r\n=1+1,-2\r\n'
    unknown = client.get(url(project, '/download/spreadsheet/provenance'))
    assert unknown.status_code == 404 and unknown.json()['detail'] == 'Unknown export.'


def test_provenance_download_reads_persisted_provider_and_model(site):
    client, project = site
    missing = client.get(url(project, '/download/provenance'))
    assert missing.status_code == 404 and missing.json()['detail'] == 'No results yet.'
    project.raw_dir.mkdir(parents=True, exist_ok=True)
    write_raw_result(project, ExtractionResult('a.pdf', 'ok', audit_operation_id='op1'), provider='gemini', model='m-a')
    (project.output_dir / 'coded_data.csv').write_bytes(b'row_id,source_pdf,status\r\nr1,a.pdf,ok\r\nr2,b.pdf,not_run\r\n')
    response = client.get(url(project, '/download/provenance'))
    assert response.status_code == 200
    assert response.headers['content-disposition'] == "attachment; filename*=utf-8''Export%20study-provenance.csv"
    assert response.text == ('row_id,source_pdf,provider,model,audit_operation_id\r\n'
                             'r1,a.pdf,gemini,m-a,op1\r\nr2,b.pdf,,,\r\n')


def test_download_filename_with_special_characters_uses_rfc6266_encoding(site, tmp_path):
    client, _ = site
    project = create_project('Études "quoted"', root=tmp_path / 'projects')
    (project.output_dir / 'coded_data.csv').write_bytes(b'row_id\r\nr1\r\n')
    response = client.get(url(project, '/download/spreadsheet/coded'))
    assert response.headers['content-disposition'] == (
        "attachment; filename*=utf-8''%C3%89tudes%20%22quoted%22-coded_data-spreadsheet.csv")
    plain = create_project('Exports', root=tmp_path / 'projects')
    (plain.output_dir / 'coded_data.csv').write_bytes(b'row_id\r\nr1\r\n')
    response = client.get(url(plain, '/download/provenance'))
    assert response.headers['content-disposition'] == 'attachment; filename="Exports-provenance.csv"'


def test_results_tab_links_to_spreadsheet_and_provenance_downloads(site):
    client, project = site
    for filename in ('coded_data.csv', 'evidence.csv'):
        (project.output_dir / filename).write_bytes(b'row_id,source_pdf\r\n')
    page = client.get(url(project, '?tab=results')).text
    for suffix in ('/download/provenance', '/download/spreadsheet/coded', '/download/spreadsheet/evidence'):
        assert url(project, suffix) in page
