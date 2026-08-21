# Changelog

Condensed release history, newest first. Fine-grained rationale lives in
the commit messages.

---

## 2026-08-20 — Public release

- **Went public** under [AGPL-3.0](LICENSE); SoulSync-ported components
  remain MIT (see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)).
- **Performance:** deletion scoring and library ranking no longer issue
  one ChromaDB call per item — a chunked bulk prefetch replaces
  thousands of sequential round-trips. The taste engine loads
  enrichment timestamps as column tuples instead of full ORM objects
  (~17× faster at 50k rows).
- **Security:** response-header middleware (`X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`), implemented as pure ASGI
  so SSE streams stay untouched.
- **Accessibility:** aria-labels on all icon-only buttons and unlabeled
  inputs/selects; decorative SVGs hidden from screen readers.
- **Tests:** one battery command — `python tests/run_all.py` — runs all
  suites; CI (GitHub Actions) runs exactly that. Five suites that
  silently executed zero tests under the old convention now run for
  real. 48/48 green.
- **Docs & packaging:** the README became a proper project front page
  (logo, badges, features, quick start) with the deep material moved
  where it belongs — a new [operator guide](docs/USAGE.md) for daily
  use and troubleshooting, and [ARCHITECTURE.md](ARCHITECTURE.md) for
  internals. Added `.env.example` with the complete setting list,
  `CONTRIBUTING.md`, `SECURITY.md`, and a pinned `requirements.txt`.

## 2026-08 — Curation quality: search, judge, integrity

- **Curated semantic search v3.** The LLM only parses the query
  (anchor / constraints); scoring is deterministic against raw metadata
  tags — lexical-first matching, concept/tone families, negation,
  demographic and comedy guards, per-constraint evidence notes, and an
  honest coverage banner when no title carries the full profile.
- **Multi-vector theme facets.** Every title's theme phrases become
  individual vector points in a separate collection, so contrast
  queries ("cute pastel" + "sociopathic manipulation") can hit a title
  in both regions instead of searching the meaningless middle.
- **4-pillar deletion judge.** Taste, household use, custodianship and
  resonance ruled per title from verified evidence (creator/plot/themes,
  awards, Wikipedia significance, community reception, watch state) —
  with learned owner principles extending the constitution, durable
  KEEP protections, and a thin-evidence gate: titles without enrichment
  data are skipped, never judged blind.
- **Two-bake model split.** Chat/persona and batch deletion pitches run
  on separately baked model tags (benchmarked head-to-head), with
  residency-guarded VRAM eviction.
- **Background-job visibility.** Every custodian task reports live
  progress in the Activity view; a test freezes the audited set so new
  jobs are visible by default.
- **Data-integrity sweep.** TV-domain migration, tri-state significance
  (transient failures no longer stamp forever), zombie-document
  self-healing (audit walks the vector corpus, rebuilds deterministically
  from cached prefetch data — no LLM, no confabulation surface),
  adult-genre merge guard, corrupt-source-id detection.
- **SoulSync-ported robustness layer** (MIT): durable entity pins with a
  "Fix match" UI for same-named-work collisions, playlist reconcile
  (delta updates instead of delete+recreate) with stale-key self-heal,
  and an implausible-mass-staleness guard so an unreachable service is
  never mistaken for a deleted library.

## 2026-06 — Grounding, learning, multi-user recovery

- **Verified-data evidence everywhere.** Pitches, discussions and
  reevaluations reason from cached facts (creators, plot, themes, OMDb
  writer + awards, Wikipedia significance) instead of model memory.
- **Entity resolution.** Year + MusicBrainz-id disambiguation so
  same-named works are judged as themselves; watch status comes from
  real Plex history.
- **Learning curator.** Keep-feedback becomes standing considerations
  that softly protect similar titles in future proposals; episodic
  memory extraction with semantic-similarity conflict handling.
- **Platform work.** New environment migration, embedding-stack v2
  (prefix-aware model, new collection, eval-gated migration),
  concurrency lifecycle hardening, recovery flows for deleted Plex
  sections and re-attributed users.

## 2026-05 and earlier — the 1.0 hardening passes

An audit-driven refactor arc (~100 numbered passes) that took the
codebase from prototype to 1.0:

- Critical-path fixes, multi-user attribution, Settings surface,
  concurrency lifecycle, Spotify batch wiring.
- Music pipeline cascade (Plex match → MBID resolve → Spotify genres →
  Last.fm fallback), scheduler missed-job replay, ARR library cache,
  Synopsis Browser, Add-New flow.
- Anime mapping bridge (AniList ↔ TVDB ↔ TMDB), recommendations vector
  path, game-mode VRAM hand-off, scheduler reliability.
- numpy/scipy correctness fixes, MediaIdentity store, server-side game
  watcher, protection-intent detection.
- Music negative caching, proposal reevaluation, Plex user ratings into
  taste, DB-lock cascade fixes, the unified per-library breakdown panel.
