<div align="center">

<img src="assets/curatarr_256.png" alt="Curatarr" width="112">

# Curatarr

**A self-hosted AI curator for Plex and the \*arr stack.**

Curatarr learns what you actually watch, recommends what's worth adding,
and makes a reasoned case for what to delete — with every prompt running
on your own hardware.

[![License][badge-license]][link-license]
[![Tests][badge-tests]][link-tests]
[![Python][badge-python]][link-python]
[![Local LLM][badge-local]][link-ollama]
[![Platform][badge-platform]](#requirements)

</div>

> [!WARNING]
> **Curatarr can delete media.** Approving a deletion proposal removes the
> files from your Radarr / Sonarr / Lidarr libraries — permanently. Keep
> backups, review the analysis views before approving, and treat every
> approval as final. The software is provided as-is, without warranty.

---

## What it does

Most library tools tell you *what* you have. Curatarr forms an opinion
about it.

It sits between Plex, your \*arr services and a local [Ollama][link-ollama]
model, and continuously builds a per-user **taste vector** from real watch
history. Every title in your library gets enriched with real metadata —
creators, themes, awards, critical reception, cultural significance — and
embedded into a local vector store. From there the curator recommends,
proposes deletions, argues its case in chat, and keeps the whole thing
tidy on its own.

Nothing is sent to a hosted LLM. Nothing about your library leaves the
machine except the metadata lookups the enrichment pipeline needs.

## Features

- **Taste-aware recommendations** — from your own library or open-ended
  discovery, each with a written pitch explaining *why* it fits you.
- **Deletion proposals with an argument** — a 4-pillar judge (taste,
  household use, custodianship, resonance) rules KEEP / CUT / STAGNANT
  from verified evidence, then writes the case. Every proposal has its
  own discussion thread; titles without enrichment data are skipped
  rather than judged blind.
- **Semantic library search** — "like *X* but darker and more mature"
  resolves the anchor title, scores each constraint against real metadata
  tags, cites its evidence per hit, and admits when nothing in your
  library carries the full profile.
- **A curator that learns** — tell it once that you value a franchise, a
  partner's favourite, or archival oddities, and that preference softly
  protects similar titles in every future proposal.
- **Grounded, never hallucinated** — judgments reason from cached facts
  (TMDB, OMDb, AniList, MusicBrainz, Last.fm, Wikipedia), not from the
  model's own memory of a title.
- **Multi-user** — every play is attributed to its Plex account; each
  user gets their own taste vector, recommendations, playlists and chat.
- **Writes back to Plex** — per-user "Curatarr Recommended" playlists
  (updated in place, not recreated) and rotating collection shelves.
- **Proactive messages** — a new season for something you binged, a
  strong pick for tonight, a check-in after a long break.
- **A data custodian instead of a button zoo** — ~20 maintenance tasks
  each carry a cadence and catch up whenever the machine is on. Every job
  reports live progress in the Activity view.
- **Self-healing library knowledge** — the profile audit requeues stale
  entries, rebuilds orphaned documents from cache, re-resolves corrupt
  id clusters, and refuses to mistake an unreachable service for a
  deleted library. A **Fix match** button permanently pins the right
  identity when two same-named works collide.
- **Game mode** — when a game starts, the models are evicted from VRAM
  and only keyless API pre-fetching continues. The pipeline resumes by
  itself afterwards.

## How it works

```
   Plex ──history──▶┌──────────────────────────────────┐
                    │            Curatarr              │
 *arr  ◀──manage───▶│                                  │
                    │  enrich ▶ embed ▶ taste vector   │
Metadata ──API────▶ │     │                    │       │
  APIs              │     ▼                    ▼       │
                    │  ChromaDB           recommend /  │
 Ollama ◀──prompts─▶│  + SQLite           judge / chat │
 (local)            └──────────────────────────────────┘
```

1. **Sync** — watch history is pulled from Plex and attributed per user.
2. **Enrich** — each title is resolved against the metadata APIs and
   given an LLM-written profile, then embedded into ChromaDB.
3. **Model taste** — profiles plus watch history plus your stated
   preferences become a per-user, per-category taste vector.
4. **Act** — that vector drives recommendations, deletion candidates,
   search ranking and the curator's side of every conversation.

The full technical reference — data model, pipeline internals, design
decisions and the invariants learned the hard way — lives in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Getting started

### Requirements

| | |
|---|---|
| **Python** | 3.11 or newer |
| **Plex Media Server** | with an admin token |
| **[Ollama][link-ollama]** | running locally, GPU strongly recommended |
| **Radarr / Sonarr / Lidarr** | optional — unlocks deletion proposals per category |
| **TMDB API key** | recommended — the primary movie/show metadata source |
| **OMDb / Last.fm / Spotify keys** | optional — extra ratings, awards and music genres |

Recommended models: a 20–32 B reasoning-capable model as the *curator*
(e.g. `qwen3.6:27b`), something small and fast as the *summariser*
(e.g. `granite4.1:8b`), and `nomic-embed-text-v2-moe` for embeddings —
which runs on CPU by design, so the GPU stays free for the curator.
AniList and MusicBrainz need no keys.

### Installation

**Windows**

```bat
git clone https://github.com/Randomname653/Curatarr.git curatarr
cd curatarr
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
start.bat
```

`start.bat` is the development entry point: live console, hot reload, and
it self-heals missing dependencies and Ollama model bakes. For everyday
background use, `start_tray.bat` runs Curatarr as a tray icon with an
autostart toggle, log access and graceful shutdown.

**Linux / macOS**

```bash
git clone https://github.com/Randomname653/Curatarr.git curatarr
cd curatarr
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python build_models.py     # first run: bake the curator + summariser tags
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

### First run

Open `http://localhost:8000`. The setup wizard covers Plex sign-in
(PIN-based OAuth, no password), your Ollama models, \*arr connections,
external API keys, which Plex library maps to which category, and the
admin account. The first sync starts immediately and enrichment queues
itself from there.

> [!NOTE]
> Curatarr binds to `0.0.0.0` so other people in the household can reach
> it. It is built for a trusted home network — see [SECURITY.md](SECURITY.md)
> before exposing it anywhere else.

## Configuration

Settings live in `.env`, written by the setup wizard and editable by hand.
[`.env.example`](.env.example) documents every option with its default;
the ones most people touch:

| Env var | What it does |
|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | Plex server and admin token |
| `OLLAMA_ENDPOINT` | Default `http://localhost:11434` |
| `BASE_CURATOR_MODEL` | Model baked into the `curatarr-curator` tag |
| `BASE_SUMMARIZER_MODEL` | Model baked into the `curatarr-summarizer` tag |
| `RADARR_URL` / `SONARR_URL` / `LIDARR_URL` (+ API keys) | \*arr connections |
| `TMDB_API_KEY` | Primary metadata source |
| `SYNC_INTERVAL_HOURS` | Plex history pull cadence (default 24) |
| `ENRICHMENT_TTL_DAYS` | How long an enriched profile stays fresh (default 90) |
| `EXTRA_GAME_PROCESSES` | Extra `.exe` names that should pause the LLM |

## Documentation

| Document | What's in it |
|---|---|
| [Usage guide](docs/USAGE.md) | Day-to-day operations, maintenance commands, troubleshooting |
| [Architecture](ARCHITECTURE.md) | Data flow, subsystem internals, design decisions, hard-won invariants |
| [Configuration reference](.env.example) | Every setting with defaults and comments |
| [Roadmap](ROADMAP.md) | What's planned and what's deliberately parked |
| [Changelog](CHANGELOG.md) | Condensed release history |
| [Contributing](CONTRIBUTING.md) | Dev setup, test conventions, code style |
| [Security](SECURITY.md) | Threat model and vulnerability reporting |

## Privacy

- **No hosted LLM.** Every prompt goes to your own Ollama instance.
- **Your history stays local.** SQLite and ChromaDB live under `data/`;
  that directory, `.env` and personal exports are all gitignored.
- **Only ids go out.** The enrichment pipeline queries public metadata
  APIs for titles and identifiers — never your viewing behaviour.
- **Taste vectors** are stored unencrypted by default; an opt-in
  PIN-based AES-GCM path exists.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for dev setup and conventions. The test battery is a single command and
CI runs exactly the same one:

```bash
python tests/run_all.py
```

## License

[GNU AGPL-3.0][link-license] — free to use, modify and self-host; derived
work and network-hosted forks must stay open source. Ported components
keep their original licenses, listed in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Acknowledgements

- [SoulSync](https://github.com/Nezreka/SoulSync) — several robustness
  patterns (entity pins, playlist reconcile, staleness guards) are ported
  from it under MIT.
- [Ollama](https://ollama.com), [ChromaDB](https://www.trychroma.com) and
  [FastAPI](https://fastapi.tiangolo.com) carry the stack.
- The [\*arr](https://wiki.servarr.com) projects and
  [Plex](https://www.plex.tv), which Curatarr is useless without.
- Metadata from [TMDB](https://www.themoviedb.org),
  [OMDb](https://www.omdbapi.com), [AniList](https://anilist.co),
  [MusicBrainz](https://musicbrainz.org), [Last.fm](https://www.last.fm)
  and [Wikipedia](https://www.wikipedia.org).

> This product uses the TMDB API but is not endorsed or certified by TMDB.

<!-- badges -->
[badge-license]: https://img.shields.io/badge/license-AGPL--3.0-blue
[badge-tests]: https://github.com/Randomname653/Curatarr/actions/workflows/tests.yml/badge.svg
[badge-python]: https://img.shields.io/badge/python-3.11%2B-blue
[badge-local]: https://img.shields.io/badge/LLM-100%25%20local-E5A00D
[badge-platform]: https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey
[link-license]: LICENSE
[link-tests]: https://github.com/Randomname653/Curatarr/actions/workflows/tests.yml
[link-python]: https://www.python.org/downloads/
[link-ollama]: https://ollama.com
