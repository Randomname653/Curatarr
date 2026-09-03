# Curatarr — Architecture & Knowledge Base

> Living technical reference for anyone modifying the code.
> [README.md](README.md) is for *running* Curatarr and
> [docs/USAGE.md](docs/USAGE.md) for *operating* it; this file is for
> *understanding and changing* it. It captures the data flow, the
> non-obvious design decisions, and the tribal knowledge that isn't visible
> from any single file — the kind of thing you'd otherwise only learn by
> re-debugging a problem someone already solved.
>
> When you change a subsystem, update the matching section here.
> `CHANGELOG.md` records *what* changed per release; this file records the
> *current* mental model.

---

## Project layout

```
curatarr/
├── src/
│   ├── main.py                FastAPI app + lifespan
│   ├── config.py              Settings (env + defaults)
│   ├── middleware.py          Security response headers (pure ASGI)
│   ├── log_setup.py           Logging bootstrap (file always, console if tty)
│   ├── paths.py               ROOT/DATA_DIR anchoring, CWD-independent
│   ├── tray_app.py            Windows tray launcher (production entry point)
│   ├── routers/               HTTP surface — one file per API area (§19)
│   ├── services/              Business logic — the §5-§13 subsystems plus
│   │                          the satellite modules indexed in §19
│   ├── database/              SQLAlchemy models + WAL connection
│   ├── schemas/               Pydantic request/response shapes
│   ├── vector_store/          ChromaDB wrapper
│   ├── cache/                 Versioned metadata cache (SQLite)
│   ├── embeddings/            Embedding generation
├── frontend/index.html        Single-page UI (vanilla JS, no build step)
├── frontend/vendor/           marked.min.js + purify.min.js, bundled locally
├── scripts/                   Standalone runners + icon renderer
├── tests/                     Plain-script battery — python tests/run_all.py
├── tests/benchmarks/          Model/prompt benchmarking harness (§19)
├── docs/USAGE.md              Operator guide
├── docs/BENCHMARKS.md         Model measurements behind the §15/§16 choices
├── data/                      Runtime state (gitignored)
│   ├── curatarr.db            SQLite (WAL)
│   ├── cache/enrichment.db    Versioned API cache
│   └── chromadb/              Vector store
├── build_models.py            Bake the Ollama model tags
├── update_db.py               Idempotent schema migration
└── start.bat / start_tray.bat Windows launchers (console / tray)
```

`scripts/dev/` exists locally but is gitignored — one-shot debug and
repair tools written along the way, not part of the supported surface.

---

## 0. 2026-08 delta — subsystems newer than the sections below

The 2026-08 run added several subsystems the numbered sections don't cover
yet. Until those sections are rewritten, this is the map:

