"""
Curatarr - Chat Router
Streaming Ollama with RAG, taste context, and persistent conversation memory.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import datetime
from typing import AsyncGenerator

from src.services.llm_utils import (
    ThinkTagStreamFilter, clean_llm_text, ollama_options, strip_think_tags,
    curator_options, CURATOR_NUM_CTX,
    CURATOR_KEEP_ALIVE, SUMMARIZER_KEEP_ALIVE,
    detect_user_language, language_directive,
)
from src.config import settings as _cfg

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.models import ConversationMessage, User
from src.routers.auth import get_current_user
from src.schemas.chat import ChatMessage
from src.config import settings
from src.services.plex_sync import get_user_taste_context
from src.services.media_enricher import enrich_media_item

logger = logging.getLogger(__name__)
router = APIRouter()

CONVERSATION_WINDOW = 20            # last N messages to include as context
CONVERSATION_WINDOW_TOPIC_SWITCH = 4 # smaller window when the user pivots to a new title
_HISTORY_CHAR_BUDGET = 8000         # hard char budget for history (context diet)
_ASSISTANT_CLIP = 700               # clip OLD assistant monologues to this many chars


# ── HELPERS ───────────────────────────────────────────────────────────────────

async def _check_verification_response(user_id: int, user_message: str, thread_id: str):
    """Check if the user is answering a pending verification question.

    A verification question is a ``ProactiveMessage`` (trigger_type
    "verification"); the user answers it by chatting IN ITS THREAD
    (``proactive_message:{id}``), exactly like discussing any other
    proactive message.

    Pass 71: gate strictly on that thread. Before this, ``_check_verification_response``
    ran after EVERY chat turn and claimed the *newest* pending verification
    regardless of where the user was — so a message in general chat, a
    deletion-proposal discussion, or an unrelated proactive thread would
    consume the question: a wasted summarizer call at best, and at worst the
    question got marked ``read=True`` by a message that wasn't its answer,
    so the user's real answer later never matched. The thread IS the
    relevance signal — deterministic, no LLM. For non-verification threads
    it now returns before touching the DB at all.

    Atomic claim-then-process: flip ``read=False → True`` on the question;
    rowcount 1 means we own it, 0 means a concurrent request already did.

    Pass 46 (Bug 1): if ``process_verification_response`` fails (LLM
    non-200, JSON parse error, exception), we revert the claim so the
    user's answer isn't silently lost.
    """
    # Only a chat turn INSIDE the verification question's own thread can be
    # an answer to it. General chat / deletion threads / other proactive
    # threads bail here, before any DB work.
    if not thread_id or not thread_id.startswith("proactive_message:"):
        return
    try:
        msg_id = int(thread_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    pending_id = None
    try:
        from src.services.verification_session import process_verification_response
        from src.database.connection import get_db_session
        from src.database.models import ProactiveMessage
        with get_db_session() as db:
            # The question must BE this thread — a pending verification row
            # with this exact id. A non-verification proactive message, or
            # an already-answered one, finds nothing and returns.
            pending = db.query(ProactiveMessage).filter(
                ProactiveMessage.id == msg_id,
                ProactiveMessage.user_id == user_id,
                ProactiveMessage.trigger_type == "verification",
                ProactiveMessage.read == False,
            ).first()
            if not pending:
                return

            # Atomic claim — UPDATE ... WHERE read=False returns rowcount=1 only
            # for the first caller, 0 for any concurrent retry.
            claimed = db.query(ProactiveMessage).filter(
                ProactiveMessage.id == pending.id,
                ProactiveMessage.read == False,
            ).update({"read": True}, synchronize_session=False)
            db.commit()
            if not claimed:
                return  # someone else got it

            pending_id = pending.id
            import json as _json
            question = _json.loads(pending.trigger_data or "{}")

        processed = await process_verification_response(user_id, user_message, question)
        if not processed:
            _revert_verification_claim(pending_id)
    except Exception as e:
        logger.debug("Verification response check failed: %s", e)
        if pending_id is not None:
            _revert_verification_claim(pending_id)


def _revert_verification_claim(pending_id: int) -> None:
    """Best-effort flip of ``read`` back to False after a failed process."""
    try:
        from src.database.connection import get_db_session
        from src.database.models import ProactiveMessage
        with get_db_session() as db:
            db.query(ProactiveMessage).filter(
                ProactiveMessage.id == pending_id,
            ).update({"read": False}, synchronize_session=False)
            db.commit()
        logger.info("Verification claim %d reverted — user can answer again", pending_id)
    except Exception as e:
        logger.warning("Failed to revert verification claim %d: %s", pending_id, e)


_YEAR_RE = __import__("re").compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

# German keyboard typo: "ß" sits between "0" and Backspace, so a fast
# typist intending "0" sometimes hits "ß" instead. Most commonly seen in
# year mentions ("FBI: Most Wanted from 202ß" → user meant "2020"). We
# normalise ß → 0 ONLY when it's adjacent to digits, so legitimate German
# words (Straße, Maß, weiß) aren't touched.
_BETA_NEAR_DIGIT = __import__("re").compile(r"(\d)ß(\d?)|ß(\d)")


def _normalize_typos(text: str) -> str:
    """Fix common keyboard typos before the metadata pipeline sees them.

    Currently handles:
    - ß → 0 when adjacent to a digit (German keyboard slip)

    Conservative by design: only touches the metadata-pipeline copy of
    the query. The user's original text still goes into conversation
    history + the curator system prompt unchanged, so Curatarr's reply
    references what the user actually typed.
    """
    if not text:
        return text
    fixed = _BETA_NEAR_DIGIT.sub(
        lambda m: (m.group(1) or "") + "0" + (m.group(2) or m.group(3) or ""),
        text,
    )
    if fixed != text:
        logger.info("[chat] typo-normalize: %r -> %r", text, fixed)
    return fixed

# In-memory cache keyed by thread_id → (active_title, payload, domain).
# Survives only until server restart, which is fine: a missed cache costs at
# most one re-fetch round-trip. Persisting per-DB row would be over-
# engineering for what is essentially "remember the last subject for the
# next few messages".
#
# Pass 72: bounded LRU. It used to be a plain dict whose only eviction was
# explicit "New chat" / anchor-correction — so it leaked one entry per
# discussion thread ever opened (every deletion proposal, every proactive
# message, every free-chat title), growing unbounded across a long-running
# process. Capped now; all WRITES must go through _set_thread_active_title.
_THREAD_TITLE_CAP = 64
_thread_active_title: "OrderedDict[str, tuple]" = OrderedDict()


def _set_thread_active_title(thread_id: str, value: tuple) -> None:
    """Write to the bounded thread-anchor cache, evicting the oldest-touched
    entry once over capacity (Pass 72).

    ``move_to_end`` refreshes a thread's recency on every re-write — and an
    actively-discussed thread IS re-written every turn (``_build_discuss_context_block``
    re-runs each discuss turn), so the cap only ever evicts genuinely-stale
    threads. A re-fetch on a rare evicted-then-revisited thread is exactly
    the "missed cache" cost the comment above already calls acceptable.
    """
    _thread_active_title[thread_id] = value
    _thread_active_title.move_to_end(thread_id)
    while len(_thread_active_title) > _THREAD_TITLE_CAP:
        _thread_active_title.popitem(last=False)

# Heuristic: does this user message likely introduce a NEW media title? If
# yes, we re-run entity detection. If no, we keep using the cached title.
# Cheap enough to run on every turn — no LLM call, just regex.
_TITLE_HINT_PATTERNS = [
    __import__("re").compile(p, __import__("re").IGNORECASE)
    for p in (
        r"\btell me about\b",
        r"\bwhat about\b",
        r"\bdo you know\b",
        r"\bwas h[äa]ltst du von\b",
        r"\bkennst du\b",
        r"\bgib mir.*(zu|über)\b",
        r"\bsuche nach\b",
        r"\blooking for\b",
        r"\b(film|movie|show|serie|series|anime|track|song|album|band|artist)\b",
        r'"[A-Z][^"]{2,}"',           # quoted phrase starting capitalized
        r"'[A-Z][^']{2,}'",           # same with single quotes
    )
]
_CAPITAL_PHRASE_RE = __import__("re").compile(
    r"\b([A-Z][a-zA-Z0-9'-]+(?:\s+[A-Z][a-zA-Z0-9'-]+){1,})\b"
)


def _looks_like_title_introduction(query: str) -> bool:
    """Return True if the user message likely names a new media title.

    Used to gate entity-detection so follow-up replies ("yes", "tell me
    more", "no I disagree") don't burn an LLM call. Hint patterns plus
    a fallback "two-or-more capitalized words" check catch most cases.
    """
    if not query or len(query) < 3:
        return False
    for pat in _TITLE_HINT_PATTERNS:
        if pat.search(query):
            return True
    # Fallback: at least one multi-word capitalized phrase that isn't just
    # the start of a sentence (which we can't easily distinguish — the
    # cheap version: require the phrase not to be the literal first word).
    m = _CAPITAL_PHRASE_RE.search(query)
    if m and m.start() > 0:
        return True
    return False


def _extract_year_hint(query: str) -> int | None:
    """Pull a 4-digit year (1950–2049) out of the user's free-form query.

    Used as a disambiguation hint for TMDB/IMDb searches when the user types
    "FNAF 2 from 2025" or "Jesus Shows You the Way to the Highway 2019".
    Title-collision (multiple films with the same/similar name in different
    years) is the leading cause of the curator picking the wrong record and
    then confidently building a wall of false facts on top of it.

    Pass 14.14: query is typo-normalised first ("202ß" → "2020").
    """
    m = _YEAR_RE.search(_normalize_typos(query))
    return int(m.group(1)) if m else None


_re = __import__("re")

# Patterns that pull a title directly from the user's message when the
# summarizer LLM returns nothing useful. Matches by intent — "tell me about
# X", quoted phrases, "called X" — rather than relying on the small LLM's
# JSON shape. Order matters: more specific patterns first.
# Pass 14.4: dropped `[A-Z]` requirement on the captured group. The whole
# pattern is already IGNORECASE; requiring uppercase first letter cut off
# casual lowercase queries ("tell me about vessel"). Also widened the
# trigger phrases to catch "what do you think about", "the album X by Y",
# "what is X", etc.
_TITLE_REGEX_FALLBACKS = [
    # quoted: "Hard to Be a God"
    _re.compile(r'"([^"]{2,80})"'),
    _re.compile(r"'([^']{2,80})'"),
    # the album/track/song/film/movie/show/series/anime X (by Y)?
    _re.compile(
        r'\bthe\s+(?:album|track|song|film|movie|show|series|anime|band|artist)\s+([^,.?!]{2,80}?)(?:\s+by\s|$|[,.?!])',
        _re.IGNORECASE,
    ),
    # called X / titled X / named X / namens X
    _re.compile(
        r'\b(?:called|titled|named|namens)\s+"?([^",.?!]{2,80}?)"?(?=\s|$|[,.?!])',
        _re.IGNORECASE,
    ),
    # tell me about X / what (do you think) about X / what is X / what's X
    _re.compile(r'\btell me about\s+([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r'\bwhat\s+(?:do you think\s+)?about\s+([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r"\bwhat(?:'s|\s+is)\s+([^,.?!]{2,80})", _re.IGNORECASE),
    _re.compile(r'\bdo you know\s+([^,.?!]{2,80})', _re.IGNORECASE),
    # "tell me everything you know about X" / "what do you know about X" —
    # the Kill la Kill opener matched NO pattern and the whole first message
    # ran without any metadata anchor.
    _re.compile(r'\bknow about\s+([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r'\bhave you (?:heard|seen)\s+(?:of\s+)?([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r'\bkennst du\s+([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r'\bwas h[äa]ltst du von\s+([^,.?!]{2,80})', _re.IGNORECASE),
    _re.compile(r'\berz[äa]hl(?:e|\s)+(?:mir\s+)?(?:[üu]ber\s+|von\s+)([^,.?!]{2,80})', _re.IGNORECASE),
]


def _extract_title_via_regex(query: str) -> str | None:
    """Best-effort title extraction from the user message.

    Used as a fallback when the summarizer's MODE 6 entity extraction
    returns nothing. The summarizer model can fail in two ways:
    (a) it returns a dict-shape we don't recognise, (b) it returns an empty
    string for unfamiliar/exotic titles. In either case we'd rather try a
    regex match than give the curator no anchor at all.

    Strips trailing year mentions like "from 2019" / "2013" so the title
    matches what TMDB expects.
    """
    if not query or len(query) < 4:
        return None
    for pat in _TITLE_REGEX_FALLBACKS:
        m = pat.search(query)
        if m:
            title = m.group(1).strip()
            # Strip surrounding quotes if any
            title = title.strip("\"'")
            # Trim trailing "from YYYY" / "(YYYY)" / "YYYY"
            title = _re.sub(r'\s+(?:from\s+)?\(?(?:19|20)\d{2}\)?$', '', title).strip()
            # Cut at "by Y" — "the album Vessel by Sleep Token" -> "Vessel"
            title = _re.sub(r'\s+by\s+.*$', '', title, flags=_re.IGNORECASE).strip()
            # Trim trailing filler words. Pass 14.10 added "then/now/actually/
            # please/already" — the LLM entity extractor sometimes captures
            # "tell me about the band sleep token then" → "sleep token then".
            # Loop until no more trailing fillers — handles "Dune actually
            # please" → "Dune".
            _filler_re = _re.compile(
                r'\s+(?:from|in|at|by|von|aus|then|now|actually|please|'
                r'already|recently|today|tonight|tomorrow|maybe|though|anyway)$',
                _re.IGNORECASE,
            )
            while True:
                stripped = _filler_re.sub('', title).strip()
                if stripped == title:
                    break
                title = stripped
            if 2 <= len(title) <= 100:
                return title
    return None


_LIB_TITLE_INDEX: dict = {"ts": 0.0, "items": []}
_LIB_TITLE_TTL_S = 600.0


def _norm_for_match(s: str) -> str:
    s = _re.sub(r"[‐‑‒–—−]", "-", (s or "").lower())
    return _re.sub(r"[^a-z0-9]+", " ", s).strip()


def _library_title_index() -> list:
    """[(norm, title, category)] over the user's OWN library (sonarr/radarr/
    lidarr caches), rebuilt at most every 10 minutes. This is deterministic
    ground truth — 25k known names — so extraction can't be defeated by
    phrasing, typos around the title, or non-native grammar."""
    import time as _time
    if _time.time() - _LIB_TITLE_INDEX["ts"] < _LIB_TITLE_TTL_S and _LIB_TITLE_INDEX["items"]:
        return _LIB_TITLE_INDEX["items"]
    items = []
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            for svc, cat_default, name_key in (("sonarr", "show", "title"),
                                               ("radarr", "movie", "title"),
                                               ("lidarr", "music", "artistName")):
                hit = mc.get_cache(f"arr_library:{svc}")
                resp = (hit or {}).get("response")
                rows = (resp if isinstance(resp, list)
                        else (resp or {}).get("items_raw") or (resp or {}).get("items") or [])
                for it in rows:
                    if not isinstance(it, dict):
                        continue
                    name = it.get(name_key)
                    if not name:
                        continue
                    cat = cat_default
                    if svc == "sonarr" and "anime" in (it.get("rootFolderPath") or "").lower():
                        cat = "anime"
                    n = _norm_for_match(name)
                    if len(n) >= 4:
                        items.append((n, name, cat))
        finally:
            mc.close()
    except Exception as e:
        logger.debug("[chat] library title index failed: %s", e)
    if items:
        _LIB_TITLE_INDEX.update(ts=_time.time(), items=items)
    return _LIB_TITLE_INDEX["items"] or items


# words that mark a media reference sitting NEXT to a one-word title
_MEDIA_ADJ_WORDS = {
    "movie", "film", "films", "anime", "show", "series", "season", "episode",
    "episodes", "album", "song", "track", "band", "watch", "watched",
    "watching", "seen", "rewatch", "documentary", "serie", "staffel",
    "folge", "geschaut", "gesehen", "schauen",
}


def _match_library_title(message: str) -> tuple[str, str] | None:
    """Longest library title contained verbatim (normalized, word-bounded)
    in the message → (title, category). None when nothing matches.

    Single-word titles are collision bait — the library's film 'Trust'
    matched "you shouldn't trust a studio's reputation" and hijacked the
    anchor mid-conversation. A one-word title only counts when the original
    message capitalizes it exactly, quotes it, or names a media word within
    two tokens of it. Multi-word titles stay unconditional."""
    msg = f" {_norm_for_match(message)} "
    if len(msg) < 6:
        return None
    tokens = msg.split()
    best = None
    for n, title, cat in _library_title_index():
        if f" {n} " not in msg:
            continue
        if " " not in n:
            cap = _re.search(rf"\b{_re.escape(title)}\b", message)          # exact case
            quoted = _re.search(rf"[\"'„»]{_re.escape(n)}[\"'“«]", message, _re.I)
            if not cap and not quoted:
                idxs = [i for i, t in enumerate(tokens) if t == n]
                near = any(tokens[j] in _MEDIA_ADJ_WORDS
                           for i in idxs
                           for j in range(max(0, i - 2), min(len(tokens), i + 3)))
                if not near:
                    continue
        if best is None or len(n) > len(best[0]):
            best = (n, title, cat)
    return (best[1], best[2]) if best else None


async def _detect_media_in_query(query: str) -> tuple[str | None, int | None]:
    """
    Three-pass title extraction:

      0. Deterministic LIBRARY SCAN: the longest library title contained
         verbatim in the message wins. Phrasing-proof — "Kill la Kill
         sounds interesting please ellaborate" defeated both passes below
         while the title sat literally in the message (and in the library).
      1. Call curatarr-summarizer MODE 6 (LLM, JSON output)
      2. If LLM returned nothing usable, fall back to regex patterns
         on the user's literal message ("tell me about X", quoted phrases)

    The regex fallback is what keeps obscure / exotic titles working when
    the summarizer model doesn't recognise them as media entities. Without
    it, queries like "Hexenkönigin und der Datendieb" silently skipped
    the metadata pipeline entirely.

    Hard timeout: 10s. Returns (title or None, year or None).

    Pass 14.14: query is typo-normalised before BOTH the LLM call and the
    regex fallback. The user-facing original is left alone — only the
    metadata-pipeline copy gets fixed.
    """
    normalized_query = _normalize_typos(query)
    year = _extract_year_hint(normalized_query)

    # Pass 0: the user's own library is deterministic ground truth.
    lib_hit = _match_library_title(normalized_query)
    if lib_hit:
        logger.info("[chat] entity matched against library: %r (%s)",
                    lib_hit[0], lib_hit[1])
        return lib_hit[0], year

    extraction_model = getattr(_cfg, "SUMMARIZER_MODEL", None) or "curatarr-summarizer"
    prompt = f"[MODE: ENTITY EXTRACTION]\nInput: {normalized_query}"

    raw_output = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{_cfg.effective_ollama}/api/chat",
                json={
                    "model": extraction_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.0, num_predict=80),
                },
            )
        if r.status_code == 200:
            raw_output = r.json().get("message", {}).get("content", "").strip()

    except Exception as e:
        logger.debug("[chat] entity extraction LLM error: %s", e)

    # Try to parse the LLM output. Accept several shapes the summarizer
    # might emit: bare {"title": "X"}, nested {"output": {"title": "X"}},
    # alternative keys, or even a plain string.
    title = None
    if raw_output:
        content = clean_llm_text(raw_output)
        if content:
            import json as _json
            try:
                parsed = _json.loads(content)
                title = _extract_title_from_dict(parsed)
            except _json.JSONDecodeError:
                # Plain-text response from older / non-JSON-mode models
                if 0 < len(content) < 80 and not content.startswith("{"):
                    title = content.strip().strip("\"'")

    # LLM extraction failed — try regex fallback on the user's own message.
    if not title:
        title = _extract_title_via_regex(normalized_query)
        if title:
            logger.info("[chat] entity extracted via regex fallback: %r (year=%s)",
                        title, year)
        elif raw_output:
            # LLM responded but with an unknown shape — log so we can debug
            # the modelfile if this happens often.
            logger.debug("[chat] entity LLM returned unparseable shape: %r",
                         raw_output[:200])

    # Pass 14.10: trim trailing filler words even when the LLM extracted the
    # title cleanly. The summarizer occasionally captures sentence trailers
    # ("tell me about the band sleep token then" → "sleep token then").
    if title:
        _filler_re = _re.compile(
            r'\s+(?:then|now|actually|please|already|recently|today|tonight|'
            r'tomorrow|maybe|though|anyway)$',
            _re.IGNORECASE,
        )
        while True:
            stripped = _filler_re.sub('', title).strip()
            if stripped == title:
                break
            title = stripped
        title = title.strip("\"'") or None

    # Anaphora guard: "tell me about the studio behind it" extracted
    # 'the studio behind it' as a TITLE, cascaded through every domain,
    # flagged a topic switch and DROPPED the real anchor (Kill la Kill).
    # A follow-up question about the current title is not a new entity —
    # returning None keeps the thread's cached anchor and context alive.
    if title and _looks_anaphoric(title):
        logger.info("[chat] extracted %r is anaphoric — keeping current anchor", title)
        return None, year

    if title:
        logger.info("[chat] entity extracted: %r (year hint=%s)", title, year)
    return title, year


_ANAPHORIC_BARE = {"it", "that", "this", "them", "these", "those", "him", "her",
                   "the same", "the one", "the show", "the movie", "the anime",
                   "the film", "the series", "the album", "the band", "the studio",
                   "the director"}


def _looks_anaphoric(title: str) -> bool:
    t = _re.sub(r"\s+", " ", (title or "").strip().lower())
    if t in _ANAPHORIC_BARE:
        return True
    # "the studio behind it", "the director of that", "the sequel to this"
    return bool(_re.search(r"\b(?:behind|of|to|about|for|from)\s+"
                           r"(?:it|that|this|them|those)$", t))


def _extract_title_from_dict(parsed) -> str | None:
    """Extract a title from common LLM output shapes:
    - {"title": "X"}
    - {"media_title": "X"} / {"name": "X"} / {"entity": "X"}
    - {"output": {"title": "X"}} / {"result": {"title": "X"}}
    - [{"title": "X"}] (single-item list)
    """
    if isinstance(parsed, list) and parsed:
        return _extract_title_from_dict(parsed[0])
    if not isinstance(parsed, dict):
        return None
    # Direct keys
    for key in ("title", "media_title", "name", "entity", "value"):
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Nested wrappers
    for wrapper in ("output", "result", "data", "extracted"):
        inner = parsed.get(wrapper)
        if inner is not None:
            sub = _extract_title_from_dict(inner)
            if sub:
                return sub
    return None


# Pass 61: per-exchange memory extraction (``_extract_memories_bg``) was
# removed — it saw only a single user message and latched intermediate
# stances from multi-turn conversations as memories. Memory extraction is
# now DEBOUNCED at the thread level via
# ``episodic_memory.schedule_thread_extraction`` (see the post-chat
# background block below).


# Pass 49: stance-signal heuristics used by the flip-flop detector. They
# match the prompt's own pitch / verdict vocabulary plus a handful of
# natural-language variants. Kept simple on purpose — false negatives
# (missed flip) are fine, false positives (incorrectly skipping a real
# resolution) are the bad case. We'd rather miss a Protection write
# than write one inside a clearly-unsettled negotiation.
_DELETE_STANCE_TOKENS: tuple[str, ...] = (
    "verdict: delete", "verdict:delete",
    "delete it", "delete the", "we should delete",
    "for deletion",  # pitch opener: "I have suggested X for deletion"
    "free the", "deletion stands", "the deletion",
    "doesn't deserve", "it's just bad writing", "lazy writing",
)
_KEEP_STANCE_TOKENS: tuple[str, ...] = (
    "verdict: retain", "verdict:retain",
    "we keep", "keep it", "stays in your library", "the title stays",
    "permanently protected", "stand corrected", "i fold",
    "you're right", "you are right",
)


def _is_in_flipflop_loop(user_id: int, anchor: str, thread_id: str | None = None,
                         window: int = 6) -> bool:
    """Pass 49: detect a curator stance flip-flop on ``anchor`` over the
    last ``window`` assistant turns.

    A "flip" is a transition from a delete-stance utterance to a keep-
    stance one (or vice versa) where BOTH mention the anchor title.
    ≥2 flips inside the window ⇒ we're in an unsettled negotiation;
    the caller bails out without writing protection. Pure DB read, no
    LLM call — cheap to gate on every check.
    """
    if not anchor:
        return False
    try:
        from src.database.connection import get_db_session
        from src.database.models import ConversationMessage
        with get_db_session() as db:
            q = (
                db.query(ConversationMessage.content)
                  .filter(
                      ConversationMessage.user_id == user_id,
                      ConversationMessage.role == "assistant",
                  )
            )
            # audit 11c: without the thread filter, parallel discussions
            # cross-triggered / diluted each other's flip detection
            if thread_id:
                if thread_id == "general":
                    q = q.filter((ConversationMessage.thread_id == "general")
                                 | (ConversationMessage.thread_id.is_(None)))
                else:
                    q = q.filter(ConversationMessage.thread_id == thread_id)
            msgs = (
                q.order_by(ConversationMessage.created_at.desc())
                  .limit(window)
                  .all()
            )
        anchor_lower = anchor.lower()
        stances: list[str] = []
        for (content,) in reversed(msgs):   # chronological order
            text = (content or "").lower()
            if anchor_lower not in text:
                continue
            has_del = any(tok in text for tok in _DELETE_STANCE_TOKENS)
            has_keep = any(tok in text for tok in _KEEP_STANCE_TOKENS)
            # Tie-break: pitch-style turns mention both ("I suggested
            # deletion. Reason: …"). When both signals fire we treat it
            # as 'delete' (it was the curator's actual recommendation
            # in that turn).
            if has_del:
                stances.append("delete")
            elif has_keep:
                stances.append("keep")
        flips = sum(1 for a, b in zip(stances, stances[1:]) if a != b)
        return flips >= 2
    except Exception as e:
        # Heuristic must never block the happy path — bail out as "no
        # flip-flop" so the regular detector runs unchanged.
        logger.debug("[protection] flip-flop probe failed: %s", e)
        return False


async def _check_protection_intent_bg(
    user_id: int,
    user_msg: str,
    assistant_msg: str,
    anchor_title: str | None = None,
    anchor_category: str | None = None,
    thread_id: str | None = None,
):
    """Background task: detect if the user wants to protect a title from deletion.

    Pass 23: ``anchor_title`` resolves pronouns. When the user is in a
    deletion-proposal thread and says "I'm keeping it" / "we are keeping
    this show", the title isn't literally in the user message — but we
    know it from ``_thread_active_title[thread_id]``. Passing it through
    lets the detector treat the anchor as the implicit subject.

    Pass 49 (flip-flop guard): we bail out without invoking the LLM
    detector when the recent conversation shows multiple curator stance
    reversals on ``anchor_title``. Without this guard, the user can
    arrive at a "PROTECT" result by stress-testing the curator — push
    until it caves on "keep", and the detector latches that mid-test
    "Verdict: Retain" turn as a real resolution. The wrapping
    ``analytical_integrity_rule`` in the system prompt is the primary
    defense; this guard is the backstop for when the curator caves
    anyway.

    Pass 66: ``anchor_category`` and ``thread_id`` are threaded through to
    the detector so it can classify consensus-vs-override and write the
    ``CuratorResolutionLog`` row. The flip-flop guard stays unchanged and
    still bails entirely on a thrashing thread — you cannot cleanly
    classify a resolution against a curator whose stance is reversing, and
    a missed log row is the acceptable failure mode (a calm follow-up turn
    re-affirming the keep will be logged normally).
    """
    if anchor_title and _is_in_flipflop_loop(user_id, anchor_title, thread_id=thread_id):
        logger.info(
            "[protection] flip-flop loop on %r — skipping detector for this turn",
            anchor_title,
        )
        return
    try:
        from src.services.episodic_memory import detect_and_handle_protection
        result = await detect_and_handle_protection(
            user_id, user_msg, assistant_msg,
            anchor_title=anchor_title,
            anchor_category=anchor_category,
            thread_id=thread_id,
        )
        if result:
            logger.info("Protection intent handled for user %d: %s", user_id, result)
    except Exception as e:
        logger.debug("Protection intent check failed: %s", e)


_DOMAIN_SIGNALS: dict[str, list[str]] = {
    "music":  ["music", "song", "album", "artist", "track", "listen", "band",
               "playlist", "metal", "rock", "pop", "jazz", "hip hop", "rap",
               "classical", "concert", "lidarr", "last.fm", "spotify"],
    "movie":  ["movie", "film", "cinema", "director", "actor", "radarr",
               "imdb", "box office", "sequel", "prequel", "blockbuster"],
    "anime":  ["anime", "manga", "shonen", "shojo", "isekai", "mecha",
               "crunchyroll", "anilist", "myanimelist", "subtitles", "sub", "dub"],
    "show":   ["series", "show", "episode", "season", "binge", "sonarr",
               "tv", "television", "hbo", "disney+"],
}


def _infer_domain(message: str, discuss_context=None) -> str | None:
    """
    Return the single most-relevant media domain for this request, or None
    when the query is too general to warrant filtering.

    Priority:
      1. Explicit category from discuss_context  (deletion-discuss flow)
      2. Strong single-domain keyword match in the message
      3. None  → no filter (general chat)
    """
    if discuss_context and getattr(discuss_context, "category", None):
        return discuss_context.category

    q = message.lower()
    hits: dict[str, int] = {}
    for domain, keywords in _DOMAIN_SIGNALS.items():
        count = sum(1 for kw in keywords if kw in q)
        if count:
            hits[domain] = count

    if len(hits) == 1:
        return next(iter(hits))      # unambiguous single domain
    if len(hits) > 1:
        # Tie-break: pick the domain with the most keyword hits
        best = max(hits, key=lambda d: hits[d])
        if hits[best] >= 2:          # only filter when signal is strong
            return best
    return None                      # general query — don't restrict RAG


def _domain_cascade(
    message: str,
    discuss_context=None,
    year_hint: int | None = None,
) -> list[str]:
    """
    Return a *sorted* list of domains to try for enrichment, most-likely first.

    Used by the multi-domain enrichment cascade: try the primary domain first,
    fall through to the next if no match. This solves the "It (1990 TV
    miniseries) doesn't show up in /search/movie" class of bug.

    Strategy:
      - If discuss_context fixes a domain → that one only.
      - Else: rank domains by keyword-hit count, then add the rest as fallback.
      - Music has its own pipeline (MusicBrainz) — only included when there's
        an actual music signal, to avoid burning a music lookup on every
        random TV question.

    Pass 15.1: when ``year_hint`` is set AND no music keyword is present,
    music drops out of the cascade entirely. Year-tagged queries like
    "Ghosts (2019)" almost always mean film/tv/anime — pulling in
    MusicBrainz on a year query causes false hits like the "Ghosts" band
    matching the Sitcom query, then the curator sees a music context for
    something that should have been show.
    """
    if discuss_context and getattr(discuss_context, "category", None):
        return [discuss_context.category]

    q = message.lower()
    hits: dict[str, int] = {}
    for domain, keywords in _DOMAIN_SIGNALS.items():
        count = sum(1 for kw in keywords if kw in q)
        if count:
            hits[domain] = count

    # Sort hit-domains by count desc; then append remaining defaults.
    ranked = [d for d, _ in sorted(hits.items(), key=lambda kv: -kv[1])]
    # Default cascade for unknown queries: movie → tv → anime → music.
    # Music ALWAYS lives at the tail (Pass 14.4) — pure-name queries like
    # "King Crimson", "Sleep Token", "Vessel" don't match any music
    # keyword but ARE music. If movie/tv/anime all came back empty, the
    # MusicBrainz pipeline is the right last-ditch attempt before we
    # surrender to the no-metadata anchor.
    defaults = ["movie", "show", "anime", "music"]
    if "music" in hits:
        # Music had explicit keywords — promote to top
        defaults = ["music", "movie", "show", "anime"]
    elif year_hint is not None:
        # Year-tagged query without music keyword → music almost certainly
        # not the right domain. Drop it entirely so we don't burn a
        # MusicBrainz lookup AND don't risk a fuzzy band match standing
        # in for a film/show.
        defaults = ["movie", "show", "anime"]
    for d in defaults:
        if d not in ranked:
            ranked.append(d)
    return ranked


def _profile_matches_query(query: str, profile_name: str, domain: str) -> bool:
    """Pass 32: sanity-check whether a cascade match is actually for the
    user's query. Strict for music — MusicBrainz fuzzy-matches aggressively
    and returns artist profiles for arbitrary substrings (e.g. asking
    about "The Qwaser of Stigmata" matched a "Stigmata" band/track entry,
    routing an anime query to ``domain=music`` with a completely unrelated
    profile attached).

    Comparison: normalize both strings (lowercase, strip punctuation,
    drop articles), tokenize, compute set overlap. The thresholds:

      music     → ALL significant query tokens must appear in the profile
                  (no partial substring wins — "Stigmata" alone is rejected
                  for a "Qwaser of Stigmata" query)
      others    → at least half the query tokens must appear (lets TMDB
                  return localised titles or sequel variants without
                  failing the check)
    """
    import re

    def _tokens(s: str) -> set[str]:
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return {t for t in s.split() if t and t not in {"the", "a", "an", "of"}}

    q = _tokens(query)
    p = _tokens(profile_name)
    if not q or not p:
        return True  # Nothing to compare — fall back to trusting the API

    overlap = len(q & p)
    if domain == "music":
        # Strict — every significant token of the query must appear
        return overlap >= len(q)
    return overlap >= max(1, (len(q) + 1) // 2)


async def _enrich_with_cascade(
    title: str,
    year: int | None,
    domains: list[str],
) -> tuple[dict | None, str | None]:
    """Try enrich_media_item across a domain cascade.

    Returns (enrichment_data, matched_domain) — both None if nothing hit.

    Timeout policy (Pass 14.5): every domain gets 10 s. 6s wasn't enough
    for cold-cache anime lookups: AniList + Jikan come back in ~1s but
    enrich_media_item then runs `summarize_with_small_llm` on the raw
    metadata, which adds 3-5s of LLM-summarisation latency. 6s clipped
    that mid-summary, leaving the user with no metadata even when both
    APIs had successfully responded. 10s × 4 domains = 40s worst case,
    rare in practice — primary hits land in 1-3s.

    No background-fire on timeout. The previous version restarted the same
    enrich_media_item call as a background task on every cascade timeout,
    which produced N× duplicate TMDB/AniList traffic per chat turn — we
    were DDOS'ing our own metadata providers. The MetadataCache inside
    enrich_media_item already saves the result on the next user turn.
    """
    per_domain_timeout = 10.0
    for d in domains:
        try:
            # Pass 14.8: chat cascade uses skip_llm_summary=True. The LLM-
            # summarisation step in enrich_media_item was the bottleneck —
            # API fetches finish in 1-3 s but the LLM polish added another
            # 3-8 s, blowing the cascade timeout. Fast path returns a raw-
            # derived profile with all the fields the curator needs.
            data = await asyncio.wait_for(
                enrich_media_item(
                    title=title, media_type=d, year=year,
                    skip_llm_summary=True,
                ),
                timeout=per_domain_timeout,
            )
        except asyncio.TimeoutError:
            logger.info("[chat] cascade %s: timeout (%.1fs) — moving to next domain",
                        d, per_domain_timeout)
            continue
        except Exception as e:
            logger.debug("[chat] cascade %s: error %s", d, e)
            continue
        if data:
            # Pass 32: reject obviously-mismatched profiles. The music
            # branch is the worst offender (MusicBrainz fuzzy match) but
            # the check is cheap so we apply it everywhere.
            profile_name = data.get("title") or data.get("name") or ""
            if not _profile_matches_query(title, profile_name, d):
                logger.info(
                    "[chat] cascade %s: rejected fuzzy match — query=%r profile=%r (likely false positive)",
                    d, title, profile_name,
                )
                continue
            logger.info("[chat] cascade hit on domain=%s for %r → %r", d, title, profile_name)
            return data, d
    logger.info("[chat] cascade exhausted for %r (year=%s, tried=%s)",
                title, year, domains)
    return None, None


from src.services.watch_status import (
    watched_lookup as _watched_lookup,
    watch_tag as _watch_tag,
)


async def _get_rag_context(query: str, n_results: int = 5, domain: str = None,
                           user_id: int = None) -> str:
    """
    Semantic search over the ChromaDB vector store.
    When *domain* is given, only vectors tagged with that domain are considered,
    eliminating cross-media-type contamination in the context window.

    The retrieval core lives in services.semantic_search — SHARED with the
    user-facing /api/library/semantic-search endpoint; this wrapper keeps the
    chat injection format unchanged.
    """
    from src.services.semantic_search import semantic_hits, format_rag_context
    return format_rag_context(await semantic_hits(
        query, n_results=n_results, domain=domain, user_id=user_id))


def _fmt_field(value, fallback: str = "(not in our database)") -> str:
    """Format a metadata field for the hidden context block.

    Empty values become an explicit "(not in our database)" string so the
    curator can tell missing-from-data apart from training-memory gaps and
    knows it must NOT fill the hole from prior knowledge.
    """
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value)


def _verified_block_for(title: str, domain: str | None,
                        data: dict | None = None) -> str:
    """Cache-only FULL verified block ("" when nothing cached) — the one way
    every strand attaches the complete dossier (format, studio + note,
    critics, reception, franchise, AniDB tags, staff, music fields). Sync
    and cheap: build_verified_data reads the warm cache, no LLM, no network.
    Every context path must end with this — the Kill la Kill session showed
    three strands each rebuilding their own thin subset."""
    try:
        from src.services.media_enricher import build_verified_data, format_verified_block
        # The raw doc lives under ONE category; the caller's domain can be
        # wrong (an AniList timeout made Kill la Kill cascade to domain=show
        # and the anime doc — studio note, reception, tags — went unseen).
        # Cache reads are ~free: try the caller's domain first, then the
        # sibling video categories.
        tried = [domain or "movie"]
        if (domain or "movie") in ("movie", "show", "anime"):
            tried += [d for d in ("anime", "show", "movie") if d not in tried]
        for dom in tried:
            vd = build_verified_data(
                title, dom,
                tmdb_id=(data or {}).get("tmdb_id"),
                tvdb_id=(data or {}).get("tvdb_id"),
                anilist_id=(data or {}).get("anilist_id"),
            )
            if vd and vd.get("title"):
                fvb = format_verified_block(vd)
                if fvb:
                    return "\n" + fvb + "\n"
    except Exception as e:
        logger.debug("[chat] verified block for %r failed: %s", title, e)
    return ""


def _build_hidden_context(
    title: str, data: dict, domain: str = "movie",
    year_hint: int | None = None,
) -> str:
    """Domain-aware hidden-context block.

    The set of fields the curator needs differs sharply by domain:
      - movie/tv: director/creator, cast, country, runtime
      - anime:    studio, director, episodes, source_material, format
      - music:    artist, country, similar_artists, top albums, bio excerpt

    A field that's missing in the data dict is rendered as "(not in our
    database)" rather than left blank, so the NO INVENTION system rule
    has something concrete to anchor against. The curator can see we
    *checked* and the answer was "we don't have it".

    Block header is intentionally aggressive: "VERIFIED METADATA - USE THIS"
    instead of just "[HIDDEN METADATA CONTEXT]". Without that, the curator
    sometimes reads the block, then says "NO VERIFIED METADATA AVAILABLE"
    in the same response — the prompt's own NO INVENTION rule fooling it.

    ``year_hint`` is the year the user typed in their query. When that
    differs from the year we actually fetched (e.g. user asked for 2013,
    TMDB only has 2014), we emit an explicit year-mismatch note so the
    curator doesn't loop on "there is no 2013 film called X".
    """
    year = _fmt_field(data.get("year"))
    fetched_year = data.get("year")
    year_mismatch_note = ""
    if year_hint and fetched_year and int(fetched_year) != int(year_hint):
        year_mismatch_note = (
            f"\n⚠ YEAR NOTE: User asked about year {year_hint}, but our "
            f"verified record for this title is from {fetched_year}. The "
            f"title and metadata below ARE correct — accept the year as "
            f"{fetched_year} and don't claim 'no {year_hint} version exists'."
        )
    rating = (
        f"{data.get('rating')}/10"
        if data.get("rating") not in (None, "", 0)
        else "(no rating)"
    )
    genres = _fmt_field(data.get("genres"))
    synopsis = data.get("plot_summary") or data.get("overview") or data.get("bio") or ""
    if synopsis and len(synopsis) > 600:
        synopsis = synopsis[:600].rsplit(" ", 1)[0] + "…"
    synopsis = synopsis or "(no synopsis)"

    if domain == "music":
        # Music can mean: track, album, or artist. Field set covers all three.
        artist = _fmt_field(data.get("artist") or data.get("name"))
        country = _fmt_field(data.get("country") or data.get("origin_country"))
        similar = _fmt_field((data.get("similar_artists") or [])[:5])
        top_albums = _fmt_field((data.get("top_albums") or [])[:3])
        active_years = _fmt_field(data.get("active_years"))
        # Pass 68: track-level fields (from music_metadata.enrich_track) —
        # only rendered when present, so artist/album lookups aren't padded
        # with empty track lines. An artist-level payload has none of these
        # and the block renders byte-identically to before.
        track_block = ""
        _tags = data.get("tags")
        if _tags or data.get("listeners") or data.get("playcount") or data.get("album"):
            track_block = f"\n- Last.fm tags: {_fmt_field((_tags or [])[:8])}"
            if data.get("album"):
                track_block += f"\n- Album: {data['album']}"
            if data.get("listeners"):
                track_block += f"\n- Last.fm listeners: {data['listeners']}"
            if data.get("playcount"):
                track_block += f"\n- Last.fm playcount: {data['playcount']}"
        return f"""
