"""
Curatarr - Recommendations Engine

Uses taste vectors + LLM to generate personalised recommendations
with a written pitch per item, organised by category.

Features:
  - Cache persistence: results are stored in the DB.
  - Force refresh: manual regeneration is available via the UI.
  - Context-aware recommendations from the library or discovery feed.
"""

import asyncio
import json
import logging
import math
from datetime import datetime
from typing import Optional

import httpx
import numpy as np

from src.config import settings
from src.services.ollama_hosts import host_for
from src.services import judge_record
from src.services.pc_guard import ollama_client
from src.services.llm_utils import clean_llm_text, strip_think_tags, ollama_options, curator_options, CURATOR_KEEP_ALIVE, seasonal_context
from src.database.connection import get_db_session
from src.database.runner import run_read, run_write
from src.services.episodic_memory import retrieve_memories, format_memories_for_context
from src.database.models import (
    TasteVectorEntry, User,
    EncryptedTasteVector, ProtectedMedia, EpisodicMemory
)
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)


# A title the curator wrote about, set apart from its own prose: emphasis
# or quotes around it (**Her**, "Big"), or a title of several words as a
# whole phrase in its own capitalisation.
_TITLE_MARK = r"(?:\*\*|\*|_|\"|“|”|'|‘|’)"


def positioned_titles(text: str, titles) -> set:
    """The titles the curator took a position on in ``text`` (its recent
    replies). A plain lower-case substring test exempted every short title
    from deletion: "Her" is in every "other", "IF" in every "if", "10" in
    any number, "Back to the Future Part II" in "Part III" - 458 titles on
    the owner's real 60 days of chat (2026-10-25), 116 with this rule.
    A one-word or very short title counts only when it is emphasised or
    quoted; a longer one as a whole phrase in its own capitalisation."""
    import re
    low = text.lower()
    out = set()
    for t in titles:
        t = (t or "").strip()
        if not t or t.lower() not in low:      # the cheap test first
            continue
        if " " not in t or len(t) <= 3:
            pat = _TITLE_MARK + re.escape(t) + _TITLE_MARK
        else:
            pat = r"(?<![\w'])" + re.escape(t) + r"(?![\w'])"
        if re.search(pat, text):
            out.add(t)
    return out


def _chroma_get_by_ids(ids: list) -> dict:
    """Embeddings by id, for run_chroma. The wrapper is looked up here, on
    the Chroma thread: importing this module no longer builds the client."""
    from src.vector_store.chromadb_wrapper import get_chroma_db
    return get_chroma_db().get_by_ids(ids)


# Pass 42 (A4): single source of truth for the LLM-forbidden phrases.
# Previously this list lived inline in three different prompt strings
# (recs pitch / external discovery / deletion pitch) and the three
# slowly drifted — Pass 39 extended the deletion-pitch blacklist with
# new crutch phrases the code review surfaced, but the recommendation
# pitch and discovery prompts kept the old shorter list, so banned
# words still leaked into other surfaces. One constant, one builder,
# three uses below.
BLACKLISTED_PITCH_PHRASES: tuple[str, ...] = (
    # Generic AI clichés
    "high-octane", "mind-bending", "cyber-rebellion", "adrenaline",
    "edge-of-your-seat", "relentless", "masterpiece", "epic",
    "rhythmic propulsion", "kinetic precision",
    # Curator-coined hooks that became repetitive over many pitches
    "sakuga density", "narrative propulsion", "lore economy",
    "exposition bloat", "structural discipline", "thematic depth",
    "tonal register",
)


def _blacklist_rule(rule_number: int) -> str:
    """Format the BLACKLIST rule line for a numbered-rule prompt template."""
    joined = ", ".join(f"'{p}'" for p in BLACKLISTED_PITCH_PHRASES)
    return (
        f"{rule_number}. UNIVERSAL BLACKLIST: You are STRICTLY FORBIDDEN from using "
        f"any of these crutch phrases: {joined}."
    )


# ── Anti-tunneling: domain-specific vocabulary guidelines ─────────────────────

_VOCAB_GUIDELINES: dict[str, str] = {
    "documentary": (
        "DOMAIN VOCABULARY — Documentary: Focus on information density, research depth, "
        "pedagogical value, talking-head fatigue, and the insight-to-runtime ratio. "
        "Critique whether the film earns its runtime with genuine revelation or recycles "
        "surface-level takes. Use terms like 'archival depth', 'editorial discipline', "
        "'journalistic rigour', 'expository pacing'."
    ),
    "music": (
        "DOMAIN VOCABULARY — Music: You have the artist's GENRE, scene, themes and "
        "mood tags — NOT the audio. Do NOT judge timbre, mix, production, dynamics, "
        "compression or 'sheen': you cannot hear them, so any such claim is invented. "
        "Critique instead from what the tags tell you — genre/scene fit against the "
        "user's core sound, emotional register, cultural/era placement, catalogue "
        "depth vs novelty, replay value, and whether the library already covers this "
        "lane — and whether they still earn shelf space."
    ),
    "spoken_word": (
        "DOMAIN VOCABULARY — Spoken word / cabaret: this is a LANGUAGE performer "
        "stored in the music library (Kabarett, Hörbuch, comedy readings, poetry). "
        "Music metrics — rhythm patterns, sonic friction, drops, production, and "
        "mismatch with the user's core music sound — do NOT apply and are not "
        "evidence against it. Judge the linguistic craft: rhetorical density, "
        "thematic bite, precision of delivery, and the replay value of the writing."
    ),
    "anime": (
        "DOMAIN VOCABULARY — Anime: Focus on worldbuilding coherence, animation fluidity "
        "versus static frames, trope fatigue, narrative propulsion, and pacing discipline. "
        "Use terms like 'sakuga density', 'lore economy', 'arc coherence', "
        "'tonal register', 'exposition-to-action ratio'."
    ),
    "show": (
        "DOMAIN VOCABULARY — TV Show: Focus on season-over-season quality decay, "
        "character-arc consistency, dialogue density, premise exhaustion, and whether "
        "the show still has narrative runway. Use terms like 'serialisation discipline', "
        "'character regression', 'procedural fatigue', 'showrunner vision'."
    ),
    "movie": (
        "DOMAIN VOCABULARY — Film: Focus on cinematography, character arcs, narrative "
        "economy, tonal consistency, and structural integrity. Use terms like "
        "'mise-en-scène', 'third-act collapse', 'tonal whiplash', 'screenplay economy', "
        "'directorial voice'."
    ),
}

# Spoken-word detection for music items. Deliberately NOT bare "comedy":
# musical comedy acts are still music; these markers name the language forms.
_SPOKEN_WORD_MARKERS = ("kabarett", "cabaret", "spoken word", "audiobook",
                        "hörbuch", "hörspiel", "lesung", "poetry")


def _is_spoken_word(genres_lower: str) -> bool:
    """True when a music item's genres mark it as a language work (the
    Malmsheimer case: a 174-play Kabarett artist judged — and pitched for
    deletion — with music metrics against the owner's electronic sound)."""
    return any(m in genres_lower for m in _SPOKEN_WORD_MARKERS)


_FORBIDDEN_CROSS_DOMAIN = (
    "STRICTLY FORBIDDEN CROSS-DOMAIN TERMS: Never use music-related terms "
    "(rhythmic, sonic, synthetic, frequency, beat, melody, tempo, harmonic) "
    "when critiquing films, documentaries, or TV shows unless the content is "
    "explicitly about music. Never use film-specific jargon (cinematography, "
    "mise-en-scène, frame composition) when critiquing music or anime."
)

# Injected into a pitch / discussion ONLY when there is no verified-data block
# (the item isn't enriched and the on-demand fast-enrich couldn't resolve it —
# the curator has the synopsis but no verified enrichment). Without this the
# curator confabulates confident execution verdicts on zero data and dismisses
# the user's own signals as "noise" (the Fringe case: trashed an 8.4 show with
# invented "procedural fatigue / case-of-the-week" critique it could not know).
# It must hedge INTERNALLY (caution + lean-keep) — NOT narrate a data-gap excuse
# at the user. The old version scripted "I only have a one-line synopsis here"
# as the opener; it fired on every un-enriched title and was usually FALSE (the
# full synopsis is right there in the prompt — only the OMDb/Wikipedia
# enrichment is missing), so it read as a constant, inaccurate excuse.
NO_VERIFIED_DATA_HEDGE = (
    "⚠️ LIMITED DATA: you have the SYNOPSIS but no *verified* enrichment for this "
    "title (no confirmed themes, significance, critical reception, cast or "
    "production facts — it isn't enriched yet). Therefore:\n"
    "- Judge ONLY what the synopsis actually shows: premise, genre, and taste "
    "fit. Do NOT invent execution verdicts you cannot know from a synopsis — "
    "pacing, 'case of the week', 'formulaic', 'dated', 'procedural fatigue', "
    "collapse of vision, or how well it is made.\n"
    "- Do NOT dismiss the user's external signals (an IMDb/TMDB rating, their own "
    "knowledge) as 'noise' or 'irrelevant'. Without verified data of your own, "
    "THEIR signal outweighs your inference — engage it honestly.\n"
    "- Stay genuinely low-confidence and lean toward KEEPING / deferring rather "
    "than pressing for deletion.\n"
    "- Do NOT open with or pad the pitch with a disclaimer about your data gap "
    "('I only have a one-line synopsis…'). The user does not need it — let the "
    "uncertainty show in measured wording, not an excuse."
)


def _get_vocab_guideline(category: str, genres_str: str) -> str:
    """
    Return exactly one domain-specific vocabulary guideline for this item.
    Only this single string enters the prompt — not the full dict.

    Priority order mirrors your select_guideline logic:
      1. Music (category beats genre — music is never misclassified as doco)
      2. Documentary (genre tag, case-insensitive)
      3. Anime (category OR genre tag — Sonarr sometimes sets category=show)
      4. Show
      5. Movie (default)
    """
    genres_lower = (genres_str or "").lower()

    if category == "music":
        if _is_spoken_word(genres_lower):
            return _VOCAB_GUIDELINES["spoken_word"]
        return _VOCAB_GUIDELINES["music"]
    if "documentary" in genres_lower or "docuseries" in genres_lower:
        return _VOCAB_GUIDELINES["documentary"]
    if category == "anime" or "anime" in genres_lower:
        return _VOCAB_GUIDELINES["anime"]
    if category == "show":
        return _VOCAB_GUIDELINES["show"]
    return _VOCAB_GUIDELINES["movie"]


def _taste_section(summary_text: str, category: str) -> str:
    """Pick the taste-summary section matching this deletion category.

    The taste summary is authored as ``[MOVIE] … [SHOW] … [ANIME] … [MUSIC] …``
    blocks (compute_all_taste_vectors summarises all four when each has >=5
    entries — music included, e.g. "electro-house and hardstyle, German rap's
    incisive lyricism"). The old code took ``summary_text[:400]``, which ALWAYS
    returned the [MOVIE] block — so anime/show/music pitches were judged against
    the user's FILM taste ("cerebral tension", "psychological thrillers"), not
    their actual taste in the medium (the Skate-Leading pitch critiquing it for
    lacking "cerebral dissonance" — a film yardstick). This returns the block
    matching THIS category. If it's genuinely absent (a user with <5 music plays,
    or one who hasn't recomputed since music was added), we return "" — never the
    film blurb — so the pitch falls back to taste-free framing, not a wrong domain.
    """
    import re
    s = summary_text or ""
    if not s:
        return ""
    tag = {"movie": "MOVIE", "show": "SHOW", "anime": "ANIME",
           "music": "MUSIC"}.get(category, (category or "").upper())
    m = re.search(rf"\[{re.escape(tag)}\]\s*(.*?)(?=\n\s*\[[A-Z]+\]|\Z)", s, re.S)
    return m.group(1).strip()[:600] if m else ""


# ── Enrichment cache lookup for rating context ────────────────────────────────

# What counts as having listened to a track, rather than having started it.
# Mirrors the rule import_spotify.py applies when it sets ``completed``
# ("trackdone", or not skipped and >= 2 min). Used to reject skips from the
# listening depth below: a track abandoned repeatedly is evidence AGAINST
# an artist, so counting it as devotion had the sign backwards.
REAL_LISTEN_MS = 120_000

# How long a Keep on a deletion card holds a title out of the proposal pool.
# The Deletions view states this number in its Keep copy;
# tests/test_keep_and_delete_feedback.py pins the two together.
KEEP_COOLDOWN_DAYS = 90

# A candidate whose deletion score lands above this goes on to the curator's
# judgement; the card's "Why?" names the number, and
# tests/test_score_factors.py pins the copy to it.
SHORTLIST_SCORE = 30

# Taste mismatch for a title with NO usable taste evidence — no embedding,
# no genre data, or a non-finite cosine. Just UNDER the shortlist bar: the
# old neutral 0.5 was 40 points and crossed SHORTLIST_SCORE on its own, so a
# new user's whole library — and every title enrichment never embedded — was
# shortlisted on the absence of evidence. Other evidence (bloat, a low
# rating, a recorded dislike, drop similarity) can still carry it over.
NO_TASTE_MISMATCH = (SHORTLIST_SCORE - 5) / 80.0

# Size never outweighs taste: the outlier path was capped at 25 points, the
# blanket log1p fallback was not (a 2 TB series scored 38).
SIZE_PTS_CAP = 25.0

# The pillar judge stops after TARGET_CUTS proposals or JUDGE_CAP judged
# titles. Learned keep-considerations are scored for the same JUDGE_CAP
# slice — they used to stop at 30, so ranks 31-60 were judged without them.
TARGET_CUTS, JUDGE_CAP = 10, 60
# Split install (server + node): a run happens only while the PC is free,
# so it builds a RESERVE - this many times deeper - that lasts while the PC
# is off. Stored verdicts (services/verdict_cache) cost no model call and do
# not count against the cap, up to REUSE_FACTOR x the cap per run.
RESERVE_FACTOR = 3
REUSE_FACTOR = 2


def _run_caps() -> tuple:
    """(target cuts, judge cap) for this run: the reserve on a split install."""
    from src.services.ollama_hosts import split
    if split():
        return TARGET_CUTS * RESERVE_FACTOR, JUDGE_CAP * RESERVE_FACTOR
    return TARGET_CUTS, JUDGE_CAP

# The last pillar-judging run per category, for the Deletions view's run
# banner. A judge call that FAILED (timeout, malformed JSON) used to collapse
# into the EVALUATE fallback and vanish exactly like a title the model
# genuinely could not decide — the owner saw fewer proposals and no reason.
# In-memory on purpose: it describes this server's last run, nothing to keep.
DELETION_RUN_SUMMARY: dict[str, dict] = {}


# ── Household taste (2026-10-21) ─────────────────────────────────────────────
# The taste term measured distance from the OWNER's history only: a series
# another household member would enjoy, but has not watched yet, read as
# 80 points of "far from your watch history". A title close to a member's
# taste now loses part of those points - how much grows with the size of that
# member's profile, so a thin profile barely moves anything - and a strong fit
# reaches the judge as a household argument. Never a floor: only an actual
# watch protects a title outright (pillars._household_claim).
HOUSEHOLD_TASTE_MAX_DAMPING = 0.5
HOUSEHOLD_TASTE_FULL_ITEMS = 20
HOUSEHOLD_TASTE_STRONG_FIT = 0.9


def _taste_comparators(blob: dict):
    """(unit taste vectors, item -> comparable unit vector) in ONE user's own
    space: centered by that blob's corpus mean, like the owner's cosine."""
    mean = None
    if blob.get("corpus_mean") and _normalize_vec(blob["corpus_mean"]) is not None:
        mean = np.asarray(blob["corpus_mean"], dtype=float)

    def cmp(vec):
        vn = _normalize_vec(vec)
        if vn is None or mean is None:
            return vn
        if vn.shape != mean.shape:
            return None
        c = vn - mean
        n = np.linalg.norm(c)
        return c / n if n > 1e-12 else None

    sources = [c.get("embedding") for c in (blob.get("cluster_centroids") or [])
               if isinstance(c, dict)] or [blob.get("embedding")]
    return [v for v in (cmp(s) for s in sources if s) if v is not None], cmp


