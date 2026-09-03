"""Conversation starters: a pooled, expiring, variety-enforced opener supply.

The three hardcoded prompts on the landing page were placeholders. This
replaces them with the proactive-messages pattern — background generation
into a pool, consumption with decay — plus the lesson learned from that
pattern's weakness: the bubbles all sound the same, because every trigger
writes in the same register and nothing ever forces variety.

The anti-sameness rules therefore live in CODE, on the output:

* every starter in a batch must use a DIFFERENT form (question /
  observation / challenge / callback / tonight_pick) — a JSON field the
  parser enforces, not a prose wish;
* every starter must be anchored to a concrete FACT handed to the model
  (a title, a count, a time gap) — generic candidates are dropped;
* candidates that open with the same words as a recent starter are
  dropped (the model repeats its favourite openings otherwise);
* the pool decays: TTL, an impression cap for starters nobody clicks,
  and one-shot retirement on click. Ignored is a signal.

Starters are CURATOR OPENERS: clicking a chip makes the curator SAY the
line and the user replies — the proactive-message stage direction, not
the suggested-prompt one. (The design went the other way twice first:
curator-voiced text sent as the USER'S message swapped the roles on
stage — observed live, the user "analysing" their own Psycho-Pass stall
while the curator read the "you" as itself — and rewriting the voice to
first person fixed the grammar but wasted the register. The MECHANIC
moved instead; the voice was right all along.) The reply turn re-injects
the opener via the ``starter`` discuss context: a server-owned row with
an ownership check, never a persisted fake assistant turn.

Dayparts make "time-of-day dependent" cheap: starters are TAGGED at
generation (a tonight-pick is an evening starter), and selection filters
by the current hour — no per-request LLM anywhere.

Batch surfaces speak English by the deterministic-language law
(llm_utils.detect_user_language rule 3); the reply follows whatever
language the user answers in, as always.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.database.models import ChatStarter, WatchHistoryEntry
from src.services.llm_utils import strip_think_tags, curator_options, CURATOR_KEEP_ALIVE

logger = logging.getLogger(__name__)

STARTER_TTL_HOURS = 48
# The landing page loads often; ~a dozen visits over two days without a
# click means the starter did not land. Move on.
MAX_IMPRESSIONS = 12
POOL_TARGET = 8          # generation tops the pool up to about this many
FORMS = ("question", "observation", "challenge", "callback", "tonight_pick")

# A weekday name anchors an opener to one calendar day: "It is Wednesday
# evening" surfacing on Thursday reads like a broken clock. So a day-named
# starter expires at local midnight instead of the 48h TTL, and pick time
# retires any survivor whose day no longer matches (covers old pool rows).
# \b keeps habitual plurals ("on Sundays") out — those are day-agnostic.
_DAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday"
                     r"|sunday|montag|dienstag|mittwoch|donnerstag|freitag"
                     r"|samstag|sonntag)\b", re.IGNORECASE)
_DAY_NAMES = {0: ("monday", "montag"), 1: ("tuesday", "dienstag"),
              2: ("wednesday", "mittwoch"), 3: ("thursday", "donnerstag"),
              4: ("friday", "freitag"), 5: ("saturday", "samstag"),
              6: ("sunday", "sonntag")}


def _named_day(text: str) -> Optional[str]:
    m = _DAY_RE.search(text or "")
    return m.group(1).lower() if m else None


def _end_of_local_day_utc() -> datetime:
    """Next local midnight, in the naive-UTC terms the pool compares with."""
    local = datetime.now().astimezone()
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0,
                                              second=0, microsecond=0)
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


def current_daypart(now: Optional[datetime] = None) -> str:
    h = (now or datetime.now()).hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 17:
        return "day"
    if 17 <= h < 23:
        return "evening"
    return "night"


# ── facts: deterministic, cheap, per user ──────────────────────────────────

def collect_facts(user_id: int, now: Optional[datetime] = None) -> list[dict]:
    """Concrete hooks from the user's own watch log. No LLM, one query.

    Every fact is something a starter can NAME — the specificity is what
    keeps ten starters from sounding like one starter ten times.
    """
    now = now or datetime.utcnow()
    facts: list[dict] = []
    # Two queries, plain dicts, everything read INSIDE the session — the
    # first cut read ORM rows after the session closed (DetachedInstanceError,
    # swallowed by the background refill: zero starters, zero log lines).
    # And it took "the last 400 rows" of a history where music outnumbers
    # video 300:1, so the video facts never made the window: a Spotify
    # afternoon erased the weekend's binge. Video gets its own window.
    with get_db_session() as db:
        video = [
            {"title": r.title, "series_title": r.series_title,
             "media_type": r.media_type, "season": r.season,
             "episode": r.episode, "viewed_at": r.viewed_at,
             "completed": bool(r.completed)}
            for r in (db.query(WatchHistoryEntry)
                      .filter(WatchHistoryEntry.user_id == user_id,
                              WatchHistoryEntry.media_type.in_(
                                  ("movie", "show", "anime")))
                      .order_by(WatchHistoryEntry.viewed_at.desc())
                      .limit(200).all())]
        music = [
            {"series_title": r.series_title, "viewed_at": r.viewed_at}
            for r in (db.query(WatchHistoryEntry)
                      .filter(WatchHistoryEntry.user_id == user_id,
                              WatchHistoryEntry.media_type == "music",
                              WatchHistoryEntry.viewed_at
                              >= now - timedelta(days=14))
                      .limit(4000).all())]

    if video:
        r = video[0]
        days = max(0, (now - r["viewed_at"]).days) if r["viewed_at"] else None
        facts.append({
            "kind": "last_watched",
            "title": r["series_title"] or r["title"],
            "media_type": r["media_type"],
            "episode": (f"S{r['season']}E{r['episode']}"
                        if r["season"] is not None and r["episode"] is not None
                        else None),
            "days_ago": days,
            "completed": r["completed"],
        })

    # Per-series aggregates over the recent window: momentum and abandonment.
    series: dict[str, dict] = {}
    for r in video:
        if (r["media_type"] not in ("show", "anime")
                or not (r["series_title"] or r["title"])):
            continue
        key = r["series_title"] or r["title"]
        agg = series.setdefault(key, {"plays": 0, "last": None,
                                      "media_type": r["media_type"]})
        agg["plays"] += 1
        if agg["last"] is None or (r["viewed_at"] and r["viewed_at"] > agg["last"]):
            agg["last"] = r["viewed_at"]
    active = [(k, v) for k, v in series.items()
              if v["last"] and (now - v["last"]).days <= 7 and v["plays"] >= 3]
    stalled = [(k, v) for k, v in series.items()
               if v["last"] and 21 <= (now - v["last"]).days <= 120 and v["plays"] >= 3]
    if active:
        k, v = max(active, key=lambda kv: kv[1]["plays"])
        facts.append({"kind": "current_binge", "title": k,
                      "media_type": v["media_type"],
                      "plays_recent": v["plays"]})
    if stalled:
        k, v = max(stalled, key=lambda kv: kv[1]["plays"])
        facts.append({"kind": "stalled_series", "title": k,
                      "media_type": v["media_type"],
                      "days_since": (now - v["last"]).days,
                      "plays_before_stall": v["plays"]})

    if music:
        artists: dict[str, int] = {}
        for r in music:
            if r["series_title"]:
                artists[r["series_title"]] = artists.get(r["series_title"], 0) + 1
        if artists:
            top, plays = max(artists.items(), key=lambda kv: kv[1])
            if plays >= 10:
                facts.append({"kind": "music_rotation", "artist": top,
                              "plays_14d": plays})

    # Wall clock, not the UTC `now` of the query windows: between local
    # midnight and 2am the UTC weekday is still yesterday's.
    facts.append({"kind": "now", "weekday": datetime.now().strftime("%A"),
                  "daypart": current_daypart()})
    return facts


# ── generation: one LLM batch → enforced-diverse pool rows ────────────────

def _extract_json_array(text: str) -> list[dict]:
    t = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fenced:
        t = fenced.group(1).strip()
    if not t.startswith("["):
        s, e = t.find("["), t.rfind("]")
        if s != -1 and e > s:
            t = t[s:e + 1]
    data = json.loads(t)
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _opening(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split()[:3])


_PROMPT = """You are Curatarr, an opinionated personal media curator. You write OPENERS: short first messages FROM YOU TO THE USER, shown as clickable chips on your chat page. Clicking one makes you SAY it — the conversation begins with your line, and the user replies to you. Write in YOUR voice, addressed to the user: direct, curious, a little pointed, never generic.

