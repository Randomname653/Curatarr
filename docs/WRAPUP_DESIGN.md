# Wrap-Up — Research, Audit & Flagship Redesign

Status: **design, awaiting approval** (2026-10-23). No code from this document
is in the tree yet; section 3 is the drop-in draft the implementation PRs
will start from.

Decisions taken with the owner before this was written:

| Question | Decision |
|---|---|
| Deliverable | Design first, code after approval (this document) |
| Audience | **Per person** — every household member gets their own wrap-up |
| Curation Report | **Folds into** the wrap-up (no second page) |
| Media split | Per medium **and** combined — scopes `all · movie · show · anime · music` |

Still open (defaults chosen, see §4): tone/language, image export, household
comparisons.

---

## 1. Current State Critique & Benchmark Gap Analysis

### 1.1 What exists today

There is no wrap-up. The closest thing is the admin **Curation Report**:

| Piece | Where | What it does |
|---|---|---|
| Aggregator | `src/services/curation_stats.py:65` `build_curation_stats()` | 12 monthly buckets of debate outcomes (`CuratorResolutionLog`), GB freed per month (`DeletionProposal.storage_mb`), Stubbornness Index (override-kept titles untouched 90+ days), redundancy report, watch-hours per month/type, last 10 explicit-feedback rows from the taste blobs |
| Route | `src/routers/stats.py:26` `GET /api/stats/curation` | `require_admin`; attaches a cached narrative from `app_state["curation_narrative:{uid}:{year}"]` |
| Narrative | `src/routers/stats.py:41` `POST /api/stats/curation/narrative` | One f-string prompt → `_call_llm(prompt, max_tokens=400)` → one free-text paragraph, stored per user+year |
| UI | `frontend/js/report.js` | Five flat `.stat-box` tiles, 12-column month chart, debate split bar, Stubbornness and Taste-evolution lists, the paragraph |
| Nav | `frontend/js/nav.js:62` | `report` is in `ADMIN_VIEWS` — household members never see it |

Related but not a recap: `plex_sync._generate_taste_summary()` (the per-type
taste summary that feeds the judge, `TasteVectorEntry.summary_text`) and
`chat_starters` (LLM-phrased one-liners from deterministic watch facts — the
right *pattern*, wrong *format*).

Generation cost today: one curator call, 400 tokens, 120 s HTTP timeout per
model in `_call_llm`; with the 27B curator warm that is ~20–40 s, cold
considerably more. No progress indication beyond a busy button.

### 1.2 Why it falls short

1. **Wrong subject.** The docstring says it plainly: "the yearly review of the
   CURATION work (not their viewing habits)". It is a ledger of the owner's
   debates with the curator, not a portrait of anyone's taste.
2. **Wrong audience.** Admin-only. The people who would enjoy a wrap-up most —
   the rest of the household — are locked out.
3. **Thin facts → generic prose.** The prompt gets five totals and a list of
   up to five titles. With nothing specific to say, any model writes
   "what the numbers say about this collection". There is no outlier, no
   contrast, no moment, no item the prose could anchor on.
4. **Free text, unvalidated.** No schema, no reference checking, no number
   guard. The model may invent a figure and nothing notices.
5. **One horizon.** Fixed 12 months / current calendar year; the narrative key
   is per year, so last December's review is overwritten in place by January's.
6. **Data bugs the redesign must not inherit:**
   - Watch hours sum `duration_ms` of every play (`curation_stats.py:126`):
     an abandoned 10-minute start of a 3-hour film counts as 3 hours.
   - **Spotify plays count zero.** `spotify_import.py` stores `duration_ms=None`
     and the played time in `view_offset_ms`, so imported listening never reaches
     the hours figure.
   - Plex history-import rows (`plex_sync.py:1503`) carry no duration at all
     (`completed=1`, `duration_ms` NULL) — they count zero too, while being the
     bulk of older history.
7. **No artwork, no motion, no identity.** Flat tiles, no posters, no
   `--poster-tint`, no `countTo()`, no share surface.

### 1.3 What the data can actually support (checked in code)

| Signal | Source | Quality |
|---|---|---|
| Per-play timestamp, played offset, completion, genres, tmdb id | `watch_history` (`models.py:72`) | Good for Plex sync; history-import rows lack duration → **estimate + flag** |
| Spotify streams | `watch_history` `source="spotify"`, ms played in `view_offset_ms` | Good (per stream, ≥ MIN_MS_PLAYED) |
| Sittings / binges / phantom "mark watched" rows | `services/viewing_sessions.py` `sittings()` | Good — already filters bulk-marks and upgrade re-imports |
| Drops | `taste_engine._is_drop`, `SERIES_DROP_*` | Good |
| User's own score, film/TV | `swipe_ratings.stars` (1–10) | Good where swiped. **Not** Plex `userRating` on video — Kometa overwrites it (`models.py:550`) |
| User's own score, music | `plex_ratings` (PlexAmp stars) | Good |
| Community score + vote mass, RT, Metacritic, significance, year, runtime | `media_enricher.build_verified_data()` (cache only, no LLM, no fetch) | Good for enriched titles |
| Release year | `media_identities.year`, verified data | Good for video; **none for tracks** (artist-level only) |
| Deletions, GB, "the one that came back" | `deletion_proposals` (`status`, `storage_mb`, `resolved_at`, `returned_at`) | Good — but keyed to whoever ran the curation (the owner) |
| Defended titles | `curator_resolution_log` (override + `override_reason`), `protected_media` (`source="manual"`) | Good |
| Most-debated thread | `conversation_messages.thread_id = 'deletion_proposal:{id}'` | Good |
| Principles adopted | `curator_principles.activated_at`, `status="active"` | Good |
| Unwatched-but-owned | `media_tech_profiles.size_mb` minus the user's played set; `cached_recommendations lane="library"` | Good |
| Taste vectors + clusters | Chroma item vectors (`taste_engine._chroma_item_vector`), `_cluster_centroids`, `_domain_corpus_mean` | Good; clustering needs ≥40 items |

### 1.4 Benchmark — what the market does (late 2025 / 2026)

| Product | Mechanic worth stealing |
|---|---|
| **Spotify Wrapped 2025** | *Listening Age* (the era your plays belong to); *Clubs* — behavioural roles like "Scout" (finds artists early) vs "Loyalist" (sticks with the familiar); Top Albums; a fan leaderboard (your rank among an artist's listeners); an archive of your five most memorable listening days; story pacing, one fact per screen, a closing share card |
| **Letterboxd Year in Review** | Hours, most-watched actor/director, highest-rated, **most-watched week and weekday**, **first and last film of the year**; it lives as a stats page you can open at any time, per year |
| **Steam Replay 2025** | Top titles by playtime, **longest session**, monthly breakdown, genre mix, **share of time on this year's releases vs 1–7 vs 8+ years old**, comparison with the average player |
| **Wrapperr** (self-hosted, Plex + Tautulli) | The direct self-hosted competitor: custom timeframes, Plex login, movies/shows/music, expiring share links, a "viewing age" estimate. Purely statistical — no curator, no opinions, no deletions |
| **Apple Music Replay** (from memory, not re-checked) | Updates **all year**, not only in December — the recap as a living page |

### 1.5 Top 5 lessons we bring into Curatarr

1. **An identity, not an inventory.** The headline is a role you *earned*
   ("Loyalist", "Scout"), computed from behaviour in code and only *named*
   by the LLM — never flattery the model made up. → `traits[]` → `archetype_title`.
2. **Era as a number.** Listening Age / viewing age / Steam's release-age split
   are the cheapest high-delight stat in the genre. → *Viewing Age* tile +
   fresh / recent / catalogue split.
3. **Moments beat totals.** Letterboxd's busiest week, first & last film;
   Spotify's memorable days; Steam's longest session. → *Biggest Day*,
   *Deepest Sitting*, *First & Last*, *48-Hour Binge*.
4. **A yardstick makes a number mean something.** Steam's "vs the average",
   Spotify's fan rank. One household is too small for population maths, so
   our yardsticks are **the community score** (Contrarian Gem, Unpopular
   Dismissal) and **your own past** (Taste Drift, genre risers).
5. **Always open, every horizon, one closing card.** Letterboxd's per-year
   page + Apple's living Replay + Spotify's share card → period selector
   (30 d / 6 m / year / all time), instant cached replay, a snapshot card.

And the one thing nobody else can do: **the narrator has opinions.**
Wrapperr has no curator; Spotify has no deletion debates. Curatarr's wrap-up is
told by the curator that argued with you all year — The Graveyard, The
Shield and Roast & Toast are its signature.

---

## 2. Wrap-Up Architecture & Feature Table

```
┌──────────── GET /api/wrapup?scope&period ─────────────┐   instant
│ build_wrapup_facts()  — SQL + verified-data cache     │   (facts cached
│   tempo · outliers · eras · curatarr angle · traits   │    in `wrapups`)
│ + cached story (if any) + stale flag                  │
└───────────────────────────────────────────────────────┘
┌──────────── POST /api/wrapup/generate ────────────────┐   background task
│ 1 facts  2 taste drift (Chroma thread)  3 artwork      │   phases → ticker
│ 4 curator writes (Ollama `format` = WrapupStory)       │   (GET …/status)
│ 5 validate: schema → refs → number guard → cliché guard│
│   one repair turn, else deterministic fallback story   │
│ 6 store story_json keyed by facts_hash                 │
└───────────────────────────────────────────────────────┘
```

| Layer | Current State | New "Gold Standard" Design | Concrete Signals / Mechanics Used |
|---|---|---|---|
| **Data collector — Tempo** | Hours = Σ`duration_ms` per month (wrong for partial plays, zero for Spotify and history imports) | `played_ms` per play (Spotify → offset; finished → duration or estimate; partial → offset), `hours_estimated_share` disclosed in the tile | Hours watched/listened, active days, peak hour & weekday (local clock via `viewing_sessions.utc_offset`), daypart shares, **Biggest Day**, longest streak, first & last play |
| **Data collector — Outliers** | none | Per-title roll-up + `build_verified_data` for the top 60 titles | **Comfort Title** (most re-completed / replayed episodes), **Deepest Sitting** (`sittings()`), **48-Hour Binge**, **Contrarian Gem** (swipe ★ or rewatch vs community score, vote mass ≥ 200), **Unpopular Dismissal** (dropped/deleted/low-swiped acclaimed title), completion vs drop rate |
| **Data collector — Eras & Genres** | none | Release years from `media_identities` / verified data, genres from play rows, hour-weighted | **Viewing Age** (weighted median year), oldest vs newest, decade shares, fresh (≤ 12 months) / recent (1–7 y) / catalogue (8 y+) split, genre risers & fallers vs the user's own baseline |
| **Data collector — Discovery** | none | First-ever play per title/artist | New vs returning share → **Scout vs Loyalist** |
| **Data collector — Curatarr Angle** | totals + Stubbornness for the admin | Per user, only tiles that have data | **Graveyard**: GB freed, deletions, **Boldest Deletion** (highest community score deleted), **The One That Came Back** (`returned_at`); **Shield**: overrides + top `override_reason`, manual protections; **Most Debated** thread; **Principles adopted**; **Dust Collector** (largest owned title never played by this user) |
| **Data collector — Vector & Vibe Drift** | none | Chroma vectors of the period's titles vs the user's pre-period baseline, centred on the domain corpus mean | Drift score (1 − cos of centred centroids), 2–3 **taste clusters** with period share vs baseline share (rising / fading), cluster top titles |
| **Data collector — Traits** | none | Deterministic rules with thresholds + evidence fact ids, ranked by strength | night_owl, matinee, completionist, sampler, loyalist, scout, archivist, day_one, binger, highbrow, contrarian, purger, defender |
| **LLM schema** | free paragraph, 400 tokens, no validation | `WrapupStory` (Pydantic), forced via Ollama `format`, temperature 0.75, one repair turn | `archetype_title` / `archetype_subhead` / `archetype_trait` (must be one of the top-3 traits), 2–4 `era_chapters` with `item_refs` + `fact_refs`, `roast_and_toast`, `look_ahead_hook` (must reference an owned-unwatched item), `cluster_names`, `share_line` |
| **LLM guards** | none | Reference check, **number guard** (every digit in the prose must exist in the fact sheet), cliché guard, length caps | Zero-hallucination by construction: titles enter via ids, numbers via copy |
| **Caching** | `app_state` key per user+year, overwritten | Table `wrapups` (user, scope, period_key) → facts_json, facts_hash, story_json, status | Closed periods (`year:2025`) are frozen; rolling ones mark the story *stale* when the facts hash moves; facts re-computed at most every 15 min |
| **Generation** | blocking POST, busy button | Background task (`track_task` + `task_monitor`), polled status, 6-phase ticker; one generation per (user, scope, period) at a time | `aacquire_state_lock("wrapup_gen:{uid}:{scope}:{period}")` |
| **UI — Hero Persona Card** | none | Archetype in display type, subhead, ambient glow from the #1 title's poster (`.card` + `posterLoaded` → `--poster-tint`) | Reveal animation, `haptic('medium')` on reveal, reduced-motion aware |
| **UI — Bento tiles** | five flat `.stat-box` | Asymmetric 6-col grid (2-col on phones); tiles render only when earned | Hours & Rhythm (24-bar clock), Highbrow vs Comfort, Contrarian Gem, Unpopular Dismissal, Binge, Viewing Age, Scout/Loyalist, Graveyard, Shield, Clusters, Dust Collector; `countTo()` on scroll-in |
| **UI — Chapter cards** | none | Each chapter = fanned posters (`_posterImg`) + kicker + commentary + "Discuss this era" (`discussInChat('custom', …)`) | Roast & Toast pair, Look-Ahead card with the actual owned title |
| **UI — Snapshot** | none | 9:16 self-contained card: archetype, 3 numbers, top-5 posters, `share_line`; Web Share API with clipboard fallback | Screenshot-ready on phone and desktop; PNG export is v2 |
| **UI — Period & scope** | fixed 12 months | Period chips (30 d / 6 m / year / all) + scope tabs (only scopes with plays) | Instant: facts come back cached; story shown if one exists for that hash |
| **Curation ledger** | the whole Report | Admin-only collapsible section at the bottom of the owner's wrap-up: month chart, debate split, Stubbornness, redundancy (today's `report.js` renderer, moved) | Nothing is lost |

