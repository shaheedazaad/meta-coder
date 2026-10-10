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
- **Claude Code** gets our own short system prompt and one tool, Read, limited to a folder that holds only the PDF. It reads the article itself, so the image-only copy can be coded too.
- **Codex** gets our own short instructions and no tools. It has no tool that reads a PDF (given file access, it improvises with whatever shell tools the machine has), so it receives the text layer in the prompt, and the image-only copy ends as `error`. It runs `gpt-6.1-sol` unless `--model` names another model.
- **Not the same as an API call.** Both CLIs still add their own prompt around ours (about 2,000 tokens for Claude Code and 7,000 for Codex when measured), and that prompt changes between CLI versions. Each saved result records `cli_version`; compare a run only with a baseline from the same provider, and expect a shift after a CLI update.
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
