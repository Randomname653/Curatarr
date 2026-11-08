# Curatarr — Roadmap

_Last updated: 2026-11-08. This is the working plan, not a promise — items
move as live usage teaches us things. History lives in [CHANGELOG.md](CHANGELOG.md)._

---

## Now (in progress / upcoming)

**Plays while Curatarr is off** (done for both servers, see below)

Curatarr may run only a few hours a day on a PC, without a NAS. Plex keeps
one history event per play on the server, so the next sync picks up
everything played meanwhile. Jellyfin keeps only each item's current state
(played, how often, when last), so what the delta poll recovers after a gap
is coarser:

- a title played twice while Curatarr was off counts twice, but only the
  last play has its date and time; the device is unknown;
- a film stopped halfway is not marked played on the server and is not seen
  at all.

The Playback Reporting plugin stores every play, but Curatarr reads it only
once per member, for the time before Curatarr. Reading it again for the
window Curatarr was off closes the gap for households that have the plugin.
Plex in turn sees running sessions only through the daily sync; polling
`/status/sessions` would give it the live view Jellyfin already has.

**Playlists and collections, checked against the live server**
- How reliably the "Curatarr Recommended" playlists and the designed
  collections follow the recommendations (they refresh weekly, the
  playlists earlier when the sync finds a member's picks all sampled),
  where they show up in Plex and
  Jellyfin, and whether collections should be promoted to the home screen.

**Still being watched after the NAS move**
- **Needs attention down to the real cases.** What remains are AniList
  entries for another work, and anime no source knows under their English
  title.
- **Game detection on the PC.** A new game is caught by its share of the
  card's 3D engine, but one idling in its menu draws too little for the
  threshold. Tune the threshold on real games before lowering it.

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

**Taste engine stage 3**
- Per-facet taste centroids. Today a member's taste in a category is one
  to five centroids over whole-title embeddings. Per facet means separate
  centres for what a title is about, how it feels and how it looks, so
  deletion scoring and recommendations can match on the facet that
  carries the taste instead of on a blend of all of them.

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
- **Emby** — a third adapter behind the media-server seam, only with a
  test server to build against. Jellyfin forked from Emby, but the
  adapter uses the 10.9 routes Emby does not have, Emby has no Quick
  Connect, and its numeric item ids look like Plex keys, so the id memory
  would have to store the server kind with every key.
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

## Done since the last update

- **Jellyfin** — one install runs on Plex *or* Jellyfin 10.9+ and moves
  between them from Settings, both ways, keeping its history. Tested
  against a live Jellyfin (sign-in, all play-history tiers, writes, the
  move and its undo). Design in [docs/JELLYFIN_DESIGN.md](docs/JELLYFIN_DESIGN.md).
- **Memory floor after the NAS move** — stayed flat after the facet index
  went to one point per phrase.
- **Curator tone per member** — friendly, neutral or sharp, with the same
  rules on facts and on the user's own account for all three.
- **Because you watched A, B and C** — a recommendation lane and a
  playlist per member; **Rediscover** and **A year ago** rows beside it.
- **Milestones** with a history, **Your decades** and the **anime season**
  in the Wrap-Up.
- **Collaboration credits** count for their artists (first-billed artist
  known, the credit itself unknown); the Knowledge Base files them as credits.
- **Collections** edited in place and shown on the home screens (Plex),
  with a season shelf per library (the household's calendar and language).
- **Playlists** in each member's own account (the server token per
  account), and the music playlist without a mapped music library.
- **Jellyfin plays while Curatarr is off** caught up at once, without any
  plugin; Playback Reporting adds times and devices.

## Decided against

- **Updating the container from inside the app.** Curatarr would need the
  Docker socket, which is root on the host. The sidebar says when an image
  is published; the redeploy stays with the host (`pull_policy: always`
  makes it one save).
- **A facet compaction** — the rebuilt per-title graph answered worse;
  one point per phrase (2026-10-25) made it unnecessary.
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