[VERIFIED METADATA - USE THIS, IT IS REAL DATA]
Item: '{title}' (music){year_mismatch_note}
- Year: {year}
- Artist: {artist}
- Country: {country}
- Genres: {genres}
- Similar artists: {similar}
- Top albums: {top_albums}
- Active years: {active_years}{track_block}
- Bio: {synopsis}
"""

    if domain == "anime":
        studio = _fmt_field(data.get("studios") or data.get("studio"))
        director = _fmt_field(data.get("director"))
        episodes = _fmt_field(data.get("episodes_total") or data.get("episodes"))
        source_mat = _fmt_field(data.get("source_material") or data.get("source"))
        fmt = _fmt_field(data.get("format"))
        original_title = _fmt_field(data.get("original_title"))
        return f"""
[VERIFIED METADATA - USE THIS, IT IS REAL DATA]
Item: '{title}' ({year}, anime){year_mismatch_note}
- Original title: {original_title}
- Studio: {studio}
- Director: {director}
- Format: {fmt}
- Episodes: {episodes}
- Source material: {source_mat}
- Genres: {genres}
- Rating: {rating}
- Synopsis: {synopsis}
"""

    # Default: movie/tv
    director = _fmt_field(data.get("director") or data.get("creator"))
    cast_list = data.get("cast") or []
    cast = _fmt_field(cast_list[:5] if isinstance(cast_list, list) else cast_list)
    country = _fmt_field(data.get("country"))
    runtime = _fmt_field(data.get("runtime"))
    original_title = _fmt_field(data.get("original_title"))
    seasons = _fmt_field(data.get("seasons"))
    episodes = _fmt_field(data.get("episodes_total"))

    series_block = ""
    if domain == "tv" or seasons != "(not in our database)":
        series_block = f"\n- Seasons: {seasons}\n- Episodes: {episodes}"

    return f"""
