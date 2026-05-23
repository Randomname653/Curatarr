# Curatarr — Architecture & Knowledge Base

> Living technical reference for anyone modifying the code. README.md is for
> *running* Curatarr; this file is for *understanding and changing* it. It
> captures the data flow, the non-obvious design decisions, and the tribal
> knowledge that isn't visible from any single file — the kind of thing you'd
> otherwise only learn by re-debugging a problem someone already solved.
>
> When you change a subsystem, update the matching section here. The
> `CHANGELOG.md` records *what* changed pass-by-pass; this file records the
> *current* mental model.

---

## 1. One-paragraph overview

Curatarr is a single-tenant FastAPI app that sits between a Plex server, the
*arr stack (Radarr/Sonarr/Lidarr), external metadata APIs (TMDB, OMDb,
AniList, MusicBrainz, Last.fm, Spotify, Jikan), and a locally-hosted Ollama
LLM. It pulls each Plex user's watch history, enriches every title with
metadata + an LLM-written profile, embeds those profiles into a vector store,
and uses the resulting per-user "taste vector" to (a) recommend new media,
(b) propose deletions of media that no longer fits, and (c) hold a
character-driven chat about any of it. Everything runs locally; no library
data leaves the machine except the metadata-API lookups (movie/artist IDs)
that the enrichment pipeline needs.

---

## 2. Process lifecycle

`src/main.py` is the entry point. On startup (`lifespan`):

1. Creates `data/chromadb/` + `data/cache/` dirs.
2. `init_db()` — creates tables + runs idempotent column/table migrations
   (`src/database/connection.py`).
3. `start_scheduler()` — registers APScheduler cron jobs (see §13).
4. **Resets stuck runtime flags** — `enrichment_running`,
   `music_pipeline_running`, `music_pipeline_stop_requested` → `"0"`, and
   seeds `game_active` with the *actual* current game state (not a blind 0,
   which left a race window). This is the recovery path for a crash that
   left a flag set.
5. `prewarm_arr_caches()` — loads the ARR library cache from disk (L2→L1)
   and fires background refreshes (see §12).
6. Mounts routers (each under `/api/<name>`).

`apscheduler.executors.default` is bumped to WARNING level so the per-job
"Running/executed" chatter doesn't drown the logs (the Game-mode watcher
fires every 30 s).

---

## 3. Data model (`src/database/models.py`)

SQLite via SQLAlchemy 2.0, WAL mode. The tables that matter:

