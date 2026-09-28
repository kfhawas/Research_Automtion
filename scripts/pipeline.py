#!/usr/bin/env python3
"""
pipeline.py — Reading List Summarizer

End-to-end automation: spreadsheet of links -> structured, validated,
student-friendly summaries -> shareable export (.docx, ready for Google Docs).

Pipeline stages (mirrors the n8n/Make.com design in workflow/README.md):

    1. read_input()          spreadsheet -> rows, de-duplicated by URL
    2. fetch_article()        URL -> raw article text (with graceful failure)
    3. redact_sensitive()     strip emails/phone numbers before they ever
                               reach the LLM step (privacy constraint)
    4. summarize()             article text -> structured summary dict
                               (pluggable: real LLM call, or precomputed
                               lookup for offline/demo runs)
    5. validate_summaries.py   schema + conciseness gate (imported)
    6. export_docx() / export_html()   compiled, shareable output

Usage:
    # Offline/demo mode (no LLM key needed) — uses precomputed summaries,
    # exactly how this was run to build the test-dataset demo:
    python3 pipeline.py --input data/reading_list.xlsx \\
                         --summaries data/summaries.json \\
                         --output output/reading_summaries.docx

    # Live mode (requires ANTHROPIC_API_KEY):
    python3 pipeline.py --input data/reading_list.xlsx \\
                         --output output/reading_summaries.docx
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import openpyxl

sys.path.insert(0, os.path.dirname(__file__))
from validate_summaries import validate_entry  # noqa: E402

SUMMARY_PROMPT_TEMPLATE = """You are a study-assistant that turns one article into a \
concise, student-friendly summary. Follow this exact structure and return ONLY valid JSON:

{{
  "title": "<article title>",
  "main_idea": "<1-2 sentence core argument, <=40 words>",
  "supporting_points": ["<3-6 bullets, key arguments/evidence, each <=45 words>"],
  "key_takeaways": ["<2-4 bullets, so-what conclusions, each <=45 words>"]
}}

Rules:
- Plain language a student can skim in under a minute.
- Do not invent facts not in the source text.
- No markdown, no extra commentary, JSON only.

ARTICLE TEXT:
{article_text}
"""

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\b(?:\+?\d[\d\-\s]{7,}\d)\b")


@dataclass
class Row:
    id: int
    title: Optional[str]
    url: str
    category: Optional[str]
    notes: Optional[str]


def read_input(path: str) -> list:
    """Spreadsheet -> rows, de-duplicated by URL (keeps first occurrence)."""
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows, seen_urls = [], {}

    for raw in ws.iter_rows(min_row=2, values_only=True):
        record = dict(zip(headers, raw))
        url = (record.get("url") or "").strip()
        if not url:
            continue
        row = Row(
            id=record.get("id"),
            title=record.get("title") or None,
            url=url,
            category=record.get("category") or None,
            notes=record.get("notes") or None,
        )
        if url in seen_urls:
            print(f"  [dedup] row {row.id} skipped, duplicate of row {seen_urls[url]}")
            continue
        seen_urls[url] = row.id
        rows.append(row)

    return rows


def fetch_article(url: str, timeout: int = 15) -> str:
    """Fetch + extract main article text. Raises on any failure so the
    caller can record a clean 'error' row instead of crashing the batch."""
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "reading-summarizer/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n\n".join(p for p in paragraphs if len(p.split()) > 5)
    if not text:
        raise ValueError("no extractable article text found")
    return text


def redact_sensitive(text: str) -> str:
    """Strip obvious PII before it's sent to any LLM/API step. This is the
    concrete answer to the 'sensitive info should not leave the tool'
    constraint: redact at the edge, before the network call."""
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = PHONE_RE.sub("[redacted-phone]", text)
    return text


def summarize(article_text: str, row: Row, precomputed: dict) -> dict:
    """Pluggable summarization step.

    - If ANTHROPIC_API_KEY is set, calls the Claude API with the fixed
      structure prompt above and parses the JSON response.
    - Otherwise, falls back to a precomputed lookup (by row id) — this is
      how the bundled demo dataset was produced: an LLM (Claude, run
      interactively) generated the structured summaries for the test
      articles, saved to data/summaries.json, so the rest of the pipeline
      can be exercised end-to-end without a live API key.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        client = anthropic.Anthropic()
        prompt = SUMMARY_PROMPT_TEMPLATE.format(article_text=article_text[:12000])
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        return json.loads(raw)

    entry = precomputed.get(row.url)
    if entry is None or entry.get("status") != "ok":
        raise RuntimeError(
            f"no live LLM key set and no precomputed summary for {row.url} "
            "(set ANTHROPIC_API_KEY, or add this URL to --summaries)"
        )
    return {
        "title": entry["title"],
        "main_idea": entry["main_idea"],
        "supporting_points": entry["supporting_points"],
        "key_takeaways": entry["key_takeaways"],
    }


