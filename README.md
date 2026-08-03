# Gud content pipeline

Finds good news, verifies it, writes it, illustrates it, and emits one JSON bundle
per day covering six interest clusters. Runs unattended.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## First hour

```bash
python gud_pipeline.py check-feeds      # some feeds will be dead. fix them.
python gud_pipeline.py run --dry-run    # plumbing test, zero cost
python gud_pipeline.py run --limit 40   # first real run, ~$0.30
python gud_pipeline.py stats            # see what got rejected and why
```

Read the rejection reasons. They are your editorial feedback loop — if good stories
are being killed, loosen a gate in `gud_pipeline.py`; if rubbish is getting through,
tighten `prompts.py`. Expect to spend two or three evenings on this. It's the real work.

## Daily, once you're happy

```bash
python gud_pipeline.py run
```

Put it on GitHub Actions cron at 03:00 UTC. Commit `output/*.json` or push to a bucket.

## The eight stages

| Stage | What it does | Gate |
|---|---|---|
| 1 collect | Pulls ~27 allowlisted RSS feeds, skips anything seen or >4 days old | Allowlist |
| 2 dedupe | Groups the same story across outlets by content-word overlap | — |
| 3 triage | Haiku kills ~90% — PR, politics, tragedy-as-uplift, cute-but-empty | — |
| 4 score | **Two models score independently.** Disagreement > 2 = reject | Corroboration + consensus |
| 5 write | Sonnet writes 90-word card + 300-word deeper, original prose only | — |
| 6 verify | Separate call sees only source + summary, lists unsupported claims | Grounding |
| 7 illustrate | Picks an SVG scene from keywords — no photographs, no licensing risk | — |
| 8 publish | One bundle per day, per cluster, with evergreen fallback | Threshold |

## Files

- `feeds.py` — the allowlist. **This is your editorial policy.** Highest-leverage file here.
- `prompts.py` — all LLM prompts. Tune editorial judgement here, never in the logic.
- `gud_pipeline.py` — the machine.

## Tuning knobs

At the top of `gud_pipeline.py`:

```python
MIN_AXIS_SCORE = 6           # every rubric axis must clear this
MIN_TOTAL_SCORE = 38         # out of 50
MAX_MODEL_DISAGREEMENT = 2   # ambiguous stories don't publish
MIN_CORROBORATION = 2        # independent sources required
DEDUPE_THRESHOLD = 0.55      # raise if unrelated stories merge
```

**Never loosen these to fill a slot.** An empty cluster falls back to the evergreen
bank; a lowered bar publishes something false. A repeat is boring. A wrong story is fatal.

## Output

`output/2026-07-25.json`:

```json
{
  "date": "2026-07-25",
  "ledger": 412,
  "stories": {
    "nature": {
      "id": "a1b2c3...", "headline": "...", "tease": "...",
      "summary": "...", "deeper": "...", "scene": "whale",
      "sources": [{"name": "Reuters", "url": "...", "tier": 1}],
      "scores": [9, 8, 9, 10, 8], "evergreen": false
    }
  }
}
```

This is exactly what the app fetches. Serve it from a CDN and the app needs no
backend at all on its hottest path.

## Building the evergreen bank

Your one-weekend job. Run the pipeline daily for two or three weeks with
`--limit` off, then:

```bash
python gud_pipeline.py bank
```

Surplus publishable stories become fallbacks. Aim for 20 per cluster. Because good
news doesn't expire, the bank never goes stale — it protects you forever.

## Costs

Roughly **$2–4/day** at full volume (~800 items ingested, 30 dual-scored, 12
written and verified). The dual-model scoring is the main expense and it's the
gate doing the most work — don't cut it.

Verify current pricing at docs.claude.com; the `PRICES` dict needs to match.

## What this does not do

- No app. This produces JSON. The app is a separate build.
- No hosting. Add a CDN or object store.
- No push notifications. Wire to Expo/APNs when the app exists.
- No user flagging endpoint. Needs the app first.
