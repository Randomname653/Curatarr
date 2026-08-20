# Curatarr

> **A personal AI media curator for your Plex + *arr stack.**
> Watches what you actually watch, learns your taste, and acts on it —
> recommends what to add, proposes what to delete, and talks it through
> with you in plain language. Everything runs locally; nothing about your
> library leaves the machine.

> ⚠️ **This app deletes media.** Approved deletion proposals remove files
> from your Radarr / Sonarr / Lidarr libraries — deleted media is gone.
> Keep backups, use the dry-run/analysis views first, and treat every
> approval as final. Provided as-is, without warranty (see
> [LICENSE](LICENSE)); you run it at your own risk.

---

## What it does

Curatarr sits between Plex, your *arr services (Radarr / Sonarr / Lidarr),
and a local Ollama LLM. It continuously builds a per-user **taste vector**
out of your real watch history, then uses that vector to:

- **Recommend** new movies / shows / anime / music — from your library or
  from external discovery — with a written pitch per item that explains
  *why* you'd like it.
- **Propose deletions** through a 4-pillar judge (taste, household use,
  custodianship, resonance) that rules KEEP / CUT / STAGNANT per title
  from *verified* evidence — with a written pitch, a discussion thread
  per proposal, and automatic protection records for what it decides to
  keep. Titles without enrichment data are skipped, never judged blind.
- **Search your library semantically** — "like X but darker, more mature"
  resolves the anchor title, scores every constraint against real
  metadata tags, cites its evidence per hit, and says honestly when
  nothing in your library carries the full profile.
- **Push the results where you watch**: per-user "Curatarr Recommended"
  Plex playlists (updated in place, not recreated) and rotating
  "Curatarr ·" collection shelves designed by the curator.
- **Send proactive messages** when the curator notices something worth
  saying — a new season for an anime you binged, a high-confidence pick
  for an evening, a check-in after a long break.
- **Hold a conversation** about any of the above. Free chat, deletion
  discussion, and proposal discussion each keep their own thread so
  topics don't bleed into each other.

Single-tenant by design (one Plex server, one or more Plex users on it),
fully offline once configured, everything stored in local SQLite +
ChromaDB.

## Feature highlights

- **Local LLM only.** No hosted model ever sees a prompt. Bring your own
  Ollama with two roles — a *curator* model (chat, pitches, deletion
  reasoning) and a *summariser* model (metadata enrichment, memory
  extraction); the setup wizard bakes both system prompts into local
  model tags. An optional third bake serves only the deletion pipeline
  when benchmarks favour a different model there.
- **A grounded, learning curator.** Every judgment reasons from verified
  data — real creators, plot, themes, awards, Wikipedia-sourced cultural
  significance — never the model's own (often wrong) memory. Keep-feedback
  becomes standing *considerations* that softly protect similar titles in
  future proposals. Same-named works are disambiguated by year and
  MusicBrainz id, and the curator knows what you've actually watched.
- **Multi-user.** Every play is attributed to its Plex account; each user
  gets their own taste vector, recommendations, playlists, and chat. The
  first user is the admin and curates the shared library; everyone else
  gets personal features without the destructive surfaces.
- **A data custodian instead of a button zoo.** Debt-based maintenance
  (anacron-style): ~20 tasks each carry a cadence and catch up whenever
  the machine is on — enrichment cycles, metadata walkers, taste
  recompute, playlist pushes, cache refresh, profile audits. Every job is
  live in the Activity view with real progress.
- **Self-healing library knowledge.** The profile audit walks both the
  cache and the vector corpus: stale or wrong-entity profiles requeue,
  orphaned documents rebuild deterministically from cached data, corrupt
  id clusters re-resolve — and a mass-staleness guard keeps an
  unreachable service from being mistaken for a deleted library. When two
  same-named works collide, the **Fix match** button pins the correct
  identity permanently.
- **Game-mode.** When a known game process is running, the LLMs are
  evicted from VRAM and only API pre-fetching continues; the full
  pipeline resumes on its own when the game exits.
- **Deep metadata enrichment.** Plex / *arr items are tied to TMDB, OMDb,
  AniList, MusicBrainz, Last.fm and Spotify metadata with cache
  versioning, rule-based fallbacks, not-findable sentinels and per-state
  progress. A music pipeline imports Spotify listening exports, matches
  them to Plex tracks, resolves MusicBrainz ids and fills genres.
