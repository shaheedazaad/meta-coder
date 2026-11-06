"""Upload validation and atomic storage, using disposable local files."""
import io

import pytest

from meta_coder.projects import ProjectError
from meta_coder.uploads import (
    UploadTooLarge, list_uploaded_pdfs, safe_pdf_name, save_pdf_upload, unique_destination,
)


@pytest.mark.parametrize(('name', 'expected'), [
    ('../../paper.pdf', 'paper.pdf'),
    ('C:\\fakepath\\paper.PDF', 'paper.PDF'),
    ('páper?.pdf', 'páper_.pdf'),
    (' .paper.pdf ', 'paper.pdf'),
    ('x' * 200 + '.pdf', 'x' * 160 + '.pdf'),
])
def test_filename_normalization(name, expected):
    assert safe_pdf_name(name) == expected


@pytest.mark.parametrize('name', ['', '.', '..', '...', 'paper.txt', 'paper'])
def test_invalid_names_are_rejected(name):
    with pytest.raises(ProjectError):
        safe_pdf_name(name)


def test_duplicate_uploads_preserve_all_existing_files(tmp_path):
    original = save_pdf_upload(tmp_path, 'paper.pdf', io.BytesIO(b'%PDF-first'))
    second = save_pdf_upload(tmp_path, 'paper.pdf', io.BytesIO(b'%PDF-second'))
    third = unique_destination(tmp_path, 'paper.pdf')
    assert original.read_bytes() == b'%PDF-first'
    assert second.name == 'paper (2).pdf'
    assert second.read_bytes() == b'%PDF-second'
    assert third.name == 'paper (3).pdf'
    assert not list(tmp_path.glob('*.uploading'))


@pytest.mark.parametrize('data', [b'', b'%PD', b'not a pdf'])
def test_invalid_content_leaves_no_partial_file(tmp_path, data):
    with pytest.raises(ProjectError, match='valid PDF'):
        save_pdf_upload(tmp_path, 'paper.pdf', io.BytesIO(data))
    assert list(tmp_path.iterdir()) == []


def test_size_limit_is_inclusive_and_cleans_up_failure(tmp_path):
    assert save_pdf_upload(tmp_path, 'ok.pdf', io.BytesIO(b'%PDF'), max_bytes=4).read_bytes() == b'%PDF'
    with pytest.raises(UploadTooLarge):
        save_pdf_upload(tmp_path, 'big.pdf', io.BytesIO(b'%PDF!'), max_bytes=4)
    assert [p.name for p in tmp_path.iterdir()] == ['ok.pdf']


def test_stream_failure_cleans_up_partial_output(tmp_path):
    class BrokenStream:
        def read(self, size):
            raise OSError('stream disconnected')

    with pytest.raises(OSError, match='disconnected'):
        save_pdf_upload(tmp_path, 'paper.pdf', BrokenStream())
    assert list(tmp_path.iterdir()) == []


def test_large_upload_reads_multiple_chunks(tmp_path):
    data = b'%PDF' + b'x' * (1024 * 1024 + 3)
    assert save_pdf_upload(tmp_path, 'paper.pdf', io.BytesIO(data)).read_bytes() == data


def test_listing_only_returns_pdf_files_in_order(tmp_path):
    for name in ['z.PDF', 'a.pdf', 'notes.txt', 'partial.pdf.uploading']:
        (tmp_path / name).write_bytes(b'')
    (tmp_path / 'directory.pdf').mkdir()
    assert [p.name for p in list_uploaded_pdfs(tmp_path)] == ['a.pdf', 'z.PDF']


def test_existing_partial_upload_is_not_deleted_by_a_competing_upload(tmp_path):
    partial = tmp_path / 'paper.pdf.uploading'
    partial.write_bytes(b'another upload owns this file')
    with pytest.raises(FileExistsError):
        save_pdf_upload(tmp_path, 'paper.pdf', io.BytesIO(b'%PDF-new'))
    assert partial.read_bytes() == b'another upload owns this file'
    assert not (tmp_path / 'paper.pdf').exists()


@pytest.mark.parametrize(('name', 'expected'), [
    ('Müller 2020.pdf', 'Müller 2020.pdf'),
    ('研究.pdf', '研究.pdf'),
    ('e\u0301.pdf', 'é.pdf'),
    ('Smith (2020) & Lee.pdf', 'Smith (2020) & Lee.pdf'),
    ('CON.pdf', '_CON.pdf'),
    ('lpt1.PDF', '_lpt1.PDF'),
    ('nul.extra.pdf', '_nul.extra.pdf'),
    ('control\x01.pdf', 'control_.pdf'),
])
def test_unicode_and_portable_names(name, expected):
    assert safe_pdf_name(name) == expected


def test_multibyte_filename_fits_portable_byte_limit(tmp_path):
    name = safe_pdf_name('研' * 170 + '.pdf')
    assert len(name.encode('utf-8')) <= 180
    assert save_pdf_upload(tmp_path, name, io.BytesIO(b'%PDF')).name == name