---

## 3. Complete Drop-In Implementation Plan & Code

### 3.0 Rollout (four PRs, each shippable)

| PR | Content | User-visible result |
|---|---|---|
| **1. Facts + bento** | `wrapup_facts.py`, `wrapups` table, `GET /api/wrapup`, `wrapup.js` without story, Report → Wrap-Up nav for everyone, ledger section for the admin; fix the hours maths | Every member sees their stats, instantly, all periods/scopes |
| **2. Story** | `wrapup_story.py` (schema, prompt, guards, fallback), generate + status routes, ticker, hero/chapters/roast/look-ahead | The curator writes the wrap-up |
| **3. Drift + snapshot** | `taste_drift()` on the Chroma thread, clusters tile, `cluster_names`, snapshot card + share | Taste evolution and the shareable card |
| **4. Seasons** | Custodian pre-generates `30d/all` weekly when the LLM lane is idle, and every user's `year:YYYY` from 1 December; a proactive message "Your 2026 is ready" | Wrapped-season moment |

Tests per PR follow the repo's pattern (`tests/test_curation_stats.py` style:
pure functions fed fixture rows): period resolution, `played_ms`, traits
thresholds, the number guard, reference validation, the deterministic
fallback, and `tests/test_frontend_hygiene.py` must stay green (literal
`act()` names, registry entries, no inline styles).

### 3.1 Storage — `src/database/models.py`

```python
class Wrapup(Base):
    """One wrap-up per (user, scope, period): the deterministic facts it was
    built from and — once the curator wrote it — the validated story.

    ``facts_hash`` is the identity of the numbers: a story written for an
    older hash is shown with a "numbers moved" badge instead of being thrown
    away. Closed periods (a past calendar year) never recompute their facts,
    so their story is final."""
    __tablename__ = "wrapups"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    scope        = Column(String(16), nullable=False)    # all/movie/show/anime/music
    period_key   = Column(String(16), nullable=False)    # 30d / 6m / year:2026 / all
    facts_json   = Column(Text, nullable=True)
    facts_hash   = Column(String(40), nullable=True)
    facts_at     = Column(DateTime, nullable=True)
    story_json   = Column(Text, nullable=True)
    story_hash   = Column(String(40), nullable=True)     # facts_hash the story was written for
    story_at     = Column(DateTime, nullable=True)
    story_model  = Column(String(128), nullable=True)
    story_source = Column(String(16), nullable=True)     # llm / llm_repaired / fallback
    error        = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "scope", "period_key", name="uq_wrapup_user_scope_period"),
    )
```

`create_all` builds it on existing installs (new table, no column migration).
The old `app_state` key `curation_narrative:*` is left to expire; nothing
reads it after PR 1.

### 3.2 `build_wrapup_facts()` — `src/services/wrapup_facts.py`

