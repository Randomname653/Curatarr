"""
ARR Suite LLM - Media Enricher

Full pipeline per media item:
  1. Fetch rich metadata from TMDB (plot, cast, keywords, similar titles, ratings)
  2. Fetch AniList data for anime (themes, demographics, tags)
  3. Small fast LLM (configurable, default qwen2.5:3b) summarizes everything
     into a structured JSON profile
  4. nomic-embed-text generates the embedding
  5. Everything stored in ChromaDB + SQLite cache

The small LLM does ALL the synthesis work so the curator LLM never has to
guess or hallucinate facts about a title.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional, Any

import httpx

from src.config import settings
from src.cache.metadata_cache import MetadataCache, write_fields
from src.services.llm_utils import clean_llm_text, strip_think_tags, ollama_options

logger = logging.getLogger(__name__)


# Pass 99-fu2: prompt version tag for the two-tier cache split.
#
# BUMP THIS WHENEVER the curator or summariser system prompts change in a
# way that should invalidate previously-polished profiles. The next
# enrichment run after the bump will:
#   - skip any ``enriched:*`` cache entry whose ``prompt_version`` doesn't
#     match (treats it as "needs re-polish")
#   - reuse the matching ``raw:*`` cache entry (no API re-fetch) and feed
#     it straight to the consumer's LLM polish
#
# A bump costs ~LLM-summarizer-throughput × library-size — i.e. for a
# 30k-item library at ~5 s/item, roughly 40 hours of LLM compute, but
# ZERO new API calls. The raw cache absorbs the API cost across bumps.
#
# Format: opaque short string. Bump format is "v{n}" for clarity but
# anything that changes the literal counts as a bump.
#
# CAVEAT: a bump only re-polishes items the producer actually SEES. The
# enrichment.py Step-5 pre-filter skips items already marked
# EnrichmentStatus.enriched=True + error IS NULL — so to re-polish the
# ALREADY-LLM-done items after a bump, run enrichment with force=True
# (force bypasses that pre-filter; the raw cache still spares the API calls).
#
# Pass 99-fu9: bumped v1 -> v2 when switching the summariser base model
# gpt-oss:20b -> granite4.1:8b, to re-polish the whole library uniformly.
# Phase-2 quality pass: bumped v2 -> v3 after adding the GROUNDING
# DISCIPLINE block to SUMMARIZE_PROMPT (4 rules: source-trace, exact
# numbers, tags-are-themes-not-character-bios, tone-hints-authoritative).
# A/B test over 22 control+random items: 1 actual fix (Danny Phantom),
# 0 regressions, 0 parse errors. Items re-polish naturally as the cache
# read sees the version mismatch + falls through to fresh polish.
# Bumped v3 -> v4 when the archetype-tag blacklist landed (Idea A from
# the external review — the LLM was attention-bridging tags like
# "tsundere" to nearby character nouns; filtering them deterministically
# at prompt-input is more reliable than the soft v3 rule asking the LLM
# to handle them correctly).
# Bumped v4 -> v5 when Idea C landed (title-number self-correction loop
# — post-generation regex check that every digit-run from the title
# appears verbatim in the polished output; one retry with override
# directive on miss). Beats the LLM's "smoother prose" prior for
# numbers ("4400" -> "4,000 individuals") deterministically.
# Bumped v5 -> v6 when BASE_SUMMARIZER_MODEL switched granite4.1:8b ->
# granite4.1:3b (Tournament finale 2026-05-24 — see tournament_finale_*.md;
# 3b scored 8/8 curated vs 8b's 6/8, ≈1.7× faster, frees ~4GB VRAM).
# Forces a uniform re-polish of every cached profile under the new model
# so old 8b outputs do not coexist with new 3b outputs in the library.
# Bumped v6 -> v7 with the cast_top3 surgical fix (Round 2-style A/B
# bench 2026-05-24 tournament_round2_2026-05-24_16-39.md): v5 had a
# 10.1% cast-hallucination rate (16/159) — Seiyuu-Wahnsinn + truncated
# "(played by)" glitch + directors-as-cast — that auto-eval never caught
# because we had no assertion comparing returned cast against source
# CAST field. The new line forces "ACTOR-only, verbatim from CAST field,
# empty array if none" and dropped the rate to 0.0% (0/107) at the cost
# of ~33% fewer cast entries overall (the model now correctly emits
# empty arrays when source is sparse). Pass-rate dipped 96.2 → 93.6
# but the regression was entirely HTTP-500 / JSON-parse errors on
# random items, a bench-only concurrency artifact (production runs at
# concurrency=1).
# Bumped v7 -> v8 with the NO-FILL POLICY (rule #5) + model-swap back
# to granite4.1:8b. Three benches (R1/R2/R3, 32-100 items each, 3
# variants v5.1/v5.2/v5.3 × 3 models granite-3b/qwen-4b/granite-8b)
# confirmed granite4.1:8b + the v5.2 "no-fill" rule as the most stable
# Quality/Speed combination: 98% pass-rate median, 14s p50, 0% cast
# hallucinations, lowest cross-run std-dev (2.1pp). The v5.2 NO-FILL
# rule targets "Sarah Connor in Orphan Black" / "Aloha System in SAO"
# / "Ezren/Lyra in Dragon Prince" - all character/lore name fabrications
# the soft grounding rules 1-4 could not catch. See tournament_round2
# _2026-05-24_21-20.md for the full A/B/C bench data.
# Bumped v8 -> v9 with GROUNDING rule #6 (DESCRIBE, DON'T EDITORIALIZE /
# anti-over-labeling) + descriptive-not-interpretive themes/keywords hints.
# Targets the summariser projecting loaded fandom labels ("siscon dynamics")
# and critical-theory frames ("heteronormative expectations", "subverts",
# "deconstructs") onto premises that are, on their face, simpler — a class of
# error rules 1-5 don't catch because nothing is factually fabricated, only
# the *reading* is imposed. Additive guidance only (no model/format change),
# same precedent as the v2->v3 GROUNDING bump. Existing v8 profiles re-polish
# lazily / under force=True; raw cache spares all API calls.
_PROMPT_VERSION = "v9"


# ── PHASE-2 QUALITY: CHARACTER-ARCHETYPE TAG BLACKLIST ──────────────────────
# AniList/Jikan attach character-archetype labels to a work's keywords as
# WORK-LEVEL themes ("this show contains a tsundere character"). The
# summariser model (granite4.1:8b) treats them as attention-bridge
# candidates and routinely misattributes the archetype to a nearby named
# character in its output — Mamako (overprotective mom, exact opposite of
# tsundere) gets labelled "tsundere mother Mamako" because "tsundere" is
# in the keywords list. The v3 GROUNDING rule #3 ("tags are work-level
# themes, not character bios") nudged the model but didn't reliably stick.
#
# Deterministic fix: strip these archetype labels from the keywords list
# before injecting it into the prompt. What the model doesn't see, it
# can't misattribute. The original keywords are still cached on the raw
# blob + polished profile for downstream taste-vector use; only the
# prompt-input list is filtered.
_ARCHETYPE_TAG_BLACKLIST: set[str] = {
    # The "-dere" personality-type family (Japanese anime character tropes)
    "tsundere", "kuudere", "yandere", "deredere", "dandere",
    "himedere", "kamidere", "kogudere", "shundere", "darudere",
    # Opposite-archetype labels that misattribute identically
    "haraguro",   # outwardly-sweet, inwardly-cruel
}


def _filter_archetype_tags(keywords) -> list[str]:
    """Drop character-archetype tags from a keywords/tags list before it
    goes into the LLM prompt. Returns a new list — does NOT mutate the
    input. Non-string entries pass through unchanged."""
    out: list = []
    for k in keywords or []:
        if isinstance(k, str) and k.strip().lower() in _ARCHETYPE_TAG_BLACKLIST:
            continue
        out.append(k)
    return out


# ── PHASE-2 QUALITY: TITLE-NUMBER SELF-CORRECTION LOOP (Idea C) ──────────────
# Digit-runs that appear in the source TITLE are facts the LLM must
# reproduce verbatim. The v3 grounding rule #2 ("numbers in source are
# exact, reproduce verbatim") was a soft nudge that the model overrides
# with narrative-fluency priors — e.g. "The 4400" gets summarised as
# "4,000 individuals" because that reads smoother in prose. Asking
# harder in the prompt doesn't help (test_prompt_v3 confirmed).
#
# Deterministic fix: after the first LLM polish, regex-check that every
# digit-run from the title appears verbatim in the plot_summary OR
# embedding_text. If any is missing, retry the LLM call ONCE with an
# explicit override directive listing the required numbers. Cost: zero
# for ~98% of items (no digits in title), one extra LLM call (~3-5 s)
# for items where the first attempt actually dropped the number.

_TITLE_NUMBER_RX = re.compile(r"\d+")


def _extract_title_numbers(title: str) -> list[str]:
    """Digit-runs in the title that must be preserved verbatim in the
    polished output. ``"The 4400"`` → ``["4400"]``; ``"2001: A Space
    Odyssey"`` → ``["2001"]``; ``"Frieren"`` → ``[]``."""
    return _TITLE_NUMBER_RX.findall(title or "")


def _numbers_preserved_in_profile(numbers: list[str], profile: dict) -> bool:
    """True if every number in ``numbers`` appears as a standalone token
    in the profile's plot / embedding / why_watch text. \\b boundaries
    enforce verbatim match — "4400" must appear as "4400", not as
    "4,400" (with comma), "4000" (paraphrased), or substring of
    "44000". This is exactly the strictness that catches the LLM's
    smoother-prose paraphrasing of source numbers."""
    if not numbers:
        return True
    text = " ".join([
        (profile.get("plot_summary")    or "") if isinstance(profile.get("plot_summary"),    str) else "",
        (profile.get("embedding_text") or "") if isinstance(profile.get("embedding_text"), str) else "",
        (profile.get("why_watch")       or "") if isinstance(profile.get("why_watch"),       str) else "",
    ])
    for n in numbers:
        if not re.search(rf"\b{re.escape(n)}\b", text):
            return False
    return True

# Pass 99-fu2: tier-2 raw cache TTL. Long enough that prompt bumps don't
# trigger API re-fetches; short enough that genuinely-changed upstream
# metadata (a TMDB rewrite, a Last.fm artist merge) still gets refreshed.
_RAW_CACHE_DAYS = 90


def _write_raw_cache(cat: str, id_key, raw_dict: dict, days: int = _RAW_CACHE_DAYS) -> None:
    """Persist a fresh API fetch under raw:{cat}:{id_key} for reuse.

    Best-effort: any cache failure is logged at debug and swallowed so a
    flaky cache write can't break the producer's hand-off to the consumer.
    Strips underscore-prefixed transport fields before writing — those
    are caller-instance specific (plex_rating_key, _cache_key, etc.)
    and get re-injected on read in ``fetch_and_prepare_raw``.
    """
    if not id_key:
        return
    try:
        c = MetadataCache()
        try:
            cleaned = {k: v for k, v in raw_dict.items() if not str(k).startswith("_")}
            c.set_cache(f"raw:{cat}:{id_key}", cleaned, days=days)
        finally:
            c.close()
    except Exception as e:
        logger.debug("[enricher] raw-cache write failed for raw:%s:%s — %s",
                     cat, id_key, e)


# ── Verified-data assembly (NO LLM) ───────────────────────────────────────────
#
# The curator's pitches / discussions / re-evaluations were data-starved: the
# delete pitch got only a thin ARR overview, and the Level-2 re-eval got NOTHING
# from cache (it was told to "use your training memory"). Meanwhile the pipeline
# already fetched a rich dataset. ``build_verified_data`` assembles it from the
# two caches we already keep — the polished ``enriched:*`` profile (themes,
# keywords, mood, plot_summary, director, cast) merged with the ``raw:*`` API
# cache (extended plot, writer/creator, awards, country) — with NO LLM call.
# ``format_verified_block`` renders it for a prompt. Feed this wherever the
# curator REASONS about a title (delete pitch, discussion, re-eval, recs) so a
# capable model reasons from facts instead of a synopsis stub or its own memory.

def _vd_first(cache, keys: list):
    """First non-empty cached profile dict among ``keys`` (cache-only)."""
    for k in keys:
        try:
            hit = cache.get_cache(k)
        except Exception:
            hit = None
        if hit:
            resp = hit.get("response")
            if isinstance(resp, dict) and resp:
                return resp
    return None


