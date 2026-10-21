# Privacy and data sharing

MetaCoder runs on your computer, but every AI action sends research documents to the provider you choose. Check what that provider may do with them before you upload unpublished, confidential, or personal material.

This page describes what MetaCoder sends and summarises provider terms as checked on 10 October 2026. Terms change; read the linked pages yourself, and ask your institution's data protection, ethics, or legal advisers where needed. This page is not legal advice.

## What leaves your computer

| Action | Sent to | Content sent |
| --- | --- | --- |
| Extraction with Gemini | Google's Gemini API (`generativelanguage.googleapis.com`) | The complete study PDF, plus instructions, your effect definition, coding manual fields, and the row IDs and locators for that PDF |
| Extraction with OpenRouter | OpenRouter (`openrouter.ai`), which forwards the request to a model provider | The complete study PDF, plus the same instructions and coding details |
| Extraction with an OpenAI-compatible endpoint | The API base URL you entered | Text extracted from the PDF on your computer, with page numbers, plus the same instructions and coding details. The PDF file itself is not sent |
| AI manual drafting | Your drafting provider | Text extracted on your computer from the uploaded manual document |
| Coding sheet conversion | Your drafting provider | The uploaded CSV content, your saved manual, your notes, and the names of uploaded PDFs |
| Model checks | The selected provider | A request for model details; no documents |
| Update checks | GitHub | A request for the latest release number; no documents or keys |

Your API key is sent only to the provider it belongs to, to authorise requests. Nothing is sent to the MetaCoder developers.

## What is stored on your computer

Projects are saved in the app's data folder (see [Data locations](projects.md#data-locations)). A project contains your manual, coding sheet, source PDFs, results, and an `audit/` folder. The [audit history](auditing.md) keeps copies of the inputs and the full request sent to the provider, including the encoded PDF or extracted text, and the provider's response.

API keys are kept in your operating system's credential store, not in project files or exports. Project and audit ZIP exports do contain your documents, so share them with the same care as the PDFs themselves.

## Google Gemini

Google's [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms) (last modified 28 April 2026) distinguish unpaid and paid use:

- **Unpaid Services**, including the unpaid quota of the Gemini API: Google uses submitted content and responses "to provide, improve, and develop Google products and services and machine learning technologies". Human reviewers may read and annotate inputs and outputs, after Google disconnects them from your account and API key. The terms state: "Do not submit sensitive, confidential, or personal information to the Unpaid Services."
- **Paid Services**: Google "doesn't use your prompts (including associated system instructions, cached content, and files such as images, videos, or documents) or responses to improve our products". Prompts and responses are logged "for a limited period of time" for detecting policy violations and any required legal or regulatory disclosures.
- The Gemini API counts as a paid service "only when accessing the API through a Cloud Project associated with an active billing account".
- For users in the European Economic Area, Switzerland, or the United Kingdom, the terms say the paid-service data terms apply to all services, including unpaid quota.

Whether your requests are paid or unpaid depends on the Google Cloud project behind your API key, not on any MetaCoder setting. MetaCoder's default Gemini **Service tier** is `flex`, a cheaper, slower option. The [pricing page](https://ai.google.dev/gemini-api/docs/pricing) marks Flex as unavailable on the free tier for most models, but choosing `flex` does not itself make a key paid. To use paid terms, enable billing for the project that owns your key in Google AI Studio or Google Cloud, and check the **Used to improve our products** row on the pricing page.

## OpenRouter

OpenRouter passes your request to a third-party model provider, so two sets of terms apply.

- **OpenRouter itself**: its [data collection page](https://openrouter.ai/docs/guides/privacy/data-collection) states that it does not store your prompts or responses unless you opt in to logging. One opt-in lets OpenRouter use inputs and outputs in exchange for a discount; leave it off for research documents.
- **The model provider**: policies differ by provider. In your [OpenRouter privacy settings](https://openrouter.ai/settings/privacy) you can exclude providers that may store inputs for training, and you can require [Zero Data Retention (ZDR)](https://openrouter.ai/docs/guides/features/zdr) endpoints. OpenRouter says that where it cannot establish a provider's policy it assumes the endpoint "both retains and trains on data". MetaCoder does not send per-request routing preferences, so your account settings decide which providers are eligible.
- **PDF handling**: if a model accepts files directly, OpenRouter passes the PDF to it. If not, OpenRouter converts the PDF first. Its [PDF documentation](https://openrouter.ai/docs/guides/overview/multimodal/pdfs) names the parsing engines it uses; at the time of checking, the default for models without native file input was a third-party OCR service charged per page. MetaCoder warns when you select a model without native file input.

## OpenAI-compatible endpoints

Data handling depends entirely on the endpoint. A local server such as Ollama or LM Studio on `http://localhost` keeps the extracted text on your computer. A university-hosted service or commercial API is governed by its own terms and your institution's agreement. Ask the service owner whether inputs are logged, retained, or used for training.

## Practical guidance

- **Ethics approval and consent.** Most meta-analyses code published articles, but check your protocol or ethics approval if you code unpublished manuscripts, grey literature, data shared by authors, or documents containing participant information.
- **Unpublished or confidential manuscripts.** Material shared under confidentiality, such as reviewer copies or author-supplied reports, should only go to a service whose terms you and the owner accept. Prefer paid Gemini access, OpenRouter with ZDR, or a local endpoint.
- **Copyrighted articles.** Uploading licensed PDFs to a third-party service may be restricted by your library's licence terms. Ask your library if you are unsure.
- **Avoid the unpaid Gemini tier** for anything you would not publish, unless the EEA, Swiss, or UK terms apply to you.
- **Record your choice.** Note the provider, model, and service tier in your methods section; see [Validating the coding](validation.md#report-your-methods).
