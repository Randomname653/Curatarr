# Curatarr 1.0

> **A personal AI media curator for your Plex + *arr stack.**
> Watches what you actually watch, learns your taste, and acts on it —
> recommends what to add, proposes what to delete, and talks it through
> with you in plain language. Everything runs locally; nothing about your
> library leaves the machine.

---

## What it does

Curatarr sits between Plex, your *arr services (Radarr / Sonarr / Lidarr),
and a local Ollama LLM. It continuously builds a per-user **taste vector**
out of your real watch history, then uses that vector to:

- **Recommend** new movies / shows / anime / music — either from your
  library or from external discovery — with a written pitch per item that
  explains *why* you'd like it.
- **Propose deletions** for media that is clearly no longer of interest
  (low affinity score, abandoned shows, drift from your active taste),
  with a written reason and a discussion thread per proposal.
- **Send proactive messages** when the curator notices something worth
  saying — a new season for an anime you binged, a high-confidence
  pick for an evening, a check-in after a long break.
- **Hold a conversation** about any of the above. Free chat, deletion
  discussion, and proposal discussion each have their own thread so
  topics don't bleed into each other.

It is single-tenant by design (one Plex server, one or more Plex users on
it), works fully offline once configured, and stores everything in local
SQLite + ChromaDB.

---

## Highlights

- **Local LLM only.** Curatarr never sends prompts to a hosted model.
  Bring your own Ollama with two roles: a *curator* model (for chat,
  pitches, deletion reasoning) and a *summariser* model (for metadata
  enrichment + memory extraction). The setup wizard bakes both system
  prompts into local model tags so daily use is a single `ollama` pull.