- **Library reclassification.** Audits every Sonarr series against the
  rules for its true category (anime vs. Western animation vs. Asian
  live-action) and moves the mis-filed ones through the Sonarr API —
  without re-enriching them.

---

## Requirements

- **Python 3.11+**
- **Ollama** running locally (default `http://localhost:11434`) with at
  least one model pulled per role. Recommended starting points:
  - Curator: a 20B–32B reasoning-capable model (e.g. `qwen3.6:27b`).
  - Summariser: a smaller, faster model (e.g. `granite4.1:8b`).
  - Embeddings: `nomic-embed-text-v2-moe` (runs on CPU by design — the
    GPU stays free for the curator).
- **Plex Media Server** with an admin token.
- **Radarr / Sonarr / Lidarr** — optional, but they unlock deletion
  proposals and the library breakdown for their categories.
- **External metadata keys** (all optional except TMDB): TMDB (movies /
  shows), OMDb (extra ratings + awards), Last.fm (music genres),
  Spotify Client ID + Secret (music genres, no user login). AniList and
  MusicBrainz need no keys.

A GPU is strongly recommended for the curator model; the summariser runs
comfortably on a single-GPU desktop (12 GB+ VRAM).

## Quick start

**Windows**

```bat
git clone https://github.com/Randomname653/Curatarr.git curatarr
cd curatarr
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

:: Ollama must be running (ollama serve), then:
start.bat
```

`start.bat` is the dev entry: live console, hot reload, and it self-heals
missing dependencies and Ollama model bakes. For background operation,
**`start_tray.bat`** runs Curatarr as a tray icon with autostart toggle,
log access and graceful shutdown; output goes to `data\logs\curatarr.log`.

**Linux / macOS**

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python build_models.py    # first run: bake the curator + summariser tags
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. The setup wizard walks you through Plex
(PIN-based OAuth), Ollama models, *arr connections, external API keys,
Plex-library-to-category mapping, and the admin user. The first Plex sync
runs on startup; enrichment queues automatically from there.

## Configuration

All persistent settings live in `.env` (created by the setup wizard, but
editable). [`.env.example`](.env.example) documents the complete list
with defaults; the most-used fields:

| Env var | What it does |
|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | Plex server URL + admin token |
| `OLLAMA_ENDPOINT` | Default `http://localhost:11434` |
| `BASE_CURATOR_MODEL` | Model baked into the `curatarr-curator` tag |
| `BASE_SUMMARIZER_MODEL` | Model baked into the `curatarr-summarizer` tag |
| `EMBEDDING_MODEL` | Default `nomic-embed-text-v2-moe` |
| `RADARR_URL`, `RADARR_API_KEY` | (optional) Radarr connection |
| `SONARR_URL`, `SONARR_API_KEY` | (optional) Sonarr connection |
| `LIDARR_URL`, `LIDARR_API_KEY` | (optional) Lidarr connection |
| `TMDB_API_KEY` | (recommended) TMDB v3 key |
| `OMDB_API_KEY` | (optional) free key from omdbapi.com |
| `LASTFM_API_KEY` | (optional) Last.fm API key |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | (optional) Spotify Client Credentials |
| `SYNC_INTERVAL_HOURS` | Default 24 — Plex history pull cadence |
| `ENRICHMENT_TTL_DAYS` | Default 90 — how long an enriched profile stays fresh |
| `EXTRA_GAME_PROCESSES` | Comma-separated `.exe` names that pause the LLM |
| `JWT_SECRET` | Auto-generated; never commit it |

The setup wizard re-writes `.env` whenever you save changes via Settings,
so manual edits are safe but the wizard is the canonical editor.

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

- **FastAPI + uvicorn** serve the single-page frontend
  (`frontend/index.html`, vanilla JS, no build step) plus a JSON API;
  SSE streams live task progress.
- **SQLAlchemy + SQLite** in WAL mode for transactional data
  (`data/curatarr.db`), plus a second SQLite DB for the versioned
  enrichment cache (`data/cache/enrichment.db`) — a Cache-Inventory
  panel in the UI shows per-source rows/live/stale/size.
- **ChromaDB** for vector embeddings (`data/chromadb/`): one document
  per title plus per-theme facet points for multi-vector retrieval.
