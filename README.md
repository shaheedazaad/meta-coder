# MetaCoder

Browser app for LLM-assisted meta-analysis moderator coding that runs on your computer.

MetaCoder codes moderators and other study characteristics for each effect row of an existing coding sheet. It does not extract or compute effect sizes; your coding sheet must already list the effects you want coded.

> [!TIP]
> **New to MetaCoder?** Read the [user guide](https://shaheedazaad.github.io/meta-coder/) for installation instructions and a walkthrough of the full coding workflow. The guide is also bundled with the app and works offline.

## What it does

- Builds a coding manual in a structured editor, imports an existing `manual.yml`, or drafts one from a PDF, DOCX, RTF or Markdown document.
- Takes a coding sheet with one row per effect, or converts a CSV in another layout with AI, and matches its rows to your study PDFs or XML (JATS) full text.
- Codes every row for a source in one request to Gemini, OpenRouter or an OpenAI-compatible endpoint, and flags responses with mismatched row IDs or cut-short or repaired output for review instead of accepting them.
- Exports `coded_data.csv` with a matching `evidence.csv` of supporting pages and quotes, readable per-source YAML, and an audit ZIP of every request and response.
- Keeps projects on your computer and API keys in your operating system's credential store.

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

To update, stop MetaCoder, rerun the same installer, and start it again. Your projects, preferences, and saved API keys are kept. The app shows a banner when a newer release is available. See the guide to [install a specific version](https://shaheedazaad.github.io/meta-coder/#install-or-update) or for [how update checks work](https://shaheedazaad.github.io/meta-coder/#update-metacoder).

To run from a source checkout instead, see [CONTRIBUTING.md](CONTRIBUTING.md#run-from-source).

## Uninstall

Stop MetaCoder, then run the uninstaller for your platform.

**macOS / Linux** — run in a terminal:

```bash
curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.sh | bash
```

**Windows** — run in PowerShell:

```powershell
irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.ps1 | iex
```

Projects, settings, saved API keys, and Pixi are kept. See the guide for [what the uninstaller removes](https://shaheedazaad.github.io/meta-coder/#uninstall-metacoder).

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
