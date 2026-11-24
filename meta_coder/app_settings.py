"""Settings shared across every project.

Extraction setups are extractors (see extractors.py), which each project
chooses from. The upload cap, PDF-matching server and the extractor used to
draft coding manuals and convert coding sheets are global because they
describe app-wide workflows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import app_data_dir
from .provenance import atomic_write


MIN_UPLOAD_SIZE_CAP_MB = 1
MAX_UPLOAD_SIZE_CAP_MB = 1024
DEFAULT_UPLOAD_SIZE_CAP_MB = 128


@dataclass
class AppSettings:
    upload_size_cap_mb: int = DEFAULT_UPLOAD_SIZE_CAP_MB
    grobid_url: str = ""
    # The extractor used for manual drafting and coding-sheet conversion.
    drafting_extractor_id: str = ""

    def clamped(self) -> "AppSettings":
        return AppSettings(
            upload_size_cap_mb=max(
                MIN_UPLOAD_SIZE_CAP_MB, min(int(self.upload_size_cap_mb), MAX_UPLOAD_SIZE_CAP_MB)
            ),
            grobid_url=(self.grobid_url or "").strip(),
            drafting_extractor_id=(self.drafting_extractor_id or "").strip(),
        )

    @property
    def upload_size_cap_bytes(self) -> int:
        return self.upload_size_cap_mb * 1024 * 1024


def _settings_path() -> Path:
    return app_data_dir() / "app_settings.json"


def read_raw_app_settings() -> dict | None:
    """The settings file as saved: {} when missing, None when unreadable."""
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def load_app_settings() -> AppSettings:
    raw = read_raw_app_settings()
    if not raw:
        return AppSettings().clamped()
    try:
        return AppSettings(
            upload_size_cap_mb=int(raw.get("upload_size_cap_mb") or DEFAULT_UPLOAD_SIZE_CAP_MB),
            grobid_url=str(raw.get("grobid_url") or ""),
            drafting_extractor_id=str(raw.get("drafting_extractor_id") or ""),
        ).clamped()
    except (ValueError, TypeError):
        return AppSettings().clamped()


def save_app_settings(settings: AppSettings) -> AppSettings:
    normalized = settings.clamped()
    atomic_write(_settings_path(), (json.dumps(asdict(normalized), indent=2) + "\n").encode("utf-8"))
    return normalized
