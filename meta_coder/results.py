"""Collates per-PDF extraction results + the coding sheet into the two output
files (plan.md Divergence #2): a wide `coded_data.csv` and a parallel
`evidence.csv`, both keyed by the user's own `row_id`. Pure/testable — no I/O.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import yaml

from .coding_sheet import CodingSheet, CodingSheetRow
from .extraction import ExtractionResult
from .manual import BASE_COLUMNS, MULTIPLE_SEPARATOR, CodingManual, FieldSpec


def _field_mapping(coded: dict, field_name: str) -> dict:
    """Malformed model cells stay in raw JSON, but cannot break exports."""
    value = coded.get(field_name)
    return value if isinstance(value, dict) else {}


# How each `missing` code (mechanism.MISSING_CODES) reads in coded_data.csv. A
# null value without a code, as in results saved before the codes existed,
# stays "Not Reported".
MISSING_LABELS = {
    "not_reported": "Not Reported",
    "not_applicable": "Not Applicable",
    "unclear": "Unclear",
}


def _missing_label(field_value: dict) -> str:
    code = field_value.get("missing")
    return MISSING_LABELS.get(code if isinstance(code, str) else "", "Not Reported")


def _cell_text(value: object, spec: FieldSpec) -> str:
    """A coded value as CSV text. The levels selected for a `multiple` field
    share one cell, in the manual's level order whatever order the model used."""

    if not isinstance(value, list):
        return str(value)
    order = {level.value: index for index, level in enumerate(spec.levels)}
    items = sorted((str(item) for item in value), key=lambda item: order.get(item, len(order)))
    return MULTIPLE_SEPARATOR.join(items)


def cells_to_check(coded_by_row_id: object) -> int:
    """How many coded cells a person should look at first: currently those the
    model marked `unclear`. Never affects a PDF's status."""

    if not isinstance(coded_by_row_id, dict):
        return 0
    return sum(
        isinstance(cell, dict) and cell.get("value") is None and cell.get("missing") == "unclear"
        for coded in coded_by_row_id.values()
        if isinstance(coded, dict)
        for cell in coded.values()
    )


def _audit_field(field_value: dict) -> dict[str, Any]:
    entry: dict[str, Any] = {"value": field_value.get("value")}
    if entry["value"] is None and "missing" in field_value:
        entry["missing"] = field_value["missing"]
    entry["evidence"] = field_value.get("evidence")
    return entry


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
            field_value = _field_mapping(coded, field_name)
            value = field_value.get("value", "")
            coded_row[field_name] = (
                _missing_label(field_value)
                if value is None
                else _cell_text(value, manual.effects[field_name])
            )
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
                field_name: _audit_field(_field_mapping(coded, field_name))
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
    if to_check := cells_to_check(result.coded_by_row_id):
        data["cells_to_check"] = to_check
    data["effects"] = effects

    return yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)
