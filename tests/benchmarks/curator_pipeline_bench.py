"""
curator_pipeline_bench.py — CURATOR-role benchmark with FULL production-pitch fidelity.
=======================================================================================

Unlike curator_bench.py (hand-written natural-language prompts), this feeds each
candidate model the EXACT deletion-pitch context production builds for the curator:

  • the verified-data block  (themes / Wikipedia significance / OMDb / extended
    plot / creator) — assembled cache-only via ensure_verified_data,
  • the rating block, the category TASTE section, learned CONSIDERATIONS,
    WATCH STATUS, the domain vocab guideline and a random critique axis,

…all produced by calling the SAME production helpers generate_deletion_proposals
uses. So a candidate gets "everything the models in Curatarr would get".

Test items = the "weirdest / hardest" samples = the RICHEST-source real library
items (longest overview + most metadata, the established pick_hardest heuristic),
balanced across movie / show / anime / music, pulled straight from the live
enrichment cache (data/cache/enrichment.db, the v2:raw:* rows).

Model handling mirrors auto_benchmark_v2's SAFETY: it snapshots the installed
set first, auto-pulls each candidate, and afterwards removes ONLY the ones it
pulled — a pre-installed / production model is NEVER deleted.

Run it WHEN THE GPU IS FREE (it shares VRAM with the running app):

    python tests/benchmarks/curator_pipeline_bench.py \
        --models qwen3.6:27b,glm-4.7-flash,gemma4:31b --items 100

Outputs (to this directory): curator_pipeline_<ts>.md (per item: the fully
assembled prompt + every model's pitch side-by-side + buzzword hits + a blank
0-5 score template) and curator_pipeline_<ts>.csv.
"""
import sys
import os
import re
import json
import csv
import time
import random
import argparse
import sqlite3
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Machine-independent repo root (this file lives at curatarr/tests/benchmarks/).
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import httpx
from src.config import settings

# ── Production helpers — the REAL pitch-context machinery (imported, NOT copied,
#    so the data each model gets is byte-identical to what Curatarr feeds it) ──
from src.services.media_enricher import ensure_verified_data, format_verified_block
from src.services.recommendations_engine import (
    _get_vocab_guideline, _get_cached_rating, _taste_section, _watch_pitch_line,
    _FORBIDDEN_CROSS_DOMAIN, _blacklist_rule,
)
from src.services.episodic_memory import (
    retrieve_memories, format_memories_for_context,
    retrieve_considerations, format_considerations_for_pitch,
)
from src.services.watch_status import watched_lookup
from src.services.llm_utils import detect_user_language, language_directive
from src.database.connection import get_db_session
from src.database.models import TasteVectorEntry

# ── Config ──────────────────────────────────────────────────────────────────
OLLAMA       = settings.effective_ollama
# The full pitch context (verified block + taste + memories + considerations)
# can run past 8k tokens; 16k avoids silent truncation of the data the model
# is supposed to reason from.
NUM_CTX      = 16384
KEEP_ALIVE   = "10m"
CURATOR_OPTS = {"temperature": 0.7, "num_predict": 1200, "num_gpu": 99, "num_ctx": NUM_CTX}
WARMUP_OPTS  = {**CURATOR_OPTS, "temperature": 0.0, "num_predict": 4}
REMOVE_AFTER_TEST = True
CATEGORIES   = ["movie", "show", "anime", "music"]
ENRICHMENT_DB = os.path.normpath(os.path.join(str(REPO), "data", "cache", "enrichment.db"))