| Subsystem | Files | One-liner |
|---|---|---|
| Data Custodian | `src/services/data_custodian.py` | Debt-based maintenance: ~20 tasks with cadences + persisted last-run stamps; a 30-min tick runs whatever is overdue, one at a time. Replaces most cron-shaped scheduler jobs. Every runner either creates its own Activity card or gets the tick's wrapper card (tested invariant). |
| Curated search v3 | `src/services/semantic_search.py` | LLM parses the query once (anchor/constraints); scoring is deterministic over raw enrichment tags (lexical-first, concept/tone families, negation, guards) with per-constraint evidence notes + coverage honesty. |
| Multi-vector facets | `src/services/facet_index.py`, collection `media_facets_v1` | Each title's theme phrases are individual vector points (separate collection — mixing into `media_knowledge_v2` breaks n_results math, the anchor resolver, and taste calibration). Gives contrast queries resolution. |
| 4-pillar judge | `src/services/pillars.py` | Deletion verdicts (HARD_KEEP/KEEP_WITH_FLAG/CUT/STAGNANT/EVALUATE) from assembled evidence facts; KEEPs persist to ProtectedMedia; thin evidence skips the judge. `del_score` only pre-ranks. |
| Two-bake split | `src/services/llm_priority.py`, `PITCHER_MODEL` | Deletion runs use a dedicated pitcher bake (benchmarked); chat stays on the curator bake. Residency-guarded eviction; per-run `/api/tags` probe with visible fallback. |
| Owner match pins | `MediaMatchOverride`, hook at top of `fetch_and_prepare_raw`, "Fix match" UI | Durable entity resolution per (service, arr_id); overrides every automatic id source; apply purges the item's caches + flips status so the pipeline rebuilds on the pin. |
| Corpus hygiene | `src/services/corpus_repair.py`, audit in `src/routers/enrichment.py` | Audit walks chroma docs too: zombies requeue (arr-live) or rebuild deterministically from prefetch (arr-gone); corrupt-id clusters requeue; per-service implausible-mass-staleness guard (`src/services/stale_guard.py`). |
| Raw-cache refresher | `src/services/raw_refresh.py` | LLM-free background re-pull of API source data (discovery cards, expired prefetch incl. gone media) — the read-through cache no longer silently ages out. |
| Playlist reconcile | `src/services/plex_playlists.py` | In-place delta edits (identity/pins/art survive) + stale-ratingKey self-heal with write-back; music stays delete+recreate (album→track expansion). |
| Embedding profile SSOT | `src/services/embed_service.py` | The app_state embedding profile is the single truth (v2-moe stack); `effective_embedding_model()` feeds tray/setup/game-watcher checks; v1 collection deleted 2026-08-18. |
| Significance tri-state | `fetch_significance` / `topup_significance` | str = significance, `""` = definitive none (stamp), None = transient (no stamp — walker retries). |
| TMDB id namespaces | `size_norms._tmdb_namespace`, `routers/recommendations._namespace_for` | A TMDB id is NOT an identity on its own: films and series are numbered in separate sequences, so movie 90 and series 90 are unrelated works. Anything keyed on a bare `tmdb_id` pairs them. Group and look up by `(namespace, id)`; refuse rather than guess when the media type is unknown. One definition, delegated to — a second copy is how this came back once already. |
| Rows are not viewings | `series_progress` (`replays`, `abandoned_starts`, `count_real_views`), `plex_sync` RESUME_WINDOW_DAYS | One viewing can write two `watch_history` rows: Plex reports the partial view and the finished view through different queries. Counting raw rows also turns repeated ABANDONED starts into "replays", which inverts the signal. Count completed views, collapse those inside one viewing window, and let the finished view promote the unfinished row it belongs to. |
| FORM guards | `pillars.build_evidence` (`_is_spoken_word`, `_is_factual`) | Some works lose by default when measured with the wrong yardstick — cabaret judged on sonic fit, a documentary judged on narrative subversion. The evidence carries an explicit FORM line telling the judge which criteria apply. Add a guard per form, narrowly; a wrong yardstick is worse than none. |
| Batch language | `llm_utils.detect_user_language` | Surfaces with no live conversation (deletion pitches, proactive nudges) get English. They used to classify unrelated chat history, so the same title could be pitched in one language and re-evaluated in another. A per-user locale setting is the proper home for the general case. |
| Conversation starters | `src/services/chat_starters.py`, `/api/chat/starters`, custodian task `chat_starters` | Pooled curator OPENERS replacing the three hardcoded landing prompts: one LLM batch → a code-enforced diversity gate (distinct forms, distinct openings, mandatory fact anchor) → 48 h-TTL pool with impression decay. Clicking one makes the CURATOR say it (`starter:{id}` thread — same server-owned-context pattern as proposals). Day-named openers expire at local midnight and a pick-time guard retires day-mismatches; anchor_title/media_type pin verified data when the opener is about one work. |
| Status-poll memos | `src/services/ttl_memo.py` | `@ttl_response(seconds, key=…)` — in-process TTL cache + single-flight lock on the polled status endpoints (sync/enrichment/backfill/discover). They recomputed full-table aggregates every 10 s poll; now one compute per window serves all pollers. Exceptions are never cached. |
| Request telemetry + rolling session | `src/middleware.py`, `src/routers/auth.py` | `SlowRequestLogMiddleware` logs >300 ms-to-first-byte requests (the tool that found the poll hotspots). JWTs last 7 d; after 24 h of age `TokenRefreshMiddleware` re-issues via the `x-curatarr-refreshed-token` header so an active user never hits the mid-conversation 401 cliff. |
| Watched-title discussion | chat `watched_title` branch, `src/services/episode_context.py`, `src/services/watch_status.py` | Clicking the last-played strip opens a per-work thread (`watched:tmdb:{id}`) anchored on the user's OWN WatchHistoryEntry row. Episode position is stated as fact with an EPISODE-HONESTY rule — there is no per-episode metadata, and the curator must say so instead of inventing plot. |
| Chat output sanitization | `frontend/vendor/purify.min.js` | All LLM markdown renders via `DOMPurify.sanitize(marked.parse(…))` — marked passes raw HTML through by design, and curator output can echo text fetched from external metadata sources. Single sink, wrapped once. |

