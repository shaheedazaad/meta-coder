# Run extraction

Once your manual and coding sheet are ready, use **Run** to select a provider, save the extraction settings, and process matched PDFs.

[![Extraction provider, model, and run controls](assets/screenshots/run.png)](assets/screenshots/run.png)

## Prepare the run

1. Save changes to the analysis definition, coding manual, and coding sheet.
2. Review source matches and resolve any blocking messages on **Run**.
3. Choose a provider and model. Use **Manage connection** if its key or endpoint is missing.
4. Choose **Save run settings**, then start extraction.

Each request includes all coding-sheet rows for one PDF. The response must include the exact row IDs requested; an inconsistent response is marked for review rather than accepted as a partial guess. A response that the provider cut short (for example at its output token limit or by a content filter), or whose JSON had to be repaired, is also marked for review: its values are kept but may be truncated. A response with no usable text at all is reported as an error instead, as is an OpenRouter PDF whose converted text is still empty after one retry with a different parser.

## Control throughput

Expand **Advanced run settings** to adjust parallel requests, request delay, timeout, and [maximum output](#maximum-output). Lower concurrency or add delay if the provider rejects requests because of rate limits. Provider-specific controls include Gemini service tier, reasoning effort for the other supported provider types, and, for OpenRouter, which backend provider serves the model and whether requests may fall back to others (see [Route OpenRouter requests to one provider](settings.md#route-openrouter-requests-to-one-provider)).

### Maximum output

**Maximum output (tokens)**, also under **Advanced run settings**, is the most the model may write for one article, reasoning or thinking included. A value you enter is sent as entered. Leave it empty to use the provider's own limit:

- **Gemini** uses the model's maximum.
- **OpenRouter** uses the lowest limit among the providers that serve the model with structured output, looked up when each run or retry starts. If the project [routes requests to one provider](settings.md#route-openrouter-requests-to-one-provider) with fallbacks off, only that provider's limits count. Some providers default to a low cap if none is sent. Where a provider states its context length, its limit leaves room in that context for the article (a quarter of the context, at least 32,768 tokens); a provider whose context is too small for that is left out. If the lookup fails, the run shows a warning and sends no limit, so OpenRouter's default applies.
- **OpenAI-compatible** servers use their own default.

The value each run sent is recorded in its [audit history](auditing.md). An article with many rows can need a lot of output. If articles need review with `finish reason: length`, raise the maximum output or lower the reasoning effort, then retry them. Changing the maximum output does not mark successful results as outdated.

To estimate spending before processing every PDF, see [Cost](costs.md). Extraction settings are saved per project. They are separate from the global model used to draft manuals or convert sheets.

## Cancel and retry

Progress updates while the run is active. While a run is active or cancelling, MetaCoder refuses changes to the coding manual, coding sheet, source matches, sources, and generated data, so results always match the inputs they were coded from; wait for the run to finish or cancel it first. Cancelling stops queued PDFs from starting and interrupts requests already in flight; those PDFs are marked cancelled. A provider may still finish, and charge for, a request it had already received. PDFs coded before you cancelled are kept. Retry failed or needs-review PDFs individually from the results table, or use the bulk retry control. To recode a chosen set of PDFs, including ones that finished **ok**, select them on Results and use [**Retry selected**](results.md#work-with-several-pdfs-at-once).

Starting another run processes matched PDFs still needing coding; it does not automatically reprocess successful studies. A successful result is marked **outdated** and coded again if its PDF, its coding-sheet row IDs or locators, the coding manual, or the provider and model settings have changed since. Pacing, timeout, Gemini service tier and OpenRouter provider routing do not count as changes. Outdated results stay in the project folder but are left out of new exports; they count again if you restore the earlier inputs. To intentionally rerun everything, export what you need and clear generated data in [Project settings](projects.md). To rerun only some PDFs, [delete their output](results.md#work-with-several-pdfs-at-once) on Results.

A failed retry never replaces a successful result. The results table shows the latest attempt, while the earlier success remains in the exports.

A failed or cancelled retry doesn't replace a needs-review result either, so the values awaiting review stay in the exports. The results table keeps showing the needs-review result with **Last retry failed:** and the error beside its review reasons. A retry that returns a new result, even one that again needs review, replaces it. Every attempt, including the failed one, is still kept in the project's records.

## Review the output

Open [Results](results.md) after processing. A completed request is not a substitute for checking the extracted values and evidence.
