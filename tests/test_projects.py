from meta_coder.projects import clear_output, create_project, project_archive_files


def test_clear_output_removes_generated_files_but_keeps_inputs(tmp_path):
    project = create_project("t", root=tmp_path)
    (project.raw_dir / "paper.json").write_text("{}", encoding="utf-8")
    (project.output_dir / "coded_data.csv").write_text("row_id\n", encoding="utf-8")
    (project.sources_dir / "paper.pdf").write_text("%PDF-1.4", encoding="utf-8")

    clear_output(project)

    assert not (project.output_dir / "coded_data.csv").exists()
    assert list(project.raw_dir.iterdir()) == []
    assert (project.sources_dir / "paper.pdf").is_file()  # untouched
    assert project.manual_path.is_file()  # untouched


def test_project_archive_files_excludes_internal_metadata(tmp_path):
    project = create_project("t", root=tmp_path)
    (project.sources_dir / "paper.pdf").write_text("%PDF-1.4", encoding="utf-8")

    files = project_archive_files(project)
    arcnames = {arcname for _, arcname in files}

    assert "coding_manual.yml" in arcnames
    assert "coding_sheet.csv" in arcnames
    assert "sources/paper.pdf" in arcnames
    assert not any(name.startswith(".meta_coder") for name in arcnames)
    for abs_path, _ in files:
        assert abs_path.is_file()


import json
import shutil

import pytest

from meta_coder import projects
from meta_coder.manual import ManualError


@pytest.mark.parametrize('name', ['', ' \n ', 'a' * 101])
def test_invalid_project_names_do_not_create_files(tmp_path, name):
    with pytest.raises(projects.ProjectError):
        create_project(name, root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_create_and_reload_project_using_default_root(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, 'projects_dir', lambda: tmp_path)
    project = create_project('  A  study\n project ')
    assert project.name == 'A study project'
    assert projects.get_project(project.project_id) == project
    assert projects.list_projects() == [project]
    assert project.audit_dir.is_dir()


@pytest.mark.parametrize('identifier', ['../escape', 'xyz', 'A' * 16])
def test_invalid_project_identifier(tmp_path, identifier):
    with pytest.raises(projects.ProjectError, match='identifier'):
        projects.get_project(identifier, root=tmp_path)


def test_missing_and_corrupt_metadata(tmp_path):
    identifier = 'a' * 16
    with pytest.raises(projects.ProjectError, match='not found'):
        projects.get_project(identifier, root=tmp_path)
    metadata = tmp_path / identifier / '.meta_coder' / 'project.json'
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{broken')
    with pytest.raises(projects.ProjectError, match='could not be read'):
        projects.get_project(identifier, root=tmp_path)
    metadata.write_text('{}')
    project = projects.get_project(identifier, root=tmp_path)
    assert project.name == 'Untitled project' and project.created_at == ''


def test_project_symlink_cannot_escape_root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / ('a' * 16)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(projects.ProjectError, match='path'):
        projects.get_project('a' * 16, root=root)


def test_listing_skips_junk_and_sorts_newest_first(tmp_path):
    root = tmp_path / 'projects'
    assert projects.list_projects(root=root) == []
    older = create_project('Older', root=root)
    newer = create_project('Newer', root=root)
    for project, date in [(older, '2020'), (newer, '2021')]:
        metadata = project.path / '.meta_coder' / 'project.json'
        data = json.loads(metadata.read_text())
        data['created_at'] = date
        metadata.write_text(json.dumps(data))
    (root / 'junk').mkdir()
    (root / ('b' * 16)).mkdir()
    (root / 'notes').write_text('ignore')
    assert [p.name for p in projects.list_projects(root=root)] == ['Newer', 'Older']


def test_delete_project_and_missing_archive(tmp_path):
    project = create_project('Delete', root=tmp_path)
    projects.delete_project(project)
    assert not project.path.exists()
    assert project_archive_files(project) == []
    with pytest.raises(projects.ProjectError, match='not found'):
        projects.delete_project(project)


def test_manual_validation_preserves_outputs_and_reset_recovers(tmp_path):
    project = create_project('Manual', root=tmp_path)
    original = projects.read_manual_text(project)
    result = project.raw_dir / 'result.json'
    result.write_text('{}')
    with pytest.raises(ManualError):
        projects.write_manual_text(project, 'invalid: true')
    assert projects.read_manual_text(project) == original
    assert result.exists()
    projects.write_manual_text(project, original.replace('effect_definition:', 'effect_definition: treatment versus control'))
    assert not result.exists()
    project.manual_path.write_text('corrupted')
    shutil.rmtree(project.output_dir)
    projects.reset_manual_to_default(project)
    assert projects.read_manual_text(project) == original
    assert project.raw_dir.is_dir() and project.audit_dir.is_dir()


def test_archive_excludes_incomplete_pdf_upload(tmp_path):
    project = create_project('Partial upload', root=tmp_path)
    (project.sources_dir / 'paper.pdf.uploading').write_bytes(b'%PDF unfinished')
    assert 'sources/paper.pdf.uploading' not in {name for _, name in project_archive_files(project)}