def build_verified_data(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None,
    cache_id=None, cache=None,
) -> Optional[dict]:
    """Assemble the full VERIFIED dataset for a title from cache — NO LLM, no
    live fetch. Merges the polished ``enriched:*`` profile (themes, keywords,
    mood, plot_summary, director, cast) with the ``raw:*`` API cache (extended
    plot, writer, awards/extra_context, country). Returns None when nothing is
    cached. ``cache`` may be injected so a batch caller opens one handle."""
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        # plex_rating_key here is the arr doc-id ("sonarr:3176" / "radarr:107")
        # for library items — and the enrichment pipeline keys EVERY library
        # item's profile under exactly that ("enriched:anime:sonarr:3176"). It's
        # the most reliable id we have (an anime may be cached under its
        # anilist_id but NOT its tvdb/tmdb), so include it as a lookup key, not
        # just for the raw_prefetch. Ordered after the hard external ids but
        # before the weak title[:40] fallback.
        id_keys = [v for v in (cache_id, anilist_id, anidb_id, tmdb_id, tvdb_id, plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        enriched = _vd_first(cache, [f"enriched:{media_type}:{k}" for k in id_keys]) or {}
        # The enriched profile embeds the resolved external ids it was built from.
        # The OMDb-only fields (writer / awards / extended plot) and the Wikipedia
        # significance live on the ID-keyed raw entry (e.g. raw:anime:239214) — a
        # doc-id-only lookup (raw:anime:sonarr:2908) misses them, so a deletion-
        # pitch DISCUSSION got a thin profile with no significance/writer even
        # though the data existed. Fold the embedded ids into the raw lookup.
        raw_id_keys = list(id_keys)
        for _f in ("anilist_id", "tmdb_id", "tvdb_id", "anidb_id", "_anilist_id", "_tmdb_id"):
            _v = enriched.get(_f)
            if _v and _v not in raw_id_keys:
                raw_id_keys.append(_v)
        raw_keys = []
        if plex_rating_key:
            raw_keys.append(f"raw_prefetch:{plex_rating_key}")
        raw_keys += [f"raw:{media_type}:{k}" for k in raw_id_keys]
        # Field-level MERGE, not first-hit: the data is fragmented across keys, so
        # take each field from the highest-priority entry that actually has it
        # (doc-id / prefetch win for overview etc.; the id-keyed raw fills in
        # significance + OMDb).
        raw = {}
        for _k in raw_keys:
            _hit = cache.get_cache(_k)
            _resp = _hit.get("response") if _hit else None
            if isinstance(_resp, dict):
                for _fk, _fv in _resp.items():
                    if _fv not in (None, "", [], 0) and raw.get(_fk) in (None, "", [], 0):
                        raw[_fk] = _fv
        if not enriched and not raw:
            return None

        def pick(*vals):
            for v in vals:
                if v not in (None, "", [], 0):
                    return v
            return None

        def _longer_text(*vals):
            best = ""
            for v in vals:
                if isinstance(v, str) and len(v.strip()) > len(best):
                    best = v.strip()
            return best or None

        return {
            "media_type":     media_type,
            "title":          pick(enriched.get("title"), raw.get("title"), title),
            "year":           pick(enriched.get("year"), raw.get("year")),
            "genres":         pick(enriched.get("genres"), raw.get("genres")),
            # Prefer whichever API plot is FULLER — OMDb's "full" plot is
            # sometimes a one-liner that would displace a rich TMDB overview
            # if trusted by source alone — then the LLM plot_summary.
            "plot":           pick(_longer_text(raw.get("overview_extended"), raw.get("overview")), enriched.get("plot_summary")),
            # Alias so the dict is also consumable by chat._build_hidden_context
            # (which reads plot_summary/overview) when cached as a thread anchor.
            "plot_summary":   pick(_longer_text(raw.get("overview_extended"), raw.get("overview")), enriched.get("plot_summary")),
            "themes":         enriched.get("themes"),
            "keywords":       pick(enriched.get("keywords"), raw.get("keywords")),
            "mood":           enriched.get("mood"),
            "director":       pick(enriched.get("director"), raw.get("director")),
            "writer":         raw.get("writer"),
            "source_author":  raw.get("source_author"),
            "source_kind":    raw.get("source_kind"),
            "cast":           pick(enriched.get("cast_top3"), raw.get("cast")),
            "rating":         pick(enriched.get("rating"), raw.get("rating")),
            "country":        raw.get("country"),
            "seasons":        raw.get("seasons"),
            "episodes_total": raw.get("episodes_total"),
            "runtime_min":    raw.get("runtime_min"),
            "studios":        raw.get("studios"),
            "studio_note":    _studio_note_for(cache, raw.get("studios")),
            "director_note":  _director_note_for(cache, pick(enriched.get("director"),
                                                             raw.get("director"))),
            "extra_context":  raw.get("extra_context"),
            "source":         pick(enriched.get("source"), raw.get("source")),
            # imdb_id drives the dynamic OMDb top-up (ensure_verified_data);
            # omdb_checked stops it (and the bulk backfill) re-querying.
            "imdb_id":        pick(raw.get("imdb_id"), enriched.get("imdb_id")),
            "omdb_checked":   bool(raw.get("omdb_checked")),
            "ratings_checked": bool(raw.get("ratings_checked")),
            # OMDb full harvest: critic scores + box office (Ratings array)
            "rt_score":       raw.get("rt_score"),
            "metacritic":     raw.get("metacritic"),
            "box_office":     raw.get("box_office"),
            # Wikipedia-sourced cultural/historical significance (archive pillar);
            # significance_checked stops the just-in-time top-up re-querying.
            "significance":         raw.get("significance"),
            "wikidata":             raw.get("wikidata"),
            "significance_checked": bool(raw.get("significance_checked")),
            # Community reception (AniList/MAL/TMDB reviews, condensed);
            # reception_checked stops the just-in-time top-up re-querying.
            "reception":         raw.get("reception"),
            "reception_checked": bool(raw.get("reception_checked")),
            # multi-season awareness: how the LAST season landed (reviews
            # attach to season entries — the finale is invisible from S1)
            "finale_reception":  raw.get("finale_reception"),
            # Typed franchise graph (AniList) + AniDB community tags (weekly
            # offline snapshot); relations_checked gates the light catch-up.
            "relations":         raw.get("relations"),
            "relations_checked": bool(raw.get("relations_checked")),
            "anidb_tags":        raw.get("anidb_tags"),
            "staff":             raw.get("staff"),
            # Music-artist fields (musicbrainz+lastfm) — they sat on the raw
            # doc for 15k artists while the evidence block never showed them.
            "discogs_styles":  _discogs_styles_for(raw),
            "bio":             raw.get("bio"),
            "listeners":       raw.get("listeners"),
            "artist_type":     raw.get("type") if raw.get("media_type") == "music" else None,
            "similar_artists": raw.get("similar_artists"),
        }
    finally:
        if owns:
            cache.close()


def _discogs_styles_for(raw: dict) -> Optional[list]:
    """Aggregated Discogs styles for a music artist (local snapshot read)."""
    if (raw or {}).get("media_type") != "music":
        return None
    try:
        from src.services.discogs_offline import artist_styles
        return artist_styles(raw.get("name") or raw.get("title")) or None
    except Exception:
        return None


def _director_note_for(cache, director) -> Optional[str]:
    """Cached director-reputation note (sync read; fetch is JIT-only)."""
    if not director or not isinstance(director, str):
        return None
    try:
        from src.services.studio_notes import get_director_note_cached
        return get_director_note_cached(director.split(",")[0].strip(), cache)
    except Exception:
        return None


def _studio_note_for(cache, studios) -> Optional[str]:
    """First cached studio-reputation note for this title's studios (sync
    read only — fetching happens in ensure_verified_data / the walker)."""
    if not studios:
        return None
    try:
        from src.services.studio_notes import get_studio_note_cached
        for s in (studios if isinstance(studios, list) else [studios])[:2]:
            note = get_studio_note_cached(s, cache)
            if note:
                return note
    except Exception:
        pass
    return None


def format_verified_block(data: Optional[dict], *, header: str = None) -> str:
    """Render the verified dataset as a curator-facing prompt block. Empty fields
    are omitted; the header forbids invention beyond what's listed. "" if no data."""
    if not data:
        return ""
    lines = [header or (
        "[VERIFIED DATA — reason ONLY from the facts below; do NOT add plot "
        "points, people, awards, year, or franchise context not listed here]"
    )]

    def add(label, val, cap=None):
        if val in (None, "", [], 0):
            return
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val if v)
        val = str(val)
        if cap and len(val) > cap:
            val = val[:cap].rsplit(" ", 1)[0] + "…"
        lines.append(f"  {label}: {val}")

    _type_label = {
        "show": "TV series", "anime": "Anime (TV series)",
        "movie": "Film", "music": "Music artist",
    }.get(data.get("media_type"))
    add("Type", _type_label)
    add("Title", data.get("title"))
    add("Year", data.get("year"))
    add("Genres", data.get("genres"))
    add("Creator/Writer", data.get("writer"))
    # Named separately from the screenwriter on purpose: "adapted from a novel
    # by X" is a different, and usually stronger, pedigree claim than who wrote
    # the script. Judging an adaptation on the adapter alone reads a le Carré
    # as an anonymous genre piece.
    if data.get("source_author"):
        add(f"Adapted from ({(data.get('source_kind') or 'source').lower()}) by",
            data["source_author"])
    add("Director", data.get("director"))
    add("Director note", data.get("director_note"), cap=350)
    cast = data.get("cast")
    add("Cast", cast[:4] if isinstance(cast, list) else cast)
    add("Staff", data.get("staff"))
    add("Themes", data.get("themes"))
    add("Keywords", data.get("keywords"))
    add("Mood", data.get("mood"))
    add("Country", data.get("country"))
    # Format facts sat in the dict but never rendered — the curator argued
    # about commitment/pacing without knowing it was e.g. a 12x24min single
    # season (the Lostorage WIXOSS "what do you know" dump exposed this).
    if data.get("episodes_total"):
        fmt = f"{data['episodes_total']} episodes x {data['runtime_min']} min" \
            if data.get("runtime_min") else f"{data['episodes_total']} episodes"
        if data.get("seasons"):
            fmt = f"{data['seasons']} season(s), {fmt}"
        add("Format", fmt)
    add("Studio", data.get("studios"))
    add("Studio note", data.get("studio_note"), cap=350)
    add("Notable", data.get("extra_context"))
    # rating can be a CONTENT rating string for anime ("PG-13 - Teens 13 or
    # older") — that rendered as "PG-13 - Teens 13 or older/10". Only format
    # numeric scores as N/10.
    try:
        add("Rating", f"{float(data.get('rating')):g}/10")
    except (TypeError, ValueError):
        add("Content rating", data.get("rating"))
    critics = []
    if data.get("rt_score"):
        critics.append(f"Rotten Tomatoes {data['rt_score']}")
    if data.get("metacritic") not in (None, "", "N/A"):
        critics.append(f"Metacritic {data['metacritic']}/100")
    add("Critics", ", ".join(critics) if critics else None)
    add("Box office", data.get("box_office"))
    # music-artist lines (all None for video docs, so they simply don't print)
    add("Artist type", data.get("artist_type"))
    add("Styles (Discogs)", data.get("discogs_styles"))
    if data.get("listeners"):
        try:
            add("Community", f"{int(data['listeners']):,} Last.fm listeners")
        except (TypeError, ValueError):
            add("Community", data.get("listeners"))
    sim = data.get("similar_artists")
    add("Similar artists", sim[:8] if isinstance(sim, list) else sim)
    add("Bio", data.get("bio"), cap=650)
    add("Significance", data.get("significance"), cap=600)
    # Wikidata facts sit NEXT to the distilled prose, never inside it. They are
    # statements from a graph, so they need no summarising and carry no risk of
    # a model rewriting them; keeping them separate also makes it obvious to a
    # reader which line was distilled and which was simply looked up.
    if data.get("wikidata"):
        from src.services.wikidata import format_wikidata_line
        add("On record", format_wikidata_line(data["wikidata"]), cap=400)
    add("Community reception", data.get("reception"), cap=900)
    add("Finale reception", data.get("finale_reception"), cap=700)
    rels = data.get("relations")
    if rels and isinstance(rels, list):
        add("Franchise", "; ".join(
            f"{r.get('type')}: {r.get('title')}" + (f" ({r['year']})" if r.get("year") else "")
            for r in rels if isinstance(r, dict) and r.get("title")))
    add("AniDB tags", data.get("anidb_tags"), cap=400)
    add("Plot", data.get("plot"), cap=700)
    return "\n".join(lines)


def _wiki_hit_matches(query_title: str, hit_title: str, media_type: str) -> bool:
    """True when a Wikipedia search hit is plausibly the SAME work as the queried
    title. Guards against same-name collisions — Wikipedia ranks the most POPULAR
    same-name entity first, so a blind ``hits[0]`` resolved "Momoiro Sisters"
    (obscure anime) to the J-pop act "Momoiro Clover Z", and "White Album anime"
    to the video game. Requires the hit's base title (sans the "(…)"
    disambiguator) to equal the queried title, and rejects a cross-medium
    disambiguator ("(album)" for a film, "(film)" for music, …)."""
    import re as _re
    def _norm(s: str) -> str:
        s = _re.sub(r"\s*\([^)]*\)\s*$", "", s or "")          # drop trailing (disambiguator)
        # keep word boundaries: the reorder check below compares word
        # multisets, which needs words to survive normalisation
        return _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    nq, nh = _norm(query_title), _norm(hit_title)
    if not nq:
        return False
    if nq != nh:
        # Same words, different order, still the same work: the English
        # localisation of Japanese titles swaps surname/given-name order —
        # the library says "Thus Spoke Kishibe Rohan", Wikipedia files
        # "Thus Spoke Rohan Kishibe", and an order-sensitive equality kept
        # a reachable article invisible to significance AND to the deletion
        # discussion's deep read. Word-multiset equality accepts the
        # reorder while different words still fail.
        if sorted(nq.split()) != sorted(nh.split()):
            return False
    m = _re.search(r"\(([^)]*)\)\s*$", hit_title or "")
    disambig = (m.group(1).lower() if m else "")
    cross = {
        "movie": ("album", "song", "single", "band", "musician", "singer", "video game"),
        "anime": ("album", "song", "single", "band", "musician", "singer", "video game"),
        "show":  ("album", "song", "single", "band", "musician", "singer", "video game", "film"),
        "music": ("film", "television series", "tv series", "video game", "anime", "manga", "novel"),
    }.get(media_type, ())
    return not any(w in disambig for w in cross)


_SIG_VOCAB = re.compile(
    r"award|prize|nominat|record|guinness|influen|acclaim|best.sell|classic"
    r"|milestone|legacy|landmark|box.office|bomb|flop|canon|adapted into"
    r"|first (?:anime|film|series|game)|genre.defin"
    # documented depiction of serious historical/systemic subject matter —
    # the 1923 case: the residential-school arc (institutional genocide,
    # church-run colonization) never reached the significance line because
    # the vocabulary only knew awards and milestones
    r"|genocide|coloniz|residential school|boarding school|slaver|atrocit"
    r"|massacre|internment|holocaust|censor|banned|controvers", re.I)


def _significance_slice(extract: str, lead_chars: int = 2500,
                        budget: int = 7000) -> str:
    """Lead + the paragraphs that actually carry significance vocabulary,
    within ``budget`` — instead of the first N chars of a long article."""
    lead = extract[:lead_chars]
    out = [lead]
    used = len(lead)
    for para in extract[lead_chars:].split("\n"):
        p = para.strip()
        if not p or not _SIG_VOCAB.search(p):
            continue
        take = p[:1200]
        if used + len(take) > budget:
            break
        out.append(take)
        used += len(take)
    return "\n".join(out)


# TMDB crew jobs that name the SOURCE of an adaptation, most specific first.
# Order is the priority: "Novel" beats a generic "Story" credit when a title
# carries both.
_SOURCE_JOBS = ("Novel", "Book", "Graphic Novel", "Comic Book", "Short Story",
                "Theatre Play", "Musical", "Original Story", "Story",
                "Characters")

_CAST_LINE = re.compile(r"\b[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)+\s+as\s+[A-Z]")


def _looks_like_cast_list(text: str) -> bool:
    """True when the text is a Wikipedia cast section rather than significance.

    "Name Name as Character" repeated is a character list, whatever the
    distiller called it. Three or more is not a passing mention: an article
    that genuinely discusses a performance names one or two roles in prose.
    """
    return len(_CAST_LINE.findall(text or "")) >= 3


# The distillation contract, kept as a module constant so its VERSION can be
# derived from it. A significance value is only as good as the prompt that
# produced it, and values were stamped "checked" forever: a title distilled
# under an earlier, weaker version of these rules kept that answer for good.
# Hashing the text means any future edit here retires the old answers by
# itself, with no constant to remember to bump.
_SIGNIFICANCE_PROMPT = """[MODE: SIGNIFICANCE EXTRACTION]
State the documented historical / cultural significance of "{title}" using ONLY the encyclopedia text below.

Significance means the text EXPLICITLY documents at least one of: awards or nominations; being a genuine first / landmark / genre-defining work; documented influence on later works; a major commercial milestone (best-selling, record-breaking, a very long run, many adaptations); canonical / "classic" status; or the work depicting / examining a serious historical atrocity or systemic injustice (genocide, colonization, residential/boarding-school systems, slavery, internment) — only when the text explicitly describes that as part of the work, and name WHAT it depicts.

These are NOT significance — if the text has only these, there is none: cast or crew names; filming location, production company or funding body; premiere date or platform; a creator's debut; being called "high-profile"; or the plot.

STRICT RULES:
- Output ONLY the significant facts themselves, as plain prose. NEVER explain your reasoning, mention "the rules" / "qualifying" / "documented milestone", say what does or does not count, or narrate your own filtering (NO "but these are not listed as…", "thus the only qualifying significance is…", "per the rules…"). Just state the facts, or NONE.
- Use ONLY facts in the text. Do NOT add evaluative words like "pioneering", "landmark", "acclaimed", "influential", "seminal" unless the text itself uses that word about THIS work.
- Do NOT editorialise or extrapolate (no "part of a surge", "signifies investment", "marks a shift", etc.).
- 1-3 plain sentences, prose only, no lists or headings.
- If the text documents no real significance (only production facts, cast, or plot), output exactly: NONE

TEXT:
{extract}"""

# Which article the distiller was handed matters as much as what it was told to
# do with it: a perfect prompt over the wrong page yields a confident "NONE".
# So the stamp covers the retrieval rules as well, and tightening them retires
# the answers they produced — the same self-retiring trick, one layer deeper.
_SIG_RETRIEVAL_VERSION = "5"   # 5: word-order-tolerant hit match (JP name reorder)
                               # 3: the library's title searched alongside the
                               #    enriched one
                               # 4: the article resolved from the IMDb id via
                               #    its Wikidata sitelink, before any name

_SIG_PROMPT_VERSION = hashlib.sha1(
    (_SIGNIFICANCE_PROMPT + _SIG_RETRIEVAL_VERSION).encode("utf-8")
).hexdigest()[:8]

# Wikipedia states what a subject IS in its opening sentence. Scanning far past
# it was the whole bug: the article on the Birmingham street gang mentions the
# television series it inspired somewhere in its first 1,500 characters, so a
# search for the 2013 show accepted a page about Victorian criminals, and the
# distiller — correctly — reported no significance.
_SIG_LEAD_CHARS = 300

# The guard only knew "may refer to". Wikipedia's disambiguation pages also open
# "usually refers to" (Fargo) and "most commonly refers to" (Alien), both of
# which sailed through and were distilled as though they were the work.
_DISAMBIG = re.compile(
    r"\b(?:may|can|usually|commonly|most commonly|also)\s+refers?\s+to\b", re.I)


_WIKI_API = "https://en.wikipedia.org/w/api.php"


async def _wiki_get(client, params: dict, *, tries: int = 3):
    """One Wikipedia API call that respects being told to slow down.

    Every other rate-limited service in this file has a throttle — TMDB raises
    a transient error carrying Retry-After, AniList holds a shared backoff
    timestamp. Wikipedia had none, which was survivable while one walker asked
    one question at a time and stops being so the moment several backfills run
    at once. A 429 answered without waiting is not a miss, and must never be
    read as one.

    Returns the last response, so callers keep their own status handling; a
    network-level failure returns None, which they already treat as transient.
    """
    delay = 1.0
    for attempt in range(tries):
        try:
            r = await client.get(_WIKI_API, params=params)
        except Exception:
            if attempt == tries - 1:
                return None
            await asyncio.sleep(delay)
            delay *= 2
            continue
        if r.status_code not in (429, 503) or attempt == tries - 1:
            return r
        try:
            wait = float(r.headers.get("Retry-After", "") or delay)
        except ValueError:
            wait = delay
        await asyncio.sleep(min(wait, 30.0))
        delay *= 2
    return None


async def fetch_significance(
    title: str, media_type: str = "movie", year: Optional[int] = None,
    also_known_as: tuple = (), imdb_id: Optional[str] = None,
) -> Optional[str]:
    """Fetch a title's CULTURAL / HISTORICAL significance from Wikipedia and
    distil it by SUMMARISING the fetched text — never from model memory.

    This is the "archive pillar" knowledge the curator lacks: it reasons only
    from thin synopsis metadata, so it dismissed Cat's Eye (a phantom-thief
    landmark) as "mainstream nostalgia", and when asked directly it confidently
    INVENTED a wrong creator. Grounding the significance in a real, fetched
    source — exactly like the plot grounding — gives it the facts without the
    hallucination.

    TRI-STATE return (the Panic-Room catch: a Fincher film sat permanently
    significance-less because a transient failure was stamped as checked):
      str  — documented significance,
      ""   — DEFINITIVE nothing (no matching article, or the distiller read
             the article and said NONE) → caller may stamp checked,
      None — TRANSIENT failure (Wikipedia/summarizer error) → caller must
             NOT stamp; the walker retries next pass.
    """
    hint = {"anime": "anime", "movie": "film", "show": "television series",
            "music": "band musician"}.get(media_type, "")
    # The name a title is enriched under is not the name Wikipedia files it
    # under. Anime especially: the cache row for "Frieren: Beyond Journey's
    # End" (the library's title, which IS the article's name) carries the
    # romanised "Sousou no Frieren" inside — and the exact-match guard below
    # rightly refuses to bridge two different names, so the search could never
    # succeed. Every known name of THIS work gets a turn; names of OTHER works
    # (recommendations, franchise siblings) must never be passed here.
    def _nkey(n):
        return "".join(c for c in n.lower() if c.isalnum())
    names = [title]
    for aka in also_known_as:
        if aka and aka.strip() and _nkey(aka) not in {_nkey(n) for n in names}:
            names.append(aka.strip())
    # medium plausibility for the DIRECT-title lookup below: the exact page
    # exists but could be a same-named stranger, so its opening must read
    # like the right kind of entity before we trust it.
    plaus = {
        "music": re.compile(r"musician|band|singer|composer|record (?:producer|label)"
                            r"|DJ|discograph", re.I),
        "movie": re.compile(r"\bfilm\b|\bmovie\b", re.I),
        "show": re.compile(r"television|tv series|streaming series", re.I),
        "anime": re.compile(r"anime|manga|television", re.I),
    }.get(media_type)
    try:
        # Wikipedia's API rejects generic / browser-spoofing User-Agents with a
        # 403 — it requires a descriptive UA that includes a contact/URL.
        async with httpx.AsyncClient(timeout=20, headers={
            "User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                          "personal media curator) python-httpx"
        }) as client:
            extract = ""
            # The strongest identity first: the IMDb id names the Wikidata
            # entity and the entity names its article. No guessing, so none of
            # the name guards below apply — a sitelinked article cannot be a
            # same-named stranger. This is what finds "The Fall Guy (2024
            # film)" and "Stick (TV series)", which no name lookup can guess.
            # Measured: 72 of 120 titles the name path had stamped empty had
            # their article reachable this way.
            if imdb_id:
                from src.services.wikidata import resolve_enwiki_article
                article = await resolve_enwiki_article(imdb_id)
                if article:
                    exa = await _wiki_get(client, {
                        "action": "query", "prop": "extracts", "explaintext": 1,
                        "redirects": 1, "titles": article, "format": "json",
                    })
                    if exa is None or exa.status_code != 200:
                        return None    # transient — the article exists, we know it
                    for _pid, pdata in (exa.json().get("query", {})
                                        .get("pages", {})).items():
                        extract = pdata.get("extract") or ""
                        break
                # article == "" (no entity / no enwiki page) falls through to
                # the name path — a page can exist without a sitelink; None
                # (transient) falls through too rather than failing the title.
            # DIRECT title lookup next: coded names ("C418") drown in keyword
            # search — the hint pushed the actual article out of the top 5
            # while the exact page sat there all along. Each known name of the
            # work gets a turn before falling back to search.
            for name in names:
                if extract:
                    break
                ex0 = await _wiki_get(client, {
                    "action": "query", "prop": "extracts", "explaintext": 1,
                    "redirects": 1, "titles": name, "format": "json",
                })
                if ex0 is None or ex0.status_code != 200:
                    continue
                for _pid, pdata in (ex0.json().get("query", {}).get("pages", {})).items():
                    cand = pdata.get("extract") or ""
                    if (len(cand) >= 300
                            and not _DISAMBIG.search(cand[:200])
                            and (plaus is None
                                 or plaus.search(cand[:_SIG_LEAD_CHARS]))):
                        extract = cand
                    break
                if extract:
                    break
            if not extract:
                searched = False
                for name in names:
                    sr = await _wiki_get(client, {
                        "action": "query", "list": "search",
                        "srsearch": f"{name} {hint}".strip(),
                        "format": "json", "srlimit": 5,
                    })
                    if sr is None or sr.status_code != 200:
                        continue
                    searched = True
                    hits = sr.json().get("query", {}).get("search", [])
                    # Pick the first hit whose article title actually MATCHES
                    # one of this work's names — NOT a blind hits[0], which
                    # resolves same-name collisions to whatever entity
                    # Wikipedia ranks most popular.
                    page = next((h["title"] for h in hits
                                 if any(_wiki_hit_matches(n, h["title"], media_type)
                                        for n in names)), None)
                    if not page:
                        continue
                    ex = await _wiki_get(client, {
                        "action": "query", "prop": "extracts", "explaintext": 1,
                        "redirects": 1, "titles": page, "format": "json",
                    })
                    if ex is None or ex.status_code != 200:
                        # The tri-state was built because a transient failure
                        # once stamped Panic Room significance-less for good.
                        # The search leg above learned that; this leg had not —
                        # a 429 here fell through to the "no substance" return
                        # below and became a permanent verdict.
                        return None
                    for _pid, pdata in (ex.json().get("query", {}).get("pages", {})).items():
                        extract = pdata.get("extract") or ""
                        break
                    if extract:
                        break
                if not searched:
                    return None    # transient — no search attempt succeeded
                if not extract:
                    logger.debug("[significance] no Wikipedia hit matched %r (%s)",
                                 names, media_type)
                    return ""      # definitive — search worked, nothing matches
            if not extract or len(extract) < 120:
                return ""      # definitive — page exists but has no substance
            # The blind [:7000] cut dropped the exact paragraphs significance
            # lives in: Cutthroat Island's Guinness "biggest box-office bomb"
            # sits in Reception/Legacy PAST the 7k mark of a 16k article, so
            # the distiller only ever saw production trivia and said NONE.
            # Keep the lead, then cherry-pick paragraphs carrying
            # significance vocabulary, within the same total budget.
            extract = _significance_slice(extract)
    except Exception as e:
        logger.debug("[significance] Wikipedia fetch failed for %r: %s", title, e)
        return None

    prompt = _SIGNIFICANCE_PROMPT.format(title=title, extract=extract)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                "model": SUMMARIZER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                **ollama_options(temperature=0.1, num_predict=400),
            })
        if r.status_code != 200:
            return None
        out = clean_llm_text(strip_think_tags(
            r.json().get("message", {}).get("content", "") or ""
        )).strip()
        # The 8B summariser sometimes ignores "prose only" and appends a markdown
        # "Key points" / bullet block or a leading bold header — keep just the
        # leading prose paragraph(s).
        import re as _re
        # The 8B summariser ignores "prose only" and emits headers, a meta
        # preamble, numbered/bulleted lists, or a trailing echoed "NONE". Clean
        # all of those down to the leading prose.
        out = _re.sub(r"^\s*\*\*[^\n]*\*\*\s*\n+", "", out)                       # leading **header**
        out = _re.sub(r"^\s*(?:the text|this (?:text|article|entry)|here(?:'s| is))"
                      r"[^\n:]{0,90}:\s*\n*", "", out, flags=_re.I)               # meta preamble
        out = _re.split(r"\n\s*(?:\*\*|Key\s[Pp]oints|Key\s[Ee]vidence|Conclusion\b)",
                        out, maxsplit=1)[0]                                       # drop trailing block
        out = _re.sub(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s*", " ", out)              # flatten list items (incl. leading)
        out = _re.sub(r"\s*\bNONE\b.*$", "", out, flags=_re.I | _re.S)           # trailing echoed NONE
        out = _re.sub(r"\s{2,}", " ", out.replace("**", "")).strip()
    except Exception as e:
        logger.debug("[significance] distillation failed for %r: %s", title, e)
        return None
    if not out or out.upper().startswith("NONE") or len(out) < 20:
        return ""              # definitive — the distiller read it and said NONE
    if _looks_like_cast_list(out):
        # The prompt already forbids returning cast or crew names, and the
        # distiller still handed back a verbatim Wikipedia "Cast" section
        # ("X as Character, Y as Character, …"). A prose rule cannot enforce
        # this; a shape check can. Recorded as "checked, nothing found" so the
        # walker does not burn the article again — the archive pillar is
        # better off knowing it has nothing than reading a character list as
        # cultural standing.
        logger.info("[significance] discarded a cast list for %r", title)
        return ""
    return out


async def fetch_wikipedia_summary(
    title: str, media_type: str = "movie", *, max_chars: int = 6000,
) -> Optional[str]:
    """Fetch the readable text of a title's Wikipedia article (lead + the first
    sections — plot, reception, themes — up to ``max_chars``) as RAW grounding,
    no LLM distillation. Uses the same entity-match collision guard as
    ``fetch_significance`` (so "Momoiro Sisters" never returns the J-pop act).

    This is for the deletion DISCUSSION, where the curator reasons far more
    precisely from the real article than from a thin synopsis, and the cost (one
    title, interactive) is affordable — the batch scan keeps the cheap distilled
    significance instead. Returns None when no matching article is found."""
    hint = {"anime": "anime", "movie": "film", "show": "television series",
            "music": "band musician"}.get(media_type, "")
    query = f"{title} {hint}".strip()
    try:
        async with httpx.AsyncClient(timeout=20, headers={
            "User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                          "personal media curator) python-httpx"
        }) as client:
            sr = await client.get("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": query,
                "format": "json", "srlimit": 5,
            })
            hits = sr.json().get("query", {}).get("search", []) if sr.status_code == 200 else []
            page = next((h["title"] for h in hits
                         if _wiki_hit_matches(title, h["title"], media_type)), None)
            if not page:
                return None
            ex = await client.get("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "prop": "extracts",
                "explaintext": 1, "redirects": 1, "titles": page, "format": "json",
            })
            pages = ex.json().get("query", {}).get("pages", {}) if ex.status_code == 200 else {}
            for _pid, pdata in pages.items():
                extract = (pdata.get("extract") or "").strip()
                if len(extract) >= 120:
                    return extract[:max_chars]
            return None
    except Exception as e:
        logger.debug("[wiki-summary] fetch failed for %r: %s", title, e)
        return None


