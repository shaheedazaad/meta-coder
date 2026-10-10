# Cost

MetaCoder is free, but hosted providers charge for AI requests. Costs are usually modest per study, yet they add up across many PDFs, retries, and repeated runs. Run a small batch first and check the usage before processing everything.

## What drives cost

Providers charge per token, a unit of text roughly the size of a short word. Input tokens (what is sent) and output tokens (what the model writes) are priced separately, and output usually costs more.

- **One request per PDF.** Each extraction request contains one complete PDF and all of its coding-sheet rows, so long articles cost more than short ones.
- **PDF length.** Gemini and OpenRouter models with native PDF input count each page as tokens, including figures and tables. OpenAI-compatible endpoints receive extracted text instead.
- **Coding manual size.** The instructions and field descriptions are repeated in every request, so a long manual adds to every PDF.
- **Output size.** More rows, more fields, and longer evidence quotes increase output tokens.
- **Reasoning.** Models that "think" before answering are billed for those reasoning tokens as output. A higher **Reasoning effort** setting usually increases cost.
- **Service tier.** For Gemini, the `flex` service tier (MetaCoder's default) costs less than `standard` but may respond more slowly.
- **Retries and re-runs.** OpenRouter and OpenAI-compatible requests are retried automatically after temporary errors; each attempt may be charged. Retrying a study, or clearing results after changing the manual or coding sheet, sends the PDFs again.
- **Drafting.** AI manual drafting and coding sheet conversion are separate, usually small, requests.

OpenRouter also charges for its own PDF conversion when a model lacks native file input; see its [PDF documentation](https://openrouter.ai/docs/guides/overview/multimodal/pdfs).

## Estimate before a full run

1. Upload only three to five typical PDFs at first, including a long one, and run extraction. A run processes only matched PDFs, so the remaining coding-sheet rows are skipped for now. Upload the other PDFs later; the next run codes only studies that still need coding.
2. Open **Results** and read the **Input tokens** and **Output tokens** columns. The same figures are stored with each response in the [audit history](auditing.md).
3. Multiply the average per PDF by the number of PDFs and apply your provider's current prices. Add a margin for retries and re-runs.
4. Compare the estimate with your provider's billing or usage page, which shows the actual charge.

## Example

The following example uses prices checked on 10 October 2026 from the [Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing). Prices change; Google lists higher rates from 1 January 2027 for these models.

Gemini 3.8 Flash on the Flex tier was listed at US $0.375 per million input tokens and US $1.875 per million output tokens. Google's [document understanding guide](https://ai.google.dev/gemini-api/docs/document-processing) counts each PDF page as 258 tokens for Gemini 3 models. A 20-page article with a 2,000-token prompt is therefore about 7,000 input tokens. With 3,000 output tokens including reasoning, one request costs about US $0.008, and 200 such articles about US $1.65 before retries.

Treat this as an illustration of the calculation, not a quote. Your own token counts from a trial run are a better guide.

## Pricing pages

- Gemini: [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing). Paid and unpaid use also have different [data terms](privacy.md#google-gemini).
- OpenRouter: each model page lists its price per provider. OpenRouter states that it passes provider prices through and charges a fee when you buy credits; see the [OpenRouter FAQ](https://openrouter.ai/docs/faq).
- OpenAI-compatible endpoints: check your service's pricing. A local model has no per-request charge, but uses your computer's resources.

## Keep costs under control

- Finalise the manual and coding sheet on a small batch of uploaded PDFs before adding the rest. Changing them later clears working results and the next run sends PDFs again.
- Set a spending limit or budget alert with your provider, where available.
- Use `flex` for Gemini runs that are not urgent, and leave **Reasoning effort** at the model default unless a trial shows it improves accuracy.
- Retry only failed or needs-review studies rather than re-running the whole project.
