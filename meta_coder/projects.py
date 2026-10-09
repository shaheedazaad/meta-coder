from __future__ import annotations

import json
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .manual import CodingManual, manual_to_yaml_text, parse_coding_manual
from .paths import DEFAULT_MANUAL_PATH, projects_dir


PROJECT_ID_RE = re.compile(r"^[a-f0-9]{16}$")
MANUAL_NAME = "coding_manual.yml"
CODING_SHEET_NAME = "coding_sheet.csv"


class ProjectError(ValueError):
    pass


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    path: Path
    created_at: str

    @property
    def sources_dir(self) -> Path:
        return self.path / "sources"

    @property
    def output_dir(self) -> Path:
        return self.path / "output"

    @property
    def raw_dir(self) -> Path:
        return self.output_dir / "raw"

    @property
    def audit_dir(self) -> Path:
        return self.output_dir / "coded"

    @property
    def manual_path(self) -> Path:
        return self.path / MANUAL_NAME

    @property
    def coding_sheet_path(self) -> Path:
        return self.path / CODING_SHEET_NAME


def _metadata_path(path: Path) -> Path:
    return path / ".meta_coder" / "project.json"


def _write_metadata(project: Project) -> None:
    meta_path = _metadata_path(project.path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {"id": project.project_id, "name": project.name, "created_at": project.created_at},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def create_project(name: str, *, root: Path | None = None) -> Project:
    clean_name = " ".join(name.split()).strip()
    if not clean_name:
        raise ProjectError("Enter a project name.")
    if len(clean_name) > 100:
        raise ProjectError("Project names must be 100 characters or fewer.")

    base = (root or projects_dir()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    project_id = secrets.token_hex(8)
    path = base / project_id
    (path / "sources").mkdir(parents=True)
    (path / "output" / "raw").mkdir(parents=True)
    (path / "output" / "coded").mkdir(parents=True)
    shutil.copyfile(DEFAULT_MANUAL_PATH, path / MANUAL_NAME)
    (path / CODING_SHEET_NAME).write_text("row_id,source_pdf,locator\n", encoding="utf-8")

    project = Project(
        project_id=project_id,
        name=clean_name,
        path=path,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    _write_metadata(project)
    return project


def get_project(project_id: str, *, root: Path | None = None) -> Project:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectError("Invalid project identifier.")
    base = (root or projects_dir()).expanduser().resolve()
    path = (base / project_id).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ProjectError("Invalid project path.") from exc
    meta_path = _metadata_path(path)
    if not meta_path.is_file():
        raise ProjectError("Project not found.")
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError("Project metadata could not be read.") from exc
    return Project(
        project_id=project_id,
        name=str(raw.get("name") or "Untitled project"),
        path=path,
        created_at=str(raw.get("created_at") or ""),
    )


def list_projects(*, root: Path | None = None) -> list[Project]:
    base = (root or projects_dir()).expanduser().resolve()
    if not base.exists():
        return []
    found: list[Project] = []
    for path in base.iterdir():
        if not path.is_dir() or not PROJECT_ID_RE.fullmatch(path.name):
            continue
        try:
            found.append(get_project(path.name, root=base))
        except ProjectError:
            continue
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def delete_project(project: Project) -> None:
    if not _metadata_path(project.path).is_file():
        raise ProjectError("Project not found.")
    shutil.rmtree(project.path)


def read_manual_text(project: Project) -> str:
    return project.manual_path.read_text(encoding="utf-8")


def _reset_output(project: Project) -> None:
    """MVP behavior: any manual change resets output/ (see todo.md step 3 /
    plan.md Divergence #1 — field-level invalidation is a later upgrade, not
    implemented yet)."""

    if project.output_dir.exists():
        shutil.rmtree(project.output_dir)
    (project.output_dir / "raw").mkdir(parents=True)
    (project.output_dir / "coded").mkdir(parents=True)


def write_manual(project: Project, manual: CodingManual) -> None:
    project.manual_path.write_text(manual_to_yaml_text(manual), encoding="utf-8")
    _reset_output(project)


def write_manual_text(project: Project, text: str) -> None:
    """Kept for internal/test use. The app's only user-facing editing path is the
    structured GUI (see web.py's save_manual route), which builds a CodingManual
    and calls write_manual directly — the manual is never hand-edited as text."""

    manual = parse_coding_manual(text)  # raises ManualError if invalid
    write_manual(project, manual)


def reset_manual_to_default(project: Project) -> None:
    """Recovery path for the rare case the on-disk manual.yml is invalid (e.g.
    edited outside the app) and the structured editor has nothing valid to build
    from. Discards it and restores the app's default starter manual."""

    shutil.copyfile(DEFAULT_MANUAL_PATH, project.manual_path)
    _reset_output(project)


def clear_output(project: Project) -> None:
    """Manage-tab action: delete all generated output (raw model JSON, per-PDF
    audit YAML, coded_data.csv, evidence.csv) while keeping the manual, coding
    sheet, and uploaded source PDFs untouched."""

    _reset_output(project)


def project_archive_files(project: Project) -> list[tuple[Path, str]]:
    """Files to bundle for a full project download, as (absolute_path, arcname)
    pairs — everything under the project directory except the internal
    `.meta_coder` bookkeeping folder (project id/name/created_at, an
    implementation detail, not project content)."""

    if not project.path.is_dir():
        return []
    files: list[tuple[Path, str]] = []
    for path in sorted(project.path.rglob("*")):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(project.path.resolve()):
            continue
        rel = path.relative_to(project.path)
        if path.name.endswith((".tmp", ".uploading")):
            continue
        if rel.parts and rel.parts[0] == ".meta_coder":
            continue
        files.append((path, rel.as_posix()))
    return files