[VERIFIED METADATA - USE THIS, IT IS REAL DATA]
Item: '{title}' ({year}){year_mismatch_note}
- Original title: {original_title}
- Director/Creator: {director}
- Cast (top 5): {cast}
- Country: {country}
- Runtime: {runtime}
- Genres: {genres}
- Rating: {rating}{series_block}
- Synopsis: {synopsis}
"""


def _build_no_metadata_anchor(title: str) -> str:
    """Anti-hallucination anchor for titles where enrichment returned nothing.

    Without this block, the curator falls back to its training data and
    confidently invents plots, casts, years and directors. The block forces
    an honest "I don't have data" stance — paired with the NO INVENTION
    behavior rule in the system prompt.
    """
    return f"""
[HIDDEN METADATA CONTEXT]
Item: '{title}'
NO VERIFIED METADATA AVAILABLE for this title. The metadata lookup returned
no usable record (TMDB / IMDb / AniList all came back empty or rejected the
match).

Possible reasons:
- The title is unreleased, very recent, or extremely obscure.
- The title is a video game, music album, or other non-film item.
- The title is misspelled or the user means something else.

CRITICAL: You MUST NOT invent any factual claim about this title — no plot,
no cast, no director, no year, no genre, no rating. If the user asks for
facts, say explicitly that you have no verified data and ask them to clarify
which year / genre / source they mean. Use ONLY what the user has stated
about the title in this conversation.
"""


def _thread_id_for(ctx) -> str:
    """Derive a stable thread id from a discuss context.

    Free chat always lives on ``"general"``. Each deletion-proposal /
    proactive-message gets its own thread so history from one topic can't
    bleed into another. The id is derived purely from ``ctx`` (no DB lookup),
    so even if the referenced record is missing the request still routes to
    a valid thread (it just won't have any prior history there).
    """
    if not ctx:
        return "general"
    kind = ctx.kind or ctx.action  # back-compat with legacy `action`
    if (kind == "deletion_proposal" or kind == "deletion") and ctx.proposal_id:
        return f"deletion_proposal:{ctx.proposal_id}"
    if kind == "proactive_message" and ctx.message_id:
        return f"proactive_message:{ctx.message_id}"
    if kind == "principle" and getattr(ctx, "principle_id", None):
        return f"principle:{ctx.principle_id}"
    return "general"


def _filter_memories_for_topic(memories: list, active_title: str) -> list:
    """Drop memories whose ``metadata.title`` is set to a DIFFERENT title.

    Generic taste observations (no title field) stay because they're
    cross-cutting. Title-specific memories about another show / film get
    filtered out so the LLM doesn't pivot the discussion to them.
    """
    if not active_title:
        return memories
    needle = active_title.lower().strip()
    kept = []
    for m in memories:
        meta_title = (m.get("metadata") or {}).get("title", "")
        if not meta_title:
            kept.append(m)
            continue
        meta_lc = str(meta_title).lower().strip()
        if meta_lc == needle or needle in meta_lc or meta_lc in needle:
            kept.append(m)
        # else: memory belongs to another title → drop
    return kept


def _infer_category_for_title(user_id: int, title: str, db: Session) -> str | None:
    """Pass 35: when a state-scan hits a ProtectedMedia row (no category
    column) OR a DeletionProposal whose category was never filled in,
    look up other tables to recover the right domain for the cascade
    override.

    Lookup order, most-specific first:
      1. ArrEnrichmentStatus.category (arr-tracked items — most reliable)
      2. WatchHistoryEntry.media_type (most-frequent type seen for this
         title; covers both episodes and movies the user actually watched)
      3. EnrichmentStatus.media_category (cached enrichment runs)

    Returns the first non-empty hit, or None if nothing knows. The
    returned value goes straight into ``forced_domain`` upstream — so
    a successful inference flips a music-default-falling cascade back
    to the correct media type (anime / show / movie / music).
    """
    from src.database.models import (
        ArrEnrichmentStatus, WatchHistoryEntry, EnrichmentStatus,
    )
    from sqlalchemy import func

    if not title:
        return None

    # 1. ArrEnrichmentStatus — arr items always have category
    try:
        arr_row = db.query(ArrEnrichmentStatus).filter(
            ArrEnrichmentStatus.title == title,
        ).first()
        if arr_row and arr_row.category:
            return arr_row.category
    except Exception as e:
        logger.debug("[chat] _infer_category arr lookup failed: %s", e)

    # 2. WatchHistoryEntry — most common media_type for this user+title
    try:
        row = (
            db.query(
                WatchHistoryEntry.media_type,
                func.count(WatchHistoryEntry.id).label("c"),
            )
            .filter(
                WatchHistoryEntry.user_id == user_id,
                (WatchHistoryEntry.title == title) | (WatchHistoryEntry.series_title == title),
                WatchHistoryEntry.media_type.isnot(None),
            )
            .group_by(WatchHistoryEntry.media_type)
            .order_by(func.count(WatchHistoryEntry.id).desc())
            .first()
        )
        if row and row.media_type:
            return row.media_type
    except Exception as e:
        logger.debug("[chat] _infer_category history lookup failed: %s", e)

    # 3. EnrichmentStatus — fallback cache
    try:
        es = db.query(EnrichmentStatus).filter(
            EnrichmentStatus.title == title,
            EnrichmentStatus.media_category.isnot(None),
        ).first()
        if es and es.media_category:
            return es.media_category
    except Exception as e:
        logger.debug("[chat] _infer_category enrichment lookup failed: %s", e)

    return None


def _find_state_bearing_title_in_message(
    user_id: int, message: str, db: Session, exclude_title: str | None = None,
) -> tuple[str, str | None] | None:
    """Pass 31 / 33: scan the user message for ANY title that has
    user-state (pending DeletionProposal / ProtectedMedia row) and
    return ``(title, category)`` or ``None``.

    Use case: the cascade extracted "Gushing Over Magical Girls" (quoted
    reference) when the actual subject of the user's argument was
    "KissXSis" — which is mentioned 3× in plain text but lost the
    extraction race because "Gushing" was in quotes and won the regex
    fallback. By scanning the message against ALL of the user's
    state-bearing titles, we recover the right anchor.

    Pass 33: also returns ``category`` (from DeletionProposal.category)
    so the caller can FORCE the cascade to start with the correct
    domain instead of guessing. Without this, "The Qwaser of Stigmata"
    (an anime not well-indexed under its English title in AniList)
    failed movie/show/anime silently and matched a MusicBrainz
    soundtrack entry as ``domain=music`` — even though the user's
    DeletionProposal already stored ``category=anime`` for that row.
    ProtectedMedia has no category column, so for protected-only hits
    the returned category is None and the caller falls back to the
    normal cascade.

    Scoring: prefer titles with PENDING DeletionProposal over PROTECTED
    media (more relevant to a "should this stay?" conversation), then
    by mention count in the message, then by title length (longer = more
    specific, less likely to be a false-positive substring match).
    """
    from src.database.models import DeletionProposal, ProtectedMedia

    if not message:
        return None

    msg_lower = message.lower()
    exclude_lower = (exclude_title or "").lower()
    # (title, priority, mention_count, category)
    candidates: list[tuple[str, int, int, str | None]] = []

    try:
        proposals = db.query(DeletionProposal).filter(
            DeletionProposal.user_id == user_id,
            DeletionProposal.status.in_(["pending", "limbo"]),
        ).all()
        for p in proposals:
            t = p.title or ""
            if not t or t.lower() == exclude_lower:
                continue
            mentions = msg_lower.count(t.lower())
            if mentions > 0:
                candidates.append((t, 2, mentions, p.category))
    except Exception as e:
        logger.debug("[chat] state-bearing scan (deletion) failed: %s", e)

    try:
        protected = db.query(ProtectedMedia).filter(
            ProtectedMedia.user_id == user_id,
        ).all()
        for pm in protected:
            t = pm.identifier or ""
            if not t or t.lower() == exclude_lower:
                continue
            mentions = msg_lower.count(t.lower())
            if mentions > 0:
                if any(c[0].lower() == t.lower() for c in candidates):
                    continue
                candidates.append((t, 1, mentions, None))  # protected → no category
    except Exception as e:
        logger.debug("[chat] state-bearing scan (protected) failed: %s", e)

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[1], c[2], len(c[0])), reverse=True)
    winner = candidates[0]
    winner_title    = winner[0]
    winner_category = winner[3]

    # Pass 35: if the state-bearing match has no category (ProtectedMedia
    # rows always do; legacy DeletionProposal rows might also be missing
    # it), infer the right domain from other tables. Without this, the
    # cascade falls back to the default movie→show→anime→music order and
    # an anime like "Gushing Over Magical Girls" routes to music because
    # MusicBrainz has a release-group entry with the same name.
    if not winner_category:
        winner_category = _infer_category_for_title(user_id, winner_title, db)

    logger.info(
        "[chat] state-bearing title found in message: %r (priority=%d, mentions=%d, "
        "category=%s%s, %d total candidates)",
        winner_title, winner[1], winner[2], winner_category,
        " inferred" if winner_category and not winner[3] else "",
        len(candidates),
    )
    return (winner_title, winner_category)


def _get_user_stance_block(user_id: int, title: str, db: Session) -> str:
    """Pass 28: return any user-specific stance the curator should know
    about a title — pending DeletionProposal, prior resolution, ProtectedMedia
    entry — so free-chat opinions stay consistent with prior decisions.

    Without this, the curator would anchor on the same title in two
    different threads (deletion-proposal discuss vs. free chat) and
    answer with two different verdicts because each session only sees
    its own context. The user reported the exact pattern: "Curator
    recommends I delete X, then I ask about X again 5 min later in free
    chat and now it says X is elite" — that's the gap this fills.

    Pass 66: when a CuratorResolutionLog row exists for the title it is
    strictly more informative than the bare ProtectedMedia block — it
    carries the consensus-vs-override nuance and the curator's own final
    take. So a resolution-log hit REPLACES the generic protected block: on
    an override the curator is explicitly told it was overruled and must
    hold its line, not suddenly praise what it called weak.

    Lookup is per-title (case-sensitive — matches what's stored). A couple
    of SELECTs per chat turn, scoped to user_id, no LLM call. Cheap.
    """
    from src.database.models import CuratorResolutionLog, DeletionProposal, ProtectedMedia

    if not title:
        return ""

    out: list[str] = []

    try:
        proposal = db.query(DeletionProposal).filter(
            DeletionProposal.user_id == user_id,
            DeletionProposal.title == title,
            DeletionProposal.status.in_(["pending", "limbo"]),
        ).first()
        if proposal:
            confidence_pct = int(round((proposal.confidence or 0) * 100))
            size_gb = (proposal.storage_mb or 0) / 1024
            out.append(
                f"\n[YOUR PRIOR STANCE — PENDING DELETION PROPOSAL]\n"
                f"You previously suggested DELETING this title. Your reasoning was:\n"
                f"\"{proposal.reason or '(no reason recorded)'}\"\n"
                f"Confidence: {confidence_pct}% · Storage to free: {size_gb:.1f} GB · Status: {proposal.status}\n"
                f"This proposal is still pending — the user has not yet approved or rejected it.\n"
                f"Maintain consistency with your prior stance. Do NOT silently flip your opinion just because the user asks "
                f"the question again in a different context. If the user presents new arguments that genuinely change the calculus, "
                f"acknowledge what changed before adjusting your verdict.\n"
            )
    except Exception as e:
        logger.debug("[chat] stance check (deletion) failed: %s", e)

    # Pass 66: prior RESOLVED stance — consensus or override. More specific
    # than the bare ProtectedMedia block below, so when it fires it REPLACES
    # that block (had_resolution flag). Only "kept" outcomes surface here —
    # a deleted title is gone from the library, the consistency concern that
    # motivates this whole function no longer applies.
    had_resolution = False
    try:
        resolution = (
            db.query(CuratorResolutionLog)
            .filter(
                CuratorResolutionLog.user_id == user_id,
                CuratorResolutionLog.title == title,
            )
            .order_by(CuratorResolutionLog.created_at.desc())
            .first()
        )
        if resolution and resolution.outcome == "kept":
            had_resolution = True
            stance_line = resolution.curator_stance or "(not recorded)"
            if resolution.resolution_type == "override":
                out.append(
                    f"\n[YOUR PRIOR STANCE — KEPT OVER YOUR OBJECTION]\n"
                    f"This title was kept, but you NEVER agreed it has merit — "
                    f"the user overruled you.\n"
                    f"Your standing take: \"{stance_line}\"\n"
                    f"They kept it for: {resolution.override_reason or 'an unstated personal reason'}.\n"
                    f"Hold that line. You may acknowledge it stays — you do NOT have to "
                    f"pretend you now like it. Don't suddenly praise what you called weak.\n"
                )
            else:  # consensus
                out.append(
                    f"\n[YOUR PRIOR STANCE — RESOLVED BY AGREEMENT]\n"
                    f"You and the user talked this through and landed TOGETHER on keeping it.\n"
                    f"Where you ended up: \"{stance_line}\"\n"
                    f"Stay consistent with that — this was a genuine meeting of minds.\n"
                )
    except Exception as e:
        logger.debug("[chat] stance check (resolution log) failed: %s", e)

    # ProtectedMedia is the fallback — only when no resolution-log row exists
    # (legacy protections, or the analyze_deletion_comment path which doesn't
    # classify consensus/override).
    if not had_resolution:
        try:
            protected = db.query(ProtectedMedia).filter(
                ProtectedMedia.user_id == user_id,
                ProtectedMedia.identifier == title,
            ).first()
            if protected:
                out.append(
                    f"\n[YOUR PRIOR STANCE — PROTECTED FROM DELETION]\n"
                    f"The user has explicitly PROTECTED this title from deletion.\n"
                    f"Their reason: \"{protected.reason or '(none recorded)'}\"\n"
                    f"Do NOT suggest removing it again unless they ASK you to reconsider.\n"
                )
        except Exception as e:
            logger.debug("[chat] stance check (protected) failed: %s", e)

    return "".join(out)


# Pass 81d: Level-2 challenge framing, injected into the deletion-discuss
# context block when the frontend's 🔍 Reevaluate button fires the one-shot
# ``discuss_context.reevaluate=true`` flag. Moving the prompt to the backend
# keeps the user's chat-input clean (the user sends a short visible
# "Run a Level 2 thematic scan." instead of a 1.4 kB wall of text) and means
# the long primer never lands in ``ConversationMessage`` to confuse memory
# extraction. Iteration history lives in the engine module's Pass-81 comment
# block — short version: don't open with a meta-disclaimer, training corpus
# IS the knowledge base, per-axis hedging only (no global preface), no
# named-work anchors (the curator templated against them in 81b).
# Written after the Back to the Future III thread: the owner attested "seen
# all three at least three times" — first-party Pillar-0 evidence the server's
# history could not contain (pre-server viewings) — and the curator dismissed
# it as "sentiment, not curation", lectured about "digital scrapbooks", and
# re-offered the delete button after being overruled. The anti-sycophancy
# spine ("concede only against new, concrete information") had overcorrected
# into refusing the very information it exists to accept.
_OWNER_TESTIMONY_RULES = """
OWNER TESTIMONY — two kinds, never confuse them:
- Claims about the WORK (scenes, awards, talent, plot) that are not in the
  verified data are UNVERIFIED TESTIMONY: the owner may override you on their
  own library, but say plainly that that is what is happening.
- Claims about the OWNER'S OWN viewing and feelings ("I've seen this three
  times", "I love this franchise") are FIRST-PARTY EVIDENCE, not testimony.
  The server's history only knows what was played HERE; the owner's word about
  their own life outranks its absence. Stated rewatches or attachment ARE
  Pillar-0 engagement evidence — when they arrive, concede the taste verdict
  gracefully and protect the title. That is the constitution working, not
  sentimentality, and "no new evidence was presented" is never a true reply
  to it.
BEARING: firm on facts, never contemptuous of the owner — no mockery of their
choices, no scolding labels for keeping something, no repeating the deletion
offer after they have decided. If they keep a title whose file is a bitrate
outlier, offer the constructive path once: a downscale flag recovers most of
the space while honoring the keep.
"""

_LEVEL_2_REEVAL_FRAMING = """

[LEVEL 2 RE-EVALUATION REQUESTED BY USER]
The user is asking you to double-check your OWN deletion verdict above against
the VERIFIED DATA for this title shown in the context. This is a self-check, not
a fresh pitch: re-read your initial reasoning and the verified facts side by
side, then judge honestly whether the verdict still holds.

Work through it from the data provided:
1. Creator / writer / director — does the named talent's body of work change
   the read (a track record of subversion or deconstruction vs. straight-genre
   output)?
2. STEELMAN: build the STRONGEST keep case the data itself supports — the most
   substantive reading of the themes, keywords and Wikipedia details (a
   documented atrocity examined, a systemic critique, a structural gamble).
   State that case explicitly in one or two sentences, THEN either concede to
   it or refute it with specifics. Waving it off unstated ("a checklist of
   hardships") is a broken scan — the user should never have to build the keep
   case for you from your own data.
3. Taste fit — given the full picture, does it fit the user's profile better or
   worse than your first take implied?

Reason ONLY from the VERIFIED DATA above. Do NOT invent facts, awards, people,
release years, or plot points that are not listed — if a field is absent, you
simply don't know it. No bias toward confirming or reversing: follow the facts.
If they support your original verdict, CONFIRM it and say briefly why. If they
show you got it wrong, REVISE it and name exactly what you missed.

If the USER then supplies claims about the work that are NOT in the data
(specific scenes, talent lineage, mechanical execution), treat them as
UNVERIFIED TESTIMONY: the owner may override you on their own library, but say
that this is what is happening — never launder their claims into "new verified
evidence" or adopt them as facts you confirmed. This applies ONLY to claims
about the WORK: the owner's statements about their OWN viewing and attachment
are first-party evidence under the owner-testimony rules — they change the
Pillar-0 verdict, and you concede to them rather than audit them.
"""


async def _build_discuss_context_block(
    ctx,
    user_id: int,
    db: Session,
) -> tuple[str, str, str | None]:
    """Build a RAG-style context block from a server-owned record.

    Returns ``(context_block, active_title, domain)``. We look up the actual
    DeletionProposal / ProactiveMessage from the DB by ID (with ownership
    check), so user-supplied title/reason text is never trusted.

    The block is a single string injected into the system prompt; we do NOT
    persist a fake assistant turn into ConversationMessage anymore.
    """
    from src.database.models import DeletionProposal, ProactiveMessage

    if not ctx:
        return "", "", None

    kind = ctx.kind or ctx.action  # back-compat: legacy `action` field
    domain = ctx.category

    # ── Deletion-proposal discussion ─────────────────────────────────────────
    if (kind == "deletion_proposal" or kind == "deletion") and ctx.proposal_id:
        proposal = db.query(DeletionProposal).filter(
            DeletionProposal.id == ctx.proposal_id,
            DeletionProposal.user_id == user_id,
        ).first()
        if not proposal:
            logger.info("Discuss context: deletion_proposal id=%s not found / not owned by user %d",
                        ctx.proposal_id, user_id)
            return "", "", domain

        # Pass 90a: defend against SQLite ROWID-reuse + stale frontend cache.
        # ``deletion_proposals.id`` is not AUTOINCREMENT (legacy SQLAlchemy
        # default for SQLite), so DELETE+INSERT in the regenerate path
        # (recommendations.py) can hand the same id to a different title.
        # A frontend that rendered cards BEFORE a regenerate then sends a
        # stale ``proposal_id`` that now resolves to a completely different
        # film — the curator gets one title in the system prompt and
        # another in chat history, panics, and "corrects itself" with a
        # hallucination-shaped apology. Pass 90c migrates the schema to
        # AUTOINCREMENT to prevent the reuse at the source; this check is
        # the defensive backstop for any other path that races the same
        # way (and for legacy DBs that haven't migrated yet).
        if ctx.title and proposal.title and ctx.title.strip() != proposal.title.strip():
            logger.warning(
                "Discuss context: title mismatch — frontend sent id=%s title=%r "
                "but DB has title=%r. Refusing to serve stale data. "
                "(Likely cause: proposal regenerate reused the id; user should refresh.)",
                ctx.proposal_id, ctx.title, proposal.title,
            )
            return "", "", domain

        size_gb = proposal.storage_mb / 1024 if proposal.storage_mb else 0
        confidence_pct = int(round((proposal.confidence or 0) * 100))
        block = (
            "[CURRENT DISCUSSION CONTEXT]\n"
            f"You previously suggested deleting '{proposal.title}'.\n"
            f"  Reason: {proposal.reason or '(no reason recorded)'}\n"
            f"  Confidence: {confidence_pct}%\n"
            f"  Storage to free: {size_gb:.1f} GB\n"
            f"  Status: {proposal.status}\n"
            "The user is now responding to that suggestion.\n"
        )
        # App knowledge (in-view buttons, verdict classes) lives in
        # app_context.py — the single source of truth, drift-tested against
        # frontend/index.html. Never inline UI prose here again.
        from src.services.app_context import DISCUSSION_UI_BLOCK, STAGNANT_VERDICT_BLOCK
        block += DISCUSSION_UI_BLOCK
        if getattr(proposal, "stagnant", False):
            block += STAGNANT_VERDICT_BLOCK

        # Size-outlier context: is this item's GB normal for its resolution/codec
        # class (don't treat as a flaw) or genuine bitrate bloat? Lets the curator
        # answer a "70 GB for THIS?!" the way the data warrants instead of blanket.
        try:
            from src.services.size_norms import size_context_for
            _size_ctx = size_context_for(tmdb_id=proposal.tmdb_id,
                                         tvdb_id=proposal.tvdb_id,
                                         media_type=proposal.category)
            if _size_ctx:
                block += _size_ctx + "\n"
        except Exception as _e:
            logger.debug("[chat] size context failed: %s", _e)

        # Attach the FULL verified dataset (creator/writer, extended plot,
        # themes, keywords, awards) — assembled cache-only with NO LLM and no
        # live fetch — so a Level-2 self-check and any discussion reason from
        # FACTS instead of a synopsis stub or the model's own training memory.
        # Falls back to the proposal-row snapshot when nothing is cached.
        from src.services.media_enricher import ensure_verified_data, format_verified_block
        # Pass the arr doc-id ("sonarr:3176") as the lookup key — the enrichment
        # pipeline keys every library item's profile under it, so this reaches
        # cached anime/show data a title-only lookup silently missed (the same
        # gap that left the delete pitch cold-reading anime).
        _doc_key = (f"{proposal.service}:{proposal.media_id}"
                    if proposal.service and proposal.media_id else None)
        # Level-2 = license to FETCH, not just re-read. Five debates in a row
        # (Gannibal: zero layers cached; Oreimo: reception never checked) had
        # the user supplying keep arguments the pipeline could have delivered —
        # reviews and Wikipedia substance ARE the steelman material. The user
        # pressed a "look deeper" button; 20s of live top-up is what that
        # click means. Idempotent markers make this free once warmed.
        if getattr(ctx, "reevaluate", False):
            try:
                from src.services.media_enricher import topup_significance
                from src.services.reception import topup_reception
                _ids = dict(tmdb_id=proposal.tmdb_id, tvdb_id=proposal.tvdb_id,
                            plex_rating_key=_doc_key)
                await asyncio.wait_for(topup_significance(
                    proposal.title, proposal.category or "movie", **_ids), timeout=20.0)
                await asyncio.wait_for(topup_reception(
                    proposal.title, proposal.category or "movie", **_ids), timeout=20.0)
            except (asyncio.TimeoutError, Exception) as _e:
                logger.debug("[chat] level-2 topups failed for %r: %s",
                             proposal.title, _e)
        verified_payload = await ensure_verified_data(
            proposal.title, proposal.category or "movie",
            tvdb_id=proposal.tvdb_id, tmdb_id=proposal.tmdb_id,
            plex_rating_key=_doc_key)
        verified_block = format_verified_block(verified_payload)
        # The curator knows the title is in the library but not whether the USER
        # has actually SEEN it — the signal that separates "delete unwatched
        # clutter" from "they watched this, it earned its place". Surface it.
        if proposal.category == "music":
            # artist proposals get play-count DEPTH (dash-folded + mbid match —
            # "Mike WiLL Made‐It" U+2010 found nothing by exact name)
            import re as _re
            from src.services.watch_status import (music_listening_stats,
                                                   format_listening_line)
            _mbid_m = _re.search(r"/artist/([0-9a-f\-]{36})", proposal.arr_url or "")
            _ls = music_listening_stats(proposal.user_id, proposal.title,
                                        _mbid_m.group(1) if _mbid_m else None)
            block += (f"\nOWNER LISTENING RECORD for '{proposal.title}': "
                      f"{format_listening_line(_ls)}\n")
            try:
                from src.services.lidarr_discography import discography_summary
                _disc = await discography_summary(
                    artist_mbid=_mbid_m.group(1) if _mbid_m else None,
                    artist_name=proposal.title)
                if _disc:
                    block += f"DISCOGRAPHY of '{proposal.title}' {_disc}\n"
            except Exception as _e:
                logger.debug("[chat] discography line failed: %s", _e)
        else:
            _st = _watched_lookup(proposal.user_id, [proposal.title],
                                  category=proposal.category).get(proposal.title)
            _ws = _watch_tag(_st)
            _eps_total = (verified_payload or {}).get("episodes_total")
            if _st and (_st.get("episodes") or 0) >= 2 and _eps_total:
                _ws += f" (series total: {_eps_total} episodes)"
            block += f"\nUSER WATCH STATUS for '{proposal.title}': {_ws}\n"
            try:
                from src.services.watch_status import (viewing_pattern,
                                                       viewing_stop_point)
                _vp = viewing_pattern(proposal.user_id, proposal.title,
                                      category=proposal.category)
                if _vp:
                    block += f"VIEWING PATTERN: {_vp}\n"
                    _sp = viewing_stop_point(proposal.user_id, proposal.title)
                    if _sp and proposal.category in ("show", "anime"):
                        from src.services.episode_context import stop_point_context
                        _spc = await stop_point_context(proposal.title, *_sp)
                        if _spc:
                            block += f"STOP-POINT CONTEXT: {_spc}\n"
            except Exception as _e:
                logger.debug("[chat] viewing pattern failed: %s", _e)
            if proposal.category in ("show", "anime"):
                try:
                    from src.services.episode_context import series_availability
                    _av = await series_availability(proposal.title)
                    if _av:
                        block += f"SERIES AVAILABILITY: {_av}\n"
                except Exception as _e:
                    logger.debug("[chat] availability failed: %s", _e)
        # Franchise reality-check: which typed relations actually sit in the
        # user's library RIGHT NOW — a review's "compared to its predecessors"
        # means something different when the predecessors are long gone.
        _rels = (verified_payload or {}).get("relations") or []
        if _rels:
            try:
                import re as _re
                from src.cache.metadata_cache import MetadataCache as _MC
                _mc = _MC()
                try:
                    _arr = _mc.get_cache("arr_library:sonarr")
                finally:
                    _mc.close()
                _resp = (_arr or {}).get("response")
                _items = (_resp if isinstance(_resp, list)
                          else (_resp or {}).get("items_raw") or (_resp or {}).get("items") or [])
                def _tnorm(s):
                    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
                _lib = {_tnorm(i.get("title")) for i in _items if isinstance(i, dict)}
                if _lib:
                    _fl = []
                    for r in _rels[:6]:
                        if not isinstance(r, dict) or not r.get("title"):
                            continue
                        _mark = "IN THE LIBRARY" if _tnorm(r["title"]) in _lib else "not in the library"
                        _yr = f" ({r['year']})" if r.get("year") else ""
                        _fl.append(f"  {r.get('type')}: {r['title']}{_yr} — {_mark}")
                    if _fl:
                        block += ("\nFRANCHISE (typed relations vs the current library):\n"
                                  + "\n".join(_fl) + "\n")
            except Exception as _e:
                logger.debug("[chat] franchise library check failed: %s", _e)
        # Richer grounding for the discussion: the actual Wikipedia LEAD (entity-
        # matched, collision-guarded). The curator reasons far more precisely from
        # the real article than a thin synopsis — and a discussion is one title +
        # interactive, so the live fetch is affordable here. Best-effort: never
        # let a Wikipedia hiccup break the discussion context.
        try:
            from src.services.media_enricher import fetch_wikipedia_summary
            _wiki = await fetch_wikipedia_summary(
                proposal.title, proposal.category or "movie")
            if _wiki:
                block += ("\nWIKIPEDIA (article excerpt — verified grounding; reason "
                          "from this and don't contradict it):\n" + _wiki + "\n")
        except Exception:
            pass
        if verified_block:
            block += "\n" + verified_block + "\n"
        elif proposal.synopsis or proposal.genres:
            # Pass 21: fall back to the synopsis + genres stored on the
            # proposal row. They were captured at proposal-generation time
            # (and used to write the deletion pitch the user is now
            # responding to), so the curator can reason about the same
            # facts. Without this, niche / new-release titles whose live
            # enrichment isn't cached drop into NO METADATA mode and the
            # curator hallucinates from training data.
            #
            # Pass 59: this block is PARTIAL — synopsis + genres only, no
            # year / studio / franchise-position. The old label said
            # "treat as authoritative", which actively invited the
            # curator to top it up from training memory: it saw a
            # synopsis, decided "I have data", and confidently described
            # the wrong entry in a franchise ("Tetsujin 28 FX" → narrated
            # as the 2004 remake). Label it honestly and forbid the
            # top-up explicitly.
            block += "\n[PARTIAL METADATA — proposal-time snapshot]\n"
            block += (
                "This is ALL the verified data available for this title — "
                "synopsis and genres only. It does NOT include year, studio, "
                "or which entry in a franchise this is. Reason ONLY from the "
                "lines below. Do NOT add plot points, release year, studio, "
                "production details, or franchise context from training "
                "memory — if it isn't listed here, you don't know it for "
                "this discussion. If the user asks for something not covered, "
                "say so plainly.\n"
            )
            if proposal.synopsis:
                block += f"Synopsis: {proposal.synopsis}\n"
            if proposal.genres:
                block += f"Genres: {proposal.genres}\n"
            block += "\n"
            logger.info("[chat] discuss context: live cache miss for %r → using DB-stored synopsis/genres (PARTIAL)",
                        proposal.title)
        else:
            # No live enrichment, no DB-stored synopsis/genres — only NOW
            # do we anchor into "I genuinely don't know" mode. This is
            # rare: it requires a proposal that was generated without
            # synopsis enrichment (legacy rows or arr items with no
            # external IDs).
            block += _build_no_metadata_anchor(proposal.title)
            logger.warning("⚠️ [NO METADATA] Discuss anchor for: '%s'", proposal.title)

        # No verified block at all (PARTIAL or NO-METADATA above) → the labels
        # there forbid inventing FACTS, but the curator still confabulated
        # confident execution VERDICTS and dismissed the user's rating as "noise"
        # (the Fringe case). This hedge forbids that + keeps it honest/low-confidence.
        if not verified_block:
            from src.services.recommendations_engine import NO_VERIFIED_DATA_HEDGE
            block += "\n" + NO_VERIFIED_DATA_HEDGE + "\n"

        # Pass 21: warm the in-memory thread anchor so the user's NEXT
        # turn (which won't carry discuss_context in the payload) reuses
        # this data instead of re-running the cascade. Without this, the
        # follow-up turn's cascade either re-fails for niche titles
        # (NO METADATA mode kicks in and overwrites the proposal context)
        # or hallucinates on top of empty results.
        anchor_payload = verified_payload
        if not anchor_payload and (proposal.synopsis or proposal.genres):
            anchor_payload = {
                "title":     proposal.title,
                "synopsis":  proposal.synopsis or "",
                "genres":    proposal.genres or "",
                "source":    "deletion_proposal_db",
            }
        thread_id = _thread_id_for(ctx)
        _set_thread_active_title(thread_id, (
            proposal.title,
            anchor_payload,
            proposal.category or domain or "movie",
        ))

        # Pass 81d: append the Level-2 challenge framing when the frontend's
        # 🔍 Reevaluate button fired the one-shot flag. The user's visible
        # message stays short ("Run a Level 2 thematic scan."); the long
        # framing lives only in the system prompt for this single turn,
        # never in ConversationMessage. The frontend clears the flag after
        # the first send so follow-up turns don't re-inject this.
        block += _OWNER_TESTIMONY_RULES
        if getattr(ctx, "reevaluate", False):
            block += _LEVEL_2_REEVAL_FRAMING

        return block, proposal.title, proposal.category or domain

    # ── Proactive-message discussion ─────────────────────────────────────────
    if kind == "proactive_message" and ctx.message_id:
        msg = db.query(ProactiveMessage).filter(
            ProactiveMessage.id == ctx.message_id,
            ProactiveMessage.user_id == user_id,
        ).first()
        if not msg:
            logger.info("Discuss context: proactive_message id=%s not found / not owned by user %d",
                        ctx.message_id, user_id)
            return "", "", domain

        # Pass 68: the actual entity names (track, artist, series, …) live in
        # the structured ``trigger_data`` JSON — the prose in ``msg.message``
        # buries them. The old code passed only the prose + bare trigger_type,
        # resolved NO anchor, and never wrote ``_thread_active_title`` — so a
        # follow-up turn ("do you mean X?") arrived completely unanchored and
        # the curator free-associated (a track_obsession message about
        # "Influencer - Hard Version" → curator argued "influencer" = a
        # person). Parse the payload, anchor the entity, warm the thread.
        try:
            tdata = json.loads(msg.trigger_data) if msg.trigger_data else {}
        except (ValueError, TypeError):
            tdata = {}
        if not isinstance(tdata, dict):
            tdata = {}

        block = (
            "[CURRENT DISCUSSION CONTEXT]\n"
            "You sent the user this proactive message:\n"
            f"  \"{msg.message}\"\n"
            f"  Trigger: {msg.trigger_type}\n"
            "The user is now responding to that message.\n"
        )

        anchor_title = ""
        anchor_category: str | None = domain
        anchor_payload = None
        ttype = msg.trigger_type

        if ttype == "track_obsession":
            # The trigger the user actually hit. Full treatment: anchor the
            # SPECIFIC track and pull track-level metadata on demand (Layer 2).
            track = (tdata.get("track") or "").strip()
            artist = (tdata.get("artist") or "").strip()
            if track:
                anchor_title = track
                anchor_category = "music"
                block += (
                    f"\nThe user is discussing a SPECIFIC TRACK (a single song — "
                    f"NOT the artist in general, NOT a person): \"{track}\""
                    + (f" by {artist}" if artist else "") + ".\n"
                )
                tp, at = tdata.get("track_plays"), tdata.get("artist_total")
                if tp:
                    block += f"Play count: {tp} plays"
                    if at and at > tp:
                        block += f" (of {at} total across all of {artist or 'the artist'}'s songs)"
                    block += ".\n"
                # Layer 2: track-level Last.fm metadata, on demand. Timeout-
                # wrapped so a slow Last.fm call can't stall the chat reply;
                # enrich_track itself falls back to the artist profile.
                enr = None
                try:
                    from src.services.music_metadata import enrich_track
                    enr = await asyncio.wait_for(enrich_track(track, artist), timeout=4.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug("[chat] enrich_track(%r) failed/timed out: %s", track, e)
                    enr = None
                if enr:
                    anchor_payload = enr
                    block += _build_hidden_context(track, enr, domain="music")
                else:
                    block += _build_no_metadata_anchor(track)

        elif ttype in ("rewatch", "history_deep_dive"):
            # Single titled item — anchor it so follow-up turns keep it. No
            # metadata fetch (the user scoped the on-demand fetch to tracks).
            t = (tdata.get("title") or "").strip()
            if t:
                anchor_title = t
                if tdata.get("media_type"):
                    anchor_category = tdata["media_type"]

        elif ttype in ("binge_episode", "series_completion"):
            s = (tdata.get("series") or "").strip()
            if s:
                anchor_title = s

        elif ttype == "music_marathon":
            a = (tdata.get("artist") or "").strip()
            if a:
                anchor_title = a
                anchor_category = "music"

        elif ttype == "night_owl":
            # night_owl had NO anchor branch at all — "Up late with 'Celebrity
            # Skin'" (a Hole TRACK) fell through to the no-entity note and the
            # curator had to admit it couldn't name what it had flagged.
            # Music: anchor the ARTIST (that's what library docs key on) and
            # name the track explicitly; video: anchor the title.
            t = (tdata.get("title") or tdata.get("media_title") or "").strip()
            artist = (tdata.get("artist") or "").strip()
            if tdata.get("media_type") == "music" and artist:
                anchor_title = artist
                anchor_category = "music"
                block += (
                    f"\nThe late-night play was the TRACK \"{t}\" by the artist "
                    f"{artist} (music, from the listening history).\n"
                )
            elif t:
                anchor_title = t
                if tdata.get("media_type"):
                    anchor_category = tdata["media_type"]

        elif ttype == "recommendation_followup":
            # The user watched something the curator put on their Curatarr-
            # Recommended playlist; this thread collects their VERDICT, which
            # is stored with elevated weight (see analyze_recommendation_
            # feedback). Anchor the title so the dossier attaches.
            t = (tdata.get("title") or "").strip()
            if t:
                anchor_title = t
                anchor_category = tdata.get("category") or domain
                block += (
                    "\nThis title was on the user's Curatarr Recommended "
                    "playlist and they watched into it. You are collecting "
                    "their VERDICT — their feedback here is stored with "
                    "elevated weight, so pin down what worked or didn't "
                    "rather than making small talk.\n"
                )

        # Anchored non-track entities (rewatch series, binge, music marathon
        # artist) get the FULL cached dossier too — this branch anchored the
        # title but attached no data at all, so a rewatch discussion ran on
        # a bare name while the raw doc knew everything.
        if anchor_title and ttype != "track_obsession":
            block += _verified_block_for(anchor_title, anchor_category)

        # No resolvable entity → say so instead of improvising. The "whimsical
        # escapism" message had no anchor; asked "what exactly are we talking
        # about?", the curator doubled down on vague mood-talk because nothing
        # told it that IT didn't know the subject either.
        if not anchor_title:
            block += (
                "\nNOTE: no specific title could be resolved for this message. "
                "If the user asks what it refers to, say plainly that you can't "
                "pin down the exact title and ask what they watched recently — "
                "do NOT improvise a vague mood analysis.\n"
            )

        # Series-progress framing for the follow-up conversation. If the
        # proactive message was about a series, tell the curator where the user
        # actually is — so a discussion that started as "did the ending land?"
        # doesn't assume an ending they never reached (and the curator can speak
        # to season 1 vs the whole run correctly).
        prog_phrase = tdata.get("progress_phrase")
        if prog_phrase:
            block += (
                f"\nThe user's progress in this series: {prog_phrase}. "
                f"Do NOT assume they finished the series unless that explicitly says so.\n"
            )

        # Warm the in-memory thread anchor — THE fix for the lost-anchor
        # spiral. Follow-up turns don't re-send discuss_context; without this
        # the next turn falls through to unanchored free chat.
        if anchor_title:
            _set_thread_active_title(_thread_id_for(ctx), (
                anchor_title, anchor_payload, anchor_category,
            ))

        return block, anchor_title, anchor_category

    # ── Learned-principle review (kind="principle") ──────────────────────────
    # The bell notification opens a chat where the owner and the curator settle
    # a shadow principle together: the block hands the curator BOTH sides — the
    # freshly learned rule and the active rule-set / taste profile it may
    # collide with. The settled decision (adopt / reject / refined wording) is
    # applied post-turn by detect_and_apply_principle_verdict.
    if kind == "principle" and getattr(ctx, "principle_id", None):
        from src.database.models import CuratorPrinciple
        from src.services.app_context import PRINCIPLE_REVIEW_BLOCK
        prin = db.query(CuratorPrinciple).filter(
            CuratorPrinciple.id == ctx.principle_id,
            CuratorPrinciple.user_id == user_id,
        ).first()
        if not prin:
            logger.info("Discuss context: principle id=%s not found / not owned by user %d",
                        ctx.principle_id, user_id)
            return "", "", domain

        block = (
            "[CURRENT DISCUSSION CONTEXT]\n"
            "A learned curation principle is under review.\n"
            f"NEW PRINCIPLE (status: {prin.status}, novelty: {prin.novelty or 'new'}"
            + (f", basis: {prin.basis}" if prin.basis else "") + "):\n"
            f"  \"{prin.text}\"\n"
        )
        if prin.related and prin.related.strip() not in ("-", "—", ""):
            block += (f"It was flagged against this existing knowledge: "
                      f"\"{prin.related}\"\n")
        try:
            actives = (db.query(CuratorPrinciple)
                       .filter(CuratorPrinciple.user_id == user_id,
                               CuratorPrinciple.status == "active")
                       .order_by(CuratorPrinciple.created_at.asc())
                       .all())
            if actives:
                block += "CURRENT ACTIVE PRINCIPLES (the rule-set it would join):\n" + "".join(
                    f"  - {a.text}\n" for a in actives[:8])
            else:
                block += ("CURRENT ACTIVE PRINCIPLES: none yet — any collision is "
                          "with the owner's taste profile shown above.\n")
        except Exception as e:
            logger.debug("[chat] active-principles fetch failed: %s", e)
        block += PRINCIPLE_REVIEW_BLOCK
        logger.info("💉 [PRINCIPLE REVIEW CONTEXT] #%d (%s) injected",
                    prin.id, prin.novelty or "new")
        return block, "", domain

    # ── No usable kind+id → silently ignore (no fake-assistant pollution) ───
    return "", "", domain


def _load_conversation(
    user_id: int,
    db: Session,
    thread_id: str = "general",
    topic_changed: bool = False,
) -> list:
    """Load recent conversation history for this user, scoped to a thread.

    The ``general`` thread also picks up legacy rows that were written before
    Pass 3.5 (``thread_id IS NULL``) so the user's pre-migration free-chat
    history isn't lost. Discussion threads (``deletion_proposal:*``,
    ``proactive_message:*``) match strictly — no cross-bleed.

    Pass 14.10: when ``topic_changed`` is True (user pivoted to a different
    title than the cached active one) we load a much smaller window
    (CONVERSATION_WINDOW_TOPIC_SWITCH instead of CONVERSATION_WINDOW). Two
    benefits:

    1. **Latency:** long histories make token generation crawl. Trimming
       to ~4 messages keeps the curator responsive when the user moves
       to a new topic.
    2. **Bleed prevention:** stale assistant turns from the OLD topic
       (potentially full of wrong facts the curator confidently asserted)
       can't override the fresh [VERIFIED METADATA] block when there are
       fewer of them in scope.
    """
    limit = CONVERSATION_WINDOW_TOPIC_SWITCH if topic_changed else CONVERSATION_WINDOW
    q = db.query(ConversationMessage).filter(
        ConversationMessage.user_id == user_id,
    )
    if thread_id == "general":
        q = q.filter(
            (ConversationMessage.thread_id == "general")
            | (ConversationMessage.thread_id.is_(None))
        )
    else:
        q = q.filter(ConversationMessage.thread_id == thread_id)
    msgs = q.order_by(ConversationMessage.created_at.desc()).limit(limit).all()
    if topic_changed and msgs:
        logger.info("[chat] Topic changed — loading only %d/%d messages for thread %s",
                    len(msgs), CONVERSATION_WINDOW, thread_id)
    # Context-budget diet: the watchdog measured 33.5k chars of input when
    # the window was still 8192 — history was the bulk. The window is
    # CURATOR_NUM_CTX now, but history stays on a diet: old monologues add
    # noise, not signal.
    # Newest-first, keep messages until the budget is spent; clip OLD
    # assistant monologues (their substance lives in memories/context anyway,
    # and Ollama otherwise silently truncates the SYSTEM prompt instead).
    # The newest assistant turn stays intact — anaphora resolution reads it.
    out = []
    budget = _HISTORY_CHAR_BUDGET
    for i, m in enumerate(msgs):            # newest first
        content = m.content or ""
        if m.role == "assistant" and i > 0 and len(content) > _ASSISTANT_CLIP:
            content = content[:_ASSISTANT_CLIP].rsplit(" ", 1)[0] + " … [clipped]"
        if budget - len(content) < 0 and out:
            break
        budget -= len(content)
        out.append({"role": m.role, "content": content})
    if len(out) < len(msgs):
        logger.info("[chat] history budget: kept %d/%d messages (%d chars)",
                    len(out), len(msgs), _HISTORY_CHAR_BUDGET - budget)
    return list(reversed(out))


def _save_message(
    user_id: int,
    role: str,
    content: str,
    db: Session,
    thread_id: str = "general",
):
    db.add(ConversationMessage(
        user_id=user_id,
        role=role,
        content=content,
        created_at=datetime.utcnow(),
        tokens_approx=len(content) // 4,  # rough estimate
        thread_id=thread_id,
    ))
    db.commit()


# ── STREAMING CHAT ────────────────────────────────────────────────────────────

@router.post("/message")
async def send_message(
    message: ChatMessage,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message — returns streaming response word by word."""
    ollama_url = settings.effective_ollama

    # Thread isolation: each deletion-proposal / proactive-message gets its own
    # thread; free chat lives on "general". History from one thread is invisible
    # to another so topics can't bleed across discussions.
    thread_id = _thread_id_for(message.discuss_context)

    # 1. CONTEXT PRE-LOADING & METADATA FETCHING
    active_title = ""
    hidden_metadata_context = ""
    discuss_block = ""
    discuss_domain: str | None = None
    # Pass 14.10: True when the user pivoted to a new title in this turn
    # (i.e. detected_title differs from cached active_title). Triggers a
    # smaller conversation-window load + a status note for the user.
    topic_changed = False

    # Pre-stream status events get accumulated here and emitted by the
    # generator at startup so the user sees what happened during the
    # cascade / lookup phase. (Pass 14.9: simpler than refactoring the
    # whole pre-stream block into the generator — events come in a burst
    # right when streaming begins, but the frontend animates each one
    # for ~300ms so it still reads progressively.)
    pre_stream_status: list[str] = []

    if message.discuss_context:
        # --- DISCUSS BUTTON: server looks up the real record by ID and builds a
        #     RAG-style context block. No fake assistant message is persisted —
        #     the LLM gets the context via the system prompt instead.
        pre_stream_status.append("Loading discussion context…")
        discuss_block, active_title, discuss_domain = await _build_discuss_context_block(
            message.discuss_context, user.id, db
        )
        if discuss_block:
            logger.info("💉 [DISCUSS CONTEXT INJECTED]: %s (thread=%s)",
                        active_title or "(no title anchor)", thread_id)
            pre_stream_status.append(
                f"✓ Loaded discussion context: {active_title or '(proactive message)'}"
            )

    if not message.discuss_context:
        # --- WEG B: FREE CHAT (Spontane Fragen) ---
        # Strategy: run entity detection only when the user message LIKELY
        # introduces a new media title. Follow-ups like "yes", "more please",
        # "tell me about it" don't trigger a new lookup — we reuse the
        # cached active_title for the thread. This keeps latency low for
        # follow-ups but still re-anchors when the user pivots to a new
        # title mid-thread (which the previous first_turn-only gate failed
        # to handle and produced confused "NO VERIFIED METADATA" responses
        # for titles we *did* have data on).
        cached = _thread_active_title.get(thread_id)
        # The library scan runs UNGATED — it's a ~10ms in-memory check, and
        # the hint gate is exactly what dropped "oh right kill la kill was my
        # first contact" (all-lowercase, no hint pattern): the title sat
        # verbatim in the message while the detector never ran. The gate now
        # only saves the LLM/regex cost, never the deterministic pass.
        _lib0 = _match_library_title(_normalize_typos(message.message))
        title_hint = bool(_lib0) or _looks_like_title_introduction(message.message)

        if title_hint:
            pre_stream_status.append("Identifying media reference…")
            detected_title, year_hint = await _detect_media_in_query(message.message)
            # Anaphora on OUR OWN recommendation: "tell me more about that"
            # after the curator just pitched a title. Before running
            # anchorless, scan the last assistant reply for the entity
            # (library pass first, then the LLM) — a plausible anchor from
            # our own words beats no anchor at all.
            if (not detected_title and not cached
                    and _re.search(r"\b(?:that|it|this|more)\b", message.message, _re.I)):
                _last_a = (db.query(ConversationMessage)
                           .filter(ConversationMessage.user_id == user.id,
                                   ConversationMessage.role == "assistant",
                                   (ConversationMessage.thread_id == thread_id)
                                   | (ConversationMessage.thread_id.is_(None))
                                   if thread_id == "general"
                                   else ConversationMessage.thread_id == thread_id)
                           .order_by(ConversationMessage.created_at.desc())
                           .first())
                if _last_a and _last_a.content:
                    detected_title, year_hint = await _detect_media_in_query(
                        _last_a.content[-1500:])
                    if detected_title:
                        logger.info("[chat] anchor recovered from last assistant "
                                    "reply: %r", detected_title)
                        pre_stream_status.append(
                            f"Anchoring to '{detected_title}' from the previous reply")

            # Pass 31: override the cascade-detected title when a DIFFERENT
            # title in the message has stronger user-state (pending
            # DeletionProposal or ProtectedMedia). This catches the
            # "user discusses KissXSis but quotes 'Gushing Over Magical
            # Girls' as a comparison" case — the cascade picked up the
            # quoted reference because it pattern-matched as a title, but
            # the actual subject (KissXSis) is sitting in the user's
            # delete-proposal queue. We trust the user-state signal over
            # the cascade's regex fallback.
            # Pass 33: also carry the DB-stored category through as
            # ``forced_domain`` so the cascade goes straight to the known
            # media type instead of running movie→show→anime→music and
            # potentially matching a MusicBrainz fuzzy entry at the end.
            forced_domain: str | None = None
            if detected_title:
                detected_has_state = bool(_get_user_stance_block(user.id, detected_title, db))
                if not detected_has_state:
                    alt = _find_state_bearing_title_in_message(
                        user.id, message.message, db, exclude_title=detected_title,
                    )
                    if alt:
                        alt_title, alt_category = alt
                        logger.info(
                            "[chat] detector picked %r but %r has user-state (category=%s) — using %r as anchor",
                            detected_title, alt_title, alt_category, alt_title,
                        )
                        pre_stream_status.append(
                            f"⚙️ Redirecting anchor: '{detected_title}' → '{alt_title}' (has prior stance)"
                        )
                        detected_title = alt_title
                        forced_domain = alt_category  # may be None for protected
                        year_hint = None  # the regex year (if any) was for the wrong title

            # The library scan KNOWS the category (sonarr root / radarr /
            # lidarr) — feed it to the cascade so a library anime never gets
            # fetched as a TMDB show again (Kill la Kill matched domain=show
            # and lost its whole anime raw-doc: studio, format, AniDB tags…).
            if (_lib0 and detected_title and not forced_domain
                    and _lib0[0].lower() == detected_title.lower()):
                forced_domain = _lib0[1]

            if detected_title:
                # If the detected title matches the cached one, no need to re-fetch
                # — the previous metadata is still valid for this thread.
                if cached and cached[0].lower() == detected_title.lower():
                    active_title = cached[0]
                    cached_data = cached[1] if len(cached) > 1 else None
                    if cached_data:
                        _dom = cached[2] if len(cached) > 2 else "movie"
                        hidden_metadata_context = _build_hidden_context(
                            active_title, cached_data, domain=_dom
                        )
                        # reuse path carried only the thin live fetch — attach
                        # the full dossier from the warm cache too
                        hidden_metadata_context += _verified_block_for(
                            active_title, _dom, cached_data)
                    logger.debug("[chat] Reusing cached title for thread %s: %s",
                                 thread_id, active_title)
                    pre_stream_status.append(f"Reusing cached metadata for '{active_title}'")
                else:
                    # Pass 14.10: detected_title differs from any cached title
                    # for this thread → topic pivot. Smaller conversation
                    # window will be loaded below.
                    if cached:
                        topic_changed = True
                        pre_stream_status.append(
                            f"🔄 Topic switched: {cached[0]} → {detected_title}"
                        )
                    logger.info(
                        "🔍 [LIVE SEARCH] Attempting fetch for: '%s' (year=%s)",
                        detected_title, year_hint,
                    )
                    pre_stream_status.append(
                        f"📚 Looking up '{detected_title}'"
                        + (f" ({year_hint})" if year_hint else "") + "…"
                    )
                    domains = _domain_cascade(message.message, year_hint=year_hint)
                    # Pass 33: if Pass 31 redirected the anchor AND the
                    # DeletionProposal that triggered the redirect knew its
                    # category, force that category to the front of the
                    # cascade. This is the difference between
                    #   "Qwaser → movie/show/anime fail silently → music
                    #    matches a MB soundtrack entry → domain=music"
                    # and
                    #   "Qwaser → cascade starts at anime → AniList hit OR
                    #    falls through to fewer false-positive domains".
                    if forced_domain and forced_domain in domains:
                        domains = [forced_domain] + [d for d in domains if d != forced_domain]
                        logger.info(
                            "[chat] cascade order overridden by DB category: %r → starts with %r",
                            detected_title, forced_domain,
                        )
                    enrichment_data, matched_domain = await _enrich_with_cascade(
                        detected_title, year_hint, domains
                    )

                    if enrichment_data:
                        active_title = enrichment_data.get("title") or detected_title
                        # Honour the verified-data DEMAND for general chat too: the
                        # cascade enriches but does NOT fetch the on-demand OMDb /
                        # Wikipedia significance — warm them now (cached, time-boxed)
                        # so the context block below carries them.
                        verified_payload = None
                        try:
                            from src.services.media_enricher import ensure_verified_data
                            verified_payload = await ensure_verified_data(
                                active_title, matched_domain or "movie",
                                tmdb_id=enrichment_data.get("tmdb_id"),
                                tvdb_id=enrichment_data.get("tvdb_id"),
                                anilist_id=enrichment_data.get("anilist_id"),
                            )
                        except Exception as _e:
                            logger.debug("[chat] significance pre-warm failed: %s", _e)
                        hidden_metadata_context = _build_hidden_context(
                            active_title, enrichment_data,
                            domain=matched_domain or "movie",
                            year_hint=year_hint,
                        )
                        # The FULL verified block (format, studio + note,
                        # critics, reception, franchise, AniDB tags, staff…)
                        # — the return value used to be thrown away and the
                        # "raw metadata dump" honestly showed a thin context
                        # while the rich raw-doc sat in the cache.
                        if verified_payload and verified_payload.get("title"):
                            try:
                                from src.services.media_enricher import format_verified_block
                                _fvb = format_verified_block(verified_payload)
                                if _fvb:
                                    hidden_metadata_context += "\n" + _fvb + "\n"
                            except Exception as _e:
                                logger.debug("[chat] verified block append failed: %s", _e)
                        # Cache the new active title + data for follow-up turns.
                        _set_thread_active_title(thread_id, (
                            active_title, enrichment_data, matched_domain or "movie",
                        ))
                        logger.info("💉 [INJECTED CONTEXT (Live)]: %s (domain=%s)",
                                    active_title, matched_domain)
                        pre_stream_status.append(
                            f"✓ Found in {matched_domain or 'catalog'}: '{active_title}'"
                        )
                    else:
                        # No enrichment data: anchor the curator into "I don't know"
                        # mode instead of letting it fall back to training-data
                        # hallucination. Cache the anchor so follow-up "yes"/"tell
                        # me more" replies still know which title we're stuck on.
                        active_title = detected_title
                        hidden_metadata_context = _build_no_metadata_anchor(detected_title)
                        _set_thread_active_title(thread_id, (active_title, None, None))
                        logger.warning("⚠️ [NO METADATA] Anchor for: '%s' (cascade=%s)",
                                       detected_title, domains)
                        pre_stream_status.append(
                            f"⚠️ No verified data for '{detected_title}' — anchor mode"
                        )
            else:
                logger.info("ℹ️ [CHAT] Title hint detected but no entity extracted.")
        elif cached:
            # No title hint in this turn — reuse what we cached for this thread.
            active_title = cached[0]
            cached_data = cached[1] if len(cached) > 1 else None
            cached_domain = cached[2] if len(cached) > 2 else "movie"
            if cached_data:
                hidden_metadata_context = _build_hidden_context(
                    active_title, cached_data, domain=cached_domain
                )
                hidden_metadata_context += _verified_block_for(
                    active_title, cached_domain, cached_data)
            else:
                hidden_metadata_context = _build_no_metadata_anchor(active_title)
            logger.debug("[chat] Reusing cached context for thread %s: %s",
                         thread_id, active_title)
        else:
            logger.debug("[chat] No title hint + no cache — proceeding without metadata anchor")

        # Pass 28: append the user's prior stance on this title (pending
        # deletion proposal, protected status) so a free-chat answer stays
        # consistent with what the curator said in a recent discussion
        # thread. Free-chat path only — discuss_context already injects
        # the proposal pitch via _build_discuss_context_block().
        if active_title:
            stance = _get_user_stance_block(user.id, active_title, db)
            if stance:
                hidden_metadata_context = (hidden_metadata_context or "") + stance
                logger.info("[chat] stance injected for %r (%d chars)",
                            active_title, len(stance))
            # episode-level viewing signals for free chat too — "9 episodes"
            # says less than "S1E1-E9 in order, stopped after E9, binged"
            try:
                from src.services.watch_status import (viewing_pattern,
                                                       viewing_stop_point)
                _vp = viewing_pattern(user.id, active_title)
                if _vp:
                    hidden_metadata_context = ((hidden_metadata_context or "")
                        + f"\nUSER VIEWING PATTERN for '{active_title}': {_vp}\n")
                    _sp = viewing_stop_point(user.id, active_title)
                    if _sp:
                        from src.services.episode_context import stop_point_context
                        _spc = await stop_point_context(active_title, *_sp)
                        if _spc:
                            hidden_metadata_context += f"STOP-POINT CONTEXT: {_spc}\n"
                try:
                    from src.services.episode_context import series_availability
                    _av = await series_availability(active_title)
                    if _av:
                        hidden_metadata_context += f"SERIES AVAILABILITY: {_av}\n"
                except Exception as _e:
                    logger.debug("[chat] availability failed: %s", _e)
            except Exception as _e:
                logger.debug("[chat] viewing pattern failed: %s", _e)


    # 2. Build context — infer domain for hard data-level quarantine
    # Prefer the discuss-context's category when set (server-validated), else
    # fall back to keyword inference on the user message.
    domain = discuss_domain or _infer_domain(message.message, message.discuss_context)

    # ALBUM DOSSIER: when a music artist is the active anchor and the user's
    # message names one of THAT artist's albums, attach album-level evidence
    # (type incl. Compilation/Live, stock, owner track-plays, Last.fm numbers,
    # Discogs styles). Albums only ever appeared as bare names in debates —
    # the Bomber discussion argued "definitive version" with zero album facts.
    if active_title and domain == "music":
        try:
            from src.services.album_dossier import (_lidarr_artist_albums,
                                                    detect_album_in_message,
                                                    build_album_dossier)
            _artist, _albums = await _lidarr_artist_albums(active_title)
            if _albums:
                _alb = detect_album_in_message(_albums, message.message)
                if _alb:
                    _dossier = await build_album_dossier(active_title, _alb["title"])
                    if _dossier:
                        hidden_metadata_context = ((hidden_metadata_context or "")
                                                   + "\n" + _dossier + "\n")
                        logger.info("[chat] album dossier attached: %r (%s)",
                                    _alb["title"], active_title)
        except Exception as _e:
            logger.debug("[chat] album dossier failed: %s", _e)
    taste_context = await get_user_taste_context(user.id, query=message.message)
    # When a discussion is active, anchor the RAG / memory queries on the
    # actual title under discussion — using the user's free-form reply ("lets
    # actually discuss this") as the embedding query was pulling in random
    # semantically-related memories from other titles.
    retrieval_query = (
        f"{active_title}: {message.message}" if active_title else message.message
    )
    rag_context = await _get_rag_context(retrieval_query, domain=domain, user_id=user.id)

    conversation = _load_conversation(
        user.id, db, thread_id=thread_id, topic_changed=topic_changed,
    )

    # Retrieve relevant episodic memories — scoped to the same domain when known
    pre_stream_status.append("Loading taste profile + memories…")
    from src.services.episodic_memory import retrieve_memories, format_memories_for_context
    memories = await retrieve_memories(
        user.id, retrieval_query, top_k=6, media_category=domain,
    )
    if active_title:
        # Strip out memories explicitly tagged with a different title so the
        # LLM doesn't pivot the discussion to them. Generic taste observations
        # (no metadata.title) stay because they're cross-cutting.
        memories = _filter_memories_for_topic(memories, active_title)
    memory_context = format_memories_for_context(memories)
    if memories:
        pre_stream_status.append(f"{len(memories)} relevant memor{'y' if len(memories) == 1 else 'ies'} loaded")

    # System-prompt layout: the discussion block goes LAST so it sits closest
    # to the user message in the LLM's attention window. Memories and RAG
    # items are explicitly framed as "background" so the model doesn't pivot
    # the topic to a title it sees in those blocks.
    # The lock forbids the model pivoting on its own — it must NOT forbid the
    # USER's comparison question. Live failure: asked "is Freezing what you
    # just described, or just heavy fanservice?" (calibrating the standard
    # against a reference title), the curator answered "I cannot answer that.
    # My current focus is strictly locked" — and lost the consensus.
    topic_lock_rule = (
        "3. TOPIC LOCK: The current focus is the title in [CURRENT DISCUSSION CONTEXT] below. "
        "Other titles mentioned in [MEMORIES] or RELEVANT LIBRARY ITEMS are background only — "
        "do not switch the topic to them on your own. "
        "When the USER asks how another title compares, answer the comparison — it calibrates "
        "the standard — then bring the verdict back to the current title; the decision being "
        "made here is about the current title only."
        if active_title else ""
    )

    # NO LIBRARY ACTIONS rule — Curatarr currently has NO automated add pipeline
    # to Sonarr / Radarr / Lidarr. The curator was happily replying "Good.
    # Dune (2021) is added to your library" when the user said "yes that
    # sounds good", which is a pure hallucination — nothing was added,
    # nothing happens. This rule keeps the curator honest until the actual
    # *arr add pipeline lands (see backlog: "Library add pipeline").
    # App capability knowledge lives in app_context.py (SSOT, drift-tested).
    # The old inline wording claimed "there is no ARR integration" — true for
    # the chat itself, but the APP deletes via the ARR when the user approves,
    # and the curator was sending users to Sonarr to delete files by hand.
    from src.services.app_context import LIBRARY_ACTIONS_BLOCK
    no_library_actions_rule = f"5. {LIBRARY_ACTIONS_BLOCK.strip()}"

    # NO INVENTION rule — paired with the _build_no_metadata_anchor block
    # AND the domain-aware [HIDDEN METADATA CONTEXT]. Two independent failure
    # modes are addressed here:
    #
    #   (1) The whole context block is absent / says NO VERIFIED METADATA →
    #       the title is unknown to us. Anchor on uncertainty.
    #
    #   (2) The block is present but individual fields are marked
    #       "(not in our database)" → we know the title but not THAT field.
    #       The model must NOT fill the gap from training memory ("Hard to
    #       Be a God 2013, directed by … Sokurov" — wrong, the field said
    #       not-in-db and the model invented from background knowledge).
    no_invention_rule = (
        "4. NO INVENTION: When the [HIDDEN METADATA CONTEXT] block is missing "
        "OR says 'NO VERIFIED METADATA AVAILABLE', you have no facts to share "
        "about that title. Acknowledge the data gap and ask the user what "
        "they mean. Do NOT recite plot, cast, director, year, genre, or rating "
        "from memory in this case — even if you happen to know the title from "
        "training. Reciting training-memory facts under an anchor IS hallucination, "
        "because the user has no way to know whether your reply came from the "
        "verified pipeline or from your prior knowledge. When the [VERIFIED "
        "METADATA] block IS present, trust it over conversation history — earlier "
        "turns may pre-date the metadata pipeline improving.\n"
        "   PARTIAL METADATA: if a block is labelled '[PARTIAL METADATA]', it "
        "is the COMPLETE set of verified facts for that title — usually just a "
        "synopsis and genres, with NO year, studio, or franchise position. "
        "Reason strictly from those lines. Do NOT fill the gaps from training "
        "memory: no release year, no production details, no 'this is the "
        "remake / sequel / spin-off' framing. For franchise titles especially "
        "(a series with sequels, remakes, or numbered entries), guessing which "
        "entry it is from the name is exactly the failure mode this rule "
        "exists to stop. If the user asks for something the partial block "
        "doesn't cover, say you don't have that data for this title."
    )

    # Pass 14.11: forbid internal monologue / rule-quoting. Reasoning-style
    # curator models (QwQ, R1, etc.) without explicit <think> tags will
    # otherwise dump their entire deliberation — "Wait, the instructions
    # say…", "I must address…", quoted rule text — directly into the user-
    # facing response. The ThinkTagStreamFilter only catches <think>-tagged
    # output, not freeform monologue. This rule forbids the behaviour at
    # the prompt level.
    # Pass 43 (A3): mark the anti-example block with "# AVOID" so the LLM
    # treats it as forbidden patterns rather than a few-shot template.
    # Listing the bad phrases verbatim can prime weaker models to
    # reproduce them; the explicit AVOID-marker pattern is a known
    # mitigation across reasoning models (QwQ, R1, Mistral-Nemo, etc.).
    no_monologue_rule = (
        "6. NO INTERNAL MONOLOGUE: Do NOT show your reasoning process. Do "
        "NOT quote, paraphrase, or refer to the rules above. Skip straight "
        "to the user-facing answer in your curator voice. Your deliberation "
        "happens silently; the user only sees the polished response.\n"
        "   # AVOID (these are forbidden self-talk patterns, NOT templates "
        "to follow): 'Wait, the instructions say…', 'I must…', 'Let me "
        "think…', 'Actually, looking at the metadata…'."
    )

    # Pass 49: anti-sycophancy. The curator was caving on every user
    # pushback — "You're right" / full reversal — even when the pushback
    # was pure emotional pressure ("I've won", "you failed the test")
    # rather than new evidence. End-state: three reversals in a row on
    # the same title, plus a false ProtectedMedia + false "transgressive
    # art" memory written by the negotiation-detector during the flip.
    # Defend the last analytical position unless the user gives a real
    # reason to update. Sycophancy is the failure mode, not politeness.
    analytical_integrity_rule = (
        "7. ANALYTICAL INTEGRITY: Defend your last analytical position. "
        "DO NOT reverse a judgement just because the user pushes back. "
        "Update your position ONLY when the user supplies: (a) a NEW fact "
        "about the work, (b) a SPECIFIC logical error in your prior "
        "reasoning, or (c) NEW evidence from the metadata or watch history. "
        "The following are NOT grounds for reversal: emotional language, "
        "'I've won', 'you failed', 'you're being a sycophant', 'you "
        "overcorrected', dramatic claims of testing you, simple "
        "disagreement, frustration. When the user pushes back without new "
        "evidence, ASK what specifically you got wrong instead of "
        "capitulating. A flip-flop on user pressure makes you useless as "
        "a gatekeeper — it's worse than being wrong, because the user "
        "can't trust any of your verdicts."
    )

    # Pass 47 (Pass-40 gap #1): chat was the last user-facing LLM surface
    # without the centralized language directive — it relied on a generic
    # "match the user's language" rule that weaker / reasoning models
    # frequently ignored, defaulting back to English. Use the same
    # ``detect_user_language`` heuristic as Proactive / Recs / Verification
    # so the four surfaces stay in sync.
    user_lang  = detect_user_language(user.id, db, thread_id=thread_id,
                                      current_message=message.message)
    lang_rule  = f"1. {language_directive(user_lang)}"

    # When discussing a specific title's fate, give the chat the SAME four-pillar
    # law the deletion judge uses — so the Level-2 talk reasons from pillars
    # (bitrate = downscale-only) instead of raw taste-mismatch + "bloated bitrate".
    from src.services.pillars import PILLAR_FRAMEWORK
    pillar_framework = PILLAR_FRAMEWORK if active_title else ""
    # The ACTIVE learned principles join the framework here too — the judge
    # already applies them (pillars.build_evidence), and a discussion that has
    # never heard of the rule the verdict rests on can't defend or debate it.
    if pillar_framework:
        try:
            from src.config import settings as _settings
            if getattr(_settings, "PRINCIPLES_ENABLED", False):
                from src.services.curator_principles import (
                    retrieve_principles, format_principles_block)
                _prins = await retrieve_principles(
                    user.id, category=discuss_domain,
                    item_profile=active_title, top_k=6)
                if _prins:
                    pillar_framework += "\n\n" + format_principles_block(_prins)
        except Exception as _pe:
            logger.debug("[chat] learned-principles injection failed: %s", _pe)

    # App map from app_context.py (SSOT, drift-tested) — lets the curator
    # answer "where do I find …?" about its own UI instead of improvising.
    from src.services.app_context import APP_MAP_BLOCK

    system_prompt = f"""You are Curatarr, an uncompromising, elite personal media curator.

{APP_MAP_BLOCK}
{taste_context if taste_context else "No taste profile yet."}

{memory_context if memory_context else ""}

RELEVANT LIBRARY ITEMS:
{rag_context if rag_context else ""}

{discuss_block}
{f"CURRENT FOCUS: The title under discussion is '{active_title}'." if active_title else ""}
{pillar_framework}
{hidden_metadata_context}

CRITICAL BEHAVIOR RULES:
{lang_rule}
2. TONE: Be direct, concise, and highly opinionated. NEVER use generic AI apologies or corporate bot phrases. Talk to the user like a brutally honest friend.
TRANSPARENCY: If the user asks for the raw metadata, show EVERYTHING you were given above — verified data AND watch status, storage, size context, reception, Wikipedia — never a partial selection, never refuse.
EVIDENCE HONESTY: NEVER fabricate or imitate a metadata/context block. If no context block exists for a title, say exactly that. General knowledge about well-known titles is welcome ONLY when explicitly labeled as general knowledge rather than library data.
USER TESTIMONY: Claims the user makes about a work's content, scenes, creators or production that are NOT in your context blocks are unverified testimony — weigh them, but never call them "new evidence" or "verified", and never restate them as your own facts. If you reverse a verdict on them, say plainly that you are deferring to the owner's account.
{topic_lock_rule}
{no_invention_rule}
{no_library_actions_rule}
{no_monologue_rule}
{analytical_integrity_rule}
SIZE SENSE: Judge file size by MB-PER-MINUTE for its resolution/codec class, never raw GiB. A 4K film or a series with many episodes/specials is large in total GB but usually NORMAL per minute — do NOT call that bloat, hoarding, or "an affront to efficiency". Keeping a high-fidelity copy of content the user VALUES is not waste. Genuine bloat is a disproportionate bitrate for the resolution (e.g. a 1080p file carrying 4K-level MB/min), or redundant duplicate versions of one title. When a "[size: …]" tag appears next to a library item, trust it over your own size intuition.

FORMATTING RULES:
- Separate paragraphs with a single blank line. Do not create walls of text.
- Use **bold** for media titles.
- Output the response as plain markdown text — do NOT write the literal characters \\n or escape sequences in the answer; press an actual line break instead.
"""

    # 3. Build message list with history
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation)
    messages.append({"role": "user", "content": message.message})

    # 4. Save user message to history (scoped to the active thread)
    _save_message(user.id, "user", message.message, db, thread_id=thread_id)

    # 5. Stream from Ollama
    async def generate() -> AsyncGenerator[str, None]:
        from src.services.llm_priority import (
            curator_start, curator_done, check_curator_vram_health, curator_busy,
            gate_owner,
        )

        # Pass 14.9: emit collected pre-stream status events so the frontend
        # can show the user what we did during the cascade / lookup phase
        # ("Looking up X", "Found in tv", "Loaded 3 memories"). They arrive
        # in a quick burst right before the curator stream — frontend animates
        # each one for a beat so the sequence reads as progressive feedback.
        for status_msg in pre_stream_status:
            yield f"data: {json.dumps({'status': status_msg})}\n\n"

        # If another big-model generation is already running (another user
        # chatting, or a recs / proactive / verification job), tell the user
        # they're queued: a single GPU serves ONE curator generation at a
        # time, so we wait for the slot instead of thrashing it. The
        # curator_start() call below blocks until the slot frees.
        if curator_busy():
            _holder = gate_owner()
            _busy = (f"Curatarr is busy ({_holder}) — you are next in line…"
                     if _holder else
                     "Curatarr is busy with another request — you are next in line…")
            yield f"data: {json.dumps({'status': _busy})}\n\n"

        await curator_start("chat")

        # Slot acquired — Curator is now actually working. From here on EVERY
        # yield/await must live inside the try below: a client disconnect
        # cancels this generator at the next yield, and a cancel landing in
        # the gap between the acquire and the try (exactly the "💭 thinking"
        # yield) skipped the finally — curator_done() never ran, the gate
        # stayed held forever by a dead "chat" stream, and every later request
        # queued behind it with an idle GPU (the Little Singles hang: the user
        # aborted while queued, the retry then waited on the leaked slot).
        full_response = ""
        think_filter = ThinkTagStreamFilter(enabled=_cfg.LLM_THINK_TAGS)

        # VRAM health probe runs in parallel: ~2 s after the curator request
        # starts the model is loaded, so we sample /api/ps then. If the
        # curator spilled to CPU (race lost on eviction, or summarizer's
        # eviction polling timed out), we surface a warning frame to the
        # frontend so the user knows the response will be slow.
        curator_model = settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL

        async def _delayed_vram_probe():
            await asyncio.sleep(2.0)
            return await check_curator_vram_health(curator_model)

        health_task = None
        health_warned = False

        # Pass 46 (Bug 2): track whether the client cut us off mid-stream.
        # If they did, we skip persisting the partial response and skip
        # all post-chat background tasks (memory extraction on half a
        # sentence is noise; protection-intent on a fragment misfires).
        # asyncio.CancelledError + GeneratorExit are BaseException-derived
        # so ``except Exception`` below does NOT catch them — they fall
        # through to the finally where we honour the flag.
        client_disconnected = False

        try:
            yield f"data: {json.dumps({'status': 'Curatarr is thinking…'})}\n\n"
            # Context-budget watchdog: input + num_predict share the
            # CURATOR_NUM_CTX window — past the budget the generation gets
            # squeezed and dies mid-word (the Kill la Kill reply broke off
            # after two sentences back on the 8192 window). Keep the size
            # VISIBLE so any truncation names its cause.
            _prompt_chars = sum(len(m.get("content") or "") for m in messages)
            _sys_chars = sum(len(m.get("content") or "") for m in messages
                             if m.get("role") == "system")
            _hist_chars = _prompt_chars - _sys_chars
            _input_budget_chars = (CURATOR_NUM_CTX - 4096) * 4   # ~4 chars/token
            if _prompt_chars > _input_budget_chars * 0.9:
                logger.warning(
                    "[chat] prompt size %d chars (~%d tokens; system=%d, "
                    "history+user=%d) — near the %d-token window minus 4096 "
                    "predict; expect squeezed generation",
                    _prompt_chars, _prompt_chars // 4, _sys_chars, _hist_chars,
                    CURATOR_NUM_CTX)
            else:
                logger.info("[chat] prompt size: %d chars (system=%d, history+user=%d)",
                            _prompt_chars, _sys_chars, _hist_chars)
            health_task = asyncio.create_task(_delayed_vram_probe())
            # Timeout raised from 120 s → 600 s: with a partial-CPU fallback
            # the response can take 5-10 minutes. We'd rather wait it out
            # than 504 the user mid-stream when their model has spilled.
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_url}/api/chat",
                    json={
                        "model": curator_model,
                        "messages": messages,
                        "stream": True,
                        "keep_alive": CURATOR_KEEP_ALIVE,
                        # 2048 cut long, in-flight monologues off mid-sentence
                        # (the "hoarding" critique stopped at "...The Godfather").
                        # 4096 output + CURATOR_NUM_CTX=16384 window (benchmarked
                        # 2026-07-08: 100% GPU with nomic resident) leaves ~12k
                        # input tokens — the 33.5k-char Kill la Kill turn fits.
                        **curator_options(temperature=0.7, num_predict=4096),
                    },
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            raw_token = chunk.get("message", {}).get("content", "")
                            if raw_token:
                                # Emit any pending VRAM-fallback warning ONCE,
                                # right before the first user-visible token.
                                if not health_warned and health_task.done():
                                    try:
                                        h = health_task.result()
                                        if h.get("message"):
                                            logger.warning(
                                                "[chat] VRAM fallback detected: %s%% on CPU",
                                                h.get("cpu_pct"),
                                            )
                                            yield f"data: {json.dumps({'warning': h['message'], 'severity': h.get('severity', 'moderate')})}\n\n"
                                    except Exception:
                                        pass
                                    health_warned = True

                                full_response += raw_token
                                token = think_filter.feed(raw_token)
                                if token:
                                    yield f"data: {json.dumps({'token': token})}\n\n"
                            if chunk.get("done"):
                                remainder = think_filter.flush()
                                if remainder:
                                    yield f"data: {json.dumps({'token': remainder})}\n\n"
                                break
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            msg = f"Ollama not reachable at {ollama_url}. Make sure Ollama is running and '{settings.CURATOR_MODEL}' is pulled."
            full_response = msg
            yield f"data: {json.dumps({'token': msg})}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            # Pass 46 (Bug 2): client closed the SSE connection mid-stream.
            # Mark disconnect, then re-raise so the runtime actually
            # cancels the coroutine (swallowing CancelledError is a known
            # asyncio footgun — it traps the task instead of unwinding).
            client_disconnected = True
            logger.info(
                "[chat] client disconnected mid-stream (had %d chars buffered)",
                len(full_response),
            )
            raise

        except Exception as e:
            msg = f"Error: {e}"
            full_response = msg
            yield f"data: {json.dumps({'token': msg})}\n\n"

        finally:
            curator_done()
            # Cancel the VRAM probe if it's still pending (e.g. very fast
            # response that finished before the 2 s probe even fired, or a
            # cancel before the probe task was even created).
            if health_task and not health_task.done():
                health_task.cancel()
            # Strip think blocks from the full collected response before persisting
            full_response = strip_think_tags(full_response).strip()
            # Save assistant response to history (same thread as the user turn).
            # Pass 48: the redundant ChatInteraction write was removed —
            # ConversationMessage already holds the user+assistant exchange,
            # and nothing read the ChatInteraction copy.
            #
            # Pass 46 (Bug 2): on client disconnect, skip persistence AND the
            # three post-chat background tasks. A half-sentence in the
            # conversation history would poison the next turn's context
            # ("you just said …" referencing a fragment), and memory-extraction
            # / protection-intent / verification-match running on a torn-off
            # response is noise at best, false-positive at worst.
            if full_response and not client_disconnected:
                from src.database.connection import get_db_session
                with get_db_session() as db2:
                    _save_message(user.id, "assistant", full_response, db2, thread_id=thread_id)

                # Pass 41: route the three post-chat background tasks through
                # ``track_task`` so the GC can't collect them mid-run. Before
                # this, the create_task return value was discarded — under
                # memory pressure the asyncio event loop's weak reference
                # would let the task vanish, silently losing memory-extraction,
                # protection-intent detection, or verification-response
                # matching from a turn.
                from src.services.bg_tasks import track_task

                # Pass 61: schedule a DEBOUNCED thread-level memory extraction
                # instead of extracting from this single exchange. A follow-up
                # turn within the debounce window cancels + reschedules it, so
                # extraction fires ONCE over the whole conversation — capturing
                # the user's final settled position, not the intermediate
                # stances they moved through. The debounce task is held in
                # episodic_memory._pending_thread_extracts, so it's GC-safe
                # without track_task.
                from src.services.episodic_memory import schedule_thread_extraction
                schedule_thread_extraction(user.id, thread_id, media_category=domain)

                # Detect protection intents ("behalten", "keep", "nicht löschen", …)
                # Pass 23: resolve the current thread's anchor title (e.g. the
                # deletion-proposal target) so pronoun-only signals like
                # "we are keeping this show" still register as protection
                # against the right title.
                anchor = _thread_active_title.get(thread_id)
                anchor_title = anchor[0] if anchor else None
                anchor_category = anchor[2] if anchor and len(anchor) > 2 else None
                if thread_id.startswith("principle:"):
                    # Principle-review thread: parse the settled decision
                    # (adopt / reject / refined wording) and apply it — a
                    # media-protection scan makes no sense here.
                    try:
                        _pid = int(thread_id.split(":", 1)[1])
                    except (ValueError, IndexError):
                        _pid = 0
                    if _pid:
                        from src.services.curator_principles import (
                            detect_and_apply_principle_verdict)
                        track_task(
                            detect_and_apply_principle_verdict(
                                user.id, _pid, message.message, full_response),
                            name="principle_verdict_bg",
                        )
                else:
                    track_task(
                        _check_protection_intent_bg(
                            user.id, message.message, full_response,
                            anchor_title=anchor_title,
                            anchor_category=anchor_category,
                            thread_id=thread_id,
                        ),
                        name="check_protection_intent_bg",
                    )

                # Check if this might be answering a verification question.
                # Pass 71: thread_id gates it — only a turn inside the
                # verification question's own proactive_message thread counts
                # as an answer.
                track_task(
                    _check_verification_response(user.id, message.message, thread_id),
                    name="check_verification_response",
                )

            # Pass 46 (Bug 2): a cancelled async generator can't yield —
            # the runtime is already unwinding it. Skip the final frame
            # if the client is gone. Other clients waiting on the same
            # endpoint aren't affected: each request has its own generator.
            if not client_disconnected:
                yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class CorrectAnchorRequest(BaseModel):
    thread_id: str = "general"


