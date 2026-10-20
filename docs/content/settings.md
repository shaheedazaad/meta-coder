# Connections and settings

Open **Settings** in the app header to configure provider connections and the model used for AI drafts. Extraction settings belong to each project's [Run tab](run.md).

[![Provider configuration and drafting preferences in Settings](assets/screenshots/settings.png)](assets/screenshots/settings.png)

## Connect a provider

Expand a provider under **Provider configuration**, enter its API key, then save. Keys are stored in your computer's operating system credential store. After restarting the app, access is requested when an action needs the key, rather than on launch.

| Provider | Connection | Study PDF handling |
| --- | --- | --- |
| Gemini | Save a Gemini API key | Native PDF input |
| OpenRouter | Save an OpenRouter API key | PDF input through the provider |
| OpenAI-compatible | Save the API base URL and a key if required | Text extracted on your computer, with page numbers |

If an OpenRouter model has no native PDF input, OpenRouter converts each PDF with its default parser, the third-party Mistral OCR service, which charges per page in addition to the model's token cost. MetaCoder warns about this when you save such a model in a project's Run settings. Information shown only in figures or unusual table layouts may be lost.

For an OpenAI-compatible endpoint, include the API prefix such as `/v1`, without `/chat/completions`. Enter the exact model ID in the relevant model field. When changing endpoints, replace or remove the old key too.

Choose a JSON output mode supported by the endpoint: **Strict JSON schema**, **JSON object**, or **Prompt only**. Responses are validated in every mode. Scanned pages and figures may need OCR or a provider with native PDF support.

## Choose a drafting model

Under **Drafting model**, select the provider and model for document-to-manual drafts and coding sheet conversion. Choose **Save drafting preferences**. This does not change a project's extraction provider or model.

The maximum document upload size is configurable here, from 1 to 1,024 MB.

## Credential store unavailable

If your computer's system credential store is unavailable, key entry is disabled. Restore access to your operating system's credential service and restart the app. An OpenAI-compatible endpoint that genuinely allows keyless requests can still be configured without a saved key.

To remove a saved key, expand **Key removal** under its provider. Removing a key does not delete your projects.