```python
"""
Curatarr — wrap-up facts: everything the wrap-up says, computed before any
model sees it.

The narrative is only as good as the numbers under it, and a number the
model computes is a number the model can get wrong. So every superlative,
contrast and outlier is decided HERE, in SQL and plain Python over the
user's own rows, and the curator only gets to name and phrase them
(services/wrapup_story). Blocking — run it with ``run_read``.

Hours are PLAYED time, not item length: an abandoned ten-minute start of a
three-hour film is ten minutes. Spotify rows carry the played time in
view_offset_ms (duration_ms is NULL there), and the Plex history import
writes finished rows with no duration at all — those get a per-type
estimate, and the share of estimated time travels with the figure so the
tile can say "≈".
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

SCOPES = ("all", "movie", "show", "anime", "music")
VIDEO = ("movie", "show", "anime")
PERIODS = ("30d", "6m", "year", "all")

# Assumed length of a finished play that carries no duration (history import)
_EST_MIN = {"movie": 105, "show": 42, "anime": 24, "music": 3.5}
_MIN_VOTES = 200            # a community score below this mass is noise
_ACCLAIMED = 7.8            # /10 — "acclaimed" for the highbrow and dismissal tiles
_ACCLAIMED_VOTES = 1000
_CONTRARIAN_COMMUNITY_MAX = 6.8
_CONTRARIAN_USER_MIN = 8.0
_TOP_ENRICH = 60            # titles whose verified data is read (cache only)
_FRESH_DAYS = 365


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime
    end: datetime
    label: str
    closed: bool            # a past calendar year: its facts never change


def resolve_period(key: str, now: Optional[datetime] = None) -> Period:
    now = now or datetime.utcnow()
    if key == "30d":
        return Period(key, now - timedelta(days=30), now, "Last 30 days", False)
    if key == "6m":
        return Period(key, now - timedelta(days=182), now, "Last 6 months", False)
    if key == "all":
        return Period(key, datetime(1970, 1, 1), now, "All time", False)
    m = re.fullmatch(r"year:(\d{4})", key or "")
    if m and 2000 <= int(m.group(1)) <= now.year:
        y = int(m.group(1))
        end = datetime(y + 1, 1, 1)
        return Period(key, datetime(y, 1, 1), min(end, now), f"{y} in review", end <= now)
    raise ValueError(f"unknown period {key!r}")


def _scope_types(scope: str) -> tuple:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    return VIDEO + ("music",) if scope == "all" else (scope,)


# ── played time ─────────────────────────────────────────────────────────────

def played_ms(row: dict) -> tuple[int, bool]:
    """(played milliseconds, estimated?) for one play row — the Python twin of
    _played_ms_sql, used where rows are already in memory."""
    if row.get("source") == "spotify":
        return int(row.get("view_offset_ms") or 0), False
    if row.get("completed"):
        if row.get("duration_ms"):
            return int(row["duration_ms"]), False
        return int(_EST_MIN.get(row.get("media_type"), 45) * 60_000), True
    return int(row.get("view_offset_ms") or 0), False


def _played_ms_sql(W):
    from sqlalchemy import case, func
    est = case(*[(W.media_type == k, int(v * 60_000)) for k, v in _EST_MIN.items()],
               else_=45 * 60_000)
    return case(
        (W.source == "spotify", func.coalesce(W.view_offset_ms, 0)),
        (W.completed == True, func.coalesce(W.duration_ms, est)),   # noqa: E712
        else_=func.coalesce(W.view_offset_ms, 0))


def _estimated_sql(W):
    from sqlalchemy import and_, case, func
    return case((and_(W.completed == True, W.duration_ms.is_(None),       # noqa: E712
                      func.coalesce(W.source, "") != "spotify"), 1), else_=0)


def _h(ms) -> float:
    return round((ms or 0) / 3_600_000, 1)


# ── the collector ───────────────────────────────────────────────────────────

def build_wrapup_facts(user_id: int, scope: str, period_key: str,
                       now: Optional[datetime] = None, *,
                       is_admin: bool = False) -> dict:
    """The complete, deterministic fact set for one wrap-up. Every section is
    optional: a section with nothing real to say is left out, never zeroed —
    the UI renders only earned tiles and the prompt only sees real facts."""
    from sqlalchemy import func

    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    from src.services.viewing_sessions import utc_offset

    now = now or datetime.utcnow()
    period = resolve_period(period_key, now)
    types = _scope_types(scope)
    ms = _played_ms_sql(W)
    base = (W.user_id == user_id, W.viewed_at >= period.start,
            W.viewed_at < period.end, W.media_type.in_(types))
    off = f"{int(utc_offset().total_seconds() // 60):+d} minutes"
    local = func.datetime(W.viewed_at, off)

    out: dict = {"scope": scope, "period": {
        "key": period.key, "label": period.label, "closed": period.closed,
        "start": period.start.isoformat(), "end": period.end.isoformat()}}

    with get_db_session() as db:
        tot = db.query(func.count(), func.sum(ms), func.sum(_estimated_sql(W) * ms),
                       func.min(W.viewed_at), func.max(W.viewed_at)).filter(*base).one()
        plays, total_ms, est_ms, first_at, last_at = tot
        if not plays:
            out["empty"] = True
            return _finish(out)

        out["tempo"] = _tempo(db, W, ms, base, local, total_ms, est_ms, plays)
        out["tempo"]["by_type"] = {
            t: _h(v) for t, v in db.query(W.media_type, func.sum(ms))
            .filter(*base).group_by(W.media_type).all() if v}
        out["first_last"] = _first_last(db, W, base)

        titles = _title_rollup(db, W, ms, base)
        out["top"] = titles[:5]
        out["discovery"] = _discovery(db, W, user_id, period, types, titles)

        video_types = [t for t in types if t in VIDEO]
        if video_types:
            vrows = _video_rows(db, W, user_id, period, video_types)
            prior = _prior_completions(db, W, user_id, period, vrows)
            out["completion"] = _completion(vrows, now)
            out["comfort"] = _comfort(vrows, prior)
            out["binge"] = _binge(vrows, period)
            enriched = _enrich(db, titles, user_id)
            out["eras"] = _eras(titles, enriched, period)
            out["highbrow_comfort"] = _highbrow_split(titles, enriched, out["comfort"])
            out["contrarian"] = _contrarian(db, user_id, titles, enriched, out["comfort"])
            out["dismissal"] = _dismissal(db, user_id, period, vrows, enriched, now)
        if "music" in types:
            out["music"] = _music(db, W, ms, user_id, period, base)

        out["genres"] = _genre_drift(db, W, ms, user_id, period, types)
        out["curatarr"] = _curatarr_angle(db, user_id, period, types, is_admin)
        out["look_ahead"] = _look_ahead(db, user_id, types)

    out["traits"] = derive_traits(out, now)
    return _finish(out)


def _finish(out: dict) -> dict:
    core = {k: v for k, v in out.items() if k not in ("generated_at",)}
    out["facts_hash"] = hashlib.sha1(
        json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()
    out["generated_at"] = datetime.utcnow().isoformat()
    return out


# ── tempo & rhythm ──────────────────────────────────────────────────────────

_DAYPARTS = (("night", range(0, 5)), ("morning", range(5, 11)),
             ("afternoon", range(11, 17)), ("evening", range(17, 23)), ("night", range(23, 24)))


def _tempo(db, W, ms, base, local, total_ms, est_ms, plays) -> dict:
    from sqlalchemy import func
    hr = func.strftime("%H", local).label("h")
    by_hour = [0] * 24
    for h, v in db.query(hr, func.sum(ms)).filter(*base).group_by(hr).all():
        by_hour[int(h)] = int(v or 0)
    dw = func.strftime("%w", local).label("w")           # 0 = Sunday
    by_dow = [0] * 7
    for d, v in db.query(dw, func.sum(ms)).filter(*base).group_by(dw).all():
        by_dow[int(d)] = int(v or 0)
    day = func.date(local).label("d")
    days = db.query(day, func.sum(ms), func.count()).filter(*base).group_by(day).all()

    parts = Counter()
    for name, hours in _DAYPARTS:
        parts[name] += sum(by_hour[h] for h in hours)
    total = max(1, sum(by_hour))
    weekend = by_dow[0] + by_dow[6]
    weekend_day = db.query(func.sum(ms)).filter(
        *base, func.strftime("%w", local).in_(("0", "6")),
        func.strftime("%H", local).between("11", "17")).scalar() or 0
    big = max(days, key=lambda r: r[1] or 0)
    return {
        "hours": _h(total_ms), "plays": int(plays),
        "hours_estimated_share": round((est_ms or 0) / max(1, total_ms or 0), 2),
        "active_days": len(days),
        "longest_streak_days": _streak([d for d, _, _ in days]),
        "peak_hour": max(range(24), key=lambda h: by_hour[h]),
        "peak_weekday": max(range(7), key=lambda d: by_dow[d]),
        "by_hour_h": [_h(v) for v in by_hour],
        "dayparts": {k: round(v / total, 2) for k, v in parts.items()},
        "weekend_share": round(weekend / max(1, sum(by_dow)), 2),
        "weekend_daytime_share": round(weekend_day / total, 2),
        "biggest_day": {"date": big[0], "hours": _h(big[1]), "plays": int(big[2])},
    }


def _streak(dates: list) -> int:
    ds = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates if d)
    best = cur = 1 if ds else 0
    for a, b in zip(ds, ds[1:]):
        cur = cur + 1 if (b - a).days == 1 else 1
        best = max(best, cur)
    return best


def _first_last(db, W, base) -> dict:
    def one(order):
        r = (db.query(W.title, W.series_title, W.media_type, W.viewed_at)
             .filter(*base).order_by(order).first())
        return r and {"title": r.series_title or r.title, "media_type": r.media_type,
                      "at": r.viewed_at.isoformat()}
    return {"first": one(W.viewed_at.asc()), "last": one(W.viewed_at.desc())}


# ── titles ──────────────────────────────────────────────────────────────────

def _title_rollup(db, W, ms, base) -> list:
    """Hour-ranked titles (a series, a film, an artist), top 80."""
    from sqlalchemy import func
    key = func.coalesce(W.series_title, W.title).label("k")
    rows = (db.query(key, W.media_type, func.sum(ms), func.count(),
                     func.max(W.tmdb_id), func.max(W.plex_item_id),
                     func.min(W.viewed_at), func.max(W.viewed_at))
            .filter(*base).group_by(key, W.media_type)
            .order_by(func.sum(ms).desc()).limit(80).all())
    return [{"title": k, "media_type": mt, "hours": _h(v), "plays": int(n),
             "tmdb_id": tmdb, "plex_item_id": pk,
             "first": a.isoformat() if a else None, "last": b.isoformat() if b else None}
            for k, mt, v, n, tmdb, pk, a, b in rows if k]


def _discovery(db, W, user_id, period, types, titles) -> dict:
    """Scout vs Loyalist: the share of this period's time that went to titles
    (artists, for music) the user had never played before the period began."""
    from sqlalchemy import func
    if period.key == "all" or not titles:
        return {}
    keys = [t["title"] for t in titles]
    key = func.coalesce(W.series_title, W.title)
    known = {k for (k,) in db.query(key).filter(
        W.user_id == user_id, W.viewed_at < period.start,
        W.media_type.in_(types), key.in_(keys)).distinct()}
    total = sum(t["hours"] for t in titles) or 1
    new = [t for t in titles if t["title"] not in known]
    return {"new_share": round(sum(t["hours"] for t in new) / total, 2),
            "new_titles": len(new), "returning_titles": len(titles) - len(new),
            "best_new": new[0]["title"] if new else None}


def _video_rows(db, W, user_id, period, video_types) -> list:
    cols = (W.plex_item_id, W.title, W.series_title, W.media_type, W.season,
            W.episode, W.viewed_at, W.duration_ms, W.view_offset_ms, W.completed,
            W.tmdb_id, W.source)
    names = [c.key for c in cols]
    q = (db.query(*cols).filter(W.user_id == user_id, W.viewed_at >= period.start,
                                W.viewed_at < period.end, W.media_type.in_(video_types))
         .order_by(W.viewed_at.asc()))
    return [dict(zip(names, r)) for r in q]


def _prior_completions(db, W, user_id, period, vrows) -> set:
    """(title-key, season, episode) completed BEFORE the period — for comfort
    rewatches. Movies key on (title, None, None)."""
    series = {r["series_title"] or r["title"] for r in vrows}
    if not series or period.key == "all":
        return set()
    q = db.query(W.series_title, W.title, W.season, W.episode).filter(
        W.user_id == user_id, W.viewed_at < period.start, W.completed == True,   # noqa: E712
        (W.series_title.in_(series)) | (W.title.in_(series)))
    return {(s or t, se, ep) for s, t, se, ep in q}


# ── completion, comfort, binge ──────────────────────────────────────────────

def _completion(vrows, now) -> dict:
    from src.services.taste_engine import _is_drop
    from src.services.viewing_sessions import _watched_enough
    if not vrows:
        return {}
    done = sum(1 for r in vrows if _watched_enough(r))
    drops = [r for r in vrows if _is_drop(r, now)]
    dropped_titles = Counter(r["series_title"] or r["title"] for r in drops)
    return {"completion_rate": round(done / len(vrows), 2),
            "drop_rate": round(len(drops) / len(vrows), 2),
            "dropped_titles": [t for t, _ in dropped_titles.most_common(5)]}


def _comfort(vrows, prior) -> dict:
    """The comfort title: most re-completions inside the period plus
    completions of things already finished before it."""
    reps = Counter()
    seen = Counter()
    for r in vrows:
        if not r["completed"]:
            continue
        k = (r["series_title"] or r["title"], r["season"], r["episode"])
        seen[k] += 1
        if seen[k] > 1 or k in prior:
            reps[k[0]] += 1
    if not reps:
        return {}
    title, n = reps.most_common(1)[0]
    total_done = sum(seen.values()) or 1
    return {"title": title, "replays": n,
            "comfort_share": round(sum(reps.values()) / total_done, 2),
            "comfort_titles": [t for t, _ in reps.most_common(8)]}


def _binge(vrows, period) -> dict:
    """Deepest sitting (viewing_sessions.sittings — phantom bulk-marks excluded)
    and the most episodes of one series inside any 48 hours."""
    from src.services.viewing_sessions import PHANTOM_GAP_MIN, _watched_enough, sittings
    by_series = defaultdict(list)
    for r in vrows:
        if r["series_title"]:
            by_series[r["series_title"]].append(r)
    best_sit, best_48 = None, None
    span = max(1, (period.end - period.start).days + 1)
    for s, rows in by_series.items():
        for sit in sittings(rows, now=period.end, window_days=span):
            if not best_sit or sit["episodes"] > best_sit["episodes"]:
                best_sit = {"series": s, "episodes": sit["episodes"],
                            "span_min": sit["span_min"], "date": sit["start"].date().isoformat()}
        eps, prev = [], None
        for r in rows:
            real = prev is None or (r["viewed_at"] - prev).total_seconds() >= PHANTOM_GAP_MIN * 60
            if real and _watched_enough(r):
                eps.append(r["viewed_at"])
            prev = r["viewed_at"]
        lo = 0
        for hi, t in enumerate(eps):
            while t - eps[lo] > timedelta(hours=48):
                lo += 1
            n = hi - lo + 1
            if not best_48 or n > best_48["episodes"]:
                best_48 = {"series": s, "episodes": n, "ended": t.date().isoformat()}
    out = {}
    if best_sit and best_sit["episodes"] >= 3:
        out["deepest_sitting"] = best_sit
    if best_48 and best_48["episodes"] >= 4:
        out["in_48h"] = best_48
    return out


# ── verified data (cache only) ──────────────────────────────────────────────

def _enrich(db, titles, user_id) -> dict:
    """{title: verified subset} for the top video titles — build_verified_data
    reads the enrichment cache only (no LLM, no network). Series resolve their
    ids through media_identities by title."""
    from sqlalchemy import func
    from src.cache.metadata_cache import MetadataCache
    from src.database.models import MediaIdentity as MI
    from src.services.media_enricher import build_verified_data
    from src.services.rating_value import count_number, rating_number

    vids = [t for t in titles if t["media_type"] in VIDEO][:_TOP_ENRICH]
    if not vids:
        return {}
    idents = {}
    for mi in db.query(MI).filter(func.lower(MI.title).in_(
            [t["title"].lower() for t in vids])):
        idents.setdefault((mi.title.lower(), mi.media_type), mi)
    out = {}
    with MetadataCache() as cache:
        for t in vids:
            mi = idents.get((t["title"].lower(), t["media_type"]))
            vd = build_verified_data(
                t["title"], t["media_type"],
                tmdb_id=(mi and mi.tmdb_id) or t.get("tmdb_id"),
                tvdb_id=mi and mi.tvdb_id, anilist_id=mi and mi.anilist_id,
                anidb_id=mi and mi.anidb_id,
                plex_rating_key=(mi and mi.plex_rating_key) or t.get("plex_item_id"),
                cache=cache) or {}
            votes = max(count_number(vd.get("vote_count")), count_number(vd.get("scored_by")))
            out[t["title"]] = {
                "year": vd.get("year") or (mi and mi.year),
                "rating": rating_number(vd.get("rating")),
                "votes": votes,
                "metacritic": rating_number(vd.get("metacritic")),
                "significant": bool(vd.get("significance")),
                "genres": (vd.get("genres") or [])[:3],
                "tmdb_id": (mi and mi.tmdb_id) or t.get("tmdb_id"),
                "tvdb_id": mi and mi.tvdb_id,
            }
    return out


def _eras(titles, enriched, period) -> dict:
    """Viewing Age (hour-weighted median release year), oldest vs newest, decade
    shares, and Steam's fresh / recent / catalogue split."""
    pts = [(int(enriched[t["title"]]["year"]), t["hours"], t["title"])
           for t in titles if enriched.get(t["title"], {}).get("year")]
    if len(pts) < 3:
        return {}
    pts.sort()
    half, acc, median = sum(h for _, h, _ in pts) / 2, 0.0, pts[0][0]
    for y, h, _ in pts:
        acc += h
        if acc >= half:
            median = y
            break
    ref = period.end.year
    total = sum(h for _, h, _ in pts) or 1
    dec = Counter()
    split = Counter()
    for y, h, _ in pts:
        dec[f"{y // 10 * 10}s"] += h
        age = ref - y
        split["fresh" if age <= 1 else "recent" if age <= 7 else "catalogue"] += h
    return {"viewing_age_year": median,
            "oldest": {"title": pts[0][2], "year": pts[0][0]},
            "newest": {"title": pts[-1][2], "year": pts[-1][0]},
            "decades": {k: round(v / total, 2) for k, v in sorted(dec.items())},
            "split": {k: round(split[k] / total, 2) for k in ("fresh", "recent", "catalogue")}}


def _acclaimed(e: dict) -> bool:
    return bool(e) and ((e.get("rating") or 0) >= _ACCLAIMED and (e.get("votes") or 0) >= _ACCLAIMED_VOTES
                        or (e.get("metacritic") or 0) >= 75 or e.get("significant"))


def _highbrow_split(titles, enriched, comfort) -> dict:
    comfort_set = set((comfort or {}).get("comfort_titles") or [])
    hb = cf = other = 0.0
    for t in titles:
        if t["media_type"] not in VIDEO:
            continue
        if t["title"] in comfort_set:
            cf += t["hours"]
        elif _acclaimed(enriched.get(t["title"])):
            hb += t["hours"]
        else:
            other += t["hours"]
    total = hb + cf + other
    if total < 2:
        return {}
    top_hb = next((t["title"] for t in titles if t["title"] not in comfort_set
                   and _acclaimed(enriched.get(t["title"]))), None)
    return {"highbrow": round(hb / total, 2), "comfort": round(cf / total, 2),
            "other": round(other / total, 2), "top_highbrow": top_hb,
            "top_comfort": next(iter(comfort_set), None)}


def _swipe_stars(db, user_id) -> dict:
    from src.database.models import SwipeRating
    return {(r.title or "").lower(): r.stars for r in db.query(SwipeRating).filter(
        SwipeRating.user_id == user_id, SwipeRating.seen == True,      # noqa: E712
        SwipeRating.stars.isnot(None))}


def _contrarian(db, user_id, titles, enriched, comfort) -> dict:
    """Biggest gap between the user's own verdict and the community's: swipe
    stars where the user gave them, a comfort rewatch counts as an 8.5."""
    stars = _swipe_stars(db, user_id)
    comfort_set = set((comfort or {}).get("comfort_titles") or [])
    best = None
    for t in titles:
        e = enriched.get(t["title"]) or {}
        com, votes = e.get("rating"), e.get("votes") or 0
        if com is None or votes < _MIN_VOTES or com > _CONTRARIAN_COMMUNITY_MAX:
            continue
        mine, basis = stars.get(t["title"].lower()), "rated"
        if mine is None and t["title"] in comfort_set:
            mine, basis = 8.5, "rewatched"
        if mine is None or mine < _CONTRARIAN_USER_MIN:
            continue
        gap = round(mine - com, 1)
        if not best or gap > best["gap"]:
            best = {"title": t["title"], "media_type": t["media_type"], "you": mine,
                    "community": round(com, 1), "votes": votes, "gap": gap, "basis": basis}
    return best or {}


def _dismissal(db, user_id, period, vrows, enriched, now) -> dict:
    """The acclaimed title the user walked away from: dropped, deleted, or
    swiped 4★ or less — the one with the highest community score wins."""
    from src.database.models import DeletionProposal as DP
    from src.services.taste_engine import _is_drop
    cands = {}
    for r in vrows:
        if _is_drop(r, now):
            cands.setdefault(r["series_title"] or r["title"], "dropped")
    for (t,) in db.query(DP.title).filter(DP.user_id == user_id, DP.status == "deleted",
                                           DP.resolved_at >= period.start,
                                           DP.resolved_at < period.end):
        cands.setdefault(t, "deleted")
    for t, s in _swipe_stars(db, user_id).items():
        if s <= 4:
            cands.setdefault(t, "rated low")
    best = None
    for title, how in cands.items():
        e = enriched.get(title) or next(
            (v for k, v in enriched.items() if k.lower() == title.lower()), None) or {}
        if (e.get("rating") or 0) < _ACCLAIMED or (e.get("votes") or 0) < _ACCLAIMED_VOTES:
            continue
        if not best or e["rating"] > best["community"]:
            best = {"title": title, "how": how, "community": round(e["rating"], 1),
                    "votes": e["votes"], "year": e.get("year")}
    return best or {}


# ── music ───────────────────────────────────────────────────────────────────

def _music(db, W, ms, user_id, period, base) -> dict:
    """Top artists and tracks, the track on repeat (most plays in one day),
    the skip rate, and the user's best-rated artist (PlexAmp stars)."""
    from sqlalchemy import func
    from src.database.models import PlexRating
    mb = (*base[:3], W.media_type == "music")
    artists = (db.query(W.series_title, func.sum(ms), func.count())
               .filter(*mb, W.series_title.isnot(None))
               .group_by(W.series_title).order_by(func.sum(ms).desc()).limit(5).all())
    tracks = (db.query(W.title, W.series_title, func.count())
              .filter(*mb).group_by(W.title, W.series_title)
              .order_by(func.count().desc()).limit(5).all())
    day = func.date(W.viewed_at)
    rep = (db.query(W.title, W.series_title, day, func.count())
           .filter(*mb).group_by(W.title, W.series_title, day)
           .order_by(func.count().desc()).first())
    n, done = db.query(func.count(), func.sum(W.completed)).filter(*mb).one()
    fav = (db.query(PlexRating.artist_name, func.max(PlexRating.rating))
           .filter(PlexRating.user_id == user_id, PlexRating.artist_name.isnot(None))
           .group_by(PlexRating.artist_name).order_by(func.max(PlexRating.rating).desc())
           .first())
    if not n:
        return {}
    return {
        "top_artists": [{"artist": a, "hours": _h(v), "plays": int(c)} for a, v, c in artists],
        "top_tracks": [{"track": t, "artist": a, "plays": int(c)} for t, a, c in tracks],
        "on_repeat": rep and rep[3] >= 5 and {"track": rep[0], "artist": rep[1],
                                              "date": rep[2], "plays": int(rep[3])} or None,
        "skip_rate": round(1 - (done or 0) / n, 2),
        "best_rated_artist": fav and fav[0],
    }


# ── genres vs baseline ──────────────────────────────────────────────────────

def _genre_shares(db, W, ms, filters) -> dict:
    from sqlalchemy import func
    acc = Counter()
    for g, v in db.query(W.genres, func.sum(ms)).filter(*filters, W.genres.isnot(None)) \
            .group_by(W.genres).all():
        parts = [p.strip() for p in (g or "").split(",") if p.strip()]
        for p in parts:
            acc[p] += (v or 0) / len(parts)
    total = sum(acc.values()) or 1
    return {k: v / total for k, v in acc.items()}


def _genre_drift(db, W, ms, user_id, period, types) -> dict:
    now_s = _genre_shares(db, W, ms, (W.user_id == user_id, W.viewed_at >= period.start,
                                      W.viewed_at < period.end, W.media_type.in_(types)))
    if not now_s:
        return {}
    top = sorted(now_s.items(), key=lambda kv: -kv[1])[:5]
    out = {"top": [{"genre": g, "share": round(s, 2)} for g, s in top]}
    if period.key != "all":
        was = _genre_shares(db, W, ms, (W.user_id == user_id, W.viewed_at < period.start,
                                        W.media_type.in_(types)))
        if was:
            delta = {g: now_s.get(g, 0) - was.get(g, 0) for g in set(now_s) | set(was)}
            ranked = sorted(delta.items(), key=lambda kv: kv[1])
            out["rising"] = [{"genre": g, "pp": round(d * 100)} for g, d in ranked[::-1][:2] if d >= 0.05]
            out["fading"] = [{"genre": g, "pp": round(d * 100)} for g, d in ranked[:2] if d <= -0.05]
    return out


# ── the Curatarr angle ──────────────────────────────────────────────────────

def _curatarr_angle(db, user_id, period, types, is_admin) -> dict:
    from sqlalchemy import func
    from src.database.models import (ConversationMessage as CM, CuratorPrinciple as CP,
                                     CuratorResolutionLog as CRL, DeletionProposal as DP,
                                     ProtectedMedia as PM)
    inp = lambda col: (col >= period.start, col < period.end)      # noqa: E731
    out: dict = {}

    dels = (db.query(DP).filter(DP.user_id == user_id, DP.status == "deleted",
                                DP.category.in_(types), *inp(DP.resolved_at)).all())
    if dels:
        biggest = max(dels, key=lambda d: d.storage_mb or 0)
        out["graveyard"] = {
            "deleted": len(dels),
            "gb_freed": round(sum(d.storage_mb or 0 for d in dels) / 1024, 1),
            "biggest": {"title": biggest.title, "gb": round((biggest.storage_mb or 0) / 1024, 1),
                        "poster": biggest.poster_url},
        }
    came_back = (db.query(DP).filter(DP.user_id == user_id, DP.returned_at.isnot(None),
                                     *inp(DP.returned_at)).first())
    if came_back:
        out["came_back"] = {"title": came_back.title, "poster": came_back.poster_url}

    ov = (db.query(CRL).filter(CRL.user_id == user_id, CRL.outcome == "kept",
                               CRL.resolution_type == "override", CRL.category.in_(types),
                               *inp(CRL.created_at)).all())
    manual = (db.query(func.count(PM.id)).filter(PM.user_id == user_id, PM.source == "manual",
                                                 *inp(PM.created_at)).scalar() or 0)
    if ov or manual:
        reasons = Counter(r.override_reason for r in ov if r.override_reason)
        fiercest = ov[0] if ov else None
        out["shield"] = {
            "overrides": len(ov), "manual_protections": int(manual),
            "top_reason": reasons.most_common(1)[0][0] if reasons else None,
            "fiercest": fiercest and {"title": fiercest.title,
                                      "curator_stance": (fiercest.curator_stance or "")[:140]},
        }

    deb = (db.query(CM.thread_id, func.count()).filter(
        CM.user_id == user_id, CM.role == "user",
        CM.thread_id.like("deletion_proposal:%"), *inp(CM.created_at))
        .group_by(CM.thread_id).order_by(func.count().desc()).first())
    if deb and deb[1] >= 3:
        dp = db.get(DP, int(deb[0].split(":", 1)[1]))
        if dp:
            out["most_debated"] = {"title": dp.title, "messages": int(deb[1]),
                                   "outcome": dp.status, "poster": dp.poster_url}

    principles = (db.query(CP.text).filter(CP.user_id == user_id, CP.status == "active",
                                           *inp(CP.activated_at))
                  .order_by(CP.activated_at.desc()).limit(3).all())
    if principles:
        out["principles"] = [p for (p,) in principles]

    out["dust_collector"] = _dust_collector(db, user_id, types)
    if is_admin:
        out["ledger"] = True        # the UI fetches the old report data for this section
    return {k: v for k, v in out.items() if v}


def _dust_collector(db, user_id, types) -> Optional[dict]:
    """The largest owned title this user has never played."""
    from sqlalchemy import func
    from src.database.models import MediaTechProfile as MTP, WatchHistoryEntry as W
    big = (db.query(MTP.title, MTP.media_type, MTP.size_mb, MTP.tmdb_id)
           .filter(MTP.media_type.in_(types), MTP.title.isnot(None))
           .order_by(MTP.size_mb.desc()).limit(60).all())
    if not big:
        return None
    titles = [b.title for b in big]
    played = {t.lower() for (t,) in db.query(func.coalesce(W.series_title, W.title))
              .filter(W.user_id == user_id,
                      (W.series_title.in_(titles)) | (W.title.in_(titles))).distinct()}
    for b in big:
        if b.title.lower() not in played:
            return {"title": b.title, "media_type": b.media_type,
                    "gb": round((b.size_mb or 0) / 1024, 1), "tmdb_id": b.tmdb_id}
    return None


def _look_ahead(db, user_id, types) -> list:
    """Owned-but-unwatched picks from the recommendation cache's library lane —
    the only items the look-ahead hook may recommend."""
    from src.database.models import CachedRecommendation as CR
    rows = (db.query(CR).filter(CR.user_id == user_id, CR.lane == "library",
                                CR.category.in_(types))
            .order_by(CR.confidence.desc()).limit(5).all())
    return [{"title": r.title, "category": r.category, "year": r.year,
             "reason": (r.reason or "")[:160], "poster": r.poster_url} for r in rows]


# ── traits: the archetype is decided here, only named by the model ─────────

def derive_traits(f: dict, now: datetime) -> list:
    """[{key, strength 0..1, evidence}] past their threshold, strongest first.
    ``strength`` = how far past the threshold, so the strongest trait is the
    one the user most clearly earned."""
    t = f.get("tempo") or {}
    out = []

    def add(key, value, threshold, ceiling, evidence):
        if value is None or value < threshold:
            return
        out.append({"key": key, "evidence": evidence,
                    "strength": round(min(1.0, (value - threshold) / max(1e-6, ceiling - threshold)), 2)})

    dp = t.get("dayparts") or {}
    add("night_owl", dp.get("night"), 0.30, 0.70, f"{round(100 * dp.get('night', 0))}% after 23:00")
    add("matinee", t.get("weekend_daytime_share"), 0.35, 0.70, "weekend daytime")
    c = f.get("completion") or {}
    if (t.get("plays") or 0) >= 20:
        add("completionist", c.get("completion_rate"), 0.90, 1.0, "finishes what it starts")
        add("sampler", c.get("drop_rate"), 0.25, 0.60, "drops early")
    d = f.get("discovery") or {}
    if d:
        add("scout", d.get("new_share"), 0.60, 0.95, "new titles")
        add("loyalist", 1 - d["new_share"] if "new_share" in d else None, 0.75, 1.0, "returning titles")
    e = f.get("eras") or {}
    if e:
        add("archivist", now.year - e["viewing_age_year"], 25, 50, f"viewing age {e['viewing_age_year']}")
        add("day_one", e["split"].get("fresh"), 0.50, 0.90, "this year's releases")
    b = (f.get("binge") or {}).get("in_48h") or {}
    add("binger", b.get("episodes"), 10, 30, b and f"{b['episodes']} episodes in 48 h")
    hc = f.get("highbrow_comfort") or {}
    add("highbrow", hc.get("highbrow"), 0.50, 0.85, "acclaimed picks")
    add("comfort_seeker", hc.get("comfort"), 0.35, 0.70, "rewatches")
    add("contrarian", (f.get("contrarian") or {}).get("gap"), 2.5, 5.0, "loves what others don't")
    cu = f.get("curatarr") or {}
    add("purger", (cu.get("graveyard") or {}).get("gb_freed"), 200, 2000, "GB freed")
    add("defender", (cu.get("shield") or {}).get("overrides"), 3, 15, "overrode the curator")
    out.sort(key=lambda x: -x["strength"])
    return out[:5]
```

