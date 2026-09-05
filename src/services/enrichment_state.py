"""
Curatarr — enrichment state: the ONE vocabulary for "how far is this item".

Before this module three places classified an EnrichmentStatus row three
different ways (a SQL CASE in /library/breakdown, an if-chain in
kb_overview, ad-hoc string tests in the producer) and none of them agreed:
the KB tile reported a fresh not-found sentinel as "enriched" while its
3-day cache entry was live, and 798 "Not found" rows as "retry queued".

Everything here is pure and import-light so the router, the KB overview,
the producer and the tests can share it without cycles:

  * ``classify_enrichment_row``  — status-derived state FIRST, cache
    liveness only for the enriched branch.
  * ``STATE_DEFINITIONS``        — label / explainer / next step per state;
    served to the frontend so the UI never carries its own copy.
  * backoff                       — attempt counter → next retry (SoulSync's
    ``wishlist_backoff`` shape, MIT): two free tries, then 3 → 6 → 12 → 24
    days, capped at 30 days, forever (owner decision: never give up).
  * ``TRANSIENT``                 — the falsy marker a fetcher returns when the
    upstream was unavailable (429 / 5xx / network), so a wave of API trouble
    is never written down as "this title does not exist".
  * ``open_reason``               — one sentence that says WHY an item is
    still open, built from the persisted per-source evidence.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Optional

# ── transient marker ──────────────────────────────────────────────────────────

class TransientMiss(dict):
    """Falsy, empty, identity-checked. Fetchers return the ``TRANSIENT``
    singleton instead of ``None`` when the API did not answer properly.
    Legacy callers that test truthiness (``if raw:``) see exactly what they
    saw before; ``_merge_source_into_raw`` checks identity and stamps the
    source ``"transient"`` instead of ``"miss"``."""
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<TRANSIENT>"


TRANSIENT = TransientMiss()

# ── error-string vocabulary (EnrichmentStatus.error) ─────────────────────────

NOT_FOUND_PREFIX = "Not found"
IGNORED_ERROR = "ignored by owner"
PROCESSING_FAILED_ERROR = "Processing failed"
RULE_BASED_ERROR = "rule_based — LLM upgrade pending"

REASON_NO_SOURCE = "no_source_data"     # every consulted source missed
REASON_YEAR = "year_mismatch"           # found a same-named work, wrong year
REASON_LOW_CONFIDENCE = "low_confidence"  # title-search hit too far off
NOT_FOUND_REASONS = (REASON_NO_SOURCE, REASON_YEAR, REASON_LOW_CONFIDENCE)


def not_found_error(reason: Optional[str] = None) -> str:
    reason = reason if reason in NOT_FOUND_REASONS else REASON_NO_SOURCE
    return f"{NOT_FOUND_PREFIX}: {reason}"


def is_not_found_error(error: Optional[str]) -> bool:
    return (error or "").strip().lower().startswith(NOT_FOUND_PREFIX.lower())


def not_found_reason(error: Optional[str]) -> Optional[str]:
    """Reason code behind a not-found error string. Legacy rows carry
    ``"Not found in metadata APIs"`` → ``no_source_data``."""
    if not is_not_found_error(error):
        return None
    tail = (error or "").split(":", 1)
    if len(tail) == 2 and tail[1].strip() in NOT_FOUND_REASONS:
        return tail[1].strip()
    return REASON_NO_SOURCE


# ── backoff (SoulSync core/video/wishlist_backoff.py shape, MIT) ─────────────

FREE_ATTEMPTS = 2        # attempts 1-2: due again on the very next run
BASE_DELAY_DAYS = 3.0    # attempt 3 waits 3 d, then 6, 12, 24 …
MAX_DELAY_DAYS = 30.0    # … capped at 30 d, forever (never give up)


def retry_delay_days(attempts: int) -> float:
    """Days to wait after the ``attempts``-th miss. 0 while inside the free
    attempts; doubles from BASE_DELAY_DAYS afterwards; capped."""
    attempts = int(attempts or 0)
    if attempts <= FREE_ATTEMPTS:
        return 0.0
    return float(min(MAX_DELAY_DAYS, BASE_DELAY_DAYS * (2 ** (attempts - FREE_ATTEMPTS - 1))))


def next_retry_at(attempts: int, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the item becomes due again; ``None`` = due on the next run."""
    delay = retry_delay_days(attempts)
    if delay <= 0:
        return None
    return (now or datetime.utcnow()) + timedelta(days=delay)


def sentinel_cache_days(attempts: int) -> int:
    """Cache TTL for a not-found sentinel — never shorter than the retry
    wait, so the cache and the DB row agree on when the item is due."""
    return max(3, int(math.ceil(retry_delay_days(attempts))))


