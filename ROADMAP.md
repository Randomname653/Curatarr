# Curatarr — Roadmap

_Last updated: 2026-10-23. This is the working plan, not a promise — items
move as live usage teaches us things. History lives in [CHANGELOG.md](CHANGELOG.md)._

---

## Now (in progress / upcoming)

**Settling in after the NAS move**

Curatarr now runs around the clock in a container, with the gaming PC's card
as a second host. Both cards work at once, and both installs update from the
app. What the first days of that setup still have to show:

- **Memory floor.** The growth came from the facet writer (fixed). What is
  left after a restart is mostly the facet index (~1.2 GB, about 40% of it
  dead slots). Watch that it stays flat; if it still climbs per enrichment
  item, measure the live enrichment path. The offline measurement could not
  reproduce its network clients.
- **Needs attention down to the real cases.** The weekly audit clears the
  false wrong-entity findings, and Plex-index music no longer counts as
  "no id". What remains are AniList entries for another work, and anime no
  source knows under their English title.
- **Game detection on the PC.** A new game is caught by its share of the
  card's 3D engine, but one idling in its menu draws too little for the
  threshold. Classifying it covers that. Tune the threshold on real games
  before lowering it.

**Search stage 2 — LLM facets**
- Extend the enrichment schema with three targeted facet texts
  (aesthetics / tone-register / relationship-dynamics) so register
  distinctions ("commercial kitsch" vs. "transgressive art", otome trash
  vs. subversion) become searchable. Stage 1 (theme facets as individual
  vectors) is live; stage 2 runs only where stage-1 measurement says the
  register gap is real. Weigh it against the facet index's memory, see
  below.

**Remaining external-eval half-fixes**
- Memory decay clock semantics, the dormant loved-recommendation detector,
  a proper Chroma backup job (one manual snapshot exists), and the
  drop-confirmation gate. Each needs verification against the eval report
  before touching code.

## Next

**A smaller facet index**
- 199k facet points carry only 88k distinct phrases. Deduplicating the
  phrases (one vector per phrase, titles pointing at it) would about halve
  the index and probably help recall.
- A compaction that keeps recall. The rebuild drops the dead slots
  (~450 MB), but the rebuilt graph answered worse (recall@20 0.81 against
  0.89), so `FACET_COMPACT` stays off. Try a higher HNSW `M` / build
  order before turning it on.

**Needs attention, the rest automated**
- Anime whose AniList entry is another work (a film for a series, a sequel
  for the first season): re-resolve through the TVDB → AniList crossref
  where it names exactly one entry, before asking a human.
- Collaboration credits from the Plex index ("A & B", "A feat. B") folded
  into the member artists, so they stop counting as artists of their own in
  the Knowledge Base.

**Two cards, visible**
- One view of what each card is doing right now: the lane's holder and
  queue, the PC's state (free / busy / which game / away), and the memory of
  the container. Today it is spread over the log, the badge and Activity.

**Household**
- Netflix viewing-history import (CSV) for household members, next to the
  Spotify import.

**Discovery features on the taste-vector stack** (SoulSync-inspired)
- **Decade time machine** — "my taste, but the 1980s": taste-similarity
  partitioned by era, one lane per decade.
- **Consensus recommendations** — "because you watched A, B and C":
  scored from taste-vector distances, the curator narrates the connection.
- **Seasonal collections** — a seasonal hint for the collection designer
  (October → horror, December → cozy) — one context line, rotates
  automatically.

**Curation reporting**
- Curation report + yearly review expansion: watch stats, discovery
  follow-up verdicts, taste drift over the year, biggest additions —
  the "household year in media" the current report only sketches.

**Taste engine stage 3**
- Per-facet taste centroids (the multi-centroid machinery exists) for
  deletion scoring and recommendations.

## Later

- **Release watchlist** — follow directors/studios/creators; notify the
  household when something new lands and it fits the taste profile.
- **Enrichment vocabulary expansion** — the tag vocabulary is the truth
  layer of search; register cases keep a running fixture list.
- **German locale** as a global UI/curator language setting.
- **Movie-night ballot** — household nomination/vote/watch flow
  (SoulSync-inspired), a project of its own.
- **Tautulli importer** — full historical watch attribution for setups
  that have it.
- **Deeper music enrichment** — give the curator the same depth of
  knowledge about music that it has about films and shows. Music awards
  (Grammys) from Wikidata belong here. So do MusicBrainz relationship
  edges (cover-of / part-of), if album-MBID capture ever lands; they were
  de-scoped because each would queue behind the 1.1 s MusicBrainz gate.
- **Per-episode knowledge** — TMDB carries per-episode data (titles,
  overviews, air dates), but everything here is enriched per SERIES today.
  Watch history is already per episode, so discussions can state the
  episode FACT ("S2E5, last night") while honestly disclaiming episode-
  level plot knowledge. Real per-episode enrichment (its own cache layer,
  its own staleness, ~20x the rows) is a project of its own.
- **Unattended PC updates** — the agent updates itself when the bell says
  so; an opt-in "update while the PC is idle" could do it without the
  click. Off by default: an update during a reply is the one thing nobody
  wants.
- **Stronger remote sign-in** — hardware keys through the identity
  provider in front of the public address.

## Decided against

- **Updating the container from inside the app.** Curatarr would need the
  Docker socket, which is root on the host. The sidebar says when an image
  is published; the redeploy stays with the host (`pull_policy: always`
  makes it one save).
- **Compaction on by default** — see "A smaller facet index": less memory
  is not worth worse search.
- **Resolving anime through AniDB ids.** That id space drifts; measured, it
  led to sequels and specials. Names in the offline snapshot, with the
  year, are the fallback instead.

## Standing principles

- Streaming availability is never a curation factor — the library replaces
  streaming, not the other way around.
- Every API we already call gets harvested fully before a new one is added.
- The LLM never invents facts: verified data in, register-honest prose out;
  thin evidence skips the judge instead of feeding it.
- Every live miss the owner catches becomes a test fixture — so if
  something goes wrong while you use it, please
  [open an issue](https://github.com/Randomname653/Curatarr/issues).