**Taste drift** — `src/services/wrapup_facts.py`, async, PR 3:

```python
async def taste_drift(user_id: int, scope: str, period_key: str) -> dict:
    """How this period's watching moved against the user's own past, in the
    embedding space the taste engine already uses: the centroid of the period's
    item vectors vs the centroid of everything before it, both centred on the
    domain corpus mean (raw text embeddings share one cone — uncentred, two
    different tastes still read ~0.94). Plus up to three clusters of the
    period with their share now vs before. Seconds of Chroma reads and NumPy:
    on the Chroma thread, never on the loop."""
    if scope == "all" or period_key == "all":
        return {}            # the corpus mean is per domain; all-time has no "before"
    from src.database.runner import run_read
    from src.vector_store.chromadb_wrapper import run_chroma

    period = resolve_period(period_key)
    now_rows, past_rows = await run_read(_drift_rows, user_id, scope, period)
    if len(now_rows) < 12 or len(past_rows) < 24:
        return {}
    return await run_chroma(lambda: _drift_sync(scope, now_rows, past_rows))


def _drift_rows(user_id, scope, period):
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    ms = _played_ms_sql(W)

    def top(filters, n):
        key = func.coalesce(W.series_title, W.title)
        with get_db_session() as db:
            q = (db.query(key, W.series_title, func.max(W.plex_item_id), func.sum(ms))
                 .filter(W.user_id == user_id, W.media_type == scope, *filters)
                 .group_by(key).order_by(func.sum(ms).desc()).limit(n))
            return [{"title": k, "series_title": s, "plex_item_id": pk,
                     "media_type": scope, "w": float(v or 0)} for k, s, pk, v in q]
    return (top((W.viewed_at >= period.start, W.viewed_at < period.end), 200),
            top((W.viewed_at < period.start,), 400))


def _drift_sync(category, now_rows, past_rows) -> dict:
    import numpy as np
    from src.services.taste_engine import (_chroma_item_vector, _cluster_centroids,
                                           _domain_corpus_mean)

    def vecs(rows):
        out = []
        for r in rows:
            v = _chroma_item_vector(r, None)
            if v:
                out.append((np.asarray(v, dtype=float), r["w"], r["title"]))
        return out

    now_v, past_v = vecs(now_rows), vecs(past_rows)
    if len(now_v) < 12 or len(past_v) < 24:
        return {}
    mean = _domain_corpus_mean(category)
    mean = np.asarray(mean, dtype=float) if mean is not None else 0.0

    def centroid(vs):
        m = np.stack([v for v, _, _ in vs]) - mean
        w = np.asarray([w for _, w, _ in vs])
        c = (m * w[:, None]).sum(axis=0)
        return c / (np.linalg.norm(c) or 1.0)

    drift = round(float(1 - centroid(now_v) @ centroid(past_v)), 3)
    clusters = _cluster_centroids([(list(v), w, t) for v, w, t in now_v],
                                  max_k=3, corpus_mean=mean if np.ndim(mean) else None) or []
    out_clusters = []
    if clusters:
        cents = np.stack([np.asarray(c["embedding"]) for c in clusters])
        past_m = np.stack([v for v, _, _ in past_v])
        past_w = np.asarray([w for _, w, _ in past_v])
        assign = np.argmax(past_m @ cents.T, axis=1)
        for i, c in enumerate(clusters):
            before = float(past_w[assign == i].sum() / (past_w.sum() or 1))
            out_clusters.append({
                "id": f"C{i + 1}", "share": c["share"], "share_before": round(before, 3),
                "trend": ("rising" if c["share"] - before >= 0.10 else
                          "fading" if before - c["share"] >= 0.10 else "steady"),
                "top_titles": c["top_titles"]})
    # Thresholds are a first guess — calibrate on the household's real
    # month-over-month drift before they are shown as words.
    return {"drift": drift,
            "label": "steady" if drift < 0.15 else "shifting" if drift < 0.35 else "new direction",
            "clusters": out_clusters}
```

### 3.3 The LLM story — `src/services/wrapup_story.py`

The design rules, in order of importance:

1. **The model never computes, never ranks, never picks titles.** It gets a
   numbered fact sheet (`F1…`), numbered items (`T1…` titles it may name,
   `U1…` owned-unwatched it may recommend, `C1…` clusters it may name) and
   the top-3 traits. It returns references, and prose that copies numbers.
2. **Forced shape, then validated meaning.** Ollama `format` forces the JSON
   (as `pillars.adjudicate` does); `WrapupStory` validates lengths and
   patterns; `check_story()` validates that every reference exists, every
   number in the prose exists in the sheet, and no stock phrase slipped in.
   One repair turn naming the fault (the pillars pattern), then the
   deterministic fallback — the page never shows an error where a story
   should be.