def _load_household_tastes(owner_id: int, category: str) -> list[dict]:
    """Every OTHER active user's taste profile for this category that can be
    scored against: calibrated anchors, at least one embedded title."""
    out: list[dict] = []
    with get_db_session() as db:
        rows = (db.query(User.plex_username, EncryptedTasteVector.encrypted_blob)
                .join(User, User.id == EncryptedTasteVector.user_id)
                .filter(EncryptedTasteVector.media_category == category,
                        EncryptedTasteVector.user_id != owner_id,
                        User.is_active == True)  # noqa: E712
                .all())
    for name, raw in rows:
        try:
            blob = json.loads(raw or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(blob, dict) or blob.get("version") == 1:
            continue
        items = blob.get("embedding_items") or 0
        items = items if isinstance(items, int) else len(items)
        try:
            lo, hi = float(blob["cal_p10"]), float(blob["cal_p90"])
        except (KeyError, TypeError, ValueError):
            continue
        if items <= 0 or not (math.isfinite(lo) and math.isfinite(hi)) or hi - lo <= 1e-3:
            continue
        tastes, cmp = _taste_comparators(blob)
        if tastes:
            out.append({"name": name or "another user", "tastes": tastes, "cmp": cmp,
                        "anchors": (lo, hi), "items": items})
    return out


def _household_fits(item_vec, household: list[dict]) -> list[tuple]:
    """[(name, fit 0-1, damping weight)] of each member for one item."""
    out = []
    for h in household:
        iv = h["cmp"](item_vec)
        if iv is None:
            continue
        cos = max(float(np.dot(t, iv)) for t in h["tastes"])
        if not math.isfinite(cos):
            continue
        fit = 1.0 - _cosine_to_mismatch(cos, *h["anchors"])
        weight = HOUSEHOLD_TASTE_MAX_DAMPING * min(1.0, h["items"] / HOUSEHOLD_TASTE_FULL_ITEMS)
        out.append((h["name"], fit, weight))
    return out


def deletion_score_factors(*, mismatch: float, taste_src: str, size_pts: float,
                           size_gb: float, size_ratio: float | None,
                           rating: float | None, rating_swing: float,
                           user_rating: float | None, user_rating_swing: float,
                           feedback_swing: float, drop_penalty: float,
                           play_prot: float, plays: int = 0,
                           household_taste_pts: float = 0.0,
                           household_name: str | None = None,
                           keep_value_pts: float = 0.0,
                           considerations: list | None = None) -> list[dict]:
    """The deletion score taken apart into the terms that built it, each with
    a plain-language label — what the Deletions card's "Why?" shows.

    ``points`` carry the sign they have in the score: positive pushes toward
    deletion, negative toward keeping. Terms under half a point are dropped
    (a 5.0/10 rating, an unrated title). The sum of every term is the score
    the shortlist cut (> SHORTLIST_SCORE) was made on; the curator's verdict
    comes after.
    Labels describe the evidence, never a verdict."""
    out: list[dict] = []

    def add(key: str, pts: float, label: str) -> None:
        if abs(pts) >= 0.5:
            out.append({"key": key, "points": round(pts, 1), "label": label})

    taste_pts = mismatch * 80
    if taste_src == "none":
        add("taste", taste_pts, "No taste data for it yet, so it scores as neutral")
    else:
        basis = "your watch history" if taste_src == "embedding" else "the genres you watch"
        if mismatch >= 0.6:
            add("taste", taste_pts, f"Far from {basis}")
        elif mismatch >= 0.35:
            add("taste", taste_pts, f"Somewhat off {basis}")
        else:
            add("taste", taste_pts, f"Close to {basis}")
    if size_ratio:
        add("size", size_pts, f"{size_gb:g} GB, {size_ratio:.1f}× typical for its format")
    else:
        add("size", size_pts, f"{size_gb:g} GB on disk")
    if rating is not None:
        add("rating", -rating_swing, f"Community rating {rating:.1f}/10")
    if user_rating is not None:
        add("your_rating", -user_rating_swing, f"You rated it {user_rating:g}/10")
    add("feedback", feedback_swing,
        "You said you didn't like it" if feedback_swing > 0 else "You said you liked it")
    add("dropped", drop_penalty, "Similar to titles you stopped watching")
    add("plays", -play_prot, f"You've played it {plays} times")
    add("household_taste", -household_taste_pts,
        f"Close to {household_name or 'another household member'}'s taste")
    if keep_value_pts:
        what = ""
        for c in considerations or []:
            text = (c.get("text") or c.get("content") or "").strip() if isinstance(c, dict) else ""
            if text:
                what = f": “{text[:80]}{'…' if len(text) > 80 else ''}”"
                break
        add("keep_rules", -keep_value_pts, f"Matches something you said you value{what}")
    return out


def _score_factors(cand: dict) -> dict | None:
    """{"total": score, "factors": [...]} for one shortlisted candidate, or
    None when the terms were not recorded (never raises: an explanation
    must not cost a proposal)."""
    terms = cand.get("score_terms")
    if not terms:
        return None
    try:
        return {"total": round(cand["score"], 1),
                "factors": deletion_score_factors(
                    **terms, keep_value_pts=cand.get("keep_value_pts") or 0.0,
                    considerations=cand.get("considerations"))}
    except Exception as e:
        # The terms are our own numbers: a failure here is a bug, not data.
        logger.warning("[deletions] score factors failed for %r: %s",
                       (cand.get("item") or {}).get("title"), e, exc_info=True)
        return None


def _with_guard(sf: dict | None, cand: dict, verdict: dict | None) -> dict | None:
    """Carry a Python-side override of the model's verdict (``_guard``, e.g.
    "household_floor": a CUT turned into "Your call" because another member
    engaged with the title) inside the stored score_factors JSON, so the card
    can say WHY it is the owner's call. Without it the guard died with the
    verdict dict and the card showed the generic "merely fine" tooltip — the
    opposite of the real reason. Rides in the existing column: no schema
    change, and older rows simply have no "guard"."""
    guard = (verdict or {}).get("_guard")
    if not guard:
        return sf
    base = sf or {"total": round(cand.get("score") or 0, 1), "factors": []}
    return {**base, "guard": guard}


def _idx_key(item: dict) -> str:
    return f"{item.get('title') or ''}\x00{item.get('tmdb_id') or ''}"


def _member_keeps_for(user_id: int, items: list, category: str) -> dict:
    """Blocking (run_read): ``{_idx_key(item): [member names]}`` for the items
    another active household member asked to keep (pillars._other_users_keeps,
    the legacy path's Pillar III). An item whose lookup failed maps to None."""
    from src.database.connection import get_db_session
    from src.services.pillars import _other_users_keeps
    out: dict = {}
    with get_db_session() as db:
        for it in items:
            try:
                out[_idx_key(it)] = _other_users_keeps(db, user_id, it, category)
            except Exception as e:  # noqa: BLE001 — per item, logged; the item waits
                logger.warning("[deletions] member-keep lookup failed for %r: %s",
                               it.get("title"), e)
                out[_idx_key(it)] = None
    return out


def _failure_sentence(kinds: dict) -> str:
    """'Ollama is not answering (2); the answer was cut off (1)' — the causes
    behind a run's failures, most frequent first, for the Deletions banner."""
    from src.services.llm_errors import DESCRIPTIONS, LLMFailure
    parts = []
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        try:
            text = DESCRIPTIONS[LLMFailure(kind)]
        except ValueError:
            text = "an unexpected error"
        parts.append(f"{text} ({n})")
    return "; ".join(parts)


def _record_deletion_run(category: str, *, judged: int = 0, flagged: int = 0,
                         deferred: int = 0, fail_kinds: dict | None = None,
                         stopped: str = "", degraded: str = "") -> dict:
    """Store one category's run for the Deletions banner. ``failed`` keeps
    its old meaning (titles that got no verdict because something broke);
    ``failed_by_kind`` / ``failed_reason`` say what broke, ``stopped``
    why the run ended before its cap — a game, or an Ollama outage — and
    ``degraded`` which signal the run had to score without (an unreadable
    taste profile)."""
    kinds = {k: n for k, n in (fail_kinds or {}).items() if n}
    run = {
        "at": datetime.utcnow().isoformat(),
        "judged": judged,
        "flagged": flagged,
        "deferred": deferred,
        "failed": sum(kinds.values()),
        "failed_by_kind": kinds,
        "failed_reason": _failure_sentence(kinds),
        "stopped": stopped,
        "degraded": degraded,
    }
    DELETION_RUN_SUMMARY[category] = run
    return run


def run_judged_everything(category: str) -> bool:
    """Whether the last run of ``category`` went through to its end: only
    then may it replace the category's stored proposals wholesale. A run
    that waited for its node or stopped early judged a part (or nothing),
    and the proposals it did not reach stay (2026-10-23: a game started
    mid-run, three categories were deferred - and "Run analysis" for all of
    them replaced every stored proposal with the one category that ran)."""
    run = DELETION_RUN_SUMMARY.get(category)
    return bool(run) and not run.get("stopped")


def deletion_run_summary(category: str | None = None) -> dict | None:
    """The last run for one category, or all categories summed ("All" tab)."""
    if category:
        return DELETION_RUN_SUMMARY.get(category)
    runs = list(DELETION_RUN_SUMMARY.values())
    if not runs:
        return None
    out = {k: sum(r.get(k, 0) for r in runs) for k in ("judged", "flagged", "deferred", "failed")}
    out["at"] = max(r.get("at") or "" for r in runs)
    kinds: dict = {}
    for r in runs:
        for k, n in (r.get("failed_by_kind") or {}).items():
            kinds[k] = kinds.get(k, 0) + n
    out["failed_by_kind"] = kinds
    out["failed_reason"] = _failure_sentence(kinds)
    out["stopped"] = "; ".join(dict.fromkeys(r["stopped"] for r in runs if r.get("stopped")))
    out["degraded"] = "; ".join(dict.fromkeys(r["degraded"] for r in runs if r.get("degraded")))
    return out


def _judge_lane() -> tuple:
    """(may the next verdict load the model, why not).

    The same signal the game watcher evicts on (process_monitor.is_game_running:
    a game process, or another program holding the card). The deletion run
    checked it only when it started; a game launched mid-run had the watcher
    unload the pitcher every 30 s while the loop loaded it straight back with
    num_gpu=99 into the game's memory — an OOM or a 300 s timeout per title.
    A failed reading counts as free, like the lane itself does: detection
    trouble must not silently stop every deletion run."""
    try:
        # A split install judges on the node: is_game_running() reads THIS
        # machine there, so a game started on the node mid-run went unseen
        # until its calls failed as "outages" (2026-10-23).
        from src.services.ollama_hosts import judge_ready, split
        if split() and not judge_ready():
            from src.services.pc_host import node_name, status
            why = status().get("busy_reason") or status().get("reason") or "busy or away"
            return False, f"{node_name(cap=True)} is not free ({why})"
        from src.services.process_monitor import is_game_running
        if not is_game_running():
            return True, ""
        from src.services.llm_errors import DESCRIPTIONS, LLMFailure
        from src.services.llm_lane import gpu_reason
        why = gpu_reason()
        return False, DESCRIPTIONS[LLMFailure.LANE_CLOSED] + (f" ({why})" if why else "")
    except Exception as e:
        # Runs before every verdict: say it once, not once per title.
        if not _LANE_CHECK_WARNED:
            _LANE_CHECK_WARNED.append(True)
            logger.warning("[deletions] lane check failed - judging as if the "
                           "GPU were free: %r", e, exc_info=True)
        else:
            logger.debug("[deletions] lane check failed: %s", e)
        return True, ""


_LANE_CHECK_WARNED: list = []


# Two outages in a row end the run: Ollama down, or a model that cannot load,
# is down for the next title too. Each attempt can hold the GPU gate for the
# full judge timeout, and a 60-title cap of them is five hours.
_OUTAGE_STOP_AFTER = 2


def _play_protection(plays: int) -> float:
    """Score-side Resonance: sustained REPEATED listening protects an artist
    in the deletion PRE-RANK. The judge always saw the listening line — but
    the score never counted it, so a 174-play Kabarett artist ranked into the
    pitch batch on pure taste-vector mismatch and the persona then dismissed
    the plays rhetorically ('a habit, not value'). Log-scaled and capped at
    30 so a true mismatch (80) still surfaces; <=2 plays stays 0 — a single
    ancient play remains the strongest CUT signal there is."""
    import math
    if not plays or plays <= 2:
        return 0.0
    return min(30.0, math.log1p(float(plays)) * 6.0)


def _rating_cache_key(item: dict, category: str) -> str:
    """The enrichment cache key a candidate's profile lives under — mirrors
    the key logic in media_enricher.py."""
    id_key = item.get("tmdb_id") or item.get("tvdb_id") or (item.get("title") or "")[:40]
    return f"enriched:{category}:{id_key}"


def _rating_from_cached(cached: dict | None) -> tuple:
    """(rating_context_str, rating_float | None, cached_genres_list) from one
    cache entry (or None). Pure — no I/O."""
    if not cached:
        return None, None, []
    from src.services.rating_value import count_number, rating_number
    profile    = cached.get("response", {})
    # Cached profiles hold text here too ("N/A/10 (N/A votes)", a content
    # rating); a bare float() on one stopped the whole deletion scan.
    rating     = rating_number(profile.get("rating"))
    vote_count = count_number(profile.get("vote_count"))
    genres_raw = profile.get("genres") or []

    # Pass 54: a rating of exactly 0 is NOT a 0/10 verdict — it's the
    # "no rating data" sentinel. TMDB returns 0 for titles with no
    # votes, AniList's averageScore is null (→ 0 here) for fresh /
    # niche titles. Treating it as a real score punished obscure
    # small titles hard (rating_swing of -20 in the deletion score)
    # and fed the curator a literal "zero rating" pitch argument.
    # Require a positive rating to count; 0 falls through to the
    # caller's neutral-5.5 default. ``genres_raw`` is still returned
    # so cached genres survive the rating gate.
    if rating is not None and rating > 0:
        reliability = "limited votes — treat as indicative" if vote_count < 200 else f"{vote_count:,} votes"
        ctx = f"External rating: {rating:.1f}/10 ({reliability})"
        return ctx, rating, genres_raw
    # Rating absent or zero — still hand back the cached genres.
    return None, None, genres_raw


def _get_cached_rating(item: dict, category: str, strict: bool = False) -> tuple:
    """
    Look up TMDB/AniList rating from the enrichment SQLite cache.

    Returns (rating_context_str, rating_float | None, cached_genres_list)
    rating_context_str is injected into the deletion prompt.

    ``strict`` re-raises a failed cache read instead of answering "nothing
    cached": the judge's form guards must tell the two apart.

    One title. A loop over candidates uses ``_bulk_cached_ratings`` — one
    query for the whole batch.
    """
    try:
        from src.cache.metadata_cache import MetadataCache
        with MetadataCache() as cache:
            cached = cache.get_cache(_rating_cache_key(item, category))
    except Exception as exc:
        if strict:
            raise
        logger.warning("Enrichment cache lookup failed for '%s': %s", item.get("title", ""), exc)
        return None, None, []
    try:
        return _rating_from_cached(cached)
    except (TypeError, ValueError, AttributeError) as exc:
        # An odd-shaped record is read as "nothing cached", as before; it
        # is not an outage, so strict callers are not stopped by it.
        logger.warning("Cached rating unreadable for '%s': %s", item.get("title", ""), exc)
    return None, None, []


def _bulk_cached_ratings(items: list, category: str) -> dict:
    """{cache_key: (ctx, rating, genres)} for every item, in one batched
    cache read (``MetadataCache.get_cache_many``). Blocking — call it with
    ``asyncio.to_thread``. A failed read yields an empty map: the scorer then
    treats every title as unrated (neutral), exactly as a per-item miss did."""
    keys = [_rating_cache_key(it, category) for it in items]
    try:
        from src.cache.metadata_cache import MetadataCache
        with MetadataCache() as cache:
            found = cache.get_cache_many(keys)
    except Exception as exc:
        logger.warning("[deletions] bulk rating lookup failed: %s", exc)
        found = {}
    return {k: _rating_from_cached(found.get(k)) for k in keys}


def _aggregate_arr_rating(item: dict) -> tuple[float, dict]:
    """Aggregate ratings across every source the ARR item exposes.

    Radarr v3 nests ratings by source:
      ``{"tmdb":{"value":7.5,"votes":1000}, "imdb":{"value":7.2,"votes":5000},
         "rottenTomatoes":{"value":85,"votes":0}, "metacritic":{"value":78,"votes":0}}``

    Strategy:
      * Pull the value from each known source.
      * Normalise 0-100 scales (RT %, Metacritic) → 0-10.
      * Weight TMDB / IMDb by their vote counts, audience scores
        (RT / Metacritic) by a fixed editorial weight (200) so they have
        meaningful pull but don't dominate when a film has 50k IMDb votes.
      * If only one source has data, return that source — no averaging
        side-effects.

    Returns ``(weighted_avg_0_10, breakdown)`` where ``breakdown`` lists the
    contributing sources for logging / display, e.g.
    ``{"tmdb": 7.5, "imdb": 7.2, "rt": 8.5, "metacritic": 7.8}``.
    """
    ratings = item.get("ratings") or {}
    if not isinstance(ratings, dict):
        # Pre-Radarr-v3 flat format — fall back to vote_average.
        legacy = item.get("vote_average", 0) or 0
        return (float(legacy), {"legacy": float(legacy)} if legacy else {})

    breakdown: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0

    # Editorial weight for RT / Metacritic — lets them show up even when their
    # 'votes' field is empty in Radarr's payload. Tuned so a single critic
    # score sits roughly at-par with a few hundred IMDb votes.
    EDITORIAL_WEIGHT = 200.0

    for source_key, normalised_key, is_percent in (
        ("tmdb",            "tmdb",       False),
        ("imdb",            "imdb",       False),
        ("rottenTomatoes",  "rt",         True),
        ("metacritic",      "metacritic", True),
    ):
        src = ratings.get(source_key) or {}
        if not isinstance(src, dict):
            continue
        val = src.get("value") or 0
        votes = src.get("votes") or 0
        if not val:
            continue
        if is_percent and val > 10:
            val = val / 10  # 85% RT → 8.5
        # Audience scores → use vote count as weight; critic scores → editorial.
        weight = max(votes, 0) if not is_percent else EDITORIAL_WEIGHT
        if weight <= 0:
            weight = EDITORIAL_WEIGHT  # zero-vote sources still count, just less.
        weighted_sum += float(val) * weight
        weight_total += weight
        breakdown[normalised_key] = round(float(val), 2)

    # Flat / legacy format on top of nested (some Sonarr versions).
    if not breakdown:
        flat = ratings.get("value", 0) or 0
        if flat:
            breakdown["flat"] = float(flat)
            return (float(flat), breakdown)

    if weight_total <= 0:
        return (float(item.get("vote_average", 0) or 0), breakdown)

    return (weighted_sum / weight_total, breakdown)


CATEGORY_LABELS = {
    "music": "🎵 Music",
    "movie": "🎬 Movies",
    "show":  "📺 TV Shows",
    "anime": "⛩️ Anime",
}


async def _call_llm(prompt: str, max_tokens: int = 800, skip_priority: bool = False,
                    model_override: Optional[str] = None, host=None) -> Optional[str]:
    """Call curator model, fall back to base model. Signals enrichment to pause.

    Pass skip_priority=True when the caller manages the curator lifecycle itself
    (e.g. a batch loop that wraps all calls in a single curator_start/done).
    model_override prepends a specific bake to the fallback chain — used ONLY
    by the legacy deletion-pitch call sites for the two-bake split; discovery,
    collections and the yearly review pass nothing and stay on the curator.
    ``host``: the deletion run passes the host it resolved for the whole run.
    """
    host = host or host_for("judge" if model_override else "curator")

    async def _chain() -> Optional[str]:
        chain = ([model_override] if model_override else []) + [
            settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]
        for model in dict.fromkeys(m for m in chain if m):
            try:
                async with ollama_client(timeout=120) as client:
                    r = await client.post(
                        f"{host.url}/api/chat",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "keep_alive": CURATOR_KEEP_ALIVE,
                            **curator_options(temperature=0.7, num_predict=max_tokens,
                                              host=host),
                        },
                    )
                if r.status_code == 200:
                    content = strip_think_tags(
                        r.json().get("message", {}).get("content", "").strip()
                    )
                    logger.debug("LLM response (%d chars) from %s", len(content), model)
                    return content
                logger.warning("LLM HTTP %s from model %s", r.status_code, model)
                if r.status_code == 404:
                    continue
            except httpx.TimeoutException:
                logger.warning("LLM timeout on model %s", model)
            except Exception as e:
                logger.warning("LLM call failed (%s): %s", type(e).__name__, e)
        return None

    if skip_priority:
        return await _chain()
    # BACKGROUND + interruptible: a chat turn arriving mid-generation aborts
    # this request (Ollama stops decoding when the connection closes), takes
    # the GPU, and the chain re-runs afterwards — instead of the user waiting
    # out up to 800 tokens behind "Curatarr is busy (recommendations)".
    from src.services.llm_priority import background_curator_call
    return await background_curator_call("recommendations", _chain,
                                         exclusive_model=model_override)


_TRENDING_TTL_H = 24


def _trending_fresh(cached: dict, now: datetime = None,
                    ttl_h: float = _TRENDING_TTL_H) -> bool:
    """True when a cached {fetched_at, …} app_state blob is younger than ttl_h."""
    try:
        fetched = datetime.fromisoformat(cached.get("fetched_at") or "")
    except (TypeError, ValueError, AttributeError):
        return False
    return ((now or datetime.utcnow()) - fetched).total_seconds() < ttl_h * 3600


async def _get_music_neighbors(user_id: int, top_titles: list) -> list:
    """SoulSync graph neighbours of the user's favourite artists — the
    similar_artists field was fetched-and-discarded until now. Read-only
    metadata (never the download endpoint), cached 168h per user; any
    failure or an unconfigured SoulSync degrades to []."""
    from src.services.app_state import get_state, set_state
    key = f"soulsync_neighbors:{user_id}"
    try:
        cached = json.loads(await run_read(get_state, key) or "{}")
        if _trending_fresh(cached, ttl_h=168):
            return cached.get("names") or []
    except Exception as e:
        logger.warning("[music-neighbors] cache unreadable - refetching: %s", e)
    names: list = []
    try:
        from src.services import soulsync_client
        seen = {(t or "").lower() for t in top_titles}
        # Five independent lookups against a LAN neighbour — no reason to
        # serialise them (adapted from Jules PR #1). return_exceptions keeps
        # one unreachable artist from discarding the other four.
        infos = await asyncio.gather(
            *(soulsync_client.artist_info(a) for a in top_titles[:5]),
            return_exceptions=True)
        failed = 0
        for info in infos:
            if isinstance(info, BaseException):
                failed += 1
                continue
            for s in (info or {}).get("similar_artists") or []:
                if s and s.lower() not in seen and s not in names:
                    names.append(s)
        names = names[:12]
        if failed:
            # Deliberately NOT cached. The sequential version aborted before
            # the cache write when a lookup raised, so a blip meant "try again
            # next run"; gathering makes partial success the norm, and caching
            # it would freeze a degraded neighbour list for the full 168 h.
            logger.debug("[music-neighbors] %d/%d lookups failed — using "
                         "partial result without caching", failed, len(infos))
        elif names:
            await run_write(set_state, key, json.dumps(
                {"fetched_at": datetime.utcnow().isoformat(), "names": names}))
    except Exception as e:
        from src.services.enrichment_state import log_topup_failure
        log_topup_failure(logger, "[music-neighbors]", user_id, e)
    return names


def _trending_line(titles: list) -> str:
    """ONE optional discovery-prompt line. Soft by design: the taste profile
    stays the judge, and partition() strips owned/seen afterwards anyway."""
    if not titles:
        return ""
    return ("Currently trending this week — consider ONLY those that genuinely "
            "fit the taste profile: " + ", ".join(titles[:10]))


async def _get_trending(cat: str) -> list:
    """≤10 'Title (Year)' zeitgeist candidates per category from TMDB
    (trending + upcoming/on-air; anime via popular-JP-animation discover).
    Cached 24 h in app_state; ANY failure degrades to [] and the prompt
    line simply drops out. Runs inside custodian_recs, so it is already
    gated behind the weekly needs_llm tick — no extra scheduling."""
    if cat == "music":
        return []
    from src.services.app_state import get_state, set_state
    key = f"tmdb_trending:{cat}"
    try:
        cached = json.loads(await run_read(get_state, key) or "{}")
        if _trending_fresh(cached):
            return cached.get("titles") or []
    except Exception as e:
        logger.warning("[trending] cache unreadable for %s - refetching: %s", cat, e)
    api_key = settings.TMDB_API_KEY
    if not api_key:
        return []
    if cat == "movie":
        paths = [("/trending/movie/week", {}), ("/movie/upcoming", {})]
    elif cat == "anime":
        # generic tv-trending is US-network noise for this category — ask
        # TMDB for currently-popular Japanese animation instead
        paths = [("/discover/tv", {"with_genres": "16", "with_origin_country": "JP",
                                   "sort_by": "popularity.desc"})]
    else:
        paths = [("/trending/tv/week", {}), ("/tv/on_the_air", {})]
    titles: list = []
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=15) as client:
            for path, extra in paths:
                r = await client.get(f"https://api.themoviedb.org/3{path}",
                                     params={"api_key": api_key, **extra})
                if r.status_code != 200:
                    continue
                for it in (r.json().get("results") or []):
                    name = it.get("title") or it.get("name")
                    year = (it.get("release_date") or it.get("first_air_date") or "")[:4]
                    if not name:
                        continue
                    t = f"{name} ({year})" if year else name
                    if t not in titles:
                        titles.append(t)
        titles = titles[:10]
        if titles:
            await run_write(set_state, key, json.dumps(
                {"fetched_at": datetime.utcnow().isoformat(), "titles": titles}))
    except Exception as e:
        from src.services.enrichment_state import log_topup_failure
        log_topup_failure(logger, "[trending]", cat, e)
    return titles


