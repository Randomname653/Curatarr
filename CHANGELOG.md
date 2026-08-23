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
- **The distiller was reading the wrong article.** Wikipedia states what a
  subject is in its opening sentence; the plausibility check scanned 1,500
  characters past it, far enough to reach a passing mention. So a lookup for
  the 2013 series accepted the page on the Birmingham street gang that
  inspired it, the distiller correctly reported no significance, and that was
  stamped as a permanent verdict. The same held for Fargo and Alien, whose
  disambiguation pages open "usually refers to" and "most commonly refers to"
  while the guard knew only "may refer to". Judged on the lead sentence now,
  with the whole family of disambiguation phrasings caught: Peaky Blinders
  returns its National Television Award, Fargo its seven Oscar nominations,
  Alien its Academy Award. 2,069 titles carried an empty "no significance"
  stamp, and the ones that carried text were no safer — where the wrong
  article had awards, another work's standing was attached to the title.
- **A failed second fetch is no longer a permanent verdict.** The tri-state
  return exists because a transient failure once left a title
  significance-less for good. The search call honoured it; the follow-up
  article fetch did not, and a rate-limited response fell through to
  "definitively nothing". Wikipedia also gained the backoff every other
  rate-limited source here already had — it was the only one that answered a
  "slow down" by moving on.
- **The stamp now covers how the article was chosen, not only what the model
  was asked.** A perfect prompt over the wrong page yields a confident
  nothing, so retrieval rules are part of the version hash and tightening
  them retires the answers they produced. The just-in-time path tests that
  version too; it used to gate on the bare "checked" flag, which is what made
  a wrong verdict permanent there.

- **The privacy section said less than the code does.** "Only ids go out"
  was already inaccurate: enrichment searches TMDB, Wikipedia, Jikan,
  AniList, MusicBrainz and Last.fm *by name*, so those services learn which
  titles and artists a library holds. OMDb alone is queried purely by id.
  Corrected in the README, ARCHITECTURE and SECURITY, and stated the way it
  actually splits: titles go out, behaviour does not — watch history,
  ratings, the taste vector and anything typed stay on the machine. The
  local-LLM promise is unchanged and remains true.
- **Dead cache rows are collected again.** An earlier pass removed the
  expired-row cleanup because nothing called it; the rows kept accumulating,
  13,201 of 92,474 of them, on a table two hot paths scan in full. Every read
  path already filters on expiry, so they were unreachable rather than merely
  stale. A daily custodian tick now prunes them — the lesson taken last time
  was to delete the method, the one available was to call it.

- **The same class, hunted across the codebase.** Four more instances of
  transient-as-permanent, each fixed the same way: studio and director notes
  cached "NONE" for *ten years* when Wikipedia or the summariser merely
  failed to answer (394 poisoned rows purged; they re-resolve on demand);
  Last.fm negative-cached a 429 as "no such artist" for a week; the
  MusicBrainz→Deezer resolver froze a thrown request as "no link" for 60
  days; and the chat-memory extractor advanced its cursor past message
  windows the model never processed, permanently dropping them from
  personalisation. Director names are also matched with romanisation folding
  now — the credits say "Shinbou" where Wikipedia writes "Shinbo", and the
  exact-name guard permanently rejected the very people it was built to
  find.