async def topup_omdb(
    title: str,
    media_type: str,
    *,
    imdb_id: str,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    cache_id=None, cache=None,
) -> bool:
    """Fetch OMDb for a title that's cached but missing the OMDb-only rich fields
    (writer, awards/extra_context, extended plot) and merge them into its raw:*
    cache entry. NO LLM. Returns True if anything was added.

    Why this exists: ``build_verified_data`` is cache-only, and the rich OMDb
    fields only exist for the ~1/3 of items that got OMDb at enrichment time
    (OMDb needs an imdb_id and was long rate-limited). This is the shared
    primitive behind the dynamic just-in-time top-up (``ensure_verified_data``)
    AND the admin bulk-backfill — both just call it with the supporter key's
    100k/day headroom."""
    if not imdb_id:
        return False
    omdb = await fetch_omdb_data(imdb_id)
    if omdb is None:
        return False               # transient — do not stamp, the walker retries

    plot   = omdb.get("plot_full") or omdb.get("overview")
    awards = omdb.get("awards")
    writer = omdb.get("writer")
    country = omdb.get("country")
    # Full-harvest rule: the Ratings array (RT/Metacritic) and box office were
    # extracted by fetch_omdb_data and then thrown away here for months.
    _ratings   = omdb.get("ratings") or {}
    rt_score   = _ratings.get("rt")
    metacritic = _ratings.get("metacritic")
    box_office = omdb.get("box_office")
    _bad = ("", "N/A", None)

    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        id_keys = [v for v in (cache_id, anilist_id, anidb_id, tmdb_id, tvdb_id) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        added = False
        for k in id_keys:
            key = f"raw:{media_type}:{k}"
            hit = cache.get_cache(key)
            if not hit:
                continue
            raw = hit.get("response")
            if not isinstance(raw, dict):
                continue
            fields = {}
            if plot not in _bad and not (raw.get("overview_extended") or "").strip():
                fields["overview_extended"] = plot; added = True
            if awards not in _bad and "Awards:" not in (raw.get("extra_context") or ""):
                ec = (raw.get("extra_context") or "").strip()
                fields["extra_context"] = (ec + " | " if ec else "") + f"Awards: {awards}"; added = True
            if writer not in _bad and not (raw.get("writer") or "").strip():
                fields["writer"] = writer; added = True
            if country not in _bad and not (raw.get("country") or "").strip():
                fields["country"] = country
            if rt_score not in _bad and not raw.get("rt_score"):
                fields["rt_score"] = rt_score; added = True
            if metacritic not in _bad and not raw.get("metacritic"):
                fields["metacritic"] = metacritic; added = True
            if box_office not in _bad and not raw.get("box_office"):
                fields["box_office"] = box_office
            # Idempotency marker: OMDb was consulted for this title. Set even
            # when OMDb had no writer/awards — many titles legitimately don't —
            # so neither the bulk backfill nor the dynamic top-up ever re-query
            # it. Without this, awardless films stayed "candidates" forever and
            # every backfill re-fetched the exact same set.
            # omdb == {} carries no data on purpose: the id has no OMDb
            # record, and the marker below is the whole point of the write.
            if not raw.get("omdb_checked"):
                fields["omdb_checked"] = True
            # Ratings-era marker: docs OMDb-checked BEFORE the full-harvest fix
            # lack it, so the backfill touches them exactly once more.
            if not raw.get("ratings_checked"):
                fields["ratings_checked"] = True
            if fields:
                write_fields(cache, key, raw, fields, days=_RAW_CACHE_DAYS)
        return added
    finally:
        if owns:
            cache.close()


async def topup_significance(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None, year: Optional[int] = None,
    cache_id=None, cache=None,
) -> bool:
    """Fetch the Wikipedia-sourced cultural/historical significance for a title
    (see ``fetch_significance``) and store it on its raw:* cache entry, idempotent
    via a ``significance_checked`` marker. NO model memory. Returns True if a
    significance string was actually added.

    Mirrors ``topup_omdb``: the just-in-time companion to the cache-only
    ``build_verified_data`` so the curator gets the archive-pillar facts right
    before it reasons about a title — then cached, so at most one Wikipedia +
    summariser round per item, ever."""
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        id_keys = [v for v in (cache_id, anilist_id, anidb_id, tmdb_id, tvdb_id, plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        targets = []
        for k in id_keys:
            hit = cache.get_cache(f"raw:{media_type}:{k}")
            if not hit or not isinstance(hit.get("response"), dict):
                continue
            raw = hit["response"]
            if (raw.get("significance_checked")
                    and raw.get("significance_v") == _SIG_PROMPT_VERSION):
                return False  # already done, under the rules in force today
            targets.append((f"raw:{media_type}:{k}", raw))
        if not targets:
            return False  # nothing cached to attach to (rare after the R6 enrich)
        # The cache id is the LIBRARY's name for the work — for anime usually
        # the English one Wikipedia files the article under, while ``title`` is
        # the enriched (often romanised) one. A numeric id is an id, not a name.
        aka = ()
        if cache_id and not str(cache_id).isdigit():
            aka = (str(cache_id),)
        # The raw entries themselves know the IMDb id (96% of the library
        # since the *arr harvest) — the strongest article resolver there is.
        imdb = next((raw.get("imdb_id") for _k, raw in targets
                     if raw.get("imdb_id")), None)
        sig = await fetch_significance(title, media_type, year=year,
                                       also_known_as=aka, imdb_id=imdb)
        if sig is None:
            # TRANSIENT failure (Wikipedia/summarizer error) — do NOT stamp.
            # The old code stamped checked=True here, which is how Panic Room
            # and Wild Side sat permanently significance-less: one bad moment
            # became forever. The walker simply retries next pass.
            return False
        added = False
        for key, raw in targets:
            # "" = DEFINITIVE nothing — stamp so we never re-query a title
            # with no documented significance.
            fields = {
                "significance_checked": True,
                # Which set of rules produced this answer. An entry stamped with
                # an older version is not trusted: the distillation is only as
                # good as the prompt behind it, and "checked" used to mean
                # "never again".
                "significance_v": _SIG_PROMPT_VERSION,
            }
            drop = ()
            if sig:
                fields["significance"] = sig
                added = True
            else:
                # A re-check that now finds nothing must not leave the previous
                # version's text standing next to a fresh stamp.
                drop = ("significance",)
            # Only these fields. This walker holds the GPU for a long time per
            # title, so a whole-row write would be handing back a copy of the
            # entry read minutes ago — discarding whatever a faster walker
            # added in the meantime.
            write_fields(cache, key, raw, fields, drop=drop, days=_RAW_CACHE_DAYS)
        return added
    finally:
        if owns:
            cache.close()


async def ensure_verified_data(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None,
    cache=None,
    allow_summarizer: bool = True,
) -> Optional[dict]:
    """``build_verified_data`` + a dynamic, just-in-time OMDb top-up. Use this
    (async) wherever the curator is ABOUT to reason about a title — delete pitch,
    discussion, re-eval — so a cached-but-OMDb-starved item gets its writer /
    awards / extended plot filled in right before processing (then cached, so at
    most one OMDb call per item, ever).

    ``allow_summarizer=False`` skips the Wikipedia-significance top-up (the one
    step that needs the SUMMARIZER model). Callers that hold the curator GPU
    gate (the judge funnel, the legacy pitch loop) MUST pass False: on a
     24 GB card the summarizer load evicts the resident 20 GB curator and the
    next verdict pays a 60-120 s reload — GPU-idle churn that also stalls every
    chat queued on the gate. The wait_for timebox does NOT prevent it (the
    request is already queued inside Ollama when the app-side wait gives up).
    Raw API top-ups (fast-enrich, OMDb) carry no model and always run."""
    data = build_verified_data(
        title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
        anilist_id=anilist_id, anidb_id=anidb_id,
        plex_rating_key=plex_rating_key, cache=cache,
    )
    # R6 — nothing cached at all: the curator would otherwise cold-read from a
    # thin synopsis stub (the "live cache miss → PARTIAL" trap that produced the
    # King & Conqueror nonsense — invented execution verdicts, rating-as-proof).
    # Do a FAST enrich (raw API fetch, NO LLM polish, ~2-5 s) so it reasons from
    # the real plot / creator / rating instead. Best-effort + time-boxed so a
    # slow API can't hang the request path; on timeout/failure we just fall back
    # to whatever the caller had (the stub), i.e. no worse than before.
    if not data:
        try:
            fresh = await asyncio.wait_for(
                enrich_media_item(
                    title=title, media_type=media_type, tmdb_id=tmdb_id,
                    tvdb_id=tvdb_id, anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, skip_llm_summary=True,
                ),
                timeout=8.0,
            )
            if fresh:
                # Re-read through the resolved IDs: enrich_media_item writes the
                # raw cache under whatever id_key it resolved (often a tmdb_id we
                # weren't given), so a title-only re-read would miss it.
                data = build_verified_data(
                    title, media_type,
                    tmdb_id=fresh.get("tmdb_id") or tmdb_id,
                    tvdb_id=fresh.get("tvdb_id") or tvdb_id,
                    anilist_id=fresh.get("anilist_id") or anilist_id,
                    anidb_id=fresh.get("anidb_id") or anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] fast-enrich on cache-miss failed for %r: %s", title, e)

    # Archive-pillar grounding: fetch the title's cultural/historical significance
    # from Wikipedia (once, cached) so the curator can judge "is this a defining
    # title?" from REAL facts instead of dismissing landmarks it knows nothing
    # about (the Cat's Eye case). Time-boxed + best-effort + idempotent. Runs
    # regardless of OMDb state — the OMDb early-return below must not skip it.
    # The walker re-offers an entry whose stamp predates the current rules
    # (archive_backfill._has_significance); this path used to gate on the bare
    # "checked" flag, so a verdict reached here kept an answer produced under
    # retrieval rules that have since been found wrong. Same test, both paths.
    if (allow_summarizer and data and not data.get("significance")
            and data.get("significance_v") != _SIG_PROMPT_VERSION):
        try:
            if await asyncio.wait_for(topup_significance(
                title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                anilist_id=anilist_id, anidb_id=anidb_id,
                plex_rating_key=plex_rating_key, year=data.get("year"), cache=cache,
            ), timeout=14.0):
                data = build_verified_data(
                    title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                    anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] significance top-up failed for %r: %s", title, e)

    # Community-reception grounding (AniList/MAL/TMDB reviews) — the judge's
    # blind spot on obscure titles. Same contract as the significance top-up:
    # allow_summarizer-gated (gate holders never trigger it), time-boxed,
    # idempotent via reception_checked.
    if (allow_summarizer and data and not data.get("reception")
            and not data.get("reception_checked")
            and media_type in ("anime", "movie", "show")):
        try:
            from src.services.reception import topup_reception
            if await asyncio.wait_for(topup_reception(
                title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                anilist_id=anilist_id, anidb_id=anidb_id,
                plex_rating_key=plex_rating_key, year=data.get("year"), cache=cache,
            ), timeout=45.0):
                data = build_verified_data(
                    title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                    anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] reception top-up failed for %r: %s", title, e)

    # Studio reputation note (Wikipedia lead -> summarizer, once per STUDIO
    # ever) — the Princess Lover! debate flipped on exactly this knowledge
    # arriving from outside. Summarizer-gated like significance.
    if (allow_summarizer and data and data.get("studios")
            and not data.get("studio_note")):
        try:
            from src.services.studio_notes import ensure_studio_note
            studios = data["studios"]
            first = (studios if isinstance(studios, list) else [studios])[0]
            if await asyncio.wait_for(ensure_studio_note(first, cache=cache),
                                      timeout=30.0):
                data = build_verified_data(
                    title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                    anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] studio note failed for %r: %s", title, e)

    # Director reputation note — JIT-ONLY (4.3k distinct directors forbid a
    # bulk walker; debated titles collect the relevant names fast).
    if (allow_summarizer and data and data.get("director")
            and not data.get("director_note")):
        try:
            from src.services.studio_notes import ensure_director_note
            if await asyncio.wait_for(ensure_director_note(data["director"],
                                                           cache=cache),
                                      timeout=30.0):
                data = build_verified_data(
                    title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                    anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] director note failed for %r: %s", title, e)

    # Franchise/tags catch-up for docs reception-checked before relations
    # existed — one AniList call + a local snapshot read, no LLM involved,
    # so it is NOT allow_summarizer-gated. Idempotent via relations_checked.
    if (data and data.get("reception_checked") and not data.get("relations_checked")
            and media_type in ("anime", "movie", "show")):
        try:
            from src.services.reception import topup_franchise
            if await asyncio.wait_for(topup_franchise(
                title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                anilist_id=anilist_id, anidb_id=anidb_id,
                plex_rating_key=plex_rating_key, year=data.get("year"), cache=cache,
            ), timeout=15.0):
                data = build_verified_data(
                    title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                    anilist_id=anilist_id, anidb_id=anidb_id,
                    plex_rating_key=plex_rating_key, cache=cache,
                )
        except Exception as e:
            logger.debug("[verified] franchise top-up failed for %r: %s", title, e)

    # Fully OMDb-checked (incl. the ratings era), has the fields, or no
    # imdb_id → done. Docs checked before the full-harvest fix (writer/awards
    # present but no ratings_checked) get exactly one more fetch for RT/MC.
    if (not data or not data.get("imdb_id")
            or (data.get("omdb_checked") and data.get("ratings_checked"))
            or ((data.get("writer") or data.get("extra_context"))
                and data.get("ratings_checked"))):
        return data
    try:
        if await topup_omdb(
            title, media_type, imdb_id=data["imdb_id"],
            tmdb_id=tmdb_id, tvdb_id=tvdb_id, anilist_id=anilist_id,
            anidb_id=anidb_id, cache=cache,
        ):
            data = build_verified_data(
                title, media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                anilist_id=anilist_id, anidb_id=anidb_id,
                plex_rating_key=plex_rating_key, cache=cache,
            )
    except Exception as e:
        logger.debug("[verified] OMDb top-up failed for %r: %s", title, e)
    return data


def _domain_for_write(media_type, genres) -> str:
    """Normalize a profile's media_type into one of OUR four domains before
    it becomes a chroma quarantine key. TMDB's raw 'tv' leaked straight into
    domain= for years and minted the 995-doc legacy epoch that starved show
    taste calibration (external eval) — migrated once by
    scripts/migrate_tv_domain.py; this keeps new writes clean. The anime
    split uses the same genre heuristic as classify_sonarr_category."""
    mt = (media_type or "movie").lower()
    if mt != "tv":
        return mt
    g = genres if isinstance(genres, str) else ", ".join(genres or [])
    return "anime" if "anime" in g.lower() else "show"


async def run_significance_backfill(limit: int = 150, task=None) -> dict:
    """Custodian walker: fetch Wikipedia significance for LIVE raw:* entries
    that were never significance-checked. Until now this only happened
    just-in-time for titles the user happened to discuss — the archive pillar
    was blind for everything else. Summarizer-tier work: yields to any active
    curator and stops when a game grabs the GPU.

    ``task`` = the custodian's Activity card; per-title progress goes there.
    Returns {"checked": n, "added": n, "remaining": n} — remaining>0 means the
    custodian should keep the task due and continue next tick."""
    import json as _json
    from src.services.llm_priority import wait_for_curator

    def _gaming() -> bool:
        try:
            from src.services.app_state import get_state
            return get_state("game_active") == "1"
        except Exception:
            return False

    cache = MetadataCache()
    try:
        cur = cache.conn.cursor()
        cur.execute(
            """
            SELECT cache_key, response FROM api_cache
            WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
              AND expires_at > ?
              AND (
                    -- never looked at
                    (response NOT LIKE '%"significance_checked"%'
                     AND response NOT LIKE '%"significance"%')
                    -- or answered under an older set of distillation rules;
                    -- without this the version stamp would never fire, because
                    -- a checked entry was simply never offered again
                 OR response NOT LIKE ?
                  )
            """,
            (datetime.now().isoformat(), f"%{_SIG_PROMPT_VERSION}%"),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        cache.close()

    total = len(rows)
    checked = added = 0
    for row in rows[:limit]:
        if _gaming():
            logger.info("[significance-backfill] game active — stopping early "
                        "(%d/%d this pass)", checked, min(limit, total))
            break
        key = row["cache_key"]
        key = key.split(":", 1)[1] if key.startswith("v2:") else key   # drop version
        try:
            _, cat, id_key = key.split(":", 2)                          # raw:{cat}:{id}
            resp = _json.loads(row["response"]) if isinstance(row["response"], str) else row["response"]
            title = (resp or {}).get("title") or id_key
            year = (resp or {}).get("year")
        except Exception:
            continue
        if task is not None:
            try:
                from src.services.task_monitor import task_monitor
                task_monitor.update(
                    task, processed=checked, total=min(limit, total),
                    message=f"{cat}: {title} ({total - checked:,} unchecked overall)")
            except Exception:
                pass
        await wait_for_curator()
        try:
            if await asyncio.wait_for(
                topup_significance(title, cat, plex_rating_key=id_key, year=year),
                timeout=45.0,
            ):
                added += 1
        except Exception as e:
            logger.debug("[significance-backfill] %r failed: %s", title, e)
        checked += 1
    return {"checked": checked, "added": added,
            "remaining": max(0, total - checked)}


async def run_omdb_backfill(task=None, limit: Optional[int] = None) -> dict:
    """Admin bulk backfill: scan the raw:* cache for video items that have an
    imdb_id but are missing OMDb-only fields (writer / awards), and top each up
    via OMDb. Throttled by the OMDb semaphore; fast over the supporter key
    (100k/day). ``task`` is an optional task_monitor handle for progress.
    Returns ``{"candidates": N, "enriched": M}``."""
    import json as _json

    # 1. Gather candidates from the raw cache, deduped by (media_type, imdb_id)
    #    so we hit OMDb once per title even though it's cached under several keys.
    mc = MetadataCache()
    try:
        cur = mc.conn.cursor()
        cur.execute(
            "SELECT cache_key, response FROM api_cache WHERE "
            "cache_key LIKE '%raw:movie:%' OR cache_key LIKE '%raw:show:%' "
            "OR cache_key LIKE '%raw:anime:%'"
        )
        rows = cur.fetchall()
    finally:
        mc.close()

    candidates: dict = {}
    for k, resp in rows:
        try:
            b = _json.loads(resp)
        except Exception:
            continue
        if not isinstance(b, dict):
            continue
        imdb = b.get("imdb_id")
        if not imdb or imdb in ("", "N/A"):
            continue
        if b.get("omdb_checked") and b.get("ratings_checked"):
            continue   # fully checked (incl. the ratings era) — never re-query
        if (((b.get("writer") or "").strip() or (b.get("overview_extended") or "").strip())
                and b.get("ratings_checked")):
            continue   # OMDb data present AND ratings-era done — skip
        # docs checked before the full-harvest fix (no ratings_checked) fall
        # through: they get exactly ONE more fetch to pick up RT/Metacritic
        try:
            cat = k.split("raw:", 1)[1].split(":", 1)[0]
        except Exception:
            continue
        candidates.setdefault((cat, imdb), (b.get("title"), cat, imdb, b.get("tmdb_id")))

    cand_list = list(candidates.values())
    if limit:
        cand_list = cand_list[:limit]
    total = len(cand_list)
    logger.info("[omdb-backfill] %d candidate titles (imdb_id present, OMDb missing)", total)

    def _tick(msg, **kw):
        if task is None:
            return
        try:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, message=msg, **kw)
        except Exception:
            pass

    _tick(f"OMDb backfill: {total} candidates", total=total)

    state = {"updated": 0, "done": 0}
    cache = MetadataCache()
    try:
        async def _one(title, cat, imdb, tmdb):
            try:
                if await topup_omdb(title, cat, imdb_id=imdb, tmdb_id=tmdb, cache=cache):
                    state["updated"] += 1
            except Exception as e:
                logger.debug("[omdb-backfill] %r failed: %s", title, e)
            state["done"] += 1
            if state["done"] % 100 == 0:
                _tick(f"OMDb backfill: {state['done']}/{total} "
                      f"({state['updated']} enriched)", processed=state["done"])

        # Fire in chunks (OMDb concurrency is capped inside fetch_omdb_data, but
        # we don't want 15k coroutines alive at once).
        CHUNK = 200
        for i in range(0, total, CHUNK):
            await asyncio.gather(*[_one(*c) for c in cand_list[i:i + CHUNK]])
    finally:
        cache.close()

    logger.info("[omdb-backfill] done — %d/%d titles enriched", state["updated"], total)
    return {"candidates": total, "enriched": state["updated"]}


# ── Pass 99-fu3: Per-service concurrency caps for the parallel producer ─────
#
# Sized to stay well within each service's published rate limits even with
# the bulk producer running N workers in parallel. Anything more would risk
# bursts that trigger 429s (which we now handle gracefully via Pass 99,
# but better to never trigger them in the first place).
#
# MusicBrainz already has its own ``_MB_SEM = asyncio.Semaphore(1)`` in
# ``src/services/music_metadata.py`` — not duplicated here.
# Last.fm uses a 250 ms inter-call sleep in music_matcher; 4 concurrent
# callers stay under its 5 req/sec cap with margin to spare.
_SEM_TMDB:     "asyncio.Semaphore | None" = None  # 16 — TMDB has no published cap on free tier; 16 generous
_SEM_OMDB:     "asyncio.Semaphore | None" = None  # 4 — OMDb 1000/day; 4 concurrent safe
_SEM_JIKAN:    "asyncio.Semaphore | None" = None  # 2 — Jikan 3 req/sec; 2 leaves headroom
_LOCK_ANILIST: "asyncio.Lock | None"      = None  # serialises AniList; _anilist_wait isn't reentrant-safe


def _ensure_concurrency_primitives() -> None:
    """Lazy-init the module-level semaphores/locks on first use.

    They CAN be created at module import in modern Python, but lazy-init
    keeps test imports + tools that import this module outside an event
    loop safe (Semaphore() still works without a loop, but Lock() in
    some older patches did not — defensive).
    """
    global _SEM_TMDB, _SEM_OMDB, _SEM_JIKAN, _LOCK_ANILIST
    if _SEM_TMDB is None:
        _SEM_TMDB     = asyncio.Semaphore(16)
        _SEM_OMDB     = asyncio.Semaphore(10)   # Pass 99-fu11: OMDb is now PRIMARY for movie/show + supporter key (100k/day) → raised from 4
        _SEM_JIKAN    = asyncio.Semaphore(2)
        _LOCK_ANILIST = asyncio.Lock()


# ── PHASE 2 #38 — PER-ITEM SOURCE-STATE TRACKING ─────────────────────────────
# Records which external APIs were consulted for each item and how they
# responded. The dict is built up across one call of ``fetch_and_prepare_raw``
# and persisted to ``enrichment_status.sources_state`` by
# ``_write_enrichment_db``. The source-upgrade scheduler (#41) reads this to
# find items where a slow source (MusicBrainz, Jikan) is still pending and
# should be retried in the background.
#
# Status values:
#   "ok"        — API was called, returned useful data
#   "miss"      — API was called, returned nothing usable
#   "transient" — API was called, returned 429 / 5xx (retry later)
#   "skipped"   — not called this pass (e.g. fast_only mode)
#
# Known source keys: "tmdb", "omdb", "anilist", "jikan", "mb", "lastfm".
# The dict is sparse — only sources actually consulted (or deliberately
# skipped) appear. ``_record_source`` overwrites the entry if the same
# source is called twice in one pass (last call wins — matches "the
# latest evidence we have about this source for this item").

def _new_source_state() -> dict:
    """Return an empty per-item source-state container."""
    return {}


def _record_source(state: dict, src: str, status: str) -> None:
    """Record one API outcome into ``state`` with a UTC timestamp.

    Caller is responsible for picking the right status (the helper does
    not interpret return values from the API client — that varies by
    fetcher). Timestamp is ISO-8601 + ``Z`` so the scheduler can sort
    without timezone parsing.
    """
    state[src] = {"status": status, "at": datetime.utcnow().isoformat() + "Z"}


# ── PHASE 2 #39 — PER-SOURCE STREAMING-MERGE HELPERS ─────────────────────────
# These power the streaming fetch (``fetch_and_prepare_streaming``): instead
# of running APIs sequentially and waiting for the slowest before LLM polish,
# the producer fires every relevant source for an item IN PARALLEL. The first
# source with sufficient data triggers an LLM polish (item lands provisional);
# the remaining sources merge into raw + DB sources_state in the background,
# and when ALL expected sources are back the row flips fetch_tier='full' +
# provisional=False — the "provisorium marker rausnehmen" moment.

def _expected_sources_for(media_type: str, is_anime: bool, ids: dict) -> list[str]:
    """Which APIs should we fire IN PARALLEL for this item?

    Picks the set of sources based on the category + what IDs are available
    (an OMDb call only makes sense with an imdb_id, etc.). A source listed
    here means the streaming runner will await it as part of the "all
    expected sources back → mark non-provisional" gate; a source NOT listed
    will never block the upgrade.

    Music is always {mb, lastfm} (the existing pair). Movies/shows are
    {tmdb} + {omdb} when imdb_id is present (the OMDb-primary case from
    Pass 99-fu11). Anime is {anilist, jikan}; ``tmdb`` joins the set only
    when AniList has no usable result + we'd be falling back to a TMDB
    search anyway.
    """
    if media_type == "music":
        return ["mb", "lastfm"]
    if is_anime:
        sources = ["anilist"]
        sources.append("jikan")  # Jikan can title-search too — always try (audit 11b: was `if … or True`)
        return sources
    # movie / show
    sources = ["tmdb"]
    if ids.get("imdb_id"):
        sources.append("omdb")
    return sources


def _has_enough_data_for_polish(raw: dict, media_type: str) -> bool:
    """``True`` if ``raw`` already carries enough fields for the LLM
    summariser to produce a useful profile.

    "Enough" means the prompt's required fields are non-empty. Anything
    less and the LLM either invents data (bad) or returns a thin shell;
    we'd rather wait for one more source.
    """
    if media_type == "music":
        # SUMMARIZE_MUSIC_PROMPT needs name + (genres or bio).
        name = raw.get("title") or raw.get("name")
        if not name:
            return False
        has_substance = bool(raw.get("genres") or raw.get("tags") or raw.get("bio"))
        return has_substance
    # movie / show / anime — all use SUMMARIZE_PROMPT which needs title + overview.
    title = raw.get("title") or raw.get("original_title")
    overview = raw.get("overview") or raw.get("overview_extended") or raw.get("extra_context")
    return bool(title and overview)


def _merge_source_into_raw(raw: dict, source: str, data: Optional[dict],
                           media_type: str, is_anime: bool) -> None:
    """Merge a single per-source result into the accumulating ``raw`` dict.

    Always updates ``raw["sources_state"][source]`` with status + timestamp
    so the scheduler can see what's complete vs pending. Successful results
    flow through the existing ``_merge_raw_metadata`` helper (which already
    knows how to combine TMDB / OMDb / AniList / Jikan / MB-Last.fm shapes
    sanely); a miss / transient just stamps the state and returns.
    """
    if not data:
        _record_source(raw.setdefault("sources_state", {}), source, "miss")
        return

    # Each source's data has a slightly different shape — normalise the
    # transient fields first so the merge stays clean.
    if source == "lastfm" and media_type == "music":
        norm = {
            "genres":          data.get("genres", []),
            "tags":            data.get("tags", []),
            "similar_artists": data.get("similar_artists", []),
            "bio":             data.get("bio", ""),
            "listeners":       data.get("listeners"),
        }
    elif source == "mb" and media_type == "music":
        norm = {
            "mbid":    data.get("mbid"),
            "type":    data.get("type", ""),
            "country": data.get("country", ""),
            "genres":  data.get("genres", []),
            "tags":    data.get("tags", []),
            "rating":  data.get("rating"),
        }
    else:
        # OMDb / TMDB / AniList / Jikan all come out of their own fetchers
        # already shaped for ``_merge_raw_metadata``; pass through.
        norm = dict(data)

    # Seed any keys that aren't yet on raw (setdefault preserves existing
    # data — the first source's fields win on conflict, subsequent sources
    # only fill gaps). Then run the canonical _merge_raw_metadata for the
    # richer list/text fields (genres/cast/tags de-dup, alt_plot_sources
    # population, etc.).
    if not raw.get("title"):
        # Some Last.fm/MB shapes use ``name`` instead of ``title``;
        # promote it so downstream code finds the artist name.
        if media_type == "music" and norm.get("name"):
            raw["title"] = norm["name"]
    for k, v in norm.items():
        raw.setdefault(k, v)

    # _merge_raw_metadata is the canonical merge (de-dup genres / cast /
    # tags, populate alt_plot_sources, extra_context, tone_hints). It
    # expects primary + supplements; we pass raw as primary so it keeps
    # IDs + earlier text fields preferentially.
    try:
        merged = _merge_raw_metadata(raw, norm)
        raw.clear()
        raw.update(merged)
    except Exception as _me:
        # _merge_raw_metadata historically crashed on shape drift
        # (Pass 99-fu11 float-rating bug). Don't let a merge failure
        # take down the whole streaming run — log + keep the
        # setdefault'd data we already have.
        logger.debug("[stream] merge into raw failed for %s: %s", source, _me)

    # Provenance label: comma-join the contributing source names.
    existing_label = raw.get("source") or ""
    if not existing_label:
        raw["source"] = source
    elif source not in existing_label.split("+"):
        raw["source"] = f"{existing_label}+{source}"

    _record_source(raw.setdefault("sources_state", {}), source, "ok")


async def _fetch_source(source: str, ctx: dict) -> Optional[dict]:
    """Dispatch one source-name to the right async fetcher + return its result.

    ``ctx`` carries everything a fetcher might need: ``title``, ``media_type``,
    ``tmdb_id``, ``anilist_id``, ``anidb_id``, ``imdb_id``, ``mal_id``, ``year``.
    Returns the source-specific dict on success or ``None`` on miss / failure
    — the streaming runner stamps the corresponding sources_state entry.

    Each branch swallows its own exceptions so one source's failure can't
    take down the whole parallel fan-out. The per-source semaphores
    inside the underlying fetchers (TMDB / OMDb / Jikan / AniList /
    MusicBrainz) still enforce each API's rate limit independently — we
    fire the tasks in parallel but each task hits its own gate before
    actually calling the API.
    """
    title      = ctx.get("title", "")
    media_type = ctx.get("media_type", "")
    try:
        if source == "lastfm":
            from src.services.music_metadata import fetch_lastfm_artist
            return await fetch_lastfm_artist(title)
        if source == "mb":
            from src.services.music_metadata import fetch_musicbrainz_artist
            return await fetch_musicbrainz_artist(title, mbid=ctx.get("mbid"))
        if source == "omdb":
            imdb = ctx.get("imdb_id")
            if not imdb:
                return None
            return await fetch_omdb_data(imdb)
        if source == "tmdb":
            tmdb_id = ctx.get("tmdb_id")
            year    = ctx.get("year")
            endpoint = "movie" if media_type == "movie" else "tv"
            try:
                if tmdb_id:
                    return await fetch_tmdb_full(tmdb_id, endpoint)
                return await _tmdb_search_and_fetch(title, endpoint, year=year)
            except TMDBTransientError as te:
                # Don't propagate — let the abort-streak logic in the
                # producer's _process_one handle 429s based on the
                # sources_state we'll stamp as "transient" upstream.
                logger.debug("[stream] tmdb transient on %r: HTTP %s",
                             title, te.status_code)
                return None
        if source == "anilist":
            anilist_id = ctx.get("anilist_id")
            anidb_id   = ctx.get("anidb_id")
            if anilist_id:
                return await fetch_anilist_full(anilist_id)
            if anidb_id:
                result = await search_anilist_by_title(title, year=ctx.get("year"))
                if result:
                    result["anidb_id"] = anidb_id
                return result
            return await search_anilist_by_title(title, year=ctx.get("year"))
        if source == "jikan":
            mal_id = ctx.get("mal_id")
            return await fetch_jikan_data(
                mal_id=mal_id,
                title=None if mal_id else title,
            )
    except Exception as e:
        logger.debug("[stream] %s fetch failed for %r: %s", source, title, e)
    return None


async def _streaming_fetch_runner(
    ctx: dict,
    expected_sources: list[str],
) -> tuple[Optional[dict], dict[str, asyncio.Task]]:
    """Fire ``expected_sources`` in parallel + return the first sufficient
    raw blob plus a dict of any still-running tasks.

    Behaviour:
      1. ``asyncio.create_task`` for each expected source.
      2. Iterate ``asyncio.as_completed`` — on every completion, merge the
         result into a shared ``raw`` accumulator.
      3. The moment ``_has_enough_data_for_polish(raw, media_type)`` flips
         true, snapshot ``raw`` + return it; the still-pending tasks ride
         along in ``remaining`` so the producer can spawn a background
         finalizer that updates the DB once they all complete.
      4. If we drain every source without ever hitting the polish
         threshold → return (None, {}) so the caller writes a not_found
         sentinel (matches the pre-streaming behaviour).

    ``ctx`` is the same context dict ``_fetch_source`` uses; it must
    contain ``media_type`` at minimum.
    """
    media_type = ctx.get("media_type", "")
    # Seed raw with the always-known fields BEFORE per-source merges run.
    # Without this seed, ``_has_enough_data_for_polish`` for music can
    # reject an item whose Last.fm data is present but whose ``title``
    # field was never set (Last.fm's per-source dict uses ``name``).
    raw: dict = {
        "title":         ctx.get("title", ""),
        "media_type":    media_type,
        "sources_state": _new_source_state(),
    }
    tasks: dict[str, asyncio.Task] = {
        s: asyncio.create_task(_fetch_source(s, ctx)) for s in expected_sources
    }
    name_by_task = {t: s for s, t in tasks.items()}
    sufficient = False

    # Walk completions in arrival order. We DO NOT cancel pending tasks
    # when sufficiency is reached — the producer wants those background
    # results to keep flowing into the DB (the "slower API füllt langsamer
    # parallel" half of the streaming model the user designed).
    pending = set(tasks.values())
    initial_raw: Optional[dict] = None
    is_anime = ctx.get("is_anime", False)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        # Merge every task that completed in this batch BEFORE checking
        # sufficiency. If lastfm + mb both finish in the same wait window,
        # we want both in sources_state, not just whichever the for-loop
        # iteration order picked first.
        for t in done:
            src = name_by_task[t]
            try:
                data = t.result()
            except Exception as _e:
                data = None
                logger.debug("[stream] %s task error: %s", src, _e)
            _merge_source_into_raw(raw, src, data, media_type, is_anime)
        # Now check sufficiency post-merge.
        if not sufficient and _has_enough_data_for_polish(raw, media_type):
            sufficient = True
            # Snapshot for the LLM polish; deep-copy so later background
            # merges into ``raw`` can't mutate the snapshot the caller
            # hands to the consumer for polish.
            from copy import deepcopy
            initial_raw = deepcopy(raw)
            # ``pending`` is the still-running task set — the finalizer
            # awaits these to flip provisional=False once they all return.
            remaining = {name_by_task[p]: p for p in pending}
            initial_raw["_remaining_tasks"]    = remaining
            initial_raw["_live_sources_state"] = raw["sources_state"]
            initial_raw["_live_raw_ref"]       = raw   # finalizer updates here
            return initial_raw, remaining
    # All sources drained, never reached the polish threshold.
    return None, {}


async def _finalize_streaming_merge(
    plex_rating_key: str,
    live_raw: dict,
    remaining_tasks: dict,
    media_type: str,
    id_key,
    is_anime: bool,
) -> None:
    """Phase 2 #39: background finalizer for the streaming-merge runner.

    Awaits the still-running per-source tasks from a streaming run that
    returned early on first-sufficiency, merges each result into the
    shared ``live_raw`` (the same dict the runner was mutating), then
    updates the DB row's sources_state JSON + flips fetch_tier='full' +
    provisional=False once all expected sources are accounted for. Also
    overwrites the raw cache blob with the merged full-tier data so a
    future fetch on the same id_key gets the upgraded result on a
    cache hit.

    Race-safety: the producer schedules THIS coroutine via
    ``asyncio.create_task`` only AFTER the consumer's polish-write has
    been queued via ``cat_queues[cat].put``. The consumer's
    ``_write_enrichment_db`` commits provisional=True (initial state);
    when THIS function commits later, it does so on the same row but
    only touches the source-state columns (sources_state, fetch_tier,
    provisional) — no overlap with the consumer's
    (enriched, enriched_at, error) write.
    """
    if not remaining_tasks:
        # Nothing to wait on — all sources were merged inline already.
        # Still update the DB to flip provisional=False if needed.
        pass

    for src, task in remaining_tasks.items():
        try:
            data = await task
        except Exception as _e:
            data = None
            logger.debug("[finalize] %s task error for %s: %s", src, plex_rating_key, _e)
        _merge_source_into_raw(live_raw, src, data, media_type, is_anime)

    # Update the DB row.
    final_state = live_raw.get("sources_state", {})
    try:
        # Lazy imports inside the function — this is called from an
        # asyncio.create_task background context and we want to avoid
        # circular-import hassles with the router module.
        from src.database.connection import get_db_session
        from src.database.models import EnrichmentStatus
        import json as _json
        with get_db_session() as db:
            row = db.query(EnrichmentStatus).filter(
                EnrichmentStatus.plex_rating_key == plex_rating_key,
            ).first()
            if row:
                row.sources_state = _json.dumps(
                    final_state, separators=(",", ":"), sort_keys=True,
                )
                row.fetch_tier = "full"
                row.provisional = False
                db.commit()
                logger.debug(
                    "[finalize] promoted %s to full (sources: %s)",
                    plex_rating_key, sorted(final_state.keys()),
                )
    except Exception as _e:
        logger.warning("[finalize] DB update failed for %s: %s", plex_rating_key, _e)

    # Update the raw cache blob with the merged data so future cache
    # hits get the upgraded full-tier content. _write_raw_cache strips
    # underscored transport keys, so we can pass live_raw directly.
    try:
        # Mark the cached blob as full-tier for the read path.
        live_raw["cache_tier"] = "full"
        live_raw["plex_rating_key"] = plex_rating_key
        if id_key:
            _write_raw_cache(media_type, id_key, live_raw)
    except Exception as _e:
        logger.debug("[finalize] raw-cache write failed for %s: %s", plex_rating_key, _e)


# ── ANILIST RATE-LIMIT CIRCUIT BREAKER ────────────────────────────────────────
# AniList allows 90 req/min. We enforce a 0.75 s floor (~80/min) and honour
# any Retry-After header from 429 responses via a shared module-level backoff.
# Pass 99-fu3: the wait function is now wrapped in ``_LOCK_ANILIST`` so
# parallel producer workers serialise correctly through it.

_anilist_backoff_until: float = 0.0   # monotonic timestamp; 0 = not backed off
_anilist_last_req: float = 0.0        # monotonic timestamp of last request sent
_ANILIST_MIN_INTERVAL: float = 0.75   # seconds between requests


async def _anilist_wait() -> None:
    """Block until AniList is no longer backed off and the minimum interval has passed.

    Pass 99-fu3: held under ``_LOCK_ANILIST`` so concurrent producer
    workers serialise through the throttle. The pre-fu3 implementation
    read + wrote ``_anilist_last_req`` without a lock, so N parallel
    callers could all pass the ``since_last < interval`` check at the
    same time and burst-request — triggering the very 429s the throttle
    was meant to avoid.
    """
    _ensure_concurrency_primitives()
    async with _LOCK_ANILIST:
        global _anilist_last_req
        now = time.monotonic()
        # Honour circuit-breaker backoff first
        backoff_wait = _anilist_backoff_until - now
        if backoff_wait > 0:
            logger.info("AniList circuit-breaker: sleeping %.0fs", backoff_wait)
            await asyncio.sleep(backoff_wait)
        # Enforce minimum per-request spacing
        since_last = time.monotonic() - _anilist_last_req
        if since_last < _ANILIST_MIN_INTERVAL:
            await asyncio.sleep(_ANILIST_MIN_INTERVAL - since_last)
        _anilist_last_req = time.monotonic()


def _anilist_set_backoff(headers) -> float:
    """Parse Retry-After from a 429 response and set the module-level backoff.

    Returns the number of seconds we will wait.
    """
    global _anilist_backoff_until
    raw = headers.get("retry-after") or headers.get("Retry-After") or ""
    try:
        seconds = max(float(raw), 1.0) if raw else 60.0
    except ValueError:
        seconds = 60.0
    seconds += 3.0  # small safety buffer
    _anilist_backoff_until = time.monotonic() + seconds
    logger.warning("AniList 429 — circuit-breaker active for %.0fs", seconds)
    return seconds

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Small fast model for metadata summarization - change in .env as SUMMARIZER_MODEL
SUMMARIZER_MODEL = (
    getattr(settings, "SUMMARIZER_MODEL", None)
    or getattr(settings, "BASE_SUMMARIZER_MODEL", None)
    or "qwen2.5:3b"
)


# ── TMDB FULL FETCH ───────────────────────────────────────────────────────────

class TMDBTransientError(Exception):
    """Pass 99: raised by ``_tmdb_get`` for 429 / 5xx / network failures.

    The pre-99 silent ``return {}`` on any non-200 was indistinguishable
    from a legitimate "TMDB has no record of this title" — the caller
    chain then collapsed to ``raw=None`` and the producer wrote a
    ``not_found`` sentinel. A short TMDB outage during a bulk run could
    poison thousands of rows with bogus not_found sentinels in minutes.

    Raising on transient errors lets the producer (a) sleep for the
    Retry-After window, (b) skip the item without writing a sentinel —
    the next enrichment run picks it up cleanly when TMDB recovers.

    ``retry_after_s`` carries the server's Retry-After header value when
    present (in seconds), or a sensible default the producer can use.
    """
    def __init__(self, status_code: int, retry_after_s: float = 5.0,
                 path: str = "", body_snippet: str = ""):
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.path = path
        self.body_snippet = body_snippet
        super().__init__(f"TMDB {path} → HTTP {status_code} (retry in {retry_after_s}s)")


async def _tmdb_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    """Single TMDB API call with error handling.

    Returns:
      - ``{}`` only when TMDB_API_KEY isn't configured (caller skips TMDB entirely).
      - ``r.json()`` on HTTP 200 (may be ``{"results": []}`` for a legitimate miss).

    Raises:
      - ``TMDBTransientError`` on 429 (rate-limit), 5xx (server error),
        or any network-level exception. Caller should back off and retry,
        NOT treat as "title not findable".
      - On 4xx other than 429 (e.g. 404 for a stale movie ID): returns ``{}``.
        That's a real "no data" answer — caller is welcome to write a
        not_found sentinel.
    """
    if not settings.TMDB_API_KEY:
        return {}
    _ensure_concurrency_primitives()
    p = {"api_key": settings.TMDB_API_KEY, "language": "en-US", **(params or {})}
    async with _SEM_TMDB:   # Pass 99-fu3: cap parallel TMDB calls (16 concurrent)
        try:
            r = await client.get(f"https://api.themoviedb.org/3{path}", params=p)
        except Exception as e:
            # Network-level failure (DNS, connection reset, timeout). Always
            # treat as transient — the next run may well succeed.
            raise TMDBTransientError(0, retry_after_s=5.0, path=path,
                                     body_snippet=f"network: {type(e).__name__}: {e}")

    if r.status_code == 200:
        return r.json()

    if r.status_code == 429 or 500 <= r.status_code < 600:
        # Honour Retry-After if present; else back off conservatively.
        # TMDB usually returns it as integer seconds; sometimes HTTP-date,
        # which we don't parse — fall back to 5s in that case.
        ra_raw = r.headers.get("retry-after", "")
        try:
            ra = float(ra_raw) if ra_raw else 5.0
        except (TypeError, ValueError):
            ra = 5.0
        ra = max(1.0, min(ra, 120.0))   # clamp to [1s, 2min] sane window
        raise TMDBTransientError(r.status_code, retry_after_s=ra, path=path,
                                 body_snippet=r.text[:120])

    # 4xx other than 429 — real "not found" / "bad request". Caller can
    # interpret an empty result as "TMDB really doesn't have this".
    return {}


async def fetch_tmdb_full(tmdb_id: int, media_type: str = "movie") -> dict:
    """
    Fetch everything TMDB knows about a title in parallel:
    - Main details (plot, runtime, genres, rating, year)
    - Credits (top cast + director)
    - Keywords / tags
    - Similar titles
    - Videos (trailer available?)
    - External IDs (IMDb)
    """
    endpoint = "movie" if media_type in ("movie", "radarr") else "tv"

    async with httpx.AsyncClient(timeout=15) as client:
        details, credits, keywords, ext_ids = await asyncio.gather(
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}", {"append_to_response": ""}),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/credits"),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/keywords"),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/external_ids"),
        )

    if not details:
        return {}

    # Extract cast (top 8) and director
    cast = []
    director = None
    crew = credits.get("crew", [])
    for member in credits.get("cast", [])[:8]:
        cast.append(member.get("name", ""))
    for member in crew:
        if member.get("job") == "Director":
            director = member.get("name")
            break
    # For TV: creator instead of director
    if not director and endpoint == "tv":
        creators = details.get("created_by", [])
        if creators:
            director = creators[0].get("name")

    # Source material. TMDB files the novelist / playwright under their own
    # crew job; the flat `writer` field only ever carried the screenwriter.
    # For an adaptation the source author is the strongest pedigree signal
    # there is — a le Carré adaptation is not "a spy thriller by whoever
    # wrote the screenplay" — and it was invisible to every judgment. The
    # keyword "based on novel or book" said an adaptation existed without
    # ever saying whose.
    by_job = {}
    for member in crew:
        job = (member.get("job") or "").strip()
        if job in _SOURCE_JOBS and member.get("name"):
            by_job.setdefault(job, member["name"])
    source_author = source_kind = None
    for job in _SOURCE_JOBS:            # in priority order, not crew order
        if job in by_job:
            source_author, source_kind = by_job[job], job
            break

    # Keywords / tags
    kw_list = keywords.get("keywords") or keywords.get("results", [])
    keyword_names = [k.get("name", "") for k in kw_list[:20]]

    # Genres
    genres = [g.get("name", "") for g in details.get("genres", [])]

    # Year
    date_str = details.get("release_date") or details.get("first_air_date") or ""
    year = int(date_str[:4]) if date_str else None

    # Rating
    vote_avg = details.get("vote_average")
    vote_count = details.get("vote_count")

    # Runtime
    runtime = details.get("runtime") or (
        details.get("episode_run_time", [None])[0]
        if details.get("episode_run_time") else None
    )

    # Seasons (TV)
    seasons = details.get("number_of_seasons")
    episodes = details.get("number_of_episodes")

    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": details.get("title") or details.get("name", ""),
        "original_title": details.get("original_title") or details.get("original_name", ""),
        "year": year,
        "overview": details.get("overview", ""),
        "genres": genres,
        "cast": cast,
        "director": director,
        "source_author": source_author,
        "source_kind": source_kind,
        "keywords": keyword_names,
        "rating": vote_avg,
        "vote_count": vote_count,
        "runtime_min": runtime,
        "seasons": seasons,
        "episodes_total": episodes,
        "imdb_id": ext_ids.get("imdb_id"),
        "original_language": details.get("original_language"),
        "tagline": details.get("tagline", ""),
        "source": "tmdb",
    }


