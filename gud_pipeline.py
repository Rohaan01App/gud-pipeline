#!/usr/bin/env python3
"""
Gud content pipeline.

Runs unattended. Finds good news, verifies it, writes it, illustrates it,
and emits one JSON bundle per day per cluster.

    python gud_pipeline.py check-feeds     # validate the allowlist (do this first)
    python gud_pipeline.py run --dry-run   # plumbing test, no API calls, no cost
    python gud_pipeline.py run             # the real thing
    python gud_pipeline.py run --limit 40  # cheap first real run
    python gud_pipeline.py bank            # promote publishable stories to evergreen
    python gud_pipeline.py publish         # write tomorrow's bundle
    python gud_pipeline.py stats           # queue health

Storage is SQLite. At six stories a day you will not outgrow it for years;
migrate to Postgres/Supabase when the app needs a shared backend, not before.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from feeds import FEEDS, CLUSTERS, SCENES, SCENE_HINTS
import prompts

# ---------------------------------------------------------------- config

DB_PATH = Path(os.getenv("GUD_DB", "gud.db"))
OUT_DIR = Path(os.getenv("GUD_OUT", "output"))

TRIAGE_MODEL = "claude-haiku-4-5-20251001"
SCORE_MODEL_A = "claude-haiku-4-5-20251001"
SCORE_MODEL_B = "claude-sonnet-5"
WRITE_MODEL = "claude-sonnet-5"
VERIFY_MODEL = "claude-sonnet-5"

# Gates. Tightening these is how you fix quality; never loosen them to fill a slot.
MIN_AXIS_SCORE = 6          # every rubric axis must clear this
MIN_TOTAL_SCORE = 38        # out of 50
MAX_MODEL_DISAGREEMENT = 3  # per-axis gap between the two scoring models (3 = normal disagreement; 2 was too tight)
MIN_CORROBORATION = 2       # independent sources required
DEDUPE_THRESHOLD = 0.55     # title similarity for "same story"
TRIAGE_BATCH = 20

# Rough USD per million tokens. VERIFY THESE — pricing changes.
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}

STOPWORDS = set("""a an the of in on at to for from by with and or but as is are was were be been
being this that these those it its his her their our your my we they he she you i not no than then
after before over under new first study says say said could would may might can will more most new""".split())

COST = {"in": 0, "out": 0, "calls": 0, "usd": 0.0}


# ---------------------------------------------------------------- db

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY,
        url_hash TEXT UNIQUE,
        url TEXT, title TEXT, summary_raw TEXT, source TEXT, tier INTEGER,
        feed_cluster TEXT, published TEXT, fetched_at TEXT,
        cluster_id INTEGER,
        state TEXT DEFAULT 'new',      -- new|triaged|rejected|scored|written|publishable|published|banked
        reject_reason TEXT,
        cluster TEXT,
        scores TEXT, total INTEGER,
        headline TEXT, tease TEXT, summary TEXT, deeper TEXT,
        scene TEXT, verify TEXT,
        publish_on TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_state ON items(state);
    CREATE INDEX IF NOT EXISTS idx_cluster ON items(cluster, state);
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, at TEXT, collected INTEGER, kept INTEGER,
        published INTEGER, usd REAL, notes TEXT
    );
    """)
    return con


# ---------------------------------------------------------------- anthropic

_client = None

def client():
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError:
            sys.exit("pip install anthropic")
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("Set ANTHROPIC_API_KEY")
        _client = anthropic.Anthropic(api_key=key)
    return _client


def ask(model, system, user, max_tokens=2000, retries=3):
    """One LLM call with backoff and cost tracking. Returns text."""
    for attempt in range(retries):
        try:
            r = client().messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            pin, pout = PRICES.get(model, (3.0, 15.0))
            COST["in"] += r.usage.input_tokens
            COST["out"] += r.usage.output_tokens
            COST["calls"] += 1
            COST["usd"] += (r.usage.input_tokens * pin + r.usage.output_tokens * pout) / 1_000_000
            return "".join(b.text for b in r.content if b.type == "text")
        except Exception as e:
            if attempt == retries - 1:
                print(f"    ! {type(e).__name__}: {e}")
                return None
            time.sleep(2 ** attempt * 2)
    return None