def due_clause(model, now: Optional[datetime] = None):
    """SQLAlchemy predicate: the row is due for another attempt. Mirrors
    ``is_due`` so the scheduler filters in SQL instead of in Python."""
    from sqlalchemy import or_
    now = now or datetime.utcnow()
    return or_(model.next_retry_at.is_(None), model.next_retry_at <= now)


def is_due(next_retry: Optional[datetime], now: Optional[datetime] = None) -> bool:
    return next_retry is None or next_retry <= (now or datetime.utcnow())


# ── states ────────────────────────────────────────────────────────────────────

STATES = (
    "enriched", "enriched_provisional", "enriched_dead",
    "rule_based", "awaiting_llm",
    "retry_due", "not_found",
    "queued", "processing_error",
    "ignored", "never_processed",
)

# The states that count as "done" for a percentage.
DONE_STATES = ("enriched", "enriched_provisional")

STATE_DEFINITIONS: dict[str, dict[str, str]] = {
    "enriched": {
        "label": "✅ Enriched",
        "explainer": "A full profile was written from a canonical fetch and its cache entry is live.",
        "next_step": "Nothing to do.",
    },
    "enriched_provisional": {
        "label": "🌗 Enriched (provisional)",
        "explainer": "A profile was written from the fast pass; the slow sources (MusicBrainz, MyAnimeList) are still being merged in the background.",
        "next_step": "Promoted automatically once the slow sources have answered.",
    },
    "enriched_dead": {
        "label": "🪦 Enriched, cache gone",
        "explainer": "The status flag says enriched but the cached profile has expired or was purged.",
        "next_step": "Re-queued automatically on the next enrichment run.",
    },
    "rule_based": {
        "label": "🔧 Rule-based only",
        "explainer": "A heuristic profile was written because the LLM was unavailable; it is thinner than a real profile.",
        "next_step": "Retried for an LLM upgrade on the next run.",
    },
    "awaiting_llm": {
        "label": "⏳ Awaiting LLM",
        "explainer": "The API data is cached but the LLM polish was paused (game mode).",
        "next_step": "Finished on the next run once the GPU is free.",
    },
    "retry_due": {
        "label": "🔁 Retry due",
        "explainer": "No source knew this title so far; its waiting time is over and the next run tries again.",
        "next_step": "Runs automatically — or supply an id under Needs attention to skip the guessing.",
    },
    "not_found": {
        "label": "❌ Not found (waiting)",
        "explainer": "Every consulted source missed, or the only hit was a same-named work from the wrong year. Retries back off: two quick tries, then 3, 6, 12, 24 days, then monthly — forever.",
        "next_step": "Wait, or pin the right entry / an external id under Needs attention.",
    },
    "queued": {
        "label": "🕒 Queued",
        "explainer": "Tracked but not processed yet — freshly seeded, reset by an admin, or its API data is waiting for the polish.",
        "next_step": "Picked up by the next enrichment run.",
    },
    "processing_error": {
        "label": "⚠️ Processing error",
        "explainer": "The pipeline crashed on this item and recorded the error.",
        "next_step": "Retried on every run; a persistent error shows up in the log.",
    },
    "ignored": {
        "label": "🚫 Ignored",
        "explainer": "The owner accepted that this item stays unenriched. Excluded from retries and from the open count.",
        "next_step": "Un-ignore under Needs attention to retry.",
    },
    "never_processed": {
        "label": "📋 Never processed",
        "explainer": "No tracking row yet — the item has not reached the enrichment queue.",
        "next_step": "Start an enrichment run.",
    },
}

assert tuple(STATE_DEFINITIONS) == STATES, "STATE_DEFINITIONS must list every state in order"


def classify_enrichment_row(row: Any, has_live_cache: Optional[bool] = None,
                            now: Optional[datetime] = None) -> str:
    """Map one EnrichmentStatus row (or any object with ``enriched``,
    ``error``, ``provisional``, ``next_retry_at``) to exactly one state.

    Status-derived states come FIRST; cache liveness is consulted only for
    the enriched branch. This is what stops a live not-found sentinel from
    reading as "enriched" (the fresh-miss bug). ``has_live_cache=None``
    means the caller has no cache information (SQL-only callers) — then an
    enriched row is simply "enriched"."""
    if row is None:
        return "never_processed"
    error = (getattr(row, "error", None) or "").strip()
    el = error.lower()
    if el.startswith("ignored"):
        return "ignored"
    if not getattr(row, "enriched", False):
        return "processing_error" if error else "queued"
    if el.startswith(NOT_FOUND_PREFIX.lower()):
        nra = getattr(row, "next_retry_at", None)
        return "retry_due" if is_due(nra, now) else "not_found"
    if el.startswith("api_cached"):
        return "awaiting_llm"
    if el.startswith("rule_based"):
        return "rule_based"
    if error:
        return "processing_error"
    if has_live_cache is False:
        return "enriched_dead"
    if getattr(row, "provisional", False):
        return "enriched_provisional"
    return "enriched"


