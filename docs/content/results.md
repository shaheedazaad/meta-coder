# Review and export

Use **Results** to inspect processed studies, investigate failures, and download coded data alongside its evidence. Check a sample of values against the original PDFs before using the exports.

[![The Results tab before the first extraction run](assets/screenshots/results.png)](assets/screenshots/results.png)

*The demonstration project has not called a provider; this screenshot shows the initial empty state.*

## Review each study

The results table reports each PDF's status and available usage information. Output token counts include any thinking or reasoning tokens the provider bills as output. An **outdated** status means the inputs changed after the PDF was coded; the CSV files keep the last run's values until you [run extraction](run.md#cancel-and-retry) again. Open the raw response (**view raw output (YAML)** in the Detail column) when you need to understand a provider error or a response that needs review. If a needs-review PDF's retry failed, its row still shows the needs-review result, followed by **Last retry failed:** and the error; its values stay in the CSV files. Readable per-PDF YAML records make it easier to inspect coded effects and supporting quotations: open one with **view coded effects (YAML)** in the same column. The link appears once the PDF has been processed, and the file is also saved in the project's `output/coded/` folder.

Check that each `row_id` corresponds to the intended experiment or condition, that categories follow your manual, and that cited pages and quotations support the values. For a systematic check against human coding, see [Validating the coding](validation.md). The token columns help you [estimate cost](costs.md#estimate-before-a-full-run).

## Work with several PDFs at once

Use the **Show** menu above the table to list only the PDFs with one status, such as Failed or Needs review. Each option shows how many PDFs have that status. Tick the box beside a PDF to select it. The box in the table header selects every PDF that matches the current filter, on every page. Your selection is kept while you change pages or filters in the same browser tab.

With PDFs selected, a bar above the table offers:

- **Retry selected** codes exactly the selected PDFs again with the current Run settings, whatever their status. You can use it to recode PDFs that finished **ok** after you notice a problem. If a new attempt fails, a PDF that was coded successfully keeps its earlier result. The table then shows the failed attempt and notes that the earlier successful result is still used.
- **Delete output** deletes the selected PDFs' saved output: their raw responses and per-PDF YAML. `coded_data.csv`, `evidence.csv` and `confidence.csv` are rebuilt from the PDFs that remain, so the deleted PDFs' rows are left blank, marked `not_run`. The source PDFs and coding sheet are kept, and the next run codes them again. The [audit history](auditing.md) records the deletion and keeps a copy of each deleted file.

Both actions ask you to confirm first. Neither is available while a run is active.

## Confidence ratings

If the manual [asks for confidence ratings](manual.md#confidence-ratings), **Results** offers `confidence.csv`. It has the same rows and columns as `coded_data.csv`, with each coded cell replaced by `high`, `medium` or `low`. For a value that is not reported, the rating says how sure the model is that the article really does not report it. The `notes` column is blank, as are rows that have not been coded. Each field's rating also appears in the per-PDF YAML.

The Detail column of the results table counts each PDF's **low-confidence cells**. Start your checks there. A low rating does not change a PDF's status or cause it to be recoded.

## Download the data

| Export | Purpose |
| --- | --- |
| `coded_data.csv` | Coded values arranged by effect row |
| `evidence.csv` | Same rows and columns as `coded_data.csv`, with the supporting page and quote for each coded cell |
| `confidence.csv` | The model's confidence in each coded cell, when the manual asks for it |
| `provenance.csv` | Provider, model and audit operation behind each row of `coded_data.csv`; blank for results saved by earlier versions |
| Spreadsheet copies | `coded_data.csv`, `evidence.csv` and (when present) `confidence.csv` for opening in Excel or LibreOffice: text that could run as a formula (starting with `=`, `+`, `-` or `@`) begins with `'`, and accented characters display correctly. The `'` is part of the file, so some apps keep it visible at the start of the cell. Use the plain files for analysis |
| Per-PDF YAML | Readable coded effects and evidence for a single study |
| Audit ZIP | Complete project export with persistent request/response history, provenance, and checksums |

Keep the coded data and evidence together. Investigate missing values and needs-review results before treating an export as complete. Use the [retry controls](run.md#cancel-and-retry) for failed studies after addressing the cause.

## Preserve a run

Use **Download audit ZIP** to retain the full [audit and reproducibility record](auditing.md). CSV exports alone do not contain complete provenance.

Download results or a project ZIP before changing the saved manual or coding sheet: those changes clear working extraction results. The persistent audit history retains earlier requests, inputs, responses, and recorded run exports. See [Project settings](projects.md) for backup and cleanup controls.