- **A silent regression, caught by its own class.** The cache-addressing
  patch used `cache_id` inside `topup_franchise` without adding the
  parameter, so every call raised NameError — swallowed at debug level,
  invisible to the test battery. Fixed, with the transient guard the
  function never had (an AniList rate-limit no longer stamps "no franchise
  graph" permanently), and a test now walks the AST to assert every function
  declares the names it uses.
- **The setup wizard no longer eats hand-added settings.** Re-running it
  rebuilt `.env` from its template alone, silently destroying every key
  outside it — ten of thirty-three on the reference install, including the
  SoulSync connection. Unknown keys are carried over verbatim into a
  preserved section.

- **Silence is no longer mistaken for emptiness.** A TMDB 429 and "this
  film has no reviews" both arrived as an empty list, so an outage was
  stamped as a permanent "no community data" — 1,148 of 4,570 checked titles
  carried nothing at all, indistinguishably. Every reception fetcher now
  answers three ways (data, definitively nothing, did-not-answer), a source
  that did not answer raises instead of stamping, and a busy condenser counts
  as not answering. Checked-but-empty entries from before the distinction
  are offered exactly once more. OMDb had the inverse bug: "Movie not
  found!" — a definitive answer — was treated as transient, so the title
  stayed pending forever and was re-queried on every walk; found-nothing now
  stamps, quota exhaustion still never does.

- **The article is resolved by identity, and only then by name.** Asked why
  only English Wikipedia is used, the measurement answered a different
  question: of 120 titles stamped "no significance" that carry an IMDb id, 72
  had an English article the name search could not reach — filed under
  disambiguators like "The Fall Guy (2024 film)" — while only 6 had solely a
  Japanese article and 0 only a German one. The bottleneck was never the
  language; it was guessing names. The IMDb id now resolves the exact article
  through its Wikidata sitelink before any name is tried, with the entity's
  claim verified rather than trusted from search — tt0000000 exists on
  Wikidata as someone's placeholder, attached to a real film, so all-zero ids
  join nothing. A sitelinked article needs none of the name guards: identity
  came from the id.

- **Every name of a work gets a Wikipedia turn.** The library files
  "Frieren: Beyond Journey's End" — which is the article's name — while the
  enriched record inside says "Sousou no Frieren", and the search ran on the
  inner name alone. The exact-match guard then rightly refused to bridge two
  different names, so one of the defining anime of the decade was stamped
  "no documented significance"; in a sample of the affected class, Wikipedia
  had the article for 8 of 13. The library's title now rides along as a
  known alias and the direct lookup and the search try each name in turn.
  Recommendation lists (`similar_titles`) are explicitly never aliases —
  those are other works, and using them would attach a neighbour's fame.
  What remains empty after this is mostly honest: a mid-tier seasonal anime
  has no English Wikipedia article, and its standing evidence comes from the
  reception and on-record sources instead.

- **A walker that could not find the row it was meant to write.** Raw cache
  entries are filed under the *library's* title, while the `title` inside them
  is the enriched one — so the row for "Frieren: Beyond Journey's End" is
  titled "Sousou no Frieren", and "Dan Da Dan" holds "DAN DA DAN". Every
  top-up rebuilt its lookup key from that inner title, missed, found nothing
  to write and returned "nothing to do". The walker counted the title as
  visited and moved on, so it stayed outstanding for good and coverage
  plateaued with no error anywhere. Measured: 342 of 400 pending Wikidata
  titles were unreachable, and 30% of pending significance titles. The key a
  row was read from is now carried into the work list and tried first —
  the same lesson as the *arr ids: do not re-derive an identity you are
  already holding.
- **The backfill keeps a couple of titles in flight.** Measured per
  significance title: 0.6s of Wikipedia and 2.0s of model, strictly
  alternating, so the card idled through every fetch and the network through
  every distillation. Overlapping them is worth about 23% — which is also the
  entire ceiling an offline Wikipedia copy could offer, at 100 GB and a
  rewritten retrieval path. Deliberately narrow: Ollama serialises requests
  for one model on one card, so a wider pool would only queue, and would press
  harder on services that asked us to be gentle.

- **Wikidata joins Wikipedia** rather than replacing it. Wikipedia is prose
  and must be distilled; Wikidata is a graph of statements, so the source of
  an adaptation and its named awards are looked up rather than summarised —
  no model, no key, nothing to get wrong. They sit on separate evidence
  lines so it stays obvious which was distilled and which was read.
- **The *arr services already knew the ids we were missing.** A quarter of
  the library had no IMDb id — anime is enriched from AniList and never
  touches TMDB — and two sources key on exactly that id. Sonarr held the id
  for 1,833 of those titles all along, from a call made on every sync.
  Harvesting them lifted id coverage from 76% to 96%.
- **Deciding *which* entry a title refers to is the part worth distrusting.**
  What an *arr says about its own entry is ground truth; matching a cached
  title to that entry by name is not. So an id join comes first and reads no
  title at all, the name is only consulted afterwards, and the year holds a
  veto over it — franchises reuse names across decades. Anime is looked for
  in Radarr as well as Sonarr, because anime films live there while the
  series live in Sonarr. Audited against an independent anidb→tvdb mapping,
  the name step agreed on 1,788 of 1,796 checkable titles; seven of the eight
  disagreements were the mapping pointing at a parent series. The one real
  error was a 2013 film wearing the id of the 1978 series of the same name —
  the year was in both records the whole time, unused. Claims carry the rule
  version that produced them, so tightening the rules offers the old ones for
  re-judgement rather than freezing them.
- **Backfill is offered where the gap is visible.** A fresh install has none
  of this metadata, and the daily tick fills it at a pace meant for a library
  that has been running for months. The Knowledge Base now shows a "Finish
  the backfill" panel with coverage per source and a button each — and it
  removes itself once a source is well covered, leaving the remainder to the
  ordinary ticks. Titles that can never gain a source (no IMDb id, a quarter
  of the library) count as settled rather than holding the offer open for
  good. `scripts/facts_speedrunner.py` does the same from a terminal, with
  `--skip-significance` for a run that never touches the GPU.

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