async def fetch_omdb_data(imdb_id: str) -> Optional[dict]:
    """
    Fetch additional metadata from OMDB (free, 1000 req/day).
    Returns plot, awards, Rotten Tomatoes score, Metacritic.
    Requires OMDB_API_KEY in .env — silently returns None if unavailable.
    """
    omdb_key = getattr(settings, "OMDB_API_KEY", None)
    if not omdb_key or not imdb_id:
        return None
    _ensure_concurrency_primitives()
    try:
        async with _SEM_OMDB, httpx.AsyncClient(timeout=8) as client:   # Pass 99-fu3: cap 4 concurrent
            r = await client.get("https://www.omdbapi.com/", params={
                "apikey": omdb_key,
                "i": imdb_id,
                "plot": "full",
            })
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("Response") == "False":
            # OMDb answers "False" for two OPPOSITE reasons, and they must not
            # share a value. "Movie not found!" is a definitive statement about
            # the id — treating it as transient meant the title stayed pending
            # for ever, was re-queried on every walk, and the backfill button
            # could never finish its own job. "Request limit reached!" is a
            # statement about TODAY, and stamping it would freeze a quota
            # hiccup into a permanent "nothing there".
            err = (d.get("Error") or "").lower()
            if "not found" in err or "incorrect imdb" in err:
                return {}          # definitive — this id has no OMDb record
            return None            # transient — quota, bad key, anything else

        # Extract Rotten Tomatoes and Metacritic scores
        ratings = {}
        for rating in d.get("Ratings", []):
            src = rating.get("Source", "")
            val = rating.get("Value", "")
            if "Rotten Tomatoes" in src:
                ratings["rt"] = val
            elif "Metacritic" in src:
                ratings["metacritic"] = val.replace("/100", "")

        # Pass 99-fu11: extract OMDb's CORE fields too, so OMDb can serve as a
        # PRIMARY source for movie/show (not just a plot/ratings supplement) —
        # this offloads TMDB, which 429-aborts under the parallel lanes. When
        # OMDb is used as a supplement, _merge_raw_metadata only reads the
        # plot/genre/awards/ratings keys, so these extra fields are inert there.
        def _omdb_int(v):
            try:
                return int(str(v).replace(",", "").split()[0])
            except (ValueError, IndexError, AttributeError):
                return None
        def _omdb_float(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None
        _na = lambda s: "" if s in ("N/A", None) else s
        _yr = re.search(r"\d{4}", str(d.get("Year") or ""))
        actors_str = _na(d.get("Actors", "")) or ""
        genre_str  = _na(d.get("Genre", "")) or ""

        return {
            "source": "omdb",
            # ── core fields (OMDb-as-primary) ──
            "title": _na(d.get("Title")) or None,
            "year": int(_yr.group()) if _yr else None,
            "media_type": "movie" if d.get("Type") == "movie" else "tv",
            "genres": [g.strip() for g in genre_str.split(",") if g.strip()],
            "overview": _na(d.get("Plot", "")),
            "director": _na(d.get("Director", "")),
            "cast": [a.strip() for a in actors_str.split(",") if a.strip()],
            "runtime_min": _omdb_int(d.get("Runtime")),
            "rating": _omdb_float(d.get("imdbRating")),
            "vote_count": _omdb_int(d.get("imdbVotes")),
            "imdb_id": d.get("imdbID"),
            # ── supplement fields (OMDb-as-supplement to TMDB) ──
            "plot_full": _na(d.get("Plot", "")),
            "awards": d.get("Awards", ""),
            "ratings": ratings,
            "metacritic": d.get("Metascore"),
            "box_office": d.get("BoxOffice"),
            "writer": d.get("Writer", ""),
            "country": d.get("Country", ""),
            "language": d.get("Language", ""),
        }
    except Exception as e:
        logger.debug("OMDB error for %s: %s", imdb_id, e)
        return None


async def fetch_jikan_data(mal_id: int = None, title: str = None) -> Optional[dict]:
    """
    Fetch additional anime metadata from Jikan (MAL API proxy).
    Free, no key needed. Returns synopsis, genres, themes, demographics, score.
    """
    _ensure_concurrency_primitives()
    try:
        async with _SEM_JIKAN, httpx.AsyncClient(timeout=10) as client:   # Pass 99-fu3: cap 2 concurrent
            if mal_id:
                r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            elif title:
                r = await client.get("https://api.jikan.moe/v4/anime",
                    params={"q": title, "limit": 8})
                if r.status_code == 200:
                    results = r.json().get("data", [])
                    if not results:
                        return None
                    # Validate title match before using any result
                    matched = None
                    for result in results:
                        candidate_titles = [
                            result.get("title", ""),
                            result.get("title_english", ""),
                            result.get("title_japanese", ""),
                        ]
                        if _titles_close_enough(title, candidate_titles):
                            matched = result
                            break
                    if not matched:
                        logger.debug(
                            "Jikan title search '%s' → no close match in top %d results, skipping",
                            title, len(results)
                        )
                        return None
                    mal_id = matched["mal_id"]
                    r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            else:
                return None

            if r.status_code == 429:
                logger.debug("Jikan rate limited for '%s' — skipping supplement", title or mal_id)
                return None
            if r.status_code != 200:
                return None
            data = r.json().get("data", {})
        if not data:
            return None

        # Extract themes, demographics, explicit genres
        explicit_genres = [g["name"] for g in data.get("explicit_genres", [])]
        themes = [t["name"] for t in data.get("themes", [])]
        demographics = [d["name"] for d in data.get("demographics", [])]
        genres = [g["name"] for g in data.get("genres", [])]

        return {
            "source": "mal",
            "mal_id": data.get("mal_id"),
            "synopsis": (data.get("synopsis") or "")[:800],
            "genres": genres,
            "themes": themes,
            "demographics": demographics,
            "explicit_genres": explicit_genres,
            "score": data.get("score"),
            "scored_by": data.get("scored_by"),
            "rank": data.get("rank"),
            "popularity": data.get("popularity"),
            "episodes": data.get("episodes"),
            "status": data.get("status"),
            "rating": data.get("rating"),  # e.g. "R - 17+ (violence & profanity)"
            "studios": [s["name"] for s in data.get("studios", [])],
            "source_material": data.get("source"),  # e.g. "Manga", "Light novel"
        }
    except Exception as e:
        logger.debug("Jikan error for %s/%s: %s", mal_id, title, e)
        return None


_ADULT_TAGS = {
    "bdsm", "ecchi", "nudity", "sexual content", "fan service", "fanservice",
    "hentai", "erotica", "explicit sex", "sexual violence", "rape",
    "exhibitionism", "voyeurism", "bondage", "sadomasochism",
}
_DARK_TAGS = {
    "gore", "body horror", "graphic violence", "torture", "war crimes",
    "psychological horror", "disturbing", "death game", "survival game",
}
_SUBVERSION_TAGS = {
    "villain protagonist", "antihero", "dark magical girl",
    "magical girl subversion", "deconstruction",
}

# Adult-GENRE guard (the Batman Beyond catch): TVDB/arr genre lists are
# community-editable and 'Hentai' arrived on a DCAU series via Sonarr — the
# old curator then confabulated an explicit profile from that one word. An
# adult genre survives the merge only when an anime-database source lists it
# itself or the content rating (Rx/Hentai) confirms it. Deliberately NOT
# guarding 'Ecchi' (mild, common, low poison risk) and never touching music
# (its raw build doesn't pass through this merge).
_ADULT_GENRES = {"hentai", "erotica"}
_ADULT_CONFIRMING_SOURCES = {"anilist", "jikan", "mal", "anidb"}


def _lists_adult_genre(d: dict) -> bool:
    pool = (d.get("genres") or []) + (d.get("explicit_genres") or [])
    return any(str(g).lower() in _ADULT_GENRES for g in pool)


def _merge_raw_metadata(primary: dict, *supplements) -> dict:
    """
    Merge metadata from multiple sources into a richer raw profile.
    Primary source takes precedence. Supplements add/extend fields.
    Collects ALL plot descriptions across sources (instead of picking the longest)
    so the LLM receives the full picture. Derives tone hints from structural
    metadata (demographics, content rating, explicit tags) to guide mood output.
    """
    merged = dict(primary)
    all_keywords = list(primary.get("keywords", []))
    all_genres = list(primary.get("genres", []))
    extra_context = []
    tone_hints = []
    adult_genre_confirmed = (
        str(primary.get("source", "")).lower() in _ADULT_CONFIRMING_SOURCES
        and _lists_adult_genre(primary))

    # Track all distinct plot descriptions by source name
    plot_sources: dict[str, str] = {}
    primary_overview = (primary.get("overview") or "").strip()
    if primary_overview:
        plot_sources["primary"] = primary_overview

    for sup in supplements:
        if not sup:
            continue

        # Collect supplement plot texts under their source label
        sup_source = sup.get("source", "supplement")
        for plot_field in ("synopsis", "plot_full", "overview"):
            text = (sup.get(plot_field) or "").strip()
            if text and text != primary_overview and text not in plot_sources.values():
                plot_sources[sup_source] = text
                break

        # Merge keywords/tags — deduplicated
        for key in ("themes", "tags", "keywords", "explicit_genres"):
            for tag in (sup.get(key) or []):
                if tag and tag.lower() not in [k.lower() for k in all_keywords]:
                    all_keywords.append(tag)

        # Merge genres
        for g in (sup.get("genres") or []):
            if g and g not in all_genres:
                all_genres.append(g)
        if (str(sup_source).lower() in _ADULT_CONFIRMING_SOURCES
                and _lists_adult_genre(sup)):
            adult_genre_confirmed = True

        # Append awards/ratings info as context
        if sup.get("awards") and sup["awards"] not in ("N/A", ""):
            extra_context.append(f"Awards: {sup['awards']}")
        if sup.get("ratings"):
            for src, val in sup["ratings"].items():
                extra_context.append(f"{src.upper()}: {val}")
        if sup.get("source_material"):
            extra_context.append(f"Source: {sup['source_material']}")
        if sup.get("demographics"):
            extra_context.append(f"Target audience: {', '.join(sup['demographics'])}")
            for demo in sup["demographics"]:
                if demo in ("Shounen", "Shoujo"):
                    tone_hints.append(f"{demo} demographic — typically optimistic/kinetic/adventurous")
                elif demo in ("Seinen", "Josei"):
                    tone_hints.append(f"{demo} demographic — can be mature/dark/complex")

        # Tone hints from MAL content rating. Jikan puts a STRING here
        # (e.g. "R - 17+", "Rx - Hentai"); OMDb/TMDB put a NUMERIC score in
        # `rating`, which is NOT a content rating. Guard the type so a float
        # can't crash the ``in`` checks below — Pass 99-fu11: once
        # fetch_omdb_data started returning a float `rating`, this line threw
        # ``argument of type 'float' is not iterable`` for EVERY movie/show in
        # _merge_raw_metadata, so _process_one errored on each one and the
        # movie/show/anime lanes produced nothing (only cached music flowed).
        mal_rating = sup.get("rating", "")
        if isinstance(mal_rating, str) and mal_rating:
            extra_context.append(f"Content rating: {mal_rating}")
            if "Rx" in mal_rating or "Hentai" in mal_rating:
                tone_hints.append("Adult/explicit sexual content — do not sanitize in summary")
                adult_genre_confirmed = True   # the content rating itself confirms
            elif "R+" in mal_rating:
                tone_hints.append("Mature content (R+) — likely intense/dark/adult themes")
            elif mal_rating in ("G", "PG"):
                tone_hints.append("All-ages content — likely lighthearted/optimistic/comedic")

        # Tone hints from explicit genres (Comedy is a strong signal)
        all_explicit = sup.get("explicit_genres", []) + sup.get("genres", [])
        if "Comedy" in all_explicit and "Horror" not in all_explicit and "Psychological" not in all_explicit:
            if "Ecchi" in all_explicit or "Harem" in all_explicit:
                tone_hints.append("Ecchi comedy — primarily comedic/lighthearted despite adult themes")
            else:
                tone_hints.append("Comedy is a primary genre — mood should reflect comedic tone")

        # Fill in missing fields
        for field in ("studios", "score", "episodes", "mal_id", "rank",
                      "director", "writer", "country", "year", "popularity"):
            if sup.get(field) and not merged.get(field):
                merged[field] = sup[field]

    # Tone hints from tags across ALL sources (primary + supplements)
    all_tags_lower = {t.lower() for t in all_keywords}
    matched_adult = all_tags_lower & _ADULT_TAGS
    matched_dark = all_tags_lower & _DARK_TAGS
    matched_subversion = all_tags_lower & _SUBVERSION_TAGS

    if matched_adult:
        tag_list = ", ".join(sorted(matched_adult))
        tone_hints.append(
            f"Explicit/adult tags present: [{tag_list}] — describe these accurately, do not sanitize"
        )
    if matched_dark:
        tag_list = ", ".join(sorted(matched_dark))
        tone_hints.append(f"Dark/disturbing content tags: [{tag_list}]")
    if matched_subversion:
        tag_list = ", ".join(sorted(matched_subversion))
        tone_hints.append(f"Subversive premise tags: [{tag_list}] — make the subversion explicit in plot_summary")

    # Build overview_extended from the longest available plot text
    best_plot = max(plot_sources.values(), key=len) if plot_sources else ""
    if best_plot and best_plot != primary_overview:
        merged["overview_extended"] = best_plot

    # Store all alternative plot descriptions for the LLM
    alt_plots = {k: v for k, v in plot_sources.items() if k != "primary" and v != best_plot}
    if alt_plots:
        merged["alt_plot_sources"] = alt_plots

    merged["keywords"] = list(dict.fromkeys(all_keywords))[:25]  # dedup, max 25
    genres_final = list(dict.fromkeys(all_genres))
    if not adult_genre_confirmed:
        _dropped = [g for g in genres_final if str(g).lower() in _ADULT_GENRES]
        if _dropped:
            genres_final = [g for g in genres_final
                            if str(g).lower() not in _ADULT_GENRES]
            logger.info("[merge] dropped unconfirmed adult genre(s) %s for %r — "
                        "no anime-DB source or content rating backs them",
                        _dropped, merged.get("title"))
    merged["genres"] = genres_final
    if extra_context:
        merged["extra_context"] = " | ".join(extra_context)
    if tone_hints:
        merged["tone_hints"] = " | ".join(tone_hints)

    return merged

ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    description(asHtml: false)
    genres
    tags { name rank isMediaSpoiler }
    averageScore meanScore popularity
    episodes duration
    startDate { year }
    endDate { year }
    studios(isMain: true) { nodes { name } }
    staff(sort: RELEVANCE) { edges { role node { name { full } } } }
    recommendations(sort: RATING_DESC) { nodes { mediaRecommendation { title { romaji english } } } }
    demographics: tags(sort: RANK_DESC) { name rank category }
  }
}
"""


async def fetch_anilist_full(anilist_id: int) -> dict:
    """Fetch rich AniList data including tags, demographics, staff."""
    try:
        await _anilist_wait()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_QUERY, "variables": {"id": anilist_id}},
            )
            if r.status_code == 429:
                _anilist_set_backoff(r.headers)
                return {}
            if r.status_code != 200:
                return {}
            media = r.json().get("data", {}).get("Media")
            if not media:
                return {}
    except Exception as e:
        logger.debug("AniList %s error: %s", anilist_id, e)
        return {}

    # Pass 56: AniList can return ``title: null`` on some entries. dict.get
    # with a default only substitutes when the KEY is missing — a present-
    # but-null value passes straight through, so ``media["title"].get(...)``
    # crashed with "'NoneType' object has no attribute 'get'". ``or {}``
    # catches the null case too.
    _mt = media.get("title") or {}
    title = _mt.get("english") or _mt.get("romaji", "")

    # Tags: filter spoilers, keep top 15 by rank
    tags = [
        t["name"] for t in sorted(
            [t for t in (media.get("tags") or []) if not t.get("isMediaSpoiler")],
            key=lambda t: t.get("rank", 0), reverse=True
        )[:15]
    ]

    # Staff: director/series director
    director = None
    for edge in (media.get("staff", {}).get("edges") or []):
        if "Director" in (edge.get("role") or ""):
            director = edge["node"]["name"]["full"]
            break

    # Recommendations
    similar = []
    for node in (media.get("recommendations", {}).get("nodes") or [])[:8]:
        # Pass 56: ``or {}`` (not a .get default) — AniList may send
        # mediaRecommendation: null or title: null on sparse entries.
        rec = node.get("mediaRecommendation") or {}
        t = rec.get("title") or {}
        name = t.get("english") or t.get("romaji", "")
        if name:
            similar.append(name)

    studios = [s["name"] for s in (media.get("studios", {}).get("nodes") or [])]

    return {
        "anilist_id": anilist_id,
        "media_type": "anime",
        "title": title,
        "year": (media.get("startDate") or {}).get("year"),
        "overview": (media.get("description") or "").replace("<br>", "\n")[:1000],
        "genres": media.get("genres", []),
        "tags": tags,
        "director": director,
        "studios": studios,
        # Pass 54: averageScore is AniList's weighted score — it stays null
        # for fresh / niche titles that haven't cleared the confidence
        # threshold yet. meanScore (plain average) is populated earlier, so
        # fall back to it before giving up. Either way it's a real user
        # rating; 0 only if AniList genuinely has neither.
        "rating": (media.get("averageScore") or media.get("meanScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


# ── SMALL LLM SUMMARIZER ──────────────────────────────────────────────────────

SUMMARIZE_MUSIC_PROMPT = """[MODE: MUSIC METADATA STRUCTURING]
Produce a structured JSON profile for a music artist. Be precise — this drives semantic search.