- **A grounded, learning curator.** Every deletion pitch, discussion,
  reevaluation, recommendation and chat reasons from *verified* data — real
  creator / plot / themes, OMDb writer + awards, and a Wikipedia-sourced
  cultural **significance** for the "is this archive-worthy?" question — never
  the model's own (often wrong) memory. It **learns your keep-feedback**: tell it
  once that you value a franchise, a partner favourite, or a documentary, and
  that *consideration* softly protects similar titles in future proposals. It
  resolves the **right same-named work** (year + MusicBrainz id disambiguation,
  so *Lupin III* isn't judged as a spin-off), and it knows what you've actually
  **watched** (from Plex history) — so an unseen title you're curious about isn't
  treated like proven dead weight.
- **Multi-user attribution.** Plex `accountID` is tracked on every play.
  Each Plex user gets their own taste vector, their own recommendations,
  their own deletion proposals, their own chat thread.
- **Enrichment pipeline** that ties Plex / Radarr / Sonarr / Lidarr items
  to TMDB / OMDb / AniList / MusicBrainz / Last.fm / Spotify metadata
  with cache versioning, rule-based fallbacks, not-findable sentinels,
  and per-state progress tracking surfaced in the UI.
- **Music pipeline** that imports Spotify exports, matches them to local
  Plex tracks (Phase 1), resolves MusicBrainz MBIDs (Phase 1.4), fills
  genres from Spotify (Phase 1.5) and Last.fm (Phase 2), and feeds the
  resulting metadata into the music taste vector.
- **Game-mode** suspension: when a known game process is running, the
  curator + summariser models are evicted from VRAM and only API
  pre-fetching (no LLM) continues. The full pipeline resumes on its own
  when the game exits.
- **Per-library unified breakdown panel** (Pass 96) shared by the Library
  Configuration and Enrichment Status pages: same numbers, same
  explainers, hover any count for its source. No more cryptic
  percentages.
- **Standalone runners** for the heavy music phases so a multi-day
  backlog can be cleared from a separate process while the in-app
  enrichment keeps running.
- **Library reclassification** (Manage → 🔀 Reclassify, admin). Audits every
  Sonarr series against the rules for its *true* category — anime = on AniDB
  **or** Asian-origin **and** animated — and moves the mis-filed ones: Western
  cartoons + Japanese live-action (tokusatsu, dramas) out of the Anime
  library, real anime back in. Moves go through the Sonarr editor API and
  re-file each item inside Curatarr **without re-enriching** it.
- **Admin-vs-user roles.** The first Plex user is the admin and curates the
  shared library (deletions, library mapping, orphaned recovery, reclassify);
  every other user gets their own taste / recs / chat but no access to the
  shared-curation surfaces.

---

## Architecture

```
┌─────────────────┐         ┌──────────────────────────────┐
│  Plex Server    │ ──sync─▶│ Curatarr (FastAPI + uvicorn) │
└─────────────────┘         │                              │
                            │  ┌──── Routers ────┐         │
┌─────────────────┐         │  │ auth chat       │         │
│  Radarr/Sonarr  │ ◀──────▶│  │ history library │         │
│  Lidarr         │         │  │ enrichment ...  │         │
└─────────────────┘         │  └──────┬──────────┘         │
                            │         │                    │
┌─────────────────┐         │  ┌──────▼──────────┐         │
│  Ollama (local) │ ◀──────▶│  │  Services       │         │
│  curator +      │         │  │  taste_engine   │         │
│  summariser     │         │  │  media_enricher │         │
└─────────────────┘         │  │  music_matcher  │         │
                            │  │  scheduler ...  │         │
┌─────────────────┐         │  └──────┬──────────┘         │
│  TMDB / OMDb /  │ ──API──▶│         │                    │
│  AniList / MB / │         │  ┌──────▼──────────┐         │
│  Last.fm /      │         │  │   Storage       │         │
│  Spotify        │         │  │   SQLite        │         │
└─────────────────┘         │  │   ChromaDB      │         │
                            │  └─────────────────┘         │
                            └──────────────────────────────┘
```

- **FastAPI + uvicorn** serves the single-page frontend (`frontend/index.html`)
  plus a JSON API. SSE is used for live task progress.
- **SQLAlchemy + SQLite** in WAL mode for transactional data
  (`data/curatarr.db`).
- **ChromaDB** for vector embeddings of enriched items
  (`data/chromadb/`).
- **A second SQLite DB** for the enrichment cache (`data/cache/enrichment.db`)
  versioned by `_CACHE_VERSION` so logic bumps invalidate old entries
  cleanly.
- **APScheduler** drives daily/hourly jobs (Plex sync, taste recompute,
  ARR pre-enrich, enrichment TTL refresh, weekly VACUUM, music pipeline).

---

## Requirements

- **Python 3.11+**
- **Ollama** running locally (default `http://localhost:11434`) with at
  least one model pulled per role. Recommended starting points:
  - Curator: a 20B–32B reasoning-capable model (e.g. `qwen3.6:27b`,
    `gpt-oss:20b`, `deepseek-r1-abliterated:32b`).
  - Summariser: a smaller, faster model (e.g. `dolphin3`,
    `gpt-oss:20b`, `deepseek-r1-abliterated:8b`).
  - Embeddings: `nomic-embed-text`.
- **Plex Media Server** with an admin token.
- **Radarr / Sonarr / Lidarr** are optional but unlock the deletion
  proposals + library-config breakdown for their respective categories.
- **External metadata keys** (all optional except TMDB):
  - **TMDB API key** — required for movies / shows.
  - **OMDb API key** — optional, fills in ratings TMDB doesn't have.
  - **Last.fm API key** — optional, music genre fallback.
  - **Spotify Client ID + Secret** — optional, music genre primary
    source via Client Credentials flow (no user login).
  - AniList + MusicBrainz are used without keys.

GPU is strongly recommended for the curator model. The summariser model
runs comfortably on a single-GPU desktop (12 GB+ VRAM).

---

## Quick start (Windows)

```bat
:: 1. Clone and enter the repo
git clone <repo-url> curatarr
cd curatarr

:: 2. Create a venv (one-time)
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

:: 3. Start Ollama in another terminal
ollama serve

:: 4. Launch Curatarr — the bat file activates the venv, installs any
::    missing deps, bakes Ollama models if not present, opens the browser.
start.bat
```

On Linux / macOS:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# First run: build the curator + summariser Ollama tags
python build_models.py

# Run the server
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. The setup wizard walks you through:

1. **Plex** — PIN-based OAuth (no password needed).
2. **Ollama** — endpoint + base curator + summariser models.
3. ***arrs** — URL + API key per service you use (skip what you don't).
4. **External APIs** — TMDB / OMDb / Last.fm / Spotify keys (pasted
   into `.env`).
5. **Library mapping** — assign each Plex section to a category
   (movie / show / anime / music / ignore).
6. **Admin user** — sets your username + password.

After the wizard you'll see the dashboard. The first Plex sync runs on
startup; enrichment is queued automatically from there.

---

## Configuration

All persistent settings live in `.env` (created by the setup wizard, but
editable). Key fields:

| Env var | What it does |
|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | Plex server URL + admin token |
| `OLLAMA_ENDPOINT` | Default `http://localhost:11434` |
| `BASE_CURATOR_MODEL` | Model name pulled into `curatarr-curator` tag |
| `BASE_SUMMARIZER_MODEL` | Model name pulled into `curatarr-summarizer` tag |
| `EMBEDDING_MODEL` | Default `nomic-embed-text` |
| `RADARR_URL`, `RADARR_API_KEY` | (optional) Radarr connection |
| `SONARR_URL`, `SONARR_API_KEY` | (optional) Sonarr connection |
| `LIDARR_URL`, `LIDARR_API_KEY` | (optional) Lidarr connection |
| `TMDB_API_KEY` | (recommended) TMDB v3 key |
| `OMDB_API_KEY` | (optional) free key from omdbapi.com |
| `LASTFM_API_KEY` | (optional) Last.fm api key |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | (optional) Spotify Client Credentials |
| `SYNC_INTERVAL_HOURS` | Default 24 — Plex history pull cadence |
| `ENRICHMENT_TTL_DAYS` | Default 90 — how long an enriched profile stays fresh |
| `ENRICH_PARALLEL_SLOTS` | 0 = auto, else hard cap on concurrent enrichment workers |
| `EXTRA_GAME_PROCESSES` | Comma-separated `.exe` names that should pause the LLM |
| `JWT_SECRET` | Auto-generated; do not commit |

The setup wizard re-writes `.env` whenever you save changes via Settings
→ Library, so manual edits are safe but the wizard is the canonical
editor for most users.

---

## How the moving parts fit

### 1. Plex sync (`src/services/plex_sync.py`)
- Pulls `/status/sessions/history/all` with pagination, attributes each
  row to a Plex `accountID`, dedupes by `(plex_item_id, viewed_at)`,
  detects binges, and writes to `watch_history`.
- Re-attribution: an admin can run "Re-attribute history" from Settings
  → Maintenance if the Plex user mapping changes.

### 2. Enrichment pipeline (`src/routers/enrichment.py`, `src/services/media_enricher.py`)
- **Producer/consumer** asyncio queue. Producer fetches raw metadata
  from TMDB / AniList / OMDb / MusicBrainz / Last.fm, writes raw to the
  cache and a sentinel to `EnrichmentStatus`. Consumer runs the
  summariser LLM, generates the embedding, writes ChromaDB, and updates
  the `EnrichmentStatus` row to one of six mutually-exclusive states:
  - **LLM-polished** — full summary written.
  - **Rule-based** — fallback profile, LLM upgrade pending.
  - **Awaiting LLM polish** — game-mode cached, LLM paused.
  - **Not findable** — every API missed, 3-day TTL sentinel.
  - **Processing error** — pipeline crashed mid-item.
  - **Never processed** — item never reached the queue.
- The unified per-library breakdown panel surfaces all six states with
  explainers and denominators (Pass 96).

### 3. Music pipeline (`src/services/music_matcher.py`)
Sequenced phases, all idempotent and resumable:
- **Phase 1 — Plex Match.** Imported Spotify plays whose track exists
  in your Plex Music library get their `plex_item_id` rewritten from
  `spotify:track:…` to the Plex rating key.
- **Phase 1.4 — MBID Resolve.** Distinct still-unmatched Spotify artists
  get a MusicBrainz MBID written via the throttled MB API (1 req/s).
- **Phase 1.5 — Spotify genres.** Genres for unmatched plays are filled
  via Spotify's Client Credentials API (no user login).
- **Phase 2 — Last.fm fallback.** Anything Spotify couldn't resolve
  gets a final try against Last.fm.
- **Standalone runners** (`scripts/music_enricher.py`,
  `scripts/mbid_speedrunner.py`) bypass the daily batch cap and use
  the same `force_set_state` mutex so they can't collide with the
  in-app pipeline.

### 4. Taste engine (`src/services/taste_engine.py`)
- Reads watch history, enriched profiles, episodic memories, explicit
  feedback. Produces a per-user/per-category embedding centroid
  (`TasteVectorEntry` + `EncryptedTasteVector`), plus a written summary
  line the curator uses in every prompt.
- Recomputes on demand or after `recs_invalidate_at` is bumped (any
  Plex sync that wrote rows, any enrichment that finished items).

### 5. Recommendations (`src/services/recommendations_engine.py`)
- Vector-similarity search over ChromaDB scoped to the current category
  (`domain` metadata filter prevents cross-category bleed).
- Optional ARR library scope (recommend only items already in
  Radarr/Sonarr/Lidarr) or discovery scope (open-ended).
- Each rec is rendered into a `CachedRecommendation` row with a written
  pitch from the curator model.

### 6. Deletion proposals (`src/routers/recommendations.py`)
- Heuristic + taste-vector affinity score per ARR item. Items below
  threshold are proposed for deletion with a written reason and
  storage-saved figure.
- Discussion thread per proposal (`thread_id="deletion_proposal:{id}"`)
  isolates that conversation from free chat. Approve / reject / discuss
  all write to `CuratorResolutionLog` (audit trail) and any decision
  feeds back into the taste vector.
- Each pitch + discussion is grounded in **verified data** — real creator /
  plot / themes, OMDb writer + awards, and an on-demand Wikipedia **significance**
  — and shows the candidate's **watch status** (from Plex history). Your stored
  keep-feedback (kept franchises, partner favourites, cultural/archive value)
  softly pulls similar titles off the list: the curator *applies* your standing
  preferences to NEW proposals, never as a hard veto.

### 7. Proactive messages (`src/services/proactive_messages.py`)
- The curator polls for triggers (new season for a binged anime, idle
  user with strong picks ready, etc.), writes `ProactiveMessage` rows,
  and the frontend shows a badge until the user reads it. Per-trigger
  toggles in Settings → Notifications.

### 8. Episodic memory (`src/services/episodic_memory.py`)
- Every chat extraction runs a small LLM extraction pass that captures
  explicit statements ("I love X"), feedback ("don't recommend
  romance"), and protection intents ("keep this even if the score
  drops"). Memories are scored, deduped, and injected into every
  curator prompt as a `[MEMORIES]` block.
- A **standing preference** with no specific title (e.g. "I value historical
  documentaries") finds conflicts by *semantic similarity*, so restating it
  reinforces it instead of quietly decaying unrelated memories. Keep/value
  memories drive the deletion **considerations** that protect similar titles in
  future proposals — feedback the curator *learns from*, not just stores.

### 9. Scheduler (`src/services/scheduler.py`)
Default daily/weekly cadence:
- Plex sync — every `SYNC_INTERVAL_HOURS` (24 h default).
- ARR pre-enrich — daily.
- Music pipeline — daily.
- Taste recompute — after every Plex sync that wrote rows.
- Enrichment TTL refresh — daily, prefers un-enriched items first.
- Weekly DB VACUUM.

---

## Daily operations

| Task | Where |
|---|---|
| Re-run Plex sync now | History → "Force sync" |
| Re-run enrichment | Knowledge Base → "Start enrichment" |
| Recompute taste vectors | Knowledge Base → "Recompute taste vectors" |
| Audit + requeue stale enrichments | Knowledge Base → "🔍 Audit metadata" |
| Browse / add via ARR | Sidebar → 🎬 Movies / 📺 TV / 🎵 Music |
| Review deletions (admin) | Sidebar → "Deletions" |
| Reclassify anime ↔ TV (admin) | Sidebar → Manage → "🔀 Reclassify" |
| View live tasks | Sidebar → "Activity" |
| Per-library breakdown | Library Configuration page (or Enrichment Status page) |
| Spotify backlog (artists not in Lidarr) | 🎵 Music → "Spotify Backlog" tab |
| Manual music pipeline | `python run_pipeline_spotify.py` |
| Heavy Spotify backlog clear | `python scripts/music_enricher.py` |
| MBID backlog speedrun | `python scripts/mbid_speedrunner.py` |
| Schema migration after pull | `python update_db.py` |
| Benchmark a candidate model | `python benchmark.py` |
| Bulk Spotify history import | `python import_spotify.py /path/to/Streaming_History/` |

---

## Project layout

```
curatarr/
├── src/
│   ├── main.py                    FastAPI app + lifespan
│   ├── config.py                  Settings (env + defaults)
│   ├── routers/                   HTTP surface
│   │   ├── auth.py                Plex PIN OAuth + JWT
│   │   ├── chat.py                Free / discuss / proposal threads
│   │   ├── history.py             Plex sync + taste read/recompute
│   │   ├── library.py             ARR services + breakdown (Pass 96)
│   │   ├── libraries.py           Plex section mapping
│   │   ├── enrichment.py          Pipeline + status SSE
│   │   ├── recommendations.py     Recs + deletion proposals
│   │   ├── messages.py            Proactive message inbox
│   │   ├── music.py               Music pipeline triggers + Spotify backlog
│   │   ├── tasks.py               Task monitor SSE
│   │   ├── process_monitor.py     Game-mode controls
│   │   ├── setup.py               Setup wizard
│   │   └── users.py               User management
│   ├── services/                  Business logic
│   │   ├── plex_sync.py           History pull + binge detect
│   │   ├── media_enricher.py      TMDB / AniList / OMDb / MB / Last.fm
│   │   ├── music_matcher.py       Phases 1 / 1.4 / 1.5 / 2
│   │   ├── taste_engine.py        Per-user/category vectors
│   │   ├── recommendations_engine.py  Vector search + LLM pitch
│   │   ├── episodic_memory.py     Memory extraction + injection
│   │   ├── proactive_messages.py  Trigger detection + sending
│   │   ├── scheduler.py           APScheduler jobs
│   │   ├── llm_priority.py        Curator/summariser priority swap
│   │   ├── llm_utils.py           Language detection, JSON cleaning
│   │   ├── process_monitor.py     Game-mode detection
│   │   ├── app_state.py           DB-backed runtime flags
│   │   ├── task_monitor.py        Live task progress / SSE source
│   │   ├── verification_session.py  Two-step destructive-action gate
│   │   ├── setup_wizard.py        .env writer + model baker
│   │   ├── orphan_repair.py       Deleted-Plex-section recovery
│   │   ├── anime_mapping.py       AniList ↔ TVDB ↔ TMDB bridge
│   │   ├── arr_client.py          Radarr/Sonarr/Lidarr unified client
│   │   ├── stream_tickets.py      Short-lived SSE auth tickets
│   │   └── bg_tasks.py            Fire-and-forget GC protection
│   ├── database/                  Models + connection
│   │   ├── connection.py          Engine + WAL pragmas + migrations
│   │   └── models.py              All SQLAlchemy tables
│   ├── schemas/                   Pydantic request/response shapes
│   ├── cache/
│   │   └── metadata_cache.py      Versioned API cache (SQLite)
│   ├── embeddings/
│   │   └── embedding_generator.py nomic-embed-text wrapper
│   ├── vector_store/
│   │   └── chromadb_wrapper.py    ChromaDB collection API
│   └── crypto/
│       └── encryptor.py           AES-256-GCM for taste vectors (Phase B)
│
├── frontend/
│   └── index.html                 Single-page UI (vanilla JS, no build)
│
├── scripts/                       Standalone runners
│   ├── music_enricher.py          Bypass daily music-batch cap
│   └── mbid_speedrunner.py        Bypass MB MBID-batch cap
│
├── tests/                         pytest suite
│
├── data/                          Runtime (gitignored)
│   ├── curatarr.db                SQLite (WAL)
│   ├── cache/enrichment.db        API cache (versioned)
│   └── chromadb/                  Vector store
│
├── build_models.py                Bake curatarr-curator + curatarr-summarizer
├── update_db.py                   Idempotent schema migration
├── import_spotify.py              Bulk Spotify history import
├── run_pipeline_spotify.py        Manual music pipeline trigger
├── benchmark.py                   Ollama model throughput benchmark
├── start.bat                      Windows launcher
├── requirements.txt
└── CHANGELOG.md                   Full pass-by-pass history (~100 KB)
```

`scripts/dev/` exists locally but is gitignored — it holds ~40 one-shot
debug / fix / repair tools written along the way. Use them on your own
DB if you ever hit one of the scenarios they target; they're not part
of the supported surface.

---

## Privacy & data

- **Nothing is sent to a hosted LLM.** Every prompt goes to your own
  Ollama instance.
- **Plex history stays on disk.** SQLite + ChromaDB live under
  `data/` and are gitignored.
- **Spotify history** (if imported) sits in the same DB and is
  excluded from the per-Plex-account counts when displaying library
  coverage so it can't inflate "% enriched".
- **API keys** live in `.env` (gitignored).
- **JWT secret** auto-generated on first run, never committed.
- **Taste vectors** are stored unencrypted by default ("Phase A"). A
  PIN-based AES-GCM encryption path exists ("Phase B") but is opt-in.

The `.gitignore` is paranoid: SQLite + ChromaDB blobs, `.env*`, the
bundled sqlite CLI, every `debug_*.py` / `fix_*.py` / `repair_*.py` /
`scripts/dev/`, the `.claude/` worktrees folder, and `spotify_data/`
(personal export folder) are all excluded.

---

## Troubleshooting

- **"enrichment_running flag stuck at 1"** — happens if the server
  crashed mid-run. Either wait for the next sync to clear it, or:
  ```bash
  python -c "from src.services.app_state import force_set_state; \
    force_set_state('enrichment_running', '0')"
  ```
  The same pattern works for `music_pipeline_running`.

- **"Curator running on CPU" banner** — your curator model didn't fit in
  VRAM. Reduce `num_ctx`, pick a smaller `BASE_CURATOR_MODEL`, or close
  whatever else is using GPU memory.

- **Lidarr 0% enriched** — fixed in Pass 91c; the music pipeline writes
  to the general `EnrichmentStatus` table, not `ArrEnrichmentStatus`.
  The breakdown panel now intersects Lidarr's artist set with enriched
  music titles for a meaningful coverage number.

- **Anime taste vector showing 0 enriched** — fixed in Pass 95; if you
  had pre-`_CACHE_VERSION=v2` cache entries, the read path now falls
  back to the un-versioned key once.

- **`database is locked` flood (Windows + Syncthing)** — if `data/` lives
  inside a Syncthing folder, Syncthing hashing the live WAL SQLite DB causes
  lock storms (the main DB sets `busy_timeout=60s`, so a lock that outlasts
  that is an external holder). `sync_guard` auto-excludes `data/` via the
  folder's `.stignore` while the app runs. Syncthing creates that file
  **Hidden**; pre-`fc5b5ce` builds failed to write it ("Permission denied")
  so the exclusion never applied — fixed by writing the hidden file in place.
  If you still see locks, confirm `data/` is excluded in Syncthing.

- **Long debugging hunt** — check `CHANGELOG.md`. It documents every
  Pass with rationale, what broke, what fixed it, and the commit hash.

---

## License

Personal-use codebase by the project owner. See repo for the current
license file.

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md) — the full pass-by-pass history from
the original audit through the 1.0 release. Highlights of the work that
landed for 1.0:

- **Pass 1.5 – 8**: critical-path fixes, multi-user attribution,
  Settings sidebar, concurrency lifecycle, Spotify batch wiring.
- **Pass 9 – 16**: music pipeline cascade, scheduler missed-job
  replay, weekly VACUUM, ARR library cache, Synopsis Browser, Add-New
  flow, Spotify Backlog.
- **Pass 17 – 44**: anime mapping bridge, recommendations vector path,
  game-mode VRAM hand-off, scheduler reliability, mechanical polish.
- **Pass 45 – 79**: numpy/scipy correctness fixes, MediaIdentity
  store, taste-vector embedding usage, server-side game watcher,
  protection-intent detection.
- **Pass 80 – 96**: music negative caching, deletion proposal
  reevaluate, Plex user ratings into taste, MBID speedrunner, audit-
  trail symmetry, TTL prioritisation, DB-lock cascade fixes, cancelled-
  task status fidelity, anime taste-vector cache fallback, **unified
  per-library breakdown panel**.