3. **Wit from juxtaposition, not adjectives.** The prompt asks for two real
   facts set against each other (the 9★ for a 5.8-rated film next to the
   dropped Palme d'Or winner) — that is where the "razor-sharp" comes from.

```python
"""
Curatarr — the wrap-up story: the curator names and phrases what
wrapup_facts decided.

The model gets a numbered fact sheet and answers in a forced JSON shape
(Ollama ``format``, the pillars pattern). Everything it may mention enters by
reference — facts F#, titles T#, owned-unwatched picks U#, clusters C# — and
every digit in its prose must already be on the sheet, so a wrap-up can be
wrong in taste but never in fact. One repair turn names the fault; after
that the deterministic story stands in (story_source "fallback").
"""
from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

logger = logging.getLogger(__name__)

_Ref = Annotated[str, StringConstraints(pattern=r"^[FTUC]\d{1,3}$")]
_Short = lambda n: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=n)]  # noqa: E731


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: _Short(48)
    kicker: _Short(90)                      # one line under the title
    commentary: _Short(420)                 # 2-3 sentences
    item_refs: list[_Ref] = Field(min_length=1, max_length=4)
    fact_refs: list[_Ref] = Field(min_length=1, max_length=4)


class RoastToast(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roast: _Short(240)
    roast_refs: list[_Ref] = Field(min_length=1, max_length=3)
    toast: _Short(240)
    toast_refs: list[_Ref] = Field(min_length=1, max_length=3)


class LookAhead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: _Short(220)
    item_ref: Optional[_Ref] = None         # a U# when one fits
    fact_refs: list[_Ref] = Field(default_factory=list, max_length=3)


class ClusterName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: _Ref
    name: _Short(32)


class WrapupStory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archetype_title: _Short(40)
    archetype_subhead: _Short(140)
    archetype_trait: str
    era_chapters: list[Chapter] = Field(min_length=2, max_length=4)
    roast_and_toast: RoastToast
    look_ahead_hook: LookAhead
    cluster_names: list[ClusterName] = Field(default_factory=list, max_length=3)
    share_line: _Short(100)


STORY_SCHEMA = WrapupStory.model_json_schema()

# Stock phrases a wrap-up must not contain. Matched case-insensitively as
# whole words; one hit sends the repair turn.
_CLICHES = (
    "journey", "tapestry", "eclectic", "delve", "testament", "embark", "realm",
    "rollercoaster", "it's clear that", "it is clear that", "whether you're",
    "truly", "ultimate", "diverse taste", "you've got taste", "cinematic adventure",
    "a year of", "here's to", "buckle up", "enthusiast", "aficionado", "lover of",
)
_CLICHE_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in _CLICHES) + r")\b", re.I)
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")


# ── the fact sheet ──────────────────────────────────────────────────────────

def render_fact_sheet(f: dict, drift: dict) -> tuple[str, dict]:
    """(sheet text, index). The index maps every reference to what it stands
    for and holds the set of numbers the prose may use."""
    facts, items, picks, clusters = [], {}, {}, {}

    def fact(text: str) -> str:
        fid = f"F{len(facts) + 1}"
        facts.append(f"{fid}: {text}")
        return fid

    def item(title: str, **meta) -> str:
        for k, v in items.items():
            if v["title"] == title:
                return k
        tid = f"T{len(items) + 1}"
        items[tid] = {"title": title, **meta}
        return tid

    t = f.get("tempo") or {}
    p = f["period"]
    approx = "≈" if t.get("hours_estimated_share", 0) > 0.25 else ""
    fact(f"Period: {p['label']}. Scope: {f['scope']}.")
    if t:
        fact(f"{approx}{t['hours']} hours over {t['plays']} plays on {t['active_days']} active days; "
             f"longest streak {t['longest_streak_days']} days.")
        fact(f"Peak hour {t['peak_hour']}:00; {round(100 * t['dayparts'].get('night', 0))}% at night, "
             f"{round(100 * t['weekend_share'])}% on weekends.")
        bd = t["biggest_day"]
        fact(f"Biggest day {bd['date']}: {bd['hours']} hours, {bd['plays']} plays.")
    for i, top in enumerate(f.get("top") or []):
        ref = item(top["title"], media_type=top["media_type"])
        fact(f"Top {i + 1}: {ref} {top['title']} — {top['hours']} hours.")
    fl = f.get("first_last") or {}
    if fl.get("first") and fl.get("last"):
        fact(f"First play: {item(fl['first']['title'])} {fl['first']['title']}; "
             f"last: {item(fl['last']['title'])} {fl['last']['title']}.")
    if (c := f.get("completion")):
        fact(f"Finished {round(100 * c['completion_rate'])}% of plays; dropped {round(100 * c['drop_rate'])}%.")
    if (c := f.get("comfort")):
        fact(f"Comfort title {item(c['title'])} {c['title']}: {c['replays']} replays; "
             f"{round(100 * c['comfort_share'])}% of finished plays were rewatches.")
    b = f.get("binge") or {}
    if (s := b.get("deepest_sitting")):
        fact(f"Deepest sitting: {s['episodes']} episodes of {item(s['series'])} {s['series']} in one go ({s['date']}).")
    if (s := b.get("in_48h")):
        fact(f"{s['episodes']} episodes of {item(s['series'])} {s['series']} inside 48 hours.")
    if (e := f.get("eras")):
        fact(f"Viewing age {e['viewing_age_year']}. Oldest {item(e['oldest']['title'])} "
             f"{e['oldest']['title']} ({e['oldest']['year']}), newest {item(e['newest']['title'])} "
             f"{e['newest']['title']} ({e['newest']['year']}). Fresh {round(100 * e['split']['fresh'])}%, "
             f"catalogue {round(100 * e['split']['catalogue'])}%.")
    if (h := f.get("highbrow_comfort")):
        fact(f"Time split: acclaimed {round(100 * h['highbrow'])}%, comfort {round(100 * h['comfort'])}%, "
             f"other {round(100 * h['other'])}%." + (f" Top acclaimed pick {item(h['top_highbrow'])} "
                                                     f"{h['top_highbrow']}." if h.get("top_highbrow") else ""))
    if (c := f.get("contrarian")):
        fact(f"Contrarian gem {item(c['title'])} {c['title']}: you {c['you']}/10 ({c['basis']}), "
             f"community {c['community']}/10 from {c['votes']} votes.")
    if (d := f.get("dismissal")):
        fact(f"Walked away from {item(d['title'])} {d['title']} ({d['how']}), community {d['community']}/10.")
    if (d := f.get("discovery")):
        fact(f"{round(100 * d['new_share'])}% of time on titles new to you ({d['new_titles']} new, "
             f"{d['returning_titles']} returning).")
    if (m := f.get("music")):
        for a in m["top_artists"][:3]:
            fact(f"Artist {item(a['artist'], media_type='music')} {a['artist']}: {a['hours']} hours.")
        if m.get("on_repeat"):
            r = m["on_repeat"]
            fact(f"On repeat: {r['track']} by {item(r['artist'], media_type='music')} {r['artist']}, "
                 f"{r['plays']} plays on {r['date']}.")
        fact(f"Skipped {round(100 * m['skip_rate'])}% of tracks.")
    if (g := f.get("genres")):
        fact("Top genres: " + ", ".join(f"{x['genre']} {round(100 * x['share'])}%" for x in g["top"]) + ".")
        for x in g.get("rising", []):
            fact(f"Rising: {x['genre']} +{x['pp']} points vs your past.")
        for x in g.get("fading", []):
            fact(f"Fading: {x['genre']} {x['pp']} points vs your past.")
    cu = f.get("curatarr") or {}
    if (g := cu.get("graveyard")):
        fact(f"Deleted {g['deleted']} titles, {g['gb_freed']} GB freed; biggest {item(g['biggest']['title'])} "
             f"{g['biggest']['title']} ({g['biggest']['gb']} GB).")
    if (c := cu.get("came_back")):
        fact(f"Deleted, then back in the library: {item(c['title'])} {c['title']}.")
    if (s := cu.get("shield")):
        fact(f"Kept {s['overrides']} titles over the curator's objection"
             + (f", most often for '{s['top_reason']}'" if s.get("top_reason") else "") + "."
             + (f" Fiercest: {item(s['fiercest']['title'])} {s['fiercest']['title']} — curator said "
                f"\"{s['fiercest']['curator_stance']}\"." if s.get("fiercest") else ""))
    if (d := cu.get("most_debated")):
        fact(f"Most debated: {item(d['title'])} {d['title']}, {d['messages']} messages, ended {d['outcome']}.")
    for pr in cu.get("principles") or []:
        fact(f"New curation principle: \"{pr[:140]}\".")
    if (d := cu.get("dust_collector")):
        uid = f"U{len(picks) + 1}"
        picks[uid] = {"title": d["title"], "media_type": d["media_type"]}
        fact(f"Dust collector {uid} {d['title']}: {d['gb']} GB, never played by you.")
    for la in f.get("look_ahead") or []:
        uid = f"U{len(picks) + 1}"
        picks[uid] = {"title": la["title"], "media_type": la["category"]}
        fact(f"Owned, unwatched pick {uid} {la['title']}" + (f" ({la['year']})" if la.get("year") else "")
             + (f": {la['reason']}" if la.get("reason") else "") + ".")
    if drift:
        fact(f"Taste drift vs your past: {drift['label']}.")
        for c in drift.get("clusters") or []:
            clusters[c["id"]] = c
            fact(f"Cluster {c['id']} around {', '.join(c['top_titles'])}: {round(100 * c['share'])}% now, "
                 f"{round(100 * c['share_before'])}% before ({c['trend']}).")

    traits = (f.get("traits") or [])[:3]
    sheet = "\n".join(facts)
    sheet += "\n\nTRAITS (pick one as archetype_trait): " + "; ".join(
        f"{tr['key']} ({tr['evidence']})" for tr in traits)
    numbers = {n.replace(",", ".") for n in _NUM_RE.findall(sheet)}
    return sheet, {"facts": {x.split(":", 1)[0] for x in facts}, "items": items,
                   "picks": picks, "clusters": clusters, "numbers": numbers,
                   "traits": {tr["key"] for tr in traits}}


# ── the prompt ──────────────────────────────────────────────────────────────

SYSTEM = """You are Curatarr, the household's media curator, writing ONE person's wrap-up.
You know them from a year of arguing about what stays on the server. Voice: a sharp critic
friend — dry, specific, affectionate underneath. Never a greeting card, never a press release.

HARD RULES
1. Every claim rests on the FACTS below; cite them in fact_refs / item_refs. Name a title
   only if it has a T# and you reference it.
2. Numbers: copy them exactly as written in the facts. Never compute, round or invent one.
3. archetype_title: 2-4 words, a role they EARNED from the trait you pick — concrete and a
   little pointed ("The Midnight Completionist", "Loyalist With Exceptions"). No
   "Enthusiast", "Lover", "Fan", "Buff", "Aficionado".
4. Wit comes from putting two real facts side by side, not from adjectives.
   Weak: "You have an eclectic, diverse taste." Strong: "Nine stars for a 5.8 and a walkout
   on the Palme d'Or winner — your standards are real, they're just yours."
5. Chapters (2-4): distinct phases or sides of the period, each anchored on 1-4 titles.
   Title: a headline, not a summary. Commentary: 2-3 sentences.
6. roast: one blind spot or contradiction the facts prove. toast: their highest-signal
   taste, said with respect. Neither cruel nor flattering.
7. look_ahead_hook: one concrete challenge; recommend ONLY a U# item, or none.
8. cluster_names: 2-4 word names for each C# given, from its titles.
9. share_line: one line they'd be glad to post, under 100 characters.
10. Banned: journey, tapestry, eclectic, delve, testament, embark, realm, "truly",
    "it's clear that", "here's to", exclamation marks.
Answer with the JSON object only."""


def build_messages(sheet: str) -> list:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "FACTS\n" + sheet}]


# ── validation ──────────────────────────────────────────────────────────────

class StoryRejected(ValueError):
    pass


def _prose(s: WrapupStory) -> list[str]:
    out = [s.archetype_title, s.archetype_subhead, s.share_line,
           s.roast_and_toast.roast, s.roast_and_toast.toast, s.look_ahead_hook.challenge]
    for c in s.era_chapters:
        out += [c.title, c.kicker, c.commentary]
    return out


def check_story(s: WrapupStory, idx: dict) -> None:
    """Meaning checks the schema can't express. Raises StoryRejected naming
    the first fault — the text goes verbatim into the repair turn."""
    if s.archetype_trait not in idx["traits"] and idx["traits"]:
        raise StoryRejected(f"archetype_trait must be one of {sorted(idx['traits'])}")
    known = idx["facts"] | set(idx["items"]) | set(idx["picks"]) | set(idx["clusters"])
    refs = [r for c in s.era_chapters for r in c.item_refs + c.fact_refs]
    refs += s.roast_and_toast.roast_refs + s.roast_and_toast.toast_refs + s.look_ahead_hook.fact_refs
    refs += [c.ref for c in s.cluster_names]
    if s.look_ahead_hook.item_ref:
        if s.look_ahead_hook.item_ref not in idx["picks"]:
            raise StoryRejected("look_ahead_hook.item_ref must be a U# reference or null")
    for r in refs:
        if r not in known:
            raise StoryRejected(f"reference {r} is not in the facts")
    for c in s.era_chapters:
        if any(r not in idx["items"] for r in c.item_refs):
            raise StoryRejected(f"chapter '{c.title}': item_refs must be T# references")
    used = [r for c in s.era_chapters for r in c.item_refs]
    if len(used) != len(set(used)):
        raise StoryRejected("each title may anchor only one chapter")
    allowed = idx["numbers"] | {str(n) for n in range(0, 11)}
    for text in _prose(s):
        for n in _NUM_RE.findall(text):
            if n.replace(",", ".") not in allowed:
                raise StoryRejected(f"the number {n} is not in the facts — copy numbers exactly")
        if (m := _CLICHE_RE.search(text)):
            raise StoryRejected(f"banned phrase '{m.group(0)}'")
        if "!" in text:
            raise StoryRejected("no exclamation marks")


def parse_story(raw: str, idx: dict) -> WrapupStory:
    from src.services.llm_utils import parse_llm_json
    try:
        s = WrapupStory.model_validate(parse_llm_json(raw))
    except (ValidationError, ValueError, TypeError) as e:
        raise StoryRejected(f"shape: {str(e)[:200]}") from e
    check_story(s, idx)
    return s


# ── generation ──────────────────────────────────────────────────────────────

_PREDICT = 1400
_TIMEOUT = 300.0


async def write_story(sheet: str, idx: dict) -> tuple[Optional[dict], str]:
    """(story dict, source) — source llm / llm_repaired, or (None, reason)."""
    from src.config import settings
    from src.services.llm_errors import LLMCallError, post_chat
    from src.services.llm_utils import curator_payload
    from src.services.ollama_hosts import host_for

    host = host_for("curator")
    messages = build_messages(sheet)

    async def ask(msgs):
        payload = curator_payload(msgs, model=settings.CURATOR_MODEL, temperature=0.75,
                                  num_predict=_PREDICT, fmt=STORY_SCHEMA,
                                  repeat_penalty=1.0, host=host)
        data = await post_chat(host.url, payload, read_timeout=_TIMEOUT)
        return (data.get("message") or {}).get("content", "") or ""

    raw = ""
    try:
        raw = await ask(messages)
        return parse_story(raw, idx).model_dump(), "llm"
    except StoryRejected as e:
        logger.info("[wrapup] story rejected (%s) — one repair turn", e)
        try:
            raw2 = await ask(messages + [
                {"role": "assistant", "content": raw[:2000]},
                {"role": "user", "content": f"Rejected: {e}. Send the complete JSON again, "
                                            "every rule kept."}])
            return parse_story(raw2, idx).model_dump(), "llm_repaired"
        except (StoryRejected, LLMCallError) as e2:
            return None, f"repair failed: {e2}"
    except LLMCallError as e:
        return None, f"curator unavailable: {e}"


# Deterministic stand-in: the same shape, built from the strongest trait.
_TRAIT_TITLES = {
    "night_owl": ("The Midnight Shift", "Most of it happened after the house went quiet."),
    "matinee": ("The Weekend Matinee", "Saturday afternoons did the heavy lifting."),
    "completionist": ("The Completionist", "Started it, finished it, every time."),
    "sampler": ("The Sampler", "Many first episodes, fewer second ones."),
    "scout": ("The Scout", "Mostly things you had never seen before."),
    "loyalist": ("The Loyalist", "Old friends got most of the hours."),
    "archivist": ("The Archivist", "Your viewing age is older than some of your hardware."),
    "day_one": ("Day One", "If it came out this year, you were there."),
    "binger": ("The Binger", "One more episode was never one."),
    "highbrow": ("The Highbrow", "The acclaimed shelf did not gather dust."),
    "comfort_seeker": ("The Comfort Rewatcher", "Familiar ground, by choice."),
    "contrarian": ("The Contrarian", "Your favourites are not the crowd's."),
    "purger": ("The Purger", "The server is lighter for it."),
    "defender": ("The Defender", "Some titles stay because you said so."),
}


def fallback_story(f: dict, idx: dict) -> dict:
    trait = (f.get("traits") or [{"key": "loyalist"}])[0]["key"]
    title, sub = _TRAIT_TITLES.get(trait, ("Your Period", "The numbers, without the commentary."))
    tops = list(idx["items"])[:4]
    return {
        "archetype_title": title, "archetype_subhead": sub, "archetype_trait": trait,
        "era_chapters": [{"title": "Where the hours went", "kicker": "Your top titles",
                          "commentary": "", "item_refs": tops[:2] or [], "fact_refs": []},
                         {"title": "The rest of the shelf", "kicker": "",
                          "commentary": "", "item_refs": tops[2:4], "fact_refs": []}],
        "roast_and_toast": None, "look_ahead_hook": None, "cluster_names": [],
        "share_line": title,
    }
```

**Generation task** (`wrapup_story.generate_wrapup`) — what the ticker shows:

```python
PHASES = ("Counting plays", "Finding the outliers", "Mapping taste clusters",
          "Fetching artwork", "Curator is writing", "Checking the facts")


async def generate_wrapup(user_id: int, scope: str, period_key: str, is_admin: bool, task) -> None:
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    from src.services.wrapup_facts import build_wrapup_facts, taste_drift
    from src.services.wrapup_store import save_facts, save_story

    step = lambda i: task_monitor.update(task, processed=i, message=PHASES[i])   # noqa: E731
    try:
        step(0)
        facts = await run_read(build_wrapup_facts, user_id, scope, period_key, is_admin=is_admin)
        step(1)
        if facts.get("empty"):
            task_monitor.skip(task, "nothing played in this period")
            return
        step(2)
        drift = await taste_drift(user_id, scope, period_key)
        facts["drift"] = drift
        step(3)
        await attach_posters(facts)                      # bounded _fetch_tmdb gather, 24 h cache
        await run_write(save_facts, user_id, scope, period_key, facts)
        step(4)
        sheet, idx = render_fact_sheet(facts, drift)
        story, source = await write_story(sheet, idx)
        step(5)
        if story is None:
            logger.info("[wrapup] %s/%s/%s: fallback story (%s)", user_id, scope, period_key, source)
            story, source = fallback_story(facts, idx), "fallback"
        story["refs"] = {**idx["items"], **idx["picks"]}   # ref → title/poster for the UI
        await run_write(save_story, user_id, scope, period_key, facts["facts_hash"], story, source)
        task_monitor.done(task, "Wrap-up written")
    except Exception as e:                               # a task must never die silently
        logger.warning("[wrapup] generation failed: %s", e)
        task_monitor.error(task, str(e)[:200])
```

Expected timing with the 27B curator warm: facts 0.3–2 s (all-time music is
the worst case — every aggregate is SQL `GROUP BY`, no row materialisation),
drift 1–4 s, artwork < 2 s (cached), writing 60–110 s for ~1,400 tokens,
validation instant; one repair adds the same again. Cold curator load on top.

### 3.4 Routes — `src/routers/wrapup.py` (mounted at `/api/wrapup`)

```python
"""
Curatarr - Wrap-Up Router

Every household member's own wrap-up: facts are computed live (cheap SQL,
reused for 15 minutes, frozen for closed years), the story is written in a
background task and replayed from the wrapups table.
"""
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from src.database.models import User
from src.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()
_FRESH = timedelta(minutes=15)


def _check(scope: str, period: str):
    from src.services.wrapup_facts import SCOPES, resolve_period
    if scope not in SCOPES:
        raise HTTPException(400, "unknown scope")
    try:
        return resolve_period(period)
    except ValueError:
        raise HTTPException(400, "unknown period")


@router.get("")
async def get_wrapup(scope: str = "all", period: str = "30d",
                     user: User = Depends(get_current_user)):
    from src.database.runner import run_read, run_write
    from src.services.wrapup_facts import build_wrapup_facts
    from src.services.wrapup_store import available, load, save_facts
    p = _check(scope, period)
    row = await run_read(load, user.id, scope, period)
    fresh = row and row.get("facts") and (
        p.closed or datetime.utcnow() - row["facts_at"] < _FRESH)
    facts = row["facts"] if fresh else await run_read(
        build_wrapup_facts, user.id, scope, period, is_admin=bool(user.is_admin))
    if not fresh and not facts.get("empty"):
        await run_write(save_facts, user.id, scope, period, facts)
    story = row and row.get("story")
    return {
        "facts": facts,
        "story": story,
        "story_stale": bool(story) and row.get("story_hash") != facts.get("facts_hash"),
        "story_at": row and row.get("story_at"),
        "story_source": row and row.get("story_source"),
        "generating": _running_task(user.id, scope, period),
        **(await run_read(available, user.id)),   # {"scopes": [...], "years": [...]}
    }


@router.post("/generate")
async def generate(body: dict, user: User = Depends(get_current_user)):
    from src.services.app_state import aacquire_state_lock
    from src.services.bg_tasks import track_task
    from src.services.task_monitor import task_monitor
    from src.services.wrapup_story import PHASES, generate_wrapup
    scope, period = body.get("scope", "all"), body.get("period", "30d")
    _check(scope, period)
    if (tid := _running_task(user.id, scope, period)):
        return {"task_id": tid}
    lock = f"wrapup_gen:{user.id}:{scope}:{period}"
    if not await aacquire_state_lock(lock):
        raise HTTPException(409, "already writing")
    task = task_monitor.create(f"Wrap-up: {scope} · {period}", "wrapup", total=len(PHASES),
                               task_id=f"wrapup-{user.id}-{scope}-{period}")
    task_monitor.start(task)

    async def run():
        from src.services.app_state import arelease_state_lock
        try:
            await generate_wrapup(user.id, scope, period, bool(user.is_admin), task)
        finally:
            await arelease_state_lock(lock)
    track_task(run(), name=task.id)
    return {"task_id": task.id}


@router.get("/status/{task_id}")
def status(task_id: str, user: User = Depends(get_current_user)):
    from src.services.task_monitor import task_monitor
    if not task_id.startswith(f"wrapup-{user.id}-"):
        raise HTTPException(404)
    t = next((x for x in task_monitor.get_all() if x["id"] == task_id), None)
    if not t:
        raise HTTPException(404)
    return {"status": t["status"], "phase": t["processed"], "total": t["total"],
            "message": (t["logs"] or [{}])[-1].get("msg"), "error": t["error"]}


def _running_task(user_id, scope, period):
    from src.services.task_monitor import task_monitor
    tid = f"wrapup-{user_id}-{scope}-{period}"
    return next((t["id"] for t in task_monitor.get_running() if t["id"] == tid), None)
```

(`get_all()`/`get_running()` return `to_dict()` rows — verify the exact
return type when wiring; the status endpoint only needs id, status,
processed, total, logs, error.) `src/routers/stats.py` keeps
`GET /api/stats/curation` for the admin ledger; `POST /curation/narrative`
is removed with PR 2.

### 3.5 Frontend — `frontend/js/wrapup.js`

Conventions honoured: CSP (`style-src 'self'` — computed values travel as
`data-style` and `hydrateStyles` applies them), literal `act()` names
registered in `app.js`, `esc()` on every interpolated string, `countTo()`
on integers when a tile scrolls in, `haptic()` on the period switch and the
reveal, `--amber` as the one accent, `_posterImg` + `.card` so
`posterLoaded` lends the hero its tint, and reduced motion respected through
the existing helpers.

```js
// ── Wrap-Up: one person's period in review (replaces the Curation Report) ──
// Facts arrive instantly (cached server-side); the story is written in a
// background task the ticker follows. Tiles render only when the server
// sent their section — an empty tile is a lie in a recap.
import { api } from './api.js';
import { discussInChat } from './chat.js';
import { renderLedger } from './report.js';
import { EL, _errHtml, _errMsg, _posterImg, act, btnBusy, btnDone, countTo, esc, haptic, toast } from './ui.js';

const WU_KEY = 'curatarr_wrapup';
const PERIODS = [['30d', 'Last 30 days'], ['6m', '6 months'], ['year', 'Year'], ['all', 'All time']];
const SCOPE_LABEL = {all: 'Everything', movie: 'Movies', show: 'TV', anime: 'Anime', music: 'Music'};
const PHASES = ['Counting plays', 'Finding the outliers', 'Mapping taste clusters',
                'Fetching artwork', 'Curator is writing', 'Checking the facts'];
const WD = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];
const wu = {scope: 'all', period: '30d', data: null, poll: null, snapshot: false};
try { Object.assign(wu, JSON.parse(localStorage.getItem(WU_KEY) || '{}')); } catch (_) { /* private mode */ }
const _save = () => { try { localStorage.setItem(WU_KEY, JSON.stringify({scope: wu.scope, period: wu.period})); } catch (_) {} };
const _periodKey = () => wu.period === 'year' ? `year:${new Date().getFullYear()}` : wu.period;
const _pct = v => Math.round(100 * (v || 0));
const _bar = (cls, pct) => `<i class="${cls}" data-style="--v:${Math.max(0, Math.min(100, pct))}%"></i>`;

export async function loadWrapup() {
  const el = document.getElementById('wrapup-content');
  if (!wu.data) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Pulling your period together…</p>';
  try {
    wu.data = await api(`/api/wrapup?scope=${encodeURIComponent(wu.scope)}&period=${encodeURIComponent(_periodKey())}`);
    _render();
    if (wu.data.generating) _follow(wu.data.generating);
  } catch (e) {
    el.innerHTML = _errHtml(e, act('loadWrapup'));
  }
}

export function setWrapupPeriod(p) { if (p === wu.period) return; wu.period = p; _save(); haptic('tick'); loadWrapup(); }
export function setWrapupScope(s) { if (s === wu.scope) return; wu.scope = s; _save(); haptic('tick'); loadWrapup(); }
export function toggleWrapupSnapshot() { wu.snapshot = !wu.snapshot; haptic('light'); _render(); }

function _render(reveal = false) {
  const el = document.getElementById('wrapup-content');
  const d = wu.data, f = d.facts || {};
  if (f.empty) {
    el.innerHTML = _controls(d) + `<div class="wu-empty">Nothing played in this period yet. Try a longer one.</div>`;
    return;
  }
  el.innerHTML = wu.snapshot
    ? _controls(d) + _snapshot(f, d.story)
    : _controls(d) + _hero(f, d) + _bento(f) + _chapters(f, d.story) + _ledgerSlot(f);
  if (reveal) el.querySelector('.wu-hero')?.classList.add('is-reveal');
  _armCounters(el);
  if (f.curatarr?.ledger) renderLedger(document.getElementById('wu-ledger'));
}

// ── controls ────────────────────────────────────────────────────────────────
function _controls(d) {
  const per = PERIODS.map(([k, l]) => `<button type="button" class="wu-chip${k === wu.period ? ' is-on' : ''}" aria-pressed="${k === wu.period}" ${act('setWrapupPeriod', k)}>${esc(l)}</button>`).join('');
  const scopes = (d.scopes || ['all']).map(s => `<button type="button" class="wu-tab${s === wu.scope ? ' is-on' : ''}" aria-pressed="${s === wu.scope}" ${act('setWrapupScope', s)}>${esc(SCOPE_LABEL[s] || s)}</button>`).join('');
  return `<div class="wu-controls"><div class="wu-chips" role="group" aria-label="Period">${per}</div>
    <div class="wu-tabs" role="group" aria-label="Media">${scopes}</div>
    <button type="button" class="btn btn-secondary btn-sm wu-snap-btn" ${act('toggleWrapupSnapshot')}>${wu.snapshot ? 'Full view' : 'Snapshot'}</button></div>`;
}

// ── hero persona ────────────────────────────────────────────────────────────
function _hero(f, d) {
  const s = d.story, top = (f.top || [])[0], isMusic = top?.media_type === 'music';
  const title = s?.archetype_title || _traitTitle(f), sub = s?.archetype_subhead || f.period?.label || '';
  const gen = d.generating ? _ticker(0)
    : `<button type="button" class="btn btn-primary btn-sm" ${act('generateWrapup', EL)}>${s ? (d.story_stale ? 'Numbers moved — rewrite' : 'Rewrite') : 'Let the curator write it'}</button>`;
  return `<section class="card wu-hero" aria-labelledby="wu-arch">
    <div class="wu-hero-art">${_posterImg(top?.poster, 200, isMusic ? 200 : 300, isMusic)}</div>
    <div class="wu-hero-text">
      <div class="wu-eyebrow">${esc(f.period?.label || '')} · ${esc(SCOPE_LABEL[f.scope] || '')}</div>
      <h2 id="wu-arch" class="wu-arch">${esc(title)}</h2>
      <p class="wu-sub">${esc(sub)}</p>
      ${s?.share_line ? `<p class="wu-share-line">“${esc(s.share_line)}”</p>` : ''}
      <div class="wu-hero-actions" id="wu-gen">${gen}</div>
    </div></section>`;
}
const _traitTitle = f => ({night_owl: 'The Midnight Shift', scout: 'The Scout', loyalist: 'The Loyalist',
  binger: 'The Binger', archivist: 'The Archivist', completionist: 'The Completionist'})[f.traits?.[0]?.key] || 'Your period in review';

// ── bento ───────────────────────────────────────────────────────────────────
function _tile(cls, label, body) {
  return `<article class="wu-tile ${cls}"><div class="wu-tile-lbl">${esc(label)}</div>${body}</article>`;
}
const _num = (n, suffix = '') => `<span class="wu-num" data-count="${Math.round(n || 0)}">0</span>${suffix ? `<span class="wu-unit">${esc(suffix)}</span>` : ''}`;

function _bento(f) {
  const t = f.tempo || {}, tiles = [];
  if (t.hours != null) {
    const max = Math.max(1, ...(t.by_hour_h || [0]));
    const clock = (t.by_hour_h || []).map((h, i) => `<span class="wu-hr${i === t.peak_hour ? ' is-peak' : ''}" title="${i}:00 · ${h} h">${_bar('wu-hr-fill', 100 * h / max)}</span>`).join('');
    tiles.push(_tile('wu-span-hours', 'Hours & rhythm',
      `<div class="wu-big">${t.hours_estimated_share > 0.25 ? '≈' : ''}${_num(t.hours, 'h')}</div>
       <div class="wu-meta">${_num(t.active_days)} active days · best streak ${esc(String(t.longest_streak_days))} days</div>
       <div class="wu-clock" role="img" aria-label="Hours by time of day, peak at ${t.peak_hour}:00">${clock}</div>
       <div class="wu-meta">Peak ${esc(String(t.peak_hour))}:00 · busiest on ${esc(WD[t.peak_weekday] || '')} · biggest day ${esc(t.biggest_day?.date || '')} (${esc(String(t.biggest_day?.hours ?? ''))} h)</div>`));
  }
  const hc = f.highbrow_comfort;
  if (hc) tiles.push(_tile('wu-span-split', 'Highbrow vs comfort',
    `<div class="wu-split">${_bar('wu-split-hb', _pct(hc.highbrow))}${_bar('wu-split-cf', _pct(hc.comfort))}<i class="wu-split-ot"></i></div>
     <div class="wu-split-legend"><span><b>${_pct(hc.highbrow)}%</b> acclaimed${hc.top_highbrow ? ` · ${esc(hc.top_highbrow)}` : ''}</span>
     <span><b>${_pct(hc.comfort)}%</b> comfort${hc.top_comfort ? ` · ${esc(hc.top_comfort)}` : ''}</span></div>`));
  const c = f.contrarian;
  if (c?.title) tiles.push(_tile('wu-span-poster', 'The contrarian gem', _posterTile(c,
    `<div class="wu-vs"><span><b>${esc(String(c.you))}</b> you</span><span><b>${esc(String(c.community))}</b> everyone</span></div>`)));
  const dm = f.dismissal;
  if (dm?.title) tiles.push(_tile('wu-span-poster', 'The unpopular dismissal', _posterTile(dm,
    `<div class="wu-meta">${esc(dm.how)} · community ${esc(String(dm.community))}/10</div>`)));
  const b = f.binge?.in_48h || f.binge?.deepest_sitting;
  if (b) tiles.push(_tile('wu-span-sm', f.binge?.in_48h ? '48-hour binge' : 'Deepest sitting',
    `<div class="wu-big">${_num(b.episodes, 'eps')}</div><div class="wu-meta">${esc(b.series)}</div>`));
  const e = f.eras;
  if (e) tiles.push(_tile('wu-span-sm', 'Viewing age',
    `<div class="wu-big wu-year">${esc(String(e.viewing_age_year))}</div>
     <div class="wu-meta">${esc(e.oldest.title)} (${esc(String(e.oldest.year))}) → ${esc(e.newest.title)} (${esc(String(e.newest.year))})</div>
     <div class="wu-decades">${Object.entries(e.decades).map(([k, v]) => `<span title="${esc(k)} ${_pct(v)}%">${_bar('wu-dec-fill', _pct(v))}<em>${esc(k.slice(2))}</em></span>`).join('')}</div>`));
  const ds = f.discovery;
  if (ds?.new_share != null) tiles.push(_tile('wu-span-sm', ds.new_share >= 0.5 ? 'Scout' : 'Loyalist',
    `<div class="wu-big">${_num(_pct(ds.new_share), '%')}</div><div class="wu-meta">on titles new to you${ds.best_new ? ` · best find ${esc(ds.best_new)}` : ''}</div>`));
  const cu = f.curatarr || {};
  if (cu.graveyard) tiles.push(_tile('wu-span-purge', 'The graveyard',
    `<div class="wu-big">${_num(cu.graveyard.gb_freed, 'GB')}</div>
     <div class="wu-meta">${esc(String(cu.graveyard.deleted))} deleted · biggest ${esc(cu.graveyard.biggest.title)} (${esc(String(cu.graveyard.biggest.gb))} GB)</div>
     ${cu.came_back ? `<div class="wu-meta">Came back anyway: <b>${esc(cu.came_back.title)}</b></div>` : ''}`));
  if (cu.shield) tiles.push(_tile('wu-span-sm', 'The shield',
    `<div class="wu-big">${_num(cu.shield.overrides)}</div><div class="wu-meta">kept over the curator's objection${cu.shield.top_reason ? ` · mostly “${esc(cu.shield.top_reason)}”` : ''}</div>`));
  const dr = f.drift;
  if (dr?.clusters?.length) {
    const names = Object.fromEntries((wu.data.story?.cluster_names || []).map(n => [n.ref, n.name]));
    tiles.push(_tile('wu-span-clusters', `Taste clusters · ${dr.label}`, dr.clusters.map(k =>
      `<div class="wu-cluster"><span class="wu-cl-name">${esc(names[k.id] || `Around ${k.top_titles[0] || ''}`)}</span>
       <span class="wu-cl-bar">${_bar('wu-cl-fill', _pct(k.share))}</span>
       <span class="wu-cl-trend is-${esc(k.trend)}">${k.trend === 'rising' ? '↑' : k.trend === 'fading' ? '↓' : '→'} ${_pct(k.share)}%</span></div>`).join('')));
  }
  if (cu.dust_collector) tiles.push(_tile('wu-span-sm', 'Dust collector',
    `<div class="wu-big">${_num(cu.dust_collector.gb, 'GB')}</div><div class="wu-meta">${esc(cu.dust_collector.title)} — never played by you</div>`));
  return `<div class="wu-bento">${tiles.join('')}</div>`;
}

