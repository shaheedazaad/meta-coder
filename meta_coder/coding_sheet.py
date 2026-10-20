"""Coding sheet: one row per effect-instance, joining a PDF to the effects/
conditions it contains (plan.md "Problem 3").

Deliberately a fixed schema, not manual-configurable: exactly the minimal fields
needed for paper identification (`authors`, `year`), effect ID (`row_id`), and
effect location (`source_pdf`, `locator`) — nothing else. An open-ended per-manual
column set was confusing (users couldn't tell what the sheet was supposed to
contain), so the CSV may have this info and only this info.

CSV import only for the MVP (todo.md step 4) — no GUI grid yet. Validation here is
deliberately loud: a `source_pdf` that doesn't match an uploaded file, a duplicate
`row_id`, a missing identification field, or an unexpected extra column must be a
visible pre-run error, never a silently dropped/ignored row or column.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path


REQUIRED_COLUMNS = ("row_id", "source_pdf", "locator", "authors", "year")

# Not required, never validated (a row is complete without them) — carried
# through only as extra paper-identification signal for pdf_matching.py's
# assisted-matching suggestions, for the case where `source_pdf` is filled in
# late (or not at all yet) and a fuzzy filename/text match on authors+year
# alone isn't confident enough.
OPTIONAL_COLUMNS = ("title", "doi")

COLUMN_DESCRIPTIONS = {
    "row_id": "Effect ID — a short, unique identifier you invent for this effect/row.",
    "source_pdf": "Which uploaded PDF this effect comes from — must match a filename exactly.",
    "locator": "Effect location within the paper, e.g. \"Table 2, Experiment 1, DV: accuracy\".",
    "authors": "Paper identification — the study's author(s).",
    "year": "Paper identification — the study's publication year.",
    "title": "Optional — paper title, used to help suggest a source_pdf match.",
    "doi": "Optional — paper DOI, used to help suggest a source_pdf match.",
}


def coding_sheet_template_csv() -> str:
    """Return a ready-to-edit CSV with examples for every required column."""

    rows = (
        {
            "row_id": "effect-1",
            "source_pdf": "example-study.pdf",
            "locator": "Experiment 1, Table 2, accuracy",
            "authors": "Smith and Lee",
            "year": "2024",
        },
        {
            "row_id": "effect-2",
            "source_pdf": "example-study.pdf",
            "locator": "Experiment 2, Table 4, response time",
            "authors": "Smith and Lee",
            "year": "2024",
        },
        {
            "row_id": "effect-3",
            "source_pdf": "another-study.pdf",
            "locator": "Study 1, Figure 1, accuracy",
            "authors": "Garcia et al.",
            "year": "2023",
        },
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


@dataclass
class CodingSheetRow:
    row_id: str
    source_pdf: str
    locator: str
    authors: str = ""
    year: str = ""
    title: str = ""
    doi: str = ""


@dataclass
class CodingSheetIssue:
    row_number: int | None
    message: str
    # "pdf" for issues about a row's source_pdf not identifying an uploaded
    # file (missing or unmatched) — the PDF matching tab's concern.
    # "sheet" for everything else (shape/columns, row_id, other required
    # fields) — the Coding sheet tab's concern. Purely a display grouping;
    # `is_valid` treats every issue the same regardless of kind.
    kind: str = "sheet"


@dataclass
class CodingSheet:
    rows: list[CodingSheetRow]
    issues: list[CodingSheetIssue]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def sheet_issues(self) -> list[CodingSheetIssue]:
        return [issue for issue in self.issues if issue.kind == "sheet"]

    @property
    def pdf_issues(self) -> list[CodingSheetIssue]:
        return [issue for issue in self.issues if issue.kind == "pdf"]

    def rows_for_pdf(self, source_pdf: str) -> list[CodingSheetRow]:
        return [row for row in self.rows if row.source_pdf == source_pdf]

    def pdfs_with_no_rows(self, uploaded_filenames: set[str]) -> set[str]:
        covered = {row.source_pdf for row in self.rows}
        return uploaded_filenames - covered


def coding_sheet_reader(text: str) -> csv.DictReader:
    """Use the same normalized headers for validation and PDF matching."""
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff")), strict=True)
    reader.fieldnames = [name.strip() for name in (reader.fieldnames or [])]
    return reader


def parse_coding_sheet_csv(text: str, *, uploaded_filenames: set[str]) -> CodingSheet:
    issues: list[CodingSheetIssue] = []
    try:
        reader = coding_sheet_reader(text)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError("Duplicate coding-sheet column headers.")
        records = list(reader)
        for record in records:
            if None in record or any(value is None for value in record.values()):
                raise ValueError("A coding-sheet row has the wrong number of cells.")
    except (csv.Error, ValueError) as exc:
        return CodingSheet(rows=[], issues=[CodingSheetIssue(None, str(exc))])

    missing_columns = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
    if missing_columns:
        issues.append(
            CodingSheetIssue(
                row_number=None,
                message=f"Missing required column(s): {', '.join(missing_columns)}.",
            )
        )
        return CodingSheet(rows=[], issues=issues)

    allowed_columns = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
    extra_columns = [col for col in fieldnames if col not in allowed_columns]
    if extra_columns:
        issues.append(
            CodingSheetIssue(
                row_number=None,
                message=(
                    f"Unexpected column(s): {', '.join(extra_columns)}. The coding sheet "
                    f"only accepts {', '.join(allowed_columns)} — remove the rest."
                ),
            )
        )
        return CodingSheet(rows=[], issues=issues)

    rows: list[CodingSheetRow] = []
    seen_ids: dict[str, int] = {}
    for line_number, record in enumerate(records, start=2):  # header is line 1
        row_id = (record.get("row_id") or "").strip()
        source_pdf = (record.get("source_pdf") or "").strip()
        locator = (record.get("locator") or "").strip()
        authors = (record.get("authors") or "").strip()
        year = (record.get("year") or "").strip()

        if not row_id:
            issues.append(CodingSheetIssue(line_number, "row_id is required."))
            continue
        if row_id in seen_ids:
            issues.append(
                CodingSheetIssue(
                    line_number,
                    f"row_id `{row_id}` is a duplicate (first used on row {seen_ids[row_id]}).",
                )
            )
            continue
        seen_ids[row_id] = line_number

        if not authors:
            issues.append(
                CodingSheetIssue(line_number, f"row_id `{row_id}` has no authors.")
            )
            continue

        if not year:
            issues.append(CodingSheetIssue(line_number, f"row_id `{row_id}` has no year."))
            continue

        if not source_pdf:
            issues.append(
                CodingSheetIssue(line_number, f"row_id `{row_id}` has no source_pdf.", kind="pdf")
            )
            continue
        if source_pdf not in uploaded_filenames:
            issues.append(
                CodingSheetIssue(
                    line_number,
                    f"row_id `{row_id}` references `{source_pdf}`, which has not been uploaded.",
                    kind="pdf",
                )
            )
            continue


        rows.append(
            CodingSheetRow(
                row_id=row_id,
                source_pdf=source_pdf,
                locator=locator,
                authors=authors,
                year=year,
                title=(record.get("title") or "").strip(),
                doi=(record.get("doi") or "").strip(),
            )
        )

    return CodingSheet(rows=rows, issues=issues)


def read_coding_sheet(path: Path, *, uploaded_filenames: set[str]) -> CodingSheet:
    if not path.is_file():
        return CodingSheet(rows=[], issues=[])
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return CodingSheet(rows=[], issues=[CodingSheetIssue(None, "Save the CSV as UTF-8 and try again.")])
    if not text.strip():
        return CodingSheet(rows=[], issues=[])
    return parse_coding_sheet_csv(text, uploaded_filenames=uploaded_filenames)
