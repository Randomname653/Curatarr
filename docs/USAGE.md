# Usage guide

Day-to-day operation of a running Curatarr instance. For installation see
the [README](../README.md); for how it works internally see
[ARCHITECTURE.md](../ARCHITECTURE.md).

**Contents:** [The first few days](#the-first-few-days) ·
[Where things live in the UI](#where-things-live-in-the-ui) ·
[Changing the models](#changing-the-models) ·
[Command-line helpers](#command-line-helpers) · [Tuning](#tuning) ·
[Troubleshooting](#troubleshooting)

---

## The first few days

Curatarr is useful immediately but gets noticeably better once enrichment
has caught up with your library:

1. **Sync happens on startup.** Watch history lands in the database and
   is attributed per Plex user.
2. **Enrichment queues itself** and works through your library over
   hours to days, depending on size and model speed. Watch it in
   **Activity**; the Knowledge Base page shows per-library coverage.
3. **Taste vectors get meaningful** once a few hundred titles are
   enriched. Before that, recommendations lean on genres rather than
   real semantic fit.
4. **Deletion proposals need enrichment.** Titles without a profile are
   deliberately skipped rather than judged on a bare synopsis, so the
   proposal list fills up as coverage grows.

Everything is resumable. Closing the app mid-pipeline costs nothing —
the data custodian picks up whatever is overdue on the next run.

**Leaving the PC to Curatarr for a while?** Open Knowledge Base →
**Maintenance**, pick a number of hours and press **Keep working**. Until
then Curatarr works through the GPU work that is due (enrichment,
Wikipedia significance, reception, lyrics profiles, taste,
recommendations) without its usual 30-minute pauses, and keeps Windows
from going to sleep (no power settings are changed). It stops by itself
once nothing is left, or when you press **Stop**.

## Where things live in the UI

Items marked *(admin)* are visible to the admin account only.

**Library and knowledge**

| Task | Where |
|---|---|
| Re-run Plex sync now | History → **Force sync** |
| Browse / add media via Sonarr, Radarr, Lidarr | Library → **TV Shows** / **Movies** / **Music** |
| Re-enrich one library title | Any library row → **Re-enrich** menu (metadata, summary, or both) |
| Per-library coverage breakdown | Settings → **Plex libraries** |
| Titles that need a human (wrong match, low confidence, repeatedly not found) | Knowledge Base → **Needs attention**: filter by reason, then **Search & pin**, **Retry now** or **Ignore** on the row |
| Fix a wrongly-matched title | Proposal card → **More** → **Fix match**, or Knowledge Base → Needs attention → **Search & pin** |
| Cache inventory (rows, staleness, size) | Knowledge Base → Overview → Storage → **Cache inventory** |
| Anime scores and search beyond AniList | Settings → Integrations → **MyAnimeList client ID** (free at myanimelist.net/apiconfig, app type "other"; without it Curatarr asks Jikan) |

**Background work**

| Task | Where |
|---|---|
| Watch running background jobs | Sidebar → **Activity** |
| Start / resume enrichment | Knowledge Base → Maintenance → **Start enrichment** |
| Work through the backlog while you are away | Knowledge Base → Maintenance → hours → **Keep working** |
| Recompute taste vectors | Knowledge Base → Maintenance → **Recompute taste** |
| Audit + self-heal metadata | Knowledge Base → Maintenance → **Audit metadata** |

**Deletions and curation** *(admin)*

| Task | Where |
|---|---|
| Review deletion proposals | Sidebar → **Deletions**: Keep / Discuss / Delete on the card, the rest under **More**. Keep pauses suggestions for 90 days; tick **Protect permanently** in its dialog to stop them for good (lift it under Curation) |
| Delete several proposals at once | Deletions → tick the cards → selection bar at the bottom → **Delete selected** |
| See deleted titles that came back | Deletions → **Back after deletion** (shown only when there are any): **Delete again** or **Keep** |
| Reclassify anime ↔ TV | Sidebar → **Reclassify**: tick the rows, then **Apply selected** in the bottom bar |
| Uncensored cut: owned? exists? | Curation → **Upgrades** lists anime whose files on disk are TV or web releases while AniDB says the Blu-ray/DVD release is uncensored. **Search releases** asks Sonarr's indexers for uncensored or Blu-ray releases |

**Music**

| Task | Where |
|---|---|
| Music without Lidarr | Nothing to configure — the daily walk indexes your Plex music; Library → **Music** runs on it (badge "Plex index") |
| Wanted artists (no Lidarr) | Recommendations → **+ Add** or Spotify backlog → **Wish**; Library → **Music** → **Wanted** lists them, green once Plex has them |
| Spotify artists not in Lidarr | Library → **Music** → **Spotify backlog** |
| Lyrics on file, artists profiled | Knowledge Base → **Music pipeline** (the line under the stats bar; both walkers run with **Run maintenance now**) |

## Changing the models

The setup wizard's Ollama step picks the models. To change them later:

1. Set `BASE_CURATOR_MODEL` (chat, verdicts, recommendations) and/or
   `BASE_SUMMARIZER_MODEL` (enrichment) in `.env`. Any model name from
   the [Ollama library](https://ollama.com/library) works.
2. Rebuild Curatarr's model tags. The script downloads the new base
   models first:

   ```bash
   python build_models.py
   ```

3. Restart Curatarr.

On a card with less than 24 GB, choose a curator whose download size
(shown on its Ollama library page) leaves a few GB of VRAM free for the
conversation. Smaller models work, with softer verdicts; the
"Curator running on CPU" banner tells you when a model is too big.

On Windows, `start.bat` and `start_tray.bat` check the models at every
start and run step 2 for you when a model is missing.

## Command-line helpers

Run these from the Curatarr folder, with Curatarr's Python. Activate the
venv first (`venv\Scripts\activate` on Windows,
`source venv/bin/activate` elsewhere), or call its Python directly, for
example `venv\Scripts\python update_db.py`.

| Command | What it does |
|---|---|
| `python -m src.services.data_custodian --sprint 10` | **Keep working** for 10 hours from the console (`--sprint 0` stops it) |
| `python update_db.py` | Apply database migrations without starting the server (the server does the same at every start) |
| `python build_models.py` | (Re-)build the Ollama model tags from `.env` |
| `python import_spotify.py <dir> [--user N]` | Headless Spotify import — the same engine as Setup → Import, or Settings → Users → Spotify history import |
| `python run_pipeline_spotify.py` | Trigger the music pipeline manually |
| `python scripts/music_enricher.py` | Clear a large music backlog in a separate process |
| `python scripts/mbid_speedrunner.py` | Bulk-resolve MusicBrainz ids |
| `python scripts/dedupe_watch_history.py` | Report play rows that record one viewing twice (`--apply` to remove) |
| `python scripts/facts_speedrunner.py` | Clear the archive-metadata backlog in one go (`--skip-significance` leaves the GPU alone) |
| `python benchmark.py` | Measure a candidate Ollama model's throughput |
| `python tests/run_all.py` | Full test battery, what CI runs. **Stop Curatarr first**: the vector store admits one process at a time |
| `python scripts/make_icon.py` | Re-render the app icons |

The standalone runners skip the daily batch limits and use the same
locks as the in-app pipeline, so they cannot collide with it.

## Tuning

- **Enrichment too slow?** A faster summarizer model helps far more than
  anything else. `ARR_PRE_ENRICH_BATCH` controls how much is enriched in
  the nightly pre-pass.
- **Curator responses too slow?** Check the "running on CPU" banner — a
  model that doesn't fit in VRAM is an order of magnitude slower.
  `MAX_CONCURRENT_CURATOR` stays at 1 for a single GPU by design.
- **Deletion proposals feel wrong?** Argue with them in the proposal's
  discussion thread. Keep decisions and stated preferences are learned
  and applied to future proposals — that feedback loop is the intended
  way to calibrate it.
- **Gaming on the same machine?** Add your launcher or game executables
  to `EXTRA_GAME_PROCESSES`; the models are evicted from VRAM while they
  run.

---

## Troubleshooting

### Starting and signing in

**Other devices on the network can't open Curatarr**

Check, in order:

1. The address: other devices use the Curatarr machine's LAN address,
   for example `http://192.168.1.50:8000`, not `localhost`.
2. The firewall: on the first start Windows asks whether Python may
   accept connections. Allow it on **private** networks. If you dismissed
   the prompt, allow Python under Windows Security → Firewall & network
   protection → **Allow an app through firewall**.
3. Setup not finished yet: until an admin account exists, other devices
   must enter the one-time setup code that Curatarr prints in its console
   and log at every start.

**After approving the Plex sign-in, the plex.tv tab tries to open `localhost`**

The sign-in itself worked: close that tab, and the Curatarr tab you
started from signs you in within a few seconds. The plex.tv tab is sent
to `PLEX_REDIRECT_URI`, which defaults to `http://localhost:8000` and
only resolves on the Curatarr machine itself. To send it to Curatarr
instead, set it in `.env` to the address the household uses, for example
`PLEX_REDIRECT_URI=http://192.168.1.50:8000`, then restart Curatarr.

**"Ollama is not answering" when Curatarr starts**

Start Ollama, then start Curatarr again. Curatarr also checks again at
startup, so the warning alone does no harm, but nothing that needs a
model works until Ollama answers. If Ollama runs on another machine,
set `OLLAMA_ENDPOINT` in `.env`.

**The console shows `[ERROR] Curatarr exited with an error`**

Read the output above the error. The usual cause is another program
already using port 8000: `start.bat` always serves on port 8000, so stop
the other program and start again. On Windows,
`netstat -ano | findstr :8000` shows the process id that holds the port.

### The models and the GPU

**"Curator running on CPU" banner**

The curator model doesn't fit in your GPU's memory. Free GPU memory
(close games or other AI tools), or switch to a smaller curator model as
described in [Changing the models](#changing-the-models). Curatarr keeps
working in the meantime, only slowly.

**"The GPU is busy with another program right now" in the chat**

Something else (an image generator, a benchmark, a game) is holding the
graphics card, and the curator's model is too large to load next to it.
Background work keeps running on the processor, so enrichment and lyrics
profiles stay current; only the conversation waits. End the job that holds
the card, or come back when it is done. The badge in the top bar shows the
same state — green **Game mode**, amber **GPU busy · CPU lane** while the
background work continues, amber **GPU busy · paused** when it does not —
and its tooltip names the occupancy.

Settings → Integrations → **Sharing the graphics card** changes the
behaviour without a restart: whether a busy card is noticed at all, whether
the background work moves to the processor, how many threads it may take
(six by default, measured as fast as twelve) and how much free memory it
needs before it starts. That last one matters: one enrichment run took
9.7 GB of RAM, so below 12 GB free the background work waits rather than
push the machine into swap — and the chat notice then says it is waiting
instead of promising progress. The setup wizard asks the same questions on
its Ollama step. In `.env` the keys are `GPU_PRESSURE_GATE`,
`LLM_CPU_LANE`, `LLM_CPU_THREADS` and `LLM_CPU_MIN_FREE_MB`.

### Library data

**A title is enriched as the wrong work**

Two same-named works (remakes, unrelated films sharing a title) can
collide. Use **Fix match** on that title's proposal card (under **More**) or
**Search & pin** in the Knowledge Base's Needs attention tab: the pin overrides
every automatic identifier source and survives rescans and
re-enrichment.

**Recommendations feel generic**

Usually a coverage problem: check per-library enrichment coverage in the
Knowledge Base. Genre-only fallback ranking is used for titles without a
profile, and it is much weaker than the vector path.

**A whole service looks "gone" after downtime**

It isn't deleted — Curatarr refuses to treat an implausible mass of
missing items as real deletions and skips that service in the audit.
Bring the service back and re-run the audit.

**`database is locked` flood (Windows + Syncthing)**

If `data/` sits inside a synced folder, the sync client hashing a live
WAL database causes lock storms. Curatarr writes an exclusion into the
folder's `.stignore` at startup and leaves it in place — a live database
is never safe to file-sync, running or not. If locks persist, confirm
`data/` is actually excluded in your sync client.

**A pipeline flag is stuck (`enrichment_running`, `music_pipeline_running`)**

This happens when the process was killed mid-run. The next sync usually
clears it. To clear it yourself, run this from the Curatarr folder with
the venv active (replace the key with the stuck
flag):

```bash
python -c "from src.services.app_state import force_set_state; force_set_state('enrichment_running', '0')"
```

### Python and dependencies

**Which Python runs Curatarr**

`start.bat` and `start_tray.bat` pick one interpreter and say which: a
`venv` (or `.venv`) in the Curatarr folder first, else Python 3.12 through
the Python launcher (`py -3.12`), else whatever `python` is on PATH. The
console window prints it, and the app log has a `[python]` line for every
start. A shared Python works, but the launcher raises packages to the
tested versions in it, which can push other tools in the same environment
past their own pins. A venv keeps Curatarr apart. Create it once, and the
next start fills it with the tested versions (a few minutes the first
time):

```bash
py -3.12 -m venv venv
```

**Settings → Maintenance says the installed packages differ from the
tested versions**

An update changed Curatarr's dependencies and this Python still has the
old ones. `start.bat` and `start_tray.bat` fix this by themselves at the
next start. If you start uvicorn by hand, run both commands with the same
Python, then restart Curatarr:

```bash
pip install -r requirements.txt
python -m src.deps_lock --apply
```

`python -m src.deps_check` prints the comparison without installing
anything. How the pinned versions and the lock work, and how to change
them, is in [CONTRIBUTING.md](../CONTRIBUTING.md#dependencies).
