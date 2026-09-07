# Usage guide

Day-to-day operation of a running Curatarr instance. For installation see
the [README](../README.md); for how it works internally see
[ARCHITECTURE.md](../ARCHITECTURE.md).

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

## Where things live in the UI

| Task | Where |
|---|---|
| Re-run Plex sync now | History → **Force sync** |
| Start / resume enrichment | Knowledge Base → Maintenance tab → **Start Enrichment** |
| Recompute taste vectors | Knowledge Base → Maintenance tab → **Recompute taste vectors** |
| Audit + self-heal metadata | Knowledge Base → Maintenance tab → **Audit metadata** |
| Titles that need a human (wrong match, low confidence, repeatedly not found) | Knowledge Base → **Needs attention** tab: filter by reason, then **Search & pin**, **Retry now** or **Ignore** on the row |
| Cache inventory (rows, staleness, size) | Knowledge Base → Overview tab → Storage → **Cache inventory** |
| Review deletion proposals (admin) | Sidebar → **Deletions**: Delete / Keep / Discuss on the card, the rest under **More** |
| Delete several proposals at once (admin) | Deletions → tick the cards → selection bar at the bottom → **Delete selected** |
| Fix a wrongly-matched title | Proposal card → **More** → **Fix match**, or Knowledge Base → Needs attention → **Search & pin** |
| Browse / add media via \*arr | Manage → **TV Shows** / **Movies** / **Music** |
| Re-enrich one library title | Any library row → **Re-enrich** menu (metadata, summary, or both) |
| Reclassify anime ↔ TV (admin) | Manage → **Reclassify**: tick the rows, then **Apply selected** in the bottom bar |
| Watch running background jobs | Sidebar → **Activity** |
| Per-library coverage breakdown | Sidebar → **Libraries** (Library Configuration) |
| Spotify artists not in Lidarr | Manage → **Music** → **Spotify Backlog** tab |

## Command-line helpers

| Command | What it does |
|---|---|
| `python tests/run_all.py` | Full test battery (what CI runs) |
| `python update_db.py` | Idempotent schema migration — run after pulling |
| `python build_models.py` | (Re-)bake the Ollama model tags from `.env` |
| `python import_spotify.py <dir> [--user N]` | Headless Spotify import — the GUI path (Setup → Import, or Admin → Spotify history import) is the same engine |
| `python run_pipeline_spotify.py` | Trigger the music pipeline manually |
| `python scripts/music_enricher.py` | Clear a large music backlog in a separate process |
| `python scripts/mbid_speedrunner.py` | Bulk-resolve MusicBrainz ids |
| `python scripts/dedupe_watch_history.py` | Report play rows that record one viewing twice (`--apply` to remove) |
| `python scripts/facts_speedrunner.py` | Clear the archive-metadata backlog in one go (`--skip-significance` leaves the GPU alone) |
| `python benchmark.py` | Measure a candidate Ollama model's throughput |
| `python scripts/make_icon.py` | Re-render the app icons |

The standalone runners bypass the daily batch caps and share the same
state locks as the in-app pipeline, so they cannot collide with it.

## Tuning

- **Enrichment too slow?** A faster summariser model helps far more than
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

**Settings → Maintenance says the dependencies differ from requirements.txt**

The pins moved (Dependabot bumps them weekly) and this interpreter still
has the old versions. `start.bat` and the tray launcher install the pinned
versions on their own at the next start; if you run uvicorn by hand, do it
yourself with the same interpreter, then restart:

```bash
pip install -r requirements.txt
```

`python -m src.deps_check` prints the comparison without installing.

**A pipeline flag is stuck (`enrichment_running`, `music_pipeline_running`)**

Happens if the process was killed mid-run. The next sync usually clears
it; to force it:

```bash
python -c "from src.services.app_state import force_set_state; \
  force_set_state('enrichment_running', '0')"
```

**"Curator running on CPU" banner**

The curator model didn't fit in VRAM. Reduce `num_ctx`, pick a smaller
`BASE_CURATOR_MODEL`, or free GPU memory. Curatarr keeps working, just
slowly.

**`database is locked` flood (Windows + Syncthing)**

If `data/` sits inside a synced folder, the sync client hashing a live
WAL database causes lock storms. Curatarr writes an exclusion into the
folder's `.stignore` at startup and leaves it in place — a live database
is never safe to file-sync, running or not. If locks persist, confirm
`data/` is actually excluded in your sync client.

**A title is enriched as the wrong work**

Two same-named works (remakes, unrelated films sharing a title) can
collide. Use **Fix match** on that title's proposal card (under **More**) or
**Search & pin** in the Knowledge Base's Needs attention tab: the pin overrides
every automatic identifier source and survives rescans and
re-enrichment.

**A whole service looks "gone" after downtime**

It isn't deleted — Curatarr refuses to treat an implausible mass of
missing items as real deletions and skips that service in the audit.
Bring the service back and re-run the audit.

**Recommendations feel generic**

Usually a coverage problem: check per-library enrichment coverage in the
Knowledge Base. Genre-only fallback ranking is used for titles without a
profile, and it is much weaker than the vector path.
