# Run extraction

Once your manual and coding sheet are ready, use **Run** to select a provider, save the extraction settings, and process matched PDFs.

[![Extraction provider, model, and run controls](assets/screenshots/run.png)](assets/screenshots/run.png)

## Prepare the run

1. Save changes to the analysis definition, coding manual, and coding sheet.
2. Review PDF matches and resolve any blocking messages on **Run**.
3. Choose a provider and model. Use **Manage connection** if its key or endpoint is missing.
4. Choose **Save run settings**, then start extraction.

Each request includes all coding-sheet rows for one PDF. The response must include the exact row IDs requested; an inconsistent response is marked for review rather than accepted as a partial guess.

## Control throughput

Expand **Advanced run settings** to adjust parallel requests, request delay, and timeout. Lower concurrency or add delay if the provider rejects requests because of rate limits. Provider-specific controls include Gemini service tier and reasoning effort for the other supported provider types.

Extraction settings are saved per project. They are separate from the global model used to draft manuals or convert sheets.

## Cancel and retry

Progress updates while the run is active. Cancellation stops queued work; requests already in flight may finish. Successful PDFs are retained. Retry failed or needs-review PDFs individually from the results table, or use the bulk retry control.

Starting another run processes matched PDFs still needing coding; it does not automatically reprocess successful studies. A successful result is marked **outdated** and coded again if its PDF, its coding-sheet row IDs or locators, the coding manual, or the provider and model settings have changed since. Outdated results stay in the project folder but are left out of new exports; they count again if you restore the earlier inputs. To intentionally rerun everything, export what you need and clear generated data in [Project settings](projects.md).

A failed retry never replaces a successful result. The results table shows the latest attempt, while the earlier success remains in the exports.

## Review the output

Open [Results](results.md) after processing. A completed request is not a substitute for checking the extracted values and evidence.
