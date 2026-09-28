#!/usr/bin/env python3
"""
validate_summaries.py

Consistency/QA gate for the reading-summarizer pipeline.

This is the "Consistency" requirement made concrete: nothing produced by the
LLM summarization step is allowed into the export step unless it passes this
schema + readability check. Run standalone against a summaries.json file to
debug the dataset, or import `validate_entry` / `validate_all` from the
pipeline.

Usage:
    python3 validate_summaries.py data/summaries.json
"""

import json
import sys
from dataclasses import dataclass, field

REQUIRED_OK_FIELDS = ["title", "main_idea", "supporting_points", "key_takeaways"]

MIN_SUPPORTING_POINTS = 3
MAX_SUPPORTING_POINTS = 6
MIN_TAKEAWAYS = 2
MAX_TAKEAWAYS = 4

MAX_WORDS_MAIN_IDEA = 40
MAX_WORDS_PER_BULLET = 45  # keeps output "student-friendly" / skimmable


@dataclass
class ValidationResult:
    entry_id: object
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _word_count(text: str) -> int:
    return len(text.split())


def validate_entry(entry: dict) -> ValidationResult:
    entry_id = entry.get("id", "?")
    status = entry.get("status")

    # Non-"ok" rows just need to be self-consistent, not carry a summary.
    if status == "skipped_duplicate":
        if "duplicate_of" not in entry:
            return ValidationResult(entry_id, False, ["skipped_duplicate row missing 'duplicate_of'"])
        return ValidationResult(entry_id, True)

    if status == "error":
        if "error" not in entry:
            return ValidationResult(entry_id, False, ["error row missing 'error' message"])
        return ValidationResult(entry_id, True)

    if status != "ok":
        return ValidationResult(entry_id, False, [f"unrecognized status: {status!r}"])

    errors, warnings = [], []

    # 1. Structural consistency: every summary must follow the same shape.
    for field_name in REQUIRED_OK_FIELDS:
        if field_name not in entry or not entry[field_name]:
            errors.append(f"missing required field '{field_name}'")

    if errors:
        return ValidationResult(entry_id, False, errors, warnings)

    sp = entry["supporting_points"]
    kt = entry["key_takeaways"]

    if not isinstance(sp, list):
        errors.append("supporting_points must be a list")
    elif not (MIN_SUPPORTING_POINTS <= len(sp) <= MAX_SUPPORTING_POINTS):
        errors.append(
            f"supporting_points has {len(sp)} items; expected "
            f"{MIN_SUPPORTING_POINTS}-{MAX_SUPPORTING_POINTS}"
        )

    if not isinstance(kt, list):
        errors.append("key_takeaways must be a list")
    elif not (MIN_TAKEAWAYS <= len(kt) <= MAX_TAKEAWAYS):
        errors.append(
            f"key_takeaways has {len(kt)} items; expected {MIN_TAKEAWAYS}-{MAX_TAKEAWAYS}"
        )

    # 2. Student-friendly / concise: flag anything that reads like a wall of text.
    if _word_count(entry["main_idea"]) > MAX_WORDS_MAIN_IDEA:
        warnings.append(
            f"main_idea is {_word_count(entry['main_idea'])} words "
            f"(recommend <= {MAX_WORDS_MAIN_IDEA})"
        )

    if isinstance(sp, list):
        for i, bullet in enumerate(sp):
            if _word_count(bullet) > MAX_WORDS_PER_BULLET:
                warnings.append(f"supporting_points[{i}] exceeds {MAX_WORDS_PER_BULLET} words")

    if isinstance(kt, list):
        for i, bullet in enumerate(kt):
            if _word_count(bullet) > MAX_WORDS_PER_BULLET:
                warnings.append(f"key_takeaways[{i}] exceeds {MAX_WORDS_PER_BULLET} words")

    return ValidationResult(entry_id, len(errors) == 0, errors, warnings)


def validate_all(entries: list) -> list:
    results = [validate_entry(e) for e in entries]

    # 3. De-dup check across the whole batch: same URL should not produce two
    #    independent "ok" summaries (wastes LLM calls, confuses the reader).
    seen_urls = {}
    for e in entries:
        if e.get("status") != "ok":
            continue
        url = e.get("url")
        if url in seen_urls:
            results.append(
                ValidationResult(
                    e.get("id", "?"),
                    False,
                    [f"duplicate 'ok' summary for URL already summarized in row {seen_urls[url]}"],
                )
            )
        else:
            seen_urls[url] = e.get("id", "?")

    return results


def main(path: str) -> int:
    with open(path) as f:
        entries = json.load(f)

    results = validate_all(entries)
    n_ok = sum(1 for r in results if r.ok)
    n_fail = len(results) - n_ok

    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"[{status}] row {r.entry_id}")
        for e in r.errors:
            print(f"    error:   {e}")
        for w in r.warnings:
            print(f"    warning: {w}")

    print(f"\n{n_ok} passed, {n_fail} failed, {len(results)} total checks")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/summaries.json"
    sys.exit(main(path))