def parse_json(text, expect_list=False):
    """LLMs occasionally wrap JSON in prose or fences. Recover what we can."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    pattern = r"\[.*\]" if expect_list else r"\{.*\}"
    m = re.search(pattern, text, re.S)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------- stage 1: collect

def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_article(url, timeout=12):
    """Pull the full article body from its page. RSS gives only short snippets,
    which is too thin to write a grounded summary from — this gets the real text.
    Best-effort: on any failure we fall back to the snippet we already have."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(600_000).decode("utf-8", "ignore")
    except Exception:
        return None

    # Strip scripts/styles, then pull paragraph text — where article bodies live.
    raw = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", raw, flags=re.S | re.I)
    text = " ".join(clean_html(p) for p in paras)
    text = re.sub(r"\s+", " ", text).strip()
    # Only trust it if we got a real article's worth of text.
    return text[:8000] if len(text) > 400 else None


def collect(limit=None):
    """Pull every allowlisted feed. Skip anything already seen."""
    try:
        import feedparser
    except ImportError:
        sys.exit("pip install feedparser")

    con = db()
    added = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=4)

    for name, url, tier, cluster_hint in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"  ! {name}: {e}")
            continue
        if parsed.bozo and not parsed.entries:
            print(f"  ! {name}: unreadable feed")
            continue

        n = 0
        for e in parsed.entries[:40]:
            link = getattr(e, "link", None)
            title = clean_html(getattr(e, "title", ""))
            if not link or not title:
                continue

            # Skip old items so a first run doesn't ingest an entire archive.
            pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if pub:
                try:
                    when = datetime(*pub[:6], tzinfo=timezone.utc)
                    if when < cutoff:
                        continue
                except (ValueError, TypeError):
                    when = None
            else:
                when = None

            body = clean_html(getattr(e, "summary", "") or getattr(e, "description", ""))
            uh = hashlib.sha256(link.encode()).hexdigest()[:20]
            try:
                con.execute(
                    "INSERT INTO items (url_hash,url,title,summary_raw,source,tier,"
                    "feed_cluster,published,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uh, link, title, body[:4000], name, tier, cluster_hint,
                     when.isoformat() if when else "", datetime.now(timezone.utc).isoformat()))
                added += 1
                n += 1
            except sqlite3.IntegrityError:
                pass  # already have it
            if limit and added >= limit:
                break
        if n:
            print(f"  {name:22s} +{n}")
        if limit and added >= limit:
            break

    con.commit()
    con.close()
    print(f"  collected {added} new items")
    return added


def check_feeds():
    """Validate the allowlist. Feed URLs rot; run this before your first real run."""
    try:
        import feedparser
    except ImportError:
        sys.exit("pip install feedparser")
    ok = bad = 0
    for name, url, tier, _ in FEEDS:
        try:
            p = feedparser.parse(url)
            n = len(p.entries)
            if n:
                print(f"  OK   t{tier} {name:22s} {n:3d} entries")
                ok += 1
            else:
                print(f"  DEAD t{tier} {name:22s} {url}")
                bad += 1
        except Exception as e:
            print(f"  FAIL t{tier} {name:22s} {type(e).__name__}")
            bad += 1
    print(f"\n  {ok} working, {bad} need attention")
    if bad:
        print("  Fix or remove the broken ones in feeds.py — a dead feed is a silent quality leak.")


# ---------------------------------------------------------------- stage 2: dedupe

def tokens(s):
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in STOPWORDS}


