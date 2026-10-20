# Regression set

A small, human-coded set of open-access articles for checking that changes to MetaCoder's prompt, response schema, validation or quote matching do not make coding worse. It is a cheap routine check, not a validation study: the set is small and comes from one meta-analysis.

## What is in it

| File | Content |
| --- | --- |
| `pdfs/` | Nine CC BY articles and one image-only copy |
| `articles.csv` | DOI, licence, checksum, and which set each PDF belongs to |
| `manual.yml` | Coding manual with 16 fields, confidence ratings on |
| `coding_sheet.csv` | 33 rows, one per correlation, with locators |
| `ground_truth.csv` | The human code for every row and field |
| `baselines/` | Saved provider responses and scores of a reference run |
| `run_eval.py` | Runs a set, scores it, or replays saved responses |

There are two sets:

- **small**: 5 PDFs and 17 rows. Four articles and the image-only copy.
- **broad**: 10 PDFs and 33 rows. All nine articles and the image-only copy.

The image-only copy, `xia_2022_scanned.pdf`, is `xia_2022.pdf` rendered to greyscale page images. It has no text layer, so it exercises the path where quotes cannot be checked and where endpoints that receive extracted text cannot code the PDF at all.

## Where the codes come from

The articles and their codes are from Wallrich et al. (2024), *The relationship between team diversity and team performance*, https://doi.org/10.1007/s10869-024-09977-0, with the dataset at https://github.com/LukasWallrich/diversity_meta. The manual adapts that project's codebook.

Two things differ from the original data:

- **Missing codes.** The original dataset has one blank for every missing value. Here each blank is split into `not_reported` or `not_applicable`. Cells where that split needed a decision were reviewed by the meta-analysis's first author and are marked `validated` in the `check` column; the `note` column records the reason. For `xia_2022`, three blanks were replaced by values on review.
- **Reversed correlations.** For `qamar_2022` the original coding reversed the sign of the correlations, so only their size is compared (`number_abs`).

Fields about the study repeat on every row of the same article, so one disagreement on such a field counts once per row.

## Running it

```sh
# Code the small set with a provider and score it (calls the provider)
python evals/run_eval.py run --set small --provider gemini --name my-run

# Compare with the reference run; exits 1 on a regression
python evals/run_eval.py score evals/runs/my-run --baseline evals/baselines/gemini-3.7-flash-small/summary.json

# Re-validate and re-check saved responses with the current code (no provider call)
python evals/run_eval.py replay evals/runs/my-run
```

`run` reads the key from `GEMINI_API_KEY`, `OPENROUTER_API_KEY` or `OPENAI_COMPATIBLE_API_KEY` (with `--base-url`). It keeps PDFs that already have a saved result, so a run stopped by a provider error can be repeated to finish it; `--force` recodes everything. Results go to `evals/runs/`, which is not committed.

### With a Claude or ChatGPT plan instead of an API key

```sh
python evals/run_eval.py run --set small --provider claude_cli --name my-claude-run
python evals/run_eval.py run --set small --provider codex_cli --name my-codex-run
```

`claude_cli` runs the installed Claude Code CLI (`claude -p`) and `codex_cli` runs the Codex CLI (`codex exec`). Each uses the plan its CLI is signed in to, so no API key is needed. The prompt, schema, validation and quote check are the same as for the other providers. The approach follows [coarse](https://github.com/Davidvandijcke/coarse), which runs its paper reviews through the same CLIs.

- **Isolation.** Every PDF is coded in an empty temporary folder, with the CLI's MCP servers, user configuration, project instructions and session saving turned off. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and related variables are removed for the call, because with one of them set the CLI bills an API account instead of the plan.
- **Same input as the vendor's API.** Each CLI gets the article the way its vendor's API would take a PDF, and no tools. So both can code the image-only copy.
- **Claude Code** gets our own short system prompt and the PDF itself, as a document block in a `stream-json` input message. That is the block the Claude API uses for PDFs.
- **Codex** gets our own short instructions. It accepts images but no PDF. An API that takes a PDF gives the model each page's text and an image of the page, so Codex gets the text layer in the prompt, when the PDF has one, and the pages as images. They are rendered at 150 dpi with `pdftoppm`, which is part of [poppler](https://poppler.freedesktop.org/) and must be on the PATH. Codex runs `gpt-6.1-sol` unless `--model` names another model.
- **Nothing of yours is added.** Neither CLI sends your skills, plugins, memory or `AGENTS.md`/`CLAUDE.md` files. Codex has no single switch for this. Its instruction blocks are turned off one by one; it runs in a temporary home folder that holds only a link to your login, because it always reads the global `AGENTS.md` from its home; and it gets a copy of the model's catalog entry with the tools removed, because the catalog decides which tool definitions go out. The login must be in Codex's `auth.json` (the default), not in the system keyring.
- **What the CLIs still add.** Codex adds nothing: a one-line prompt costs 26 input tokens in total. Claude Code adds about 550 tokens that no setting removes while it is signed in to a plan: a line saying it is a Claude agent, a note giving your account's email address, and a block describing the machine (folder, platform, date, model). It also turns on adaptive thinking at medium effort. That can change between CLI versions, so each saved result records `cli_version`; compare a run only with a baseline from the same provider.
- **Terms.** These providers start the CLI you installed and signed in to yourself and never read its credentials. Whether scripted use fits your plan is between you and the vendor; check their current terms before relying on it.

A run counts as a regression when accuracy falls more than 5 percentage points below the baseline or more PDFs end as `needs_review` or `error`. Model output varies between runs, so smaller changes are reported but do not fail.

`tests/test_eval_set.py` checks the files against each other and replays the committed baseline, so the normal test suite catches a change to validation or quote matching that alters its scores.

## What the score means

A cell is correct when the coded value matches the human code: the same category, a number within the tolerance in `compare`, or a missing value where the human code is missing. The summary also reports, per run:

- accuracy per field and by the model's confidence rating;
- for missing values, whether the model chose the same missing code;
- the outcome of the quote check;
- PDF statuses and token counts.

Judgement fields (`interdependence`, `task_complexity`, `team_longevity`) rest on interpretation, so expect lower scores there than for fields read directly from the article.

## Licences

Every article states a Creative Commons Attribution licence in the PDF itself; `articles.csv` lists the DOI of each. The image-only copy is a derivative of `xia_2022.pdf` under the same licence.