- **APScheduler + the data custodian** drive maintenance: a 30-minute
  tick runs whatever is overdue (debt-based, catches up after
  downtime), yielding to the curator and pausing LLM work while a game
  holds the GPU.

## How the moving parts fit

### 1. Plex sync (`src/services/plex_sync.py`)
Pulls watch history with pagination, attributes each row to a Plex
account, dedupes, detects binges, and writes to `watch_history`. An
admin can re-attribute history if the Plex user mapping changes.

### 2. Enrichment pipeline (`src/routers/enrichment.py`, `src/services/media_enricher.py`)
Producer/consumer asyncio queue. The producer fetches raw metadata from
TMDB / AniList / OMDb / MusicBrainz / Last.fm; the consumer runs the
summariser LLM, generates the embedding, writes ChromaDB, and tracks one
of six mutually-exclusive states per item (LLM-polished, rule-based,
awaiting polish, not findable, error, never processed). A unified
per-library breakdown panel surfaces all six with explainers.

### 3. Music pipeline (`src/services/music_matcher.py`)
Sequenced, idempotent, resumable phases: match imported Spotify plays to
Plex tracks → resolve MusicBrainz MBIDs (throttled) → fill genres from
Spotify → Last.fm fallback. Standalone runners
(`scripts/music_enricher.py`, `scripts/mbid_speedrunner.py`) clear
multi-day backlogs from a separate process without colliding with the
in-app pipeline.

### 4. Taste engine (`src/services/taste_engine.py`)
Reads watch history, enriched profiles, episodic memories and explicit
feedback; produces per-user/per-category embedding centroids (with
multi-centroid support for genuinely multi-modal taste) plus a written
summary the curator uses in every prompt.

### 5. Recommendations (`src/services/recommendations_engine.py`)
Vector-similarity search over ChromaDB scoped to the current category,
with library scope (own it already) or discovery scope (worth
acquiring). Each recommendation carries a written pitch from the curator.

### 6. Deletion proposals (`src/routers/recommendations.py`, `src/services/pillars.py`)
Candidates are pre-ranked by score (taste mismatch, size, ratings,
listening depth, learned considerations) — but the verdict belongs to
the 4-pillar judge, ruling from verified evidence + significance +
reception + household watch state against a constitution the operator's
learned principles extend. KEEPs persist as protections; thin-evidence
titles skip the judge entirely. Every proposal has its own discussion
thread, and every decision feeds back into the taste vector.

### 7. Proactive messages (`src/services/proactive_messages.py`)
Trigger polling (new season for a binged show, strong picks waiting,
long-break check-ins) writes inbox messages with per-trigger toggles in
Settings.

### 8. Episodic memory (`src/services/episodic_memory.py`)
A small extraction pass over every chat captures statements, feedback
and protection intents; memories are scored, deduped and injected into
every curator prompt. Standing preferences match conflicts by semantic
similarity, so restating one reinforces it, and keep/value memories
drive the deletion considerations.

### 9. Scheduler + data custodian (`src/services/scheduler.py`, `src/services/data_custodian.py`)
A few interval jobs (proactive messages, the game watcher) plus the
custodian: ~20 maintenance tasks with cadences and persisted last-run
stamps, executed one at a time whenever overdue. Partial tasks stay due
and continue next tick; everything reports live progress.

### 10. Curated search (`src/services/semantic_search.py`, `src/services/facet_index.py`)
The LLM parses the query once (anchor / constraints); scoring is
deterministic against raw enrichment tags (lexical-first, concept/tone
families, negation, demographic and comedy guards). Theme facets give
contrast queries multi-vector resolution; every hit carries
per-constraint evidence notes and a coverage banner says when no title
carries the full profile.

### 11. Corpus hygiene (`src/services/corpus_repair.py`, audit in `src/routers/enrichment.py`)
The profile audit walks both the cache and the vector corpus:
incomplete or wrong-entity profiles requeue; orphaned documents rebuild
deterministically from cached prefetch data; corrupt external-id
clusters re-resolve; operator Fix-match pins override everything and
survive rescans. A mass-staleness guard keeps infrastructure outages
from being mistaken for mass deletions.

---

## Daily operations