function _posterTile(x, extra) {
  const isMusic = x.media_type === 'music';
  return `<div class="wu-poster-tile"><div class="glow-interactive${isMusic ? ' is-music' : ''}">${_posterImg(x.poster, 96, isMusic ? 96 : 144, isMusic)}</div>
    <div><div class="wu-tile-title">${esc(x.title)}</div>${extra}</div></div>`;
}

// ── chapters, roast & toast, look-ahead ─────────────────────────────────────
function _chapters(f, s) {
  if (!s?.era_chapters?.length) return '';
  const refs = s.refs || {};
  const ch = s.era_chapters.map((c, i) => {
    const posters = c.item_refs.map(r => refs[r]).filter(Boolean).slice(0, 4)
      .map(it => `<div class="wu-fan-item">${_posterImg(it.poster, 84, it.media_type === 'music' ? 84 : 126, it.media_type === 'music')}</div>`).join('');
    return `<article class="card wu-chapter">
      <div class="wu-fan" aria-hidden="true">${posters}</div>
      <div class="wu-ch-body"><div class="wu-ch-n">Chapter ${i + 1}</div>
        <h3 class="wu-ch-title">${esc(c.title)}</h3>
        ${c.kicker ? `<div class="wu-ch-kicker">${esc(c.kicker)}</div>` : ''}
        ${c.commentary ? `<p class="wu-ch-text">${esc(c.commentary)}</p>` : ''}
        <button type="button" class="btn btn-ghost btn-sm" ${act('discussWrapupChapter', i)}>Discuss this era</button></div></article>`;
  }).join('');
  const rt = s.roast_and_toast;
  const pair = rt ? `<div class="wu-pair"><article class="wu-roast"><div class="wu-tile-lbl">Roast</div><p>${esc(rt.roast)}</p></article>
    <article class="wu-toast"><div class="wu-tile-lbl">Toast</div><p>${esc(rt.toast)}</p></article></div>` : '';
  const la = s.look_ahead_hook, pick = la?.item_ref && refs[la.item_ref];
  const hook = la ? `<article class="card wu-hook">${pick ? _posterImg(pick.poster, 72, 108, false) : ''}
    <div><div class="wu-tile-lbl">Next up</div><p>${esc(la.challenge)}</p>
    ${pick ? `<button type="button" class="btn btn-secondary btn-sm" ${act('discussWrapupHook')}>Talk me into it</button>` : ''}</div></article>` : '';
  const note = wu.data.story_source === 'fallback' ? '<p class="wu-note">The curator was busy, so these are the numbers without its commentary.</p>' : '';
  return `<section class="wu-chapters" aria-label="Chapters">${ch}</section>${pair}${hook}${note}`;
}

