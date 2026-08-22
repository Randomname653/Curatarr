# Changelog

Condensed release history, newest first.

---

## 2026-08-22 — What the judge is given to reason from

- **Community reception was not slow, it was broken.** One exit from the
  reception builder returned two values where every other returned four, so
  the caller raised on every film and show. The exception was logged at debug
  level, which made a total failure look like a slow backfill for months —
  anime left through a different branch and worked, which is exactly why the
  gap read as an ordering problem. A second exit returned three. Both fixed,
  and a test now asserts that every exit from that function agrees.
- **Wikidata joins Wikipedia** rather than replacing it. Wikipedia is prose
  and must be distilled; Wikidata is a graph of statements, so the source of
  an adaptation and its named awards are looked up rather than summarised —
  no model, no key, nothing to get wrong. They sit on separate evidence
  lines so it stays obvious which was distilled and which was read.
- **`scripts/facts_speedrunner.py`** clears the archive-source backlog with
  no daily ceiling. `--skip-significance` leaves the GPU alone entirely.

- **The source of an adaptation is now fetched.** Only the screenwriter was
  ever read, so an adaptation of a celebrated novel looked like an anonymous
  genre piece — the metadata said "based on a novel" without ever saying
  whose. The novelist, playwright or original author is pulled from its own
  credit and named separately from the adapter.
- **Cast lists are no longer mistaken for cultural significance.** The
  distiller occasionally returned a verbatim Wikipedia cast section, which
  left the archive pillar reading a character list as standing. A prose rule
  forbidding it already existed; a shape check now enforces it.
- **Episode counts carry the season they sit in.** Three episodes measured
  against a two-season total reports 25% and reads as abandonment; the same
  three were half of a self-contained first series. The evidence says which
  season the plays are in.
- **Distilled answers now carry the rules that produced them.** A cached
  cultural-significance value was a verbatim cast list — but re-running
  today's prompt on the same article produced the awards, three times out of
  three. The entry was months old, written under weaker rules, and "checked"
  meant "never looked at again". Distillations are stamped with a version
  derived from the prompt text, so editing the prompt retires the old
  answers by itself; the background walker offers stale ones again.
- **Community reception is gathered before the verdict, not after.** It was
  left to a background walker, so a title could be judged with none on file
  while the follow-up discussion — which does fetch it — argued from better
  data than the verdict it was questioning.

## 2026-08-21 — Beta hardening

**Version.** The app reports `1.0.0-beta`, and says so in the UI. The
number is declared in one place instead of being repeated across two dozen
file headers that had already drifted apart.

**Play counts mean what they claim.** Replays were counted from raw history
rows, which mistook two things for a rewatch: an episode logged once
partway through and again when it finished, and repeated *abandoned* starts
of an episode that was never completed once — the exact inverse of the
signal. Views are now filtered by completion and collapsed when they fall
inside the same viewing window, and abandoned starts are reported
separately.

**Resumed views no longer leave two rows.** Plex reports a partial view and
the later finished view through two different queries; only the first was
being reconciled. The finished view now promotes the unfinished row it
belongs to. `scripts/dedupe_watch_history.py` repairs what earlier syncs
already wrote — dry run by default, and it only ever removes unfinished
rows.

**Duplicate detection respects external-id namespaces.** TMDB numbers films
and series in separate sequences, so the same number identifies two
unrelated works. Grouping on the bare id paired them and reported the pair
as one title stored twice, quoting the other work's size as the redundancy.
Duplicate lookups and technical-profile lookups are now scoped by media
type, and an unknown type is refused rather than guessed. Genuine
duplicates are unaffected.

**Listening depth counts listens, not skips.** Deletion protection for
music treated every history row as evidence of devotion, including tracks
that were skipped. It now requires a play that finished or ran past two
minutes — the same rule the import path applies. Heavy rotation is
unaffected; the change is felt only at the margin, which is precisely where
deletion candidates live.

**Non-fiction is judged as non-fiction.** A documentary was being measured
against the criteria of prestige drama. The judge now receives an explicit
form line for factual work: research depth and clarity are the yardstick, a
known outcome is the point, and a conventional structure is craft.

**Background text has a deterministic language.** Deletion pitches are
generated with no conversation attached, and language detection fell back
to scanning unrelated chat history — so the same title could be pitched in
one language and re-evaluated in another. Surfaces with no live message now
answer in English; chat still follows the language it is spoken to.