ARTIST: {title}
GENRES: {genres}
TAGS: {tags}
BIO: {bio}
SIMILAR ARTISTS: {similar}
LISTENERS: {listeners}

MOOD REFERENCE — pick ONLY 1-3 moods dominant in this artist's work:
-- bleak: hopeless, nihilistic atmosphere
-- melancholic: sadness, longing, loss as primary emotional register
-- intense: sustained emotional or sonic pressure
-- kinetic: propulsive energy, high-tempo, danceable
-- darkly comedic: dark subjects treated with humor
-- unsettling: dissonant, disturbing, uncomfortable
-- contemplative: slow, introspective, philosophical
-- optimistic: uplifting, affirming, hopeful
-- cathartic: emotional release, purging
-- euphoric: transcendent highs, exhilarating
-- romantic: love and longing as primary driver
-- epic: grand scale, anthemic, sweeping
-- dreamlike: surreal, hypnagogic, hazy
-- nostalgic: memory-driven, retro feel
-- tense: suspenseful, on-edge
-- comedic: humor is primary
-- raw: unpolished, visceral, immediate

Output this exact JSON (no extra text, no markdown fences):
{{
  "title": "...",
  "media_type": "music",
  "artist_summary": "2-3 sentences outlining their defining sound and identity.",
  "why_listen": "1 sentence — the specific sonic or lyrical quality that defines them.",
  "embedding_text": "A dense, objective 3-4 sentence paragraph for semantic vector search. CRITICAL RULE: Do NOT write a music review or use marketing filler. Densely pack the specific subgenres, lyrical themes, instrumentation, and emotional tone into a factual summary.",
  "genres": ["2-5 music genres"],
  "themes": ["4-8 lyrical or sonic themes — be highly specific"],
  "mood": ["1-3 from MOOD REFERENCE"],
  "keywords": ["8-12 precise descriptors: era, subgenre, instrumentation, vocal style, cultural references"],
  "similar_artists": {similar_json},
  "rating": {rating}
}}"""

SUMMARIZE_PROMPT = """[MODE: METADATA STRUCTURING]
Produce a structured JSON profile. Be precise — this data drives semantic vector search and recommendations.

GROUNDING DISCIPLINE — apply these strictly so the output stays faithful to the source:

1. Source-trace every concrete claim. Character traits, plot origins, settings,
   numbers, and proper names in your output must be visibly supported by the
   TITLE / OVERVIEW / EXTENDED INFO / TONE HINTS / CAST fields below. If a
   detail is not present in those fields, do not invent one.

2. Numbers in the source are exact. Reproduce any quantity that appears in
   the TITLE or OVERVIEW verbatim — do not round, paraphrase, or substitute.

3. The TAGS/KEYWORDS field describes themes of the work overall. Character-
   archetype labels (psychological-type tropes, trope names) live there as
   genre signals — they do NOT attribute the archetype to any specific
   character. Attribute an archetype to a named character only when the
   OVERVIEW makes that attribution itself.

4. The TONE HINTS field is calibrated by the upstream data source and is
   authoritative. When it says a work is comedic, the work's primary register
   is comedic — do not recast it under heavier literary frameworks based on
   subject matter alone.

5. NO-FILL POLICY (Anti-Magnet-Halluzination). Apply to all prose fields
   (plot_summary, why_watch, embedding_text):

   a) Character names: USE ONLY names that appear verbatim in OVERVIEW
      or CAST field. If a character is mentioned in OVERVIEW only by
      role (e.g. "a streetwise hustler"), refer to them by that role —
      never assign a name from your prior knowledge.

   b) Specific terms (system names, location names, faction names):
      USE ONLY terms that appear verbatim in OVERVIEW or EXTENDED INFO.
      If unsure, describe the concept generically ("an oversight system",
      "a faction") rather than invent a proper noun.

   c) Plot mechanics: do NOT introduce plot elements not present in
      OVERVIEW. If overview is sparse, prefer shorter prose to invented
      detail.

   When in doubt: OMIT, don't invent. A shorter accurate description
   beats a longer fabricated one.

6. DESCRIBE, DON'T EDITORIALIZE (Anti-Over-Labeling). themes / keywords /
   why_watch / embedding_text must DESCRIBE what the work plainly contains —
   never impose a critical, academic, or ideological READING the source itself
   does not state. Do NOT project:
     • loaded fandom labels ("siscon", "incest-coded", "problematic", "edgy"),
     • critical-theory frames ("heteronormative", "the male gaze", "subverts",
       "deconstructs", "interrogates", "late-capitalist"),
     • or psychoanalytic / thesis-style readings,
   onto a premise that is, on its face, simpler. A supportive sibling is NOT
   "siscon dynamics"; a sincere romance is NOT "a critique of heteronormative
   expectations"; a straightforward comedy is NOT "subversive". Reach for the
   plain genre/content vocabulary a knowledgeable video-store clerk would use,
   not a thesis committee. If the OVERVIEW doesn't frame the work in those
   terms, neither do you — a tag you cannot point to in the source is invented.