def _rhythm_line(ts: dict) -> str:
    """One prompt line from the taste vector's temporal distributions — only
    when a pattern is actually pronounced (>40% share), else silent."""
    t = ts.get("temporal") or {}
    tod = t.get("time_of_day_dist") or {}
    dow = t.get("day_of_week_dist") or {}
    parts = []
    if tod:
        k, v = max(tod.items(), key=lambda x: x[1])
        if v > 0.4:
            parts.append(f"mostly {k}")
    weekend = (dow.get("5") or 0) + (dow.get("6") or 0)
    if weekend > 0.4:
        parts.append("weekend-heavy")
    return f"Viewing rhythm: {', '.join(parts)}." if parts else ""


def recommendation_score_factors(*, lane: str, confidence=None, fit: dict | None = None,
                                 values: list | None = None, trending: bool = False,
                                 neighbour: bool = False, exploration: bool = False) -> dict:
    """The signals one recommendation was picked on — what its card's "Why?"
    shows. Same {"factors": [{key, label}]} shape as the deletion breakdown,
    but without ``points``: a rec has no additive score (the curator picks
    from a taste-ranked pool), so a number per factor would be invented.
    Real numbers ride along as ``rank`` / ``of`` / ``cosine`` / ``value``.

    Only the requesting user's own signals go in — never an arr address,
    path or another user's data: members see their recs and this with them."""
    out: list[dict] = []
    fit = fit or {}
    if fit.get("rank"):
        rank, pool = fit["rank"], fit.get("pool") or fit["rank"]
        basis = ("closeness to your watch history" if fit.get("basis") == "embedding"
                 else "overlap with the genres you watch")
        f = {"key": "taste", "rank": rank, "of": pool,
             "label": f"#{rank} of {pool} unwatched titles in your library by {basis}"}
        if fit.get("cosine") is not None:
            f["cosine"] = fit["cosine"]
        out.append(f)
    if fit.get("genres"):
        out.append({"key": "genres", "label": "Genres you watch: " + ", ".join(fit["genres"])})
    if fit.get("disliked"):
        out.append({"key": "disliked", "label": "You said you didn't like it"})
    for text in values or []:
        text = (text or "").strip()
        if text:
            out.append({"key": "values", "label": "Matches something you said you value: "
                        f"“{text[:80]}{'…' if len(text) > 80 else ''}”"})
            break
    if trending:
        out.append({"key": "trending", "label": "Trending or upcoming on TMDB right now"})
    if neighbour:
        out.append({"key": "neighbour", "label": "Next to artists you play most, in the listening graph"})
    if exploration:
        out.append({"key": "exploration",
                    "label": "Picked as a deliberate stretch outside your usual taste"})
    out.append({"key": "lane", "label": "In your library and still unwatched" if lane == "library"
                else "Not in your library, and nothing you've watched or removed"})
    if (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and math.isfinite(confidence)):
        pct = round(max(0.0, min(1.0, float(confidence))) * 100)
        out.append({"key": "confidence", "value": pct,
                    "label": f"The curator rated the fit {pct}%"})
    return {"kind": "recommendation", "lane": lane, "factors": out}


async def generate_recommendations(
    user_id: int,
    category: str = None,
    limit: int = 10,
    arr_library: list = None,  # list of {title, genres, year, ...} from ARR
) -> list:
    """
    Generate recommendations with an LLM pitch. Pure generator — caching is the
    caller's job (the scheduler / router own the CachedRecommendation table).

    Two lanes, derived from ``arr_library``:
      * LIBRARY  (arr_library given): pick the best owned-but-unwatched items;
        already-watched ones are filtered by stable id + normalised title.
      * DISCOVERY (no arr_library): suggest taste-fit titles the user does NOT
        own and has NOT seen. The model's output is HARD-filtered through
        library_memory.partition(), so an owned/watched title (the "Ex Machina"
        bug) can never survive — soft prompt lines alone never sufficed.
    """

    # Detect the user's language once so each pitch is written in the language
    # they chat in. One cheap SELECT; the directive is injected into every
    # prompt below.
    from src.services.llm_utils import detect_user_language, language_directive

    def _user_lang():
        with get_db_session() as _ld_db:
            return detect_user_language(user_id, _ld_db)

    _lang_code = await run_read(_user_lang)
    lang_directive_str = language_directive(_lang_code)

    # LANE: "library" when the caller hands us an owned pool to pick from,
    # "discovery" when we suggest new-to-acquire titles from taste alone.
    lane = "library" if arr_library is not None else "discovery"

    # Shared memory layer for the watched/owned filters below.
    from src.services.library_memory import (
        seen_index, owned_index, removed_index, is_seen, partition, normalize_title,
    )

    # Taste context. No cache read here — this function always generates fresh;
    # the scheduler/router persist results to CachedRecommendation.
    def _taste():
        with get_db_session() as db:
            tv = db.query(TasteVectorEntry).filter(
                TasteVectorEntry.user_id == user_id
            ).first()
            if not tv:
                return None
            return json.loads(tv.genre_affinity or "{}"), tv.summary_text or ""

    _t = await run_read(_taste)
    if _t is None:
        return []
    type_data, summary_text = _t

    # --- Generation ---
    cats = [category] if category else list(type_data.keys())
    all_recs = []

    for cat in cats:
        ts = type_data.get(cat)
        if not ts or not isinstance(ts, dict):
            continue

        top_genres = list((ts.get("genre_affinity") or {}).keys())[:6]
        top_themes = list((ts.get("themes") or {}).keys())[:5]
        top_moods = list((ts.get("moods") or {}).keys())[:4]
        top_titles = ts.get("top_titles", [])[:8]
        # One soft context line: current season + (if pronounced) viewing rhythm.
        context_line = " ".join(x for x in (seasonal_context(), _rhythm_line(ts)) if x)
        # Multimodal pairs: show the LLM the distinct taste clusters so its
        # picks serve each interest instead of an averaged blend. The same
        # blob carries the owner's disliked titles (discovery exclusions).
        disliked_titles: list = []
        # The blob COLUMN, read inside the session: get_db_session commits on
        # exit (expire_on_commit), so reading an ORM row's attribute after the
        # block raised DetachedInstanceError — swallowed by a bare
        # ``except Exception: pass`` here, which silently dropped the cluster
        # hint from every prompt for months. Only an unreadable blob is
        # expected; anything else is a bug and is logged as one.
        _cl = []
        def _blob():
            with get_db_session() as _cdb:
                return _cdb.query(EncryptedTasteVector.encrypted_blob).filter(
                    EncryptedTasteVector.user_id == user_id,
                    EncryptedTasteVector.media_category == cat).limit(1).scalar()

        try:
            _blob_raw = await run_read(_blob)
            _cb = json.loads(_blob_raw) if _blob_raw else {}
            if isinstance(_cb, dict) and _cb.get("version") != 1:
                _cl = [c for c in (_cb.get("cluster_centroids") or [])
                       if isinstance(c, dict)]
                disliked_titles = [str(t) for t in (_cb.get("disliked_titles") or [])]
        except (ValueError, TypeError) as e:
            logger.warning("[recs] %s: taste blob unreadable — no cluster hint "
                           "or disliked titles this run: %s", cat, e)
        except Exception:
            logger.warning("[recs] %s: taste-cluster read failed", cat, exc_info=True)
        if _cl:
            context_line += ("\nTheir taste here splits into distinct "
                             "clusters — serve them ALL, not a blend: "
                             + " | ".join(
                                 f"{int(float(c.get('share') or 0) * 100)}% around "
                                 f"{', '.join(str(t) for t in (c.get('top_titles') or [])[:3])}"
                                 for c in _cl[:5]))

        import re
        match = re.search(rf'\[{re.escape(cat.upper())}\]([^\[]*)', summary_text)
        cat_summary = match.group(1).strip() if match else ""

        # Both lanes need the user's FULL seen-set for this category (no [:15]
        # cap; matched by stable id + normalised title).
        seen = await run_read(seen_index, user_id, cat)
        # Per-lane evidence the "Why?" breakdown is built from after the pick.
        _why_by_title: dict = {}
        trend_titles: list = []
        nb: list = []

        if arr_library:
            # LIBRARY LANE: owned items with already-watched dropped robustly.
            unwatched = [
                item for item in arr_library
                if not is_seen(item, seen)
            ][:50]

            if not unwatched:
                continue

            # Ground the candidate list with cached verified context (themes +
            # a short plot) — NO LLM, cache-only — so the curator selects AND
            # pitches from real data instead of just title+genres. These are the
            # user's own library items, so they're already enriched; this is just
            # fast cache reads, and the pitch the user sees is now grounded.
            # Honour the verified-data DEMAND here too (not just deletion): use
            # ensure_verified_data so each candidate gets its on-demand OMDb +
            # Wikipedia significance fetched right before the curator pitches it.
            # Concurrent + per-item time-boxed (inside ensure) + cached after the
            # first run, so a recommendation pass warms the library's significance
            # over time instead of pitching from a thin profile.
            from src.services.media_enricher import ensure_verified_data
            from src.services.episodic_memory import retrieve_considerations
            import asyncio as _asyncio

            async def _cand_line(i):
                line = f"- {i['title']} ({i.get('year', '?')}) — {i.get('genres', '')}"
                try:
                    # Batch path — never distill via the summarizer here (VRAM
                    # churn against the curator; see ensure_verified_data).
                    vd = await ensure_verified_data(
                        i["title"], cat,
                        tmdb_id=i.get("tmdb_id"), tvdb_id=i.get("tvdb_id"),
                        anilist_id=i.get("anilist_id"),
                        plex_rating_key=i.get("plex_rating_key"),
                        year=i.get("year"),
                        allow_summarizer=False,
                    )
                except Exception as e:
                    from src.services.enrichment_state import log_topup_failure
                    log_topup_failure(logger, "[recs] pitch verified-data", i.get("title"), e)
                    vd = None
                if vd:
                    th = vd.get("themes") or []
                    if th:
                        line += " | themes: " + ", ".join(str(t) for t in th[:3])
                    sig = vd.get("significance")
                    if sig:
                        line += " | significance: " + str(sig)[:160]
                    plot = vd.get("plot")
                    if plot:
                        line += " | " + str(plot)[:90]
                # Learned value-considerations — the SAME bridge that protects
                # items on the deletion side, applied with the opposite sign here:
                # does the user have standing keep/value feedback (a treasured
                # franchise, a partner favourite, cultural weight) that plausibly
                # applies to THIS candidate? If so surface it so the curator can
                # give taste-fitting, user-valued items clear preference. Profile
                # built from the verified themes/plot when present (richer match)
                # else title+genres. Embeds one short string — marginal next to
                # the verified-data fetch already happening in this gather.
                prof_parts = [str(i.get("title") or ""), str(i.get("genres") or "")]
                if vd:
                    _th = vd.get("themes") or []
                    if _th:
                        prof_parts.append(", ".join(str(t) for t in _th[:5]))
                    _pl = vd.get("plot") or ""
                    if _pl:
                        prof_parts.append(str(_pl)[:200])
                prof = " — ".join(p for p in prof_parts if p.strip())
                try:
                    cons = await retrieve_considerations(
                        user_id, prof, media_category=cat, top_k=2)
                except Exception as e:
                    from src.services.enrichment_state import log_topup_failure
                    log_topup_failure(logger, "[recs] pitch considerations", i.get("title"), e)
                    cons = []
                # Precision guard for the VISIBLE ⭐ surfacing (stricter than the
                # deletion side's silent, capped score nudge — a wrong tag the
                # user reads is worse than a missed one). retrieve_considerations
                # is recall-leaning and anisotropic embeddings let a strong, broad
                # NULL-category memory match cross-domain on pure embedding alone
                # (e.g. a music memory firing on "The Pianist"). Such bleed has an
                # EMPTY lexical overlap; genuine generalisations keep a lexical
                # anchor ("franchise", "anime", a title token) or an exact-category
                # match. Require one of those before showing the user a ⭐.
                cons = [c for c in cons
                        if c.get("overlap") or c.get("media_category") == cat]
                if cons:
                    line += " | ⭐ USER VALUES: " + "; ".join(
                        str(c.get("content") or "")[:80] for c in cons[:2])
                return line, [str(c.get("content") or "") for c in cons[:2]]

            offered = unwatched[:30]
            lines = await _asyncio.gather(*[_cand_line(i) for i in offered])
            items_text = "\n".join(line for line, _ in lines)
            # The pick is matched back to its candidate for "Why?": the fit
            # score_arr_items ranked it on and the values shown beside it.
            for i, (_, vals) in zip(offered, lines):
                _why_by_title.setdefault(normalize_title(i.get("title") or ""),
                                         (i.get("_rec_fit"), vals))

            prompt = f"""[MODE: ELITE RECOMMENDATION PITCH]
You are Curatarr, a highly analytical and slightly opinionated personal media curator.

{lang_directive_str}

USER'S {cat.upper()} TASTE PROFILE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}
{context_line}

AVAILABLE {cat.upper()} LIBRARY (unwatched):
{items_text}

MISSION: Pick the best {min(limit, 5)} recommendations from the library above.

CRITICAL RULES AND GUARDRAILS:
1. THE PITCH: For each item, write 2-3 vivid sentences on specifically why it fits the user — the hook, then the texture/tone, then who it lands for. No filler, no plot-summary padding.
2. SYNTHESIZE, DON'T QUOTE: Read the Taste Profile to UNDERSTAND the user, then describe the recommended item in YOUR OWN vocabulary. The profile is reference material for you, not a phrasebook to echo back at them.
{_blacklist_rule(3)}
4. NO LAZY ANCHORING: DO NOT explicitly name titles from the Taste Profile (e.g., "If you liked [Title], you'll love this"). The pitch must stand on its own merits.
5. STANDING USER VALUES: An item tagged "⭐ USER VALUES: …" carries a preference the user has TAUGHT you over time — a treasured franchise, a partner's favourite, cultural/archival weight. When such an item ALSO fits the taste profile, give it clear preference; reflecting what they value is part of the job. But the value is a booster and tie-breaker, NOT an override — do not recommend an item that plainly clashes with the taste profile just because it's flagged.
6. JSON ONLY: Output as a strictly valid JSON array.

Output format:
[{{"title": "Exact Title", "reason": "Your elite 2-3 sentence pitch.", "confidence": 0.0-1.0}}]"""

        else:
            owned = await owned_index(cat)
            # Deleted through Curatarr (usually UNWATCHED — neither seen nor
            # owned any more) or recorded as disliked: never re-pitched.
            removed = await asyncio.to_thread(removed_index, user_id, cat, disliked_titles)
            noun = "tracks/artists" if cat == "music" else "titles"
            trend_titles = await _get_trending(cat)
            trend = _trending_line(trend_titles)
            trend_block = f"\n{trend}" if trend else ""
            if cat == "music":
                from src.services.library_memory import normalize_title as _nt
                owned_t = owned.get("titles") or set()
                nb = [n for n in await _get_music_neighbors(user_id, top_titles)
                      if _nt(n) not in owned_t and n.lower() not in owned_t]
                if nb:
                    trend_block += ("\nGraph neighbours of their favourite "
                                    "artists (from the listening graph — only "
                                    "if taste-fit): " + ", ".join(nb[:12]))
            # Soft nudge: the user's prominent taste anchors are the titles the
            # model is most tempted to echo straight back. Naming them helps —
            # but the HARD guarantee is partition() after parsing, which strips
            # anything owned or seen no matter what the model returns.
            avoid_hint = ", ".join(top_titles[:8]) or "—"
            prompt = f"""[MODE: ELITE EXTERNAL DISCOVERY]
You are Curatarr, a highly analytical and slightly opinionated personal media curator.

{lang_directive_str}

USER'S {cat.upper()} TASTE PROFILE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}
{context_line}{trend_block}

DO NOT suggest these (the user already knows them well) — and do NOT suggest anything they already OWN or have already SEEN. Owned/seen titles are stripped automatically, so any such pick is a wasted slot. Spend every pick on something genuinely NEW to them:
{avoid_hint}

MISSION: Suggest {limit} {cat} {noun} that are NEW to the user — not owned, not already seen/heard.

CRITICAL RULES AND GUARDRAILS:
1. THE PITCH: Each suggestion needs a rich 2-3 sentence pitch — the hook, the texture/tone, and why it lands for THIS user. Direct and opinionated, no filler.
2. SYNTHESIZE, DON'T QUOTE: Use the Taste Profile to UNDERSTAND what fits, then describe each suggestion in YOUR OWN vocabulary. Don't narrate the profile back at the user — they wrote those signals, they don't need them echoed.
{_blacklist_rule(3)}
4. NO LAZY ANCHORING: DO NOT explicitly name titles from the Taste Profile to draw comparisons.
5. EXPLORATION QUOTA: make ~15% of the picks (1-2) deliberate stretches slightly OUTSIDE the profile — an adjacent genre, era or tone they haven't tried; start those reasons with "Exploration pick:".
6. JSON ONLY: Output as a strictly valid JSON array.

Output format:
[{{"title": "...", "reason": "Your elite 2-3 sentence pitch.", "confidence": 0.0-1.0, "genres": "..."}}]"""

        # Richer 2-3 sentence pitches need more headroom than the old
        # 1-sentence form: ~5 items × 3 sentences ≈ 600-700 tokens of content
        # plus JSON overhead. 2600 keeps full responses from truncating
        # mid-string (which made json.loads silently fail → empty lane).
        response = await _call_llm(prompt, max_tokens=2600)
        if not response:
            continue

        # Only the model's JSON is expected to fail. The partition below is
        # the hard guarantee against owned/seen/deleted titles: a bug in it
        # must surface, not read as "the model returned junk" (empty lane).
        try:
            recs = json.loads(clean_llm_text(response))
        except ValueError as e:
            logger.warning("[recs] %s/%s: unparseable LLM answer (%d chars): %s",
                           lane, cat, len(response), e)
            continue
        if not isinstance(recs, list):
            logger.warning("[recs] %s/%s: LLM answer is not a JSON list", lane, cat)
            continue
        recs = [r for r in recs if isinstance(r, dict) and r.get("title")]
        # DISCOVERY: hard-drop anything the user owns or has already seen.
        # The model ignores soft "don't suggest" lines, so this — matching
        # on stable id + normalised title — is the real guarantee that a
        # watched/owned title (the "Ex Machina" bug) never surfaces here.
        if lane == "discovery":
            kept = partition(recs, seen, owned, removed)["new"]
            if len(kept) != len(recs):
                logger.info(
                    "[recs] discovery/%s: dropped %d owned/seen/removed of %d "
                    "LLM suggestions", cat, len(recs) - len(kept), len(recs),
                )
            recs = kept
        trend_n = {normalize_title(t) for t in trend_titles}
        nb_n = {normalize_title(n) for n in nb}
        for rec in recs:
            rec["category"] = cat
            rec["category_label"] = CATEGORY_LABELS.get(cat, cat)
            rec["lane"] = lane
            t_n = normalize_title(rec.get("title") or "")
            fit, vals = _why_by_title.get(t_n, (None, None))
            rec["score_factors"] = recommendation_score_factors(
                lane=lane, confidence=rec.get("confidence"), fit=fit, values=vals,
                trending=t_n in trend_n, neighbour=t_n in nb_n,
                exploration=(lane == "discovery" and str(rec.get("reason") or "")
                             .lstrip().lower().startswith("exploration pick")))
            all_recs.append(rec)

    return all_recs


# ── Taste-mismatch (semantic distance) helpers ────────────────────────────────