export function discussWrapupChapter(i) {
  const c = wu.data?.story?.era_chapters?.[i];
  if (!c) return;
  haptic('light');
  discussInChat('custom', {message: `About my wrap-up chapter "${c.title}": ${c.commentary} — is that fair? What does it say about what I should watch next?`});
}
export function discussWrapupHook() {
  const s = wu.data?.story, pick = s?.refs?.[s?.look_ahead_hook?.item_ref];
  if (pick) discussInChat('custom', {message: `My wrap-up says I should watch "${pick.title}" next. Make the case.`});
}

// ── snapshot ────────────────────────────────────────────────────────────────
function _snapshot(f, s) {
  const t = f.tempo || {}, posters = (f.top || []).slice(0, 5)
    .map(x => _posterImg(x.poster, 64, x.media_type === 'music' ? 64 : 96, x.media_type === 'music')).join('');
  const stats = [[t.hours, 'hours'], [t.active_days, 'active days'],
    f.eras ? [f.eras.viewing_age_year, 'viewing age'] : [f.curatarr?.graveyard?.gb_freed, 'GB freed']]
    .filter(([v]) => v != null);
  return `<section class="card wu-snap" aria-label="Shareable summary">
    <div class="wu-eyebrow">Curatarr · ${esc(f.period?.label || '')}</div>
    <h2 class="wu-arch">${esc(s?.archetype_title || _traitTitle(f))}</h2>
    <p class="wu-sub">${esc(s?.share_line || s?.archetype_subhead || '')}</p>
    <div class="wu-snap-stats">${stats.map(([v, l]) => `<div><div class="wu-num-static">${esc(String(v))}</div><div class="wu-meta">${esc(l)}</div></div>`).join('')}</div>
    <div class="wu-snap-posters">${posters}</div>
    <ol class="wu-snap-top">${(f.top || []).slice(0, 5).map(x => `<li>${esc(x.title)}</li>`).join('')}</ol>
    <button type="button" class="btn btn-primary btn-sm" ${act('shareWrapup')}>Share</button></section>`;
}

export async function shareWrapup() {
  const d = wu.data, s = d?.story, f = d?.facts || {};
  const text = `${s?.archetype_title || 'My wrap-up'} — ${s?.share_line || ''}\n` +
    (f.top || []).slice(0, 5).map((x, i) => `${i + 1}. ${x.title}`).join('\n');
  try {
    if (navigator.share) await navigator.share({title: 'My Curatarr wrap-up', text});
    else { await navigator.clipboard.writeText(text); toast('Copied to clipboard', 'success'); }
  } catch (_) { /* the share sheet was closed */ }
}

// ── generation + ticker ─────────────────────────────────────────────────────
export async function generateWrapup(btn) {
  btnBusy(btn, 'Starting…');
  try {
    const r = await api('/api/wrapup/generate', 'POST', {scope: wu.scope, period: _periodKey()});
    haptic('medium');
    _follow(r.task_id);
  } catch (e) {
    toast('Failed: ' + _errMsg(e).slice(0, 160), 'danger');
    btnDone(btn, 'Let the curator write it');
  }
}

function _ticker(phase) {
  return `<ol class="wu-ticker" role="status" aria-live="polite">${PHASES.map((p, i) =>
    `<li class="${i < phase ? 'is-done' : i === phase ? 'is-now' : ''}">${esc(p)}</li>`).join('')}</ol>`;
}

function _follow(taskId) {
  clearInterval(wu.poll);
  const slot = () => document.getElementById('wu-gen');
  if (slot()) slot().innerHTML = _ticker(0);
  wu.poll = setInterval(async () => {
    // Left the view: stop asking (nav.js's own test for the open view).
    if (!document.getElementById('wrapup-view')?.classList.contains('active')) { clearInterval(wu.poll); return; }
    try {
      const st = await api(`/api/wrapup/status/${encodeURIComponent(taskId)}`);
      if (slot()) slot().innerHTML = _ticker(st.phase || 0);
      if (st.status === 'running' || st.status === 'pending') return;
      clearInterval(wu.poll);
      if (st.status === 'error') { toast(st.error || 'The curator could not finish', 'danger'); }
      wu.data = await api(`/api/wrapup?scope=${encodeURIComponent(wu.scope)}&period=${encodeURIComponent(_periodKey())}`);
      _render(true);
      haptic('medium');
    } catch (_) { clearInterval(wu.poll); }
  }, 1500);
}