TITLE: {title} ({year})
TYPE: {media_type}
GENRES: {genres}
TAGS/KEYWORDS: {keywords}
OVERVIEW: {overview}{alt_plots_section}
EXTENDED INFO: {extra_context}
TONE HINTS (use to calibrate mood): {tone_hints}
CAST: {cast}
DIRECTOR/CREATOR: {director}
RATING: {rating}/10 ({votes} votes)

MOOD REFERENCE — pick ONLY 1-3 moods that are DOMINANT throughout the whole work:
- bleak: hopeless atmosphere, no redemption
- melancholic: the PRIMARY emotional register is sadness, loss, or longing
- intense: sustained PHYSICAL or PSYCHOLOGICAL pressure
- kinetic: propulsive energy, action-forward, fast pace
- darkly comedic: dark/taboo subjects ARE THE CORE SOURCE OF HUMOR
- unsettling: persistent dread, wrongness, psychological discomfort
- contemplative: genuinely slow and philosophical
- optimistic: genuinely hopeful, affirming tone
- cathartic: built around emotional release
- euphoric: exhilarating transcendent highs
- harrowing: deeply disturbing, demands emotional fortitude
- romantic: love/longing as primary emotional driver
- epic: grand mythic scale, sweeping stakes
- dreamlike: surreal logic, hypnagogic atmosphere
- nostalgic: emotional pull of the past, memory-driven
- tense: thriller-mode suspense, persistent threat
- comedic: humor is the primary mode
- informative: educational/documentary tone