---

## 1. One-paragraph overview

Curatarr is a single-tenant FastAPI app that sits between a Plex server, the
*arr stack (Radarr/Sonarr/Lidarr), external metadata APIs (TMDB, OMDb,
AniList, MusicBrainz, Last.fm, Spotify, Jikan — plus OpenSubtitles, by id
only and only when a key is configured), and a locally-hosted Ollama
LLM. It pulls each Plex user's watch history, enriches every title with
metadata + an LLM-written profile, embeds those profiles into a vector store,
and uses the resulting per-user "taste vector" to (a) recommend new media,
(b) propose deletions of media that no longer fits, and (c) hold a
character-driven chat about any of it. Every prompt runs locally; the only data that
leaves the machine is what the enrichment pipeline asks public metadata APIs,
and most of those are searched by NAME, not by id — so they learn which
titles and artists the library holds. Watch history, ratings, the taste
vector and anything typed stay on the machine.

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
| `encrypted_taste_vectors` | Per-user **per-category** taste | the active one. `encrypted_blob` is plain JSON — the name is historical; the planned PIN-based encryption was removed as unworkable (background jobs need the vector when no user is present to supply a PIN) and disk-level encryption is the honest answer for at-rest. One row per `(user, media_category)`. |
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

**Streaming-merge: parallel per-source fetch with provisional-then-full
upgrade** (Phase 2 #39, the current default). Pre-#39, the fetch was a
serial waterfall: OMDb-primary first, THEN TMDB supplement, THEN merge —
each item waited for its slowest source before the LLM polish could
fire. With music's MusicBrainz at 1 req/sec, that meant a music item
waited >1 sec per fetch even when Last.fm had data in 200 ms.

The new model (`_streaming_fetch_runner` in `media_enricher.py`):
  1. `_expected_sources_for(media_type, is_anime, ids)` returns the list
     of APIs to fire IN PARALLEL — `{mb, lastfm}` for music, `{tmdb,
     omdb}` for movie/show with `imdb_id`, `{anilist, jikan}` for anime,
     etc. Each source's existing semaphore (TMDB_SEM=16, OMDb_SEM=10,
     MB_SEM=1, Jikan_SEM=2, AniList_LOCK) still enforces its own rate
     limit inside the parallel fan-out.
  2. `asyncio.create_task` for each source + `asyncio.wait(FIRST_COMPLETED)`
     loop. Each completed task merges into a shared raw via
     `_merge_source_into_raw` (delegates to the existing
     `_merge_raw_metadata` for de-dup + alt_plot_sources population)
     and stamps the source's status into `raw["sources_state"]`.
  3. The MOMENT `_has_enough_data_for_polish(raw, media_type)` flips
     true (title + overview/bio present), the runner deep-copies raw
     as the initial snapshot + returns it with `_remaining_tasks`
     attached. The producer queues the snapshot for LLM polish.
  4. Remaining slow-source tasks keep running in the background; the
     consumer's `_consume_entry` spawns `_finalize_streaming_merge`
     AFTER its polish-write commits (see §5.3.1 — the consumer-first
     ordering is what makes the 🌗 provisional bucket visible). The
     finalizer awaits each remaining task, merges each result into the
     same `live_raw_ref` the runner was mutating, and on completion
     commits `sources_state` (full JSON) + `fetch_tier='full'` +
     `provisional=False` as a SECOND DB update. The raw cache blob is
     also rewritten with the merged full-tier data for future tier-2
     hits.

The OMDb-vs-TMDB primary distinction (Pass 99-fu11) is GONE: both fire
in parallel, whichever returns first is "primary" by happenstance, the
other's data is merged in as soon as it lands. The pre-#39 try/except
TMDBTransientError guard is no longer needed because a transient TMDB
fail just stamps `sources_state["tmdb"]={status:transient}` and the
item continues with the OMDb data — no special-case fall-through.

Sonarr currently does not surface `imdb_id` in collected items, so for
shows the expected_sources list collapses to `{tmdb}` and the OMDb task
isn't fired. Closing that gap is a known follow-up.

### 5.3.1 The consumer-first finalizer ordering