def run_pipeline(input_path: str, summaries_path: Optional[str]) -> list:
    precomputed = {}
    if summaries_path:
        with open(summaries_path) as f:
            for e in json.load(f):
                # Only index successful summaries. Duplicate/error rows in the
                # demo file share a URL with a real "ok" row (that's the point
                # of the test data) — indexing them too would let a later
                # "error"/"skipped_duplicate" entry clobber the good summary
                # for the same URL. Keep the first "ok" entry per URL.
                if e.get("status") == "ok":
                    precomputed.setdefault(e["url"], e)

    rows = read_input(input_path)
    results = []

    for row in rows:
        print(f"[row {row.id}] {row.url}")
        try:
            if summaries_path and row.url in precomputed:
                # demo/offline mode: skip the network fetch, reuse the
                # (already-fetched) source text implicitly via the
                # precomputed summary
                summary = summarize(article_text="", row=row, precomputed=precomputed)
            else:
                text = fetch_article(row.url)
                text = redact_sensitive(text)
                summary = summarize(text, row, precomputed)

            entry = {"id": row.id, "url": row.url, "status": "ok", **summary}
        except Exception as exc:  # noqa: BLE001 - batch job must not die on one bad row
            entry = {"id": row.id, "url": row.url, "status": "error", "error": str(exc)}
            print(f"  [error] {exc}")

        check = validate_entry(entry)
        if not check.ok:
            print(f"  [validation FAILED] {check.errors}")
            entry["status"] = "validation_failed"
            entry["validation_errors"] = check.errors
        elif check.warnings:
            print(f"  [validation warning] {check.warnings}")

        results.append(entry)

    return results


def export_docx(results: list, output_path: str) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading("Reading List — Summaries", level=0)
    doc.add_paragraph(
        "Auto-generated, structure-checked summaries. Each entry follows: "
        "Title → Main Idea → Supporting Points → Key Takeaways."
    )

    ok_results = [r for r in results if r.get("status") == "ok"]
    for r in ok_results:
        doc.add_heading(r["title"], level=1)
        doc.add_paragraph(r["url"]).italic = True
        p = doc.add_paragraph()
        p.add_run("Main idea: ").bold = True
        p.add_run(r["main_idea"])

        doc.add_paragraph("Supporting points:").runs[0].bold = True
        for point in r["supporting_points"]:
            doc.add_paragraph(point, style="List Bullet")

        doc.add_paragraph("Key takeaways:").runs[0].bold = True
        for takeaway in r["key_takeaways"]:
            doc.add_paragraph(takeaway, style="List Bullet")

    skipped = [r for r in results if r.get("status") != "ok"]
    if skipped:
        doc.add_heading("Skipped / failed rows (for debugging)", level=1)
        for r in skipped:
            doc.add_paragraph(f"Row {r['id']} ({r['url']}): {r.get('status')} — "
                               f"{r.get('error') or r.get('duplicate_of') or r.get('validation_errors')}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)
    print(f"\nSaved {output_path}")


def export_html(results: list) -> str:
    """Rich-text HTML for a Google Doc / Drive upload — this is what the
    export step hands to the Drive API or a Zapier/Make/n8n Google Docs
    node in production."""
    parts = [
        "<h1>Reading List — Summaries</h1>",
        "<p><i>Auto-generated, structure-checked summaries. Each entry follows: "
        "Title &rarr; Main Idea &rarr; Supporting Points &rarr; Key Takeaways.</i></p>",
    ]
    for r in results:
        if r.get("status") != "ok":
            continue
        parts.append(f"<h2>{r['title']}</h2>")
        parts.append(f"<p><i>{r['url']}</i></p>")
        parts.append(f"<p><b>Main idea:</b> {r['main_idea']}</p>")
        parts.append("<p><b>Supporting points:</b></p><ul>")
        parts += [f"<li>{p}</li>" for p in r["supporting_points"]]
        parts.append("</ul><p><b>Key takeaways:</b></p><ul>")
        parts += [f"<li>{k}</li>" for k in r["key_takeaways"]]
        parts.append("</ul><hr/>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="xlsx with id,title,url,category,notes")
    ap.add_argument("--summaries", help="precomputed summaries.json (offline/demo mode)")
    ap.add_argument("--output", default="output/reading_summaries.docx")
    ap.add_argument("--results-json", default="output/results.json",
                     help="raw pipeline results, incl. skipped/error rows")
    args = ap.parse_args()

    results = run_pipeline(args.input, args.summaries)

    os.makedirs(os.path.dirname(args.results_json), exist_ok=True)
    with open(args.results_json, "w") as f:
        json.dump(results, f, indent=2)

    export_docx(results, args.output)

    n_ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{n_ok}/{len(results)} rows summarized successfully.")


if __name__ == "__main__":
    main()
