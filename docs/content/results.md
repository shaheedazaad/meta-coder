# Review and export

Use **Results** to inspect processed studies, investigate failures, and download coded data alongside its evidence. Check a sample of values against the original PDFs before using the exports.

[![The Results tab before the first extraction run](assets/screenshots/results.png)](assets/screenshots/results.png)

*The demonstration project has not called a provider; this screenshot shows the initial empty state.*

## Review each study

The results table reports each PDF's status and available usage information. Open the raw response when you need to understand a provider error or a response that needs review. Readable per-PDF YAML records make it easier to inspect coded effects and supporting quotations.

Check that each `row_id` corresponds to the intended experiment or condition, that categories follow your manual, and that cited pages and quotations support the values.

## Missing values

When MetaCoder cannot code a value, the cell in `coded_data.csv` says why:

| Cell text | Meaning |
| --- | --- |
| `Not Reported` | The article does not report the value. |
| `Not Applicable` | The field does not apply to this study or effect, such as a follow-up interval for a study without a follow-up. |
| `Unclear` | The article addresses the field, but ambiguously or inconsistently, so no single value could be determined. |

The matching cell in `evidence.csv` explains the reason. Recode these three texts as missing values before analysis, and decide for each field whether `Not Applicable` belongs in the analysis as its own category.

`Unclear` cells are the ones most worth reading yourself. The results table shows how many each PDF has as **cells to check**, and the per-PDF YAML records the same count. This count does not change a PDF's status, and a later run does not recode the PDF because of it.

Results coded before these three reasons existed show `Not Reported` for every missing value.

## Fields with several categories

For a field that [allows several categories](manual.md#fields-where-several-categories-apply), the cell in `coded_data.csv` lists every selected category, separated by `; ` and in the order the manual defines them, for example `survey; interview`. Split the cell on `; ` to analyse the categories separately. If no category applies, the cell holds one of the [missing-value texts](#missing-values).

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
