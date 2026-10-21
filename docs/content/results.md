# Review and export

Use **Results** to inspect processed studies, investigate failures, and download coded data alongside its evidence. Check a sample of values against the original PDFs before using the exports.

[![The Results tab before the first extraction run](assets/screenshots/results.png)](assets/screenshots/results.png)

*The demonstration project has not called a provider; this screenshot shows the initial empty state.*

## Review each study

The results table reports each PDF's status and available usage information. Open the raw response when you need to understand a provider error or a response that needs review. Readable per-PDF YAML records make it easier to inspect coded effects and supporting quotations.

Check that each `row_id` corresponds to the intended experiment or condition, that categories follow your manual, and that cited pages and quotations support the values. For a systematic check against human coding, see [Validating the coding](validation.md). The token columns help you [estimate cost](costs.md#estimate-before-a-full-run).

## Download the data

| Export | Purpose |
| --- | --- |
| `coded_data.csv` | Coded values arranged by effect row |
| `evidence.csv` | Supporting page and quote information for the coded cells |
| Per-PDF YAML | Readable coded effects and evidence for a single study |
| Audit ZIP | Complete project export with persistent request/response history, provenance, and checksums |

Keep the coded data and evidence together. Investigate missing values and needs-review results before treating an export as complete. Use the [retry controls](run.md#cancel-and-retry) for failed studies after addressing the cause.

## Preserve a run

Use **Download audit ZIP** to retain the full [audit and reproducibility record](auditing.md). CSV exports alone do not contain complete provenance.

Download results or a project ZIP before changing the saved manual or coding sheet: those changes clear working extraction results. The persistent audit history retains earlier requests, inputs, responses, and recorded run exports. See [Project settings](projects.md) for backup and cleanup controls.