The streaming finalizer (`_finalize_streaming_merge`) is spawned by the
CONSUMER (`_consume_entry` in enrichment.py) AFTER the consumer's
`_write_enrichment_db` call commits — NOT by the producer's
`_process_one`. This ordering matters:

  - Initial design (pre-step-3): producer spawned the finalizer right
    after queueing the item. Finalizer awaited slow sources (~1-2 s)
    while the consumer's deep LLM queue (~3-5 s per item × deep) let
    the finalizer ALWAYS commit first. By the time the consumer
    pulled the item, the row already had `fetch_tier='full'` +
    `provisional=False`. The UI's 🌗 "Enriched (provisional)" bucket
    therefore NEVER lit up — items skipped straight from
    queued-for-retry to LLM-polished.
  - Current design (step 3): consumer pops the finalizer transport
    fields (`_remaining_tasks`, `_live_raw_ref`, `_finalize_*`) off
    raw BEFORE `process_and_save` (so they don't end up in the LLM
    prompt), polishes, writes the initial snapshot (`fetch_tier='fast'`,
    `provisional=True`) to DB, THEN spawns the finalizer. The
    finalizer's later UPDATE flips the row to full/non-provisional.
    Net effect: the row transitions enriched=0 →
    enriched=1+fast+provisional=True → enriched=1+full+provisional=False
    in three observable DB states. The UI bucket actually lights up.

The DB column writes don't conflict: the consumer writes
`enriched/enriched_at/error/vector_ready` (its own columns) plus
`fetch_tier/sources_state/provisional` (initial state). The finalizer
writes ONLY the latter three. SQLite serializes both transactions; the
finalizer's later write is the final authoritative state.

### 5.3.2 When the provisional bucket actually fills

The streaming flow only fires for items that hit NONE of the three
cache layers (raw_prefetch, tier-1 polished, tier-2 raw). Items with a
raw cache hit (tier-2) short-circuit straight to `_fetch_tier='full'`
without any background tasks → consumer writes full directly, no
provisional state, no finalizer.

