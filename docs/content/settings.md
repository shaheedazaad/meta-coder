# Connections and settings

Open **Settings** in the app header to configure provider connections and the model used for AI drafts. Extraction settings belong to each project's [Run tab](run.md).

[![Provider configuration and drafting preferences in Settings](assets/screenshots/settings.png)](assets/screenshots/settings.png)

## Connect a provider

Expand a provider under **Provider configuration**, enter its API key, then save. Before choosing a provider, read [Privacy and data sharing](privacy.md): data use differs between providers and, for Gemini, between free and paid access. Get a Gemini key from [Google AI Studio](https://aistudio.google.com/app/apikey) or an OpenRouter key from [openrouter.ai](https://openrouter.ai/settings/keys). Keys are stored in your computer's operating system credential store (macOS Keychain, Windows Credential Locker or Linux Secret Service). After restarting the app, access is requested when an action needs the key, rather than on launch. There is no separate unlock step and no fallback that saves keys in a plain file.

| Provider | Connection | Study PDF handling |
| --- | --- | --- |
| Gemini | Save a Gemini API key | Native PDF input |
| OpenRouter | Save an OpenRouter API key | PDF input through the provider |
| OpenAI-compatible | Save the API base URL and a key if required | Text extracted on your computer, with page numbers |

If an OpenRouter model has no native PDF input, OpenRouter converts each PDF with its default parser, the third-party Mistral OCR service, which charges per page in addition to the model's token cost. MetaCoder warns about this when you save such a model in a project's Run settings. Information shown only in figures or unusual table layouts may be lost.

## OpenAI-compatible endpoints

Enter the endpoint's API base URL, for example `http://localhost:8000/v1` or `https://api.openai.com/v1`. Include the API prefix such as `/v1`, without `/chat/completions`, which MetaCoder appends. The endpoint is shared by all projects and by AI drafting. To use it for extraction, select **OpenAI-compatible** on a project's [Run tab](run.md), enter the exact model ID your endpoint serves, and save. AI drafting has its own provider and model under **Drafting model**. MetaCoder does not need the endpoint to list its models; the model ID is checked when a request runs.

Save an API key under **OpenAI-compatible** if the server requires one; a local server can run without a key. The key is kept in the same credential store as the other providers' keys and read when an action needs it. When changing endpoints, replace or remove the old key too.

Choose a JSON output mode supported by the endpoint: **Strict JSON schema** (the default), **JSON object**, or **Prompt only**. These correspond to the [Chat Completions structured-output modes](https://developers.openai.com/api/docs/guides/structured-outputs); **Prompt only** sends no `response_format`. MetaCoder checks the row IDs and fields of every response in every mode.

This provider extracts each PDF's text on your computer and sends it with page numbers instead of attaching the PDF. Figures and scanned pages are not interpreted: run OCR first or use a provider with native PDF support. A PDF with no extractable text fails with an explicit error. Timeouts, cancellation, automatic retries of temporary errors, raw-response review and token-usage reporting work as they do for the other providers.

## Choose a drafting model

Under **Drafting model**, select the provider and model for document-to-manual drafts and coding sheet conversion. Choose **Save drafting preferences**. This does not change a project's extraction provider or model.

The maximum document upload size is configurable here, from 1 to 1,024 MB.

## Credential store unavailable

If your computer's system credential store is unavailable, key entry is disabled. Restore access to your operating system's credential service and restart the app. An OpenAI-compatible endpoint that genuinely allows keyless requests can still be configured without a saved key.

To remove a saved key, expand **Key removal** under its provider. Removing a key does not delete your projects.