def _normalize_vec(vec):
    """Unit-normalise a vector for cosine math. Returns an np array, or None if
    empty/zero. The taste vector is already ~unit, but the ChromaDB item
    embeddings are stored RAW (norm ~13) — both sides must be normalised or the
    dot product lands far outside [-1, 1] and the mismatch signal collapses."""
    if vec is None:
        return None
    try:
        a = np.asarray(vec, dtype=float)
    except (TypeError, ValueError):
        return None
    if a.ndim != 1 or not np.isfinite(a).all():
        return None
    n = np.linalg.norm(a)
    return a / n if n > 1e-12 else None


def _cosine_anchors(cos_vals: list) -> tuple:
    """p10/p90 of a batch of cosines — the stretch anchors that map the model's
    anisotropic, narrow cosine band (movies cluster ~0.6-0.74) onto a full
    [0, 1] mismatch range. Falls back to sensible defaults when too few
    embeddings are present, and guarantees hi > lo. Non-finite cosines are
    ignored: one NaN made both percentiles NaN, and with them every
    candidate's mismatch."""
    vals = [c for c in cos_vals if c is not None and math.isfinite(c)]
    if len(vals) >= 20:
        lo = float(np.percentile(vals, 10))
        hi = float(np.percentile(vals, 90))
    else:
        lo, hi = 0.61, 0.72
    if hi - lo < 1e-3:
        hi = lo + 1e-3
    return lo, hi


def _cosine_to_mismatch(cosine, lo: float, hi: float) -> float:
    """Map a cosine onto a 0-1 taste-mismatch: cos >= hi (best-fit) -> 0,
    cos <= lo (worst-fit) -> 1, linear between. No usable cosine (missing,
    non-finite, or broken anchors) -> NO_TASTE_MISMATCH, just under the
    shortlist bar — never "maximally foreign": Python's min(1.0, nan) is 1.0,
    so a NaN used to score the full 80 points."""
    if (cosine is None
            or not all(math.isfinite(x) for x in (cosine, lo, hi))
            or hi - lo <= 0):
        return NO_TASTE_MISMATCH
    return max(0.0, min(1.0, (hi - cosine) / (hi - lo)))


def _watch_pitch_line(status: dict) -> str:
    """One framed line on the candidate's own Plex watch status for the pitch.

    Deliberately NEUTRAL — watch status cuts BOTH ways and must not become an
    automatic verdict (that's why it nudges no score, only the reasoning):
      • unwatched   → untested clutter OR a deliberate to-watch pick
      • watched 1×  → got the value and done (deletable) OR a one-off they loved
      • watched n×  → re-watches = real attachment (leans keep)
      • abandoned   → a bounce (supports deletion) OR merely on-hold
    The curator weighs which; the line just hands it the fact it was missing."""
    if not status:
        return ("WATCH STATUS: the user has NOT watched this yet — weigh whether it's "
                "untested clutter or a deliberate to-watch pick; 'unwatched' alone "
                "proves neither, so don't treat it as automatic justification.")
    n = status.get("count", 0)
    if status.get("completed"):
        when = f", last seen {status['last'].strftime('%b %Y')}" if status.get("last") else ""
        if n > 1:
            return (f"WATCH STATUS: the user has watched this {n}×{when} — repeat views "
                    f"signal real attachment; a deletion pitch needs a genuine case, not "
                    f"just taste-distance.")
        return (f"WATCH STATUS: the user watched this once{when} — they may have got the "
                f"value and be done (fine to delete), or it may be a one-off they loved. "
                f"Weigh which before pitching.")
    return ("WATCH STATUS: the user STARTED but never finished this — possibly a bounce "
            "(supports deletion), possibly just on-hold. Weigh, don't assume.")


def _format_pillar_reason(v: dict) -> str:
    """Human-readable Begründung from a pillar verdict — stored on the protection
    row so the admin debug view can show WHY the judge saved a title."""
    parts = []
    if v.get("pillar_3_household"):
        parts.append(f"III Household — {v['pillar_3_household']}")
    if v.get("pillar_2_custodian"):
        parts.append(f"II Custodian — {v['pillar_2_custodian']}")
    if v.get("pillar_1_resonance"):
        parts.append(f"I Resonance — {v['pillar_1_resonance']}")
    if v.get("pillar_0_ego"):
        parts.append(f"0 Ego — {v['pillar_0_ego']}")
    if v.get("bitrate_note"):
        parts.append(f"Bitrate — {v['bitrate_note']}")
    return "\n".join(parts)


def _persist_judge_protection(user_id: int, item: dict, category: str,
                              verdict_str: str, verdict_obj: dict) -> None:
    """Upsert a judge-granted protection into ProtectedMedia(source='judge').

    The identifier matches the deletion-exclusion check at the top of the
    scoring loop (``str(tmdb_id)`` or title), so the title drops out of EVERY
    future scan until the admin lifts it in the debug UI. A manual whitelist
    entry is never clobbered (the user outranks the judge)."""
    tmdb_id = item.get("tmdb_id")
    identifier = str(tmdb_id) if tmdb_id is not None else (item.get("title") or "")
    if not identifier:
        return
    reason = _format_pillar_reason(verdict_obj)
    try:
        with get_db_session() as db:
            row = (db.query(ProtectedMedia)
                     .filter(ProtectedMedia.user_id == user_id,
                             ProtectedMedia.identifier == identifier)
                     .first())
            if row:
                if (row.source or "manual") != "manual":
                    if row.verdict != verdict_str:
                        judge_record.archive_protection(db, row, "changed",
                                                        new_verdict=verdict_str)
                    row.source, row.verdict = "judge", verdict_str
                    row.reason, row.category = reason, category
                    row.title = item.get("title")
                    row.arr_url = item.get("arr_url") or row.arr_url
            else:
                db.add(ProtectedMedia(
                    user_id=user_id, identifier=identifier, category=category,
                    source="judge", verdict=verdict_str, reason=reason,
                    title=item.get("title"), arr_url=item.get("arr_url")))
            db.commit()
    except Exception as e:
        logger.warning("[deletions] persist judge protection failed for %r: %s",
                       item.get("title"), e)


