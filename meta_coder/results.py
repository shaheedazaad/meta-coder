"""Collates per-PDF extraction results + the coding sheet into the two output
files (plan.md Divergence #2): a wide `coded_data.csv` and a parallel
`evidence.csv`, both keyed by the user's own `row_id`. Pure/testable — no I/O.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import yaml

from .coding_sheet import CodingSheet, CodingSheetRow
from .extraction import ExtractionResult
from .manual import CodingManual


BASE_COLUMNS = ("row_id", "source_pdf", "locator", "authors", "year", "status")


def collate_results(
    *,
    manual: CodingManual,
    coding_sheet: CodingSheet,
    results_by_pdf: dict[str, ExtractionResult],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    effect_columns = list(manual.effects)
    coded_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, str]] = []

    for row in coding_sheet.rows:
        result = results_by_pdf.get(row.source_pdf)
        status = result.status if result else "not_run"
        coded = (result.coded_by_row_id.get(row.row_id) if result else None) or {}

        base = {
            "row_id": row.row_id,
            "source_pdf": row.source_pdf,
            "locator": row.locator,
            "authors": row.authors,
            "year": row.year,
            "status": status,
        }

        coded_row = dict(base)
        evidence_row = dict(base)
        for field_name in effect_columns:
            field_value = coded.get(field_name) or {}
            value = field_value.get("value", "")
            coded_row[field_name] = "Not Reported" if value is None else str(value)
            evidence_row[field_name] = str(field_value.get("evidence", ""))

        coded_rows.append(coded_row)
        evidence_rows.append(evidence_row)

    return coded_rows, evidence_rows


def rows_to_csv(rows: list[dict[str, str]], manual: CodingManual) -> str:
    fieldnames = [*BASE_COLUMNS, *manual.effects]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def render_pdf_audit_yaml(
    *,
    manual: CodingManual,
    source_pdf: str,
    rows: list[CodingSheetRow],
    result: ExtractionResult,
) -> str:
    """One readable YAML file per PDF, with both codes and evidence together —
    the human audit trail. Wide coded_data.csv/evidence.csv are for joining into
    the user's own spreadsheet; this is for actually reading and checking one
    PDF's output, which is awkward to do across dozens of CSV columns."""

    effects: dict[str, Any] = {}
    for row in rows:
        coded = result.coded_by_row_id.get(row.row_id)
        entry: dict[str, Any] = {"locator": row.locator}
        if coded is None:
            entry["status"] = "not returned by the model"
        else:
            entry["fields"] = {
                field_name: {
                    "value": (coded.get(field_name) or {}).get("value"),
                    "evidence": (coded.get(field_name) or {}).get("evidence"),
                }
                for field_name in manual.effects
            }
        effects[row.row_id] = entry

    data: dict[str, Any] = {"source_pdf": source_pdf, "status": result.status}
    if result.audit_operation_id:
        data["audit_operation_id"] = result.audit_operation_id
    if result.repaired_response is not None:
        data["json_repaired"] = True
    if result.error:
        data["error"] = result.error
    if result.missing_ids:
        data["missing_row_ids"] = sorted(result.missing_ids)
    if result.extra_ids:
        data["unexpected_row_ids"] = sorted(result.extra_ids)
    data["effects"] = effects

    return yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


# OWASP CSV-injection guidance: spreadsheet apps evaluate cells starting with
# these characters as formulas. Tab and carriage return are always prefixed;
# the others also after leading whitespace, which some apps ignore.
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_CONTROL_PREFIXES = ("\t", "\r")
_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def spreadsheet_safe_cell(text: str) -> str:
    """Prefix formula-like text with an apostrophe so spreadsheets show it as
    text. Signed numbers such as `-1.25e-3` are left alone so they stay numeric."""

    if text.startswith(_CONTROL_PREFIXES):
        return "'" + text
    stripped = text.lstrip()
    if stripped.startswith(_FORMULA_PREFIXES) and not _NUMBER_RE.fullmatch(stripped):
        return "'" + text
    return text


def spreadsheet_csv(canonical_csv: str) -> bytes:
    """Re-encode a canonical results CSV for opening directly in Excel or
    LibreOffice: a UTF-8 BOM so non-ASCII text displays correctly, CRLF line
    endings, and formula-like cells (headers included) escaped. The canonical
    files themselves are never changed — they remain the exact research data."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in csv.reader(io.StringIO(canonical_csv, newline="")):
        if row:  # files written on Windows before CRLF handling was fixed contain blank lines
            writer.writerow([spreadsheet_safe_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


PROVENANCE_COLUMNS = ("row_id", "source_pdf", "provider", "model", "audit_operation_id")


def provenance_csv(coded_csv: str, results_by_pdf: dict[str, ExtractionResult]) -> str:
    """One row per row of `coded_data.csv`, recording the provider and model
    that produced that PDF's persisted result and the audit operation holding
    the full request/response. Results from mixed-provider retries are
    reported per PDF; results saved before this was recorded have blanks."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(PROVENANCE_COLUMNS)
    for row in csv.DictReader(io.StringIO(coded_csv, newline="")):
        source_pdf = row.get("source_pdf") or ""
        result = results_by_pdf.get(source_pdf) or ExtractionResult(source_pdf, "not_run")
        writer.writerow([
            row.get("row_id") or "", source_pdf,
            result.provider or "", result.model or "", result.audit_operation_id or "",
        ])
    return buffer.getvalue()