Output this exact JSON (no extra text, no markdown fences):
{{
  "title": "...",
  "year": ...,
  "media_type": "...",
  "plot_summary": "2-3 sentences. What actually happens and what is the core conflict.",
  "why_watch": "1 sentence. The main hook or specific appeal of the show.",
  "embedding_text": "A dense, objective 3-4 sentence paragraph for semantic vector search. CRITICAL RULE: Do NOT write a review, critique, or use marketing filler. Instead, densely pack the plot premise, core character dynamics, specific tropes, visual style, and emotional tone into a cohesive summary.",
  "genres": [...],
  "themes": ["4-8 concrete narrative tropes / story elements / visual themes actually present in the source — descriptive, not interpretive (see rule 6: no critical, ideological, or loaded-fandom labels)"],
  "mood": ["pick 2-3 from the MOOD REFERENCE above"],
  "keywords": ["10 plain descriptors: tone, setting, tropes, style, era, subgenre — what the work IS, not a critical reading of it (rule 6)"],
  "cast_top3": ["Maximum 3 ACTOR names. Use ONLY names that appear verbatim in the CAST field above. If CAST is empty or 'Unknown', output an empty array []. Do NOT include directors, character names, or descriptive labels like 'various villains' or 'voice cast'."],
  "director": "...",
  "rating": ...
}}"""


async def summarize_with_small_llm(raw_metadata: dict) -> Optional[dict]:
    """
    Use the small/fast summarizer model to create a structured profile.
    Falls back to a rule-based profile if Ollama is unavailable.
    """
    import json as _json

    if raw_metadata.get("media_type") == "music":
        similar = raw_metadata.get("similar_artists", [])
        # Strip any Wikipedia-style footnote citations ([4], [12], etc.) that
        # may have slipped through from cached Last.fm bios before the fix.
        _raw_bio = (raw_metadata.get("bio", "") or "")
        _clean_bio = re.sub(r'\[\d+\]', '', _raw_bio).strip()
        prompt = SUMMARIZE_MUSIC_PROMPT.format(
            title=raw_metadata.get("title") or raw_metadata.get("name", "Unknown"),
            genres=", ".join(raw_metadata.get("genres", [])),
            tags=", ".join(raw_metadata.get("tags", [])[:15]),
            bio=_clean_bio[:500],
            similar=", ".join(similar[:8]),
            similar_json=_json.dumps(similar[:8]),
            listeners=raw_metadata.get("listeners") or "N/A",
            rating=raw_metadata.get("rating") or "N/A",
        )
    else:
        # Build alternative plot section if multiple sources are available
        alt_plots = raw_metadata.get("alt_plot_sources", {})
        if alt_plots:
            lines = ["\nALTERNATIVE DESCRIPTIONS (synthesize with OVERVIEW above):"]
            for src_name, text in alt_plots.items():
                lines.append(f"- {src_name.upper()}: {text[:400]}")
            alt_plots_section = "\n".join(lines)
        else:
            alt_plots_section = ""

        # Filter character-archetype tags (tsundere/kuudere/yandere/...)
        # from the prompt-input keywords list. See _filter_archetype_tags
        # above — these tags are work-level themes but the LLM attention-
        # bridges them to nearby character noun tokens + misattributes.
        # The raw blob's keywords field is unchanged; only the prompt
        # sees the filtered list.
        _kw_for_prompt = _filter_archetype_tags(
            raw_metadata.get("keywords") or raw_metadata.get("tags", [])
        )[:20]
        prompt = SUMMARIZE_PROMPT.format(
            title=raw_metadata.get("title", "Unknown"),
            year=raw_metadata.get("year", "Unknown"),
            media_type=raw_metadata.get("media_type", "movie"),
            genres=", ".join(raw_metadata.get("genres", [])),
            keywords=", ".join(_kw_for_prompt),
            overview=(raw_metadata.get("overview_extended") or raw_metadata.get("overview", ""))[:800],
            alt_plots_section=alt_plots_section,
            extra_context=raw_metadata.get("extra_context", "N/A"),
            tone_hints=raw_metadata.get("tone_hints", "N/A"),
            cast=", ".join(raw_metadata.get("cast", [])[:5]),
            director=raw_metadata.get("director") or "Unknown",
            rating=raw_metadata.get("rating") or "N/A",
            votes=raw_metadata.get("vote_count") or "N/A",
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    **ollama_options(temperature=0.1, num_predict=2600),
                },
            )
        if r.status_code != 200:
            logger.warning("Summarizer HTTP %s for '%s' (model=%s)",
                           r.status_code, raw_metadata.get("title", "?"), SUMMARIZER_MODEL)
            return _rule_based_profile(raw_metadata)

        raw_content = r.json().get("message", {}).get("content", "").strip()
        if not raw_content:
            logger.debug("Summarizer returned empty content for %s", raw_metadata.get("title"))
            return _rule_based_profile(raw_metadata)

        content = clean_llm_text(raw_content)

        try:
            result = json.loads(content)
            raw_source = raw_metadata.get("source", "unknown")
            result["source"] = f"{raw_source}+llm"

            # Idea C: title-number self-correction loop.
            # If any digit-run from the source TITLE is missing from the
            # polished output, the LLM has paraphrased a fact ("4400" -->
            # "4,000 individuals"). Retry ONCE with an explicit override
            # listing the required numbers. Best-effort: if the retry
            # call fails or still misses, keep the first attempt rather
            # than fall back to rule-based (the polish is still useful,
            # just one fact is wrong). Cost: zero for the ~98% of items
            # whose title has no digits; one extra ~3-5 s LLM call on
            # the ~2% that need correction.
            _title_nums = _extract_title_numbers(raw_metadata.get("title", ""))
            if _title_nums and not _numbers_preserved_in_profile(_title_nums, result):
                logger.info(
                    "[summarize] title-numbers %s missing in polish for %r — "
                    "retrying with override directive",
                    _title_nums, raw_metadata.get("title", "?"),
                )
                _override = prompt + (
                    "\n\n[CRITICAL: Your previous attempt missed numbers from the "
                    "title. These numbers MUST appear verbatim (as digits, no commas, "
                    f"no paraphrase) in your plot_summary and embedding_text: "
                    f"{', '.join(_title_nums)}. Re-emit the JSON.]"
                )
                try:
                    async with httpx.AsyncClient(timeout=120) as _c2:
                        _r2 = await _c2.post(
                            f"{settings.effective_ollama}/api/chat",
                            json={
                                "model": SUMMARIZER_MODEL,
                                "messages": [{"role": "user", "content": _override}],
                                "stream": False,
                                **ollama_options(temperature=0.1, num_predict=2600),
                            },
                        )
                    if _r2.status_code == 200:
                        _content2 = clean_llm_text(
                            _r2.json().get("message", {}).get("content", "").strip()
                        )
                        try:
                            _result2 = json.loads(_content2)
                            if _numbers_preserved_in_profile(_title_nums, _result2):
                                _result2["source"] = f"{raw_source}+llm"
                                result = _result2
                                logger.info(
                                    "[summarize] number-retry FIXED %r",
                                    raw_metadata.get("title", "?"),
                                )
                            else:
                                logger.info(
                                    "[summarize] number-retry STILL missing %s for %r — "
                                    "keeping first attempt",
                                    _title_nums, raw_metadata.get("title", "?"),
                                )
                        except json.JSONDecodeError:
                            logger.debug(
                                "[summarize] number-retry JSON parse fail for %r — "
                                "keeping first attempt", raw_metadata.get("title", "?"),
                            )
                except Exception as _re:
                    logger.debug(
                        "[summarize] number-retry call failed for %r: %s — "
                        "keeping first attempt", raw_metadata.get("title", "?"), _re,
                    )

            logger.debug("Summarizer success for '%s': source=%s",
                         raw_metadata.get("title", "?"), result["source"])
            return result
        except json.JSONDecodeError as e:
            recovered = None
            try:
                partial = content.strip()

                # Strategy 1: cut at the last cleanly closed string/array field
                for marker in ['",\n', '"\n', '],\n', ']\n', '",']:
                    pos = partial.rfind(marker)
                    if pos > 100:
                        try:
                            recovered = json.loads(partial[:pos + 1] + "\n}")
                            break
                        except json.JSONDecodeError:
                            continue

                # Strategy 2: truncation happened mid-string value
                # Close the open string, then close the JSON object
                if not recovered:
                    # Find last complete key-value pair boundary before the cut
                    last_comma = partial.rfind(',\n  "')
                    if last_comma > 100:
                        try:
                            recovered = json.loads(partial[:last_comma] + "\n}")
                        except json.JSONDecodeError:
                            pass

                # Strategy 3: close the dangling string then the object
                if not recovered and not partial.endswith("}"):
                    for suffix in ['"}', '"]}', '"]\n}']:
                        try:
                            recovered = json.loads(partial + suffix)
                            break
                        except json.JSONDecodeError:
                            continue

                if recovered:
                    recovered["source"] = f"{raw_metadata.get('source', 'unknown')}+llm"
                    logger.debug("Recovered truncated JSON for '%s'", raw_metadata.get("title", "?"))
            except Exception as _e:
                # Pass 47 (B3-rest): the suffix-loop is a recovery best-effort,
                # but a silent miss here masks bugs in the recovery itself.
                logger.debug("[enricher] truncated-JSON recovery failed: %s", _e)
            if recovered:
                return recovered
            logger.warning("LLM JSON parse failed for '%s': %s — raw: %s",
                           raw_metadata.get("title", "?"), e, content[:300])

    except httpx.TimeoutException:
        logger.warning("Summarizer timeout for '%s'", raw_metadata.get("title", "?"))
    except Exception as e:
        logger.warning("Summarizer LLM error for '%s': %s — %s",
                       raw_metadata.get("title", "?"), type(e).__name__, e)

    # Rule-based fallback
    return _rule_based_profile(raw_metadata)


def _rule_based_profile(m: dict) -> dict:
    """Deterministic profile when LLM is unavailable."""
    genres = m.get("genres", [])
    keywords = m.get("keywords") or m.get("tags", [])

    if m.get("media_type") == "music":
        similar = m.get("similar_artists", [])
        bio = (m.get("bio", "") or "")[:300]
        embedding_text = (
            f"{m.get('title', '')} — {', '.join(genres[:6])}. "
            f"Tags: {', '.join(keywords[:8])}. "
            + (f"Similar to: {', '.join(similar[:5])}. " if similar else "")
            + bio
        )
        return {
            "title": m.get("title", ""),
            "media_type": "music",
            "genres": genres,
            "themes": keywords[:6],
            "mood": [],
            "artist_summary": bio,
            "why_listen": "",
            "keywords": keywords[:12],
            "similar_artists": similar,
            "rating": m.get("rating"),
            "embedding_text": embedding_text,
            "source": f"{m.get('source', 'unknown')}:rule_based",
        }

    overview = m.get("overview", "")
    overview_short = overview[:500].rsplit(' ', 1)[0] + "..." if len(overview) > 500 else overview
    
    embedding_text = (
        f"{m.get('title', '')} ({m.get('year', '')}). "
        f"Genres: {', '.join(genres)}. "
        f"Tags: {', '.join(keywords[:10])}. "
        f"{overview_short}"
    )

    return {
        "title": m.get("title", ""),
        "year": m.get("year"),
        "media_type": m.get("media_type", "movie"),
        "genres": genres,
        "themes": keywords[:4],
        "mood": [],
        "audience": "",
        "plot_summary": overview_short,
        "why_watch": "",
        "keywords": keywords[:10],
        "cast_top3": m.get("cast", [])[:3],
        "director": m.get("director"),
        "rating": m.get("rating"),
        "embedding_text": embedding_text,
        "source": f"{m.get('source', 'unknown')}:rule_based",
    }


# ── PRODUCER-CONSUMER SPLIT FUNCTIONS ────────────────────────────────────────

async def fetch_and_prepare_raw(
    title: str,
    media_type: str = "movie",
    tmdb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    anidb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    mal_id: Optional[int] = None,
    mbid: Optional[str] = None,    # MusicBrainz artist id (Lidarr) — disambiguates name collisions
    plex_rating_key: Optional[str] = None,
    sonarr_series_type: Optional[str] = None,
    year: Optional[int] = None,    # disambiguation hint for title search
    fast_only: bool = False,       # Phase 2 #38b: provisional fast-pass mode
) -> Optional[dict]:
    """Phase 1: fetch API metadata only (no LLM).

    Returns a raw dict with ``_cache_key`` / ``_plex_rating_key`` /
    ``_tmdb_id`` / ``_anilist_id`` embedded so the consumer can save
    without needing those IDs separately.

    Three return shapes that the caller MUST distinguish (Pass 99-fu):
      - dict with ``_already_enriched=True`` + ``_cached_profile`` →
        the cache already holds a fully-enriched profile for this
        item (LLM-polished, < 7 days fresh, or both). Caller should
        reconcile the EnrichmentStatus row with the cached profile
        and skip — do NOT write a not_found sentinel, do NOT re-fetch.
      - non-None dict without those keys → fresh raw API data, ready
        for the consumer's LLM pipeline.
      - ``None`` → no API data exists for this title. Caller writes
        a not_found sentinel.

    Pre-99-fu this function conflated "already done" and "no data" by
    returning None for both, which made the producer write not_found
    sentinels for items that were already fully enriched (cache hit on
    one id-key, EnrichmentStatus row reset and re-queued, fetch sees
    fresh cache and returns None, producer writes a misleading
    not_found sentinel). After a bulk EnrichmentStatus reset this
    poisoned 200 rows in 3 seconds.
    """
    cache = MetadataCache()

    # ── OWNER MATCH OVERRIDE (highest authority, before ANY resolution) ──
    # A pinned entity wins over arr-provided ids, MediaIdentity, and every
    # title search below. This is what makes a "Fix match" click durable
    # across rescans and re-enrichments (the Good-Boy-twins class — ported
    # from SoulSync's match-override layer, MIT).
    if plex_rating_key and ":" in str(plex_rating_key):
        try:
            from src.database.connection import get_db_session
            from src.database.models import MediaMatchOverride
            _svc, _, _aid = str(plex_rating_key).partition(":")
            if _aid.isdigit():
                with get_db_session() as db:
                    ov = db.query(MediaMatchOverride).filter(
                        MediaMatchOverride.service == _svc,
                        MediaMatchOverride.arr_id == int(_aid)).first()
                    if ov:
                        tmdb_id = ov.tmdb_id or tmdb_id
                        tvdb_id = ov.tvdb_id or tvdb_id
                        anilist_id = ov.anilist_id or anilist_id
                        mal_id = ov.mal_id or mal_id
                        imdb_id = ov.imdb_id or imdb_id
                        mbid = ov.mbid or mbid
                        logger.info("[enricher] owner match override active "
                                    "for %s (tmdb=%s anilist=%s mbid=%s)",
                                    plex_rating_key, ov.tmdb_id,
                                    ov.anilist_id, ov.mbid)
        except Exception as _e:
            logger.debug("[enricher] match-override lookup failed: %s", _e)

    # Resolve IDs from MediaIdentity
    if plex_rating_key and not all([tmdb_id, anilist_id]):
        try:
            from src.database.connection import get_db_session
            from src.database.models import MediaIdentity
            with get_db_session() as db:
                mi = db.query(MediaIdentity).filter(
                    MediaIdentity.plex_rating_key == plex_rating_key
                ).first()
                if mi:
                    tmdb_id    = tmdb_id    or mi.tmdb_id
                    anilist_id = anilist_id or mi.anilist_id
                    anidb_id   = anidb_id   or mi.anidb_id
                    tvdb_id    = tvdb_id    or mi.tvdb_id
                    imdb_id    = imdb_id    or mi.imdb_id
                    # Pass 73: mal_id was written by plex_sync (from Plex Guid
                    # tags) but never pulled here — so the deterministic Jikan
                    # lookup below never fired from the stored ID, it fell back
                    # to a fuzzy title search.
                    mal_id     = mal_id     or mi.mal_id
        except Exception as _e:
            # Pass 43 (B3): MediaIdentity lookup is best-effort; the
            # caller fills missing IDs via direct API calls when this
            # fast-path returns nothing. Log so persistent DB issues
            # don't hide behind the silent swallow.
            logger.debug("[enricher] MediaIdentity merge failed: %s", _e)

    is_anime = (
        media_type == "anime"
        or sonarr_series_type == "anime"
        or (sonarr_series_type is None and _looks_like_anime(title))
    )

    id_key = anilist_id or anidb_id or tmdb_id or tvdb_id or title[:40]
    cache_key = f"enriched:{media_type}:{id_key}"

    # Check raw pre-fetch cache written by game-mode consumer.
    # If present, return it directly so no API call is needed.
    if plex_rating_key:
        raw_cached = cache.get_cache(f"raw_prefetch:{plex_rating_key}")
        if raw_cached:
            raw_data = dict(raw_cached["response"])
            raw_data["_cache_key"]       = cache_key
            raw_data["_plex_rating_key"] = plex_rating_key
            raw_data["_tmdb_id"]         = tmdb_id or raw_data.get("_tmdb_id")
            raw_data["_anilist_id"]      = anilist_id or raw_data.get("_anilist_id")
            # Phase 2 #38a/b: game-mode prefetched blobs were ALWAYS the
            # canonical full fetch (game-mode skips only the LLM polish,
            # not the API round-trips). Stamp tier="full" + provisional=
            # False so the consumer's downstream write reconciles the DB
            # row consistently with everything else.
            cached_tier = raw_data.get("cache_tier", "full")
            raw_data["_fetch_tier"]  = cached_tier
            raw_data["_provisional"] = (cached_tier == "fast")
            cache.close()
            logger.debug("Raw prefetch cache hit for '%s' (%s)", title, plex_rating_key)
            return raw_data

    # Pass 99-fu2: two-tier cache read.
    #
    # Tier 1 (polished): ``enriched:{cat}:{id_key}`` carries the LLM-
    # polished profile tagged with ``prompt_version``. Hit + version
    # match → fully done; signal ``_already_enriched`` so the producer
    # reconciles the EnrichmentStatus row without re-running the LLM.
    #
    # Tier 2 (raw):     ``raw:{cat}:{id_key}`` carries just the API
    # fetch result (TMDB/AniList/MB/Last.fm response normalised). Hit
    # → hand straight to the consumer for LLM polish; SKIP the API
    # round-trip. This is what makes a prompt-version bump cheap: we
    # don't re-spam TMDB just because the LLM prompt changed.
    #
    # Pre-99-fu2 there was only one cache (the polished one), so a
    # version-stale entry would force a full re-fetch from the APIs —
    # multiplying the LLM-rebake cost by API rate limits and TMDB
    # outage risk. Splitting them keeps prompt-bumps to LLM-compute only.

    from datetime import datetime as _dt
    polished_hit = cache.get_cache(cache_key)  # tier 1
    raw_hit_key  = f"raw:{media_type}:{id_key}"
    raw_hit      = cache.get_cache(raw_hit_key)  # tier 2
    cache.close()

    if polished_hit:
        cached_profile = polished_hit.get("response", {})
        cached_source  = cached_profile.get("source", "")
        cached_version = cached_profile.get("prompt_version")
        cached_at_str  = polished_hit.get("created_at") or polished_hit.get("cached_at")
        cache_age_days = 999
        if cached_at_str:
            try:
                cached_dt = _dt.fromisoformat(str(cached_at_str))
                cache_age_days = (_dt.utcnow() - cached_dt).days
            except Exception as _e:
                logger.debug("[enricher] cached_at parse failed: %r → %s", cached_at_str, _e)

        is_llm_polished = "+llm" in cached_source or cached_source == "llm"
        is_recent_miss  = cached_source == "not_found" and cache_age_days < 3

        if is_llm_polished and cached_version == _PROMPT_VERSION:
            # Tier-1 hit, prompt version matches — terminal state.
            # Phase 2 #38a: legacy cached profiles (cached pre-#38a) lack
            # ``fetch_tier``. They went through the canonical fetch path
            # so default them to "full"; ``_write_enrichment_db`` will
            # stamp the DB column on every cache-hit reconcile.
            cached_profile.setdefault("fetch_tier", "full")
            # #38b: tier-mismatch check. If the cached profile is "fast"
            # but the caller wants a full enrichment, fall through past
            # the tier-1 short-circuit so a fresh full fetch + re-polish
            # actually runs. The #41 source-upgrade scheduler uses this
            # by triggering an enrichment run with fast_only=False on
            # provisional rows.
            if cached_profile.get("fetch_tier") == "fast" and not fast_only:
                logger.debug(
                    "[enricher] polished cache for '%s' is fast-tier but "
                    "caller wants full — falling through to fresh fetch",
                    title,
                )
            else:
                return {
                    "_already_enriched": True,
                    "_cached_profile":   cached_profile,
                    "_cache_key":        cache_key,
                    "_plex_rating_key":  plex_rating_key,
                }
        if is_recent_miss:
            # not_found sentinel still fresh — skip silently as before.
            # Same back-fill as above so not_found rows also carry the
            # tier marker on the row (the producer wrote them via the
            # full canonical path).
            cached_profile.setdefault("fetch_tier", "full")
            return {
                "_already_enriched": True,
                "_cached_profile":   cached_profile,
                "_cache_key":        cache_key,
                "_plex_rating_key":  plex_rating_key,
            }
        if is_llm_polished and cached_version != _PROMPT_VERSION:
            # Polished profile from an OLDER prompt — fall through to
            # tier-2 raw cache (and then to fresh fetch as a last
            # resort). The consumer will re-polish with the current
            # prompt. We don't return the stale profile.
            logger.debug(
                "[enricher] polished cache version mismatch (have %r, want %r) "
                "for %s — will re-polish from raw cache or fresh fetch",
                cached_version, _PROMPT_VERSION, cache_key,
            )

    if raw_hit:
        # Tier-2 hit — we have the API fetch result, skip the round-trip.
        # Embed the cache routing fields so the consumer's ``process_and_save``
        # writes the polished cache back under the same id_key.
        raw_data = dict(raw_hit["response"])
        # Phase 2 #38b: cached blob carries its own tier via
        # ``cache_tier`` (legacy cached blobs from pre-#38b don't —
        # we default them to "full" since the only writer back then was
        # the canonical full path).
        cached_tier = raw_data.get("cache_tier", "full")
        if cached_tier == "fast" and not fast_only:
            # Caller wants a full enrichment but cache only has fast-tier
            # data — bypass the cache so a real full fetch runs. The
            # outer code below will overwrite the cached blob with the
            # full result on success.
            logger.debug("[enricher] raw cache hit for '%s' is fast-tier but caller "
                         "wants full — bypassing cache", title)
        else:
            raw_data["_cache_key"]       = cache_key
            raw_data["_plex_rating_key"] = plex_rating_key
            raw_data["_tmdb_id"]         = tmdb_id    or raw_data.get("_tmdb_id")
            raw_data["_anilist_id"]      = anilist_id or raw_data.get("_anilist_id")
            raw_data["_from_raw_cache"]  = True
            # We don't have per-source breakdown here (the cached blob is
            # the MERGED result, not the per-source outcomes), so leave
            # _sources_state absent — the DB will see
            # fetch_tier=<cached_tier> and sources_state=NULL, which is
            # honest about both facts.
            raw_data["_fetch_tier"] = cached_tier
            raw_data["_provisional"] = (cached_tier == "fast")
            return raw_data

    # Anime cross-ref ID resolution
    if is_anime and tvdb_id and not anilist_id and not anidb_id:
        try:
            from src.services.anime_mapping import get_anime_mapping
            mapping = await get_anime_mapping()
            ids = mapping.lookup_tvdb(tvdb_id)
            if ids.get("anidb_id"):
                anidb_id = ids["anidb_id"]
            if ids.get("anilist_id"):
                anilist_id = ids["anilist_id"]
        except Exception as e:
            logger.debug("anime-lists lookup failed: %s", e)

    # Phase 2 #39: streaming-merge fetch.
    # All relevant per-source APIs fire IN PARALLEL via
    # _streaming_fetch_runner. The runner returns as soon as the first
    # source provides enough data for the LLM polish to run — the item
    # lands provisional with the consumer queue. Remaining slow-source
    # tasks ride along in raw["_remaining_tasks"]; the producer
    # (``_process_one`` in enrichment.py) is expected to spawn
    # ``_finalize_streaming_merge`` as a background asyncio task that
    # awaits those, merges their results into the raw cache + DB
    # sources_state, and flips provisional=False / fetch_tier='full'
    # once all expected sources are back — the "provisorium marker
    # rausnehmen" moment.
    #
    # Pre-#39 the body was a ~250-line serial waterfall: OMDb-primary,
    # then TMDB, then supplements, all sequential — the LLM polish
    # waited for the slowest (MB at 1 req/s for music = music-lane
    # killer). Each per-API semaphore (TMDB_SEM=16, OMDb_SEM=10,
    # MB_SEM=1, Jikan_SEM=2, AniList_LOCK) still enforces its own rate
    # limit inside the parallel fan-out, so we don't burst-429 any
    # upstream.

    ctx = {
        "title":       title,
        "media_type":  media_type,
        "is_anime":    is_anime,
        "tmdb_id":     tmdb_id,
        "anilist_id":  anilist_id,
        "anidb_id":    anidb_id,
        "tvdb_id":     tvdb_id,
        "imdb_id":     imdb_id,
        "mal_id":      mal_id,
        "mbid":        mbid,
        "year":        year,
    }
    expected = _expected_sources_for(media_type, is_anime, ctx)

    # ``fast_only`` skips slow sources entirely (they don't fire, don't
    # land in _remaining_tasks). The #41 source-upgrade scheduler later
    # re-runs these items with fast_only=False, which lets the streaming
    # runner fire the previously-skipped sources to complete the row.
    skipped: list[str] = []
    if fast_only:
        slow = {
            "music": {"mb"},
            "anime": {"jikan"},
            "movie": set(),
            "show":  set(),
        }
        skip_set = slow.get(media_type, set())
        skipped = [s for s in expected if s in skip_set]
        expected = [s for s in expected if s not in skip_set]

    initial_raw, remaining_tasks = await _streaming_fetch_runner(ctx, expected)
    if initial_raw is None:
        # No source had enough data for a polish — let the caller write
        # a not_found sentinel (same as the pre-#39 ``if not raw: return None``).
        return None
    # Entity-resolution safety net (Fix A, bulk path): the arr gave a year but the
    # resolved entry's year is wildly off (>5y) → a different same-named work.
    # Enrich nothing rather than feed the curator confidently-wrong metadata.
    try:
        _ry, _ty = int(initial_raw.get("year") or 0), int(year or 0)
    except (TypeError, ValueError):
        _ry = _ty = 0
    if _ty and _ry and abs(_ry - _ty) > 5:
        logger.warning(
            "[enricher] resolution YEAR MISMATCH (bulk) for %r: arr=%s resolved=%s "
            "(%r) — rejecting wrong-entity match",
            title, _ty, _ry, initial_raw.get("title"),
        )
        return None

    # Stamp skipped-source statuses into both the snapshot and the
    # live raw (the finalizer reads from the live ref). We do this
    # AFTER the runner so the polish-readiness gate isn't tricked by a
    # "skipped" entry into thinking a source contributed.
    for s in skipped:
        _record_source(initial_raw.setdefault("sources_state", {}), s, "skipped")
        live_ref = initial_raw.get("_live_raw_ref")
        if live_ref is not None:
            _record_source(live_ref.setdefault("sources_state", {}), s, "skipped")

    # Persist the initial-source raw cache so a sibling producer (a
    # different plex_rating_key resolving to the same id_key) hits the
    # tier-2 cache instead of re-firing the same APIs. ``_write_raw_cache``
    # strips underscored transport keys, so the cached blob is clean.
    # The finalizer overwrites this with the merged full-tier blob once
    # all sources are back.
    initial_raw_cache_blob = dict(initial_raw)
    initial_raw_cache_blob["plex_rating_key"] = plex_rating_key
    initial_raw_cache_blob["cache_tier"] = (
        "fast" if (fast_only or remaining_tasks) else "full"
    )
    _write_raw_cache(media_type, id_key, initial_raw_cache_blob)

    # Transport fields for the consumer (process_and_save pops these,
    # promotes them to public profile fields, and writes to the DB row
    # in _write_enrichment_db).
    initial_raw["_cache_key"]       = cache_key
    initial_raw["_plex_rating_key"] = plex_rating_key
    initial_raw["_tmdb_id"]         = tmdb_id    or initial_raw.get("tmdb_id")
    initial_raw["_anilist_id"]      = anilist_id or initial_raw.get("anilist_id")
    initial_raw["_sources_state"]   = initial_raw.get("sources_state", {})
    # Tier semantics:
    #   fast_only=True           → "fast" forever (scheduler upgrades later)
    #   remaining_tasks present  → "fast" now, finalizer flips to "full"
    #   neither                  → "full" already (all sources back inline)
    if fast_only or remaining_tasks:
        initial_raw["_fetch_tier"]  = "fast"
        initial_raw["_provisional"] = True
    else:
        initial_raw["_fetch_tier"]  = "full"
        initial_raw["_provisional"] = False
    # Finalizer payload — the producer pops these along with
    # ``_remaining_tasks`` + ``_live_raw_ref`` and spawns
    # ``_finalize_streaming_merge`` as an asyncio.create_task. See
    # enrichment.py::_process_one for the wiring.
    initial_raw["_finalize_media_type"] = media_type
    initial_raw["_finalize_id_key"]     = id_key
    initial_raw["_finalize_is_anime"]   = is_anime

    logger.debug(
        "[enricher] #39 streaming: title=%r tier=%r remaining=%s sources_initial=%s",
        title, initial_raw["_fetch_tier"],
        list(remaining_tasks.keys()) if remaining_tasks else [],
        list(initial_raw["_sources_state"].keys()),
    )
    return initial_raw


async def process_and_save(raw: dict) -> Optional[dict]:
    """Phase 2: LLM summarize + MetadataCache + ChromaDB embedding.

    ``raw`` must carry the ``_cache_key`` / ``_plex_rating_key`` /
    ``_tmdb_id`` / ``_anilist_id`` fields set by ``fetch_and_prepare_raw``.
    Returns the structured profile dict or None on LLM failure.
    """
    cache_key = raw.pop("_cache_key", None)
    plex_rating_key = raw.pop("_plex_rating_key", None) or raw.get("plex_rating_key")
    tmdb_id = raw.pop("_tmdb_id", None) or raw.get("tmdb_id")
    anilist_id = raw.pop("_anilist_id", None) or raw.get("anilist_id")
    # Phase 2 #38a: pop the source-state transport fields BEFORE the LLM
    # call so they don't end up in the LLM prompt. They get re-attached
    # below as PUBLIC profile fields (no underscore), which means the
    # polished cache stores them too — on the next ``_already_enriched``
    # hit the cached profile carries the original source-state forward
    # so ``_write_enrichment_db`` can re-stamp the DB row consistently.
    sources_state = raw.pop("_sources_state", None)
    fetch_tier    = raw.pop("_fetch_tier", None)
    provisional   = raw.pop("_provisional", None)
    media_type = raw.get("media_type", "movie")

    profile = await summarize_with_small_llm(raw)
    if not profile:
        return None

    if media_type == "music":
        profile["source"] = "musicbrainz+lastfm+llm"
    profile["tmdb_id"] = tmdb_id
    profile["anilist_id"] = anilist_id
    profile["plex_rating_key"] = plex_rating_key
    # Pass 99-fu2: tag the polished profile with the current prompt
    # version so a future bump invalidates this cache entry on read.
    profile["prompt_version"] = _PROMPT_VERSION
    # Phase 2 #38a: stamp the source-state + tier onto the profile so
    # ``_write_enrichment_db`` reads them straight off the profile dict
    # (one place, fewer arguments). Only set when present — keeps the
    # profile shape stable for callers that don't go through
    # ``fetch_and_prepare_raw`` (e.g. chat-cascade fast path).
    if sources_state is not None:
        profile["sources_state"] = sources_state
    if fetch_tier is not None:
        profile["fetch_tier"] = fetch_tier
    if provisional is not None:
        profile["provisional"] = provisional

    if cache_key:
        cache = MetadataCache()
        cache.set_cache(cache_key, profile, days=30)
        cache.close()

    # Generate embedding and store in ChromaDB
    try:
        from src.embeddings.embedding_generator import EmbeddingGenerator
        from src.vector_store.chromadb_wrapper import chroma_db

        title = profile.get("title", raw.get("title", ""))
        text_to_embed = profile.get("embedding_text")
        if not text_to_embed and media_type == "music":
            text_to_embed = (
                f"{title} — {', '.join(profile.get('genres', [])[:6])}. "
                f"Tags: {', '.join(profile.get('keywords', [])[:8])}."
            )

        if text_to_embed:
            gen = EmbeddingGenerator()
            vec = await gen.generate_embedding(text_to_embed)
            if vec:
                doc_id = str(plex_rating_key or tmdb_id or anilist_id or title)
                _dom = _domain_for_write(media_type, profile.get("genres"))
                chroma_meta = {
                    "title": title,
                    "media_type": _dom,
                    "domain": _dom,   # hard quarantine key for gated retrieval
                    "genres": ", ".join(profile.get("genres", [])[:6]
                                       if isinstance(profile.get("genres"), list) else []),
                    "themes": ", ".join(profile.get("themes", [])[:6]
                                       if isinstance(profile.get("themes"), list) else []),
                    "mood": ", ".join(profile.get("mood", [])
                                     if isinstance(profile.get("mood"), list) else []),
                    "year": profile.get("year") or 0,
                }
                try:
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[vec],
                        metadatas=[chroma_meta],
                        ids=[doc_id],
                    )
                except Exception:
                    chroma_db.update_metadata(doc_id, chroma_meta)
                # Facet points (multi-vector items, stage 1) — isolated so a
                # facet failure can never break the main index write.
                try:
                    from src.services.facet_index import write_facets
                    await write_facets(doc_id, title, media_type,
                                       chroma_meta["genres"],
                                       profile.get("themes"))
                except Exception as _fe:
                    logger.debug("facet write failed for %r: %s", title, _fe)
            await gen.close()
    except Exception as e:
        logger.debug("ChromaDB store failed for '%s': %s", profile.get("title", "?"), e)

    return profile


# ── MAIN ENRICHMENT ENTRY POINT ───────────────────────────────────────────────

async def enrich_media_item(
    title: str,
    media_type: str = "movie",
    tmdb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    anidb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    mal_id: Optional[int] = None,
    mbid: Optional[str] = None,                 # MusicBrainz artist id (Lidarr) — disambiguates name collisions
    plex_rating_key: Optional[str] = None,
    sonarr_series_type: Optional[str] = None,  # "anime"/"standard"/"daily" from Sonarr
    year: Optional[int] = None,                 # disambiguation hint for title search
    skip_llm_summary: bool = False,             # Pass 14.8: chat-cascade fast path
) -> Optional[dict]:
    """
    Full pipeline for a single media item.
    Looks up MediaIdentity for all known IDs first, then picks
    the best API for each content type:
      - Anime: AniList (anilist_id) > AniDB lookup > title search
      - Movies: TMDB (tmdb_id) > title search
      - Shows: TMDB (tmdb_id) > TVDB > title search

    ``skip_llm_summary`` (Pass 14.8): chat-cascade fast path. When True we
    run all the API fetches (TMDB / AniList / Jikan / OMDB / MusicBrainz)
    but skip the final ``summarize_with_small_llm`` call — that step adds
    3-8 s of LLM-summarisation latency that the chat cascade can't afford
    inside its per-domain timeout. The returned dict still has the fields
    the curator actually needs (title, year, director, cast, plot, genres,
    rating, country) — just without the LLM-polished tone / synopsis tags.
    Cache is bypassed for save (the partial profile would mask future full
    enrichments) but cache lookup still works (full profiles get returned).
    """
    cache = MetadataCache()

    # If we have a plex_rating_key, look up all known IDs from MediaIdentity
    if plex_rating_key and not all([tmdb_id, anilist_id]):
        try:
            from src.database.connection import get_db_session
            from src.database.models import MediaIdentity
            with get_db_session() as db:
                mi = db.query(MediaIdentity).filter(
                    MediaIdentity.plex_rating_key == plex_rating_key
                ).first()
                if mi:
                    tmdb_id    = tmdb_id    or mi.tmdb_id
                    anilist_id = anilist_id or mi.anilist_id
                    anidb_id   = anidb_id   or mi.anidb_id
                    tvdb_id    = tvdb_id    or mi.tvdb_id
                    imdb_id    = imdb_id    or mi.imdb_id
                    # Pass 73: mal_id was written by plex_sync (from Plex Guid
                    # tags) but never pulled here — so the deterministic Jikan
                    # lookup below never fired from the stored ID, it fell back
                    # to a fuzzy title search.
                    mal_id     = mal_id     or mi.mal_id
        except Exception as _e:
            # Pass 43 (B3): MediaIdentity lookup is best-effort; the
            # caller fills missing IDs via direct API calls when this
            # fast-path returns nothing. Log so persistent DB issues
            # don't hide behind the silent swallow.
            logger.debug("[enricher] MediaIdentity merge failed: %s", _e)

    # Cache key: prefer stable IDs, fall back to title
    # Determine if this is anime first — needed for cache key and routing
    is_anime = (
        media_type == "anime"
        or sonarr_series_type == "anime"
        or (sonarr_series_type is None and _looks_like_anime(title))
    )

    id_key = anilist_id or anidb_id or tmdb_id or tvdb_id or title[:40]
    cache_key = f"enriched:{media_type}:{id_key}"
    cached = cache.get_cache(cache_key)
    if cached:
        cached_profile = cached.get("response", {})
        cached_source = cached_profile.get("source", "")
        # Use cache if:
        # 1. Has LLM structuring (source contains '+llm'), OR
        # 2. Cache is fresh (< 7 days old) — avoid re-hitting APIs unnecessarily
        from datetime import datetime
        cached_at_str = cached.get("created_at") or cached.get("cached_at")
        cache_age_days = 999
        if cached_at_str:
            try:
                cached_dt = datetime.fromisoformat(str(cached_at_str))
                cache_age_days = (datetime.utcnow() - cached_dt).days
            except Exception as _e:
                # Pass 43 (B3): timestamp parse fail leaves cache_age_days at
                # the "very old" default — cache miss path takes over.
                logger.debug("[enricher] cached_at parse failed (%r): %s", cached_at_str, _e)
        if "+llm" in cached_source or cached_source == "llm" or cache_age_days < 7:
            cache.close()
            return cached_profile

    # For anime with tvdb_id: resolve via anime-lists crossref database
    # tvdb_id → anidb_id → AniList search (much more reliable than title search)
    if is_anime and tvdb_id and not anilist_id and not anidb_id:
        try:
            from src.services.anime_mapping import get_anime_mapping
            mapping = await get_anime_mapping()
            ids = mapping.lookup_tvdb(tvdb_id)
            if ids.get("anidb_id"):
                anidb_id = ids["anidb_id"]
                logger.debug("Resolved tvdb:%d → anidb:%d via anime-lists", tvdb_id, anidb_id)
            if ids.get("anilist_id"):
                anilist_id = ids["anilist_id"]
                logger.debug("Resolved tvdb:%d → anilist:%d via anime-lists", tvdb_id, anilist_id)
        except Exception as e:
            logger.debug("anime-lists lookup failed: %s", e)

    # ── MUSIC: fetch via MusicBrainz + Last.fm, then LLM-summarize ──────────
    if media_type == "music":
        from src.services.music_metadata import enrich_artist
        artist_raw = await enrich_artist(title, mbid=mbid)
        if not artist_raw:
            cache.close()
            return None
        artist_raw["title"] = artist_raw.get("name", title)
        artist_raw["media_type"] = "music"
        artist_raw["source"] = "musicbrainz+lastfm"

        # Pass 14.12: chat cascade fast-path also for music (was missing in
        # Pass 14.8 — that pass only fixed the movie/tv/anime branch). Music
        # lookups for "King Crimson" / "Sleep Token" hit MusicBrainz +
        # Last.fm in <1s but the subsequent summarize_with_small_llm pushes
        # total past the 10s cascade timeout. Skip-mode returns the raw
        # MusicBrainz/Last.fm profile directly with the curator-relevant
        # fields.
        if skip_llm_summary:
            cache.close()
            return {
                "title":           artist_raw.get("title") or title,
                "name":            artist_raw.get("name") or title,
                "year":            None,
                "media_type":      "music",
                "genres":          artist_raw.get("genres") or [],
                "country":         artist_raw.get("country") or "",
                "active_years":    artist_raw.get("active_years") or "",
                "similar_artists": artist_raw.get("similar_artists") or [],
                "top_albums":      artist_raw.get("top_albums") or [],
                "tags":            artist_raw.get("tags") or [],
                "bio":             artist_raw.get("bio") or "",
                "plot_summary":    artist_raw.get("bio") or "",
                "rating":          artist_raw.get("rating"),
                "source":          "musicbrainz+lastfm+raw",
            }

        # Run through LLM summarizer for structured genres/themes/mood/embedding_text
        profile = await summarize_with_small_llm(artist_raw)
        if not profile:
            cache.close()
            return None
        profile["source"] = "musicbrainz+lastfm+llm"
        profile["plex_rating_key"] = plex_rating_key
        profile["prompt_version"] = _PROMPT_VERSION   # Pass 99-fu2
        cache.set_cache(cache_key, profile, days=30)
        # Generate embedding and store in ChromaDB
        try:
            from src.embeddings.embedding_generator import EmbeddingGenerator
            from src.vector_store.chromadb_wrapper import chroma_db
            text_to_embed = profile.get("embedding_text") or (
                f"{title} — {', '.join(profile.get('genres', [])[:6])}. "
                f"Tags: {', '.join(profile.get('keywords', [])[:8])}."
            )
            gen = EmbeddingGenerator()
            vec = await gen.generate_embedding(text_to_embed)
            if vec:
                doc_id = str(plex_rating_key or title)
                chroma_meta = {
                    "title": title,
                    "media_type": "music",
                    "domain": "music",      # hard quarantine key for gated retrieval
                    "genres": ", ".join(profile.get("genres", [])[:6]),
                    "themes": ", ".join(profile.get("themes", [])[:6]),
                    "mood": ", ".join(profile.get("mood", [])),
                    "year": 0,
                }
                try:
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[vec],
                        metadatas=[chroma_meta],
                        ids=[doc_id],
                    )
                except Exception:
                    chroma_db.update_metadata(doc_id, chroma_meta)
                try:
                    from src.services.facet_index import write_facets
                    await write_facets(doc_id, title, "music",
                                       chroma_meta["genres"],
                                       profile.get("themes"))
                except Exception as _fe:
                    logger.debug("facet write failed for %r: %s", title, _fe)
            await gen.close()
        except Exception as e:
            logger.debug("Music ChromaDB store failed for '%s': %s", title, e)
        cache.close()
        return profile

    raw = None
    # is_anime already defined above

    # 1. Fetch raw metadata — priority by ID quality, then content type
    if anilist_id:
        # Best case: direct AniList ID
        raw = await fetch_anilist_full(anilist_id)
    elif anidb_id and is_anime:
        # AniDB ID available — search AniList by title (no direct AniDB→AniList API)
        raw = await search_anilist_by_title(title, year=year)
        if raw:
            raw["anidb_id"] = anidb_id
            # Store discovered AniList ID back to mapping for future lookups
            if raw.get("id") and anidb_id:
                try:
                    from src.services.anime_mapping import update_anilist_id
                    update_anilist_id(anidb_id, raw["id"])
                except Exception as _e:
                    # Pass 43 (B3): anime-mapping update is best-effort —
                    # log so failures don't silently rot the cross-ref DB.
                    logger.debug("[enricher] update_anilist_id(%s) failed: %s", anidb_id, _e)
    elif tmdb_id and media_type == "movie":
        # Movie with TMDB ID — direct fetch
        raw = await fetch_tmdb_full(tmdb_id, "movie")
    elif tmdb_id and not is_anime:
        # TV series with TMDB ID — direct fetch
        raw = await fetch_tmdb_full(tmdb_id, "tv")
        # Check if it's actually anime (Sonarr may mis-classify)
        if raw and "Animation" in raw.get("genres", []) and _looks_like_anime(title):
            al = await search_anilist_by_title(title, year=year)
            if al:
                al["cast"] = raw.get("cast", [])
                al["similar_titles"] = al.get("similar_titles") or raw.get("similar_titles", [])
                raw = al
    elif is_anime:
        # Anime without direct ID — AniList title search
        raw = await search_anilist_by_title(title, year=year)
        if not raw and tmdb_id:
            raw = await fetch_tmdb_full(tmdb_id, "tv")
        if not raw:
            raw = await _tmdb_search_and_fetch(title, "tv", year=year)
    elif imdb_id:
        # Have IMDb ID — TMDB can find by external ID
        raw = await _tmdb_fetch_by_external_id(imdb_id, media_type)
        if not raw:
            endpoint = "movie" if media_type == "movie" else "tv"
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    elif tvdb_id:
        # Have TVDB ID — use IMDb search if available, else title search
        endpoint = "movie" if media_type == "movie" else "tv"
        raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    else:
        # Last resort: title search
        endpoint = "movie" if media_type in ("movie",) else "tv"
        if is_anime:
            raw = await search_anilist_by_title(title, year=year)
        if not raw:
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)

    if not raw:
        cache.close()
        return None

    # Entity-resolution safety net (Fix A): the arr gave a year, but the resolved
    # entry's year is wildly off (>5y) → we matched a DIFFERENT same-named work
    # (Lupin III → the 2012 spin-off; or a stale/wrong arr tmdb_id pointing at the
    # wrong film). Enrich NOTHING rather than feed the curator confidently-wrong
    # metadata — it falls back to the arr synopsis. Small gaps (release vs
    # first-air year) are tolerated.
    try:
        _ry, _ty = int(raw.get("year") or 0), int(year or 0)
    except (TypeError, ValueError):
        _ry = _ty = 0
    if _ty and _ry and abs(_ry - _ty) > 5:
        logger.warning(
            "[enricher] resolution YEAR MISMATCH for %r: arr=%s resolved=%s (%r) "
            "— rejecting wrong-entity match",
            title, _ty, _ry, raw.get("title"),
        )
        cache.close()
        return None

    # 1b. Supplement with additional sources
    supplements = []

    if is_anime:
        # Get MAL ID from raw data if AniList stored it
        mal_id_val = mal_id or raw.get("mal_id")
        await asyncio.sleep(0.3)
        jikan_data = await fetch_jikan_data(
            mal_id=mal_id_val,
            title=None if mal_id_val else title,
        )
        if jikan_data:
            supplements.append(jikan_data)
            if jikan_data.get("mal_id") and not raw.get("mal_id"):
                raw["mal_id"] = jikan_data["mal_id"]
        elif not jikan_data:
            # No Jikan data — derive basic tone hints from what we have
            genres = raw.get("genres", [])
            tags = raw.get("tags", raw.get("keywords", []))
            tone_hints = []
            if "Comedy" in genres and "Horror" not in genres and "Psychological" not in genres:
                if any(t in tags for t in ["Ecchi", "Harem", "ecchi", "harem"]):
                    tone_hints.append("Ecchi comedy — primarily comedic/lighthearted")
                else:
                    tone_hints.append("Comedy is primary genre")
            if tone_hints:
                raw["tone_hints"] = " | ".join(tone_hints)
    else:
        imdb_id_val = raw.get("imdb_id") or imdb_id
        if imdb_id_val:
            omdb_data = await fetch_omdb_data(imdb_id_val)
            if omdb_data:
                supplements.append(omdb_data)

    # Merge all sources into enriched raw dict
    if supplements:
        raw = _merge_raw_metadata(raw, *supplements)

    raw["plex_rating_key"] = plex_rating_key

    # Pass 14.8 fast-path: skip LLM summarisation when called from the chat
    # cascade. We hand back a raw-derived profile with the curator-relevant
    # fields and DON'T cache (the partial profile would mask later full
    # enrichments). Cache misses repeat the API fetches but the API layer is
    # fast (1-3 s); the LLM step (3-8 s) was the timeout-killer.
    if skip_llm_summary:
        cache.close()
        fast_profile = {
            "title":         raw.get("title") or title,
            "original_title": raw.get("original_title") or "",
            "year":          raw.get("year"),
            "media_type":    raw.get("media_type") or media_type,
            "genres":        raw.get("genres") or [],
            "rating":        raw.get("rating"),
            "vote_count":    raw.get("vote_count"),
            "director":      raw.get("director") or "",
            "cast":          raw.get("cast") or [],
            "country":       raw.get("country") or "",
            "runtime":       raw.get("runtime"),
            "studios":       raw.get("studios") or raw.get("studio") or "",
            "episodes_total": raw.get("episodes_total") or raw.get("episodes"),
            "source_material": raw.get("source_material") or "",  # audit 11a: raw["source"] is PROVENANCE (tmdb/anilist), not source material
            "plot_summary":  raw.get("overview_extended") or raw.get("overview") or "",
            "tmdb_id":       tmdb_id or raw.get("tmdb_id"),
            "anilist_id":    anilist_id or raw.get("anilist_id"),
            "source":        f"{raw.get('source', 'api')}+raw",
        }
        return fast_profile

    # 2. Summarize with small LLM
    profile = await summarize_with_small_llm(raw)
    if not profile:
        cache.close()
        return None

    # Merge in IDs
    profile["tmdb_id"] = tmdb_id or raw.get("tmdb_id")
    profile["anilist_id"] = anilist_id or raw.get("anilist_id")
    profile["plex_rating_key"] = plex_rating_key
    profile["prompt_version"] = _PROMPT_VERSION   # Pass 99-fu2

# 3. Cache result (30 days)
    cache.set_cache(cache_key, profile, days=30)

    # ─────────────────────────────────────────────────────────────────────────
    # 4 & 5. EMBEDDING GENERIEREN UND IN CHROMADB SPEICHERN
    # ─────────────────────────────────────────────────────────────────────────
    try:
        from src.embeddings.embedding_generator import EmbeddingGenerator
        from src.vector_store.chromadb_wrapper import chroma_db
        
        text_to_embed = profile.get("embedding_text")
        
        if text_to_embed:
            # Generate the vector with nomic-embed-text
            generator = EmbeddingGenerator()
            embedding_vector = await generator.generate_embedding(text_to_embed)

            if embedding_vector:
                # Build the metadata payload for ChromaDB search
                _dom = _domain_for_write(profile.get("media_type", "movie"),
                                         profile.get("genres"))
                chroma_metadata = {
                    "title": profile.get("title", ""),
                    "media_type": _dom,
                    "domain": _dom,  # hard quarantine key for gated retrieval
                    "genres": ", ".join(profile.get("genres", [])),
                    "themes": ", ".join(profile.get("themes", [])),
                    "mood": ", ".join(profile.get("mood", [])),
                    "year": profile.get("year") or 0
                }

                # Pick a stable unique ID for the document
                doc_id = str(plex_rating_key or tmdb_id or anilist_id or profile["title"])

                # In ChromaDB upserten — duplicate-id raises on `add`, so we
                # fall back to deleting + re-adding to refresh the embedding
                # too (update_metadata alone leaves a stale vector).
                try:
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[embedding_vector],
                        metadatas=[chroma_metadata],
                        ids=[doc_id]
                    )
                except Exception as add_exc:
                    logger.debug("ChromaDB add failed for '%s' (%s) — re-adding",
                                 doc_id, add_exc)
                    try:
                        chroma_db.delete_by_id(doc_id)
                    except Exception as _e:
                        # Pass 47 (B3-rest): a silent failure here masks vector-
                        # consistency problems — we'd re-add over the existing
                        # doc but the old vector might still leak through if
                        # delete_by_id partially succeeded. Log loudly enough
                        # to spot but don't escalate (re-add usually wins).
                        logger.warning(
                            "[enricher] chroma delete_by_id failed before re-add (%s): %s",
                            doc_id, _e,
                        )
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[embedding_vector],
                        metadatas=[chroma_metadata],
                        ids=[doc_id]
                    )
                try:
                    from src.services.facet_index import write_facets
                    await write_facets(doc_id, profile.get("title", ""),
                                       chroma_metadata.get("domain", ""),
                                       chroma_metadata.get("genres", ""),
                                       profile.get("themes"))
                except Exception as _fe:
                    logger.debug("facet write failed for %r: %s",
                                 profile.get("title"), _fe)
                logger.debug("Successfully stored '%s' in ChromaDB.", profile.get("title"))

            try:
                await generator.close()
            except Exception as _e:
                # Pass 47 (B3-rest): generator close is best-effort cleanup;
                # logging at debug surfaces leak-pattern bugs without
                # spamming the happy-path log.
                logger.debug("[enricher] generator close on happy path failed: %s", _e)
    except Exception as e:
        logger.error("Failed to store '%s' in ChromaDB: %s", profile.get("title", "?"), e)
        # Best-effort: ensure we don't leak the embedding generator on failure.
        try:
            await generator.close()
        except Exception as _e:
            # Pass 43 (B3): close-on-error is itself error-prone (HTTP client
            # already-closed, transport gone). Log at debug — a generator
            # leak is benign at process scope but worth seeing if it spikes.
            logger.debug("[enricher] generator close on error failed: %s", _e)

    cache.close()
    return profile


# ── HELPERS ───────────────────────────────────────────────────────────────────

ANIME_HINTS = {
    # Japanese name patterns or common suffixes/words
    "no", "wa", "ga", "wo", "ni", "de", "na", "to", "ka",  # particles
    "shonen", "shounen", "seinen", "shoujo", "isekai", "mecha",
    "nakama", "senpai", "sensei", "chan", "kun", "san", "sama",
    "hentai", "ecchi", "kawaii",
}

def _looks_like_anime(title: str) -> bool:
    """Heuristic: does this title look like it might be anime?"""
    if not title:
        return False
    lower = title.lower()
    words = set(lower.split())
    if words & ANIME_HINTS:
        return True
    # Colon-notation like "Re:Zero" or "No Game:No Life" (no space around colon)
    # — but not normal subtitle patterns like "Captain America: The First Avenger"
    if re.search(r"\w:\w", title):
        return True
    return False


_MATCH_STOPS = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "no"}
_CLEAN_WORDS_RE = re.compile(r"[^a-z0-9 ]")

def _title_match_score(query: str, found: str) -> float:
    """
    Word-overlap similarity between two titles, normalized 0-1.
    Ignores common stop-words and punctuation.

    Performance note: Re-compiling the regex and instantiating the set inside the inner
    function takes ~25% longer. Hoisting to the module level speeds up matching algorithms
    which execute this heavily in nested loops.
    """
    if not query or not found:
        return 0.0

    def _words(s: str) -> set:
        return set(_CLEAN_WORDS_RE.sub(" ", s.lower()).split()) - _MATCH_STOPS

    q_words = _words(query)
    f_words = _words(found)
    if not q_words or not f_words:
        return 0.0
    overlap = len(q_words & f_words)
    return overlap / max(len(q_words), len(f_words))


def _candidate_matches(
    query: str,
    query_clean: str,
    is_short_query: bool,
    query_word_count: int,
    candidate: str,
    threshold: float
) -> bool:
    if not candidate:
        return False
    c_clean = candidate.lower().strip()

    # 1. Exact match — always wins
    if query_clean == c_clean:
        return True

    # Short-query guard: skip substring + fuzzy paths entirely
    if is_short_query:
        return False

    # Colon-suffix guard: candidate is "<query>: <subtitle>" → not a match
    if ":" in c_clean:
        base = c_clean.split(":", 1)[0].strip()
        if base == query_clean:
            # "King Crimson" vs "King Crimson: Deja VROOOM" — reject
            return False

    # Pass 15.2 single-word-query guard: when the query is a single word,
    # the substring must appear at the START of the candidate (followed
    # by a word boundary). Otherwise "Ghosts" matches "Inner Ghosts",
    # "Old Ghosts", "Ghost Story" — fuzzy noise that lets the wrong
    # title win the slot.
    if query_word_count == 1 and query_clean in c_clean:
        if not (
            c_clean.startswith(query_clean + " ")
            or c_clean.startswith(query_clean + ":")
            or c_clean.startswith(query_clean + "(")
        ):
            # Query word appears mid-candidate, not at start → reject.
            # Skip to the fuzzy-score path (which has its own threshold)
            # rather than falling through to the lax substring check.
            if _title_match_score(query, candidate) >= threshold:
                return True
            return False

    # 2. Substring match with a length check
    # Stops a 1-word query like "Jesus" from matching the 8-word title
    # "Jesus Shows You the Way to the Highway".
    if query_clean in c_clean or c_clean in query_clean:
        q_len = len(query_clean.split())
        c_len = len(c_clean.split())
        # Only allow the match when the titles are close in word count,
        # or when the fuzzy match score is high enough to carry it anyway.
        if abs(q_len - c_len) <= 2:
            return True

    # 3. Fuzzy word-overlap (the established score)
    if _title_match_score(query, candidate) >= threshold:
        return True

    return False


def _titles_close_enough(query: str, candidates: list[str], threshold: float = 0.6) -> bool:
    """Sharper matcher that prevents false positives on long titles.

    Short-query guard (Pass 14.4): when the query is ≤ 4 characters
    (e.g. "It", "Up", "Big"), require an EXACT match. Substring-matching
    on 2-3 character words produces noise — "It" matches "Strike It Rich",
    "Big Little Lies", "Bring It On". The cure is worse than the disease
    so for tiny queries we just trust the exact-match path.

    Colon-suffix guard (Pass 14.10): "King Crimson" must NOT match
    "King Crimson: Deja VROOOM". A colon followed by extra text marks the
    candidate as a *specific* sub-item (live concert, special edition,
    season title, …) — if the user typed only the bare name, they meant
    the band/series, not the spin-off. Reject the substring match for
    these cases.
    """
    if not query:
        return False

    query_clean = query.lower().strip()
    is_short_query = len(query_clean) <= 4
    query_word_count = len(query_clean.split())

    for c in candidates:
        if _candidate_matches(
            query=query,
            query_clean=query_clean,
            is_short_query=is_short_query,
            query_word_count=query_word_count,
            candidate=c,
            threshold=threshold
        ):
            return True

    return False


ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      synonyms
      description(asHtml: false)
      genres
      tags { name rank isMediaSpoiler }
      averageScore meanScore popularity
      episodes duration
      startDate { year }
      studios(isMain: true) { nodes { name } }
      staff(sort: RELEVANCE) { edges { role node { name { full } } } }
      recommendations(sort: RATING_DESC) { nodes { mediaRecommendation { title { romaji english } } } }
    }
  }
}
"""