async def generate_deletion_proposals(
    user_id: int,
    arr_items: list,
    category: str = "movie",
    monitor_task=None,
) -> list:
    """Surgical Deletion: Identifies trash, protects classics, respects whitelists.

    Pass 99-fu5: optional ``monitor_task`` enables intra-function progress
    reporting via task_monitor. Without it, the function ran for 1-3
    minutes per category showing "Analysing N items" with no inner movement
    — and ~80% of the time was actually phase B (LLM pitch generation for
    the top 10 candidates), invisible to the user. Now the message updates
    at phase boundaries and per-pitch so it's obvious what's happening.
    """
    def _msg(text: str) -> None:
        """Best-effort task_monitor message update; no-op when called outside the scheduler/API task path."""
        if monitor_task is None:
            return
        try:
            from src.services.task_monitor import task_monitor as _tm
            _tm.update(monitor_task, message=text)
        except Exception:  # noqa: BLE001 - progress card only; the run is unaffected
            pass

    import math

    # The reserve on a split install, the classic 10 / 60 otherwise.
    target_cuts, judge_cap = _run_caps()

    # Plex user-rating hard-protect line (Pass 82c, see the music preload below).
    HARD_PROTECT_THRESHOLD = 8.0      # Plex 0-10 scale; 8.0 = 4 stars

    def _load_phase_a() -> dict:
        """Every DB read the scoring needs, on a worker thread: the taste
        blob, protections, keep-cooldown, curator stances, the 90-day
        watch veto (tens of thousands of rows for music) and the music
        ratings. These ran on the event loop and froze every request and
        SSE stream for seconds per category. No awaits and no task_monitor
        calls in here — both belong to the loop."""
        # Pass 40: detect user's chat language once so each pitch can be
        # written in the same language the user chats in.
        from src.services.llm_utils import detect_user_language, language_directive
        with get_db_session() as _ld_db:
            _lang_code = detect_user_language(user_id, _ld_db)
        lang_directive_str = language_directive(_lang_code)
        # Every veto below SHIELDS titles. One that cannot be read must stop
        # the category, not score it as if nobody had kept, discussed, played
        # or rated anything: an empty set reads as "no protection".
        guard_failures: list[str] = []
        # Taste signals that exist but could not be read: the run goes on
        # without them, but says so (log + run banner) instead of scoring as
        # if the owner had no taste — or, with nothing left, as a cold start.
        taste_unreadable: list[str] = []

        with get_db_session() as db:
            # 1. Load Taste Vector
            tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
            user_vector = None
            taste_blurb = ""
            disliked_lc: set = set()
            feedback_sentiment_lc: dict = {}
            cal_anchors = None
            corpus_mean = None
            drop_vector = None
            drop_anchors = None
            taste_clusters = None
            ts_genres: dict = {}
            if tv:
                # Use the taste section that matches THIS category — not a blind
                # [:400] that always returned the [MOVIE] block (which judged anime/
                # show/music pitches by the user's film taste).
                taste_blurb = _taste_section(tv.summary_text, category)
                try:
                    # genre affinity of THIS category — the taste fallback for
                    # candidates without an embedding (eval 1.7)
                    _td = json.loads(tv.genre_affinity or "{}")
                    ts_genres = {g.lower(): s for g, s in
                                 ((_td.get(category) or {}).get("genre_affinity")
                                  or {}).items()}
                except (TypeError, ValueError, AttributeError) as e:
                    logger.warning("[deletions] %s genre affinity unreadable — "
                                   "scoring without genre stats: %r", category, e)
                    taste_unreadable.append("genre affinity")
                    ts_genres = {}
                encrypted = db.query(EncryptedTasteVector).filter(
                    EncryptedTasteVector.user_id == user_id,
                    EncryptedTasteVector.media_category == category
                ).first()
                if encrypted and encrypted.encrypted_blob:
                    # Version gate (eval 1.10): a v1-encrypted blob must degrade
                    # LOUDLY, not silently zero out every taste signal.
                    try:
                        _blob = json.loads(encrypted.encrypted_blob)
                        if not isinstance(_blob, dict):
                            raise ValueError(f"blob is a {type(_blob).__name__}")
                    except (TypeError, ValueError) as e:
                        # same loud degrade as the v1 gate below: no taste
                        # vector, anchors or owner feedback this run
                        logger.warning("[deletions] %s taste blob is corrupt — "
                                       "scoring WITHOUT taste signals until the "
                                       "next taste rebuild rewrites it: %r", category, e)
                        taste_unreadable.append("taste vector and owner feedback")
                        _blob = {}
                    if _blob.get("version") == 1:
                        logger.warning("[deletions] %s taste blob is encrypted "
                                       "(v1) — scoring WITHOUT taste signals "
                                       "until the next taste rebuild rewrites it", category)
                        taste_unreadable.append("taste vector and owner feedback")
                        _blob = {}
                    user_vector = _blob.get("embedding")
                    # Global calibration anchors from the last taste rebuild
                    # (eval 1.6) — per-batch anchors made stored scores
                    # non-stationary across runs.
                    _p10, _p90 = _blob.get("cal_p10"), _blob.get("cal_p90")
                    if _p10 is not None and _p90 is not None and (_p90 - _p10) > 1e-3:
                        cal_anchors = (float(_p10), float(_p90))
                    # Eval 2.3/2.4: corpus mean (anisotropy centering — anchors
                    # in a blob that carries it are CENTERED ones), plus the
                    # drop centroid + its anchors for the scoring-side penalty.
                    corpus_mean = _blob.get("corpus_mean")
                    drop_vector = _blob.get("drop_embedding")
                    # Multimodal pairs: score as MAX over cluster centroids
                    # (ComiRec serving) — the blob's anchors were calibrated
                    # against exactly that statistic.
                    taste_clusters = _blob.get("cluster_centroids")
                    _d10, _d90 = _blob.get("drop_cal_p10"), _blob.get("drop_cal_p90")
                    drop_anchors = ((float(_d10), float(_d90))
                                    if _d10 is not None and _d90 is not None
                                    and (_d90 - _d10) > 1e-3 else None)
                    # explicit owner feedback finally gets a reader: a recorded
                    # dislike nudges a candidate UP the deletion list, recorded
                    # praise nudges it down (soft — taste mismatch still rules)
                    disliked_lc = {(t or "").lower()
                                   for t in _blob.get("disliked_titles") or []}
                    feedback_sentiment_lc = {}
                    for _fb in _blob.get("explicit_feedback") or []:
                        _ft = (_fb.get("title") or "").lower()
                        if _ft:   # chronological list — the latest statement wins
                            # (sentiment, weight): weight > 1 = elevated signal
                            # (verdict on a Curatarr-recommended title); entries
                            # from before the field existed read as 1.0.
                            feedback_sentiment_lc[_ft] = (
                                _fb.get("sentiment"),
                                float(_fb.get("weight") or 1.0),
                            )

            # 2. Load Whitelist (ProtectedMedia) — category-scoped: protections
            # match by identifier (often a bare TITLE), and a title-only match
            # crosses domains. The user protecting the BAND "The Devil Wears
            # Prada" must not shield the same-named FILM from ever being
            # pitched. NULL category = legacy rows → still protect everywhere.
            from sqlalchemy import or_ as _or
            protected = {
                p.identifier for p in db.query(ProtectedMedia).filter(
                    ProtectedMedia.user_id == user_id,
                    _or(ProtectedMedia.category == category,
                        ProtectedMedia.category.is_(None)),
                ).all()
            }

            # 2b. KEEP-COOLDOWN: titles the user explicitly kept (clicked "Keep" →
            # proposal status 'rejected') stay out of the pool for a while. A bare
            # Keep previously left NO trace — the very next scan could re-pitch the
            # same title (and a STAGNANT card the user dismissed came right back).
            # This is deliberately a COOLDOWN, not a protection: after it lapses the
            # judge looks again — taste and principles evolve. Category-scoped so a
            # kept movie can't shield a same-named show/album (NULL = legacy rows).
            _REJECT_COOLDOWN_DAYS = KEEP_COOLDOWN_DAYS
            rejected_recent: set[str] = set()
            try:
                from src.database.models import DeletionProposal as _DP
                from datetime import datetime as _rcdt, timedelta as _rctd
                _rc_cutoff = _rcdt.utcnow() - _rctd(days=_REJECT_COOLDOWN_DAYS)
                _rc_rows = (
                    db.query(_DP.title, _DP.tmdb_id)
                    .filter(_DP.user_id == user_id,
                            _DP.status == "rejected",
                            _DP.resolved_at >= _rc_cutoff,
                            (_DP.category == category) | (_DP.category.is_(None)))
                    .all()
                )
                for _r in _rc_rows:
                    if _r.title:
                        rejected_recent.add(_r.title)
                    if _r.tmdb_id is not None:
                        rejected_recent.add(str(_r.tmdb_id))
                if rejected_recent:
                    logger.info("[deletions] %s: keep-cooldown active for %d recently "
                                "rejected title(s) (%dd)", category,
                                len(_rc_rows), _REJECT_COOLDOWN_DAYS)
            except Exception as e:
                guard_failures.append("keep-cooldown")
                logger.warning("[deletions] keep-cooldown load failed: %s", e, exc_info=True)

            # 3. Pass 37: pre-compute the set of candidate titles the curator
            # has positioned on in recent chat. The cold-metadata pitch path
            # doesn't know about prior discussions, so it would happily
            # propose deleting something the curator endorsed two days ago
            # ("you yourself said Nukitashi is the gold standard for
            # transgressive art … but the algorithm now wants to delete it
            # for being superficial") — the user can weaponize that
            # contradiction in the discuss thread, with no defensible
            # comeback from the curator. Skipping these items means we only
            # auto-propose deletion for titles the curator has NOT staked a
            # position on in chat. Manual deletion via the UI still works.
            from src.database.models import ConversationMessage
            from datetime import datetime as _dt, timedelta as _td
            cutoff = _dt.utcnow() - _td(days=60)
            candidate_titles = {item.get("title") for item in arr_items if item.get("title")}
            curator_positioned: set[str] = set()
            # audit 11f: this used to fire one LIKE '%title%' query PER candidate
            # (thousands per category per run). Load the recent assistant text
            # ONCE and substring-match in Python instead.
            try:
                _rows = (db.query(ConversationMessage.content)
                         .filter(ConversationMessage.user_id == user_id,
                                 ConversationMessage.role == "assistant",
                                 ConversationMessage.created_at >= cutoff)
                         .all())
                _blob = " | ".join((r.content or "") for r in _rows)
                if _blob:
                    curator_positioned = positioned_titles(_blob, candidate_titles)
            except Exception as e:
                guard_failures.append("curator stances")
                logger.warning("[deletions] curator-stance scan failed: %s", e, exc_info=True)
            if curator_positioned:
                logger.info(
                    "[deletions] %d/%d candidates skipped — curator already positioned on them in chat: %s",
                    len(curator_positioned), len(candidate_titles),
                    ", ".join(sorted(curator_positioned)[:5])
                    + (", ..." if len(curator_positioned) > 5 else ""),
                )

            # 4. Pass 38: titles the user has actively engaged with in the
            # last 90 days. Like Pass 37 (which catches titles the curator
            # discussed) but watch-history-driven — scales to libraries where
            # the user hasn't chatted about every item but still has clear
            # consumption signals. WatchHistory is the strongest "this is
            # being used" indicator we have; pitching deletion of something
            # the user actively watched would be either contradictory
            # (mid-binge) or premature (just finished). We err conservative:
            # ANY watch activity in 90 days → skip auto-pitch.
            from src.database.models import WatchHistoryEntry
            watched_cutoff = _dt.utcnow() - _td(days=90)
            recent_watched: set[str] = set()
            try:
                rows = (
                    db.query(WatchHistoryEntry.title, WatchHistoryEntry.series_title)
                    .filter(
                        WatchHistoryEntry.user_id == user_id,
                        WatchHistoryEntry.viewed_at >= watched_cutoff,
                        # Media-family guard: without it the owner's ≈350k music
                        # rows flooded this veto set with song/artist names, and any
                        # VIDEO candidate sharing a name with a recently played
                        # track ("American Pie", "Gold", …) was silently vetoed as
                        # "recently watched" — an invisible false protection.
                        (WatchHistoryEntry.media_type == "music"
                         if category == "music"
                         else WatchHistoryEntry.media_type != "music"),
                    )
                    .all()
                )
                for r in rows:
                    if r.title:
                        recent_watched.add(r.title)
                    if r.series_title:
                        recent_watched.add(r.series_title)
            except Exception as e:
                guard_failures.append("recent-watch veto")
                logger.warning("[deletions] watch-history veto check failed: %s", e,
                               exc_info=True)
            # Count overlap with candidate pool — gives a useful "how many of
            # YOUR plex items are recently watched" stat in the log without
            # dumping the whole set (which can be hundreds of titles).
            recent_watched_in_pool = candidate_titles & recent_watched
            if recent_watched_in_pool:
                logger.info(
                    "[deletions] %d/%d candidates skipped — recently watched (last 90d): %s",
                    len(recent_watched_in_pool), len(candidate_titles),
                    ", ".join(sorted(recent_watched_in_pool)[:5])
                    + (", ..." if len(recent_watched_in_pool) > 5 else ""),
                )


        # ── Pass 82c: Plex user-rating lookup for music ──────────────────────────
        # Music-only because Kometa overwrites ``userRating`` on movies/shows
        # with aggregated platform ratings, so it isn't a personal-opinion
        # signal there. Aggregated per artist_name (lowercase) since Lidarr
        # proposals are artist-scoped — a single ≥4-star track is enough to
        # protect the whole artist.
        user_rating_by_artist: dict[str, float] = {}
        if category == "music":
            try:
                from src.database.models import PlexRating
                from sqlalchemy import func as _sqlfunc
                with get_db_session() as _rdb:
                    rating_rows = (
                        _rdb.query(
                            _sqlfunc.lower(PlexRating.artist_name).label("artist_lc"),
                            _sqlfunc.max(PlexRating.rating).label("max_rating"),
                        )
                        .filter(
                            PlexRating.user_id == user_id,
                            PlexRating.artist_name.isnot(None),
                        )
                        .group_by(_sqlfunc.lower(PlexRating.artist_name))
                        .all()
                    )
                user_rating_by_artist = {
                    (r.artist_lc or ""): float(r.max_rating)
                    for r in rating_rows if r.artist_lc
                }
                if user_rating_by_artist:
                    hard_protected_count = sum(
                        1 for v in user_rating_by_artist.values()
                        if v >= HARD_PROTECT_THRESHOLD
                    )
                    logger.info(
                        "[deletions] loaded %d artist ratings (%d at ≥4 stars → hard-protected)",
                        len(user_rating_by_artist), hard_protected_count,
                    )
            except Exception as e:
                guard_failures.append("Plex artist ratings")
                logger.warning("[deletions] artist-rating preload failed: %s", e,
                               exc_info=True)
        try:
            household_tastes = _load_household_tastes(user_id, category)
        except Exception as e:
            logger.warning("[deletions] %s: household taste profiles unreadable - "
                           "scored on the owner's taste alone: %r", category, e)
            household_tastes = []
        return {
            "household_tastes": household_tastes,
            "guard_failures": guard_failures,
            "taste_unreadable": taste_unreadable,
            "lang_directive_str": lang_directive_str,
            "user_vector": user_vector,
            "taste_blurb": taste_blurb,
            "disliked_lc": disliked_lc,
            "feedback_sentiment_lc": feedback_sentiment_lc,
            "cal_anchors": cal_anchors,
            "corpus_mean": corpus_mean,
            "drop_vector": drop_vector,
            "drop_anchors": drop_anchors,
            "taste_clusters": taste_clusters,
            "ts_genres": ts_genres,
            "protected": protected,
            "rejected_recent": rejected_recent,
            "candidate_titles": candidate_titles,
            "curator_positioned": curator_positioned,
            "recent_watched": recent_watched,
            "user_rating_by_artist": user_rating_by_artist,
        }

    _ctx = await asyncio.to_thread(_load_phase_a)
    if _ctx["guard_failures"]:
        _why = ("could not read " + ", ".join(_ctx["guard_failures"])
                + " — nothing proposed rather than judging without them")
        _msg(f"{category}: not proposing deletions — {_why}")
        logger.warning("[deletions] %s: skipped — %s", category, _why)
        _record_deletion_run(category, stopped=_why)
        return []
    lang_directive_str = _ctx["lang_directive_str"]
    user_vector = _ctx["user_vector"]
    taste_blurb = _ctx["taste_blurb"]
    disliked_lc = _ctx["disliked_lc"]
    feedback_sentiment_lc = _ctx["feedback_sentiment_lc"]
    cal_anchors = _ctx["cal_anchors"]
    corpus_mean = _ctx["corpus_mean"]
    drop_vector = _ctx["drop_vector"]
    drop_anchors = _ctx["drop_anchors"]
    taste_clusters = _ctx["taste_clusters"]
    ts_genres = _ctx["ts_genres"]
    protected = _ctx["protected"]
    rejected_recent = _ctx["rejected_recent"]
    candidate_titles = _ctx["candidate_titles"]
    curator_positioned = _ctx["curator_positioned"]
    recent_watched = _ctx["recent_watched"]
    user_rating_by_artist = _ctx["user_rating_by_artist"]
    household_tastes = _ctx.get("household_tastes") or []
    if household_tastes:
        logger.info("[deletions] %s: household taste from %s", category,
                    ", ".join(f"{h['name']} ({h['items']} titles)" for h in household_tastes))
    taste_degraded = ("scored without the " + " and ".join(_ctx["taste_unreadable"])
                      + " — the stored taste profile could not be read; the "
                      "next taste rebuild rewrites it"
                      if _ctx["taste_unreadable"] else "")

    # SoulSync follows (owner decision 2026-10-09): an artist the owner
    # follows there is wanted, and its watchlist would fetch every new release
    # again anyway. SoulSync configured but not answering used to mean "no
    # follow veto this run" — followed artists became deletable. Stop instead.
    followed: set[str] = set()
    from src.services.soulsync_client import norm_name as _ss_norm
    if category == "music":
        from src.services.soulsync_client import watchlist_artists, _configured as _ss_on
        _names = await watchlist_artists()
        if _names is None and _ss_on():
            _why = "could not read the SoulSync watchlist — nothing proposed"
            _msg(f"{category}: not proposing deletions — {_why}")
            logger.warning("[deletions] %s: skipped — %s", category, _why)
            _record_deletion_run(category, stopped=_why)
            return []
        followed = {_ss_norm(n) for n in _names or []}
        _hit = sorted(t for t in candidate_titles if _ss_norm(t) in followed)
        if _hit:
            logger.info("[deletions] %d/%d candidates skipped — followed in SoulSync: %s",
                        len(_hit), len(candidate_titles), ", ".join(_hit[:5])
                        + (", ..." if len(_hit) > 5 else ""))

    _msg(f"{category}: scoring {len(arr_items):,} candidates (ChromaDB + taste vector)…")
    scored_candidates = []
    prelim: list[dict] = []                # all candidates, pre-calibration
    hard_protect_skipped: list[str] = []   # for the post-loop log line
    cosine_failures: list[str] = []        # one WARNING per run, after the loop
    user_vector_n = _normalize_vec(user_vector)   # unit vector for cosine math
    # Centered comparison space (eval 2.4): text embeddings share a common
    # cone; subtracting the corpus mean widens the discriminative range and
    # makes drop repulsion real. A blob carrying corpus_mean guarantees its
    # anchors were computed against the SAME centered space; legacy blobs
    # without it stay uncentered end-to-end — never mixed.
    # A stored mean that is non-finite (one NaN corpus row used to make it so)
    # or from another embedding space must not be used — and the blob's
    # anchors were computed in ITS centered space, so they go with it: score
    # uncentered against batch anchors instead (never mixed).
    _mean_v = None
    if corpus_mean:
        _m = _normalize_vec(corpus_mean)   # finiteness + shape check only
        if _m is not None and (not user_vector or len(user_vector) == len(corpus_mean)):
            _mean_v = np.asarray(corpus_mean, dtype=float)
        else:
            logger.warning("[deletions] %s: stored corpus mean is unusable "
                           "(non-finite or wrong dimension) — scoring uncentered "
                           "against batch anchors until the next taste rebuild",
                           category)
            cal_anchors = drop_anchors = None

    def _cmp_vec(vec):
        vn = _normalize_vec(vec)
        if vn is None or _mean_v is None:
            return vn
        if vn.shape != _mean_v.shape:
            return None
        c = vn - _mean_v
        n = np.linalg.norm(c)
        return c / n if n > 1e-12 else None

    if taste_clusters:
        user_cmps = [v for v in (_cmp_vec(c.get("embedding"))
                                 for c in taste_clusters) if v is not None]
    else:
        _u = _cmp_vec(user_vector) if user_vector else None
        user_cmps = [_u] if _u is not None else []
    drop_cmp = _cmp_vec(drop_vector) if drop_vector else None

    # Cold start: with neither a taste vector nor genre stats for this
    # category, every candidate would be ranked by size and rating alone —
    # the biggest files pitched first, on no evidence about the owner.
    if not user_cmps and not ts_genres:
        if _ctx["taste_unreadable"]:
            # a profile exists but is unreadable — not a cold start
            _why = ("could not read the stored taste profile ("
                    + ", ".join(_ctx["taste_unreadable"])
                    + ") — nothing proposed until the next taste rebuild")
            logger.warning("[deletions] %s: skipped — %s", category, _why)
        else:
            _why = "no taste profile for this category yet"
            logger.info("[deletions] %s: skipped — %s", category, _why)
        _msg(f"{category}: not proposing deletions — {_why}")
        _record_deletion_run(category, stopped=_why)
        return []

    # Bulk-prefetch item embeddings: one Chroma get per 1000 ids instead of
    # one round-trip per item (N+1 — thousands of sequential calls per run;
    # spotted by Jules PR #14). Deliberately UNFILTERED: mirroring the
    # skip-filters below just to shave the fetch list is a desync trap — a
    # filter later edited in only one copy silently costs items their
    # embedding (neutral 0.5 mismatch, invisible); over-fetching skipped
    # items in bulk is effectively free.
    # Both reads run off the event loop: the embeddings on the Chroma thread,
    # the cached ratings as ONE batched cache query (it was a fresh SQLite
    # connection, four PRAGMAs and the schema DDL per candidate, on the loop).
    from src.vector_store.chromadb_wrapper import run_chroma
    _prefetched_embeddings, _ratings_by_key = await asyncio.gather(
        run_chroma(_chroma_get_by_ids, [
            str(it.get("plex_rating_key") or it.get("tmdb_id") or it.get("title"))
            for it in arr_items
        ]),
        asyncio.to_thread(_bulk_cached_ratings, arr_items, category),
    )

    for _idx, item in enumerate(arr_items):
        # Pure CPU from here (filters, one NumPy dot per item) — yield now and
        # then so a library of thousands never holds the loop in one stretch.
        # No transaction is open across this await.
        if _idx and _idx % 250 == 0:
            await asyncio.sleep(0)
        title = item.get("title")
        tmdb_id = str(item.get("tmdb_id")) if item.get("tmdb_id") is not None else ""

        if title in protected or tmdb_id in protected:
            continue

        if followed and _ss_norm(title) in followed:
            continue

        # Zero-byte guard (deletion run 2026-07-08): six of eleven music
        # proposals were Lidarr MONITORING GHOSTS — artists with no files on
        # disk. A deletion pitch that frees 0 bytes is the wrong action
        # entirely (that's an unmonitor decision, not curation); it also
        # burns a judge call and a proposal slot on nothing.
        if not (item.get("size_mb") or 0):
            continue

        # Keep-cooldown (see 2b): the user said "Keep" recently — honor it.
        if title in rejected_recent or tmdb_id in rejected_recent:
            continue

        # Pass 37: skip auto-pitches for titles the curator has discussed
        # in chat — avoids the "your last message praised this, this
        # message proposes deleting it" self-contradiction trap.
        if title and title in curator_positioned:
            continue

        # Pass 38: skip auto-pitches for titles the user has actively
        # consumed in the last 90 days — recent engagement is the
        # strongest "this matters to me" signal, and the cold-metadata
        # mismatch score has no way to see it.
        if title and title in recent_watched:
            continue

        # Pass 39: skip kids/family/children content. Items like Tom and
        # Jerry get evaluated against the user's prestige-TV / anime
        # taste profile and produce absurd pitches ("lacks narrative
        # propulsion / thematic depth"). The format is supposed to be
        # repetitive slapstick — judging it on those axes is a category
        # error. If the user wants to delete kids content, they'll do
        # it manually; the auto-pitch shouldn't waste pixels on it.
        item_genres = item.get("genres", "")
        if isinstance(item_genres, list):
            item_genres = ", ".join(item_genres)
        gl = (item_genres or "").lower()
        if "children" in gl or "family" in gl or "kids" in gl:
            continue

        # Pass 82c: Plex-user-rating gate (music only). Look up the max
        # rating across ANY track/album/artist row tied to this artist
        # name; ≥ 4 stars (Plex 8.0) hard-excludes the candidate. The
        # soft-bias for 0 < rating < 8 is applied further down in the
        # score formula.
        user_rating = None
        if category == "music" and title:
            user_rating = user_rating_by_artist.get(title.lower())
            if user_rating is not None and user_rating >= HARD_PROTECT_THRESHOLD:
                hard_protect_skipped.append(title)
                continue

        # ── Rating: aggregate ALL ARR sources (TMDB + IMDb + RT + Metacritic),
        #          then fall back to enrichment cache (TMDB), then neutral. ──
        arr_rating, rating_breakdown = _aggregate_arr_rating(item)

        # Check enrichment cache at scoring time so well-rated items are never
        # unfairly proposed just because their ARR metadata field is empty/zero.
        _, cached_rating, _ = (_ratings_by_key.get(_rating_cache_key(item, category))
                               or (None, None, []))

        if arr_rating > 0:
            # ARR has multi-source data — that's our richest signal.
            effective_rating = arr_rating
        elif cached_rating and cached_rating > 0:
            # Pass 54: explicit > 0 guard. ``_rating_from_cached`` already
            # returns None for a zero/absent rating, but keeping the check
            # here too means a future refactor of that helper can't
            # silently let a 0 back in as a real score.
            effective_rating = cached_rating
            rating_breakdown = {"tmdb_cache": round(cached_rating, 2)}
        else:
            # No rating data — use neutral to avoid punishing unknown-quality items.
            # 5.5 ≈ "slightly below average" on 0-10; enough protection to avoid
            # false-positive proposals while still allowing genuine trash through.
            # This is the branch obscure small titles (no TMDB votes, AniList
            # averageScore still null) land in after Pass 54 — they get judged
            # on taste-fit alone, not punished for a phantom 0/10.
            effective_rating = 5.5
            rating_breakdown = {}

        size_gb = (item.get("size_mb", 0) or 0) / 1024

        # ── Semantic taste-mismatch: proper cosine (user taste vector vs the
        # item's ChromaDB embedding). The lookup id is the enrichment-pipeline
        # key "{service}:{arr_id}", carried on the candidate as plex_rating_key
        # (Pass 64). Both vectors MUST be normalised: the stored embeddings are
        # raw (norm ~13), so a bare np.dot against the unit taste vector landed
        # at ≈ 8-14 — far outside [-1, 1] — which the old ``1 - dist`` clamp
        # pinned to 0, silently killing the dominant deletion signal. The cosine
        # is mapped to a 0-1 mismatch AFTER the loop, calibrated against this
        # batch's own (anisotropic, narrow) cosine distribution.
        doc_id = str(item.get("plex_rating_key") or item.get("tmdb_id") or title)
        item_vector_res = _prefetched_embeddings.get(doc_id)
        item_vec = item_vector_res.get("embedding") if item_vector_res else None
        cosine = None
        drop_cos = None
        if item_vec is not None and user_cmps:
            try:
                iv_c = _cmp_vec(item_vec)
                if iv_c is not None:
                    # MAX over centroids: one strong interest match counts,
                    # a blend of unrelated interests doesn't dilute it.
                    cosine = max(float(np.dot(u, iv_c)) for u in user_cmps)
                    if drop_cmp is not None:
                        drop_cos = float(np.dot(drop_cmp, iv_c))
            except Exception as e:
                # counted, reported once after the loop: per-item warnings
                # would flood the log on one bad shape across the library
                cosine_failures.append(f"{title!r}: {e!r}")
                cosine = None
        # A non-finite cosine is no taste evidence: it falls to the genre /
        # no-taste branch below instead of mapping to maximum mismatch.
        if cosine is not None and not math.isfinite(cosine):
            cosine = None
        if drop_cos is not None and not math.isfinite(drop_cos):
            drop_cos = None

        hh_fits = _household_fits(item_vec, household_tastes) if (
            item_vec is not None and household_tastes) else []
        prelim.append({
            "item": item,
            "cosine": cosine,
            "drop_cos": drop_cos,
            "hh_fits": hh_fits,
            "size_gb": size_gb,
            "rating": effective_rating,
            "rating_breakdown": rating_breakdown,
            "arr_rating": arr_rating,
            "cached_rating": cached_rating,
            "user_rating": user_rating,
        })

    if cosine_failures:
        logger.warning("[deletions] %s: taste cosine failed for %d candidate(s) — "
                       "scored without taste fit (first: %s)",
                       category, len(cosine_failures), cosine_failures[0])

    # Pass 82c: audit trail — log which artists were hard-protected by the
    # user's ≥ 4-star Plex rating. Helps explain "why isn't <artist> in the
    # deletion list?" without forcing the user to dig through the DB.
    if hard_protect_skipped:
        preview = ", ".join(sorted(hard_protect_skipped)[:5])
        more = f", +{len(hard_protect_skipped) - 5} more" if len(hard_protect_skipped) > 5 else ""
        logger.info(
            "[deletions] hard-protected by ≥ 4-star user rating: %d artist(s) — %s%s",
            len(hard_protect_skipped), preview, more,
        )

    # ── Calibrate taste-mismatch against THIS batch's cosine distribution ────
    # Embedding cosines are anisotropic — they cluster in a narrow band, so a
    # raw (1 - cosine) hardly varies. Stretch the batch's p10..p90 band onto
    # [0, 1] so the mismatch — and the resulting confidence — spreads across the
    # full range and reflects real RELATIVE taste-fit. Items with no embedding
    # get a neutral 0.5. Scoring (size + rating swings) is unchanged from here.
    _cos_vals = [p["cosine"] for p in prelim if p["cosine"] is not None]
    if cal_anchors:
        # Anchors from the last taste rebuild: computed once against the
        # WHOLE category corpus, so the same item scores the same across
        # runs regardless of batch composition.
        _lo, _hi = cal_anchors
        _anchor_src = "global"
    else:
        _lo, _hi = _cosine_anchors(_cos_vals)
        _anchor_src = "batch"
    logger.info(
        "[deletions] %s: %d/%d candidates have embeddings; cosine anchors "
        "p10=%.3f p90=%.3f (%s)",
        category, len(_cos_vals), len(prelim), _lo, _hi, _anchor_src,
    )
    # Tech-profile index for outlier-based size scoring — loaded once (a DB query
    # per candidate would be thousands). Empty when the tech sync hasn't run yet,
    # in which case size_pts falls back to today's blanket log1p below.
    from src.services.size_norms import load_tech_index, size_outlier
    _tech_idx = await asyncio.to_thread(load_tech_index)
    # Owner listening depth (music): ONE grouped query → per-artist plays by
    # folded name and by mbid. The score needs it for every candidate — the
    # per-item music_listening_stats call would be hundreds of round trips.
    _plays_by_name: dict = {}
    _plays_by_mbid: dict = {}
    _variants_fn = None
    if category == "music":
        try:
            from sqlalchemy import func as _f, or_ as _or
            from src.database.connection import get_db_session as _gds
            from src.database.models import WatchHistoryEntry as _W
            from src.services.watch_status import _artist_variants as _variants_fn

            def _load_play_rows() -> list:
                # A GROUP BY over every music play (≈350k rows) — on a
                # worker thread, not the loop.
                with _gds() as _pdb:
                    # Skipped tracks are excluded (see REAL_LISTEN_MS): they used to
                    # count as listening depth, which handed deletion protection to
                    # artists the user demonstrably keeps skipping. Only the margins
                    # move — heavy rotation is capped by _play_protection anyway —
                    # but the margins are exactly who gets pitched for deletion.
                    return (_pdb.query(_f.lower(_W.series_title),
                                       _W.artist_mbid, _f.count(_W.id))
                            .filter(_W.user_id == user_id,
                                    _W.media_type == "music",
                                    _or(_W.completed == True,  # noqa: E712
                                        _W.view_offset_ms >= REAL_LISTEN_MS))
                            .group_by(_f.lower(_W.series_title),
                                      _W.artist_mbid)
                            .all())

            for _nm, _mb, _n in await asyncio.to_thread(_load_play_rows):
                if _nm:
                    _plays_by_name[_nm] = _plays_by_name.get(_nm, 0) + _n
                if _mb:
                    _plays_by_mbid[_mb] = _plays_by_mbid.get(_mb, 0) + _n
        except Exception as _e:
            # The play map IS the listening-depth protection: without it
            # every heavily played artist would score as if never played.
            _why = "could not read listening depth — nothing proposed rather than judging without it"
            logger.warning("[deletions] music: play-map build failed: %s", _e, exc_info=True)
            _msg(f"{category}: not proposing deletions — {_why}")
            _record_deletion_run(category, stopped=_why, degraded=taste_degraded)
            return []
    _play_protected: list = []
    _taste_src_counts = {"embedding": 0, "genre": 0, "none": 0}
    for p in prelim:
        # Taste source (eval 1.7): items without an embedding are missing
        # NOT at random (enrichment failures, keying epochs, obscure titles)
        # — a silent neutral 0.5 pinned them all mid-range. Genre affinity
        # is a real, if coarser, taste signal; only items with neither get
        # the neutral value, now with a visible reason code.
        if p["cosine"] is not None:
            mismatch = _cosine_to_mismatch(p["cosine"], _lo, _hi)
            taste_src = "embedding"
        else:
            _genres = [g.strip().lower()
                       for g in (p["item"].get("genres") or "").split(",")
                       if g.strip()]
            _overlap = sum(ts_genres.get(g, 0) for g in _genres)
            if ts_genres and _genres:
                # damped to [0.3, 0.6] — coarse metadata must not produce
                # extreme verdicts either way (the old 0.85 ceiling scored a
                # title outside the top-20 genres at 68 points)
                mismatch = 0.6 - 0.3 * min(1.0, _overlap)
                taste_src = "genre"
            else:
                # no evidence: held under the shortlist bar (NO_TASTE_MISMATCH)
                mismatch = NO_TASTE_MISMATCH
                taste_src = "none"
        _taste_src_counts[taste_src] += 1
        # Vector mismatch is the dominant signal (0-80). Size is logarithmic so
        # huge files don't dominate; rating swing is protective above 5/10 and
        # penalising below. USER rating swing (music only) weights the user's own
        # star rating heaviest. Same coefficients as before — only the mismatch
        # input is now a real, calibrated cosine instead of a flat constant.
        # Size penalty — OUTLIER-based, not blanket. A 4K film or a many-specials
        # series that is normal-per-minute for its class gets ~0; only genuine
        # bitrate-bloat is penalised (capped at 25 so it never dominates the taste
        # mismatch). Falls back to the old log1p blanket when no tech profile
        # exists (tech sync not run / item not in Plex), so nothing regresses.
        _it = p["item"]
        _tp = (_tech_idx.get(("tmdb", _it.get("tmdb_id")))
               or _tech_idx.get(("tvdb", _it.get("tvdb_id"))))
        # The first call loads the norms table (main DB), the rest hit its cache.
        _so = await run_read(size_outlier, _tp["media_type"], _tp["resolution"], _tp["codec"],
                             _tp["mb_per_min"],
                             is_remux=_tp.get("is_remux", False)) if _tp else None
        if _so:
            size_pts = (min(SIZE_PTS_CAP, (_so["ratio"] - 1.0) * 12.0)
                        if _so["verdict"] == "bloated" else 0.0)
        else:
            size_pts = min(SIZE_PTS_CAP, math.log1p(p["size_gb"]) * 5)
        rating_swing = (p["rating"] - 5.0) * 4
        user_rating_swing = (
            (p["user_rating"] - 5.0) * 6 if p["user_rating"] is not None else 0.0
        )
        # Explicit owner feedback (taste blob): recorded dislike pushes the
        # candidate toward a pitch, recorded praise pulls it away. Soft on
        # purpose — well below the mismatch signal and the consider cap.
        _tl = (p["item"].get("title") or "").lower()
        _sent, _fb_weight = feedback_sentiment_lc.get(_tl) or (None, 1.0)
        # Latest statement wins: an explicit positive verdict overrides an
        # older entry on the dislike list (the OR used to make dislikes a
        # ratchet no later praise could undo). The bare dislike-list check
        # only fires for titles WITHOUT any recorded feedback statement.
        if _sent == "negative":
            feedback_swing = 15.0 * min(_fb_weight, 2.0)
        elif _sent == "positive":
            feedback_swing = -15.0 * min(_fb_weight, 2.0)
        elif _tl in disliked_lc:
            feedback_swing = 15.0
        else:
            feedback_swing = 0.0
        # Drop-centroid penalty (eval 2.3): similarity to the user's
        # ABANDONED items pushes toward deletion — the scoring-side form
        # of repulsion the in-centroid subtraction never delivered.
        # Stretched by the drop centroid's own global anchors; capped at
        # 20 points, deliberately soft under the 80-point mismatch.
        drop_penalty = 0.0
        if p.get("drop_cos") is not None and drop_anchors:
            d_lo, d_hi = drop_anchors
            drop_penalty = 20.0 * max(0.0, min(1.0, (p["drop_cos"] - d_lo)
                                               / (d_hi - d_lo)))
        # Listening-depth protection (music): counts what the judge's evidence
        # line always showed but the score ignored.
        play_prot = 0.0
        if category == "music":
            _pl = _plays_by_mbid.get(_it.get("musicbrainz_id") or "", 0)
            if not _pl and _variants_fn and _plays_by_name:
                for _v in _variants_fn(_it.get("title") or ""):
                    _pl = max(_pl, _plays_by_name.get(_v, 0))
            play_prot = _play_protection(_pl)
            if play_prot >= 10.0:
                _play_protected.append((_it.get("title"), _pl, round(play_prot, 1)))
        # Household taste: only against the owner's EMBEDDING mismatch (the
        # members' fit is an embedding measure too).
        hh_pts, hh_name = 0.0, None
        if taste_src == "embedding" and p.get("hh_fits"):
            _n, _fit, _w = max(p["hh_fits"], key=lambda f: f[1] * f[2])
            hh_pts, hh_name = mismatch * 80 * _w * _fit, _n
            _sn, _sfit, _ = max(p["hh_fits"], key=lambda f: f[1])
            if _sfit >= HOUSEHOLD_TASTE_STRONG_FIT:
                p["item"]["_household_taste"] = {"name": _sn, "fit": round(_sfit, 2)}
        del_score = mismatch * 80 + size_pts - rating_swing - user_rating_swing + feedback_swing + drop_penalty - play_prot - hh_pts
        if del_score > SHORTLIST_SCORE:
            scored_candidates.append({
                "item": p["item"],
                "score": del_score,
                # The same terms, kept apart for the card's "Why?" (the
                # keep-rules term is added below, once it is known).
                "score_terms": dict(
                    mismatch=mismatch, taste_src=taste_src, size_pts=size_pts,
                    size_gb=round(p["size_gb"], 1),
                    size_ratio=(_so["ratio"] if _so and _so["verdict"] == "bloated" else None),
                    rating=p["rating"], rating_swing=rating_swing,
                    user_rating=p["user_rating"], user_rating_swing=user_rating_swing,
                    feedback_swing=feedback_swing, drop_penalty=drop_penalty,
                    play_prot=play_prot,
                    plays=(_pl if category == "music" else 0),
                    household_taste_pts=hh_pts, household_name=hh_name,
                ),
                "mismatch": mismatch,
                "taste_source": taste_src,   # embedding / genre / none
                "rating_breakdown": p["rating_breakdown"],
                "rating": p["rating"],
                "arr_rating": p["arr_rating"],
                "cached_rating": p["cached_rating"],
                "user_rating": p["user_rating"],   # Plex stars (music) for the pitch
            })

    if _taste_src_counts["genre"] or _taste_src_counts["none"]:
        logger.info(
            "[deletions] %s taste sources: %d embedding, %d genre-fallback, "
            "%d without any taste signal (neutral 0.5)",
            category, _taste_src_counts["embedding"],
            _taste_src_counts["genre"], _taste_src_counts["none"],
        )
    if _play_protected:
        logger.info(
            "[deletions] listening-depth protection on %d artist(s): %s",
            len(_play_protected),
            ", ".join(f"{t} ({n} plays, -{pts})" for t, n, pts in _play_protected[:8]),
        )

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── Learned considerations (the "three pillars" and beyond) ───────────────
    # Let the user's standing "keep / value" preferences — kept franchises,
    # partner favourites, cultural value, and ANY other keep-feedback they've
    # given — pull a candidate DOWN the deletion list. Retrieved PER ITEM so a
    # principle the user stated about ONE title generalises to new, never-
    # discussed ones (the whole point: the curator should learn from feedback,
    # not just remember it). SOFT and capped: a strong taste mismatch still
    # surfaces; this never hard-protects. Only the slice the judge can reach
    # (JUDGE_CAP) is scored, to bound the per-item embedding calls — it was
    # [:30] while the judge walked to 60, so ranks 31-60 were judged without
    # their keep-value deductions.
    from src.services.episodic_memory import retrieve_considerations_many
    _CONSIDER_CAP = 45.0
    # The run's REACH: reused verdicts let it walk past judge_cap (up to
    # REUSE_FACTOR x more), and every title it can reach gets its
    # keep-value scored - the same slice bug the comment above describes.
    _reach = judge_cap * (1 + REUSE_FACTOR)
    _consider_slice = scored_candidates[:_reach]
    _profiles = []
    for cand in _consider_slice:
        it = cand["item"]
        g = it.get("genres") or it.get("genre") or ""
        if isinstance(g, list):
            g = ", ".join(str(x) for x in g)
        _profiles.append(" — ".join(p for p in (
            str(it.get("title") or ""),
            str(g),
            (it.get("overview") or it.get("synopsis") or "")[:200],
        ) if p.strip()))
    # ONE batched lookup for the whole slice: the queries share /api/embed
    # calls and the memories are decoded once (it was a serial embed + full
    # memory read per candidate).
    try:
        _cons_all = await retrieve_considerations_many(
            user_id, _profiles, media_category=category, top_k=3)
    except Exception as e:
        # No lookup is not "no keep-value": deferred to the next scan
        # instead of being judged without its learned protections.
        logger.warning("[deletions] considerations lookup failed — "
                       "deferred: %s", e)
        _cons_all = None
    for _ci, cand in enumerate(_consider_slice):
        if _cons_all is None:
            cand["considerations_failed"] = True
            continue
        cons = _cons_all[_ci]
        keep_pts = round(min(_CONSIDER_CAP, sum(c["strength"] for c in cons) * 30.0), 1)
        cand["considerations"] = cons
        cand["keep_value_pts"] = keep_pts
        cand["score"] = cand["score"] - keep_pts
    _cons_failed = sum(1 for c in _consider_slice if c.get("considerations_failed"))
    if _consider_slice and _cons_failed == len(_consider_slice):
        _why = "learned keep-considerations could not be read (embedding or vector store down)"
        _msg(f"{category}: not proposing deletions — {_why}")
        _record_deletion_run(category, stopped=_why, degraded=taste_degraded)
        return []
    if _cons_failed:
        scored_candidates = [c for c in scored_candidates
                             if not c.get("considerations_failed")]
    if any(c.get("keep_value_pts") for c in _consider_slice):
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)
        _before = len(scored_candidates)
        scored_candidates = [c for c in scored_candidates if c["score"] > SHORTLIST_SCORE]
        _dropped = _before - len(scored_candidates)
        _protected = [(c["item"].get("title"), c["keep_value_pts"])
                      for c in _consider_slice if c.get("keep_value_pts")]
        logger.info(
            "[deletions] learned considerations nudged %d item(s); %d fell below "
            "the bar (soft-kept). Protected: %s",
            len(_protected), _dropped, _protected[:8],
        )

    final_proposals = []

    # ── 4-PILLAR JUDGE PATH (PILLARS_ENABLED) ─────────────────────────────────
    # The LLM is the judge; the del_score above only PRE-RANKS who gets looked
    # at. Walk the ranking and rule on each candidate:
    #   CUT / STAGNANT → a deletion proposal (lazy monologue as the pitch);
    #                    STAGNANT carries stagnant=True so the card flags it as a
    #                    "your call" review rather than a hard cut
    #   HARD_KEEP /    → persisted to ProtectedMedia(source='judge') ONLY when a
    #   KEEP_WITH_FLAG    higher pillar (Household / Custodian / Resonance) keeps
    #                     it; a bare Ego(0) taste-match is NOT persisted (the
    #                     owner's own edge — re-judged each scan, never buried).
    #                     Persisted titles drop out of every future scan (phase A
    #                     excludes ALL ProtectedMedia) — the funnel self-leans.
    #   EVALUATE       → left alone, re-judged next scan
    # Keep judging DOWN the ranking until TARGET_CUTS proposals exist OR
    # JUDGE_CAP candidates are judged (a latency bound for a clean library).
    # Two-bake split: the WHOLE deletion block (judge + monologue) runs on the
    # pitcher bake when configured+installed, else the curator bake. Resolved
    # ONCE per run (install probe included — a bad name must fall back
    # visibly, not silently EVALUATE every candidate) and logged so the task
    # monitor shows which bake produced the pitches.
    from src.services.llm_priority import resolve_pitcher_model
    from src.services.ollama_hosts import PcUnavailable
    # The host, like the bake, is resolved ONCE per run: the evidence is
    # budgeted for its window and constitution, and verdict and pitch must
    # come from the same machine - the one the bake is looked up on.
    try:
        judge_host = host_for("judge")
    except PcUnavailable as e:
        # Split install: deletion verdicts wait for the node, never
        # the NAS's small model. The callers check judge_ready() first;
        # this catches a PC that left in between. Nothing is written.
        logger.info("[deletions] %s: %s - run deferred", category, e)
        from src.services.pc_host import node_name
        _msg(f"{category}: waiting for {node_name()}")
        # Recorded as a stopped run: its stored proposals must stay (the
        # callers supersede only what a run actually judged).
        _record_deletion_run(category, stopped=f"waiting for {node_name()}")
        return []
    pitch_model = await resolve_pitcher_model(judge_host.url)
    from src.services import verdict_cache
    # Stored verdicts are valid only for these exact weights.
    pitch_digest = await verdict_cache.bake_digest(judge_host.url, pitch_model)
    _msg(f"{category}: deletion run model: {pitch_model}")
    logger.info("[deletions] %s: run model = %s on %s (%s profile)", category,
                pitch_model, judge_host.url, judge_host.profile)

    if getattr(settings, "PILLARS_ENABLED", False):
        from collections import Counter
        from src.services.pillars import build_evidence, adjudicate, write_monologue
        from src.services.llm_errors import DESCRIPTIONS, INFRA, LLMFailure
        from src.services.llm_priority import (curator_start, curator_done,
                                               evict_if_resident, gate_contested,
                                               wait_quiet)
        # A game already running: no warm-up (it can load the summariser) and
        # no gate. The scheduler's own check runs once per job; a manual
        # Analyze and every category after the first land here.
        _lane_ok, _lane_why = _judge_lane()
        if not _lane_ok:
            _msg(f"{category}: not judging — {_lane_why}")
            logger.info("[deletions] %s: lane closed before the run (%s)",
                        category, _lane_why)
            _record_deletion_run(category, stopped=_lane_why, degraded=taste_degraded)
            return []
        judged = 0
        reused = 0                # verdicts taken from verdict_cache
        thin_skipped = 0
        misfiled_skipped = 0
        unchecked_skipped = 0
        watch_unknown_skipped = 0
        signal_unknown_skipped = 0
        # Why titles got no verdict, by llm_errors.LLMFailure value. A real
        # EVALUATE is not in here: the model looked and could not decide.
        fail_kinds: Counter = Counter()
        consecutive_outages = 0
        household_floored = 0
        stopped = ""
        _msg(f"{category}: scoring done ({len(scored_candidates):,} above threshold) "
             f"— pillar-judging the ranking…")
        # PRE-JUDGE SIGNIFICANCE WARM-UP: the judge's own JIT is summarizer-
        # gated (it holds the curator gate, and a summarizer call there
        # evicts the resident model), so it only ever SEES cached Wikipedia
        # significance — and the backfill walker is slow for music/movies.
        # Result: C418 was judged "functionally invisible" with the
        # Minecraft-composer article sitting unfetched. Warm exactly the
        # candidates about to be judged, BEFORE the gate is taken: the
        # summarizer loads once, distills the batch, then hands the GPU to
        # the curator. Cached/checked titles cost one cache read.
        # Reception is warmed in the same pass. It used to be left to the
        # background walker, so a title could be judged with no community
        # reception on file at all — and the Level-2 discussion, which DOES
        # top it up, then argued from better data than the verdict it was
        # arguing about. Reception costs no summarizer time (plain HTTP), so
        # there is no GPU reason to skip it here either.
        try:
            from src.services.enrichment_state import log_topup_failure
            from src.services.media_enricher import topup_significance
            from src.services.reception import topup_reception
            from src.services.wikidata import topup_wikidata
            from src.services.subtitle_signals import topup_subtitle_metrics
            _warm_slice = scored_candidates[:judge_cap]
            _wc = Counter()

            def _ids_of(_it):
                return dict(tmdb_id=_it.get("tmdb_id"), tvdb_id=_it.get("tvdb_id"),
                            plex_rating_key=_it.get("plex_rating_key"),
                            year=_it.get("year"))

            async def _warm_significance():
                # Serial on purpose: the summarizer loads once and distills
                # the batch title by title.
                for _c in _warm_slice:
                    _it = _c["item"]
                    try:
                        if await asyncio.wait_for(topup_significance(
                                _it.get("title"), category, **_ids_of(_it)),
                                timeout=20.0):
                            _wc["sig"] += 1
                    except Exception as _e:
                        # A miss stays unchecked and the judge defers the title.
                        log_topup_failure(logger, "[deletions] warm-up significance",
                                          _it.get("title"), _e)

            # Reception, dialogue metrics and Wikidata are plain HTTP / Plex
            # reads with no GPU in them, and each patches only its own cache
            # fields (write_fields) — so they run alongside the summarizer
            # pass and a few titles at a time instead of one after another
            # (60 titles x three round-trips of up to 20-25 s each, serially).
            _net_sem = asyncio.Semaphore(4)

            async def _warm_net(_it):
                async with _net_sem:
                    _ids = _ids_of(_it)
                    try:
                        if await asyncio.wait_for(topup_reception(
                                _it.get("title"), category, **_ids), timeout=20.0):
                            _wc["rec"] += 1
                    except Exception as _e:
                        log_topup_failure(logger, "[deletions] warm-up reception",
                                          _it.get("title"), _e)
                    # Dialogue metrics: one Plex read per candidate, no LLM, no
                    # GPU. Cheap enough to sit here, and this is the only place
                    # that can fetch them — build_evidence runs inside the gate
                    # and may only read what is already on disk.
                    try:
                        if await asyncio.wait_for(topup_subtitle_metrics(
                                _it.get("title"), category,
                                tmdb_id=_it.get("tmdb_id"),
                                tvdb_id=_it.get("tvdb_id")), timeout=25.0):
                            _wc["sub"] += 1
                    except Exception as _e:
                        log_topup_failure(logger, "[deletions] warm-up dialogue",
                                          _it.get("title"), _e)
                    try:
                        if await asyncio.wait_for(topup_wikidata(
                                _it.get("title"), category,
                                tmdb_id=_it.get("tmdb_id"), tvdb_id=_it.get("tvdb_id"),
                                plex_rating_key=_it.get("plex_rating_key")),
                                timeout=25.0):
                            _wc["wd"] += 1
                    except Exception as _e:
                        log_topup_failure(logger, "[deletions] warm-up wikidata",
                                          _it.get("title"), _e)

            await asyncio.gather(_warm_significance(),
                                 *(_warm_net(_c["item"]) for _c in _warm_slice))
            _warmed, _warmed_rec, _warmed_wd, _warmed_sub = (
                _wc["sig"], _wc["rec"], _wc["wd"], _wc["sub"])
            if _warmed or _warmed_rec or _warmed_wd or _warmed_sub:
                _msg(f"{category}: warmed {_warmed} significance, "
                     f"{_warmed_rec} reception, {_warmed_wd} on-record facts, "
                     f"{_warmed_sub} dialogue profiles")
                logger.info("[deletions] %s: pre-judge warm-up added %d article(s), "
                            "%d reception record(s), %d wikidata record(s), "
                            "%d dialogue profile(s)",
                            category, _warmed, _warmed_rec, _warmed_wd,
                            _warmed_sub)
        except Exception as _e:
            # Judging goes on with what is cached; the evidence gates defer
            # anything the warm-up was meant to fill.
            logger.warning("[deletions] pre-judge warm-up failed: %s", _e,
                           exc_info=True)
        _gate_label = f"deletion scan: {category}"
        # Two-bake split: the run's model is NOT the one chat answers on, so
        # every yield to a chat swaps ~20 GB each way. Re-acquiring the
        # instant the chat turn ends evicted the curator the user was still
        # talking to — and their follow-up evicted the pitcher again, two
        # full swaps per message. After a yield the run waits for the
        # conversation to settle before it takes the GPU back.
        _split_run = pitch_model != (settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL)
        _YIELD_QUIET_S = 90.0 if _split_run else 0.0
        _run_lane = await curator_start(_gate_label, exclusive_model=pitch_model, role="judge")
        # Whether THIS run currently owns a slot. The yield below drops it and
        # re-acquires; a cancel (scan stopped) landing in that re-acquire
        # leaves us holding nothing, and an unconditional curator_done() in
        # the finally would release the slot of whoever holds it now (the
        # waiting chat) — letting a third caller onto the GPU alongside it.
        _holds_gate = True
        # Evidence for the NEXT candidate is assembled while the GPU decodes
        # this one's verdict (DB reads, two CPU-embedder calls, cache reads —
        # none of it needs the model). {index: Task}; drained in the finally.
        _ev_ahead: dict = {}
        try:
            for _ci, cand in enumerate(scored_candidates):
                if (len(final_proposals) >= target_cuts or judged >= judge_cap
                        or _ci >= _reach):
                    break
                # YIELD between verdicts: a funnel run holds the gate for many
                # minutes; an interactive chat queued behind it should get the
                # GPU within ONE verdict, not after the whole category. The
                # semaphore wakes waiters FIFO, so release→re-acquire puts us
                # behind the waiting chat and we resume when it finishes.
                # exclusive_model on the RE-acquire is what evicts the chat's
                # curator again after an interjection — without it the next
                # adjudicate would load the pitcher AGAINST resident gemma.
                if gate_contested(_run_lane):
                    _msg(f"{category}: yielding GPU to a waiting request…")
                    _holds_gate = False
                    curator_done(lane=_run_lane)
                    await wait_quiet(_YIELD_QUIET_S)
                    _run_lane = await curator_start(_gate_label, exclusive_model=pitch_model, role="judge")
                    _holds_gate = True
                # Checked AFTER a yield too: the chat we waited for can take
                # minutes, and a game started meanwhile is still a game.
                _lane_ok, _lane_why = _judge_lane()
                if not _lane_ok:
                    stopped = _lane_why
                    _msg(f"{category}: stopping — {_lane_why}; the rest is "
                         f"judged on the next run")
                    logger.info("[deletions] %s: lane closed mid-run (%s) — "
                                "stopping cleanly", category, _lane_why)
                    break
                if consecutive_outages >= _OUTAGE_STOP_AFTER:
                    _top = fail_kinds.most_common(1)[0][0]
                    stopped = DESCRIPTIONS[LLMFailure(_top)]
                    _msg(f"{category}: stopping — {stopped}")
                    logger.warning("[deletions] %s: %d outages in a row (%s) — "
                                   "stopping the run", category,
                                   consecutive_outages, _top)
                    break
                item = cand["item"]
                judged += 1
                _msg(f"{category}: pillar-judging {judged} "
                     f"({len(final_proposals)}/{target_cuts} flagged) — "
                     f"{(item.get('title') or '?')[:50]}")
                try:
                    # No session passed: build_evidence reads the DB in a short
                    # session of its own and closes it before its network calls.
                    _pre = _ev_ahead.pop(_ci, None)
                    ev = await (_pre if _pre is not None
                                else build_evidence(item, user_id, category,
                                                    host=judge_host))
                except Exception as e:
                    # Our code, not the model: no LLM call was made, so it
                    # neither counts as judged nor resets the outage streak.
                    logger.warning("[deletions] evidence failed for %r: %s",
                                   item.get("title"), e)
                    fail_kinds[LLMFailure.EVIDENCE.value] += 1
                    judged -= 1
                    continue
                try:
                    # Start the NEXT candidate's evidence now, before the skip
                    # gates below: a skipped candidate `continue`s past the
                    # judge call, and with the prefetch placed after the gates
                    # the following candidate's evidence (DB reads, embedder,
                    # network top-ups) was then built synchronously INSIDE the
                    # GPU slot. Not while Ollama is failing: the breaker above
                    # must be able to stop the run before any new work starts.
                    _nx = _ci + 1
                    if (consecutive_outages == 0 and _nx < len(scored_candidates)
                            and _nx not in _ev_ahead):
                        _ev_ahead[_nx] = asyncio.ensure_future(build_evidence(
                            scored_candidates[_nx]["item"], user_id, category,
                            host=judge_host))
                    # THIN-EVIDENCE GATE: no enrichment, no Wikipedia — only
                    # the arr synopsis stub. Judging that produces confident
                    # confabulation (the They Will Kill You / Buffaloed
                    # mis-pitches: invented "painfully safe" verdicts on a
                    # Sokolov splatter film). Skip until the enrichment
                    # walker has reached the title; it re-enters the funnel
                    # on the next scan with real facts. Costs no LLM call.
                    if (ev.get("flags") or {}).get("evidence_thin"):
                        thin_skipped += 1
                        judged -= 1
                        logger.info("[deletions] %s: skipping %r — no "
                                    "enrichment on file yet (thin-evidence "
                                    "gate)", category, item.get("title"))
                        continue
                    # MISFILED-RECORD GATE: the profile on file belongs to a
                    # different work (Sonarr's tmdbId for the BBC documentary
                    # "Museum of Life" was one digit short and resolved to a
                    # 1999 Japanese melodrama). The judge WILL notice the
                    # contradiction — and then reason from it, which is how a
                    # documentary came to be proposed for deletion because its
                    # own metadata was wrong. Skip; the re-resolution runs with
                    # the tvdb-authoritative id and it re-enters next scan.
                    if (ev.get("flags") or {}).get("evidence_mismatched"):
                        misfiled_skipped += 1
                        judged -= 1
                        logger.warning("[deletions] %s: skipping %r — the "
                                       "profile on file is another work "
                                       "(misfiled-record gate)",
                                       category, item.get("title"))
                        continue
                    # UNCHECKED-SIGNIFICANCE GATE: the same principle one
                    # layer deeper. Partial enrichment dodges the thin gate,
                    # but if significance was never SUCCESSFULLY checked the
                    # Custodian pillar is blind and "no documented stature"
                    # is an artifact of our own pipeline, not a fact about
                    # the work. The warm-up above just tried to fetch it; a
                    # definitive "no article exists" stamps checked and IS
                    # judgeable — only the transient/unfetched case defers,
                    # and the title re-enters the funnel next scan.
                    if (ev.get("flags") or {}).get("significance_unchecked"):
                        unchecked_skipped += 1
                        judged -= 1
                        logger.info("[deletions] %s: deferring %r — "
                                    "significance never checked; not judging "
                                    "a title on data we don't have yet",
                                    category, item.get("title"))
                        continue
                    # UNREADABLE-WATCH-DATA GATE: a failed lookup of who watched
                    # it is not "nobody watched it". Pillar III is the sacred
                    # pillar, and the owner's own plays are the strongest CUT
                    # signal there is — neither is guessed at. Deferred; the
                    # title re-enters the funnel next scan.
                    _fl = ev.get("flags") or {}
                    if _fl.get("household_unknown") or _fl.get("owner_watch_unknown"):
                        watch_unknown_skipped += 1
                        judged -= 1
                        logger.warning("[deletions] %s: deferring %r — watch "
                                       "history could not be read",
                                       category, item.get("title"))
                        continue
                    # UNREADABLE-OWNER-SIGNAL GATE: the owner's recorded
                    # verdicts, keep-reasons and taught principles are what
                    # keep a valued title from being cut on taste alone. Not
                    # being able to read them is not "the owner never said".
                    if _fl.get("owner_signal_unknown"):
                        signal_unknown_skipped += 1
                        judged -= 1
                        logger.warning("[deletions] %s: deferring %r — the "
                                       "owner's feedback, keep-reasons or "
                                       "principles could not be read",
                                       category, item.get("title"))
                        continue
                    # A verdict the model already gave on these exact inputs
                    # is reused: same answer, no GPU. It does not spend the
                    # cap (up to REUSE_FACTOR x the cap), so a run reaches
                    # further down the ranking - the reserve.
                    _vkey = verdict_cache.key(ev["facts"], ev.get("law_extra", ""),
                                              pitch_model, pitch_digest, judge_host)
                    verdict = await asyncio.to_thread(verdict_cache.get_verdict, _vkey)
                    if verdict is not None:
                        reused += 1
                        if reused <= judge_cap * REUSE_FACTOR:
                            judged -= 1
                    else:
                        verdict = await adjudicate(ev["facts"], model=pitch_model,
                                                   skip_priority=True,
                                                   law_extra=ev.get("law_extra", ""),
                                                   host=judge_host)
                        await asyncio.to_thread(verdict_cache.put_verdict, _vkey, verdict)
                except Exception as e:
                    # adjudicate never raises by contract; this is a bug guard.
                    logger.warning("[deletions] pillar judge failed for %r: %s",
                                   item.get("title"), e)
                    fail_kinds["unknown"] += 1
                    continue
                # adjudicate() turns its own failures into an EVALUATE that
                # carries _error and _error_kind: count those apart from a
                # real EVALUATE, by cause.
                if (verdict or {}).get("_error"):
                    kind = verdict.get("_error_kind") or "unknown"
                    fail_kinds[kind] += 1
                    try:
                        outage = LLMFailure(kind) in INFRA
                    except ValueError:
                        outage = False
                    if outage:
                        # An outage judged nothing: it does not spend the cap.
                        consecutive_outages += 1
                        judged -= 1
                    else:
                        consecutive_outages = 0
                    continue
                consecutive_outages = 0
                v = (verdict or {}).get("verdict")
                _model_v = v
                # HOUSEHOLD FLOOR: Pillar III decided in Python
                # (pillars._household_claim). A model — above all a small
                # one — can miss the sacred pillar under a strong taste
                # mismatch; another household member's genuine engagement
                # never ends in a CUT. Not a keep either: the owner decides.
                _claim = (ev.get("flags") or {}).get("household_claim")
                if v == "CUT" and _claim:
                    household_floored += 1
                    logger.warning("[deletions] %s: CUT on %r overruled — %s "
                                   "engaged with it (household floor)",
                                   category, item.get("title"), _claim)
                    verdict = {**verdict, "verdict": "STAGNANT",
                               "_guard": "household_floor",
                               "_guard_detail": _claim}
                    v = "STAGNANT"
                # Kept past the verdict cache's 30 days (services/judge_record):
                # the Wrap-Up's court record reads the whole year.
                await judge_record.keep_verdict(
                    user_id, item, category, verdict, model_verdict=_model_v,
                    score=cand.get("score"), model=pitch_model, verdict_key=_vkey)
                if v in ("CUT", "STAGNANT"):
                    # Yield between the verdict and its monologue too: they
                    # are two full generations, and a chat queued during the
                    # verdict waited out both before the loop-top yield.
                    if gate_contested(_run_lane):
                        _msg(f"{category}: yielding GPU to a waiting request…")
                        _holds_gate = False
                        curator_done(lane=_run_lane)
                        await wait_quiet(_YIELD_QUIET_S)
                        _run_lane = await curator_start(_gate_label, exclusive_model=pitch_model, role="judge")
                        _holds_gate = True
                    _variant = f"{lang_directive_str}|{verdict.get('_guard') or ''}"
                    pitch = await asyncio.to_thread(verdict_cache.get_pitch, _vkey, _variant)
                    if not pitch:
                        pitch = await write_monologue(
                            ev["facts"], verdict, model=pitch_model,
                            lang_directive=lang_directive_str, skip_priority=True,
                            host=judge_host)
                        await asyncio.to_thread(verdict_cache.put_pitch, _vkey, _variant,
                                                pitch or "")
                    if not pitch or not pitch.strip():
                        pitch = ("Curatarr flagged this title but couldn't write its "
                                 "reasoning. Use Discuss to ask why.")
                    _smb = item.get("size_mb") or 0
                    final_proposals.append({
                        "title": item.get("title"),
                        "pitch": pitch,
                        "confidence": min(0.99, max(0.10, cand["score"] / 100)),
                        "score_factors": _with_guard(_score_factors(cand), cand, verdict),
                        "size_mb": _smb,
                        "size_gb": round(_smb / 1024, 1),
                        "arr_id": item.get("arr_id"),
                        "service": item.get("service", ""),
                        "arr_url": item.get("arr_url", ""),
                        "tvdb_id": item.get("tvdb_id"),
                        "tmdb_id": item.get("tmdb_id"),
                        "latest_activity_at": item.get("latest_activity_at"),
                        "stagnant": (v == "STAGNANT"),
                        # The scheduler groups proposals by this key before
                        # writing rows; without it every nightly-scan proposal
                        # fell into the "movie" bucket (wrong tab + wrong
                        # supersede scope). The router path re-derives it from
                        # item_map, so only the scheduler ever noticed.
                        "category": category,
                    })
                elif v in ("HARD_KEEP", "KEEP_WITH_FLAG"):
                    # Over-protection fix: persist ONLY when a higher pillar
                    # (Household / Custodian / Resonance) is what keeps it. A bare
                    # Ego(0) taste-match is the owner's own edge — re-judged each
                    # scan, never persisted (persisting it would bury their picks).
                    if (verdict.get("protecting_pillar") or "").upper() in (
                            "HOUSEHOLD", "CUSTODIAN", "RESONANCE"):
                        await run_write(_persist_judge_protection,
                                        user_id, item, category, v, verdict)
                # EVALUATE / bare-Ego keep / None → neither propose nor protect.
        finally:
            for _t in _ev_ahead.values():
                _t.cancel()
            if _ev_ahead:
                # Reap them: an unawaited failed task logs "exception never
                # retrieved", and a cancel must land before the gate is freed.
                await asyncio.gather(*_ev_ahead.values(), return_exceptions=True)
            # Eager pitcher eviction BEFORE releasing the gate ("gate free ⇒
            # VRAM free"): a chat right after the run loads the curator
            # without waiting out the pitcher's 10m keep_alive. Guarded +
            # best-effort — an eviction hiccup must never eat the proposals.
            # Only as the holder: without the slot the GPU belongs to someone
            # else, whose model choice is not ours to touch.
            if _holds_gate:
                if pitch_model != (settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL):
                    try:
                        await evict_if_resident(pitch_model, url=_run_lane.url)
                    except Exception as _ee:  # noqa: BLE001 - teardown; keep_alive frees VRAM anyway
                        logger.debug("[deletions] pitcher evict failed: %s", _ee)
                curator_done(lane=_run_lane)
        _msg(f"{category}: pillar judging done — {len(final_proposals)} flagged of "
             f"{judged} judged"
             + (f" ({reused} unchanged since the last run, verdict reused)" if reused else "")
             + (f" ({thin_skipped} skipped: not yet enriched)" if thin_skipped else "")
             + (f" ({misfiled_skipped} skipped: profile is another work)"
                if misfiled_skipped else "")
             + (f" ({unchecked_skipped} deferred: significance unchecked)"
                if unchecked_skipped else "")
             + (f" ({watch_unknown_skipped} deferred: watch history unreadable)"
                if watch_unknown_skipped else "")
             + (f" ({signal_unknown_skipped} deferred: owner feedback unreadable)"
                if signal_unknown_skipped else "")
             + (f" ({household_floored} turned into 'Your call': another "
                f"household member engaged)" if household_floored else "")
             + (f" ({sum(fail_kinds.values())} failed: "
                f"{_failure_sentence(dict(fail_kinds))})" if fail_kinds else "")
             + (f" — stopped early: {stopped}" if stopped else "")
             + (f" — {taste_degraded}" if taste_degraded else "")
             + ".")
        _record_deletion_run(
            category, judged=judged, flagged=len(final_proposals),
            deferred=(thin_skipped + misfiled_skipped + unchecked_skipped
                      + signal_unknown_skipped + watch_unknown_skipped),
            fail_kinds=dict(fail_kinds), stopped=stopped, degraded=taste_degraded)
        return final_proposals

    # ── LEGACY taste-mismatch pitch path (PILLARS_ENABLED off) ────────────────
    # Pass 39: random axis seed per pitch. Single-line nudge in the prompt
    # below — no batch-state tracking, no allocation problem (sampling
    # with replacement). Over 10 pitches the axes naturally distribute,
    # breaking the "every pitch sounds identical" rut without forcing
    # awkward angles when one genuinely doesn't fit (the prompt allows
    # opting out of the suggested axis with explicit instruction NOT to
    # fall back on the blacklisted crutch phrases).
    import random as _rand
    _PITCH_AXES = [
        ("PACING & RHYTHM",         "sluggishness, filler, runtime waste, episodic drag"),
        ("CHARACTER AGENCY",        "passive protagonists, stagnant dynamics, motivations that don't move"),
        ("CRAFT & EXECUTION",       "static frames, low-effort production, sloppy direction, flat mixing — whatever 'low effort' looks like for the medium"),
        ("PREMISE & WORLDBUILDING", "trope reliance, broken internal logic, gimmickry, lazy lore"),
        ("THEMATIC EMPTINESS",      "no satire, no critique, no point — entertainment as wallpaper"),
        ("CULTURAL DATEDNESS",      "era-stuck assumptions, aged-poorly tropes, requires niche cultural memory"),
    ]
    # Music needs its OWN axes — film angles like "character agency" or
    # "premise & worldbuilding" are nonsense for an artist and were leaking
    # plot/film framing into music pitches.
    # Every axis must be answerable from the metadata we actually have (genre,
    # scene, themes, mood tags). The old SONIC PALETTE / PRODUCTION axes asked
    # the curator to judge timbre, mix and "production sheen" it has never
    # heard, so it dutifully invented them — the root of the audio-hallucination
    # pitches. These angles critique fit, not fabricated sound.
    _PITCH_AXES_MUSIC = [
        ("GENRE & SCENE FIT",  "how their genre/scene sits against the user's core sound"),
        ("EMOTIONAL REGISTER", "the mood/intensity their music trades in vs the user's preferred register"),
        ("CULTURAL PLACEMENT", "era/scene baggage — dated, niche or novelty for this listener"),
        ("THEMATIC SUBSTANCE", "the themes their catalogue centres vs what the user values"),
        ("CATALOGUE DEPTH",    "a one-note / novelty act vs a substantive body of work"),
        ("LIBRARY REDUNDANCY", "whether the user's library already owns this lane, done better"),
    ]

    # Load relevant memories — scoped to this category so only domain-relevant
    # memories (e.g. film nostalgia for movie deletions) enter the context window.
    memories = await retrieve_memories(user_id, "deletion reasons nostalgia classics", top_k=5, media_category=category)
    memory_context = format_memories_for_context(memories)

    # Wrap all pitch LLM calls in a single curator session so the curator model
    # stays loaded throughout the batch and the summarizer is only evicted once.
    from src.services.llm_priority import curator_start, curator_done
    top_pitch_set = scored_candidates[:10]
    # Watch status for exactly the items we're about to pitch — ONE query.
    # The pitch otherwise knows the user OWNS the item but not whether they've
    # SEEN it, the signal that separates "untested clutter" from "watched and
    # done" / "re-watched and loved". Injected as neutral context (no score
    # nudge — see _watch_pitch_line), so the curator weighs it like a memory.
    from src.services.watch_status import watched_lookup as _watched_lookup
    watch_status_map = await run_read(
        _watched_lookup, user_id, [c["item"].get("title") for c in top_pitch_set],
        category=category)
    # A household member's explicit keep holds here too, without the pillar
    # judge: never a plain pitch, always the owner's call. Unreadable keeps
    # mean the item waits for the next scan (never "nobody asked").
    member_keeps = await run_read(_member_keeps_for, user_id,
                                  [c["item"] for c in top_pitch_set], category)
    _msg(
        f"{category}: scoring done ({len(scored_candidates):,} above threshold) — "
        f"generating LLM pitches for top {len(top_pitch_set)}…"
    )
    _run_lane = await curator_start(f"deletion pitches: {category}",
                                    exclusive_model=pitch_model, role="judge")
    try:
        for _pidx, cand in enumerate(top_pitch_set, start=1):
            item = cand["item"]
            if member_keeps.get(_idx_key(item), []) is None:
                continue      # household keeps unreadable: decided next scan
            _msg(
                f"{category}: LLM pitch {_pidx}/{len(top_pitch_set)} — "
                f"{(item.get('title') or '?')[:50]}"
            )

            # ── Metadata extraction ───────────────────────────────────────────
            overview      = item.get("overview") or item.get("description") or "No description available."
            overview_short = overview[:800] + "…" if len(overview) > 800 else overview
            year           = item.get("year", "Unknown Year")

            # Genres: prefer enrichment cache (richer tags) over ARR field.
            # Re-use cache values pre-computed in the scoring phase via cand dict.
            cached_rating   = cand["cached_rating"]      # None if not enriched
            arr_rating_raw  = cand["arr_rating"]          # weighted-avg across all ARR sources
            rating_breakdown = cand["rating_breakdown"]   # per-source dict for the prompt
            rating_ctx, _, cached_genres = await asyncio.to_thread(
                _get_cached_rating, item, category)

            genres_raw = cached_genres or item.get("genres", [])
            genres_str = ", ".join(genres_raw) if isinstance(genres_raw, list) else str(genres_raw)

            # ── Rating context block ──────────────────────────────────────────
            # Multi-source breakdown when available (TMDB / IMDb / RT / Metacritic
            # all in one block) so the LLM can sanity-check its critique against
            # multiple consensus signals, not just TMDB.
            if rating_breakdown:
                source_labels = {
                    "tmdb":       "TMDB",
                    "imdb":       "IMDb",
                    "rt":         "Rotten Tomatoes (audience score, 0-10)",
                    "metacritic": "Metacritic (0-10)",
                    "tmdb_cache": "TMDB (enrichment cache)",
                    "legacy":     "ARR (legacy flat)",
                    "flat":       "ARR (flat)",
                }
                breakdown_lines = "\n".join(
                    f"    · {source_labels.get(src, src)}: {val:.1f}/10"
                    for src, val in rating_breakdown.items()
                )
                rating_block = (
                    f"RATING (weighted across sources, {arr_rating_raw:.1f}/10):\n"
                    f"{breakdown_lines}"
                )
            elif rating_ctx:
                rating_block = f"RATING: {rating_ctx}"
            elif arr_rating_raw and arr_rating_raw > 0:
                rating_block = f"RATING: {arr_rating_raw:.1f}/10 (source: ARR metadata)"
            else:
                rating_block = (
                    "RATING: No external rating data available for this item. "
                    "DO NOT assume it is low quality. Base your critique ONLY on the "
                    "synopsis and taste mismatch. Defer judgment on quality entirely — "
                    "focus only on relevance fit."
                )

            # Music override: TMDB/IMDb/RT are FILM ratings and do not apply to
            # an artist — surfacing them produced "3.0 TMDB score" hallucinations.
            # Use the user's OWN Plex star rating when present (the real personal
            # signal); otherwise judge on taste-fit alone.
            if category == "music":
                _ur = cand.get("user_rating")
                if _ur and _ur > 0:
                    rating_block = (
                        f"USER RATING: {_ur / 2.0:.1f}/5 stars — the user's OWN "
                        f"rating of this artist (a strong personal signal; a low "
                        f"score means they don't care for them)."
                    )
                else:
                    rating_block = (
                        "RATING: none — the user hasn't rated this artist and no "
                        "objective music score applies. Judge ONLY on how their "
                        "sound fits the user's taste; do NOT invent or cite any "
                        "rating (no TMDB/IMDb — those are for films)."
                    )

            # ── Domain vocabulary guideline ───────────────────────────────────
            vocab_guideline = _get_vocab_guideline(category, genres_str)

            # Pass 39: random critique-axis seed. ONE line in the prompt
            # gently steers the LLM toward a specific angle so we don't
            # see the same hooks ("sakuga density", "narrative propulsion")
            # in every pitch of a batch. ``random.choice`` is sampling
            # with replacement — over 10 pitches the axes distribute
            # probabilistically without the "best axis already used up"
            # problem a deterministic rotation would have.
            axis_label, axis_hint = _rand.choice(
                _PITCH_AXES_MUSIC if category == "music" else _PITCH_AXES
            )

            # Feed the FULL verified dataset we already cached (creator/writer,
            # extended plot, themes, keywords, awards) — assembled with NO LLM,
            # cache-only — so the 27B curator reasons from FACTS instead of a
            # thin synopsis stub or its own training memory (the root of the
            # "Steppenwolf is a Dean Koontz film" hallucinations). Empty string
            # when nothing is cached → falls back to the thin item fields.
            from src.services.media_enricher import ensure_verified_data, format_verified_block
            # Pass EVERY id the candidate carries, not just tmdb_id. The
            # enrichment cache keys anime by anilist_id / tvdb_id and shows by
            # tvdb_id — so a tmdb-only lookup silently MISSED all ~5.3k cached
            # anime profiles, dumping the curator onto the thin synopsis stub →
            # cold-read / plot-inversion (the Skate-Leading bug). tvdb_id alone
            # reaches the cached anime/show data; plex_rating_key adds the
            # prefetch overview.
            # format_verified_block also reads the edition from the DB.
            verified_block = await run_read(
                format_verified_block,
                # Legacy pitch loop holds the curator gate — no summarizer here
                # (VRAM churn; see ensure_verified_data docstring).
                await ensure_verified_data(
                    item.get("title") or "", category,
                    tmdb_id=item.get("tmdb_id"),
                    tvdb_id=item.get("tvdb_id"),
                    anilist_id=item.get("anilist_id"),
                    anidb_id=item.get("anidb_id"),
                    plex_rating_key=item.get("plex_rating_key"),
                    year=item.get("year"),
                    allow_summarizer=False,
                )
            )
            if verified_block:
                item_details = verified_block
            else:
                item_details = (
                    "ITEM DETAILS:\n"
                    f"- Title: {item.get('title')} ({year})\n"
                    f"- Genres: {genres_str or 'Unknown'}\n"
                    f"- Synopsis: {overview_short}"
                )
            # No verified block → the curator has only a thin synopsis. Inject the
            # hedge so it acknowledges the blind spot instead of confabulating
            # confident execution verdicts (the Fringe case).
            data_poor_block = NO_VERIFIED_DATA_HEDGE if not verified_block else ""

            # Learned considerations stashed during scoring — the user's standing
            # keep/value preferences that plausibly apply to THIS item. The curator
            # must WEIGH them (acknowledge the tension), not blindly obey. Empty
            # string for the vast majority of items.
            from src.services.episodic_memory import format_considerations_for_pitch
            considerations_block = format_considerations_for_pitch(
                cand.get("considerations") or [])

            # Candidate's own watch status — neutral context the curator weighs.
            watch_block = _watch_pitch_line(watch_status_map.get(item.get("title")))

            # Size-outlier context (non-music): is this item's GB normal for its
            # resolution/codec class (don't flag) or genuine bitrate bloat (size
            # is a fair argument)? Stops the blanket "70 GB is too big" complaint.
            from src.services.size_norms import size_context_for
            size_ctx_block = "" if category == "music" else await run_read(
                size_context_for,
                tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
                plex_rating_key=item.get("plex_rating_key"),
                media_type=category)

            if category == "music":
                # Music-specific pitch: artist framing, no synopsis, no film
                # ratings, and hard anti-hallucination rules (a band sharing a
                # name with a film is NOT that film — the Steppenwolf trap).
                prompt = f"""[MODE: SURGICAL DELETION PITCH — MUSIC]
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
{data_poor_block}

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
            else:
                prompt = f"""[MODE: SURGICAL DELETION PITCH]