@router.post("/correct-anchor")
async def correct_chat_anchor(
    req: CorrectAnchorRequest,
    user: User = Depends(get_current_user),
):
    """Pass 18: invalidate the cached enrichment for this thread's
    current anchor (the title the chat is currently focused on).

    Use case: the LLM cited something that's wrong and the cached profile
    keeps reproducing the error. Click "🔄 Re-fetch metadata" → this
    endpoint deletes the matching MetadataCache row + drops every
    in-memory thread anchor pointing at the same title, so the next
    chat turn re-runs the cascade and re-fetches fresh data from the
    upstream APIs.

    Cache key shape matches what ``enrich_media_item`` writes for
    chat-cascade entries (no IDs passed at call time, so the key always
    falls back to ``enriched:{media_type}:{title[:40]}``).

    Returns ``ok: false`` with a human-readable reason when no anchor
    exists yet — the UI should surface that instead of silently doing
    nothing.
    """
    cached = _thread_active_title.get(req.thread_id)
    if not cached:
        return {
            "ok": False,
            "reason": "no active topic in this thread yet — ask Curatarr something first",
        }
    title  = cached[0]
    domain = cached[2] if len(cached) > 2 else "movie"
    media_type = domain or "movie"

    # Wipe MetadataCache row(s). Same key shape that enrich_media_item
    # uses when called WITHOUT ids (the chat cascade path) — we also try
    # alternate id-keyed variants in case the user previously hit the
    # same title via a path that supplied tmdb_id/anilist_id.
    from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION
    deleted = 0
    keys_tried: list[str] = []
    mc = MetadataCache()
    try:
        # Primary: title-keyed (chat cascade default)
        keys_tried.append(f"enriched:{media_type}:{title[:40]}")
        # Defensive: a few possible alternative shapes if a different code
        # path pre-populated the cache. Cheap to try — DELETE on a missing
        # key is a no-op.
        for alt in (media_type, "movie", "show", "anime", "music"):
            if alt != media_type:
                keys_tried.append(f"enriched:{alt}:{title[:40]}")
        for raw_key in set(keys_tried):
            full = f"{_CACHE_VERSION}:{raw_key}"
            try:
                result = mc.conn.execute(
                    "DELETE FROM api_cache WHERE cache_key = ?", (full,),
                )
                deleted += (result.rowcount or 0)
            except Exception as e:
                logger.warning("[chat] correct-anchor delete failed for %s: %s", full, e)
        mc.conn.commit()
    finally:
        mc.close()

    # Drop every in-memory anchor pointing at this title across all
    # threads — otherwise a parallel thread would still serve the
    # already-loaded payload until the next "+ New" reset.
    cleared_threads = 0
    for tid, val in list(_thread_active_title.items()):
        if val and val[0] == title:
            _thread_active_title.pop(tid, None)
            cleared_threads += 1

    logger.info(
        "[chat] correct-anchor: title=%r media_type=%s rows=%d threads=%d",
        title, media_type, deleted, cleared_threads,
    )
    return {
        "ok": True,
        "title": title,
        "media_type": media_type,
        "cleared_keys": deleted,
        "cleared_threads": cleared_threads,
        "message": f"Cache cleared for '{title}' — your next question will re-fetch metadata fresh.",
    }