async def search_anilist_by_title(title: str, year: Optional[int] = None) -> Optional[dict]:
    """Search AniList by title, iterate up to 5 candidates, return first close
    match (or, when a ``year`` hint is given, the closest-year close match) or
    None."""
    try:
        await _anilist_wait()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": title}},
            )
            if r.status_code == 429:
                wait_s = _anilist_set_backoff(r.headers)
                logger.info("AniList 429 for '%s' — skipping (backed off %.0fs)", title, wait_s)
                return None
            if r.status_code != 200:
                return None
            media_list = r.json().get("data", {}).get("Page", {}).get("media") or []
            if not media_list:
                return None
    except Exception as e:
        logger.debug("AniList search '%s' error: %s", title, e)
        return None

    # Iterate all returned candidates and pick the first one whose title is
    # close enough to the query.  Using a Page query (5 results) instead of
    # a single Media result means we don't blindly accept the top-ranked hit
    # when it doesn't actually match (e.g. 'Futurama' → random mouse anime,
    # or 'Golden Boy' → 'Golden Kamuy').
    # Collect ALL close-enough candidates, then — when we have a year hint —
    # prefer the one whose start year matches. Without this, the FIRST close
    # title won: "Lupin III" matched "…The Woman Called Fujiko Mine" (2012)
    # instead of the franchise entry the arr actually holds. Year breaks the tie.
    close = []
    for candidate in media_list:
        # Pass 56: ``or {}`` — a present-but-null "title" would otherwise
        # slip past the .get default and crash on ct.get(...) below.
        ct = candidate.get("title") or {}
        candidate_titles = [
            ct.get("english") or "",
            ct.get("romaji") or "",
            ct.get("native") or "",
        ] + (candidate.get("synonyms") or [])
        if _titles_close_enough(title, candidate_titles):
            close.append(candidate)

    media = None
    if close:
        if year:
            best = min(close, key=lambda c: abs(
                ((c.get("startDate") or {}).get("year") or 9999) - year))
            by = (best.get("startDate") or {}).get("year")
            if by and abs(by - year) <= 2:
                media = best
        if media is None:
            media = close[0]   # no year hint / no year-match → first close title
    found_title = title
    if media:
        ct = media.get("title") or {}
        found_title = ct.get("english") or ct.get("romaji") or title

    if not media:
        logger.debug(
            "AniList search '%s' → no close match in top %d results",
            title, len(media_list),
        )
        return None
    tags = [
        t["name"] for t in sorted(
            [t for t in (media.get("tags") or []) if not t.get("isMediaSpoiler")],
            key=lambda t: t.get("rank", 0), reverse=True
        )[:15]
    ]
    director = None
    for edge in (media.get("staff", {}).get("edges") or []):
        if "Director" in (edge.get("role") or ""):
            director = edge["node"]["name"]["full"]
            break
    similar = []
    for node in (media.get("recommendations", {}).get("nodes") or [])[:8]:
        # Pass 56: ``or {}`` (not a .get default) — AniList may send
        # mediaRecommendation: null or title: null on sparse entries.
        rec = node.get("mediaRecommendation") or {}
        t = rec.get("title") or {}
        name = t.get("english") or t.get("romaji", "")
        if name:
            similar.append(name)
    studios = [s["name"] for s in (media.get("studios", {}).get("nodes") or [])]

    return {
        "anilist_id": media["id"],
        "media_type": "anime",
        "title": found_title,
        "year": (media.get("startDate") or {}).get("year"),
        "overview": (media.get("description") or "").replace("<br>", "\n")[:1000],
        "genres": media.get("genres", []),
        "tags": tags,
        "director": director,
        "studios": studios,
        # Pass 54: averageScore is AniList's weighted score — it stays null
        # for fresh / niche titles that haven't cleared the confidence
        # threshold yet. meanScore (plain average) is populated earlier, so
        # fall back to it before giving up. Either way it's a real user
        # rating; 0 only if AniList genuinely has neither.
        "rating": (media.get("averageScore") or media.get("meanScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


async def _tmdb_search_and_fetch(
    title: str,
    endpoint: str,
    year: Optional[int] = None,
) -> Optional[dict]:
    """Search TMDB by title (with optional year hint) and fetch full details.

    The ``year`` hint disambiguates same-name titles released in different
    years (e.g. *Jesus Shows You the Way to the Highway* — 2019 surreal
    sci-fi vs. *Jesus' Son* — 1999 drama). When set, we ask TMDB to filter
    by primary release year first; if that returns nothing, we fall back to
    the unfiltered search and prefer the result whose release year matches
    the hint.
    """
    if not settings.TMDB_API_KEY:
        return None

    params: dict = {"query": title}
    if year:
        # TMDB uses different param names per endpoint
        params["year" if endpoint == "movie" else "first_air_date_year"] = year

    async with httpx.AsyncClient(timeout=10) as client:
        r = await _tmdb_get(client, f"/search/{endpoint}", params)
    results = r.get("results", [])

    # Year-filtered search came back empty? Retry without the year filter and
    # prefer year-matching candidates manually.
    if not results and year:
        logger.debug("TMDB search '%s' year=%d → 0 results, retrying without year",
                     title, year)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await _tmdb_get(client, f"/search/{endpoint}", {"query": title})
        results = r.get("results", [])

    if not results:
        return None

    # Find best-matching result rather than blindly taking the first
    title_key = "title" if endpoint == "movie" else "name"
    alt_key = "original_title" if endpoint == "movie" else "original_name"
    date_key = "release_date" if endpoint == "movie" else "first_air_date"

    def _result_year(res: dict) -> Optional[int]:
        d = (res.get(date_key) or "")[:4]
        return int(d) if d.isdigit() else None

    # Pass 1: prefer a year-matching candidate when we have a hint
    found_id = None
    if year:
        for result in results[:5]:
            candidates = [result.get(title_key, ""), result.get(alt_key, "")]
            if _titles_close_enough(title, candidates) and _result_year(result) == year:
                found_id = result.get("id")
                logger.info("TMDB '%s' → year-exact match id=%s (%d)",
                            title, found_id, year)
                break

    # Pass 2: best title match regardless of year
    if not found_id:
        for result in results[:5]:
            candidates = [result.get(title_key, ""), result.get(alt_key, "")]
            if _titles_close_enough(title, candidates):
                found_id = result.get("id")
                if year and _result_year(result) and _result_year(result) != year:
                    logger.warning(
                        "TMDB '%s' → title match id=%s but year mismatch "
                        "(hint=%d, found=%d) — using anyway",
                        title, found_id, year, _result_year(result),
                    )
                break

    if not found_id:
        logger.debug("TMDB search '%s' → no close match in top 5 results, skipping", title)
        return None

    media_type = "movie" if endpoint == "movie" else "tv"
    return await fetch_tmdb_full(found_id, media_type)


async def _tmdb_fetch_by_external_id(imdb_id: str, media_type: str) -> Optional[dict]:
    """Fetch TMDB item using an external IMDb ID — more reliable than title search."""
    if not settings.TMDB_API_KEY or not imdb_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await _tmdb_get(client, f"/find/{imdb_id}",
                                {"external_source": "imdb_id"})
        results = (
            r.get("movie_results", []) if media_type == "movie"
            else r.get("tv_results", [])
        )
        if not results:
            # Try the other type
            results = r.get("tv_results", []) if media_type == "movie" else r.get("movie_results", [])
        if not results:
            return None
        found_id = results[0].get("id")
        fetch_type = "movie" if media_type == "movie" else "tv"
        return await fetch_tmdb_full(found_id, fetch_type)
    except Exception as e:
        logger.debug("TMDB external ID lookup failed for %s: %s", imdb_id, e)
        return None
