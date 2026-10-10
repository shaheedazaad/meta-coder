"""Regression check for MetaCoder's coding against a small human-coded set.

    python evals/run_eval.py run --set small --provider gemini --name my-run
    python evals/run_eval.py score evals/runs/my-run --baseline evals/baselines/small.json
    python evals/run_eval.py replay evals/runs/my-run

`run` sends each PDF of the chosen set to a provider through the same code path
the app uses (prompt, schema, validation, quote check) and saves one JSON file
per PDF. `score` compares saved results with `ground_truth.csv`. `replay`
re-validates and re-checks the saved raw responses with the current code, with
no API call, so changes to validation or quote matching can be checked for free.

API keys are read from GEMINI_API_KEY, OPENROUTER_API_KEY or, for an
OpenAI-compatible endpoint, OPENAI_COMPATIBLE_API_KEY with --base-url. The
providers claude_cli and codex_cli need no key: they run the signed-in Claude
Code or Codex CLI, see `cli_providers.py`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from meta_coder.coding_sheet import CodingSheetRow  # noqa: E402
from meta_coder.extraction import ProviderError, parse_json_response  # noqa: E402
from meta_coder.manual import read_coding_manual  # noqa: E402
from meta_coder.mechanism import validate_response  # noqa: E402
from meta_coder.prompts import PROMPT_VERSION  # noqa: E402
from meta_coder.providers import default_model, extract_pdf_effects  # noqa: E402
from meta_coder.quote_check import annotate_quote_checks  # noqa: E402

import cli_providers  # noqa: E402

KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}
# A run fails against its baseline when accuracy drops by more than this, or
# when more PDFs need review. Model output varies between runs, so a single
# changed cell is not a regression.
ACCURACY_TOLERANCE = 0.05


def read_csv(name: str) -> list[dict[str, str]]:
    with (ROOT / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def rows_by_pdf(set_name: str) -> dict[str, list[CodingSheetRow]]:
    wanted = [a["source_pdf"] for a in read_csv("articles.csv") if set_name in a["sets"].split()]
    if not wanted:
        raise SystemExit(f"No articles in set {set_name!r} (use small or broad).")
    out: dict[str, list[CodingSheetRow]] = {name: [] for name in wanted}
    for row in read_csv("coding_sheet.csv"):
        if row["source_pdf"] in out:
            out[row["source_pdf"]].append(
                CodingSheetRow(row["row_id"], row["source_pdf"], row["locator"], row["authors"], row["year"])
            )
    return out


# --- Comparing one coded value with the human code ---------------------------

def _number(value: object) -> float | None:
    try:
        return float(str(value))
    except ValueError:
        return None


def _text(value: object) -> str:
    return " ".join(str(value).casefold().split())


def values_match(coded: object, truth: str, compare: str) -> bool:
    """`compare` is `exact`, `bool`, `years`, `number:<tolerance>` or
    `number_abs:<tolerance>` (sign ignored)."""

    kind, _, tolerance = compare.partition(":")
    if kind in ("number", "number_abs"):
        got, want = _number(coded), _number(truth)
        if got is None or want is None or isinstance(coded, bool):
            return False
        if kind == "number_abs":
            got, want = abs(got), abs(want)
        return abs(got - want) <= float(tolerance)
    if kind == "bool":
        return isinstance(coded, bool) and coded == (truth == "true")
    if kind == "years":
        clean = lambda text: _text(text).replace("–", "-").replace("—", "-").replace(" ", "")  # noqa: E731
        return clean(coded) == clean(truth)
    return _text(coded) == _text(truth)


def score(results: dict[str, dict]) -> dict:
    """Compare every ground-truth cell of the PDFs in `results` with its coded cell."""

    truth = read_csv("ground_truth.csv")
    pdf_of_row = {row["row_id"]: row["source_pdf"] for row in read_csv("coding_sheet.csv")}
    per_field: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_confidence: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    missing = Counter()
    quote_checks = Counter()
    errors = []
    total = correct = 0
    for cell in truth:
        result = results.get(pdf_of_row[cell["row_id"]])
        if result is None:
            continue
        coded_row = (result.get("coded_by_row_id") or {}).get(cell["row_id"])
        coded = coded_row.get(cell["field"]) if isinstance(coded_row, dict) else None
        coded = coded if isinstance(coded, dict) else {}
        value = coded.get("value")
        if cell["missing"]:
            ok = value is None
            if ok:
                missing["same_code" if coded.get("missing") == cell["missing"] else "other_code"] += 1
            else:
                missing["value_given"] += 1
        else:
            ok = value is not None and values_match(value, cell["value"], cell["compare"])
            if value is None:
                missing["value_missed"] += 1
        total += 1
        correct += ok
        per_field[cell["field"]][0] += ok
        per_field[cell["field"]][1] += 1
        if coded.get("confidence"):
            per_confidence[coded["confidence"]][0] += ok
            per_confidence[coded["confidence"]][1] += 1
        if coded.get("quote_check"):
            quote_checks[coded["quote_check"]] += 1
        if not ok:
            errors.append({
                "row_id": cell["row_id"], "field": cell["field"],
                "expected": cell["value"] or cell["missing"],
                "coded": value if value is not None else coded.get("missing"),
            })
    return {
        "pdfs": len(results),
        "statuses": dict(Counter(result.get("status") for result in results.values())),
        "cells": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else None,
        "per_field": {name: {"correct": c, "cells": n} for name, (c, n) in sorted(per_field.items())},
        "missing_values": dict(missing),
        "accuracy_by_confidence": {
            level: {"correct": c, "cells": n} for level, (c, n) in sorted(per_confidence.items())
        },
        "quote_checks": dict(quote_checks),
        "input_tokens": sum(result.get("input_tokens") or 0 for result in results.values()),
        "output_tokens": sum(result.get("output_tokens") or 0 for result in results.values()),
        "errors": errors,
    }


def regressions(summary: dict, baseline: dict) -> list[str]:
    problems = []
    if summary["accuracy"] is not None and baseline.get("accuracy") is not None:
        if summary["accuracy"] < baseline["accuracy"] - ACCURACY_TOLERANCE:
            problems.append(f"accuracy fell from {baseline['accuracy']:.1%} to {summary['accuracy']:.1%}")
    not_ok = lambda s: sum(n for status, n in s.get("statuses", {}).items() if status != "ok")  # noqa: E731
    if not_ok(summary) > not_ok(baseline):
        problems.append(f"PDFs not ok rose from {not_ok(baseline)} to {not_ok(summary)}")
    return problems


# --- Commands ----------------------------------------------------------------

def load_run(run_dir: Path) -> dict[str, dict]:
    results = {}
    for path in sorted(run_dir.glob("*.json")):
        if path.name != "summary.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            results[data["source_pdf"]] = data
    if not results:
        raise SystemExit(f"No saved results in {run_dir}.")
    return results


def print_summary(summary: dict) -> None:
    print(f"PDFs: {summary['pdfs']}  statuses: {summary['statuses']}")
    print(f"Cells correct: {summary['correct']}/{summary['cells']} ({summary['accuracy']:.1%})")
    for name, counts in summary["per_field"].items():
        print(f"  {name:26s} {counts['correct']}/{counts['cells']}")
    print(f"Missing values: {summary['missing_values']}")
    print(f"Accuracy by confidence: {summary['accuracy_by_confidence']}")
    print(f"Quote checks: {summary['quote_checks']}")
    print(f"Tokens: {summary['input_tokens']} in, {summary['output_tokens']} out")
    for error in summary["errors"]:
        print(f"  wrong: {error['row_id']} {error['field']}: expected {error['expected']!r}, coded {error['coded']!r}")


def finish(run_dir: Path, results: dict[str, dict], baseline: Path | None) -> int:
    summary = score(results)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print_summary(summary)
    if baseline is None:
        return 0
    problems = regressions(summary, json.loads(baseline.read_text(encoding="utf-8")))
    for problem in problems:
        print(f"REGRESSION: {problem}")
    return 1 if problems else 0


def command_run(args: argparse.Namespace) -> int:
    uses_cli = args.provider in cli_providers.PROVIDERS
    api_key = "" if uses_cli else os.environ.get(KEY_ENV[args.provider], "")
    if not api_key and not uses_cli and args.provider != "openai_compatible":
        raise SystemExit(f"Set {KEY_ENV[args.provider]} to run with {args.provider}.")
    manual = read_coding_manual(ROOT / "manual.yml")
    if uses_cli:
        model = args.model or cli_providers.DEFAULT_MODELS[args.provider]
        version = cli_providers.cli_version(args.provider)
    else:
        model = args.model or default_model(args.provider)
    run_dir = ROOT / "runs" / args.name
    run_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for source_pdf, rows in rows_by_pdf(args.set).items():
        saved = run_dir / f"{Path(source_pdf).stem}.json"
        if saved.is_file() and not args.force:
            # Keep finished PDFs so a run interrupted by a provider error can be resumed.
            data = json.loads(saved.read_text(encoding="utf-8"))
            if data.get("status") in ("ok", "needs_review"):
                results[source_pdf] = data
                print(f"{source_pdf}: {data['status']} (saved)", flush=True)
                continue
        if uses_cli:
            result = cli_providers.extract_pdf_effects(
                provider=args.provider, pdf_path=ROOT / "pdfs" / source_pdf, manual=manual, rows=rows, model=model,
            )
        else:
            result = extract_pdf_effects(
                provider=args.provider, pdf_path=ROOT / "pdfs" / source_pdf, manual=manual, rows=rows,
                api_key=api_key, model=model, base_url=args.base_url,
            )
        data = {
            "source_pdf": source_pdf, "provider": args.provider, "model": model,
            "prompt_version": PROMPT_VERSION, "status": result.status, "error": result.error,
            "coded_by_row_id": result.coded_by_row_id, "raw_response": result.raw_response,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "duration_sec": round(result.duration_sec, 1),
        }
        if uses_cli:
            data["cli_version"] = version
        saved.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        results[source_pdf] = data
        print(f"{source_pdf}: {result.status}" + (f" ({result.error})" if result.error else ""), flush=True)
    return finish(run_dir, results, args.baseline)


def command_score(args: argparse.Namespace) -> int:
    return finish(args.run_dir, load_run(args.run_dir), args.baseline)


def command_replay(args: argparse.Namespace) -> int:
    manual = read_coding_manual(ROOT / "manual.yml")
    sheet = read_csv("coding_sheet.csv")
    results = load_run(args.run_dir)
    for source_pdf, data in results.items():
        if not data.get("raw_response"):
            continue
        requested = {row["row_id"] for row in sheet if row["source_pdf"] == source_pdf}
        try:
            parsed, repaired = parse_json_response(data["raw_response"])
        except ProviderError as exc:
            data.update(status="error", error=str(exc), coded_by_row_id={})
            continue
        checked = validate_response(parsed, requested, manual.effects, confidence=manual.confidence)
        annotate_quote_checks(checked.coded_by_row_id, ROOT / "pdfs" / source_pdf)
        data.update(
            status="ok" if checked.ok and repaired is None else "needs_review",
            error=checked.error, coded_by_row_id=checked.coded_by_row_id,
        )
        print(f"{source_pdf}: {data['status']}" + (f" ({data['error']})" if data["error"] else ""))
    return finish(args.run_dir, results, args.baseline)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="code a set with a provider, then score it")
    run.add_argument("--set", default="small", choices=["small", "broad"])
    run.add_argument("--provider", default="gemini", choices=sorted((*KEY_ENV, *cli_providers.PROVIDERS)))
    run.add_argument("--model", default="")
    run.add_argument("--base-url", default="")
    run.add_argument("--name", required=True, help="folder name under evals/runs/")
    run.add_argument("--force", action="store_true", help="recode PDFs that already have a saved result")
    run.set_defaults(handler=command_run)
    for name, handler, text in (
        ("score", command_score, "score saved results"),
        ("replay", command_replay, "re-validate saved raw responses with the current code, then score"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("run_dir", type=Path)
        sub.set_defaults(handler=handler)
    for sub in commands.choices.values():
        sub.add_argument("--baseline", type=Path, help="summary.json of an earlier run to compare with")
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