In practice the provisional bucket is most visible for:
  - **Truly new items** (never fetched — no raw cache yet)
  - **`fast_only=True` runs** (Pass 99-fu13 / #38b) — items
    intentionally STAY provisional with slow sources skipped, until
    the source-upgrade scheduler (§13, #41) hourly cron picks them
    up + re-runs with `fast_only=False` to fill in the missing data
  - **TTL-refresh runs** after the 90 d raw cache TTL elapses

For warm libraries the provisional state is sub-second visible during
streaming-merge transitions (consumer write → finalizer update), so
the bucket counter mostly oscillates near 0 during normal background
enrichment.

**Fast vs Full tier** (Phase 2 #38b — see §5.3 for how it
integrates with the streaming runner). `fetch_and_prepare_raw` accepts
`fast_only: bool = False`. In **fast** mode the streaming runner's
expected-sources list is filtered to drop slow sources: music drops
`mb`, anime drops `jikan`, movie/show drop the secondary supplement.
Those sources never fire, never land in `_remaining_tasks`. Each item
lands `fetch_tier="fast"` + `provisional=True` on the EnrichmentStatus
row and the **source-upgrade scheduler (#41)** scans for
`provisional=True` later and runs a full re-fetch in the background
to fill in the missing sources. The DB column `sources_state` records
every source consulted with status `ok` / `miss` / `transient` /
`skipped` and a timestamp, so the scheduler can target exactly the
sources still pending. WHY: the parallel-benchmark (§15) proved the
LLM consumer ceiling is ~14/min — the throughput-bound is
producer-side waiting on slow APIs. Skipping MB on bulk-pass slashes
the 16 k-music lane from
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

- **Curator** (`curatarr-curator`, large — `gemma4:31b` — chat,
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

- **Enrichment resolves the WRONG same-named work without a disambiguator**
  (Fix A, `4789046`+). The pipeline searches by title STRING and takes the first
  hit — *Lupin III* resolved to the 2012 *Fujiko Mine* spin-off, *Blown Away
  1994* to the 1992 film, the hardstyle *Solstice* to a doom-metal band. The
  curator then reasons (impeccably) about the WRONG entity. Feed the arr's own
  disambiguators end to end: `year` (Radarr/Sonarr) + `mbid`=`foreignArtistId`
  (Lidarr). `search_anilist_by_title(year=)` / `_tmdb_search_and_fetch(year=)`
  prefer the year match; `fetch_musicbrainz_artist(mbid=)` pins the exact artist.
  A **>5y year delta-check** in `fetch_and_prepare_raw` rejects a wrong match
  (enrich nothing > enrich garbage). "🔍 Audit metadata" re-detects & requeues
  existing wrong-entity rows (`_entity_divergence_reason`).
- **Verified data is fragmented across cache keys; the READ must MERGE**
  (`3bf84ad`). The enriched profile (themes) sits under the doc-id key, but OMDb
  fields + Wikipedia significance live on the ID-keyed raw entry
  (`raw:anime:239214`). A doc-id-only lookup misses them → a deletion DISCUSSION
  got a thin profile. `build_verified_data` reads the enriched profile's
  *embedded* ids and merges the raw entries field-by-field.
- **Anime/show verified data is keyed by anilist_id / arr-doc-id, not tmdb_id**
  (`321ef8b`). A tmdb-only lookup silently misses ~5.3k cached anime profiles →
  the curator cold-reads / inverts the plot (Skate-Leading narrated as *Yuri on
  Ice*). Pass every id the candidate carries, incl. `plex_rating_key`
  (`"sonarr:3176"`).
- **`ensure_verified_data` (async, fetches on demand) vs `build_verified_data`
  (cache-only)**. The OMDb/Wikipedia top-up only fires via `ensure_*`. Every path
  where the curator REASONS about a title — delete, discussion, reevaluate/
  Level-2, recommend, general chat — must use `ensure_*`, else it pitches from a
  stale, significance-less profile.
- **Significance is SUMMARISED from a fetched source, never recalled, and never
  inflated** (`2221afb`, `daa3334`). The 27B confidently invents creators/legacy
  for niche titles, so `fetch_significance` distils the Wikipedia extract (UA
  must include a URL — generic UAs get 403). A new title with only production
  facts (cast/location/funding/premiere/debut) → output NONE; don't dress those
  up as "pioneering cultural significance".
- **Title-less memories must NOT match each other by empty title** (`0b6f44c`).
  `resolve_memory_conflicts` matched by `metadata.title`; general preferences
  have none, so `"" == ""` made one save run a NUANCE check against EVERY other
  title-less memory and mass-decay dozens — including the keep/value pillars — on
  every save. Title-less memories now find conflicts by embedding similarity
  (≥0.80): a restatement reinforces its twin, unrelated prefs untouched.
- **Watch status comes from `watch_history` (Plex sync), NOT Tautulli** — the
  owner doesn't run Tautulli. `completed` + `viewed_at` per playback row;
  `chat._watched_lookup` surfaces it so the curator tells an unseen-but-curious
  title from proven dead weight.
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
- **Syncthing `.stignore` is Hidden on Windows** (`fc5b5ce`). `sync_guard`
  excludes `data/` from Syncthing while the app runs so the live WAL DB isn't
  hashed (→ `database is locked`, and worse, a half-synced WAL can corrupt the
  DB). Syncthing creates `.stignore` with the **Hidden** attribute, and
  `Path.write_text` (open `'w'` → `CREATE_ALWAYS`) raises `[Errno 13]
  Permission denied` on a hidden/system file. Always write that file in place
  with `r+` (`OPEN_EXISTING`) — see `sync_guard._write_preserving`. A silent
  failure here re-enables the DB-lock storm; the main DB's `busy_timeout=60s`
  means any lock outlasting 60 s is an *external* holder (Syncthing), not
  internal contention.
- **Moving a Sonarr series between root folders needs `/series/editor`, not
  `PUT /series/{id}`** (`891aa8e`). A series has an authoritative `path` field
  separate from `rootFolderPath`; a plain PUT updates the latter but leaves
  `path`, so `moveFiles` relocates nothing (profile/type edits still apply).
  `PUT /api/v3/series/editor` recomputes `path` and performs the move
  (`moveFiles` is a body field there, not a query param).
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
- **`force=True` now actually clears the polished cache** (fixed in
  `4c5886b`). The pre-fix DELETE pattern was `LIKE 'enriched:{cat}:%'`
  but every api_cache key is prefixed with `_CACHE_VERSION` ("v2:") by
  `MetadataCache.set_cache` — the LIKE matched zero rows, so for months
  `force=True` was a no-op against the polished tier-1 cache. Re-polish
  only happened when a `_PROMPT_VERSION` bump invalidated profiles by
  version. The fix imports `_CACHE_VERSION` + prefixes the pattern so
  the DELETE actually nukes the rows it's supposed to. `force=True`
  does NOT clear the raw cache (`v2:raw:{cat}:%`) — that's API fetch
  data we already paid for; clearing it would burn quota for no quality
  gain. Items with raw cache still hit the tier-2 short-circuit on a
  force run; only the LLM re-polishes.
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
- **WHO spawns the streaming finalizer determines whether the
  provisional UI bucket is visible** (Phase 2 #39 step 3). The first
  cut had the PRODUCER spawn `_finalize_streaming_merge` right after
  queueing — that ran in parallel with the deep LLM consumer queue,
  and since the finalizer's slow-source fetch (~1-2 s) was much
  shorter than the consumer's queue-traversal time (~30 s per item ×
  N), the finalizer always committed first. The row went directly
  from `enriched=0` to `enriched=1, fetch_tier='full', provisional=
  False`. The 🌗 "Enriched (provisional)" bucket NEVER lit up —
  the user designed for visible provisional → upgraded transitions
  and couldn't see any. Fix: spawn the finalizer in the CONSUMER
  (`_consume_entry`) AFTER the polish-write commits the initial
  `fetch_tier='fast', provisional=True` snapshot. The finalizer's
  later UPDATE then visibly flips it to full. **Lesson:** in a
  two-writer race where one writer is "fast & happens in the
  background" and the other is "slow & in the foreground critical
  path", the SLOW writer (the LLM consumer) must commit FIRST or
  the intermediate state the user designed for is invisible. The
  spawn-point choice is the contract that enforces this ordering.
- **`_write_raw_cache` strips underscored keys** (Phase 2 #38b, fix
  `a2e07a6`). My initial cache-tier marker was `_tier_at_fetch` — but
  `_write_raw_cache` strips every key starting with `_` (that's the
  contract for transport-only fields like `_cache_key`, `_tmdb_id`).
  So the marker was getting wiped on write + the tier-2-hit read path
  always defaulted to "full" regardless of what was actually cached.
  Renamed to `cache_tier` (no underscore) so it survives the strip
  and persists across cache reads. **Lesson:** if a field needs to
  survive disk serialization, don't make it look like a transport
  field. Underscore prefixes are a convention; cache layers enforce
  them.

---

## 16. Environment / config (`src/config.py`)

`.env` (written by setup wizard). Key knobs:

- `PLEX_URL` / `PLEX_TOKEN`, `OLLAMA_ENDPOINT`
- `BASE_CURATOR_MODEL` (`gemma4:31b`) / `BASE_SUMMARIZER_MODEL`
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
- `marked.js` bundled locally (no jsDelivr CDN call), and its output is
  sanitized: `DOMPurify.sanitize(marked.parse(…))` before every innerHTML
  write — marked passes raw HTML through by design, and LLM output can echo
  text from external metadata sources.
- CI scanning: CodeQL on pushes/PRs plus an LLM security scan
  (`.github/workflows/llm-security-scan.yml`) — changed files per PR, weekly
  full sweep; scan errors hard-fail rather than reporting a clean result.
- **Image proxy** (`src/routers/image_proxy.py`): all external poster URLs
  go through `/api/image/proxy` so TMDB/Deezer don't see per-click browsing.
  Host whitelist + image-only content-type + 5 MB cap + no-auto-redirect
  (whitelist re-checked per hop). **No auth gate** — browsers can't attach
  Bearer headers to `<img>` tags, so the whitelist is the security boundary.
- Setup wizard warns on non-private (public) endpoint URLs.
- `.gitignore` excludes `.env*`, `*.db*`, `chroma_db/`, `data/cache/`,
  `scripts/dev/`, `.claude/`, sqlite CLI binaries.
- **Admin-curation boundary** (`efd11bf`). The first Plex user is the admin;
  shared-library curation is admin-only. Deletions
  (`/recommendations/deletions*`), library config + orphaned recovery
  (`/libraries/orphaned`, `repair-orphans`, `cleanup-orphans`), and reclassify
  (`/library/reclassify/*`) all `require_admin`. Defence in depth: hidden nav
  items + a `showView()` guard for `deletions`/`libraries`/`admin`/`reclassify`
  + the endpoint checks — the **endpoint check is the real boundary**, the UI
  gates are convenience. Non-admins keep their own taste / recs / chat.

---

## 18. Library reclassification (`src/services/library_sorting.py`, Manage → Reclassify)

Admin tool that audits Sonarr for series filed in the wrong library and moves
them. Two phases; read-only scan, explicit write.

**Detection (`scan_misclassified`, read-only).** A series' *true* category:
- on the anime-lists **AniDB** mapping (`tvdb_to_anidb`) → anime;
- else a TMDB `/find` lookup (cached in `app_state` under `tvdb_meta:<tvdbId>`)
  yields `(origin_country, is_animated)`. **Asian origin AND the Animation
  genre (id 16) → anime.** Asian-origin-but-not-animated is Japanese
  *live-action* (tokusatsu, dramas) and belongs in TV — the Animation-genre
  check is what stops Spider-Man (1978) / Ultraman / Bloody Monday landing in
  anime.
- no origin at all → fall back to the Sonarr `genres`: `"Anime"` ⇒ anime; no
  animation genre at all ⇒ live-action ⇒ TV; only a generic `"Animation"` ⇒
  `uncertain` (the admin picks → TV / → Anime per card).

TMDB is queried only where origin can change the answer (anime-lib non-AniDB,
or TV-lib titles with anime-ish settings), so the full ~3.6k-series sweep is
~15 s, not a 1000-call timeout. Results group into `to_tv` / `to_anime` /
`fix_settings` (right library, wrong `seriesType`/quality profile) /
`uncertain`. Roots + profiles are resolved live, never hardcoded (anime
profile = the one with "anime" in its name; TV = the rest).

**Apply (`apply_reclassify`, the only write path).** Per item:
1. `PUT /api/v3/series/editor` with `seriesType` + `qualityProfileId`, plus
   `rootFolderPath` + `moveFiles` when the root changes (see §15 on why the
   editor endpoint, not `PUT /series/{id}`).
2. **Re-file inside Curatarr without re-enriching.** The enrichment skip-key
   is `(title, media_category)` (§5), so a category change would otherwise
   mismatch → full re-fetch + re-embed + LLM. Instead, four cheap in-place
   updates keep the live `classify_sonarr_category(updated)` == the persisted
   category: `enrichment_status.media_category`, `arr_enrichment_status.
   category`, the MetadataCache profile key `enriched:{cat}:{key}` (copied
   old→new), and the ChromaDB vector's `domain`/`media_type` quarantine
   metadata (metadata-only, no re-embed). The new category is
   `classify_sonarr_category(updated)` (which honours `seriesType` OR the
   `"Anime"` genre), so the rare TVDB-"Anime"-genre-on-a-Western-title edge
   case stays internally consistent — no drift, still no re-fetch.

Nothing in the apply path re-fetches metadata, re-embeds, or calls an LLM.

---

## 19. Module index — the satellites

One line per file the sections above don't already anchor. This is the map,
not the territory: when one of these grows a real design decision, promote it
to its own section (or a §0 delta row) instead of growing this list.

### Entry points & platform
- `src/tray_app.py` — Windows system-tray launcher (pystray + uvicorn worker
  thread); the production entry point, vs `start.bat`'s dev console.
- `src/paths.py` — SSOT for filesystem anchoring (`ROOT`/`DATA_DIR`),
  CWD-independent so tray/autostart/frozen builds resolve data + frontend.
- `src/log_setup.py` — idempotent logging bootstrap: rotating file handler
  always; console handler only when a real stderr exists (pythonw has none).
- `src/services/shutdown_bridge.py` — import-free callback registry letting
  the tray intercept the web shutdown endpoint instead of relying on SIGINT.
- `src/services/bg_tasks.py` — keeps strong references to fire-and-forget
  asyncio tasks so they aren't garbage-collected mid-run.

### Routers not covered above
- `src/routers/imports.py` — GUI data imports (Spotify listening-history
  upload + run); upload open during setup, running the import is admin-only.
- `src/routers/messages.py` — proactive-messages API (list unread, mark read).
- `src/routers/stats.py` — admin-only curation report, backed by
  `src/services/curation_stats.py` (live SQL: monthly resolutions, GB freed,
  Stubbornness Index, redundancy, taste evolution).

### Chat & curator satellites
- `src/services/app_context.py` — SSOT for what the curator is told about the
  app's own UI (buttons/badges/verdicts), drift-tested against
  `frontend/index.html`. Never inline app knowledge in routers.
- `src/services/episode_context.py` — the Sonarr episode a user stopped at
  plus the next one ("you stopped at S1E9, next up …").
- `src/services/watch_status.py` — per-user watch status from Plex-synced
  `watch_history` (NOT Tautulli): "unseen but curious" vs "watched and moved on".
- `src/services/verification_session.py` — post-enrichment one-at-a-time
  taste-verification questions (abandoned items, conflicting signals, outliers).
- `src/services/curator_principles.py` — autonomous principle-learning from
  owner debates: extract → novelty-check → re-inject into the judge.
- `src/services/stream_tickets.py` — short-lived one-time tickets for SSE
  streams, so JWTs never ride in URL query strings.

### Deletion-debate evidence (deterministic, LLM-free)
- `src/services/album_dossier.py` — album-level music evidence (type, stock,
  community standing, owner listening, style) from Lidarr + Last.fm + Discogs.
- `src/services/lidarr_discography.py` — one-call Lidarr album summary
  (counts, disk size, monitored-but-fileless ghosts); uncached by design.
- `src/services/reception.py` — community reception (AniList/MAL/Jikan/TMDB
  reviews) for obscure titles.
- `src/services/studio_notes.py` — Wikipedia "what this studio is known for"
  reputation notes, cached ~forever.
- `src/services/wikidata.py` — structured Wikidata facts (adaptation-source
  authors, named awards) as archive-pillar evidence; imdb-id shape-guarded
  before it ever reaches SPARQL.
- `src/services/subtitle_signals.py` — LLM-free pacing/rhythm signals from a
  subtitle track, execution-side evidence for the judge.
- `src/services/subtitle_provider.py` — generic HTTP adapter contract for an
  optional self-hosted subtitle service (no bundled provider).

### Identity & mapping
- `src/services/external_ids.py` — harvests IMDb/TVDB/TMDB ids the *arrs
  already hold (id-join first, name+year veto second).
- `src/services/anime_mapping.py` — community anime-lists AniDB↔TVDB↔AniList
  crossref, cached + refreshed weekly.
- `src/services/anime_offline.py` — manami-project anime-offline-database as
  a SQLite-cached fallback lookup.
- `src/services/discogs_offline.py` — streams the monthly Discogs CC0 masters
  dump into a local per-artist style vocabulary (no API token needed).

### Music neighbours
- `src/services/soulsync_client.py` — read-only client for the SoulSync LAN
  neighbour's music-metadata API; never triggers its downloads.
- `src/services/music_catalog_sync.py` — nightly SoulSync→Lidarr catalog sync
  (structure index only — never fights SoulSync over files).
- `src/services/spotify_import.py` — engine behind the GUI Spotify upload;
  root `import_spotify.py` is a thin CLI wrapper over it.

### Library-curation satellites
- `src/services/library_memory.py` — shared seen/owned title indices so rec
  lanes never resurface an already-watched or already-owned title.
- `src/services/upgrade_curation.py` — the inverse of the deletion flag:
  strong love-signal, weak file quality.
- `src/services/collection_designer.py` + `src/services/plex_collections.py` —
  LLM-designed rotating themed collections from owned candidates; the Plex
  write layer is prefix-scoped so it never touches Kometa's collections.
- `src/services/plex_watchlist.py` — adds a title to the discussing user's
  own plex.tv watchlist when they declare watch-intent in a deletion debate.
- `src/services/orphan_repair.py` — remaps watch-history rows whose Plex
  library section no longer exists.
- `src/services/kb_overview.py` — SSOT for "how much is enriched/vectorized";
  replaced three counters that disagreed with each other.
- `src/services/archive_backfill.py` — coverage-gated on-demand backfill for
  metadata sources still catching up (vs the custodian's gentle pace).

### Models & embeddings
- `src/services/model_catalog.py` — benchmark-verified Ollama model catalog +
  VRAM-aware recommendations for the setup wizard.
- `src/services/embedding_migration.py` — one-shot v2 embedding-corpus build
  in a parallel Chroma collection, sanity-gated before flipping the profile.

### Schemas
- `src/schemas/user.py` / `chat.py` / `recommendations.py` — Pydantic shapes;
  `chat.DiscussContext` carries server-owned references (proposal, proactive
  message, principle, starter, watched-title) the backend resolves itself —
  client-supplied title/reason text is never trusted.

### CI & benchmarks
- `.github/scripts/llm_security_scanner.py` — the LLM security scan behind
  the workflow (§17): structured outputs, scan errors hard-fail.
- `tests/benchmarks/` — model/prompt benchmarking harness (curator_bench,
  tournament_bench, auto_benchmark, num_ctx_bench, curator_pipeline_bench +
  `model_baselines.csv`); measurements land in `docs/BENCHMARKS.md`.