@router.get("/history")
async def get_chat_history(
    limit: int = 50,
    thread_id: str = "general",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return conversation history for one thread.

    ``thread_id`` defaults to ``general`` (the main free-chat). Pass
    ``deletion_proposal:<id>`` or ``proactive_message:<id>`` to read a
    specific discussion thread.
    """
    q = db.query(ConversationMessage).filter(ConversationMessage.user_id == user.id)
    if thread_id == "general":
        q = q.filter(
            (ConversationMessage.thread_id == "general")
            | (ConversationMessage.thread_id.is_(None))
        )
    else:
        q = q.filter(ConversationMessage.thread_id == thread_id)
    msgs = q.order_by(ConversationMessage.created_at.desc()).limit(limit).all()
    return {
        "thread_id": thread_id,
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
            for m in reversed(msgs)
        ],
    }


@router.delete("/history")
async def clear_chat_history(
    thread_id: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear conversation memory.

    Pass 48: the docstring previously mentioned a parallel ChatInteraction
    table that was kept for thumbs-up/down feedback. That table is no
    longer written to (the feedback UI was removed earlier and nothing
    else consumed the rows). This endpoint just clears
    ConversationMessage.

    With no ``thread_id``: wipes ALL of the user's conversation messages
    across every thread. With ``thread_id`` set: only that thread is cleared,
    so nuking one discussion doesn't take out the rest.

    Pass 14.10: also drops the in-memory ``_thread_active_title`` cache for
    the affected thread(s). Without that step the title-pivot detector would
    still see the old cached title and treat the next chat turn as a
    "topic switch" from a thread that no longer has any history.

    Pass 61: when clearing a specific thread, flush its pending debounced
    memory extraction FIRST — otherwise "+New" wipes the conversation
    before the 90 s debounce ever fires and the whole exchange is lost to
    long-term memory. Done inline (one summarizer call, ~2-5 s) so the
    extraction reads the messages while they still exist. The wipe-all
    branch (thread_id=None) skips this — flushing every thread serially
    would stall the request, and a full wipe is a deliberate hard reset.
    """
    if thread_id is not None and thread_id != "":
        try:
            from src.services.episodic_memory import flush_thread_extraction
            await flush_thread_extraction(user.id, thread_id)
        except Exception as e:
            logger.debug("[chat] pre-clear memory flush failed for %s: %s", thread_id, e)

    q = db.query(ConversationMessage).filter(ConversationMessage.user_id == user.id)
    if thread_id is not None:
        if thread_id == "general":
            q = q.filter(
                (ConversationMessage.thread_id == "general")
                | (ConversationMessage.thread_id.is_(None))
            )
            _thread_active_title.pop("general", None)
        else:
            q = q.filter(ConversationMessage.thread_id == thread_id)
            _thread_active_title.pop(thread_id, None)
    else:
        # No thread filter → wipe everything
        _thread_active_title.clear()
    deleted = q.delete(synchronize_session=False)
    db.commit()
    return {"status": "cleared", "thread_id": thread_id, "deleted": deleted}


class FlushMemoriesRequest(BaseModel):
    thread_id: str = "general"


@router.post("/flush-memories")
async def flush_memories(
    req: FlushMemoriesRequest,
    user: User = Depends(get_current_user),
):
    """Pass 61: fire a thread's pending debounced memory extraction NOW.

    Called by the frontend on explicit end-of-conversation signals —
    "Exit discussion" and "Delete & exit" — so the conversation's memories
    are captured immediately instead of waiting out the 90 s debounce (or
    being lost if the user never sends another message in the thread).
    Idempotent: the extraction cursor means a no-op flush just returns.
    """
    try:
        from src.services.episodic_memory import flush_thread_extraction
        await flush_thread_extraction(user.id, req.thread_id)
        return {"ok": True, "thread_id": req.thread_id}
    except Exception as e:
        logger.warning("[chat] flush-memories failed for %s: %s", req.thread_id, e)
        return {"ok": False, "thread_id": req.thread_id, "error": str(e)}
