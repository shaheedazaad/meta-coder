# Regression set

A small, human-coded set of open-access articles for checking that changes to MetaCoder's prompt, response schema or validation do not make coding worse. It is a cheap routine check, not a validation study: the set is small and comes from one meta-analysis.

Assembled by Lukas Wallrich.

## What is in it

| File | Content |
| --- | --- |
| `articles.csv` | DOI, licence, checksum, and which set each PDF belongs to |
| `manual.yml` | Coding manual with 16 fields, confidence ratings on |
| `coding_sheet.csv` | 33 rows, one per correlation, with locators |
| `ground_truth.csv` | The human code for every row and field |
| `baselines/` | Saved provider responses and scores of reference runs |
| `run_eval.py` | Runs a set, scores it, or replays saved responses |
| `fetch_pdfs.py` | Downloads the PDFs and checks their checksums |

The PDFs (nine CC BY articles and one image-only copy, about 12 MB) are not in the repository. They are assets of the [`evals-data-v1` release](https://github.com/shaheedazaad/meta-coder/releases/tag/evals-data-v1), so they do not add to every clone. Download them once with:

```sh
python evals/fetch_pdfs.py
```

They land in `evals/pdfs/`, which git ignores. If the set changes, publish the new PDFs as a new release and update `RELEASE_URL` in `fetch_pdfs.py` and the checksums in `articles.csv` together.

There are two sets:

- **small**: 5 PDFs and 17 rows. Four articles and the image-only copy.
- **broad**: 10 PDFs and 33 rows. All nine articles and the image-only copy.

The image-only copy, `xia_2022_scanned.pdf`, is `xia_2022.pdf` rendered to greyscale page images. It has no text layer, so it exercises the path where endpoints that receive extracted text cannot code the PDF at all.

## Where the codes come from

The articles and their codes are from Wallrich et al. (2024), *The relationship between team diversity and team performance*, https://doi.org/10.1007/s10869-024-09977-0, with the dataset at https://github.com/LukasWallrich/diversity_meta. The manual adapts that project's codebook.

Two things differ from the original data:

- **Missing codes.** The original dataset has one blank for every missing value. Here each blank is split into `not_reported` or `not_applicable` in the `missing` column. Cells where that split needed a decision were reviewed by the meta-analysis's first author and are marked `validated` in the `check` column; the `note` column records the reason. For `xia_2022`, three blanks were replaced by values on review. MetaCoder does not yet code why a value is missing, so for now a missing human code matches any empty value; the reasons are kept for when it does.
- **Reversed correlations.** For `qamar_2022` the original coding reversed the sign of the correlations, so only their size is compared (`number_abs`).

Fields about the study repeat on every row of the same article, so one disagreement on such a field counts once per row.

## When it runs

**Offline, in CI on every pull request.** `tests/test_eval_set.py` checks the files against each other and the PDFs against their checksums. It also replays every committed baseline: the saved raw responses go back through the current parsing, validation and scoring, and must reproduce the committed `summary.json`. That catches a change to parsing, validation or scoring at no cost. A deliberate change updates `summary.json` in the same pull request, so its effect shows in the diff. Replay cannot show the effect of a prompt or schema change, because the saved responses came from the old prompt.

**Live, by hand.** Only a live run shows whether a change to the prompt, the schema, field descriptions or how PDFs reach a provider makes coding better or worse. Before merging such a change, run the small set on the branch and compare it with the baseline:

```sh
python evals/run_eval.py run --set small --provider gemini --name pr-123 \
    --baseline evals/baselines/<baseline>/summary.json
```

Live runs are not in CI. Each costs tokens, model output varies between runs, and GitHub does not give secrets to workflows on pull requests from forks.

## Running it

```sh
# Code the small set with a provider and score it (calls the provider)
python evals/run_eval.py run --set small --provider gemini --name my-run

# Compare with a reference run; exits 1 on a regression
python evals/run_eval.py score evals/runs/my-run --baseline evals/baselines/<baseline>/summary.json

# Re-parse and re-validate saved responses with the current code (no provider call)
python evals/run_eval.py replay evals/runs/my-run
```

`run` reads the key from `GEMINI_API_KEY`, `OPENROUTER_API_KEY` or `OPENAI_COMPATIBLE_API_KEY` (with `--base-url`). It keeps PDFs already saved as `ok` or `needs_review` and recodes those saved as `error`, so a run stopped by a provider error can be repeated to finish it. `--force` recodes everything, including `needs_review` results. Results go to `evals/runs/`, which is not committed.

A run counts as a regression when accuracy falls more than 5 percentage points below the baseline or more PDFs end as `needs_review` or `error`. It also fails when it is not comparable with the baseline: a different number of scored cells (a PDF missing from the run, or another set) or a different provider and model. A different prompt version is allowed, since checking a prompt change is the point. Model output varies between runs, so smaller changes are reported but do not fail.

`replay` does not know the provider's finish reason, so a response that was held for review only because generation stopped early replays as `ok`.

### Recording a baseline

Run the small set, then copy the run folder into `evals/baselines/` under a name that says what produced it (provider, model, set), and commit it. The replay test picks it up automatically. Record a new baseline when the prompt version changes or a change to the prompt or schema is accepted.

## What the score means

A cell is correct when the coded value matches the human code: the same category, a number within the tolerance in `compare`, or an empty value (null or a blank string) where the human code is missing. A PDF whose row is absent from the response earns nothing, blanks included. The summary also reports, per run:

- accuracy per field and by the model's confidence rating;
- for missing human codes, how often the model left the value empty;
- PDF statuses and token counts.

Judgement fields (`interdependence`, `task_complexity`, `team_longevity`) rest on interpretation, so expect lower scores there than for fields read directly from the article.

## Licences

Every article states a Creative Commons Attribution licence in the PDF itself; `articles.csv` lists the DOI of each. The image-only copy is a derivative of `xia_2022.pdf` under the same licence.