You are Curatarr, an uncompromising, elite media curator. Pitch the deletion of this item.

{lang_directive_str}

{item_details}
{rating_block}

REASON FOR DELETION CONSIDERATION: Mismatch with user taste profile.
{f'USER TASTE SUMMARY: {taste_blurb}' if taste_blurb else ''}
{f'KNOWN EXCEPTIONS & MEMORIES: {memory_context}' if memory_context else ''}
{considerations_block}
{watch_block}
{data_poor_block}
{size_ctx_block}

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
8. SIZE — DOWNSCALE NOTE, NEVER A DELETE REASON: Mention file size / bitrate ONLY if a SIZE CONTEXT line is present above (it now appears solely for genuine bloat outliers + duplicates). If there is NO SIZE CONTEXT line, do NOT mention size, gigabytes, bitrate or vector distance AT ALL. When it IS present: the pitch stands or falls on taste/premise alone — size argues a DOWNSCALE of the file, never deletion of the work, so at most close with ONE factual aside that even a keep should carry a downscale flag (cite the concrete ratio from the line, "≈3.7× the typical 1080p bitrate", not a vague genre framing). NEVER call a small / low-bitrate file "wasted storage" — a small file wastes nothing.
9. PREMISE & FIT — NOT A REVIEW: You have this item's premise, themes and metadata, NOT a screening of it. Argue why its premise / genre / themes CLASH with the user's taste. Do NOT pass verdicts on execution you cannot know — no "static", "hollow", "melodramatic stalemate", "flat", "lands/doesn't land", no claims about pacing, acting or direction — unless that judgement is explicitly in the data above. For fact-based works (history, true events, documentary) a known outcome is NOT a flaw: never call it "predictable".
10. RESPECT ACCLAIM — TASTE-FIT, NOT QUALITY: If the data shows strong acclaim (high RT / Metacritic, major awards, a documented milestone), you MUST acknowledge it, and the deletion is a pure TASTE-FIT call — never a quality verdict. Frame it honestly: "acclaimed / well-made, but not your lane — it's X, you want Y." NEVER label a highly-rated or award-winning title "narrative wallpaper", "flaccid", "worthless", or low-effort: calling a 97%-rated, multi-award film garbage is a factual error that wrecks your credibility. A work can be excellent AND a poor fit for THIS user — say exactly that."""

            pitch = await _call_llm(prompt, skip_priority=True,
                                    model_override=pitch_model, host=judge_host)
            # Pass 51: empty-pitch guard. ``_call_llm`` returns None on an
            # HTTP / timeout failure and "" when the model emitted nothing
            # but <think> tags (stripped out by strip_think_tags). Either
            # way an empty pitch reaches the UI as a blank deletion card —
            # a Delete button with no reasoning, which is both useless and
            # slightly alarming. One retry (the curator model is already
            # warm, so this is cheap), then a deterministic honest
            # fallback so the card stays actionable.
            if not pitch or not pitch.strip():
                logger.warning(
                    "[deletions] empty pitch for %r — retrying once", item.get("title"),
                )
                pitch = await _call_llm(prompt, skip_priority=True,
                                        model_override=pitch_model, host=judge_host)
            if not pitch or not pitch.strip():
                logger.warning(
                    "[deletions] pitch still empty for %r — using honest fallback",
                    item.get("title"),
                )
                pitch = (
                    "Curatarr flagged this as a poor fit for your taste but couldn't "
                    "write its reasoning. Use Discuss to ask why."
                )
            size_mb = item.get("size_mb") or 0
            # del_score → confidence:
            # The recalibrated scoring (mismatch 0-80, size 0-25, rating-swing
            # ±20) lands clear bad-fits in the 60-90 range, mediocre fits at
            # 30-50, with a theoretical max around 125. /100 + clamp at 0.99
            # turns that into a meaningful 30-99 % confidence band — the
            # earlier formula capped most realistic candidates near 50 %.
            _keepers = member_keeps.get(_idx_key(item))
            _floor = ({"_guard": "household_floor", "_guard_detail": _keepers[0]}
                      if _keepers else None)
            final_proposals.append({
                "title": item.get("title"),
                "pitch": pitch,
                "confidence": min(0.99, max(0.10, cand["score"] / 100)),
                "score_factors": _with_guard(_score_factors(cand), cand, _floor),
                # A member asked to keep it: "Your call", never a plain cut.
                "stagnant": bool(_keepers),
                "size_mb": size_mb,
                "size_gb": round(size_mb / 1024, 1),
                "arr_id": item.get("arr_id"),
                "service": item.get("service", ""),
                "arr_url": item.get("arr_url", ""),
                # Resolving IDs persisted on the proposal so the discussion path
                # can on-demand fast-enrich + cache an un-enriched title later.
                "tvdb_id": item.get("tvdb_id"),
                "tmdb_id": item.get("tmdb_id"),
                # Pass 17: forward the latest file-import timestamp so the
                # caller can persist it on the DeletionProposal row. NULL
                # when /history fetch failed or the item has no recent
                # imports — filter falls through cleanly in either case.
                "latest_activity_at": item.get("latest_activity_at"),
                # See the pillar-path note: the scheduler groups by this key.
                "category": category,
            })
    finally:
        # Same eager pitcher cleanup as the pillar path ("gate free ⇒ VRAM
        # free") — guarded and best-effort.
        if pitch_model != (settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL):
            try:
                from src.services.llm_priority import evict_if_resident
                await evict_if_resident(pitch_model, url=_run_lane.url)
            except Exception as _ee:  # noqa: BLE001 - teardown; keep_alive frees VRAM anyway
                logger.debug("[deletions] pitcher evict failed: %s", _ee)
        curator_done(lane=_run_lane)

    return final_proposals


# ── PASS 81: LEVEL 2 RE-EVALUATION ────────────────────────────────────────────
#
# The standard deletion pitch is built from surface metadata: genre tags,
# synopsis, ratings, taste-mismatch vector distance. That's the right
# default — fast, cheap, calibrated. But it falls over on "Trojan Horse"
# narratives: shows whose generic premise is a performative mask for
# subversion, deconstruction, or political allegory. The synopsis says
# "high school comedy" but the work is actually an identity-fragmentation
# study; the curator pitches deletion, the user knows better, and the gap
# is a credibility crater.
#
# Re-evaluation is a USER-INITIATED escape hatch: the user clicks the 🔍
# button next to Discuss. The button does NOT run a parallel LLM pipeline
# — it just opens the standard deletion-discussion thread and auto-sends
# a Level-2 challenge prompt as the user's first message. The curator
# answers via the existing /api/chat/message streaming pipeline, the
# verdict shows up live in the chat, the user can immediately follow up
# ("but check director X"), and memory extraction sees the whole thread.
#
# Why this routing instead of a dedicated endpoint: zero new queue, zero
# new LLM lifecycle to manage, streaming UX instead of a 30-90 s spinner,
# and the verdict becomes searchable conversation history instead of an
# appended blob on the proposal card. The Level-2 *prompt* lives in the
# frontend (see ``onReevaluateDeletion`` in frontend/js/app.js) — it's just a
# pre-filled chat input, no server help needed: the chat backend's
# ``_build_discuss_context_block`` already injects the item title +
# original verdict + synopsis as RAG, so the user-side prompt only carries
# the *challenge framing*.


async def score_arr_items(user_id: int, category: str, items: list, top_n: int = 50) -> list:
    """Rank ARR items by taste-fit before the top slice goes to the LLM.

    Pass 70: ranks primarily by SEMANTIC similarity — cosine ``np.dot`` of
    the user's taste embedding against each item's ChromaDB embedding. This
    is the mirror of the Pass-64 deletion scoring (recommend the *closest*,
    delete the *farthest*). The embedding is the real taste signal the engine
    computes; before this it was completely unused on the recommendation
    side — items were ranked only by crude genre-tag overlap.

    Items with no embedding yet (not enriched into ChromaDB) fall back to
    genre-affinity overlap. The two scores live on different scales, so we
    do NOT mix them in one sort: enriched items rank among themselves by
    cosine, un-enriched items among themselves by genre overlap, and the
    enriched group is placed first — we have real taste-fit data for those.
    ``monitored`` breaks ties and recorded dislikes group last (rank flags,
    not score bonuses — flat bonuses drowned the narrow cosine band).

    Unlike the old version this ALWAYS ranks — no early-out for small
    libraries. ``generate_recommendations`` only feeds the LLM the first ~30
    items, so order matters even when every item survives the top_n cut.
    """
    def _taste():
        with get_db_session() as db:
            tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
            type_data = json.loads(tv.genre_affinity or "{}") if (tv and tv.genre_affinity) else {}
            user_vector = None
            # Assigned only inside the blob branch below — a category with genre
            # stats but no blob yet (nothing embedded) raised UnboundLocalError.
            user_clusters = None
            disliked_lc: set = set()
            encrypted = db.query(EncryptedTasteVector).filter(
                EncryptedTasteVector.user_id == user_id,
                EncryptedTasteVector.media_category == category,
            ).first()
            if encrypted and encrypted.encrypted_blob:
                try:
                    _blob = json.loads(encrypted.encrypted_blob)
                    if not isinstance(_blob, dict):
                        raise ValueError(f"blob is a {type(_blob).__name__}")
                except (TypeError, ValueError) as e:
                    logger.warning("[recs] %s taste blob is corrupt — ranking "
                                   "without the taste vector until the next "
                                   "taste rebuild: %r", category, e)
                    _blob = {}
                if _blob.get("version") == 1:
                    logger.warning("[recs] %s taste blob is encrypted (v1) — "
                                   "ranking without the taste vector", category)
                    _blob = {}
                user_vector = _blob.get("embedding")
                user_clusters = _blob.get("cluster_centroids")
                # its own step: one odd dislike entry used to throw away the
                # taste vector parsed just above
                try:
                    disliked_lc = {str(t).lower()
                                   for t in _blob.get("disliked_titles") or [] if t}
                except TypeError as e:
                    logger.warning("[recs] %s disliked_titles unreadable — ranking "
                                   "without recorded dislikes: %r", category, e)
        return type_data, user_vector, user_clusters, disliked_lc

    type_data, user_vector, user_clusters, disliked_lc = await run_read(_taste)

    ts = type_data.get(category, {}) if isinstance(type_data, dict) else {}
    genre_affinity = {g.lower(): s for g, s in (ts.get("genre_affinity") or {}).items()}

    # No taste signal at all → nothing to rank by; preserve the old
    # "just take the first top_n" behavior.
    if not user_vector and not genre_affinity:
        return items[:top_n]

    # Rank-based flags instead of flat score bonuses (eval finding 1.2):
    # the observed cosine band is only ~0.14 wide (anisotropic space), so
    # the old +0.10/-0.25 additive bonus was 70-180% of the entire usable
    # taste signal — monitored status could outrank any taste fit. As sort-
    # tuple components they can only group (dislikes last) and break ties
    # (monitored first), never drown the cosine.
    def _dislike_rank(item: dict) -> int:
        return 0 if (item.get("title") or "").lower() in disliked_lc else 1

    def _monitored_rank(item: dict) -> int:
        return 1 if item.get("monitored") else 0

    def _genre_score(item: dict) -> float:
        genres = [g.strip().lower() for g in (item.get("genres") or "").split(",") if g.strip()]
        return sum(genre_affinity.get(g, 0) for g in genres)

    def _genres_matched(item: dict) -> list:
        """The item's genres the user's affinity rates above zero, strongest
        first — the overlap the genre fallback ranks on, named for "Why?"."""
        genres = [g.strip() for g in (item.get("genres") or "").split(",") if g.strip()]
        hits = [g for g in genres if (genre_affinity.get(g.lower()) or 0) > 0]
        return sorted(hits, key=lambda g: -genre_affinity[g.lower()])[:3]

    # Multimodal pairs rank by MAX over cluster centroids (ComiRec) — one
    # strong interest match wins; a blend of unrelated interests can't
    # dilute it. Single-centroid pairs behave exactly as before.
    if user_clusters:
        user_vecs_n = [v for v in (_normalize_vec(c.get("embedding"))
                                   for c in user_clusters) if v is not None]
    else:
        _un = _normalize_vec(user_vector) if user_vector else None
        user_vecs_n = [_un] if _un is not None else []

    # Bulk-prefetch embeddings — same N+1 rationale as the deletion loop.
    from src.vector_store.chromadb_wrapper import run_chroma
    _prefetched_embeddings = await run_chroma(_chroma_get_by_ids, [
        str(it.get("plex_rating_key") or it.get("tmdb_id") or it.get("title") or "")
        for it in items
        if (it.get("plex_rating_key") or it.get("tmdb_id") or it.get("title"))
    ])

    def _vector_score(item: dict):
        """Cosine similarity (user taste vector · item ChromaDB embedding),
        or None when either side is missing."""
        # External eval catch: P3b renamed the single vector into the
        # user_vecs_n list but left this guard on the OLD name — NameError
        # for every user with taste data (endpoint 500, scheduler swallowed
        # it silently). Guard on what actually exists.
        if not user_vecs_n:
            return None
        doc_id = str(item.get("plex_rating_key") or item.get("tmdb_id") or item.get("title") or "")
        if not doc_id:
            return None
        res = _prefetched_embeddings.get(doc_id)
        # Pass 74: ChromaDB embeddings are numpy arrays — check
        # ``is not None``, never truthiness. ``bool(array)`` raises
        # ValueError; the old ``if res.get("embedding")`` raised inside a
        # try/except here, so it was silently swallowed and EVERY item fell
        # through to the genre fallback — the pass-70 vector ranking never
        # actually ran. No blanket except any more: the one expected
        # mismatch (an embedding from another model's space) is checked
        # and counted, anything else is a bug and surfaces.
        emb = res.get("embedding") if res else None
        if emb is None:
            return None
        # Audit #6: stored embeddings are RAW (norm ~13) -- the same
        # trap the deletion path fixed with _normalize_vec. A bare
        # dot ranked by |emb| instead of taste fit and drowned the
        # 0.1 monitored bonus.
        emb_n = _normalize_vec(emb)
        if emb_n is None:
            return None
        if any(u.shape != emb_n.shape for u in user_vecs_n):
            _dim_mismatch[0] += 1
            return None
        return max(float(np.dot(u, emb_n)) for u in user_vecs_n)

    # Pure CPU (normalize + dot per item, ~100 ms per 5k items) in one worker
    # thread; everything it reads was fetched above — no DB/Chroma in here.
    def _rank_all() -> list:
        vectored: list = []
        genre_only: list = []
        for item in items:
            vs = _vector_score(item)
            if vs is not None:
                vectored.append(((_dislike_rank(item), vs, _monitored_rank(item)), item))
            else:
                genre_only.append(((_dislike_rank(item), _genre_score(item),
                                    _monitored_rank(item)), item))
        vectored.sort(key=lambda t: t[0], reverse=True)
        genre_only.sort(key=lambda t: t[0], reverse=True)
        # Enriched items first (real semantic taste-fit), genre-only fallback after.
        ranked = ([("embedding", k, it) for k, it in vectored]
                  + [("genres", k, it) for k, it in genre_only])
        # Copies, not the arr dicts: those are the shared library cache, and
        # the fit is this user's. "_rec_fit" feeds the rec card's "Why?".
        return [{**it, "_rec_fit": {
                    "basis": basis, "rank": n, "pool": len(ranked),
                    "cosine": round(k[1], 4) if basis == "embedding" else None,
                    "genres": _genres_matched(it), "disliked": k[0] == 0}}
                for n, (basis, k, it) in enumerate(ranked, 1)]

    _dim_mismatch = [0]
    ranked = await asyncio.to_thread(_rank_all)
    if _dim_mismatch[0]:
        logger.warning("[recs] %d item embedding(s) have another dimension than "
                       "the taste vector (embedding model changed?) — ranked by "
                       "genre until the next taste rebuild", _dim_mismatch[0])
    return ranked[:top_n]