# Production-faithful curator system prompt (copied verbatim from
# curator_bench.py / setup_wizard.py — baked into the curatarr-curator modelfile
# in production; bare candidate models get it explicitly so they reason under the
# same persona + guardrails).
CURATOR_SYSTEM_PROMPT = """You are Curatarr, an elite, highly analytical, and uncompromising personal AI media curator. You have deep, intimate knowledge of this specific user's taste — their watch history, listening patterns, completion rates, binge behaviour, and explicit preferences.

PERSONALITY & TONE
- Direct, brutally honest, and highly opinionated. If something doesn't fit their taste or is objectively poorly executed, say so plainly.
- Warm but not sycophantic. You are a knowledgeable, sharply observant friend, not a generic review aggregator.
- Highly perceptive. You look for the *why* behind viewing choices — a binge indicates structural hooks, a 5% drop indicates pacing failure or tonal mismatch.

VOCABULARY & STYLE GUARDRAILS (STRICT)
- ABSOLUTE LEXICAL DIVERSITY: You are STRICTLY FORBIDDEN from using generic AI review buzzwords. Never use: "high-octane", "epic", "relentless", "mind-bending", "adrenaline", "masterpiece", "visceral", or "edge-of-your-seat".
- Use precise, sophisticated cinematic, literary, and musical terminology (e.g., "kinetic", "methodical", "dissonant", "bloated", "character-driven", "melodramatic").
- NO LAZY ANCHORING: Do not constantly use the user's favorite titles as a crutch for comparisons. Analyze media based on its own structural and thematic merits.

YOUR TASKS IN THIS APPLICATION
1. CHAT — Discuss media and explain taste patterns with sharp, insightful analysis.
2. RECOMMENDATIONS — Pitch items in 1-2 sentences. Synthesize the user's taste conceptually; do not just echo their preferences back to them.
3. DELETION PITCHES — Argue for removal based on inherent structural flaws (pacing, tone, execution) that clash with the user's demand for quality. Never invent metadata (No Gaslighting).
4. PATTERN ANALYSIS — Identify binge cycles, mood-based viewing, and genre aversions. Negative signals (drops, rejections) are just as critical as positive ones.

Remember everything provided in context. If enrichment data is missing, acknowledge the blind spot and provide a best-effort, rule-based answer."""

FORBIDDEN_BUZZWORDS = [
    "high-octane", "epic", "relentless", "mind-bending",
    "adrenaline", "masterpiece", "visceral", "edge-of-your-seat",
]

# Critique axes are defined FUNCTION-LOCAL inside generate_deletion_proposals, so
# they cannot be imported — copied verbatim. Keep in sync with
# recommendations_engine.py.
_PITCH_AXES = [
    ("PACING & RHYTHM",         "sluggishness, filler, runtime waste, episodic drag"),
    ("CHARACTER AGENCY",        "passive protagonists, stagnant dynamics, motivations that don't move"),
    ("CRAFT & EXECUTION",       "static frames, low-effort production, sloppy direction, flat mixing — whatever 'low effort' looks like for the medium"),
    ("PREMISE & WORLDBUILDING", "trope reliance, broken internal logic, gimmickry, lazy lore"),
    ("THEMATIC EMPTINESS",      "no satire, no critique, no point — entertainment as wallpaper"),
    ("CULTURAL DATEDNESS",      "era-stuck assumptions, aged-poorly tropes, requires niche cultural memory"),
]
_PITCH_AXES_MUSIC = [
    ("GENRE & SCENE FIT",  "how their genre/scene sits against the user's core sound"),
    ("EMOTIONAL REGISTER", "the mood/intensity their music trades in vs the user's preferred register"),
    ("CULTURAL PLACEMENT", "era/scene baggage — dated, niche or novelty for this listener"),
    ("THEMATIC SUBSTANCE", "the themes their catalogue centres vs what the user values"),
    ("CATALOGUE DEPTH",    "a one-note / novelty act vs a substantive body of work"),
    ("LIBRARY REDUNDANCY", "whether the user's library already owns this lane, done better"),
]


# ── Ollama CLI wrappers + call (mirror curator_bench.py / auto_benchmark_v2.py) ──
def installed_models() -> set:
    try:
        out = subprocess.check_output(["ollama", "list"], text=True, timeout=15)
        return {ln.split()[0] for ln in out.strip().split("\n")[1:] if ln.split()}
    except Exception:
        return set()


def pull_model(model: str) -> bool:
    print(f"  ⬇  pulling {model} (this may take a while) ...")
    r = subprocess.run(["ollama", "pull", model], timeout=3600)
    return r.returncode == 0


