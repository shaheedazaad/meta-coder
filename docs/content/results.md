# Review and export

Use **Results** to inspect processed studies, investigate failures, and download coded data alongside its evidence. Check a sample of values against the original PDFs before using the exports.

[![The Results tab before the first extraction run](assets/screenshots/results.png)](assets/screenshots/results.png)

*The demonstration project has not called a provider; this screenshot shows the initial empty state.*

## Review each study

The results table reports each PDF's status and available usage information. Output token counts include any thinking or reasoning tokens the provider bills as output. An **outdated** status means the inputs changed after the PDF was coded; the CSV files keep the last run's values until you [run extraction](run.md#cancel-and-retry) again. Open the raw response (**view raw output (YAML)** in the Detail column) when you need to understand a provider error or a response that needs review. Readable per-PDF YAML records make it easier to inspect coded effects and supporting quotations: open one with **view coded effects (YAML)** in the same column. The link appears once the PDF has been processed, and the file is also saved in the project's `output/coded/` folder.

Check that each `row_id` corresponds to the intended experiment or condition, that categories follow your manual, and that cited pages and quotations support the values. For a systematic check against human coding, see [Validating the coding](validation.md). The token columns help you [estimate cost](costs.md#estimate-before-a-full-run).

## Confidence ratings

If the manual [asks for confidence ratings](manual.md#confidence-ratings), **Results** offers `confidence.csv`. It has the same rows and columns as `coded_data.csv`, with each coded cell replaced by `high`, `medium` or `low`. For a value that is not reported, the rating says how sure the model is that the article really does not report it. The `notes` column is blank, as are rows that have not been coded. Each field's rating also appears in the per-PDF YAML.

The Detail column of the results table counts each PDF's **low-confidence cells**. Start your checks there. A low rating does not change a PDF's status or cause it to be recoded.

## Download the data

| Export | Purpose |
| --- | --- |
| `coded_data.csv` | Coded values arranged by effect row |
| `evidence.csv` | Supporting page and quote information for the coded cells |
| `confidence.csv` | The model's confidence in each coded cell, when the manual asks for it |
| `provenance.csv` | Provider, model and audit operation behind each row of `coded_data.csv`; blank for results saved by earlier versions |
| Spreadsheet copies | `coded_data.csv`, `evidence.csv` and (when present) `confidence.csv` for opening in Excel or LibreOffice: text that could run as a formula (starting with `=`, `+`, `-` or `@`) begins with `'`, and accented characters display correctly. Use the plain files for analysis |
| Per-PDF YAML | Readable coded effects and evidence for a single study |
| Audit ZIP | Complete project export with persistent request/response history, provenance, and checksums |

Keep the coded data and evidence together. Investigate missing values and needs-review results before treating an export as complete. Use the [retry controls](run.md#cancel-and-retry) for failed studies after addressing the cause.

## Preserve a run

Use **Download audit ZIP** to retain the full [audit and reproducibility record](auditing.md). CSV exports alone do not contain complete provenance.

Download results or a project ZIP before changing the saved manual or coding sheet: those changes clear working extraction results. The persistent audit history retains earlier requests, inputs, responses, and recorded run exports. See [Project settings](projects.md) for backup and cleanup controls.
