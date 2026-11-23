# Source PDFs

Upload the study reports that contain the effects listed in your coding sheet. Use recognizable filenames so you can match and review them later.

[![Source PDFs with the upload control and a demonstration study](assets/screenshots/sources.png)](assets/screenshots/sources.png)

## Add studies

Open **Source PDFs**, choose one or more PDF files, and upload them. The list shows the files stored in the project. A filename in the coding sheet must exactly match an uploaded filename.

If a new file has the same name as one already in the project but different contents, it is saved with a numbered suffix, such as `paper (2).pdf`, rather than replacing the original. A file whose contents are identical to a PDF already in the project is skipped, even under a different name, and so is a repeat of a file earlier in the same upload. A message on **Source PDFs** lists the skipped files, for example: `Added 1 file. Skipped 2 already in this project: a.pdf, copy.pdf.`

Next, open [PDF matching](matching.md) to resolve missing matches. A PDF with no coding-sheet rows is not processed during extraction.

## Check document quality

For OpenAI-compatible extraction, MetaCoder extracts text on your computer and adds page numbers. Image-only scans and figures may not provide enough text. Use searchable PDFs, OCR the documents, or choose an appropriate provider with native PDF support.

## Remove a source

Use the file's removal control only when it is no longer needed. Review affected coding-sheet rows afterward: removing a source can leave rows without a matching PDF. Keep a [project backup](projects.md) before reorganizing a completed project.
