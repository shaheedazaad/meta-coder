# Sources

Upload the study reports that contain the effects listed in your coding sheet, as PDF files or as XML full text. Use recognizable filenames so you can match and review them later.

[![Sources with the upload control and a demonstration study](assets/screenshots/sources.png)](assets/screenshots/sources.png)

## Add studies

Open **Sources**, choose one or more PDF or XML files, and upload them. A project can mix both. The list shows the files stored in the project. A filename in the coding sheet's `source_pdf` column must exactly match an uploaded filename, including `.pdf` or `.xml`; the column keeps its name whatever the format.

Next, open [Source matching](matching.md) to resolve missing matches. A source with no coding-sheet rows is not processed during extraction.

## XML (JATS) full text

Many publishers and PubMed Central provide articles as JATS XML. Using the XML avoids PDF parsing errors and keeps the article's structure.

- **Upload checks.** An XML file must be UTF-8 text that starts with markup (`<`). Empty, binary or differently encoded files are rejected with a message saying what to fix.
- **What the model receives.** Every provider receives the XML file's contents as text, markup included, after the instructions. MetaCoder does not parse, shorten or convert it, and no file upload or PDF parser is involved. See [Privacy](privacy.md) for where it goes.
- **Evidence locations.** An XML article has no page numbers, so for XML sources the model is asked to cite the section heading path (for example `Results > Study 2`) or a table's label, row and column instead, with a short quotation.
- **Matching.** For JATS files, matching reads the article's title, author surnames, publication year and DOI from the front matter. A DOI in the coding sheet that equals the article's DOI is a certain match, even when the filename says nothing about the study. See [Source matching](matching.md).
- **Size.** Markup takes tokens too, so an XML article usually costs more input tokens than the same article's text. A very long article can exceed a model's context window; the provider then reports an error for that source.

Other publishers' XML formats can be uploaded too. They are sent to the model in the same way; matching then uses the opening text of the file instead of JATS front matter.

## Check document quality

For OpenAI-compatible extraction, MetaCoder extracts a PDF's text on your computer and adds page numbers. Image-only scans and figures may not provide enough text. Use searchable PDFs, OCR the documents, use the article's XML, or choose an appropriate provider with native PDF support.

## Remove a source

Use the file's removal control only when it is no longer needed. Review affected coding-sheet rows afterward: removing a source can leave rows without a matching source. Keep a [project backup](projects.md) before reorganizing a completed project.