FACTS about this user's recent watching (use them — an opener that names a real title, number or gap beats any clever generality):
{facts}

Write {n} openers as a JSON array. Each element:
{{"text": "...", "form": "...", "daypart": "...", "fact": "...",
  "anchor_title": "... or null", "anchor_media_type": "movie|show|anime|music or null"}}

Rules:
- "form" must be one of {forms} and every opener must use a DIFFERENT form
  (question: you ask the user something; observation: you point out a
  pattern of theirs; challenge: you provoke a defence; callback: you pick
  an earlier thread back up; tonight_pick: you offer to pick for tonight).
- "text": max 140 characters, no emoji, no quotes around titles needed.
- "fact": which fact anchored it, in a few words. An opener with no
  anchoring fact is invalid.
- "anchor_title"/"anchor_media_type": copied VERBATIM from the fact's
  title/media_type when the opener is about one specific title; null when
  it is not (a general tonight-pick, a weekday observation).
- "daypart": morning|day|evening|night|any — when this opener fits best
  (a tonight-pick is evening; most others are any).
- No two openers may open with the same words.
- Name today's weekday only when it genuinely matters — such openers are shown today only.

Return ONLY the JSON array."""


async def generate_starters(user_id: int, n: int = 5) -> int:
    """One LLM batch → validated, deduped pool rows. Returns inserted count."""
    facts = collect_facts(user_id)
    if len(facts) <= 1:          # only the clock fact → nothing to anchor on
        return 0

    prompt = _PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, indent=1),
                            n=n, forms=list(FORMS))
    # Through the priority gate, never past it: a deletion run holding the
    # pitcher must not get the curator loaded on top by a background refill.
    # The gate queues us until the GPU is free.
    from src.services.llm_priority import curator_priority
    content = None
    async with curator_priority("chat starters"):
        for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
            if not model:
                continue
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    resp = await client.post(
                        f"{settings.effective_ollama}/api/chat",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "keep_alive": CURATOR_KEEP_ALIVE,
                            **curator_options(temperature=0.9, num_predict=900),
                        },
                    )
                if resp.status_code == 200:
                    content = strip_think_tags(
                        resp.json().get("message", {}).get("content", "").strip())
                    if content:
                        break
            except Exception as e:
                logger.warning("[starters] %s failed: %s", model, e)
    if not content:
        return 0

    try:
        candidates = _extract_json_array(content)
    except Exception as e:
        logger.warning("[starters] unparseable batch for user %d: %s", user_id, e)
        return 0

    # Enforcement — in code, where it cannot be sweet-talked.
    with get_db_session() as db:
        recent = (db.query(ChatStarter.text)
                  .filter(ChatStarter.user_id == user_id)
                  .order_by(ChatStarter.created_at.desc())
                  .limit(30).all())
    seen_openings = {_opening(r.text) for r in recent}
    seen_forms: set = set()
    now = datetime.utcnow()
    accepted = []
    for c in candidates:
        text = (c.get("text") or "").strip()
        form = (c.get("form") or "").strip()
        fact = (c.get("fact") or "").strip()
        daypart = (c.get("daypart") or "any").strip()
        if not (15 <= len(text) <= 200):
            continue
        if form not in FORMS or form in seen_forms:
            continue                          # different form per starter
        if not fact:
            continue                          # unanchored = generic = dropped
        if _opening(text) in seen_openings:
            continue                          # the model repeats openings
        if daypart not in ("morning", "day", "evening", "night", "any"):
            daypart = "any"
        day = _named_day(text)
        if day is not None and day not in _DAY_NAMES[datetime.now().weekday()]:
            continue                          # names a day that isn't today
        anchor = (c.get("anchor_title") or "").strip() or None
        anchor_mt = (c.get("anchor_media_type") or "").strip() or None
        if anchor_mt not in (None, "movie", "show", "anime", "music"):
            anchor_mt = None
        seen_forms.add(form)
        seen_openings.add(_opening(text))
        accepted.append(ChatStarter(
            user_id=user_id, text=text, form=form, daypart=daypart,
            fact_used=fact[:200], created_at=now,
            anchor_title=anchor[:512] if anchor else None,
            anchor_media_type=anchor_mt,
            expires_at=(_end_of_local_day_utc() if day
                        else now + timedelta(hours=STARTER_TTL_HOURS)),
        ))

    if accepted:
        with get_db_session() as db:
            for row in accepted:
                db.add(row)
            db.commit()
    logger.info("[starters] user %d: %d/%d candidates accepted",
                user_id, len(accepted), len(candidates))
    return len(accepted)


# ── consumption ────────────────────────────────────────────────────────────

def _fresh_filter(q, user_id: int, now: datetime):
    return (q.filter(ChatStarter.user_id == user_id,
                     ChatStarter.used_at.is_(None),
                     ChatStarter.impressions < MAX_IMPRESSIONS)
            .filter((ChatStarter.expires_at.is_(None))
                    | (ChatStarter.expires_at > now)))


def pool_fresh_count(user_id: int) -> int:
    now = datetime.utcnow()
    with get_db_session() as db:
        return _fresh_filter(db.query(ChatStarter), user_id, now).count()


def pick_starters(user_id: int, limit: int = 3) -> list[dict]:
    """Up to ``limit`` fresh starters for the current daypart, forms as
    distinct as the pool allows, least-shown first. Counts the impression."""
    now = datetime.utcnow()
    dp = current_daypart()
    with get_db_session() as db:
        pool = (_fresh_filter(db.query(ChatStarter), user_id, now)
                .filter((ChatStarter.daypart.in_((dp, "any")))
                        | (ChatStarter.daypart.is_(None)))
                .order_by(ChatStarter.impressions.asc(),
                          ChatStarter.created_at.desc())
                .limit(limit * 4).all())
        # Day-named rows from before the midnight-expiry rule (or from a
        # DST edge) get retired here instead of shown a day late.
        today = _DAY_NAMES[datetime.now().weekday()]
        alive = []
        for row in pool:
            d = _named_day(row.text)
            if d is not None and d not in today:
                row.expires_at = now           # frees the slot for a refill
            else:
                alive.append(row)
        pool = alive
        picked, forms_used = [], set()
        for row in pool:                       # unique forms first…
            if row.form not in forms_used:
                picked.append(row)
                forms_used.add(row.form)
            if len(picked) == limit:
                break
        for row in pool:                       # …then fill up regardless
            if len(picked) == limit:
                break
            if row not in picked:
                picked.append(row)
        for row in picked:
            row.impressions = (row.impressions or 0) + 1
        db.commit()
        return [{"id": r.id, "text": r.text} for r in picked]


def mark_used(user_id: int, starter_id: int) -> bool:
    with get_db_session() as db:
        row = db.query(ChatStarter).filter(
            ChatStarter.id == starter_id,
            ChatStarter.user_id == user_id).first()
        if not row:
            return False
        row.used_at = datetime.utcnow()
        db.commit()
        return True