| Table | Purpose | Notes |
|---|---|---|
| `users` | Accounts | `is_admin`, `is_active` (soft-disable, never DROP) |
| `watch_history` | Every playback event | per Plex `accountID`. `source` = plex/spotify/manual. `plex_item_id` rewritten from `spotify:track:…` to a Plex key when Phase 1 matches. `artist_mbid` filled by Phase 1.4. `genres` filled by Phase 1.5/2. |
| `enrichment_status` | One row per enriched item | THE pipeline state table. `plex_rating_key` is the UNIQUE key. See §5 for the six states encoded in `(enriched, error)`. Phase-2 columns (Pass 99-fu13, #37): `fetch_tier` (`"fast"`/`"full"`/NULL=full-legacy), `sources_state` (JSON of per-API status + timestamp), `provisional` (denormalised "still upgradable" flag). Read-side default for NULL `fetch_tier` is "full" — back-compat with pre-fu13 rows. Index `idx_es_fetch_tier` keeps the upgrade-scheduler query (#41) cheap. |
| `arr_enrichment_status` | ARR-side enrichment mirror | written for items that have an `arr_id`+`service` |
| `media_identity` | Cross-ref ID store | `plex_rating_key` → tmdb/tvdb/anilist/anidb/mal/imdb. Canonical ID override for the enricher. |
| `taste_vectors` | Per-user taste (legacy/plain) | genre/actor/director affinity JSON + `summary_text` |
| `encrypted_taste_vectors` | Per-user **per-category** taste | the active one. `encrypted_blob` is plain JSON in "Phase A" (unencrypted); PIN-based AES-GCM is "Phase B" (opt-in, mostly unwired). One row per `(user, media_category)`. |
| `deletion_proposals` | Deletion candidates | `id` is AUTOINCREMENT (Pass 90c — never reuse ROWIDs, see §15). `status` pending/approved/rejected/deleted/superseded. |
| `cached_recommendations` | Generated recs | wiped + replaced per category on regen |
| `proactive_messages` | Curator-initiated nudges | `trigger_type`, `read` flag |
| `conversation_messages` | Chat history | `thread_id` isolates topics: `general` / `deletion_proposal:{id}` / `proactive_message:{id}`. Drives language detection. |
| `episodic_memories` | Extracted taste/feedback/protection memories | `memory_type`, `media_category`, vector-searched globally per user |
| `protected_media` | Deletion whitelist | `identifier` = TMDB/AniList id OR exact title |
| `curator_resolution_log` | Audit trail | consensus-vs-override classification of deletion decisions |
| `app_state` | Runtime KV flags | DB-backed so they survive restarts. See §11. |
| `plex_rating` | Per-user Plex star ratings | music-only signal for deletion hard-protect (Pass 82c) |

---

## 4. Storage layers

There are **four** distinct stores. Conflating them is the source of several
historical bugs.

1. **Main DB** — `data/curatarr.db` (SQLite WAL). All tables above.
   Connection pragmas: `journal_mode=WAL`, `synchronous=NORMAL`,
   `busy_timeout=60000`. `get_db_session()` context manager commits on exit.

2. **Enrichment / API cache** — `data/cache/enrichment.db`, separate SQLite,
   wrapped by `src/cache/metadata_cache.py` (`MetadataCache`). Single table
   `api_cache(cache_key, response, created_at, expires_at)`. Every key is
   auto-prefixed with `_CACHE_VERSION` (currently `"v2"`). Bump
   `_CACHE_VERSION` only when the *meaning* of cached values changes — old
   rows then become invisible (read-time `expires_at` filter + version
   prefix). There's a Pass-95 read-only fallback to the un-versioned key
   for one legacy migration case. See §12 for the key namespaces.

3. **Vector store** — ChromaDB at `data/chromadb/`, wrapped by
   `src/vector_store/chromadb_wrapper.py`. One document per enriched item,
   id = `{service}:{arr_id}` or the plex/tmdb id. `count_by_id_prefix()`
   gives per-service coverage. Embeddings from `nomic-embed-text` via
   `src/embeddings/embedding_generator.py`. **Gotcha:** ChromaDB returns
   embeddings as numpy arrays — never test them with `if embedding:`
   (raises on multi-element array); use `is not None` (Pass 74).

4. **AppState flags** — rows in the main DB's `app_state` table, via
   `src/services/app_state.py`. `get_state`/`set_state` route through
   SQLAlchemy; `force_set_state` (Pass 89b) opens a *fresh* sqlite3
   connection with a 120 s busy-wait and `isolation_level=None` to write
   even when the SQLAlchemy engine is in a write-lock cascade — the
   cleanup path that prevents permanently-stuck flags.

---

## 5. The enrichment pipeline (`src/routers/enrichment.py` + `src/services/media_enricher.py`)

The most intricate subsystem. Read this section before touching either file.

### 5.1 The seven EnrichmentStatus states

`(enriched, error, provisional)` encodes a mutually-exclusive state,
surfaced in the breakdown panel (§ recommendations UI) with explainers:

| State | DB condition | Meaning |
|---|---|---|
| **LLM-polished** | `enriched=True, error IS NULL, (provisional=0 OR NULL)` | Full canonical fetch + LLM profile written. Terminal. |
| **Enriched (provisional)** | `enriched=True, error IS NULL, provisional=1` | Phase-2 fast-tier (Pass 99-fu13 / #40) — only the cheap sources contributed (Last.fm for music, no Jikan/OMDb/TMDB supplement). The #41 hourly source-upgrade scheduler promotes 30 of these per hour to LLM-polished. |
| **Rule-based** | `enriched=True, error LIKE 'rule_based%'` | Heuristic fallback, LLM upgrade pending. 1-day cache TTL. |
| **Awaiting LLM** | `enriched=True, error LIKE 'api_cached%'` | API data persisted, LLM paused (game-mode or standalone music_enricher's default marker). |
| **Not findable** | `enriched=True, error LIKE 'Not found%'` | All APIs missed. 3-day sentinel TTL. |
| **Queued for retry** | `enriched=False, error IS NULL` | Tracked but not (re-)processed yet. Freshly seeded OR admin-reset. |
| **Processing error** | `enriched=False, error IS NOT NULL` | Pipeline crashed mid-item AND recorded an error string (rare). |
| *Never processed* | no row at all | denominator − tracked. |

`_write_enrichment_db()` is the single writer. The breakdown endpoint
(`src/routers/library.py::library_breakdown`) classifies via a SQL CASE
expression matching the table above. The provisional case is checked
BEFORE the generic `error IS NULL → llm_polished` so a fast-tier row
gets its own bucket; legacy rows have `provisional=0` (migration
default) and fall through to LLM-polished as before.

### 5.2 Producer / consumer

`/api/enrichment/start` builds an item list, then runs a producer + consumer
across **per-category** `asyncio.Queue`s (no longer a single shared FIFO):

- **Item collection**: watch_history rows (if source includes `watch_history`)
  + ARR collect (if source includes `arr`). Dedup by lowercased title per
  category. Then a pre-filter drops items already `enriched=True, error IS
  NULL`. Then a priority sort moves never-enriched fresh items to the front.
- **ARR collect** (`_collect_arr_items`): 60 s timeout (Pass 99-fu7 — the
  full `/api/v3/movie` + `/api/v1/artist` payloads are 15-30 MB and time
  out at the old 10 s under load). Failures use `_format_arr_error` so the
  log isn't an empty string. Radarr items carry `imdb_id` (enables
  OMDb-primary, §5.3); Sonarr items currently do not (known gap — shows
  fall back to TMDB-primary).
- **Producer** = **per-category fetch lanes** (Pass 99-fu10/fu11). One
  worker group per category, sized to each upstream's concurrency cap:
  `movie`=4, `show`=2, `anime`=1, `music`=1. Each worker calls
  `_process_one(pitem)`; per-service caps live in `media_enricher.py`
  (§5.6). WHY lanes: a single mixed 8-worker pool let the **66%-music**
  library pile ~6/8 workers onto the MusicBrainz `Semaphore(1)` (MB enforces
  ~1 req/sec) while TMDB (movie/show, 16 concurrent) sat idle → throughput
  collapsed to ~4/min. WHY *gentle* sizes (fu11): the pre-fu11 `movie=8 /
  show=4` overran TMDB's real ~5 req/sec → 429s → the 5-consecutive-transient
  guard aborted the movie+show categories (only music survived). With
  OMDb-primary (fu11), movie/show fetch goes through OMDb (supporter key,
  100 k/day, semaphore 10) so TMDB only **supplements** (and a TMDB 429 no
  longer kills the run). `anime` stays at 1 worker because AniList's
  `Lock(1)` + rate limit would otherwise hit the same abort.
  `_process_one`'s per-category abort/rolling/streak state is
  order-independent, so the lanes need no change there.
- **Per-category output queues + round-robin consumer** (Pass 99-fu12).
  Each lane writes into its OWN `asyncio.Queue(maxsize=_PREFETCH_DEPTH)`
  (`cat_queues[cat]`), and a `lanes_done: set` tracks lane completion in
  place of a sentinel. The consumer is a single LLM coroutine but rotates
  fairly: each pass walks `active_cats`, pulls up to `_CONSUME_BATCH = 10`
  items per category (skipping empty queues), processes them serially, then
  rotates. It exits when all lanes are in `lanes_done` AND every queue is
  empty; a short `asyncio.sleep(0.25)` covers the idle case while lanes are
  still feeding. WHY round-robin: the pre-fu12 single FIFO queue meant the
  music lane (instant raw-cache hits → puts a music item every few ms) filled
  the queue before movie/show/anime could land an item. The consumer drained
  in arrival order, so it saw a 10-30 minute pure-music run while the fast
  lanes' producers eventually blocked on `queue.put` (back-pressure from a
  full queue) and stopped fetching → all four categories starved at once.
  Per-category queues let each lane's back-pressure stay local; round-robin
  guarantees movie/show/anime get LLM time the moment they have anything
  ready. Observed effect: cat mix moved from "music-only" to roughly
  proportional within ten minutes of restart (`anime 26 / movie 17 / show 18
  / music 15` per 10-min window at ~7.6/min).
- **Consumer is the throughput ceiling, measured at 14.2 items/min warm**
  (parallel-bench, 2026-05 — 100 varied raw-cache payloads, single coroutine
  through `summarize_with_small_llm`). Mean per-item: 4.24 s = granite ~3.2 s
  + embedding + db/chroma writes. Prod runs at ~7-13/min sustained — the
  ~1-7/min gap is the consumer occasionally idling while the producer waits
  on a slow API. **Don't try to parallelise** it: the same benchmark proved
  the workload is compute-bound on a single GPU (N=2 = ×1.02 throughput
  but ×1.94 mean latency; N=3 = ×1.15 throughput but ×2.55 mean latency).
  See §15 "Don't parallelise the summariser consumer". **Not** a VRAM
  problem — granite (~7 GB) + nomic (~0.6 GB) leave ~16 GB free.
- **Two hard throughput floors** for a full re-enrich: the single consumer
  (~14/min warm ⇒ ~30-50 h for ~28 k items) and the MusicBrainz 1-req/sec
  cap (~17 k uncached music artists ⇒ ~12-24 h, irreducible). Lanes +
  round-robin make music non-blocking, not faster. The only way to push
  past the ~14/min consumer floor is a *faster model* (granite4.1:3b is
  measured 100% JSON-OK + ~1.5× faster but drier prose — Phase-2 candidate
  as a fast-tier summariser).

### 5.3 `fetch_and_prepare_raw` — the two-tier cache + tri-state return

This function (in `media_enricher.py`) is the heart of the cache logic. It
returns one of THREE shapes the producer must distinguish:

1. `{"_already_enriched": True, "_cached_profile": …}` — the polished cache
   (`enriched:{cat}:{id_key}`) holds an LLM profile tagged with the current
   `_PROMPT_VERSION`, OR a fresh (<3 day) not_found sentinel. Producer
   reconciles the EnrichmentStatus row to match, does NOT re-fetch or
   re-LLM, does NOT write a not_found sentinel.
2. A normal raw dict — fresh API data (or a tier-2 `raw:{cat}:{id_key}`
   cache hit). Producer queues it for the consumer's LLM polish.
3. `None` — no API data exists. Producer writes a `not_found` sentinel.

**Why three shapes** (Pass 99-fu / fu2): the pre-fu version returned `None`
for both "already done" and "no data". After a bulk EnrichmentStatus reset,
items whose polished cache was still valid returned `None` → producer wrote
not_found sentinels over perfectly-good items (200 poisoned in 3 s). The
explicit `_already_enriched` marker fixed that.

**OMDb-primary for movie/show** (Pass 99-fu11). When the item has an
`imdb_id` AND is not anime, `fetch_and_prepare_raw` tries OMDb **first**
(`fetch_omdb_data` was extended to return the full core profile —
`title / year / media_type / genres / overview / director / cast /
runtime_min / rating / vote_count / imdb_id`, not just the supplement
fields). On a successful OMDb response (`title` + `overview` present), `raw`
is built from OMDb and `omdb_was_primary=True`. The TMDB chain is then run
as a **429-safe supplement** under `try/except TMDBTransientError`:
poster/keyword/credits enrichment is best-effort, but a TMDB 429 no longer
poisons the item or aborts the category. WHY: with the supporter OMDb key
(100 k req/day, semaphore 10) we can fetch movie/show without crowding
TMDB's ~5/sec limit, which is what made the fu10 movie=8/show=4 lanes
trigger the abort guard. Anime is unaffected (still AniList-primary,
OMDb adds nothing AniList doesn't already have). Sonarr currently does
not surface `imdb_id` in collected items — `show` therefore falls back to
the TMDB-primary chain. Closing that gap is a Phase-2 task.

**Fast vs Full tier** (Phase 2 #38b). `fetch_and_prepare_raw` accepts
`fast_only: bool = False`. In **fast** mode the slow per-category
upstream is bypassed: music skips MusicBrainz (uses Last.fm only —
~5 req/s instead of MB's 1 req/s, the music-lane killer); anime skips
the Jikan supplement; movie/show skip the OMDb-supplement and the
TMDB-supplement (whichever primary worked is enough). Each item lands
`fetch_tier="fast"` + `provisional=True` on the EnrichmentStatus row;
the **source-upgrade scheduler (#41)** scans for `provisional=True`
later and runs a full re-fetch in the background to fill in the
missing sources. The DB column `sources_state` records every source
consulted with status `ok` / `miss` / `transient` / `skipped` and a
timestamp, so the scheduler can target exactly the sources still
pending. WHY: the parallel-benchmark (§15) proved the LLM consumer
ceiling is ~14/min — the throughput-bound is producer-side waiting on
slow APIs. Skipping MB on bulk-pass slashes the 16 k-music lane from
~4 h to ~1 h, getting every item to provisional "enriched temporary"
state quickly so the user gets coverage; full quality follows in
background. Two transport fields ride with raw across the pipeline:
`_fetch_tier` (`"fast"` / `"full"`) and `_provisional` (`True` only
for fast rows). The raw cache stamps `_tier_at_fetch` into the cached
blob so a future `fast_only=False` request seeing a fast-cached blob
bypasses the cache and re-fetches (otherwise the upgrade pass would
just re-read the same stale fast data). The polished cache (tier 1)
applies the same tier-mismatch check on read.

### 5.4 Two-tier cache + prompt versioning (Pass 99-fu2)

- `raw:{cat}:{id_key}` — pure API fetch result (TMDB/AniList/MB/Last.fm
  merged). 90-day TTL. **Survives prompt changes.** Written by
  `_write_raw_cache()` after every fresh fetch.
- `enriched:{cat}:{id_key}` — LLM-polished profile, tagged with
  `profile["prompt_version"] = _PROMPT_VERSION`. 90-day TTL.

**`_PROMPT_VERSION` (in media_enricher.py)**: bump this whenever the curator
or summariser system prompts change — OR the summariser model changes — in a
way that should invalidate cached profiles. Next run treats version-mismatched
polished entries as "needs re-polish", falls through to the tier-2 raw cache
(no API re-fetch), and re-runs only the LLM. A bump costs LLM-compute, not API
quota. Currently `v2` (bumped from `v1` on the gpt-oss:20b → granite4.1:8b
summariser swap, Pass 99-fu9).

**A bump alone does NOT re-polish already-LLM-done items.** The `enrichment.py`
Step-5 pre-filter skips rows with `EnrichmentStatus.enriched=True AND error IS
NULL` *before* the producer ever consults the cache version. So a bump only
re-polishes what the producer actually sees (never-enriched + rule-based/failed
rows). To re-polish the WHOLE library after a bump, run with `force=True` (the
"🔄 Force Re-Enrich" button) — it bypasses the pre-filter; the raw cache still
spares the API calls. **Two independent skip layers must BOTH yield**: cache
`prompt_version` (producer side) + `EnrichmentStatus.enriched` (pre-filter side).

`id_key` = `anilist_id or anidb_id or tmdb_id or tvdb_id or title[:40]`.
**Gotcha (historical):** `_write_enrichment_db` also writes
`enriched:{cat}:{plex_rating_key}` — a DIFFERENT key namespace that the read
path doesn't consult. Those writes are effectively dead; the canonical
read/write key is the id_key form. Don't rely on the plex_rating_key cache
entries.

### 5.5 Transient errors + abort thresholds (Pass 99)

- **`TMDBTransientError`** (`media_enricher.py`): `_tmdb_get` raises this on
  429 / 5xx / network errors (NOT on 4xx-other-than-429, which is a real
  "not found"). The producer catches it, sleeps `retry_after_s`, and SKIPS
  the item without writing a sentinel — the row stays queued for the next
  run. The pre-99 silent `return {}` on any non-200 was indistinguishable
  from a real miss and poisoned ~5,800 movies during a TMDB blip.
- **50%-not_found abort**: a per-category rolling window (last 50 items); if
  >50% are not_found sentinels, the producer aborts that category — almost
  certainly an upstream API outage, not a real findability problem.
- **5-consecutive-transient abort**: same idea for durable API-down.
- Aborted categories leave their items at `enriched=False` for the next run.

### 5.6 Per-service concurrency caps (Pass 99-fu3)

Module-level in `media_enricher.py`, lazy-init via
`_ensure_concurrency_primitives()`:

- `_SEM_TMDB = 16`, `_SEM_OMDB = 10` (raised from 4 in Pass 99-fu11 when
  OMDb became primary for movie/show under a supporter key — 100 k req/day
  ≫ free-tier 1 k), `_SEM_JIKAN = 2`, `_LOCK_ANILIST` (Lock, serialises
  through the existing `_anilist_wait` throttle).
- MusicBrainz already has its own `_MB_SEM = Semaphore(1)` in
  `music_metadata.py` (1 req/s strict).
- These keep the lane workers (§5.2 — movie 4, show 2, anime 1, music 1)
  within each API's rate limit.

---

## 6. The music pipeline (`src/services/music_matcher.py`)

Sequenced phases, all idempotent + resumable. Run daily by the scheduler OR
via the standalone scripts.

| Phase | What | Writes |
|---|---|---|
| **1 — Plex Match** | Spotify plays whose track exists in local Plex get `plex_item_id` rewritten from `spotify:track:…` to the Plex key | `watch_history.plex_item_id` |
| **1.4 — MBID Resolve** | Distinct unmatched Spotify artists get a MusicBrainz MBID | `watch_history.artist_mbid` |
| **1.5 — Spotify Genres** | Genres for unmatched plays via Spotify Client-Credentials | `watch_history.genres` |
| **2 — Last.fm Fallback** | Genres for anything Spotify couldn't resolve | `watch_history.genres` |

- **Standalone runners**: `scripts/music_enricher.py` (full music enrichment,
  bypasses the daily batch cap) and `scripts/mbid_speedrunner.py` (Phase 1.4
  only). Both LLM-free by default — they write the `api_cached — LLM pending`
  marker so the in-app enrichment consumer does the LLM polish later.
- **Coordination**: both set `music_pipeline_running="1"` in AppState while
  active. As of Pass 97 they do NOT also block on `enrichment_running` —
  the cross-mutex was removed (chunked commits + disjoint DB rows make the
  two safe to run concurrently). They self-check `music_pipeline_running`
  only (two music runners would collide).
- **Chunked commits** (Pass 89): `enrich_music_genres_spotify` commits every
  100 tracks (`_DB_COMMIT_CHUNK`) so a long run doesn't hold the SQLite
  write-lock for minutes and starve other writers.
- **ext-script rows**: Spotify-seeded artists with no Plex section get
  `plex_rating_key` prefix `ext-script:`. They're excluded from per-library
  "% enriched" counts (Pass 91b) but still feed the music taste vector.
- **Spotify dev-mode quota** (Pass 99-fu8): a Spotify app in Development
  Mode (the default) has a tiny quota; a bulk Phase-1.5 run blows it and
  Spotify returns a flat 24 h ban (`retry-after: 86400` on the data
  endpoints, token endpoint still 200). `spotify_client.py` persists a
  long retry-after to AppState (`spotify_backoff_until`, wall-clock,
  survives restarts) and skips Spotify entirely until it elapses — re-
  knocking during the ban can reset the 24 h window, so silence is what
  lets it expire. Last.fm (no such quota) carries Phase-1.5/2 genre work
  meanwhile. Short transient 429s still use an in-process monotonic
  backoff. To actually fix the quota: apply for Spotify Extended Quota
  Mode (slow, uncertain since the 2025 policy change) — or just rely on
  Last.fm, which is plenty for taste-vector genres.

---

## 7. Taste engine (`src/services/taste_engine.py`)

Reads watch history + enriched profiles + episodic memories + explicit
feedback. Produces a per-user, per-category embedding centroid (stored in
`encrypted_taste_vectors`) plus a written summary line injected into every
curator prompt. **Memory carry-over**: on recompute, prior aversions
(genre/theme/mood) and disliked titles are merged with the strongest value
preserved — recomputing doesn't wipe learned signal. Recompute triggers on
demand or after `recs_invalidate_at` bumps (any Plex sync that wrote rows,
any enrichment that finished items).

`taste_vectors.py` is the legacy module; `taste_engine.py` is the active one.

---

## 8. Recommendations + deletion proposals (`src/services/recommendations_engine.py`)

- **Recommendations**: vector-similarity search over ChromaDB scoped to the
  category (the `domain` metadata filter prevents cross-category bleed —
  show recs never surface anime, etc.). Each rec gets a written curator
  pitch. Cached in `cached_recommendations`.
- **Deletion proposals** (`generate_deletion_proposals`): two phases.
  - **Phase A — scoring**: per item, compute a deletion score from vector
    mismatch (dominant), file size (log-scaled), external rating swing, and
    (music only) the user's own Plex star rating. Hard-skips: protected
    media, titles the curator positioned on in chat (last 60 d), recently
    watched (last 90 d), kids/family content, ≥4-star user rating (music).
  - **Phase B — LLM pitches**: top 10 candidates per category get a written
    deletion pitch from the curator model. This is the expensive part
    (~40 LLM calls per full regen) and is what makes "ARR Sync" contend
    with the enrichment summariser for the LLM.
  - `monitor_task` kwarg (Pass 99-fu5) lets the scheduler thread the
    task_monitor through for per-phase / per-pitch progress messages.
- **numpy gotcha** (Pass 74): item embeddings are numpy arrays — guard with
  `is not None`, never truthiness.

---

## 9. Chat + curator (`src/routers/chat.py`, `src/services/episodic_memory.py`)

- **Thread isolation**: `conversation_messages.thread_id` keeps free chat,
  per-proposal discussions, and per-message discussions separate.
- **discuss_context** (Pass 90a): when the frontend sends a proposal_id +
  title, the server re-fetches the proposal from the DB and verifies the
  title matches before injecting context — defends against stale frontend
  references resolving to the wrong title after ID reuse.
- **Single-entity extraction**: each chat message resolves ONE primary title
  anchor (regex + LLM extraction, biased toward titles with user-state in
  the DB). The cascade then fetches metadata for that one title. This is why
  pasting a list of 100 titles gets one answered, not all 100.
- **Language detection** (`llm_utils.detect_user_language`, Pass 99-fu6):
  density-based — trips `de` only on 2+ umlauts, OR 1 umlaut + 2 tokens, OR
  5+ distinct tokens at ≥1 token/1000 chars. Replaces the old absolute
  threshold that false-positived on a few German cognates in long English
  text. The detected language is injected into curator/summariser/pitch
  prompts so output matches the user's chat language.
- **Protection-intent detection**: German + English keyword vocabularies in
  `episodic_memory.py` + `llm_utils.py` are INTENTIONAL — they recognise a
  German-speaking user's "behalten / nicht löschen" directives. Do not
  "translate" those to English.
- **German keyboard typo handling** (chat.py): `ß → 0` near digits (a fast
  typist hits ß instead of 0 in years like "202ß" → "2020"). Intentional.

---

## 10. Proactive messages (`src/services/proactive_messages.py`)

The curator polls for triggers (new season of a binged anime, idle-user
picks, long-break check-ins), writes `proactive_messages` rows, the frontend
shows a badge. Per-trigger toggles in Settings → Notifications.
**Gotcha**: `created_at` is forward-dated (`now + timedelta(seconds=…)`) to
force ordering — filters using `created_at <= now` would skip them until
wall-clock catches up.

---

## 11. LLM priority system (`src/services/llm_priority.py`)

Curatarr runs two Ollama model roles that compete for VRAM:

- **Curator** (`curatarr-curator`, large — `qwen3.6:27b` — chat,
  recommendations, deletion pitches)
- **Summariser** (`curatarr-summarizer`, small+fast — `granite4.1:8b` —
  enrichment polish, memory extraction; backup `granite4.1:3b`)

`curator_start()` marks the curator as priority → the summariser is evicted
from VRAM → the enrichment consumer pauses. `curator_done()` releases it.
This is why: (a) chatting with the curator pauses enrichment, AND (b) the
deletion-pitch generation (which uses the curator) ALSO pauses enrichment —
the "Curator active — pausing enrichment" log fires for both.

**Game mode** (`process_monitor.py`): a 30 s scheduler watcher detects known
game processes. When a game is running, both models are evicted and the
enrichment pipeline only does API pre-fetch (`_write_game_mode_db` writes the
`api_cached` marker, no LLM). Resumes automatically on game exit.

`embedding` model (nomic-embed-text, ~0.5 GB) is intentionally NOT managed by
the priority system — too small to matter against the 20-27 GB model dance.

---

## 12. Cache namespaces & TTLs (`MetadataCache`)

All keys auto-prefixed with `_CACHE_VERSION` (`"v2"`).

| Key | Written by | TTL | Purpose |
|---|---|---|---|
| `raw:{cat}:{id_key}` | `_write_raw_cache` after fresh fetch | 90 d | tier-2 raw API data (prompt-bump-safe) |
| `enriched:{cat}:{id_key}` | `process_and_save` / `enrich_media_item` | 90 d (LLM) / 3 d (not_found) / 1 d (rule-based) | tier-1 LLM-polished profile, tagged `prompt_version` |
| `enriched:{cat}:{plex_rating_key}` | `_write_enrichment_db` | same | **dead** — nobody reads this key form |
| `raw_prefetch:{plex_rating_key}` | game-mode consumer | 7 d | API data persisted during game-mode for next-run reuse |
| `arr_library:{svc}` | library.py L2 persist | 30 d | full ARR library blob (L1 in-memory + L2 disk) |

**ARR library cache** (`library.py`): two-tier. L1 = in-process dict (15 min
freshness). L2 = `arr_library:{svc}` MetadataCache rows (30 d). Live fetch
populates both. On a fetch failure, serves stale L2 with a "⚠ cached" badge.
Prewarmed at startup.

---

## 13. Scheduler jobs (`src/services/scheduler.py`)

APScheduler. Defaults:

| Job | Cadence | Notes |
|---|---|---|
| Plex sync | every `SYNC_INTERVAL_HOURS` (24 h) | + startup catch-up if overdue |
| ARR sync (deletion proposals regen) | daily | + startup catch-up. 40 LLM pitches. |
| ARR pre-enrichment | 02:30 daily | **batch-capped at `ARR_PRE_ENRICH_BATCH`=80** — a trickle, NOT a backlog-drainer. To clear a big backlog you must run the manual `/api/enrichment/start` (no cap). |
| Music match + Last.fm | daily | the in-app music pipeline |
| Enrichment TTL refresh | daily | re-queues stale profiles, prefers un-enriched first, preserves `enriched_at` (Pass 86) |
| Source-upgrade pass | hourly (:15) | Phase 2 #41 — promotes 30 oldest `provisional=True, fetch_tier='fast'` rows by re-enriching with `fast_only=False`. Bypasses the watch_history + ARR collect (uses `specific_plex_rating_keys` to target rows directly + `force=True` to defeat the LLM-done pre-filter). No-ops on game-mode or main-enrichment contention. Ceiling is the MB 1-req/s for music — 30/hour matches that pacing. ~22 days to drain ~16 k provisional music items. |
| Proactive cache fill | 30 min | |
| ARR library cache refresh | 30 min | |
| Memory decay | weekly | |
| Orphaned-section check | weekly | |
| DB vacuum | weekly | |
| Game-mode VRAM watcher | 30 s | |

**Startup catch-up gotcha**: a fresh server start fires the overdue ARR-sync
(deletion pitches, ~40 LLM calls) AND the ARR pre-enrich (80 items)
simultaneously. Both use the curator/summariser, so the first ~10 min after
a restart has LLM contention. This is the single biggest "why is enrichment
slow right after restart" cause.

---

## 14. Standalone scripts

| Script | Purpose |
|---|---|
| `import_spotify.py` | Bulk-import Spotify `Streaming_History_Audio_*.json` into watch_history (batch 5000) |
| `run_pipeline_spotify.py` | Manual trigger for the music pipeline |
| `scripts/music_enricher.py` | Standalone music enrichment, bypasses daily cap, LLM-free |
| `scripts/mbid_speedrunner.py` | Standalone Phase 1.4 MBID resolve |
| `build_models.py` | Bake `curatarr-curator` + `curatarr-summarizer` Ollama tags |
| `update_db.py` | Idempotent schema migration (`Base.metadata.create_all`) |
| `benchmark.py` | Ollama model throughput benchmark |
| `scripts/dev/` | ~40 one-shot debug/fix/repair tools, **gitignored** |

---

## 15. Tribal knowledge — invariants that bit us

Things that are non-obvious and have caused real bugs. Don't undo these
without understanding why they exist.

- **`deletion_proposals.id` must stay AUTOINCREMENT** (Pass 90c). Without it,
  SQLite reuses ROWIDs after delete; a stale frontend cache holding an old
  proposal_id then resolves to the WRONG title on a follow-up request (the
  Curator "hallucinated about The Thing when discussing The Internship"
  bug). Combined with soft-delete (`status='superseded'`, Pass 90b) so IDs
  are never freed.
- **`fetch_and_prepare_raw` returns three shapes, not two** (§5.3). Returning
  bare `None` for "already done" re-poisons items as not_found.
- **`_tmdb_get` distinguishes transient (429/5xx) from real-miss (4xx)**
  (§5.5). Silent `return {}` on any non-200 poisons thousands during a blip.
- **Cache key asymmetry** (§5.4): read path uses id_key, one writer uses
  plex_rating_key. The plex_rating_key entries are dead. If you "fix" a
  not_findable bug, clear the id_key cache, not just the plex_rating_key one.
- **ARR collect needs ≥60 s timeout** (Pass 99-fu7): the full-library
  endpoints return 15-30 MB; 10 s times out under load and silently drops
  the entire ARR item set.
- **numpy embeddings**: `is not None`, never truthiness (Pass 74).
- **`force_set_state` for flag cleanup** (Pass 89b): the normal `set_state`
  fails in a write-lock cascade — exactly when releasing a flag matters most.
- **Language-detection keyword lists are intentional** (§9): don't translate
  the German vocabularies to English.
- **ARR pre-enrich is batch-capped at 80** (§13): it will never drain a big
  backlog. Use the manual full enrichment run.
- **Two LLM workloads contend** (§11): enrichment summariser vs curator
  (chat + deletion pitches). 0-throughput enrichment usually = curator busy,
  not a bug.
- **Don't parallelise the summariser consumer — it's compute-bound, not
  I/O-bound** (parallel-bench, 2026-05). The intuitive "run 2 or 3 granite
  slots" idea was measured end-to-end (`auto_benchmark_parallel.py`, 100
  varied raw-cache payloads, `OLLAMA_NUM_PARALLEL` = 1 / 2 / 3 with
  matching `asyncio.Semaphore(N)`): throughput barely moved (×1.00 / ×1.02 /
  ×1.15) while **mean latency scaled linearly with N** (4.24 s → 8.21 s →
  10.84 s) — the classic signature of a compute-bound workload on a single
  GPU. The 4090's tensor cores are already saturated by one granite4.1:8b
  inference at `num_ctx=8192`; adding slots just time-slices the same
  cycles N ways. Memory is NOT the limit (granite ~7 GB + KV ~1.3 GB × N
  fits in 24 GB easily); GPU compute is. **Lesson:** before reaching for
  more parallelism, measure whether the unit is compute- or I/O-bound.
  For LLM inference on a single discrete GPU it's almost always compute,
  and the lever is a faster/smaller model or a better-utilised batch,
  never more concurrent clients.
- **`uvicorn --reload` on this setup is lazy** — file changes are
  detected but the process restart is deferred until a browser-side
  signal (tab close / hard refresh) invalidates the ASGI state. As
  long as a tab keeps the connection warm, the OLD process keeps
  running with its original imports + background coroutines, even if
  the .pyc has been re-compiled by an out-of-band Python import. This
  trapped a half-day during Phase-2 #38a deployment: the new
  instrumentation showed up on disk + worked in isolation but every
  newly enriched DB row had NULL `fetch_tier` for hours because the
  long-running producer coroutine had captured pre-edit function
  references at its 09:14 startup and `--reload` never fired.
  **Defensive rule**: when an edit must take effect on a live
  background task, tell the user to explicitly restart (Ctrl-C +
  start.bat), and verify the new code is live by inspecting a FRESH
  DB row written after the restart — don't trust the `.pyc` mtime
  alone, and don't trust the AppState `enrichment_running` flag
  (which can be stale across reload boundaries).
- **The consumer's real warm ceiling is ~14/min, but prod sees less** —
  the gap is producer-side, not consumer-side (parallel-bench, 2026-05).
  The same benchmark established 14.2 items/min as the *warm* steady-state
  with cache-resident raw data, no DB writes, no API fetches in the loop.
  Prod runs at ~7-13/min sustained. The delta is the consumer occasionally
  idling because the per-category queue is empty while the producer is
  waiting on a slow API (MusicBrainz 1 req/s, OMDb/TMDB network RTT,
  Last.fm). The lever to close that gap is **faster producer-side fetch**
  (Phase 2: fast-only fetch mode, Last.fm-primary music with MB upgrade
  in background, provisional state) — NOT more consumer concurrency.
- **Summariser = `granite4.1:8b`, and valid-JSON ≠ accurate** (model bench,
  2026-05). The summariser is the enrichment-throughput bottleneck. gpt-oss:20b
  ran ~12.6 s/item AND occasionally emitted *invalid* JSON (verbose; hit the
  2600-token cap mid-object). granite4.1:8b: ~3.2 s/item, 97% valid JSON over
  36 hard items, factually accurate + rich prose. **CRITICAL warning:**
  `nemotron-3-nano:4b` is fast and reads beautifully but **hallucinates** — it
  invented "Paul sells refugees to Hutu militias for food" for *Hotel Rwanda*
  and movie-only characters (El Drago/Woonan) for *One Piece*, systematically
  skewing toward "edgy/dark" to sound clever. Never choose a summariser on prose
  vibe; fact-check the `embedding_text` against known titles. `granite4.1:3b` is
  the fast 100%-JSON backup (accurate but drier).
- **The summariser's `num_ctx` is pinned in the Modelfile, not the request**
  (model bench). `build_models.py` → `setup_wizard.create_model` bakes
  `PARAMETER num_ctx 8192` into `curatarr-summarizer`; `ollama_options` sends NO
  num_ctx. That is why production loads lean (~7 GB) instead of at the base
  model's native context (granite default = 131072 → ~74 GB → spills to CPU).
  When benchmarking a candidate as a *raw base model*, you MUST pin
  `num_ctx=8192` or the VRAM + first-item timings are meaningless artifacts.
- **`force=True` does NOT clear LLM profiles** (latent bug; task filed). The
  enrichment.py force-clear `DELETE … LIKE 'enriched:{cat}:%'` omits the
  `_CACHE_VERSION` (`v2:`) key prefix that `MetadataCache.set_cache` adds, so it
  matches zero rows. Force re-enrich therefore re-polishes only because a
  `_PROMPT_VERSION` bump invalidates the profiles by *version*, not because the
  clear deleted them. The emb-cache clear (`%:emb:%`, leading `%`) does work. Fix:
  prefix the pattern with `{_CACHE_VERSION}:`.
- **A numeric `rating` field will crash `_merge_raw_metadata`** if its type
  isn't guarded (Pass 99-fu11). The Jikan supplement put a content-rating
  STRING into `sup["rating"]` (e.g. `"Rx — Hentai"`), and the merge did
  `if "Rx" in mal_rating:` directly. When `fetch_omdb_data` was extended to
  return the full core profile, it added a NUMERIC `rating` (OMDb's
  imdbRating, a float). Suddenly `"Rx" in <float>` raised `TypeError:
  argument of type 'float' is not iterable` — for *every* movie and show
  going through the merge. The producer caught it in the generic `except
  Exception` and logged a one-line "Producer error", so the symptom looked
  like silent failure: anime/movie/show stuck at the same counts forever
  while only pre-cached music flowed. Fix in place: `isinstance(mal_rating,
  str)` guard before any string membership check. **Lesson:** any time a
  fetcher's return shape grows, audit every consumer that does duck-typed
  string ops on its fields — `"X" in field` is a silent type-incompatibility
  trap.
- **A single FIFO output queue lets the fastest producer starve the others**
  (Pass 99-fu12). Cached items return from `fetch_and_prepare_raw` in single
  digits of milliseconds. With 66 % of the library being music and most
  music items hitting the raw cache after one warm-up pass, the music lane
  filled a single shared `asyncio.Queue` faster than the consumer could drain
  it. Movie/show/anime producers eventually blocked on `queue.put` (full
  queue back-pressure) and stopped fetching entirely → the user saw "only
  music in the activity feed" for 10-30 min stretches even though the
  movie/show/anime lanes were healthy. Fix: per-category output queues
  (`cat_queues[cat]`) + a round-robin consumer pulling `_CONSUME_BATCH = 10`
  per category per pass, plus a `lanes_done: set` for termination in place
  of a single sentinel. **Lesson:** when producers have wildly different
  per-item latencies (cache hits vs network round-trips), a shared FIFO
  hides which side is bottlenecked because the back-pressure couples them.
  Per-category queues + fair scheduling decouples the lanes, and the
  per-category cat-mix in the last 10 min becomes the at-a-glance health
  signal ("fair mix → fine, all-one-cat → starvation").

---

## 16. Environment / config (`src/config.py`)

`.env` (written by setup wizard). Key knobs:

- `PLEX_URL` / `PLEX_TOKEN`, `OLLAMA_ENDPOINT`
- `BASE_CURATOR_MODEL` (`qwen3.6:27b`) / `BASE_SUMMARIZER_MODEL`
  (`granite4.1:8b`, backup `granite4.1:3b`) / `EMBEDDING_MODEL`. The summariser
  is the enrichment-throughput bottleneck — it was switched gpt-oss:20b →
  granite4.1:8b (≈3.9× faster, *more* accurate, see §15). After changing it:
  edit `.env` → `python build_models.py` (rebakes the `curatarr-*` tags, bakes
  `num_ctx 8192`) → bump `_PROMPT_VERSION` → restart → `force=True` re-enrich.
  Don't pick on prose vibe — fact-check (§15 nemotron warning).
- `RADARR_URL`/`_API_KEY`, `SONARR_*`, `LIDARR_*`
- `TMDB_API_KEY` (required for movies/shows), `OMDB_API_KEY`,
  `LASTFM_API_KEY`, `SPOTIFY_CLIENT_ID`/`_SECRET` (all optional)
- `SYNC_INTERVAL_HOURS` (24), `ENRICHMENT_TTL_DAYS` (90),
  `ARR_PRE_ENRICH_BATCH` (80), `ENRICH_PARALLEL_SLOTS`
- `ENABLE_DOCS` (default False — gates `/api/docs`)
- `EXTRA_GAME_PROCESSES` — comma-separated .exe names that pause the LLM

---

## 17. Security posture (1.0 audit)

- Local-LLM only; no hosted-model calls.
- ChromaDB telemetry disabled (`anonymized_telemetry=False`).
- `marked.js` bundled locally (no jsDelivr CDN call).
- **Image proxy** (`src/routers/image_proxy.py`): all external poster URLs
  go through `/api/image/proxy` so TMDB/Deezer don't see per-click browsing.
  Host whitelist + image-only content-type + 5 MB cap + no-auto-redirect
  (whitelist re-checked per hop). **No auth gate** — browsers can't attach
  Bearer headers to `<img>` tags, so the whitelist is the security boundary.
- Setup wizard warns on non-private (public) endpoint URLs.
- `.gitignore` excludes `.env*`, `*.db*`, `chroma_db/`, `data/cache/`,
  `scripts/dev/`, `.claude/`, sqlite CLI binaries.