def similarity(a, b):
    """Jaccard on content words. Cheap, no API, and reliable for near-duplicate news.
    Swap in embeddings later if you want cross-lingual clustering."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe():
    """Group the same story from multiple outlets. Cluster size == corroboration count,
    which is exactly the signal gate 2 needs."""
    con = db()
    rows = con.execute(
        "SELECT id,title,summary_raw FROM items WHERE state='new' AND cluster_id IS NULL"
    ).fetchall()
    if not rows:
        con.close()
        return 0

    toks = {r["id"]: tokens(r["title"] + " " + (r["summary_raw"] or "")[:300]) for r in rows}
    clusters, assigned = [], {}

    for r in rows:
        placed = False
        for ci, members in enumerate(clusters):
            if similarity(toks[r["id"]], toks[members[0]]) >= DEDUPE_THRESHOLD:
                members.append(r["id"])
                assigned[r["id"]] = ci
                placed = True
                break
        if not placed:
            assigned[r["id"]] = len(clusters)
            clusters.append([r["id"]])

    for item_id, ci in assigned.items():
        con.execute("UPDATE items SET cluster_id=? WHERE id=?", (ci, item_id))
    con.commit()
    con.close()

    multi = sum(1 for c in clusters if len(c) > 1)
    print(f"  {len(rows)} items -> {len(clusters)} clusters ({multi} corroborated)")
    return len(clusters)


def corroboration(con, cluster_id):
    """Distinct sources covering this story, and the best tier among them."""
    rows = con.execute(
        "SELECT DISTINCT source,tier FROM items WHERE cluster_id=?", (cluster_id,)
    ).fetchall()
    return len(rows), min([r["tier"] for r in rows], default=3)


# ---------------------------------------------------------------- stage 3: triage

def triage(dry=False):
    con = db()
    rows = con.execute(
        "SELECT id,title,summary_raw,source FROM items WHERE state='new' ORDER BY id"
    ).fetchall()
    if not rows:
        con.close()
        return
    kept = 0

    for i in range(0, len(rows), TRIAGE_BATCH):
        batch = rows[i:i + TRIAGE_BATCH]
        listing = "\n\n".join(
            f'[{j}] {r["title"]}\n{(r["summary_raw"] or "")[:260]}'
            for j, r in enumerate(batch))

        if dry:
            results = [{"i": j, "keep": j % 4 == 0, "cluster": random.choice(CLUSTERS),
                        "reason": "dry run"} for j in range(len(batch))]
        else:
            raw = ask(TRIAGE_MODEL, prompts.TRIAGE_SYSTEM,
                      prompts.TRIAGE_USER.format(n=len(batch), items=listing), 2000)
            results = parse_json(raw, expect_list=True) or []

        by_index = {r.get("i"): r for r in results if isinstance(r, dict)}
        for j, row in enumerate(batch):
            res = by_index.get(j)
            if res and res.get("keep"):
                cl = res.get("cluster") if res.get("cluster") in CLUSTERS else row["source"]
                cl = cl if cl in CLUSTERS else "progress"
                con.execute("UPDATE items SET state='triaged', cluster=? WHERE id=?", (cl, row["id"]))
                kept += 1
            else:
                reason = (res or {}).get("reason", "no response")
                con.execute("UPDATE items SET state='rejected', reject_reason=? WHERE id=?",
                            (reason[:80], row["id"]))
        con.commit()
        print(f"    triaged {min(i + TRIAGE_BATCH, len(rows))}/{len(rows)}  kept {kept}")

    con.close()
    print(f"  kept {kept}/{len(rows)} ({100 * kept // max(len(rows),1)}%)")


# ---------------------------------------------------------------- stage 4: dual score

def axes(d):
    return [d.get(k, 0) for k in ("verified", "agency", "durable", "nonpartisan", "authentic")]


def score(dry=False, cap=30):
    """Gate 2 (corroboration) + gate 3 (two models must agree)."""
    con = db()
    rows = con.execute(
        "SELECT * FROM items WHERE state='triaged' ORDER BY tier ASC, id DESC LIMIT ?", (cap,)
    ).fetchall()
    if not rows:
        con.close()
        return
    passed = 0

    for row in rows:
        corr, best_tier = corroboration(con, row["cluster_id"])

        # Gate 2 — corroboration.
        # Tier-1 (wires, journals, agencies) and tier-2 (established newsrooms with
        # their own fact-checkers: BBC, Guardian, New Scientist, Mongabay) are
        # trustworthy enough to stand alone. Only tier-3 — solutions-journalism and
        # NGO feeds, the ones most likely to run something unverified — needs a
        # second independent source. This matches the gate to who the source is.
        if best_tier <= 2:
            pass  # trusted newsroom, single source is fine
        elif corr < MIN_CORROBORATION:
            con.execute("UPDATE items SET state='rejected', reject_reason=? WHERE id=?",
                        (f"tier-3 single source (corroboration {corr})", row["id"]))
            continue

        text = f'{row["title"]}\n\n{row["summary_raw"] or ""}'[:6000]
        user = prompts.SCORE_USER.format(source=row["source"], tier=row["tier"],
                                         corroboration=corr, title=row["title"], text=text)

        if dry:
            a = {"verified": 8, "agency": 8, "durable": 8, "nonpartisan": 9, "authentic": 8,
                 "headline": row["title"][:70], "tease": "A dry-run tease sentence."}
            b = dict(a)
        else:
            a = parse_json(ask(SCORE_MODEL_A, prompts.SCORE_SYSTEM, user, 600))
            b = parse_json(ask(SCORE_MODEL_B, prompts.SCORE_SYSTEM, user, 600))

        if not a or not b:
            con.execute("UPDATE items SET state='rejected', reject_reason='score parse failed' WHERE id=?",
                        (row["id"],))
            continue

        aa, bb = axes(a), axes(b)

        # Gate 3 — model disagreement means the story is ambiguous. Ambiguous doesn't publish.
        gap = max(abs(x - y) for x, y in zip(aa, bb))
        if gap > MAX_MODEL_DISAGREEMENT:
            con.execute("UPDATE items SET state='rejected', reject_reason=? WHERE id=?",
                        (f"models disagree by {gap}", row["id"]))
            continue

        merged = [(x + y) / 2 for x, y in zip(aa, bb)]
        total = round(sum(merged))

        if min(merged) < MIN_AXIS_SCORE or total < MIN_TOTAL_SCORE:
            con.execute("UPDATE items SET state='rejected', reject_reason=? WHERE id=?",
                        (f"scored {total}/50, weakest {min(merged):.0f}", row["id"]))
            continue

        # Prefer the more capable model's copy.
        con.execute(
            "UPDATE items SET state='scored', scores=?, total=?, headline=?, tease=? WHERE id=?",
            (json.dumps({"a": aa, "b": bb, "merged": merged, "corroboration": corr}),
             total, b.get("headline", a.get("headline", row["title"]))[:120],
             b.get("tease", a.get("tease", ""))[:160], row["id"]))
        passed += 1
        print(f'    {total}/50  {row["headline"] or row["title"][:60]}')

    con.commit()
    con.close()
    print(f"  {passed}/{len(rows)} passed scoring")


# ---------------------------------------------------------------- stages 5-7

def pick_scene(cluster, text):
    low = text.lower()
    for keys, scene in SCENE_HINTS:
        if any(k in low for k in keys):
            return scene
    return random.choice(SCENES.get(cluster, ["sun"]))


def write_and_verify(dry=False, cap=12):
    """Stage 5 writes, stage 6 checks every claim against the source. One retry, then reject."""
    con = db()
    rows = con.execute(
        "SELECT * FROM items WHERE state='scored' ORDER BY total DESC LIMIT ?", (cap,)
    ).fetchall()
    if not rows:
        con.close()
        return
    ok = 0

    for row in rows:
        sources = [r["source"] for r in con.execute(
            "SELECT DISTINCT source FROM items WHERE cluster_id=?", (row["cluster_id"],))]
        # Fetch the full article so the writer has real material, not a 2-line snippet.
        full = None if dry else fetch_article(row["url"])
        if full:
            text = f'{row["title"]}\n\n{full}'[:8000]
            print(f'    fetched full text ({len(full)} chars)')
        else:
            text = f'{row["title"]}\n\n{row["summary_raw"] or ""}'[:8000]
            print(f'    using snippet only (full fetch failed)')

        written, verdict = None, None
        for attempt in range(3):   # three chances to get it right before giving up
            if dry:
                written = {"summary": "Dry-run summary of about ninety words. " * 6,
                           "deeper": "Dry-run deeper context. " * 30}
                verdict = {"ok": True, "problems": [], "copied": False}
                break

            written = parse_json(ask(WRITE_MODEL, prompts.WRITE_SYSTEM,
                prompts.WRITE_USER.format(headline=row["headline"], cluster=row["cluster"],
                                          sources=", ".join(sources), text=text), 1800))
            if not written or not written.get("summary"):
                continue

            verdict = parse_json(ask(VERIFY_MODEL, prompts.VERIFY_SYSTEM,
                prompts.VERIFY_USER.format(text=text, summary=written["summary"] + "\n\n" + written.get("deeper", "")), 800))
            if verdict and verdict.get("ok") and not verdict.get("copied"):
                break
            if verdict:
                print(f'    retry: {verdict.get("problems", [])[:2]}')

        if not written or not verdict or not verdict.get("ok") or verdict.get("copied"):
            problems = (verdict or {}).get("problems", ["write failed"])
            con.execute("UPDATE items SET state='rejected', reject_reason=? WHERE id=?",
                        (f"verify: {str(problems)[:70]}", row["id"]))
            continue

        scene = pick_scene(row["cluster"], text)
        con.execute(
            "UPDATE items SET state='publishable', summary=?, deeper=?, scene=?, verify=? WHERE id=?",
            (written["summary"], written.get("deeper", ""), scene, json.dumps(verdict), row["id"]))
        ok += 1
        print(f'    ready [{row["cluster"]}] {row["headline"][:56]}')

    con.commit()
    con.close()
    print(f"  {ok}/{len(rows)} passed verification")


# ---------------------------------------------------------------- stage 8: publish

def publish(days=1):
    """Emit one JSON bundle per day. This is what the app fetches from your CDN."""
    con = db()
    OUT_DIR.mkdir(exist_ok=True)
    made = 0

    for offset in range(1, days + 1):
        day = (date.today() + timedelta(days=offset)).isoformat()
        existing = OUT_DIR / f"{day}.json"
        if existing.exists():
            try:
                prev = json.loads(existing.read_text())
                if prev.get("stories"):   # already has real content -> leave it
                    continue
            except Exception:
                pass
            # else: stale/empty file (e.g. from a dry run) -> rebuild it
        bundle = {"date": day, "stories": {}, "ledger": 0}

        # Never run the same underlying story in two clusters on one day, and never
        # repeat one a user may have seen recently. Two friends with different
        # interests should not both get "Portugal".
        used = set()
        recent = {r["cluster_id"] for r in con.execute(
            "SELECT DISTINCT cluster_id FROM items WHERE state='published' "
            "AND publish_on >= ?", ((date.today() - timedelta(days=45)).isoformat(),))}

        for cluster in CLUSTERS:
            row = None
            for cand in con.execute(
                    "SELECT * FROM items WHERE state='publishable' AND cluster=? "
                    "ORDER BY total DESC LIMIT 12", (cluster,)):
                if cand["cluster_id"] not in used and cand["cluster_id"] not in recent:
                    row = cand
                    break
            fallback = False

            # Gate 5 — nothing good enough? Take from the evergreen bank. Never lower the bar.
            if not row:
                for cand in con.execute(
                        "SELECT * FROM items WHERE state='banked' AND cluster=? "
                        "ORDER BY RANDOM() LIMIT 12", (cluster,)):
                    if cand["cluster_id"] not in used:
                        row = cand
                        fallback = True
                        break
            if not row:
                continue
            used.add(row["cluster_id"])

            srcs = [dict(name=r["source"], url=r["url"], tier=r["tier"]) for r in con.execute(
                "SELECT DISTINCT source,url,tier FROM items WHERE cluster_id=? LIMIT 4",
                (row["cluster_id"],))]

            bundle["stories"][cluster] = {
                "id": row["url_hash"], "cluster": cluster, "headline": row["headline"],
                "tease": row["tease"], "summary": row["summary"], "deeper": row["deeper"],
                "scene": row["scene"], "sources": srcs,
                "scores": json.loads(row["scores"] or "{}").get("merged", []),
                "evergreen": fallback,
            }
            if not fallback:
                con.execute("UPDATE items SET state='published', publish_on=? WHERE id=?",
                            (day, row["id"]))

        bundle["ledger"] = con.execute(
            "SELECT COUNT(*) c FROM items WHERE state IN ('publishable','published','banked')"
        ).fetchone()["c"]

        (OUT_DIR / f"{day}.json").write_text(json.dumps(bundle, indent=2))
        n = len(bundle["stories"])
        ever = sum(1 for s in bundle["stories"].values() if s["evergreen"])
        print(f"  {day}.json  {n}/6 clusters" + (f"  ({ever} from bank)" if ever else ""))
        made += 1

    con.commit()
    con.close()
    return made


def bank():
    """Promote surplus publishable stories to the evergreen bank.
    Good news doesn't expire, so the bank never goes stale — it's your safety net forever."""
    con = db()
    n = 0
    for cluster in CLUSTERS:
        held = con.execute("SELECT COUNT(*) c FROM items WHERE state='banked' AND cluster=?",
                           (cluster,)).fetchone()["c"]
        if held >= 20:
            continue
        rows = con.execute(
            "SELECT id FROM items WHERE state='publishable' AND cluster=? "
            "ORDER BY total DESC LIMIT ?", (cluster, 20 - held)).fetchall()
        for r in rows[1:]:  # keep the best one live for tomorrow
            con.execute("UPDATE items SET state='banked' WHERE id=?", (r["id"],))
            n += 1
    con.commit()
    con.close()
    print(f"  banked {n} evergreen stories")


def stats():
    con = db()
    print("\n  QUEUE")
    for r in con.execute("SELECT state, COUNT(*) c FROM items GROUP BY state ORDER BY c DESC"):
        print(f"    {r['state']:14s} {r['c']:5d}")

    print("\n  READY BY CLUSTER")
    for cluster in CLUSTERS:
        p = con.execute("SELECT COUNT(*) c FROM items WHERE state='publishable' AND cluster=?",
                        (cluster,)).fetchone()["c"]
        b = con.execute("SELECT COUNT(*) c FROM items WHERE state='banked' AND cluster=?",
                        (cluster,)).fetchone()["c"]
        flag = "  <-- LOW" if p + b < 3 else ""
        print(f"    {cluster:11s} {p} ready, {b} banked{flag}")

    print("\n  TOP REJECTION REASONS  (this is your editorial feedback loop)")
    rows = con.execute(
        "SELECT reject_reason r, COUNT(*) c FROM items WHERE state='rejected' "
        "AND reject_reason IS NOT NULL GROUP BY r ORDER BY c DESC LIMIT 12").fetchall()
    for r in rows:
        print(f"    {r['c']:4d}  {r['r']}")
    con.close()
    print()


# ---------------------------------------------------------------- run

def run(dry=False, limit=None):
    t0 = time.time()
    print("\n[1/8] collect");            n = collect(limit)
    print("[2/8] dedupe");               dedupe()
    print("[3/8] triage");               triage(dry)
    print("[4/8] score (dual model)");   score(dry)
    print("[5-7/8] write + verify");     write_and_verify(dry)
    print("[8/8] publish");              made = publish(days=1)
    bank()

    con = db()
    con.execute("INSERT INTO runs (at,collected,kept,published,usd,notes) VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), n, 0, made, COST["usd"],
                 "dry" if dry else ""))
    con.commit()
    con.close()

    print(f"\n  {time.time()-t0:.0f}s | {COST['calls']} calls | "
          f"{COST['in']:,} in / {COST['out']:,} out | ${COST['usd']:.3f}")

    # Tripwire 1 — the queue running dry means something broke upstream.
    con = db()
    thin = [c for c in CLUSTERS if con.execute(
        "SELECT COUNT(*) x FROM items WHERE state IN ('publishable','banked') AND cluster=?",
        (c,)).fetchone()["x"] < 3]
    con.close()
    if thin:
        print(f"  !! TRIPWIRE: thin clusters -> {', '.join(thin)}")


def main():
    ap = argparse.ArgumentParser(description="Gud content pipeline")
    ap.add_argument("command", choices=["run", "check-feeds", "publish", "bank", "stats"])
    ap.add_argument("--dry-run", action="store_true", help="no API calls, no cost")
    ap.add_argument("--limit", type=int, help="cap items collected (cheap first run)")
    ap.add_argument("--days", type=int, default=1, help="days to publish ahead")
    a = ap.parse_args()

    if a.command == "check-feeds":
        check_feeds()
    elif a.command == "run":
        run(a.dry_run, a.limit)
    elif a.command == "publish":
        publish(a.days)
    elif a.command == "bank":
        bank()
    elif a.command == "stats":
        stats()


if __name__ == "__main__":
    main()