| Task | Where |
|---|---|
| Re-run Plex sync now | History → "Force sync" |
| Re-run enrichment | Knowledge Base → "Start enrichment" |
| Recompute taste vectors | Knowledge Base → "Recompute taste vectors" |
| Audit + self-heal metadata | Knowledge Base → "🔍 Audit metadata" |
| Browse / add via ARR | Sidebar → 🎬 Movies / 📺 TV / 🎵 Music |
| Review deletions (admin) | Sidebar → "Deletions" |
| Reclassify anime ↔ TV (admin) | Sidebar → Manage → "🔀 Reclassify" |
| View live tasks | Sidebar → "Activity" |
| Per-library breakdown | Library Configuration page |
| Spotify backlog | 🎵 Music → "Spotify Backlog" tab |
| Manual music pipeline | `python run_pipeline_spotify.py` |
| Heavy Spotify backlog clear | `python scripts/music_enricher.py` |
| MBID backlog speedrun | `python scripts/mbid_speedrunner.py` |
| Schema migration after pull | `python update_db.py` |
| Benchmark a candidate model | `python benchmark.py` |
| Bulk Spotify history import | `python import_spotify.py /path/to/Streaming_History/` |

## Project layout

```
curatarr/
├── src/
│   ├── main.py                FastAPI app + lifespan
│   ├── config.py              Settings (env + defaults)
│   ├── middleware.py          Security response headers (pure ASGI)
│   ├── routers/               HTTP surface (auth, chat, history,
│   │                          library, enrichment, recommendations,
│   │                          music, tasks, setup, users, …)
│   ├── services/              Business logic (plex_sync, media_enricher,
│   │                          music_matcher, taste_engine,
│   │                          recommendations_engine, pillars,
│   │                          semantic_search, facet_index,
│   │                          episodic_memory, data_custodian,
│   │                          corpus_repair, llm_priority, …)
│   ├── database/              SQLAlchemy models + WAL connection
│   ├── schemas/               Pydantic request/response shapes
│   ├── vector_store/          ChromaDB wrapper
│   └── crypto/                AES-GCM encryptor (opt-in taste-vector
│                              encryption)
├── frontend/index.html        Single-page UI (vanilla JS, no build)
├── scripts/                   Standalone runners
├── tests/                     Plain-script test battery
│                              (python tests/run_all.py — no pytest)
├── data/                      Runtime state (gitignored)
├── build_models.py            Bake the Ollama model tags
├── update_db.py               Idempotent schema migration
├── start.bat / start_tray.bat Windows launchers (console / tray)
└── CHANGELOG.md               Condensed release history
```

## Privacy & data

- **Nothing is sent to a hosted LLM.** Every prompt goes to your own
  Ollama instance.
- **Watch history stays on disk.** SQLite + ChromaDB live under `data/`
  and are gitignored, as are `.env` (API keys, tokens) and personal
  export folders.
- **JWT secret** is auto-generated on first run and never committed.
- **Taste vectors** are stored unencrypted by default; an opt-in
  PIN-based AES-GCM encryption path exists.

## Troubleshooting

- **"enrichment_running flag stuck at 1"** — happens if the server
  crashed mid-run. Either wait for the next sync to clear it, or:
  ```bash
  python -c "from src.services.app_state import force_set_state; \
    force_set_state('enrichment_running', '0')"
  ```
  The same pattern works for `music_pipeline_running`.

- **"Curator running on CPU" banner** — the curator model didn't fit in
  VRAM. Reduce `num_ctx`, pick a smaller `BASE_CURATOR_MODEL`, or close
  whatever else is using GPU memory.

- **`database is locked` flood (Windows + Syncthing)** — if `data/`
  lives inside a Syncthing folder, Syncthing hashing the live WAL DB
  causes lock storms. Curatarr auto-excludes `data/` via the folder's
  `.stignore` while it runs; if you still see locks, confirm the
  exclusion in Syncthing.

---

## License

[GNU AGPL-3.0](LICENSE) — free to use, modify and self-host; derived
work and network-hosted forks must stay open source. Components ported
from other projects keep their original licenses — see
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) (SoulSync, MIT).

Contributions: see [`CONTRIBUTING.md`](CONTRIBUTING.md). Security
posture and vulnerability reporting: [`SECURITY.md`](SECURITY.md).

Release history: [`CHANGELOG.md`](CHANGELOG.md). The forward plan lives
in [`ROADMAP.md`](ROADMAP.md).
