# Curatarr — Roadmap

_Last updated: 2026-08-20. This is the working plan, not a promise — items
move as live usage teaches us things. History lives in [CHANGELOG.md](CHANGELOG.md)._

---

## Now (in progress / upcoming)

**Reception & archive pillars at full strength**
- **Trakt integration** — community ratings, votes, watched/trending stats
  for movies + shows via the free API. Closes the reception gap the
  per-title walkers would need months for (movies were at 45/6304 checked
  when a David Fincher film got pitched for deletion with zero reception
  on file).
- **Wikidata awards** — structured Oscars/Grammys/BAFTA data via SPARQL and
  IMDb/TMDB id mapping, hardening the significance pillar with facts
  instead of prose distillation.
- **ListenBrainz** (music listener stats, MBID-aligned) and parsing the
  MusicBrainz relationship edges we already fetch (cover-of / live-of /
  part-of).

**Search stage 2 — LLM facets**
- Extend the enrichment schema with three targeted facet texts
  (aesthetics / tone-register / relationship-dynamics) so register
  distinctions ("commercial kitsch" vs. "transgressive art", otome trash
  vs. subversion) become searchable. Stage 1 (theme facets as individual
  vectors) is live; stage 2 runs only where stage-1 measurement says the
  register gap is real.

**Remaining external-eval half-fixes**
- Memory decay clock semantics, the dormant loved-recommendation detector,
  a proper Chroma backup job (one manual snapshot exists), and the
  drop-confirmation gate. Each needs verification against the eval report
  before touching code.

## Next

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
- **Encryption at rest (PIN)** — the flow registers a hash today but
  encrypts nothing yet; needs an architectural pass (server-side vs.
  client-side key derivation), deliberately parked.
- **Movie-night ballot** — household nomination/vote/watch flow
  (SoulSync-inspired), a project of its own.
- **Tautulli importer** — full historical watch attribution for setups
  that have it.
- Extend the music enrichment so the Curatarr actually knows about music
- **Per-episode knowledge** — TMDB carries per-episode data (titles,
  overviews, air dates), but everything here is enriched per SERIES today.
  Watch history is already per episode, so discussions can state the
  episode FACT ("S2E5, last night") while honestly disclaiming episode-
  level plot knowledge. Real per-episode enrichment (its own cache layer,
  its own staleness, ~20x the rows) is a project of its own.

## Standing principles

- Streaming availability is never a curation factor — the library replaces
  streaming, not the other way around.
- Every API we already call gets harvested fully before a new one is added.
- The LLM never invents facts: verified data in, register-honest prose out;
  thin evidence skips the judge instead of feeding it.
- Every live miss the owner catches becomes a test fixture. So please tell me about errors while using it.
