from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import BinaryIO

from .projects import ProjectError
from .sources import SOURCE_SUFFIXES, SourceError, XmlChecker, source_format


SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
WINDOWS_DEVICE_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)
MAX_UPLOAD_BYTES = 128 * 1024 * 1024


class UploadTooLarge(ProjectError):
    pass


def safe_source_name(filename: str) -> str:
    basename = Path(filename.replace("\\", "/")).name
    cleaned = SAFE_NAME_RE.sub("_", unicodedata.normalize("NFC", basename)).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ProjectError("A PDF or XML filename is required.")
    if WINDOWS_DEVICE_RE.fullmatch(cleaned.split(".", 1)[0].rstrip(" ")):
        cleaned = "_" + cleaned
    if len(cleaned.encode("utf-8")) > 180:
        stem = Path(cleaned).stem
        while len(stem.encode("utf-8")) > 160:
            stem = stem[:-1]
        cleaned = stem + Path(cleaned).suffix
    if source_format(cleaned) is None:
        raise ProjectError(f"{basename or 'File'} is not a PDF or XML file.")
    return cleaned


def unique_destination(sources_dir: Path, filename: str) -> Path:
    candidate = sources_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 2
    while True:
        candidate = sources_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_source_upload(
    sources_dir: Path,
    filename: str,
    stream: BinaryIO,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> Path:
    """Save one uploaded PDF or XML source, rejecting files that can't be used.

    An XML upload is checked as it streams in (UTF-8, not binary, starts with
    markup), so a bad file never lands in the project's sources.
    """

    safe_name = safe_source_name(filename)
    xml_checker = XmlChecker(safe_name) if source_format(safe_name) == "xml" else None
    destination = unique_destination(sources_dir, safe_name)
    partial = destination.with_name(destination.name + ".uploading")
    written = 0
    owns_partial = False
    try:
        handle = partial.open("xb")
        owns_partial = True
        with handle:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(
                        f"{safe_name} exceeds the {max_bytes // (1024 * 1024)} MB upload limit."
                    )
                if xml_checker:
                    xml_checker.feed(chunk)
                handle.write(chunk)
        if xml_checker:
            xml_checker.feed(b"", final=True)
        else:
            if written < 4:
                raise ProjectError(f"{safe_name} is empty or not a valid PDF.")
            with partial.open("rb") as handle:
                if handle.read(4) != b"%PDF":
                    raise ProjectError(f"{safe_name} is not a valid PDF.")
        partial.replace(destination)
    except SourceError as exc:
        partial.unlink(missing_ok=True)
        raise ProjectError(str(exc)) from exc
    except Exception:
        if owns_partial:
            partial.unlink(missing_ok=True)
        raise
    return destination


def list_uploaded_sources(sources_dir: Path) -> list[Path]:
    """The project's uploaded PDF and XML sources, sorted by filename."""

    return sorted(p for p in sources_dir.glob("*") if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)
