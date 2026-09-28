# Reading List Summarizer

AI-assisted automation that turns a spreadsheet of article links into consistent,
student-friendly, structure-checked summaries, exported to a shareable Google Doc.

Built as a take-home exercise: design an AI-assisted workflow that generates
structured, reliable summaries from a set of reading materials.

**Design write-up:** https://claude.ai/artifact/4232VgGkwpiH4LfUMfXpgH
**Live demo export (Google Doc):** https://docs.google.com/document/d/1Mf3KJ_Qx4yirSQTj7gJH1K6LYh5_faDoqZpD0v_VDyI/edit

## What it does

1. **Input** — reads a spreadsheet of links (`data/reading_list.xlsx`), de-duplicating by URL.
2. **Fetch** — downloads each article and extracts the body text.
3. **Redact** — strips emails/phone numbers before anything reaches an LLM.
4. **Summarize** — one fixed prompt forces every summary into the same shape:
   `Title → Main Idea → Supporting Points → Key Takeaways`.
5. **Validate** — `validate_summaries.py` re-checks structure, bullet counts, and
   word limits (student-friendly length) before anything is exported. Rows that
   fail are flagged, never silently dropped.
6. **Export** — compiles passing summaries into a shareable Google Doc (or a
   downloadable `.docx`).

```
data/reading_list.xlsx --> fetch --> redact --> summarize (fixed prompt)
                                                     |
                                                     v
                                              validate (schema + length)
                                                     |
                                          pass -------+------- fail
                                           |                    |
                                           v                    v
                                   export (Doc/.docx)     flagged for review
```

## Files

| Path | What it is |
| --- | --- |
| `scripts/pipeline.py` | The runnable pipeline: fetch, redact, summarize (pluggable LLM), validate, export |
| `scripts/validate_summaries.py` | Standalone consistency/QA gate — schema + word-limit checks |
| `data/reading_list.xlsx` | 6-row test dataset (4 normal articles, 1 intentional duplicate, 1 intentional broken link) |
| `data/summaries.json` | Structured output from the demo run, used as the offline/demo LLM stand-in |
| `output/reading_summaries.docx` | Downloadable export of the demo run |
| `output/results.json` | Raw per-row pipeline output, including the skipped/error rows |

## Running it

```bash
pip install -r requirements.txt

# Offline/demo mode — no API key needed, reuses the precomputed summaries
# in data/summaries.json (this is how the bundled demo output was produced):
python3 scripts/pipeline.py --input data/reading_list.xlsx \
                             --summaries data/summaries.json \
                             --output output/reading_summaries.docx

# Live mode — set ANTHROPIC_API_KEY and drop --summaries to call a real LLM
# for every row instead:
export ANTHROPIC_API_KEY=sk-...
python3 scripts/pipeline.py --input data/reading_list.xlsx \
                             --output output/reading_summaries.docx

# Run the validator standalone against any summaries.json:
python3 scripts/validate_summaries.py data/summaries.json
```

## Design notes

- **Consistency** is enforced twice: the fixed prompt template, and an independent
  schema/word-limit check in `validate_summaries.py` that runs before export.
- **Student-friendly** output is a hard constraint, not a style hint: main ideas are
  capped at 40 words, bullets at 45 words, and the validator flags anything over.
- **Sensitive info** is redacted (emails/phone numbers) before it ever reaches an
  LLM call; for genuinely confidential material, swap in a private/enterprise LLM
  endpoint in `summarize()` rather than a consumer API.
- **Debugging the test dataset** caught a real bug: an early version indexed
  precomputed summaries by URL, so the duplicate-URL test row silently overwrote
  the valid summary for the same URL. Fixed by indexing only successful (`status:
  ok`) rows. See the design doc's "Consistency" section for the full story.
- A **no-code equivalent** (n8n / Make / Zapier: Sheets trigger → HTTP/scraper node
  → LLM node with the same fixed prompt → validation Code node → Google Docs node)
  is described in the design doc for anyone deploying this without Python.

## Test dataset

| Row | Source | Tests |
| --- | --- | --- |
| 1-4 | Wikipedia: AI, Climate Change, Remote Work, Spaced Repetition | Normal summarization path |
| 5 | Same URL as row 1 | De-duplication |
| 6 | Nonexistent page | Fetch-failure handling |

Result: 4/6 rows summarized, 1 correctly de-duplicated, 1 correctly failed on a
caught network error — no crash, no silent drop.
