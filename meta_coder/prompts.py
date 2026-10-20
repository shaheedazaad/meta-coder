"""The extraction prompt shared by every provider adapter (gemini.py, openrouter.py,
openai_compatible.py), so the instructions a model sees do not depend on which
provider ran. Adapters differ only in how the article itself is attached.

The response schema (mechanism.build_response_schema) constrains the shape and the
allowed category values; this prompt carries what the schema cannot express, in
particular the coding manual's description and what each category level means.
"""

from __future__ import annotations

from .coding_sheet import CodingSheetRow
from .manual import CodingManual


# Identifies the extraction contract a result was produced under: this prompt's
# wording, the codebook rendering below, and the response schema built by
# mechanism.build_response_schema. Bump it whenever any of those change in a way
# that could change what a model returns, so stored results can be told apart.
PROMPT_VERSION = "6"

BASELINE_RULES = """You are a research assistant coding effects for a meta-analysis.

Rules:
1. Only use information present in the article. No outside knowledge, no inference
   or best-guessing, unless a field's description explicitly says otherwise.
2. If you cannot give a value for a field, set "value" to null and set "missing" to
   the reason:
   - "not_reported": the article does not report it.
   - "not_applicable": the field does not apply to this study or effect, for example
     a follow-up interval for a study without a follow-up.
   - "unclear": the article addresses it, but ambiguously or inconsistently, so no
     single value can be determined.
   Explain the reason in "evidence" rather than guessing. When you give a value, set
   "missing" to null. A missing field is invalid.
3. Code values must follow the exact formatting the field description specifies
   (units, decimal places, category label text).
4. "evidence" must be concise but specific: say where in the article the value
   comes from (section, table or figure) and how you derived it, not a vague
   paraphrase. Put the passage that supports the value in "quote", copied exactly as
   it appears in the article: the same words, spelling and numbers, with nothing
   paraphrased, corrected or left out, and at most about 40 words. Quote one
   continuous passage; do not join separate passages. Set "quote" to null when the
   value comes from a table or figure, or when nothing can be quoted, as for a value
   that is not reported. Set "page" to the page of the PDF file the passage or value
   is on, counting the file's first page as 1, or to null if you cannot tell.
5. For a categorical field, set "value" to exactly one of its listed levels, using
   the level descriptions to decide which applies. If more than one level seems to
   apply, choose the best fit and explain the ambiguity in the row's `notes` field.
   Only for a field marked "select all that apply", set "value" to a list of every
   level that applies, in the order the levels are listed. Never return an empty
   list: if no level applies, set "value" to null as in rule 2.
6. Every object in "effects" MUST include a "row_id" that exactly matches one of the
   requested row IDs below. Never invent, rename, or omit a row_id. Return exactly
   one object per requested row, no more, no fewer."""


# Added to the rules only when the manual sets `confidence: true`.
CONFIDENCE_RULE = """7. For every field except `notes`, set "confidence" to how sure you are that the
   coded value, or the reason it is missing, is correct:
   - "high": the article states it explicitly and unambiguously.
   - "medium": it takes some interpretation, or combining information from
     different places in the article.
   - "low": it is inferred or approximate, or the article is ambiguous or
     inconsistent about it."""


def _indent(text: str, prefix: str) -> str:
    """Indent continuation lines so multi-line descriptions stay under their item."""

    return text.strip().replace("\n", "\n" + prefix)


def render_codebook(manual: CodingManual) -> str:
    """Render the coding manual's fields as compact Markdown for the prompt."""

    lines = ["Coding manual:"]
    if manual.description:
        lines.append(f"About this coding manual: {_indent(manual.description, '  ')}")
    lines.append("")
    lines.append("Fields to code for each row:")
    for name, spec in manual.effects.items():
        kind = "categorical" if spec.is_categorical else spec.type
        if spec.multiple:
            kind += ", select all that apply"
        lines.append(f"- `{name}` ({kind})")
        if spec.description:
            lines.append(f"  Description: {_indent(spec.description, '    ')}")
        if spec.levels:
            lines.append("  Levels:")
            for level in spec.levels:
                if level.description:
                    lines.append(f"  - `{level.value}`: {_indent(level.description, '      ')}")
                else:
                    lines.append(f"  - `{level.value}`")
    return "\n".join(lines)


def build_extraction_prompt(
    manual: CodingManual, rows: list[CodingSheetRow], *, article: str = "attached PDF"
) -> str:
    """`article` names where the article is, e.g. "article text below" for
    adapters that send extracted text instead of the PDF itself."""

    row_lines = "\n".join(
        f"- row_id: {row.row_id}\n  locator: {row.locator or '(none)'}" for row in rows
    )
    rules = f"{BASELINE_RULES}\n{CONFIDENCE_RULE}" if manual.confidence else BASELINE_RULES
    return (
        f"{rules}\n\n"
        f"Effect definition for this meta-analysis:\n{manual.effect_definition}\n\n"
        f"{render_codebook(manual)}\n\n"
        f"Code the following {len(rows)} row(s) from the {article}. Each row is one "
        "study/experiment/condition; use its locator to find the right one. A row with no "
        "locator means this manuscript has only one effect of interest: identify and code it. "
        "If it is not clear which effect that is, say so in the row's `notes` field rather "
        "than guessing:\n"
        f"{row_lines}"
    )
