# Contributing to MetaCoder

Thanks for helping improve MetaCoder. This guide covers reporting problems,
setting up a development environment, the checks every change must pass, and
the release process. For using the app, see the [README](README.md) and the
[user guide](https://shaheedazaad.github.io/meta-coder/).

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).

## Reporting bugs and proposing changes

- **Bugs:** open a [GitHub issue](https://github.com/shaheedazaad/meta-coder/issues)
  with your MetaCoder version, operating system, provider and model, what you
  did, what you expected, and what happened. Never include API keys. Leave out
  research documents unless you are sure they can be shared publicly.
- **Changes:** for anything beyond a small fix, open an issue first so the
  approach can be agreed before you write code.

### What a pull request needs

- Tests for the change. Python and JavaScript coverage must both stay at 100%
  (see [Tests and coverage](#tests-and-coverage)).
- Documentation updates in `docs/content/` when user-visible behaviour changes,
  plus a rebuilt bundled guide (see [Documentation](#documentation)).
- An updated `pixi.lock` if dependencies changed.
- Text files read with an explicit `encoding='utf-8'`. Windows CI fails on
  non-ASCII content otherwise.

## Development setup

### Run from source

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m meta_coder
```

Windows (PowerShell):

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m meta_coder
```

Or via [Pixi](https://pixi.sh) (locked environment, no manual `pip install`):

```sh
pixi run start
```

Either way, this opens your browser to a local, randomly-tokened URL
(`http://127.0.0.1:<port>/<token>/`). The server binds only to `127.0.0.1`.

### Pixi environments

The project has two Pixi environments (`[tool.pixi.environments]` in
`pyproject.toml`): `default` (just the app's runtime dependencies) and `dev`
(`default` plus `pytest`). Both install `meta_coder` itself as an editable
package (`meta-coder = { path = ".", editable = true }` under
`[tool.pixi.pypi-dependencies]`), so code
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

### Tips

- **Isolating project data.** By default the app stores projects, run
  settings, and non-secret preferences under the OS's standard per-user data
  directory (see `meta_coder/paths.py`). Set `META_CODER_HOME` to point that
  somewhere disposable instead, so testing doesn't touch (or get confused by)
  your real projects:
  ```sh
  META_CODER_HOME=/tmp/meta-coder-dev pixi run start
  ```
- **Building a release bundle locally**, e.g. to test `scripts/install.sh`
  end-to-end without a real host: `scripts/build_release.sh` archives a git
  ref (default `HEAD`) into `dist/meta-coder-<version>.tar.gz` — see
  [Releases](#releases) for the full flow.

## Frontend

Server-rendered Jinja templates styled with [Basecoat](https://basecoatui.com/), a
vendored CSS/JS component library (`meta_coder/static/vendor/`) with a hand-authored
`app.css` on top for page-level layout and neutral surfaces with blue action accents. No Node at install or
launch, and nothing to compile — every frontend file is checked into the repo as-is.

Templates (`meta_coder/templates/`), `meta_coder/static/app.js`, and
`meta_coder/static/app.css` are served directly, so edits show up on the next
page load with no build step.

## Tests and coverage

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

CI runs both coverage commands, so run them before opening a pull request. All
provider requests in the automated tests are faked; no API keys are needed.

`tests/test_mechanism.py` is the important one: it proves the row_id/locator
mechanism (build the response schema, hard-reject on any ID mismatch) with
hand-written fake responses, with no API call involved.
`tests/test_runner_concurrency.py` proves concurrent runs attribute results to the
right PDF, the request pacer's delay is global not per-worker, a retry of a subset
of PDFs doesn't blank out other PDFs' prior results, and cancellation skips queued
PDFs while keeping completed results.

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
pip install -e ".[docs,docs-screenshots]"
python scripts/capture_docs.py
python -m mkdocs build --strict
```

Alternatively, run `python -m playwright install chromium` and pass
`--browser chromium` to the capture script. Screenshot data is temporary and
removed when capture finishes. Generated images live in `docs/assets/screenshots/`.

## GitHub automation

The [GitHub Pages guide](https://shaheedazaad.github.io/meta-coder/) is built from the same MkDocs sources as the offline help. `.github/workflows/docs.yml` deploys it on relevant pushes to `main`, using GitHub Pages with **GitHub Actions** as its publishing source. The repository, releases, and guide are public. Configure Pages to use GitHub Actions and set the repository variable `PUBLISH_DOCS` to `true` to enable deployment.

Pull requests and pushes to `main` run Python and frontend tests with coverage on Linux, Windows, Apple Silicon macOS, and Intel macOS, test the oldest and newest supported Python versions, verify the Pixi lock, and build docs strictly.

## Releases

To publish a stable release:

1. Update both `pyproject.toml` and `meta_coder/__init__.py` to the same `X.Y.Z` version. Refresh `pixi.lock` with `pixi install` if necessary.
2. Commit the complete app, docs sources/assets, scripts, lockfiles, tests, and workflows, and push to `main`. Wait for Checks to pass.
3. Tag that commit with `git tag vX.Y.Z` and push it with `git push origin vX.Y.Z`.

The Release workflow reruns checks, validates the tag against both package versions, builds offline docs from the tagged commit, and publishes the source bundle, both installers and uninstallers, `latest.txt`, and `SHA256SUMS`. All assets are uploaded to a draft before publication. If publishing fails after creating the draft, inspect or delete that draft before retrying the workflow.

For a local bundle from a committed ref, install `.[docs]` and run `bash scripts/build_release.sh <ref>` with that Python on PATH (or set `PYTHON`). Uncommitted files are deliberately excluded. No PyPI publishing token or personal GitHub token is needed by the workflows.
