# MetaCoder

Browser app for LLM-assisted meta-analysis moderator coding that runs on your computer.

> [!TIP]
> **New to MetaCoder?** Read the [user guide](https://shaheedazaad.github.io/meta-coder/) for installation instructions and a walkthrough of the full coding workflow.

## Run it

From source:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m meta_coder
```

Or via [Pixi](https://pixi.sh) (locked environment, no manual `pip install`):

```sh
pixi run start
```

Either way, this opens your browser to a local, randomly-tokened URL
(`http://127.0.0.1:<port>/<token>/`). The server binds only to `127.0.0.1`.

## Local development

The project has two Pixi environments (`[tool.pixi.environments]` in
`pyproject.toml`): `default` (just the app's runtime dependencies) and `dev`
(`default` plus `pytest`). Both install `meta_coder` itself as an editable
package (`pypi-dependencies = { path = ".", editable = true }`), so code
changes take effect on the next request/restart — no reinstall step.

```sh
pixi install          # materializes .pixi/envs/default from the committed pixi.lock
pixi install -e dev   # same, for the dev environment (only needed once)

pixi run start         # run the app (equivalent to `python -m meta_coder`)
pixi run -e dev test   # run the test suite (equivalent to `pytest -q`)
pixi shell -e dev      # drop into an interactive shell inside the dev environment
```

If you change a dependency in `[project.dependencies]` or `[tool.pixi.*]`,
run `pixi install` (or just `pixi run ...` — it updates the lock automatically
when the manifest changed) and commit the resulting `pixi.lock` so everyone's
environment — and the release bundles built by `scripts/build_release.sh` —
stays reproducible.

Equivalent plain-venv workflow, if you'd rather not use Pixi day-to-day:

```sh
pip install -e ".[dev]"
pytest
```

A few other things worth knowing while developing:

- **Isolating project data.** By default the app stores projects, run
  settings, and non-secret preferences under the OS's standard per-user data
  directory (see `meta_coder/paths.py`). Set `META_CODER_HOME` to point that
  somewhere disposable instead, so testing doesn't touch (or get confused by)
  your real projects:
  ```sh
  META_CODER_HOME=/tmp/meta-coder-dev pixi run start
  ```
- **Frontend changes.** Templates (`meta_coder/templates/`), `meta_coder/static/app.js`,
  and `meta_coder/static/app.css` are hand-authored and served directly —
  edits show up on the next page load, no build step. `meta_coder/static/vendor/`
  holds a vendored copy of [Basecoat](https://basecoatui.com/), the component
  library the app's CSS is built on.
- **Building a release bundle locally**, e.g. to test `scripts/install.sh`
  end-to-end without a real host: `scripts/build_release.sh` archives a git
  ref (default `HEAD`) into `dist/meta-coder-<version>.tar.gz` — see
  "Installing as an end user" below for the full install-script flow.

## Documentation

Open **Documentation** in the app header, or use the guide link in each project
section. The MkDocs site is bundled with the app and works offline. Its custom
theme reuses the app's Basecoat CSS, colours, and light/dark preference.

To edit and preview the guide:

```sh
pip install -e ".[docs]"
python -m mkdocs serve
# Rebuild the bundled copy after changing docs, screenshots, or app styles:
python -m mkdocs build --strict
```

Author pages in `docs/content/`, edit the theme in `docs/theme/`, and configure
navigation in `mkdocs.yml`. `docs/hooks.py` includes the app's real styles and the
screenshots. Commit the generated `meta_coder/documentation/` directory with its
sources so source installs, wheels, and release bundles all include working help
without a runtime MkDocs dependency. A standalone build can also be deployed to
any static host; no app token or external CDN is embedded in the files.

Regenerate the ten screenshots using an isolated fictional project (no API calls
or real credentials), with installed Google Chrome:

```sh
pip install -e ".[docs-screenshots]"
python scripts/capture_docs.py
python -m mkdocs build --strict
```

Alternatively, run `python -m playwright install chromium` and pass
`--browser chromium` to the capture script. Screenshot data is temporary and
removed when capture finishes. Generated images live in `docs/assets/screenshots/`.

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

To update, stop MetaCoder, rerun the same installer, and start it again. Your projects, preferences, and saved API keys are kept. Export a project ZIP first if you want a backup. The app checks GitHub for a newer stable release when a page opens, caching the result for six hours. An available update appears as an informational banner; offline checks fail silently. Checks read public release metadata without a GitHub login; no project data or AI provider credentials are sent.

For a specific version, set `META_CODER_VERSION` (for example, `0.1.0`) before running the installer. `META_CODER_RELEASE_BASE_URL` optionally overrides the download location for mirrors and testing.

### Uninstalling

Stop MetaCoder, then run the uninstaller for your platform from this checkout:

```sh
bash scripts/uninstall.sh
```

On Windows, run `./scripts/uninstall.ps1` in PowerShell. These scripts remove
all release versions and the installer's launcher; the Windows script also
removes its user PATH entry. They can safely be run again after uninstalling.
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
3. Write a coding manual on the project's **Coding manual** tab — a structured
   editor, not raw YAML: `effect_definition` (what comparison counts as "the
   effect"), and `effects` (the fields the model codes, with categories for
   categorical ones). A starter example is pre-filled when you create a project. You
   can also import an existing `manual.yml`, or drop a PDF, DOCX, RTF, or Markdown (.md) coding manual into
   the automatic generator. Its provider/model are configured globally in Settings,
   independently of extraction. The draft appears in the editor without a page reload
   and is not saved until you explicitly validate and save it.
4. Upload the PDFs you want coded (**Source PDFs** tab).
5. Upload a coding sheet CSV (**Coding sheet** tab): `row_id`, `source_pdf`,
   `locator`, `authors`, `year` — one row per effect/experiment/condition. A row
   whose `source_pdf` doesn't match an uploaded file is flagged as a blocking error,
   not silently skipped.
   For a CSV with a different layout, use **Convert an existing coding sheet**.
   Add optional notes about its columns and intended effect rows, choose the CSV,
   and click **Convert to a draft**. Conversion uses the saved coding manual and
   the provider/model configured for manual drafting. Review the preview and
   warnings, edit the CSV if needed, then choose **Save converted sheet**.
   Conversion preserves the current sheet and results until you save; saving a
   changed sheet clears prior extraction results. Missing authors/year must be
   filled in before saving; unmatched PDFs can be identified afterward.
   CSVs must be UTF-8, with at most 2,000 source rows and 500,000 characters.
   Already formatted CSVs can still be uploaded directly under the separate
   **Upload a CSV already in the required format** option.
6. On the **Run** tab, pick a provider/model and parallelism/pacing, then run. Every
   coding-sheet row for one PDF is sent in a single request; the model must echo
   back the exact row IDs it was given, or that PDF is marked `needs_review` rather
   than accepting a best-effort guess. A response the provider cut short (e.g. at
   its output token limit) or whose JSON had to be repaired is also marked
   `needs_review`, with its values kept for inspection. Progress updates live; a run in progress can
   be cancelled (in-flight PDFs finish, queued ones stop). Failed/needs-review PDFs
   can be retried individually or all at once without reprocessing PDFs that already
   succeeded.
7. On the **Results** tab, download `coded_data.csv` and `evidence.csv` — same
   shape, `evidence.csv` has the supporting page/quote for each cell — plus one
   readable `output/coded/<pdf>.yaml` per PDF for actually reading a handful of
   coded effects and quotes rather than scanning CSV columns. Each PDF's raw
   provider response is also viewable from the Run tab. Hand-check a few rows
   against evidence before trusting the results.

## Audit and reproducibility

**Download audit ZIP** on Results exports current project files and persistent
`audit/` history. Each extraction or AI drafting action records app/source and
dependency versions, UTC timestamps, input snapshots, exact credential-free
request bodies, full provider response envelopes, model revision/fingerprint
when returned, validation outcomes, and every HTTP retry. Run records preserve
CSV exports and connect per-PDF attempts. The ZIP includes current run settings
and an `export_manifest.json` with SHA-256 checksums.

History survives retries, manual changes, source removal, and clearing working
output. Deleting a project deletes its history. API credentials are excluded;
research documents and prompts are included. Older runs cannot be backfilled,
and provider aliases or nondeterministic inference can prevent identical reruns.
See [the audit guide](docs/content/auditing.md) for the archive layout and verifier.

## Tests

```sh
pixi run -e dev test
# or, in a plain venv:
pip install -e ".[dev]"
pytest
```

Measure Python statement and branch coverage with `pixi run -e dev coverage` or
`pytest --cov=meta_coder --cov-branch --cov-report=term-missing --cov-report=html`.
The HTML report is written to `htmlcov/index.html`. The command fails if any
application Python statement or branch is uncovered.

Frontend tests render the production templates with an isolated temporary project,
then execute the complete app script in a DOM environment. They cover editing,
drafting, tabs, uploads, live search, progress polling, and navigation protection:

```sh
npm ci
npm test
npm run coverage
```

Node 24 is recommended and is needed only for development checks. The DOM fixtures also need the Python
dev dependencies above. Tests use `.venv/bin/python` when available, otherwise
`python`; set `PYTHON` to select another interpreter (for example the Pixi dev
interpreter). JavaScript coverage reports are written to `coverage/index.html`.
`npm run coverage` enforces 100% statement, branch, function, and line coverage
for the app's own JavaScript (vendored dependencies are excluded). The inline
pre-paint theme script is tested against the main script in both light and dark
system modes. Rendered pages and their DOM interactions are covered; these tests
do not claim pixel-level CSS verification or live-provider integration.

Run both coverage commands before merging changes. All provider requests in the
automated tests are faked; no API keys are needed.

`tests/test_mechanism.py` is the important one: it proves the row_id/locator
mechanism (build the response schema, hard-reject on any ID mismatch) with
hand-written fake responses, with no API call involved.
`tests/test_runner_concurrency.py` proves concurrent runs attribute results to the
right PDF, the request pacer's delay is global not per-worker, a retry of a subset
of PDFs doesn't blank out other PDFs' prior results, and cancellation stops queued
work without killing an in-flight request.

## Frontend

Server-rendered Jinja templates styled with [Basecoat](https://basecoatui.com/), a
vendored CSS/JS component library (`meta_coder/static/vendor/`) with a hand-authored
`app.css` on top for page-level layout and neutral surfaces with blue action accents. No Node at install or
launch, and nothing to compile — every frontend file is checked into the repo as-is.

### OpenAI-compatible endpoints

In **Settings**, enter the endpoint's API base URL (for example,
`http://localhost:8000/v1` or `https://api.openai.com/v1`). Include its API prefix;
MetaCoder appends `/chat/completions`. The endpoint is shared across projects
and the manual generator. Select **OpenAI-compatible** on the Run tab, enter the
exact model ID served by your endpoint, and save. The manual generator has its
own provider/model selection in Settings.

Save an API key under **OpenAI-compatible** if the server requires authentication;
local servers can run without one. Keys use the same OS credential store on your computer as the
other providers. Unlock a saved key before running. When switching endpoints,
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

## GitHub automation and releases

The [GitHub Pages guide](https://shaheedazaad.github.io/meta-coder/) is built from the same MkDocs sources as the offline help. `.github/workflows/docs.yml` deploys it on relevant pushes to `main`, using GitHub Pages with **GitHub Actions** as its publishing source. The repository, releases, and guide are public. Configure Pages to use GitHub Actions and set the repository variable `PUBLISH_DOCS` to `true` to enable deployment.

Pull requests and pushes to `main` run Python and frontend tests on Linux, Windows, Apple Silicon macOS, and Intel macOS, verify the Pixi lock, and build docs strictly.

To publish a stable release:

1. Update both `pyproject.toml` and `meta_coder/__init__.py` to the same `X.Y.Z` version. Refresh `pixi.lock` with `pixi install` if necessary.
2. Commit the complete app, docs sources/assets, scripts, lockfiles, tests, and workflows, and push to `main`. Wait for Checks to pass.
3. Tag that commit with `git tag vX.Y.Z` and push it with `git push origin vX.Y.Z`.

The Release workflow reruns checks, validates the tag against both package versions, builds offline docs from the tagged commit, and publishes the source bundle, both installers, `latest.txt`, and `SHA256SUMS`. All assets are uploaded to a draft before publication. If publishing fails after creating the draft, inspect or delete that draft before retrying the workflow.

For a local bundle from a committed ref, install `.[docs]` and run `bash scripts/build_release.sh <ref>` with that Python on PATH (or set `PYTHON`). Uncommitted files are deliberately excluded. No PyPI publishing token or personal GitHub token is needed by the workflows.
