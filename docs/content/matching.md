# Source matching

Source matching connects coding-sheet rows to uploaded studies. Review suggested matches before accepting them, especially when authors or publication years overlap.

[![Source matching with the needs a source, suggestions, matches, and orphan sections](assets/screenshots/matching.png)](assets/screenshots/matching.png)

## Review the matching sections

| Section | What to check |
| --- | --- |
| Needs a source | Sheet entries still missing an uploaded study |
| Suggestions | Candidate matches based on available file and paper information |
| Matches | The saved connections between sheet entries and sources |
| Orphaned sources | Uploaded sources with no coding-sheet rows |

Background scanning gathers matching information after uploads. Let it finish, review the suggested source for each paper, and save the appropriate matches. Only high-confidence suggestions are preselected. Medium and low suggestions are marked “(suggested)” in the list but stay unselected until you choose them, so **Apply this page** saves only the matches you can see selected. A suggestion is shown as low when the source's publication year differs from the sheet's, or when another source or paper fits almost as well. Confirm that each locator actually points to the intended effect in that source.

For an XML (JATS) source, matching uses the title, author surnames, publication year and DOI from the article's front matter rather than its filename alone. When the sheet's optional `doi` column equals the article's DOI, the suggestion is certain; when the two DOIs differ, that source is not suggested with medium or high confidence. PDFs are matched from their filename and first page (or [GROBID](settings.md), if configured), which reads PDFs only.

## Fix a wrong match

Remove the incorrect saved match, then select the correct uploaded source. If the study is missing entirely, add it in [Sources](sources.md) and return here.

## Before running

At least one coding-sheet row must match an uploaded source. Unmatched rows and orphaned sources are skipped when you run extraction. Resolve all intended matches if you want complete coverage of your sheet.