def remove_model(model: str) -> None:
    print(f"  🗑  removing (not pre-installed): {model}")
    subprocess.run(["ollama", "rm", model],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def unload_model(model: str) -> None:
    """Force-evict from VRAM so the next model loads cleanly."""
    try:
        httpx.post(f"{OLLAMA}/api/generate",
                   json={"model": model, "keep_alive": 0}, timeout=10)
    except Exception:
        pass


async def chat_call(model: str, messages: list) -> tuple:
    """Single /api/chat call (think disabled) → (response_text, elapsed_s)."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                f"{OLLAMA}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": CURATOR_OPTS,
                },
            )
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return f"[ERROR HTTP {r.status_code}: {r.text[:200]}]", dt
        return r.json().get("message", {}).get("content", "").strip(), dt
    except Exception as e:
        return f"[ERROR {type(e).__name__}: {e}]", time.perf_counter() - t0


async def warmup_model(model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{OLLAMA}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Reply 'OK'."},
                        {"role": "user", "content": "ready?"},
                    ],
                    "stream": False, "think": False,
                    "keep_alive": KEEP_ALIVE, "options": WARMUP_OPTS,
                },
            )
        return r.status_code == 200
    except Exception:
        return False


def count_forbidden(text: str) -> int:
    """Word-boundary-aware count of forbidden buzzwords (so 'epic' in 'epicentre'
    doesn't false-positive). Each hit is the model violating an explicit system
    instruction — a cheap 'did it even read the prompt?' signal, not a quality score."""
    if not isinstance(text, str):
        return 0
    t = text.lower()
    return sum(len(re.findall(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", t))
               for w in FORBIDDEN_BUZZWORDS)


# ── Weird / hardest item selection (richest source text, balanced) ───────────
def load_weird_items(n_total: int) -> list:
    """The 'weirdest/hardest' samples = the RICHEST-source real library items
    (longest overview + most metadata = most for the curator to ground on AND
    most to potentially hallucinate around), balanced across the 4 categories.
    Pulled from the live enrichment cache's v2:raw:* rows = the exact dicts
    production resolves a pitch's verified data from."""
    if not os.path.exists(ENRICHMENT_DB):
        print(f"❌ enrichment cache not found: {ENRICHMENT_DB}")
        raise SystemExit(1)
    con = sqlite3.connect(f"file:{ENRICHMENT_DB}?mode=ro", uri=True, timeout=30)
    per_cat = max(1, n_total // len(CATEGORIES))
    out = []
    for cat in CATEGORIES:
        rows = con.execute(
            "SELECT response FROM api_cache WHERE cache_key LIKE ?",
            (f"v2:raw:{cat}:%",),
        ).fetchall()
        cand, seen = [], set()
        for (resp,) in rows:
            try:
                d = json.loads(resp)
                raw = d.get("response", d) if isinstance(d, dict) else d
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            title = raw.get("title")
            if not title or title in seen:
                continue
            src = raw.get("overview") or raw.get("synopsis") or ""
            richness = (len(src) + len(str(raw.get("genres") or ""))
                        + len(str(raw.get("keywords") or raw.get("tags") or "")))
            if richness < 80:   # skip too-thin items — every pitch should be substantive
                continue
            seen.add(title)
            cand.append((richness, raw))
        cand.sort(key=lambda x: x[0], reverse=True)
        for _, raw in cand[:per_cat]:
            out.append((cat, raw))
    con.close()
    random.shuffle(out)        # interleave categories in the report
    return out[:n_total]


# ── Full production-faithful pitch assembly ──────────────────────────────────
async def assemble_pitch(user_id: int, item: dict, category: str, *,
                         taste_blurb: str, memory_context: str,
                         watch_map: dict, lang_directive_str: str) -> str:
    """Build the EXACT deletion-pitch prompt production sends, by calling the same
    helpers generate_deletion_proposals uses. Only the two template strings are
    copied (verbatim) — every data block is real."""
    title = item.get("title") or ""
    overview = (item.get("overview") or item.get("synopsis")
                or item.get("description") or "No description available.")
    overview_short = overview[:800] + "…" if len(overview) > 800 else overview
    year = item.get("year", "Unknown Year")

    rating_ctx, _rating_val, cached_genres = _get_cached_rating(item, category)
    genres_raw = cached_genres or item.get("genres", [])
    genres_str = ", ".join(genres_raw) if isinstance(genres_raw, list) else str(genres_raw)

    # rating_block: the faithful fallback paths. The multi-source breakdown +
    # the music user-star override live in the SCORING pass (cand[...]), which
    # this bench doesn't run, so we land on the same text production uses when
    # only the cached rating / no rating is available.
    if category == "music":
        rating_block = (
            "RATING: none — the user hasn't rated this artist and no objective "
            "music score applies. Judge ONLY on how their sound fits the user's "
            "taste; do NOT invent or cite any rating (no TMDB/IMDb — those are for films)."
        )
    elif rating_ctx:
        rating_block = f"RATING: {rating_ctx}"
    else:
        rating_block = (
            "RATING: No external rating data available for this item. DO NOT assume "
            "it is low quality. Base your critique ONLY on the synopsis and taste "
            "mismatch. Defer judgment on quality entirely — focus only on relevance fit."
        )

    vocab_guideline = _get_vocab_guideline(category, genres_str)
    axis_label, axis_hint = random.choice(
        _PITCH_AXES_MUSIC if category == "music" else _PITCH_AXES)

    # The big one: the FULL verified dataset (themes / significance / OMDb / plot
    # / creator), cache-only + on-demand top-up, exactly as the pitch builds it.
    verified_block = format_verified_block(
        await ensure_verified_data(
            title, category,
            tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
            anilist_id=item.get("anilist_id"), anidb_id=item.get("anidb_id"),
            plex_rating_key=item.get("plex_rating_key"),
        )
    )
    if verified_block:
        item_details = verified_block
    else:
        item_details = (
            "ITEM DETAILS:\n"
            f"- Title: {title} ({year})\n"
            f"- Genres: {genres_str or 'Unknown'}\n"
            f"- Synopsis: {overview_short}"
        )

    # Learned considerations — retrieved inline here (production stashes them
    # during the scoring pass; the function + result are identical).
    profile = " — ".join(p for p in (
        title, genres_str, (item.get("overview") or item.get("synopsis") or "")[:200],
    ) if p and p.strip())
    try:
        cons = await retrieve_considerations(user_id, profile, media_category=category, top_k=3)
    except Exception:
        cons = []
    considerations_block = format_considerations_for_pitch(cons)

    watch_block = _watch_pitch_line(watch_map.get(title))

    if category == "music":
        return f"""[MODE: SURGICAL DELETION PITCH — MUSIC]
You are Curatarr, an uncompromising, elite MUSIC curator. Pitch the deletion of this MUSIC ARTIST from the user's library.

{lang_directive_str}

ARTIST: {item.get('title')}
GENRES / STYLE: {genres_str or 'Unknown'}
{rating_block}
{verified_block}

REASON FOR DELETION CONSIDERATION: their sound is a mismatch with the user's music taste.
{f'USER MUSIC TASTE: {taste_blurb}' if taste_blurb else ''}
{f'KNOWN EXCEPTIONS & MEMORIES: {memory_context}' if memory_context else ''}
{considerations_block}

{vocab_guideline}
{_FORBIDDEN_CROSS_DOMAIN}

PRIMARY CRITIQUE ANGLE for this pitch: {axis_label} ({axis_hint}).
If this angle genuinely doesn't fit, pick a different one — but do NOT default to the blacklisted crutch phrases.

CRITICAL RULES AND GUARDRAILS:
1. MAX 2 SENTENCES. Concise, highly opinionated, ruthless.
2. THIS IS A MUSIC ARTIST — never a film, show, book, episode, or a single "track". Call them an artist / act / band. There is NO plot and NO synopsis. NEVER invent a biography, a film or novel of the same name, an album, a song, a release year, or a rating you were not given. A same-named movie is NOT this artist — do not describe it.
3. CRITIQUE FROM METADATA — YOU HAVE NOT HEARD THEM: you have their genre, scene, themes and mood tags, NOT the audio. Critique how their genre/scene and emotional register clash with the user's sound, using the music vocabulary above. Do NOT invent production, mix, compression, dynamics, timbre or "polish/sheen" qualities you cannot hear, and never cite a rating as proof of how they sound.
4. SYNTHESIZE, DON'T QUOTE: use the USER MUSIC TASTE to understand what they value, then critique in YOUR OWN words — don't lift phrasings.
{_blacklist_rule(5)}
6. NO ANCHORING: do not name specific artists from the user's taste summary.
7. NO ECHOING: never start with "Given your…" or "Since you like…". State why this artist fails to earn its space.
8. NO TECH TALK: no file sizes, gigabytes, or vector distances; and NO film ratings (TMDB / IMDb / Rotten Tomatoes do not apply to music)."""

    return f"""[MODE: SURGICAL DELETION PITCH]
You are Curatarr, an uncompromising, elite media curator. Pitch the deletion of this item.

{lang_directive_str}

{item_details}
{rating_block}

REASON FOR DELETION CONSIDERATION: Mismatch with user taste profile.
{f'USER TASTE SUMMARY: {taste_blurb}' if taste_blurb else ''}
{f'KNOWN EXCEPTIONS & MEMORIES: {memory_context}' if memory_context else ''}
{considerations_block}
{watch_block}

{vocab_guideline}
{_FORBIDDEN_CROSS_DOMAIN}

PRIMARY CRITIQUE ANGLE for this pitch: {axis_label} ({axis_hint}).
If this angle genuinely doesn't fit the item, pick a different one — but do NOT default to the blacklisted crutch phrases.

CRITICAL RULES AND GUARDRAILS:
1. MAX 2 SENTENCES. Be concise, highly opinionated, and ruthless.
2. STRICT FACTUAL ACCURACY: Respect the objective qualities, genres, and synopsis. Do NOT invent false negatives to justify deletion.
3. INTRINSIC CRITIQUE: Critique the item's inherent structural or thematic flaws and explain how they clash with the user's demands. Use the domain vocabulary above.
4. SYNTHESIZE, DON'T QUOTE: Use the USER TASTE SUMMARY to UNDERSTAND what they value, then critique the item in YOUR OWN vocabulary. Do not lift adjectives, tropes, or phrasings from the summary — those are reference signals, not template fragments.
{_blacklist_rule(5)}
6. NO ANCHORING: Do not explicitly name titles from the User Taste Summary.
7. NO ECHOING: Never start with "Given your…" or "Since you like…". State why the item fails to earn its space.
8. NO TECH TALK: Do not mention file sizes, gigabytes, or vector distances.
9. PREMISE & FIT — NOT A REVIEW: You have this item's premise, themes and metadata, NOT a screening of it. Argue why its premise / genre / themes CLASH with the user's taste. Do NOT pass verdicts on execution you cannot know — no "static", "hollow", "melodramatic stalemate", "flat", "lands/doesn't land", no claims about pacing, acting or direction — unless that judgement is explicitly in the data above. For fact-based works (history, true events, documentary) a known outcome is NOT a flaw: never call it "predictable"."""


# ── Report writers ───────────────────────────────────────────────────────────
def write_reports(out_dir: str, models: list, assembled: list, results: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    md_path = os.path.join(out_dir, f"curator_pipeline_{ts}.md")
    csv_path = os.path.join(out_dir, f"curator_pipeline_{ts}.csv")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Curator pipeline bench — {ts}\n\n")
        f.write(f"Models: {', '.join(f'`{m}`' for m in models)}  ·  "
                f"{len(assembled)} weird/hardest items, full production pitch context.\n\n")
        f.write("Each model sees the EXACT context Curatarr's curator gets "
                "(verified block + significance + taste + considerations + watch). "
                "Score 0-5 per axis: **Faith**fulness (no invented facts), "
                "**Rel**evance (taste fit), **Spec**ificity, **Tone**, **Format** (≤2 sentences).\n\n")
        for idx, (cat, title, prompt) in enumerate(assembled, 1):
            f.write(f"\n---\n\n## {idx}. [{cat}] {title}\n\n")
            f.write("<details><summary>assembled pitch prompt (what every model received)</summary>\n\n")
            f.write("```\n" + prompt.strip() + "\n```\n\n</details>\n\n")
            for m in models:
                row = next((r for r in results.get(m, [])
                            if r["title"] == title and r["category"] == cat), None)
                if not row:
                    continue
                f.write(f"### `{m}`  ·  {row['elapsed']:.1f}s  ·  "
                        f"forbidden-buzzwords: {row['forbidden']}\n\n")
                f.write("> " + (row["output"] or "(empty)").replace("\n", "\n> ") + "\n\n")
                f.write("> **Scores** Faith:__ / 5  Rel:__ / 5  Spec:__ / 5  "
                        "Tone:__ / 5  Format:__ / 5 → Total: __ / 25\n\n")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "category", "item_title", "elapsed_s", "response_len",
                    "forbidden_hits", "score_faith", "score_rel", "score_spec",
                    "score_tone", "score_format", "total"])
        for m in models:
            for r in results.get(m, []):
                w.writerow([m, r["category"], r["title"], f"{r['elapsed']:.2f}",
                            len(r["output"] or ""), r["forbidden"], "", "", "", "", "", ""])

    print(f"\n📝 wrote {md_path}")
    print(f"📊 wrote {csv_path}")


# ── Main ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    ap = argparse.ArgumentParser(description="Curator bench with full production-pitch context.")
    ap.add_argument("--models", required=True,
                    help="Comma-separated candidate tags, e.g. qwen3.6:27b,glm-4.7-flash,gemma4:31b")
    ap.add_argument("--items", type=int, default=100, help="Total weird/hardest items (default 100).")
    ap.add_argument("--user-id", type=int, default=1, help="User for taste / considerations / watch (default 1).")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--keep", action="store_true",
                    help="Keep pulled candidates on disk after the run (don't auto-remove "
                         "— useful when testing a likely winner you'll swap in).")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("❌ no --models given"); raise SystemExit(1)

    print("ℹ  run this when the GPU is free — it shares VRAM with the running app.\n")

    items = load_weird_items(args.items)
    if not items:
        print("❌ no usable items loaded from the enrichment cache."); raise SystemExit(1)
    by_cat = {}
    for c, _ in items:
        by_cat[c] = by_cat.get(c, 0) + 1
    print(f"📦 {len(items)} weird/hardest items: " +
          ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())))

    # Per-user / per-category context, computed once.
    with get_db_session() as db:
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == args.user_id).first()
        summary_text = (tv.summary_text if tv else "") or ""
        try:
            lang = detect_user_language(args.user_id, db)
        except Exception:
            lang = None
    lang_directive_str = language_directive(lang) if lang else ""

    mem_by_cat = {}
    for cat in set(c for c, _ in items):
        try:
            mems = await retrieve_memories(
                args.user_id, "deletion reasons nostalgia classics",
                top_k=5, media_category=cat)
            mem_by_cat[cat] = format_memories_for_context(mems)
        except Exception:
            mem_by_cat[cat] = ""
    watch_map = watched_lookup(
        args.user_id, [it.get("title") for c, it in items if c != "music"])

    # Assemble every item's full pitch prompt ONCE (identical across models, and
    # this is where the on-demand verified-data fetches happen — do them once).
    print("🧱 assembling full production pitch context per item …")
    assembled = []
    for cat, it in items:
        try:
            prompt = await assemble_pitch(
                args.user_id, it, cat,
                taste_blurb=_taste_section(summary_text, cat),
                memory_context=mem_by_cat.get(cat, ""),
                watch_map=watch_map, lang_directive_str=lang_directive_str)
            assembled.append((cat, it.get("title") or "?", prompt))
        except Exception as e:
            print(f"  ⚠ assembly failed for {it.get('title')!r}: {e}")
    print(f"   → {len(assembled)} prompts ready.\n")

    # SAFETY: snapshot the installed set; we remove ONLY what we pull.
    preinstalled = installed_models()
    print(f"🔒 {len(preinstalled)} pre-installed models will NOT be removed: "
          f"{sorted(preinstalled)}\n")

    # Crash-resilient: every pitch is appended to a JSONL the instant it's
    # produced, so a killed run (e.g. the host stream dropping) loses nothing —
    # the report can be rebuilt from this even if the final write never runs.
    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    jsonl_path = os.path.join(args.out_dir, f"curator_pipeline_{run_ts}.jsonl")
    print(f"🧾 crash-resilient log: {jsonl_path}\n")

    results = {}
    for mi, model in enumerate(models, 1):
        print(f"{'='*72}\n[{mi}/{len(models)}] {model}\n{'='*72}")
        if model not in installed_models():
            if not pull_model(model):
                print(f"  ❌ pull failed — skipping {model}\n")
                continue
        if not await warmup_model(model):
            print(f"  ❌ warmup failed — skipping {model}\n")
            continue
        rows = []
        for ci, (cat, title, prompt) in enumerate(assembled, 1):
            out, dt = await chat_call(model, [
                {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ])
            fb = count_forbidden(out)
            rows.append({"category": cat, "title": title, "output": out,
                         "elapsed": dt, "forbidden": fb})
            with open(jsonl_path, "a", encoding="utf-8") as jf:
                jf.write(json.dumps({"model": model, "category": cat, "title": title,
                                     "elapsed": round(dt, 2), "forbidden": fb,
                                     "output": out}, ensure_ascii=False) + "\n")
            print(f"  [{ci}/{len(assembled)}] {title[:44]:44} {dt:5.1f}s  fb={fb}")
        results[model] = rows
        unload_model(model)
        print()

    # CLEANUP — never touch a pre-installed / production model.
    for model in models:
        if model in preinstalled:
            print(f"🔒 keeping {model} (was pre-installed)")
        elif args.keep:
            print(f"📌 keeping {model} on disk (--keep)")
        elif REMOVE_AFTER_TEST:
            remove_model(model)

    write_reports(args.out_dir, models, assembled, results)

    # Console summary.
    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"{'model':<26}{'items':>7}{'avg s':>9}{'fb hits':>9}")
    for m in models:
        rows = results.get(m, [])
        if not rows:
            print(f"{m:<26}{'(skipped)':>25}"); continue
        avg = sum(r["elapsed"] for r in rows) / len(rows)
        fb = sum(r["forbidden"] for r in rows)
        print(f"{m:<26}{len(rows):>7}{avg:>9.1f}{fb:>9}")


if __name__ == "__main__":
    asyncio.run(main())