**Fewer round-trips.** Deletion scoring, library ranking, cache
invalidation, the downscale list and the backlog page all issued one query
per item. Each now issues one query per batch — the backlog page measured
35× faster on a full page. Its top-three tie-break is spelled out rather
than left to whatever order the database returned. Metadata lookups against
the optional LAN neighbour run concurrently, and a partial result is no
longer cached as though it were complete.

**Housekeeping.** Search evidence scoring was split into named steps with
scores verified unchanged against the ground-truth matrix. Markup no longer
leaks into generated prose. The test battery pins UTF-8 for child
processes, so a suite is no longer reported as failing because of the
terminal it was launched from. Runtime state and coding-agent scratch files
stay out of the repository.

## 2026-08-20 — Public release

- **Went public** under [AGPL-3.0](LICENSE); ported components remain under
  their original licenses (see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)).
- **Performance:** deletion scoring and library ranking no longer issue one
  vector-store call per item, and enrichment timestamps load as column
  tuples instead of full ORM objects.
- **Security:** response-header middleware (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`), implemented as pure ASGI so
  server-sent event streams stay untouched.
- **Accessibility:** labels on icon-only buttons and unlabeled inputs;
  decorative graphics hidden from screen readers.
- **Tests:** one battery command — `python tests/run_all.py` — runs every
  suite, and CI runs exactly that. Several suites that silently executed
  nothing under the old convention now execute for real.
- **Docs & packaging:** the README became a project front page, with daily
  operation moved to an [operator guide](docs/USAGE.md) and internals to
  [ARCHITECTURE.md](ARCHITECTURE.md). Added `.env.example`,
  `CONTRIBUTING.md`, `SECURITY.md` and a pinned `requirements.txt`.

## 2026-08 — Curation quality: search, judge, integrity

- **Curated semantic search.** The model only parses the query (anchor and
  constraints); scoring is deterministic against real metadata tags —
  lexical-first matching, concept and tone families, negation, demographic
  and comedy guards, per-constraint evidence notes, and an honest coverage
  banner when nothing carries the full profile.
- **Multi-vector theme facets.** Each title's theme phrases become
  individual points in a separate collection, so a query combining opposed
  qualities can match a title in both regions instead of searching the
  meaningless middle.
- **4-pillar deletion judge.** Taste, household use, custodianship and
  resonance, ruled per title from verified evidence — with learned curation
  principles extending the constitution, durable protections for what it
  keeps, and a thin-evidence gate so titles without enrichment data are
  skipped rather than judged blind.
- **Two-bake model split.** Conversation and batch deletion pitches run on
  separately baked model tags, with residency-guarded VRAM eviction.
- **Background-job visibility.** Every maintenance task reports live
  progress, and a test freezes the audited set so new jobs are visible by
  default.
- **Data-integrity sweep.** Domain migration, tri-state significance so
  transient failures no longer stamp a permanent verdict, self-healing for
  orphaned vector documents (rebuilt deterministically from cached data, no
  model involved), an adult-genre merge guard, and corrupt-source-id
  detection.
- **Robustness layer** (ported, MIT): durable entity pins with a "Fix match"
  action for same-named-work collisions, playlist reconcile instead of
  delete-and-recreate with stale-key self-heal, and a mass-staleness guard
  so an unreachable service is never mistaken for a deleted library.

## 2026-06 — Grounding, learning, multi-user recovery

- **Verified-data evidence everywhere.** Pitches, discussions and
  re-evaluations reason from cached facts — creators, plot, themes, awards,
  cultural significance — instead of model memory.
- **Entity resolution.** Year and MusicBrainz-id disambiguation so
  same-named works are judged as themselves; watch status comes from real
  history.
- **A curator that learns.** Keep-feedback becomes standing considerations
  that protect similar titles in future proposals; episodic memory
  extraction with semantic-similarity conflict handling.
- **Platform work.** Embedding-stack v2 (prefix-aware model, new
  collection, eval-gated migration), concurrency lifecycle hardening, and
  recovery flows for deleted library sections and re-attributed users.

## 2026-05 and earlier — the hardening passes

An audit-driven refactor arc that took the codebase from prototype to a
working household deployment:

- Critical-path fixes, multi-user attribution, settings surface,
  concurrency lifecycle, listening-history import.
- Music pipeline cascade (library match → id resolve → genre fill →
  fallback), scheduler missed-job replay, service caches, synopsis browser,
  add-new flow.
- Anime id mapping bridge, vector-based recommendations, game-mode VRAM
  hand-off, scheduler reliability.
- Numeric correctness fixes, a media-identity store, a server-side game
  watcher, protection-intent detection.
- Negative caching, proposal re-evaluation, user ratings folded into taste,
  database-lock cascade fixes, and the unified per-library breakdown panel.
