# Review and export

Use **Results** to inspect processed studies, investigate failures, and download coded data alongside its evidence. Check a sample of values against the original PDFs before using the exports.

[![The Results tab before the first extraction run](assets/screenshots/results.png)](assets/screenshots/results.png)

*The demonstration project has not called a provider; this screenshot shows the initial empty state.*

## Review each study

The results table reports each PDF's status and available usage information. Open the raw response when you need to understand a provider error or a response that needs review. Readable per-PDF YAML records make it easier to inspect coded effects and supporting quotations.

Check that each `row_id` corresponds to the intended experiment or condition, that categories follow your manual, and that cited pages and quotations support the values. Start with the cells counted under **cells to check** in the results table: values the model marked `Unclear`, values it rated with low confidence, and values whose quote was not found in the PDF.

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

## Confidence ratings

If the manual [asks for confidence ratings](manual.md#confidence-ratings), **Results** offers `confidence.csv`. It has the same rows and columns as `coded_data.csv`, with each coded cell replaced by `high`, `medium` or `low`. A rating on a missing value says how sure the model is of the reason, for example that the value really is not reported. The `notes` column is blank, as are rows that have not been coded.

Cells rated `low` are added to each PDF's **cells to check** count, together with `Unclear` cells. As with `Unclear`, a low rating does not change a PDF's status or cause it to be recoded.

## Quote checks

For each coded value, the model is asked for the passage that supports it, copied word for word, and the page it is on. The cell in `evidence.csv` shows them ahead of the model's explanation, for example `p. 4: "Participants were 124 undergraduate students" — Method, Participants`. A value read from a table or figure has no quote.

After each response, MetaCoder searches for every quote in the text stored in your PDF. The comparison ignores differences that come from the PDF's layout: line breaks, words hyphenated at the end of a line, spacing, capitalization, ligatures such as “ﬁ”, and the style of quotation marks and dashes. Numbers are compared strictly, including decimal points and minus signs. `quote_check.csv` has the same rows and columns as `coded_data.csv` and reports the outcome for each cell:

| Cell text | Meaning | What to do |
| --- | --- | --- |
| `verified` | The quote is in the PDF's text. | The quote exists. You still need to judge whether it supports the value. |
| `approximate` | A passage matches at least 90% of the quote and all of its digits. Typical causes are a misread letter in a scanned PDF or a word the model left out. | Compare the quote with the passage when the wording matters. |
| `not_found` | The PDF has readable text, and no passage matches. The model may have paraphrased, combined separate passages, or invented the quote. | Check this value in the PDF yourself. |
| `not_checked` | The quote could not be checked: it is too short to be meaningful (fewer than 12 letters, digits and signs), or the PDF has no readable text where the quote should be. | Check the value as you would without this feature. |
| blank | The model gave no quote, or the row has not been coded. | |

The per-PDF YAML also records the page on which each quote was found.

Cells marked `not_found` are added to each PDF's **cells to check** count. They do not change a PDF's status and do not cause it to be recoded.

Keep three limits in mind:

- A check confirms that a quote exists in the PDF. It does not confirm that the coded value follows from it.
- Scanned PDFs without a text layer cannot be checked, so their quotes are `not_checked`. Gemini and OpenRouter models read the pages directly and can still code such PDFs; running OCR first makes their quotes checkable.
- With Gemini and OpenRouter, the model reads the PDF itself, while the check reads the PDF's stored text. A correct quote can therefore be `not_found` when the two differ, for example across a column or page break. With an OpenAI-compatible endpoint, the model and the check read the same extracted text.

Results coded before quote checks existed have blank cells throughout.

## Download the data

| Export | Purpose |
| --- | --- |
| `coded_data.csv` | Coded values arranged by effect row |
| `evidence.csv` | Supporting page and quote information for the coded cells |
| `quote_check.csv` | Whether each supporting quote was found in the PDF's text |
| `confidence.csv` | The model's confidence in each coded cell, when the manual asks for it |
| Per-PDF YAML | Readable coded effects and evidence for a single study |
| Audit ZIP | Complete project export with persistent request/response history, provenance, and checksums |

Keep the coded data and evidence together. Investigate missing values and needs-review results before treating an export as complete. Use the [retry controls](run.md#cancel-and-retry) for failed studies after addressing the cause.

## Preserve a run

Use **Download audit ZIP** to retain the full [audit and reproducibility record](auditing.md). CSV exports alone do not contain complete provenance.

Download results or a project ZIP before changing the saved manual or coding sheet: those changes clear working extraction results. The persistent audit history retains earlier requests, inputs, responses, and recorded run exports. See [Project settings](projects.md) for backup and cleanup controls.
