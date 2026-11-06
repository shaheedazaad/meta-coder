# MetaCoder

Browser app for LLM-assisted meta-analysis moderator coding that runs on your computer.

MetaCoder codes moderators and other study characteristics for each effect row of an existing coding sheet. It does not extract or compute effect sizes; your coding sheet must already list the effects you want coded.

> [!TIP]
> **New to MetaCoder?** Read the [user guide](https://shaheedazaad.github.io/meta-coder/) for installation instructions and a walkthrough of the full coding workflow.

## Install or update

Install the latest stable [GitHub release](https://github.com/shaheedazaad/meta-coder/releases/latest) using the commands below. Python and dependencies are managed automatically with Pixi.

**macOS / Linux** — run in a terminal:

```bash
curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.sh | bash
```

**Windows** — run in PowerShell:

```powershell
irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.ps1 | iex
```

Then run `meta-coder`. Follow the installer's PATH instructions if the command is not found.

To run from a source checkout instead, see [CONTRIBUTING.md](CONTRIBUTING.md#run-from-source).

To update, stop MetaCoder, rerun the same installer, and start it again. Your projects, preferences, and saved API keys are kept. Export a project ZIP first if you want a backup. The app checks GitHub for a newer stable release when a page opens, caching the result for six hours. An available update appears as an informational banner; offline checks fail silently. Checks read public release metadata without a GitHub login; no project data or AI provider credentials are sent.

For a specific version, set `META_CODER_VERSION` (for example, `0.1.0`) before running the installer. `META_CODER_RELEASE_BASE_URL` optionally overrides the download location for mirrors and testing.

### Uninstalling

Stop MetaCoder, then run the uninstaller for your platform.

**macOS / Linux** — run in a terminal:

```bash
curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.sh | bash
```

**Windows** — run in PowerShell:

```powershell
irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.ps1 | iex
```

From a source checkout, `bash scripts/uninstall.sh` and `./scripts/uninstall.ps1`
do the same. These scripts remove all release versions and the installer's
`meta-coder` launcher; a `meta-coder` command from another installation is kept.
The Windows script also removes its user PATH entry. They can safely be run again
after uninstalling.
Projects, settings, saved API keys in your computer's OS credential store, and Pixi are kept.
`META_CODER_HOME` overrides are left untouched. On Linux, use the same
`XDG_DATA_HOME` value you used when installing.

For a source installation made with pip, run `python -m pip uninstall meta-coder`
using the Python environment where you installed it instead.

## Try it out

1. Create a project.
2. Set an API key in **Settings**: paste a Gemini key (get one at
   [Google AI Studio](https://aistudio.google.com/app/apikey)) or an OpenRouter key
   ([openrouter.ai](https://openrouter.ai/settings/keys)). Keys are saved in your computer's
   OS credential store (macOS Keychain / Windows Credential Locker / Linux Secret
   Service). After restart, keychain access is requested when you run an action
   that needs a key, never on launch. There is no separate unlock step or plaintext fallback.
3. Define the effect on **Define your analysis** (`effect_definition`: what
   comparison counts as "the effect"), then write the coding manual on the
   **Coding manual** tab — a structured editor, not raw YAML: `effects` (the
   fields the model codes, with categories for categorical ones). Both are saved
   in the same manual. A starter example is pre-filled when you create a project. You
   can also import an existing `manual.yml`, or drop a PDF, DOCX, RTF, or Markdown (.md) coding manual into
   the automatic generator. Its provider/model are configured globally in Settings,
   independently of extraction. The draft appears in the editor without a page reload
   and is not saved until you explicitly validate and save it.
4. Upload the PDFs you want coded (**Source PDFs** tab).
5. Upload a coding sheet CSV (**Coding sheet** tab): `row_id`, `source_pdf`,
   `locator`, `authors`, `year` — one row per effect/experiment/condition. A row
   whose `source_pdf` doesn't match an uploaded file is listed under **PDF
   matching** but does not block a run: unmatched rows are skipped when you run
   extraction. Other row problems (duplicate `row_id`, missing authors/year)
   must be fixed before running.
   For a CSV with a different layout, use **AI conversion**.
   Add optional notes about its columns and intended effect rows, choose the CSV,
   and click **Convert to a draft**. Conversion uses the saved coding manual and
   the provider/model configured for manual drafting. Review the preview and
   warnings, edit the CSV if needed, then choose **Save converted sheet**.
   Conversion preserves the current sheet and results until you save; saving a
   changed sheet clears prior extraction results. Missing authors/year must be
   filled in before saving; unmatched PDFs can be identified afterward.
   CSVs must be UTF-8, with at most 2,000 source rows and 500,000 characters.
   Already formatted CSVs can still be uploaded directly under the separate
   **Upload formatted CSV** option.
6. On the **Run** tab, pick a provider/model and parallelism/pacing, then run. Every
   coding-sheet row for one PDF is sent in a single request; the model must echo
   back the exact row IDs it was given, or that PDF is marked `needs_review` rather
   than accepting a best-effort guess. Progress updates live; a run in progress can
   be cancelled (queued PDFs stop; in-flight requests are interrupted and those PDFs
   marked cancelled; already-coded PDFs are kept). Failed/needs-review PDFs
   can be retried individually or all at once without reprocessing PDFs that already
   succeeded.
7. On the **Results** tab, download `coded_data.csv` and `evidence.csv` — same
   shape, `evidence.csv` has the supporting page/quote for each cell — plus one
   readable `output/coded/<pdf>.yaml` per PDF for actually reading a handful of
   coded effects and quotes rather than scanning CSV columns. Each PDF's raw
   provider response is also viewable from the Results tab (**view raw output**). Hand-check a few rows
   against evidence before trusting the results.

## OpenAI-compatible endpoints

In **Settings**, enter the endpoint's API base URL (for example,
`http://localhost:8000/v1` or `https://api.openai.com/v1`). Include its API prefix;
MetaCoder appends `/chat/completions`. The endpoint is shared across projects
and the manual generator. Select **OpenAI-compatible** on the Run tab, enter the
exact model ID served by your endpoint, and save. The manual generator has its
own provider/model selection in Settings.

Save an API key under **OpenAI-compatible** if the server requires authentication;
local servers can run without one. Keys use the same OS credential store on your computer as the
other providers and, as with them, a saved key is read from the store when you
run an action that needs it. When switching endpoints,
replace or remove the old key as appropriate.

The default output mode is **Strict JSON schema**. For servers that do not support
it, choose **JSON object** or **Prompt only** in Settings. These correspond to the
[Chat Completions structured-output modes](https://developers.openai.com/api/docs/guides/structured-outputs);
prompt-only mode omits `response_format`. All results still undergo MetaCoder's
row and field validation. Model IDs are accepted without requiring a `/models`
catalog; availability is checked when a request runs.

This provider extracts PDF text on your computer and sends it with page numbers, rather than
PDF attachments. Figures and scanned pages are not interpreted; run OCR first or
use a provider with native PDF support. PDFs with no extractable text fail with an
explicit error. Requests retain timeout, cancellation, transient-error retries,
raw-response review, and token-usage reporting.

## Audit and reproducibility

**Download audit ZIP** on Results exports current project files and persistent
`audit/` history. Each extraction or AI drafting action records app/source and
dependency versions, UTC timestamps, input snapshots, exact credential-free
request bodies, full provider response envelopes, model revision/fingerprint
when returned, validation outcomes, and each HTTP exchange, including any
automatic retries. Run records preserve
CSV exports and connect per-PDF attempts. The ZIP includes current run settings
and an `export_manifest.json` with SHA-256 checksums.

History survives retries, manual changes, source removal, and clearing working
output. Deleting a project deletes its history. API credentials are excluded;
research documents and prompts are included. Older runs cannot be backfilled,
and provider aliases or nondeterministic inference can prevent identical reruns.
See [the audit guide](docs/content/auditing.md) for the archive layout and verifier.

## Privacy, cost and validation

Every AI action sends research documents to the provider you choose, and hosted
providers charge per request. Before coding real studies, read the guide's pages on
[privacy and data sharing](https://shaheedazaad.github.io/meta-coder/privacy/), [cost](https://shaheedazaad.github.io/meta-coder/costs/), and
[validating the coding](https://shaheedazaad.github.io/meta-coder/validation/) against human coders.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for development setup, tests, the 100% coverage rule, and the release process.

## Licence

MetaCoder is released under the [MIT License](LICENSE).