# ── why is it still open? ─────────────────────────────────────────────────────

SOURCE_LABELS = {
    "tmdb": "TMDB", "omdb": "OMDb", "anilist": "AniList", "jikan": "MyAnimeList",
    "mb": "MusicBrainz", "lastfm": "Last.fm",
}
CONTEXT_KEY = "_context"   # reserved key inside the sources_state JSON


def parse_sources_state(value: Any) -> dict:
    """``sources_state`` column (JSON text or dict) → dict; never raises."""
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def source_outcomes(state: dict) -> dict[str, str]:
    """Per-source status map without the reserved context entry."""
    out = {}
    for src, entry in (state or {}).items():
        if src.startswith("_"):
            continue
        status = entry.get("status") if isinstance(entry, dict) else entry
        if status:
            out[src] = str(status)
    return out


def _fmt_day(dt: Optional[datetime]) -> str:
    return dt.strftime("%d %b") if isinstance(dt, datetime) else "?"


def open_reason(error: Optional[str], sources_state: Any = None, *,
                attempt_count: int = 0, next_retry_at: Optional[datetime] = None,
                title: str = "", category: str = "", now: Optional[datetime] = None) -> str:
    """ONE sentence a user can act on (SoulSync ``exhausted_reason`` idea):
    which sources said what, what the wrong hit was, how often we tried and
    when the next try is."""
    state = parse_sources_state(sources_state)
    ctx = state.get(CONTEXT_KEY) if isinstance(state.get(CONTEXT_KEY), dict) else {}
    reason = not_found_reason(error)
    if reason is None:
        # Not a not-found row: explain the state itself.
        pseudo = type("R", (), {"enriched": True, "error": error, "provisional": False,
                                "next_retry_at": None})()
        st = classify_enrichment_row(pseudo, now=now) if error else "enriched"
        return STATE_DEFINITIONS.get(st, STATE_DEFINITIONS["enriched"])["explainer"]

    resolved = ctx.get("resolved") if isinstance(ctx.get("resolved"), dict) else {}
    arr_year = ctx.get("arr_year")
    label_year = f" ({arr_year})" if arr_year else ""
    parts: list[str] = []
    if reason == REASON_YEAR and resolved:
        parts.append(
            f"Found '{resolved.get('title') or '?'}' ({resolved.get('year') or '?'}) "
            f"but the arr says{label_year or ' another year'} — pin the right entry")
    elif reason == REASON_LOW_CONFIDENCE and resolved:
        parts.append(
            f"Best hit '{resolved.get('title') or '?'}' ({resolved.get('year') or '?'}) "
            f"is too far from '{title or '?'}'{label_year} — pin the right entry")
    else:
        outcomes = source_outcomes(state)
        said: list[str] = []
        for src, status in outcomes.items():
            name = SOURCE_LABELS.get(src, src)
            if status == "miss":
                said.append(f"{name}: no match")
            elif status == "transient":
                said.append(f"{name} was unavailable")
            elif status == "skipped":
                said.append(f"{name} skipped (fast pass)")
            elif status == "ok":
                said.append(f"{name} answered but not enough for a profile")
        had = set(ctx.get("had_ids") or [])
        if category in ("movie", "show") and "omdb" not in outcomes and "imdb_id" not in had:
            said.append("OMDb not asked — no IMDb id from the arr")
        if not said:
            said.append("no source knew this title")
        parts.append("; ".join(said))
    tries = int(attempt_count or 0)
    if tries:
        tail = f"tried {tries}×"
        tail += (f", next try {_fmt_day(next_retry_at)}" if next_retry_at and not is_due(next_retry_at, now)
                 else ", due on the next run")
        parts.append(tail)
    return " · ".join(parts)


# ── match quality (SoulSync: score it, store it, three outcomes) ──────────────

import re as _re
from difflib import SequenceMatcher as _SM

REVIEW_THRESHOLD = 0.8     # below: accepted, but flagged for the owner
REFUSE_THRESHOLD = 0.5     # below (and the year disagrees or is unknown): refused

MATCH_BASES = ("pin", "arr_id", "identity", "title_search")

# Which ids make a resolution id-based (authoritative) per category.
ID_KEYS_BY_CATEGORY = {
    "movie": ("tmdb_id", "imdb_id"),
    "show":  ("tmdb_id", "tvdb_id", "imdb_id"),
    "anime": ("anilist_id", "mal_id", "tvdb_id"),
    "music": ("mbid",),
}

# The source whose hit decides a title-search resolution, per category.
RESOLVING_SOURCE = {
    "movie": ("tmdb", "omdb"), "show": ("tmdb", "omdb"),
    "anime": ("anilist", "jikan"), "music": ("mb", "lastfm"),
}


