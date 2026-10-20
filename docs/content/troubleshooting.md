# Troubleshooting

Start with the message shown next to the affected action. Most setup problems can be resolved without deleting a project or repeating successful extraction work.

## Run is blocked

Check that the effect definition is filled in, the coding manual is valid, and the coding sheet has no structural errors. Save or discard outstanding manual drafts. At least one row must be matched to an uploaded PDF.

See [Define your analysis](analysis.md), [Coding sheet](coding-sheet.md), and [PDF matching](matching.md).

## Missing studies in the output

Unmatched sheet rows and PDFs with no rows are skipped. Check **Needs PDF** and **Orphans** in PDF matching, then review the results table for failures. A normal run retains already successful PDFs.

## Provider errors or timeouts

Check the connection in [Settings](settings.md), the exact model ID, and the selected provider. For an OpenAI-compatible endpoint, verify its API base URL and supported JSON mode. Reduce parallelism or increase pacing for rate-limit errors; adjust the request timeout for slow responses.

Use raw responses and per-study status messages to identify the cause, then retry affected PDFs.

## Results need review

A response may be rejected when its row IDs do not match the coding sheet or it fails validation. A response is also held for review when the provider stopped early or its JSON needed repair; the status message gives the reason. Compare its values with the raw response and the PDF, and retry if they look truncated. Otherwise, inspect the raw response, clarify ambiguous locators or manual instructions, and retry. Export existing results first if you need to change the saved manual or sheet.

## Text is missing from a PDF

Image-only pages may contain no extractable text. OpenAI-compatible extraction sends extracted text rather than page images. Try a searchable or OCR-processed PDF, or a provider with native PDF support.

## Unsaved edits or an unfinished draft

Save edited sections before starting extraction or leaving the page. AI drafts remain unsaved until explicitly accepted. If background processing finishes while you are editing, save your changes and use the refresh link to load the latest results and matches.
