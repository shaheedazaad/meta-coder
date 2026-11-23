# Extractors and settings

Open **Settings** in the app header to manage **extractors** and drafting preferences. Each project then chooses one extractor on its [Run tab](run.md).

[![Extractors and drafting preferences in Settings](assets/screenshots/settings.png)](assets/screenshots/settings.png)

## What an extractor is

An extractor is a named, saved setup for reaching a model: its provider, model, endpoint, API key and request settings. You can keep several, including several for the same provider, for example two OpenRouter keys or two OpenAI-compatible servers. Before choosing a provider, read [Privacy and data sharing](privacy.md): data use differs between providers and, for Gemini, between free and paid access.

| Provider | Connection | Study PDF handling |
| --- | --- | --- |
| Gemini | An API key | Native PDF input |
| OpenRouter | An API key | PDF input through the provider |
| OpenAI-compatible | The API base URL, and a key if the server requires one | Text extracted on your computer, with page numbers |

## Add an extractor

1. Expand **Add an extractor**.
2. Give it a name you will recognise, such as "Gemini lab key", and choose a provider.
3. Enter the model ID. **Show available models** asks the provider for its model list, using the key you typed. It loads only when you click it; the typed key is sent to the provider for that one request and is not stored unless you save the extractor.
4. Paste the API key. For an OpenAI-compatible server, also enter the API base URL (include the prefix such as `/v1`, without `/chat/completions`) and a JSON output mode the server supports: **Strict JSON schema**, **JSON object**, or **Prompt only**. Responses are validated in every mode.
5. Optionally expand **Request settings** to change parallel requests, the delay between requests, the request timeout, the Gemini service tier or the reasoning effort.
6. Choose **Add extractor**.

When you save with a key, MetaCoder asks the provider whether the model can be used and shows any warning. If an OpenRouter model has no native PDF input, OpenRouter converts each PDF with its default parser, the third-party Mistral OCR service, which charges per page in addition to the model's token cost. Information shown only in figures or unusual table layouts may be lost.

## Edit, duplicate or delete

Expand an extractor to edit it. Its provider can't change; add another extractor instead. Leave the key field blank to keep the saved key, or paste a new one to replace it. **Remove key** deletes only the key.

**Duplicate** copies every setting except the key, which is never copied. Add the copy's key before running with it.

**Delete** first lists the projects that use the extractor, and says whether it is the drafting extractor, before asking you to confirm. Those projects must choose another extractor before they can run. Deleting an extractor also deletes its saved key. Your projects and their results are not affected.

Editing an extractor changes what its projects use from their next run. A result coded with a different model or endpoint is marked outdated; see [Run extraction](run.md#cancel-and-retry).

## API keys

Keys are saved in your computer's operating system credential store (macOS Keychain, Windows Credential Locker or the Linux Secret Service), in one item holding every extractor's key. They are never written to MetaCoder's files, pages or exports, and known keys are removed from any error message.

MetaCoder never opens the credential store when it starts or shows a page. The first action that needs a key, such as a run, a draft or a model check, reads the item once; your computer may ask you to allow access. The keys then stay in memory until you quit. If access is refused or the item can't be read, the action stops with a message and the item is left untouched, so you can try again.

If your computer's credential store is unavailable, key entry is disabled. Restore access to your operating system's credential service and restart the app. An OpenAI-compatible server that genuinely allows keyless requests can still be used without a key.

## Choose the drafting extractor

Under **Drafting**, choose the extractor used for document-to-manual drafts and coding-sheet conversion, then choose **Save drafting preferences**. This does not change any project's extractor.

The maximum document upload size is configurable here, from 1 to 1,024 MB.

## Upgrading from an earlier version

Earlier versions kept one key per provider plus a model and request settings in each project. The first time a newer version starts, it turns these into extractors:

- Each provider with a saved key, and an OpenAI-compatible server with a base URL, becomes an extractor named after the provider.
- Each project keeps the provider, model and request settings it had. Projects with identical settings share an extractor; a project with different settings gets its own, named after the project, which uses the same key.
- The drafting provider and model become the drafting extractor.
- Settings left at the defaults with no saved key are skipped. A project whose provider had no key or endpoint keeps its last settings on record but needs an extractor chosen before it can run.
- A saved key whose settings were missing or unusable is kept on an extractor that asks for its model or base URL.

Migration does not open the credential store. Old per-provider keys move into the new credential item the first time a key is needed, and the old items are deleted only after the new one is saved. You may be asked to allow access once.
