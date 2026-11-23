# Coding sheet

The coding sheet specifies the effects to extract. Use one row per effect, experiment, or condition, even when several rows belong to the same PDF.

[![A saved coding sheet with two effect rows](assets/screenshots/coding-sheet.png)](assets/screenshots/coding-sheet.png)

## Prepare a formatted CSV

Download the CSV template from **Coding sheet**, fill it in, and use **Upload formatted CSV**. Required columns are:

| Column | What to enter |
| --- | --- |
| `row_id` | A unique, stable ID for this effect, such as `study-01-exp-1` |
| `source_pdf` | The exact uploaded filename, including `.pdf` |
| `locator` | Where the effect is reported, such as “Experiment 1, Table 2, response time” |
| `authors` | The study authors |
| `year` | The publication year |

Optional `title` and `doi` columns help suggest PDF matches. Preserve the required column names. Use a UTF-8 CSV.

```csv
row_id,source_pdf,locator,authors,year
study-01-exp-1,demo-study.pdf,"Experiment 1, Table 2",Demo and Example,2024
study-01-exp-2,demo-study.pdf,"Experiment 2, Table 4",Demo and Example,2024
```

## Convert an existing sheet

Choose **AI conversion** if your CSV uses another layout. Add notes explaining its columns or intended effect rows, choose the file, and select **Convert to a draft**. Conversion uses the saved manual and the drafting extractor chosen in Settings.

Review the preview and warnings. Edit the CSV if necessary without changing the required headers, then choose **Save converted sheet**. Fill in missing authors and years before saving. Conversion accepts up to 2,000 source rows and 500,000 characters.

## Resolve problems

Duplicate row IDs and missing required values need to be fixed in the sheet. Missing or unrecognized PDF filenames can be resolved in [PDF matching](matching.md) after uploading the studies.

An unsaved converted draft leaves the current sheet and results intact. Saving a changed sheet clears previous extraction results.
