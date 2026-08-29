# EDGAR AI Pivot Monitor

A real-time classification pipeline over the SEC EDGAR full-text search API. It polls for
newly published 8-K filings, scores them against a weighted keyword taxonomy, sends the
survivors to an LLM for structured evaluation, and dispatches whatever clears a confidence
threshold to Discord, Slack, ntfy, or the macOS notification centre.

Roughly 1,800 lines of Python, no framework.

## Pipeline

```
EDGAR full-text search        keyword taxonomy         LLM evaluation        dispatch
  8-K filings, last N days  →  high / medium signal  →  structured verdict →  Discord
  ~dozens per query            weighted scoring         + confidence 1-10     Slack / ntfy
                                                                              desktop
        │                             │                        │
        └── dedup by accession ───────┴────────────────────────┘
            (evaluated and alerted tracked separately)
```

## The parts that were actually hard

**EDGAR refuses anonymous clients.** Full-text search requires a declared `User-Agent`.
Requests without one are rejected, and the failure does not look like an auth problem.

**Filings do not fit in a context window.** An 8-K with exhibits regularly runs past what
is sensible to send to a model. Truncating at the head throws away the announcement, which
is usually buried after the boilerplate. `_smart_excerpt()` locates candidate passages and
assembles an excerpt around them instead.

**Deduplication has two different costs, so it has two different ledgers.** The same filing
surfaces under multiple keyword queries and again on every subsequent poll. Evaluating a
filing twice wastes an API call; alerting on it twice wastes the reader's attention and
trains them to ignore the channel. Those are tracked separately, on disk, keyed by
accession number.

**Silence is ambiguous.** A monitor that finds nothing looks identical to a monitor that
died three days ago. A periodic heartbeat separates the two.

**The market-cap ceiling is a screening artifact, not a belief.** Candidates are often
screened after the market has already repriced them, so the observed cap is higher than the
pre-filing cap. The threshold is set deliberately loose, and borderline cases are passed to
the evaluation stage rather than dropped.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the values

python edgar_ai_pivot_monitor.py              # one-time scan, last 3 days
python edgar_ai_pivot_monitor.py --days 7     # widen the window
python edgar_ai_pivot_monitor.py --watch      # continuous polling
python edgar_ai_pivot_monitor.py --schedule   # windowed polling only
python edgar_ai_pivot_monitor.py --screener   # Finviz nano-cap screener
```

`LOG_LEVEL=DEBUG` for verbose diagnostics.

## Configuration

| variable | purpose |
|---|---|
| `OPENAI_API_KEY` | filing evaluation |
| `OPENAI_MODEL` | defaults to `gpt-4o` |
| `DISCORD_WEBHOOK_URL` | alert channel |
| `NTFY_TOPIC` | push notifications and heartbeat |

## Conventions

Diagnostics go through `logging`; report surfaces go through `print`. The split is
deliberate. In `--watch` and `--schedule` this runs unattended for days, where a bare print
with no timestamp is useless for reconstructing what happened. But the alert card and the
scan summary are the product, and wrapping them in log formatting would make the tool worse
to read.

## Status

Archived. Built and run in 2026, kept public as a work sample rather than as maintained
software. Nothing it emits is financial advice.