def normalize_title(s: Optional[str]) -> str:
    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return _SM(None, na, nb).ratio()


def years_agree(arr_year, hit_year) -> Optional[bool]:
    """True / False when both years are known, None when either is not."""
    try:
        ay, hy = int(arr_year or 0), int(hit_year or 0)
    except (TypeError, ValueError):
        return None
    if not ay or not hy:
        return None
    return abs(ay - hy) <= 1


def match_score(arr_title, arr_year, hit_title, hit_year, alt_titles=()) -> float:
    """0..1 — title similarity (best over the hit's names), nudged by the
    year: agreement (±1 y) adds 0.15, disagreement takes 0.2."""
    best = max([title_similarity(arr_title, t) for t in (hit_title, *alt_titles) if t] or [0.0])
    agree = years_agree(arr_year, hit_year)
    if agree is True:
        best = min(1.0, best + 0.15)
    elif agree is False:
        best = max(0.0, best - 0.2)
    return round(best, 3)


def match_decision(score: float, arr_year=None, hit_year=None) -> str:
    """``accept`` / ``review`` / ``refuse``. No match is better than a wrong
    match — but a same-year hit with an odd name (localised title) is kept
    for review rather than refused."""
    if score >= REVIEW_THRESHOLD:
        return "accept"
    if score >= REFUSE_THRESHOLD:
        return "review"
    return "review" if years_agree(arr_year, hit_year) is True else "refuse"


def resolve_match_basis(category: str, pin: Optional[dict], caller_ids: Optional[dict],
                        resolved_ids: Optional[dict]) -> str:
    """Which authority resolved the entity: the owner's pin, an id the arr
    handed over, an id we resolved ourselves (MediaIdentity / cross-ref),
    or nothing but the title."""
    keys = ID_KEYS_BY_CATEGORY.get(category, ID_KEYS_BY_CATEGORY["movie"])
    if any((pin or {}).get(k) for k in keys):
        return "pin"
    if any((resolved_ids or {}).get(k) for k in keys):
        return "arr_id" if any((caller_ids or {}).get(k) for k in keys) else "identity"
    return "title_search"


_INT_ID_KINDS = ("tmdb_id", "tvdb_id", "anilist_id", "mal_id")


def parse_rejected_ids(value: Any) -> dict[str, set]:
    """Negative pins — ``'[{"tmdb_id": 1}, {"mbid": "x"}]'`` (or the parsed
    list) → ``{"tmdb_id": {1}, "mbid": {"x"}}``. Never raises."""
    if not value:
        return {}
    entries = value
    if isinstance(value, str):
        try:
            entries = json.loads(value)
        except (TypeError, ValueError):
            return {}
    out: dict[str, set] = {}
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        for k, v in e.items():
            if v in (None, ""):
                continue
            if k in _INT_ID_KINDS:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            else:
                v = str(v).strip()
            out.setdefault(k, set()).add(v)
    return out


# ── needs attention ───────────────────────────────────────────────────────────

ATTENTION_REASONS: dict[str, dict[str, str]] = {
    "year_mismatch": {
        "label": "Wrong year", "severity": "error",
        "explainer": "A same-named work was found, but its year is far off the arr's — most likely a different title. Pin the right one.",
    },
    "low_confidence": {
        "label": "Rejected hit", "severity": "error",
        "explainer": "The only candidate was too far from the arr's title and year; nothing was written. Pin the right entry.",
    },
    "needs_review": {
        "label": "Unsure match", "severity": "warning",
        "explainer": "Matched by title search with middling similarity. Confirm it by pinning, or exclude the candidate.",
    },
    "not_found_repeatedly": {
        "label": "Not found (2+ tries)", "severity": "info",
        "explainer": "No source knew this title in two or more rounds. Supply an id, or ignore it.",
    },
    "no_external_id": {
        "label": "No id in the arr", "severity": "info",
        "explainer": "The arr item carries no TMDB / TVDB / IMDb / MusicBrainz id — only the arr can fix that.",
    },
}
ATTENTION_ORDER = tuple(ATTENTION_REASONS)


def attention_reasons(state: str, error: Optional[str], attempt_count: int = 0,
                      match_basis: Optional[str] = None,
                      match_confidence: Optional[float] = None) -> list[str]:
    """Why an item belongs on the Needs-attention page (empty = it doesn't)."""
    reasons: list[str] = []
    if state in ("not_found", "retry_due"):
        r = not_found_reason(error)
        if r in (REASON_YEAR, REASON_LOW_CONFIDENCE):
            reasons.append(r)
        elif int(attempt_count or 0) >= 2:
            reasons.append("not_found_repeatedly")
    if (state in DONE_STATES and match_basis == "title_search"
            and match_confidence is not None and float(match_confidence) < REVIEW_THRESHOLD):
        reasons.append("needs_review")
    return reasons