// ── counters: count up when a tile scrolls into view, once ──────────────────
function _armCounters(root) {
  const nodes = root.querySelectorAll('[data-count]');
  if (!('IntersectionObserver' in window)) { nodes.forEach(n => countTo(n, +n.dataset.count)); return; }
  const io = new IntersectionObserver(entries => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      countTo(en.target, +en.target.dataset.count, 900);
      io.unobserve(en.target);
    }
  }, {threshold: 0.4});
  nodes.forEach(n => io.observe(n));
}

function _ledgerSlot(f) {
  return f.curatarr?.ledger ? `<details class="section wu-ledger"><summary class="section-head"><h3>Curation ledger</h3>
    <span class="section-hint">the debates, month by month</span></summary><div id="wu-ledger" class="section-body"></div></details>` : '';
}
```

Wiring: `index.html` gets a `#wrapup-view` with `<div id="wrapup-content">`;
the sidebar's Report entry becomes "Wrap-Up" for everyone (`data-view="wrapup"`,
`#report` redirects); `nav.js` drops `report` from `ADMIN_VIEWS` and calls
`loadWrapup()`; `app.js` registers `loadWrapup, setWrapupPeriod,
setWrapupScope, toggleWrapupSnapshot, generateWrapup, discussWrapupChapter,
discussWrapupHook, shareWrapup`. `report.js` keeps its renderer as
`renderLedger(el)` (fetches `/api/stats/curation` itself) and loses the
narrative button. `wu.period === 'year'` uses the current year; a year picker
for past years comes from `available().years`.

### 3.6 Styles — `frontend/css/app.css` (appended, before the phone overrides)

```css
/* ── Wrap-Up: hero persona, bento tiles, chapter cards, snapshot ─────────
   One accent (amber), the poster's own colour as the hero's light
   (posterLoaded → --poster-tint on the .card), glass tiles on the
   elevation tokens. Computed widths ride --v from data-style. */
.wu-controls { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; margin-bottom: 18px; }
.wu-chips, .wu-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.wu-chip, .wu-tab { border: 1px solid var(--hairline-strong); background: var(--glass); color: var(--text2);
  border-radius: 999px; padding: 6px 12px; font-size: var(--fs-sm); cursor: pointer;
  transition: background var(--dur-2), color var(--dur-2), border-color var(--dur-2); }
.wu-chip.is-on, .wu-tab.is-on { background: var(--amber); border-color: var(--amber); color: var(--bg); }
.wu-snap-btn { margin-left: auto; }

.wu-hero { position: relative; display: grid; grid-template-columns: auto 1fr; gap: 28px; align-items: center;
  padding: 28px; border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-3), var(--specular);
  --tint-a: 26%; }
.wu-hero.has-tint { background-image: radial-gradient(700px 420px at 18% 40%,
  color-mix(in srgb, var(--poster-tint) var(--tint-a), transparent), transparent 72%); }
.wu-hero-art .poster-img, .wu-hero-art .poster-ph { border-radius: var(--radius-poster); box-shadow: var(--shadow-2); }
.wu-eyebrow { font-size: var(--fs-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--amber); }
.wu-arch { font-size: clamp(30px, 5.4vw, 56px); line-height: 1.02; font-weight: 800; letter-spacing: -.02em; margin: 8px 0 10px; }
.wu-sub { font-size: var(--fs-lg); color: var(--text2); max-width: var(--measure); }
.wu-share-line { font-style: italic; color: var(--text2); margin-top: 8px; }
.wu-hero-actions { margin-top: 18px; }
.wu-hero.is-reveal .wu-arch { animation: wu-rise var(--dur-3) var(--ease-spring) both; }
.wu-hero.is-reveal .wu-sub { animation: wu-rise var(--dur-3) var(--ease-spring) .12s both; }
@keyframes wu-rise { from { opacity: 0; transform: translateY(14px); filter: blur(4px); } }

.wu-bento { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin: 18px 0 26px; }
.wu-tile { background: var(--glass); backdrop-filter: var(--glass-blur); border: 1px solid var(--hairline);
  border-radius: var(--radius-lg); padding: 16px 18px; box-shadow: var(--shadow-1), var(--specular); min-width: 0; }
.wu-span-hours { grid-column: span 4; grid-row: span 2; }
.wu-span-split, .wu-span-clusters { grid-column: span 4; }
.wu-span-poster, .wu-span-purge { grid-column: span 3; }
.wu-span-sm { grid-column: span 2; }
.wu-tile-lbl { font-size: var(--fs-xs); letter-spacing: .1em; text-transform: uppercase; color: var(--text3); margin-bottom: 8px; }
.wu-tile-title { font-weight: 700; font-size: var(--fs-lg); }
.wu-big { font-size: clamp(30px, 4vw, 46px); font-weight: 800; color: var(--amber); line-height: 1; font-variant-numeric: tabular-nums; }
.wu-unit { font-size: .42em; font-weight: 600; margin-left: 4px; color: var(--text2); }
.wu-year { letter-spacing: -.02em; }
.wu-meta { font-size: var(--fs-sm); color: var(--text2); margin-top: 6px; }

.wu-clock { display: grid; grid-template-columns: repeat(24, 1fr); gap: 3px; height: 92px; align-items: end; margin: 16px 0 4px; }
.wu-hr { height: 100%; display: flex; align-items: end; }
.wu-hr-fill { display: block; width: 100%; height: var(--v); min-height: 2px; border-radius: 3px 3px 1px 1px;
  background: color-mix(in srgb, var(--amber) 35%, transparent); }
.wu-hr.is-peak .wu-hr-fill { background: var(--amber); }

.wu-split { display: flex; height: 14px; border-radius: 999px; overflow: hidden; background: var(--bg3); margin: 10px 0; }
.wu-split-hb, .wu-split-cf { display: block; width: var(--v); }
.wu-split-hb { background: var(--amber); }
.wu-split-cf { background: color-mix(in srgb, var(--amber) 40%, var(--bg3)); }
.wu-split-ot { flex: 1; }
.wu-split-legend { display: flex; justify-content: space-between; gap: 12px; font-size: var(--fs-sm); color: var(--text2); }

.wu-poster-tile { display: flex; gap: 14px; align-items: center; }
.wu-vs { display: flex; gap: 18px; margin-top: 8px; font-size: var(--fs-sm); color: var(--text2); }
.wu-vs b { display: block; font-size: var(--fs-2xl); color: var(--amber); }
.wu-vs span + span b { color: var(--text2); }

.wu-decades { display: flex; gap: 4px; align-items: end; height: 46px; margin-top: 10px; }
.wu-decades span { flex: 1; display: flex; flex-direction: column; justify-content: end; height: 100%; }
.wu-dec-fill { display: block; height: var(--v); min-height: 2px; background: var(--amber); border-radius: 2px; }
.wu-decades em { font-style: normal; font-size: 10px; color: var(--text3); text-align: center; margin-top: 3px; }

.wu-cluster { display: grid; grid-template-columns: minmax(0, 1.4fr) 2fr auto; gap: 10px; align-items: center; margin: 6px 0; }
.wu-cl-bar { height: 8px; background: var(--bg3); border-radius: 999px; overflow: hidden; }
.wu-cl-fill { display: block; height: 100%; width: var(--v); background: var(--amber); }
.wu-cl-trend { font-size: var(--fs-sm); font-variant-numeric: tabular-nums; color: var(--text2); }
.wu-cl-trend.is-rising { color: var(--amber); }

.wu-chapters { display: grid; gap: 14px; }
.wu-chapter { display: grid; grid-template-columns: 200px 1fr; gap: 22px; padding: 20px; border-radius: var(--radius-lg); }
.wu-fan { position: relative; height: 140px; }
.wu-fan-item { position: absolute; top: 0; transition: transform var(--dur-2) var(--ease-spring); }
.wu-fan-item:nth-child(1) { left: 0; transform: rotate(-6deg); }
.wu-fan-item:nth-child(2) { left: 36px; transform: rotate(-2deg); }
.wu-fan-item:nth-child(3) { left: 72px; transform: rotate(2deg); }
.wu-fan-item:nth-child(4) { left: 108px; transform: rotate(6deg); }
@media (hover: hover) { .wu-chapter:hover .wu-fan-item { transform: rotate(0) translateY(-3px); } }
.wu-ch-n { font-size: var(--fs-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--amber); }
.wu-ch-title { font-size: var(--fs-xl); margin: 4px 0; }
.wu-ch-kicker { color: var(--text2); font-size: var(--fs-sm); }
.wu-ch-text { margin: 10px 0 12px; max-width: var(--measure); line-height: 1.55; }

.wu-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 14px 0; }
.wu-roast, .wu-toast { border-radius: var(--radius-lg); padding: 18px; border: 1px solid var(--hairline); background: var(--glass); }
.wu-roast { border-left: 3px solid var(--text3); }
.wu-toast { border-left: 3px solid var(--amber); }
.wu-hook { display: flex; gap: 16px; align-items: center; padding: 18px; border-radius: var(--radius-lg); }
.wu-note, .wu-empty { color: var(--text3); font-size: var(--fs-sm); margin: 12px 0; }

.wu-ticker { list-style: none; display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 0; margin: 0; font-size: var(--fs-sm); color: var(--text3); }
.wu-ticker li.is-done { color: var(--text2); }
.wu-ticker li.is-done::before { content: '✓ '; color: var(--amber); }
.wu-ticker li.is-now { color: var(--amber); animation: wu-pulse 1.4s ease-in-out infinite; }
@keyframes wu-pulse { 50% { opacity: .45; } }

.wu-snap { max-width: 420px; aspect-ratio: 9 / 16; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column;
  gap: 14px; border-radius: 22px; box-shadow: var(--shadow-3), var(--specular);
  background: radial-gradient(500px 360px at 20% 0%, color-mix(in srgb, var(--amber) 16%, transparent), transparent 70%), var(--bg2); }
.wu-snap .wu-arch { font-size: 38px; }
.wu-snap-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.wu-num-static { font-size: 26px; font-weight: 800; color: var(--amber); font-variant-numeric: tabular-nums; }
.wu-snap-posters { display: flex; gap: 6px; }
.wu-snap-top { margin: 0; padding-left: 18px; color: var(--text2); font-size: var(--fs-sm); }
.wu-snap .btn { margin-top: auto; align-self: flex-start; }

@media (max-width: 900px) {
  .wu-bento { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .wu-span-hours, .wu-span-split, .wu-span-clusters { grid-column: span 4; }
  .wu-span-poster, .wu-span-purge, .wu-span-sm { grid-column: span 2; }
}
@media (max-width: 640px) {
  .wu-hero { grid-template-columns: 1fr; padding: 20px; gap: 16px; }
  /* The poster yields to the type, but stays loaded (visually hidden, not
     display:none) so posterLoaded still lends the hero its tint. */
  .wu-hero-art { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  .wu-bento { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .wu-span-hours, .wu-span-split, .wu-span-clusters, .wu-span-poster, .wu-span-purge { grid-column: span 2; grid-row: auto; }
  .wu-span-sm { grid-column: span 1; }
  .wu-chapter { grid-template-columns: 1fr; }
  .wu-fan { height: 132px; }
  .wu-pair { grid-template-columns: 1fr; }
  .wu-snap { aspect-ratio: auto; min-height: 80vh; }
  .wu-snap-btn { margin-left: 0; }
}
```

Reduced motion needs nothing extra: the existing global rule shrinks
durations, and `countTo()`/`haptic()` already return early.
On phones the hero poster is visually hidden but still loaded, so the tint
survives (`loading="lazy"` on a 1px element: verify on iOS Safari that it
still loads before relying on it).

---

## 4. Open points (defaults until the owner decides)

| # | Question | Default in this design |
|---|---|---|
| 7 | Tone & language | English, dry wit; roast stays affectionate (household members read their own only). A `WRAPUP_LANGUAGE` setting can swap the system prompt's language line later |
| 8 | Share as image | v1: snapshot card + Web Share text / clipboard. PNG export (canvas drawing, CSP-safe, no html2canvas) is a v2 item |
| — | Household comparison ("top 10% of the house for anime") | Off. Single household, few people: rank statements leak each other's viewing. Opt-in later if wanted |
| — | Drift thresholds | 0.15 / 0.35 are guesses; PR 3 logs the real month-over-month distribution before showing the words |
| — | Music eras | Tracks carry no release year in the DB; music gets no Viewing Age until album years are stored |

## Sources (market benchmark)

- [Axios — Spotify Wrapped 2025 rolls out clubs, rankings and listening age](https://www.axios.com/2025/12/03/spotify-wrapped-2025-new-features)
- [Android Central — Spotify Wrapped 2025: listening age, top albums](https://www.androidcentral.com/apps-software/spotify/spotify-wrapped-2025-is-here)
- [TechRadar — Spotify Wrapped 2025 live](https://www.techradar.com/news/live/spotify-wrapped-2025-live)
- [Letterboxd Wrapped 2025 (user write-up)](https://cmrosens.com/2025/12/12/letterboxd-wrapped-2025/)
- [PCWorld — Steam Replay 2025](https://www.pcworld.com/article/3015881/steam-replay-2025-is-here-see-your-year-on-steam-stats-and-all.html)
- [GamingOnLinux — Steam Replay: 14 % of playtime on 2025 releases](https://gamingonlinux.com/2025/12/steam-replay-is-live-and-notes-only-14-of-playtime-spent-by-all-steam-users-was-for-2025-releases)
- [Wrapperr (GitHub)](https://github.com/aunefyren/wrapperr) · [Unraid listing](https://ca.unraid.net/apps/wrapperr-0tvh0zj1xbkbyr)
