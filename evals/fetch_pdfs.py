"""Download the regression set's PDFs and check them against `articles.csv`.

    python evals/fetch_pdfs.py

The PDFs are kept out of the git history and published as assets of a GitHub
release. A PDF already in `evals/pdfs/` with the right checksum is left alone;
a download whose checksum does not match is discarded and reported.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdfs"
RELEASE_URL = "https://github.com/shaheedazaad/meta-coder/releases/download/evals-data-v1/"
TIMEOUT_SEC = 60


def articles() -> list[dict[str, str]]:
    with (ROOT / "articles.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as response:  # noqa: S310 (fixed https URL)
        return response.read()


def fetch(pdf_dir: Path = PDF_DIR, base_url: str = RELEASE_URL) -> list[str]:
    """Fetch every PDF that is absent or wrong; return the problems found."""

    pdf_dir.mkdir(parents=True, exist_ok=True)
    problems = []
    for article in articles():
        name, expected = article["source_pdf"], article["sha256"]
        target = pdf_dir / name
        if target.is_file() and sha256(target.read_bytes()) == expected:
            continue
        try:
            data = download(base_url + name)
        except (OSError, http.client.HTTPException) as exc:
            problems.append(f"{name}: download failed ({exc})")
            continue
        if sha256(data) != expected:
            problems.append(f"{name}: checksum does not match articles.csv")
            continue
        partial = target.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(target)
        print(f"{name}: downloaded", flush=True)
    return problems


def main() -> int:
    problems = fetch()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
