from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .fingerprints import pdf_digest
from .projects import ProjectError
from .sources import SOURCE_SUFFIXES, SourceError, XmlChecker, source_format


SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
WINDOWS_DEVICE_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)
MAX_UPLOAD_BYTES = 128 * 1024 * 1024


class UploadTooLarge(ProjectError):
    pass


class DuplicateUpload(ProjectError):
    """An upload whose content is already a source in the project."""

    def __init__(self, name: str, existing: str) -> None:
        super().__init__(f"{name} is already in this project as {existing}.")
        self.name = name
        self.existing = existing


@dataclass
class UploadResult:
    saved: list[Path] = field(default_factory=list)
    skipped: list[DuplicateUpload] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def safe_source_name(filename: str) -> str:
    basename = Path(filename.replace("\\", "/")).name
    cleaned = SAFE_NAME_RE.sub("_", unicodedata.normalize("NFC", basename)).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ProjectError("A PDF or XML filename is required.")
    if len(cleaned.encode("utf-8")) > 180:
        full_stem = Path(cleaned).stem
        stem = full_stem
        while len(stem.encode("utf-8")) > 160:
            stem = stem[:-1]
        # Do not separate a base letter from the marks (categories Mn, Mc, Me)
        # that follow it; drop the whole combining sequence instead.
        kept = stem
        while kept and len(kept) < len(full_stem) and unicodedata.category(full_stem[len(kept)]).startswith("M"):
            kept = kept[:-1]
        cleaned = (kept or stem) + Path(cleaned).suffix
    # After shortening: dropping a combining sequence can leave a device name.
    if WINDOWS_DEVICE_RE.fullmatch(cleaned.split(".", 1)[0].rstrip(" ")):
        cleaned = "_" + cleaned
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


def source_hashes(sources_dir: Path) -> dict[str, str]:
    """Map the SHA-256 of each uploaded source in sources_dir to its filename."""
    hashes: dict[str, str] = {}
    for path in list_uploaded_sources(sources_dir):
        digest = pdf_digest(path)
        if digest is not None:
            hashes.setdefault(digest, path.name)
    return hashes


def save_source_upload(
    sources_dir: Path,
    filename: str,
    stream: BinaryIO,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    known_hashes: dict[str, str] | None = None,
) -> Path:
    """Save one uploaded PDF or XML source, rejecting files that can't be used.

    An XML upload is checked as it streams in (UTF-8, not binary, starts with
    markup), so a bad file never lands in the project's sources.

    Raises DuplicateUpload, saving nothing, when a source with the same content
    is already there. known_hashes (from source_hashes) avoids hashing every
    source again for each file in a batch; it is updated with the saved file.
    """
    safe_name = safe_source_name(filename)
    xml_checker = XmlChecker(safe_name) if source_format(safe_name) == "xml" else None
    if known_hashes is None:
        known_hashes = source_hashes(sources_dir)
    destination = unique_destination(sources_dir, safe_name)
    partial = destination.with_name(destination.name + ".uploading")
    written = 0
    digest = hashlib.sha256()
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
                digest.update(chunk)
                handle.write(chunk)
        if xml_checker:
            xml_checker.feed(b"", final=True)
        else:
            if written < 4:
                raise ProjectError(f"{safe_name} is empty or not a valid PDF.")
            with partial.open("rb") as handle:
                if handle.read(4) != b"%PDF":
                    raise ProjectError(f"{safe_name} is not a valid PDF.")
        existing = known_hashes.get(digest.hexdigest())
        if existing is not None:
            raise DuplicateUpload(safe_name, existing)
        partial.replace(destination)
    except SourceError as exc:
        partial.unlink(missing_ok=True)
        raise ProjectError(str(exc)) from exc
    except Exception:
        if owns_partial:
            partial.unlink(missing_ok=True)
        raise
    known_hashes[digest.hexdigest()] = destination.name
    return destination


def save_source_uploads(
    sources_dir: Path,
    uploads: list[tuple[str, BinaryIO]],
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> UploadResult:
    """Save a batch of uploaded PDF and XML sources, hashing the existing sources once.

    Files whose content is already a source, or matches an earlier file in the
    batch, are skipped. Other per-file errors are collected and the rest of the
    batch is still saved.
    """
    known_hashes = source_hashes(sources_dir)
    result = UploadResult()
    for filename, stream in uploads:
        try:
            result.saved.append(
                save_source_upload(sources_dir, filename, stream, max_bytes=max_bytes, known_hashes=known_hashes)
            )
        except DuplicateUpload as exc:
            result.skipped.append(exc)
        except ProjectError as exc:
            result.errors.append(str(exc))
    return result


def upload_summary(result: UploadResult) -> str:
    """The notice shown after a batch that skipped duplicates."""
    count = len(result.saved)
    names = ", ".join(item.name for item in result.skipped)
    return (
        f"Added {count} file{'' if count == 1 else 's'}. "
        f"Skipped {len(result.skipped)} already in this project: {names}."
    )


def list_uploaded_sources(sources_dir: Path) -> list[Path]:
    """The project's uploaded PDF and XML sources, sorted by filename."""

    return sorted(p for p in sources_dir.glob("*") if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)
