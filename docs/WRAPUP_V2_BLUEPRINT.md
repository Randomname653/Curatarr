# Wrap-Up v2 — Research & Telemetry Blueprint

**Status:** research only, no application code changed. Written against
`claude/bold-johnson-w1warc` at `df62110` (Randomname653/Curatarr#214).
**Companion to:** `docs/WRAPUP_DESIGN.md` (v1 design and market scan §1.4–1.7).

A note on scope: the old Curation Report code (`curation_stats.py`,
`routers/stats.py`, `report.js`) now only feeds the admin ledger. v1 itself
lives in `src/services/wrapup_facts.py`, `src/services/wrapup_story.py`,
`src/routers/wrapup.py` and `frontend/js/wrapup.js`, so that is what the
critique below reads.

**The headline, before anything else.** v1's biggest problem is not the
prompt or the layout. **Most of the owner's own numbers come from a sync
path that cannot see repeat plays and records every play as finished.**
(Section 3, row 1; checked in code.) Several tiles and two archetype traits
were measuring that path, not the person. The good news is that the
missing history can be rebuilt from Plex before the season starts.

---

## 1. Why v1 misses the mark

### 1.1 The validator is rigid *and* leaky — the worst of both

`check_story` (`wrapup_story.py:341-374`) checks four things:
- every numeral in the prose must be in a set made of the numbers printed on
  the fact sheet plus 0–10 (`:366-369`);
- 22 banned phrases (`:82-88`);
- no `!` anywhere (`:373`);
- the reference IDs.

Run against real phrasings (probed with `numbers_in` / `_CLICHE_RE`):

| What the model writes | Sheet says | Verdict | Why |
|---|---|---|---|
| "nearly **2** hours" | 118 min | ✅ passes | Only because 0–10 is always whitelisted, not because 2 ≈ 118/60 |
| "about **58** hours" | `58.3 hours` | ❌ reject | Rounding is a violation |
| "nearly **60** hours" | `58.3 hours` | ❌ reject | Same |
| "your **90s** streak" | viewing age `1995` | ❌ reject | The sheet never prints decades, so `90` and `1990` don't exist |
| "**+40%** on last year" | `prev_hours` 41, `hours` 58 | ❌ reject | The comparison — the joke — is a derived number |
| "*Mamma Mia!*", "*K-On!*", "*Haikyu!!*", "*Princess Lover!*" | title on sheet | ❌ reject | The `!` ban hits the title itself; anime libraries are full of these |
| "**Journey**", "**Testament**" (bands) | top artist | ❌ reject | Artist names hit the cliché list |
| "**42** hours of Severance" | 42 appears as a *percentage* elsewhere | ✅ passes | Set membership, not claim binding: a wrong number that collides with any other fact goes through |
| "your favourite director, Mann" | no director data | ✅ passes | Non-numeric claims and title names in prose are not checked at all |

So the answer to "does `118 minutes` → `nearly 2 hours` trip it" is **no,
by luck** (the 0–10 whitelist), while the honest phrasings a good writer
uses — rounding above ten, decades, deltas, titles with punctuation — **do**
trip it. Meanwhile a confident wrong number passes when it collides with
another fact, and an invented title or director passes outright. The rules
hit style and miss what matters: whether the claims are true.

**The repair loop multiplies the damage:**
- `write_story` (`:405-465`) gives one repair turn and names only the
  *first* fault. A draft with three soft faults (a rounded number, a "truly",
  a `!` in a title) fixes one and dies on the second.
- Any rejection throws away the whole story, including the eight good
  fields. The page then falls back to `fallback_story` (`:468`), a
  14-entry trait table (`TRAIT_TITLES`, `:450-465`) that says
  "Started it, finished it."

**The format itself flattens the voice:**
- The story is generated under Ollama JSON-schema forcing (`fmt=STORY_SCHEMA`,
  `:422`). That is 15 typed fields, several with hard `max_length`
  (`commentary` 480, `roast` 260, `kicker` 110).
- Tam et al., *Let Me Speak Freely?* (EMNLP 2024), measured that
  constrained JSON decoding degrades reasoning-heavy generation, while it
  helps classification. A wrap-up is the former: finding the angle *is* the
  reasoning.
- The model is asked to do three things in one pass: pick the story, write
  it, and fill a form. It writes form fields.
- If the grammar enforces `maxLength` (llama.cpp's schema converter supports
  string length bounds), a sentence that runs long is cut mid-word rather
  than rejected. Verify this on the curator host.

**The prompt is 80% prohibition:**
- `SYSTEM` (`:293-318`) has ten hard rules, four of them bans.
- The voice is one line ("a sharp critic friend"), with a single
  weak/strong example.
- The fact sheet (`render_fact_sheet`, `:112-286`) is a flat list of ~40
  `F#:` lines, all equally weighted. Nothing tells the model that a
  9.0-vs-6.4 contrarian gap is remarkable and "Peak hour 21:00" is not.

**What the model never sees** is the one thing only Curatarr has: the
person's own words. Months of `ConversationMessage` debates, episodic
memories and principles are reduced to a count ("Most debated: X, 7 of your
messages") and one stance string.

### 1.2 Shallow metrics: what each tile actually measures on real data

| Tile | What it computes | What real Plex/Spotify data makes of it |
|---|---|---|
| **Completion / "Completionist" trait** | `_completion` → `_watched_enough` | **Broken for the owner.** Every owner row from the library listing is written with `completed=True, view_offset_ms=duration_ms` (`plex_sync.py:741-749`). Only never-finished items carry a real offset. Completion reads ~100% and `completionist` fires automatically. Rows with unknown length also count as watched (`viewing_sessions.py:43-44`), which inflates history imports. |
| **Music skip rate** | `1 - completed/n` (`wrapup_facts.py:694`) | **Meaningless.** Plex track rows are always `completed=True`, so the result is 0%. The Spotify import drops every play under 30 s (`spotify_import.py:45,230`), which is where skips live. The tile reports "Skipped 0%" for Plexamp and a large undercount for Spotify. |
| **Comfort rewatch** | Re-completions inside the period plus completions of things finished before it | **Undercounts, then over-reads.** Repeat plays between two syncs collapse into one row (`lastViewedAt` is the only timestamp, `plex_sync.py:698`). The owner's history before the first sync is one row per item ever watched. What survives is dominated by kids' shows on a shared account and fall-asleep sitcoms: the result everyone already knows ("The Office"). It also *feeds two other tiles* — see below. |
| **Contrarian gem** | Biggest gap between user stars (swipe deck) and community, where community is ≤6.8 with ≥200 votes | Swipe stars are sparse, so most hits use the imputed **8.5 for any comfort rewatch** (`:617-618`). The tile turns into "your guilty pleasure has a low TMDB score". TMDB's `vote_average` piles up between 6 and 7.5, so ≤6.8 covers a large share of genre fare. 200 votes is noise for that scale. One title, no distribution: a coin flip with a confident headline. |
| **Highbrow / comfort split** | Acclaimed = ≥7.8 with ≥1000 votes, or MC ≥75, or `significant`; comfort titles subtracted first | Inherits the comfort noise. "Acclaimed" mixes TMDB, AniList and MAL scales via `count_number(vote_count/scored_by)` (`:167`). |
| **Viewing age** | Hour-weighted median release year over the top 60 enriched titles | Series dominate the hours: one 60-hour show outweighs twenty films. A show's year is its *first-air* year, so watching season 9 of a 2005 sitcom in 2026 counts as 2005. Nearly everyone lands in 2012–2019: a number with no surprise and no reference point. Spotify's version works because it compares against your **age cohort**. We have no cohort, but we do have the household and the library. |
| **Comebacks** | Title or artist back after ≥365 days of silence | Keyed on the title *string* (`coalesce(series_title, title)`, `:416`). The most common trigger is **a new season airing**, which says nothing about the person. Pre-install history is one point per item, and in a 30-day window the tile rarely fires at all. |
| **Genre drift** | Share of played time per Plex genre string vs your past; risers/fallers at ±5 pp; "first-time" genres | Plex genres are ~25 coarse labels ("Drama" is on half the library). A multi-genre title is split evenly, so ±5 pp is noise. "First time" catches single plays of *Talk Show*, *Reality* or *Game Show*. The chroma cluster drift (`_drift_sync`) is the better version of the same idea, and it competes with this tile instead of replacing it. |
| **Time of day / night owl** | Shifted by the server's *current* UTC offset (`_local_offset`, `:133`) | Rows are naive UTC and no user timezone is stored. Half the year is one hour off across DST, which moves plays across the 23:00 night-owl boundary. |

There is a pattern under the table: **one noisy signal (comfort) is reused as
evidence in three tiles (comfort, contrarian, highbrow) and two traits.**
When it is wrong, the page is wrong in three places that look independent.

### 1.3 Pacing: why one scroll feels flat

`_render` (`wrapup.js:74`) puts everything on one page, in this order:
- the controls;
- the hero with the archetype already revealed and badges;
- an optional quiz;
- fifteen same-sized bento tiles;
- then the chapters, the roast and the hook;
- then the snapshot and the ledger.

**What goes wrong:**
- **The payoff comes first.** The archetype is the emotional conclusion; v1
  shows it before any evidence. Wrapped, Replay and Letterboxd all *end* on
  identity.
- **The story is spoiled by the tiles above it.** The chapters reuse facts
  the reader has just scanned as numbers, so the prose reads as a recap of
  the tiles.
- **Every tile has the same weight.** "2 comebacks" gets the same box as
  "300 hours". Without hierarchy there is no surprise, and without surprise
  there is no dopamine.
- **No withholding.** The format that works in stories is one idea per
  screen with a beat of suspense before the number. That beat is Loewenstein's
  *curiosity gap*. The quiz is the only place v1 uses it.
- **Peak–end rule.** People judge an experience by its peak and its end. v1's
  end is the admin ledger and a playlist button; its peak is undefined.
- **Weak tiles render because data exists, not because they are
  interesting.** There is no "is this worth saying?" gate between facts and
  UI.

---

## 2. The gold-standard feature matrix

These are the ten concepts worth stealing or inventing, ranked by how hard
they hit. Mechanics are from the 2025 season (see `WRAPUP_DESIGN.md`
§1.4–1.7 and the sources below).

| # | Concept | Who does it | The mechanic | Why it hits | Curatarr version |
|---|---|---|---|---|---|
| 1 | **Predict-then-reveal** | Spotify Wrapped 2025 (guessing games), PlayStation | Guess your top artist or game before the reveal | Curiosity gap plus a self-knowledge test. Being *wrong* about yourself is the memorable part. | 3–4 prediction beats inside the story, not one quiz up front: "Which one did you finish fastest?", "Who argued with the curator more, you or Anna?" |
| 2 | **Age against a cohort** | Spotify *Listening Age* | Release years of your plays vs your peers | A number that means something only because of comparison; a little cheeky | **Viewing age for films only**, compared with *your library's* median and the household. "Your films are 11 years older than what's on the server." |
| 3 | **Clubs and roles** | Spotify 2025 (six clubs, each with roles such as Scout or Archivist) | A small fixed taxonomy, with a role inside the group | Belonging plus distinction: "my people, and my job among them" | The archetype becomes a **two-part identity**: a household role ("the Custodian of the house") plus a personal angle ("…with a soft spot for 80s horror"). |
| 4 | **Rank among fans** | Spotify fan leaderboard / top 0.5% | Your minutes vs every other listener of an artist | Status. Concrete, bragging-sized. | **Household rank** and **server-unique claims**: "You are the only person on this server who finished *Berserk*." |
| 5 | **Auteur constellation** | Letterboxd (most-watched and highest-rated directors and actors, with minimum counts) | Credits aggregated over the year | Taste as authorship ("I'm a Villeneuve person") is identity, not consumption | Director, studio and voice-actor constellations. **Needs a credits table**, see Section 3. |
| 6 | **Divergence from consensus** | Letterboxd (your rating vs average over all rated films) | A rating *distribution*, not one title | "My standards are mine", provable | Contrarian as a scatter across every rated or replayed title, with the Judge's verdict as a third voice. |
| 7 | **New vs returning vs catalogue** | Steam Replay (this year's releases vs older), percentile vs average user | Share of time by release recency | Explains *how* you consume, not just what | Keep v1's split; add **backlog clearing vs hoarding** (time-to-watch, Section 3). |
| 8 | **The year as a grid** | Steam monthly view, PlayStation streaks, GitHub-style heatmaps | 365 cells, streaks, the biggest day | Effort made visible; loss aversion ("don't break the streak") | 365-day heatmap per medium. The biggest day becomes its own beat. |
| 9 | **Specific days over averages** | Spotify 2025 AI "listening archive" of memorable days; Daylist micro-personas ("melancholy Tuesday night") | Hyper-specific naming of a moment | Episodic memory beats aggregates; being *seen* in detail | "The Sunday you watched 8 episodes of *Severance* and then deleted *Fast X*." Day-level vignettes mined from sittings plus curation events. |
| 10 | **The Court Record** *(nobody else has this)* | — | Judge verdicts vs your actions, debates, principles you taught it | You were *in a relationship* with the curator all year; this is its story | Defiance score, debates won or lost, the principle you taught it, the title you saved that it still thinks is clutter, and quotes of your own words back to you. |

Close runners-up that Curatarr can uniquely do:
- **Dialogue density**: `MediaSubtitleProfile.words_per_min` / `silent_share`
  already exist. "31% of your year was silence; your talkiest film was
  *My Dinner with Andre*."
- **Studio reputation**: `studio_notes.py` gives "known for…" notes.
- **Reception splits**: `reception.py` stores divided community opinion
  ("gem of the genre" 9/10 vs "gratuitous" 3/10).
- **Hoarder vs pruner**: GB in vs GB out.

**What the matrix says.** Every top-tier mechanic is a **comparison** (vs
cohort, vs consensus, vs your past, vs the household, vs the Judge) or a
**specific moment**. v1 mostly prints absolutes.

---

## 3. The telemetry gap table

**Legend:** ♻ = can be backfilled for 2026 before the season; ⏳ = capture
starts the day it ships.

| Desired insight | What the code stores today | Why it's broken or insufficient | Exact schema / sync change |
|---|---|---|---|
| **Every play as its own event** (rewatches, comfort, streaks, biggest day, rhythm) | Owner: one row per item per sync, from `/library/sections/{key}/all?viewCount>>0&lastViewedAt>>…` with `viewed_at = lastViewedAt` (`plex_sync.py:387-390, 698`). The full `/status/sessions/history/all` walk (`:1226-1306`) is only used to *attribute* rows (`:616-632`). Members' rows do come from the history API (`:1197-1217`). No unique constraint, dedup in Python on `(user, item, viewed_at)` (`:681-687, 751`). | Two plays of one title between syncs → one row. The owner's pre-install history is one row per item ever. Comfort, comebacks, streaks and the previous-period baselines are all wrong for the owner, the person who curates most. | ♻ Make the **history API the event source for every account** (it already walks it). Add `UniqueConstraint(user_id, plex_item_id, viewed_at)`. Add `history_id` (Plex `historyKey`) for idempotent backfill. One-shot backfill job for the owner's full history; bump a `facts_version` so frozen closed-year `Wrapup` rows (`models.py:913-915`) recompute once. |
| **Real completion / drop / partial rewatch** | Owner rows: `completed=True, view_offset_ms=duration_ms` unconditionally (`plex_sync.py:741-749`). Real offsets only for never-finished items (`viewCount=0`, `:479-480`). Unfinished rows are rewritten forward and then into the completed view within 30 days (`:772-810`), so the start time is lost. | Completion ≈ 100% for the owner; the `completionist` trait fires on everyone the bug touches. A rewatch abandoned halfway is invisible. Sitting start times move. | ⏳/♻ Store `started_at` (never overwritten) and `ended_offset_ms` per event. Write a partial play as its own row with `completed=False` instead of mutating. Plex history has no offsets, so for the past use `completed` from the history (it lists finished views) and treat unknown as unknown, not as done. Change `_watched_enough` (`viewing_sessions.py:43`) so unknown duration is not counted as watched. |
| **Time-to-watch / backlog procrastination** ("hoarded 412 days before pressing play") | Plex video `addedAt`: **not stored**. Radarr/Sonarr `added`: only inside `api_cache['arr_library:{svc}']` with a 30-day TTL (`library.py:597, 629-633`). Import events are fetched live (`recommendations.py:2395-2447`) and kept only as `DeletionProposal.latest_activity_at` for candidates. Plex music `addedAt` sits in `plex_music.db` and is overwritten per walk (`lyrics.py:97, 210, 256`). | No per-item acquisition date that survives a cache prune. Hoarding vs clearing, the "dust collector's age" and the procrastination shelf can't be computed. | ♻ Add `added_at` (Plex `addedAt`, already in the listing payload) and `acquired_at` (earliest arr *grabbed* / *imported* history event) to `MediaIdentity`, written on every sync, **never overwritten with a later value**. Backfill from Plex's current `addedAt` and the arr `/history` endpoints, as far back as they reach. |
| **Library velocity: GB in vs GB out per month** (hoarder vs pruner) | GB freed is derived from `DeletionProposal(status='deleted').storage_mb` (`curation_stats.py:83-91`). GB added: **not stored**. `MediaTechProfile.size_mb` is a snapshot, overwritten and pruned (`plex_sync.py:2347-2370`). Deletions done directly in the arrs are not seen. | Only half the balance exists, and that half covers only deletions that went through Curatarr. | ♻ New append-only `library_events(id, at, kind ∈ {grabbed, imported, upgraded, deleted, returned}, service, arr_id, plex_rating_key, category, size_mb, source ∈ {arr_history, curatarr, plex_diff})`. Feed it from arr history (backfillable), Curatarr's own delete and return paths, and a nightly Plex-diff for items that vanished. |
| **Defiance / rebellion score** (Judge score vs your action) | User keeps a CUT: `DeletionProposal.status='rejected'` plus `CuratorResolutionLog(outcome='kept', override_reason='Card-button keep')` (`recommendations.py:2220, 2244-2252`). Score lives only on the proposal (`confidence = score/100`, `recommendations_engine.py:2851`; `score_factors`). The log has **no `proposal_id`, score, verdict or size** (`models.py:502-510`), so joins go by title. The CUT-vs-STAGNANT verdict and the pillar breakdown sit in a 30-day TTL cache (`verdict_cache.py:32, 55-58`). | The confidence you overruled can't be weighted ("you saved 6 titles the Judge was 90%+ sure about"). There is no pillar story ("Pillar III saved 14 titles for Anna"). Title joins break on renames and duplicates. | ♻ (mostly) Add `proposal_id`, `verdict`, `score`, `pillar` (the deciding pillar), `storage_mb` and `thread_id` to `CuratorResolutionLog`. Backfill by joining on proposal title and `resolved_at` (the proposal rows are kept, `retention.py:16-18`). Persist verdicts in a durable `judge_verdicts(item_key, at, verdict, score, pillars_json, model)` history table instead of only the TTL cache. |
| **User deletes what the Judge kept** | Judge keeps go into `ProtectedMedia(source='judge', verdict=…)`, overwritten on re-judging (`recommendations_engine.py:1464-1468`) and **hard-deleted** when lifted (`recommendations.py:521`). Deletions in the arrs: not recorded. | The other half of rebellion doesn't exist. | ⏳ Soft-delete `ProtectedMedia` (`lifted_at`, `lifted_by`). Write a `library_events(kind='deleted')` row when a judge-kept item disappears, carrying the last verdict. |
| **Debate outcomes: who convinced whom** | Turns are free text in `ConversationMessage` with `thread_id` such as `deletion_proposal:{id}` (`models.py:643-664`). The log's `resolution_type='consensus'` covers *both* "you convinced the curator" and "it convinced you" (`models.py:481-484`). Merged into an existing row within 24 h, or skipped by the flip-flop guard (`episodic_memory.py:1889-1933`, `chat.py:860-866`). Delete stance comes from keyword heuristics (`recommendations.py:1872-1878`). Users can wipe chat (`chat.py:3979-4050`). | The AI Debate Scorecard has no data: no persuader, no turning point, no "you won 7 of 9". | ♻ via a retro-labelling pass: add `persuader ∈ {user, curator, none}`, `turns`, `turning_msg_id` to `CuratorResolutionLog`. Going forward, set it at resolution time from the stance before and after. For the past, run a cheap 8B classifier over existing `deletion_proposal:*` threads (it only needs the first and last stance). Keep a `debate_summaries` row when chat is wiped, so a wipe deletes the words but not the score. |
| **Credits & companies**: director / studio / actor / voice-actor constellations; label for music | All in JSON `api_cache.response` (`raw:{cat}:{id}` 90 d, `enriched:{cat}:{id}` 30 d; `media_enricher.py:209, 241, 4685`): top-8 cast, first director or creator, keywords, language, country (`:2504-2600`); writer from OMDb (`:1119-1151`); studios for anime only (`:2911, 3192`). TMDB `production_companies`, `networks`, `belongs_to_collection` and full crew are **fetched and dropped**. Voice cast isn't labelled. Labels aren't fetched. | Not queryable, and pruned on a TTL. "Most-watched director" needs JSON scans over a cache that may have expired for titles watched in January. | ♻ Durable `media_credits(item_key, role ∈ {director, writer, creator, cast, voice, composer}, person_id, name, ord)` and `media_companies(item_key, kind ∈ {studio, production, network, label}, company_id, name)`, filled from the raw TMDB/AniList payloads the enricher already fetches. Add a `collection` column to `MediaIdentity`. Index on `(person_id)` and `(company_id)`. |
| **Nanogenres / micro-themes** (Letterboxd-style "slow-burn folk horror") | `facet_index.py` writes ~6 theme phrases per title into chroma `media_facets_v1`. Keywords live in the cache JSON. | Excellent raw material, but only in a vector store. Nothing aggregates it per user per period. | ♻ Nightly `facet_labels(item_key, phrase, cluster_id)` with a stable clustering of phrases into ~200 nanogenres, named once by the LLM. The Wrap-Up counts played time per nanogenre. This replaces v1's coarse genre drift. |
| **Music: listened ms, skips, album context, source/player** | Plex tracks: `view_offset_ms = duration_ms`, `source` NULL, no album, no player (`plex_sync.py:882-897`). Spotify: manual upload only (`spotify_import.py:10-17`), plays under 30 s dropped (`:45, 230`); `skipped`, `reason_start/end`, shuffle, platform and album are dropped. Matching rewrites `plex_item_id` (`music_matcher.py:122`), so a re-upload probably duplicates rows (inferred from the code, not tested). | Skip rate, album-vs-single, "intro skipper", "album listener", the Plexamp-vs-Spotify split and a real listening clock are impossible. | ⏳/♻ Add columns `album`, `listened_ms`, `skipped`, `reason_end`, `shuffle`, `player`, `platform` to `watch_history`. Keep sub-30 s Spotify plays as `skipped=True` rows that are excluded from hours. Dedup Spotify on `(spotify_uri, viewed_at)`, not `plex_item_id`. For Plexamp: Plex history events carry the player, and live `/status/sessions` polling (Tautulli-style) gives real offsets. |
| **Sonic / mood profile** (energy, mood by daypart; "morning intensity, late-night wind-down") | Track genres as a comma-string (Plex or Last.fm top-5 tags, `music_matcher.py:595, 920`). No moods or styles per track, no audio features, no Plex sonic analysis. | Daylist-style moods and Sonic-Adventure clusters can't be computed. | ♻ Read Plex's own `Mood` and `Style` tags per track/album (part of the metadata Plex already serves). If Plex Pass sonic analysis is on, use Plex's sonically-similar neighbours to place tracks into ~8 sonic clusters (via its nearest-neighbour endpoint). Store `track_tags(rating_key, kind ∈ {mood, style}, tag)`. |
| **Local time of day** | Naive UTC (`plex_sync.py:702`), shifted by the server's *current* offset (`wrapup_facts.py:133-136`, `viewing_sessions.py:54-60`). | Half the year is an hour off; household members in other timezones are wrong all year. | ♻ `User.timezone` (IANA), set from the browser's `Intl.DateTimeFormat().resolvedOptions().timeZone` on login. Convert per row with `zoneinfo`, so DST is handled. Nothing to backfill: UTC is correct, only the conversion changes. |
| **Device / platform** ("the TV vs the phone in bed") | Not stored. Plex history events carry the player; the Spotify `platform` field is dropped. | No "phone after midnight" story. | ⏳/♻ `watch_history.player`, `platform` (from the history API's device info and Spotify's `platform`). |
| **Taste over time** (drift river, "what you were in March") | `TasteVectorEntry` is one row per user, overwritten (`models.py:119`). Drift is recomputed from chroma vectors on demand. | No history of the taste profile itself, so no monthly river and no "when did it turn?". | ⏳ Append-only `taste_snapshots(user_id, month, category, centroid_json, top_clusters_json)` written monthly. Cheap, and from then on drift is exact. |
| **Survival of the year's evidence** | `api_cache` prunes (raw 90 d, enriched 30 d, verdicts 30 d, arr library 30 d; `metadata_cache.py:461-479`). Orphan cleanup deletes `watch_history` for items gone from Plex (`plex_sync.py:1742-1846`). Proposals are pruned at 90 d (`retention.py:31-89`). | A title deleted in March loses its credits, verdict and, after orphan cleanup, even its plays before December. | ⏳ Orphan cleanup must **never** delete plays (mark `orphaned_at` instead). The durable tables above replace the TTL cache as the Wrap-Up's source. Snapshot the closed year into `Wrapup` before any prune. |

**Priority order** (value × urgency; today is 2026-10-24, and the season
opens late November):

1. Event-level history plus backfill, and real completion. Every other
   number depends on these. *Done: Randomname653/Curatarr#216.*
2. `added_at` / `acquired_at` plus `library_events`. Unlocks hoarding and
   procrastination, backfillable.
   - *Built as the library ledger* (`services/library_ledger.py`, tables
     `library_items` and `library_events`), fed by the tech sync's
     whole-library walk.
   - Plex `addedAt` is the arrival date. The arr grab history is not
     needed for it: Plex's date is when the file reached the server, which
     is what time-to-watch measures.
   - Music has no ledger until the tech sync walks music.
3. Resolution-log enrichment plus the retro-labelled persuader. Unlocks the
   Court Record, Curatarr's unique beat.
   - *Built in `services/debate_outcome.py`.* Every resolution row
     carries its proposal, the judge's score and verdict, the GB at stake,
     the thread and the user's turns.
   - A rule decides `persuader` where the facts are enough. The summarizer
     reads only the deletes by consensus after a debate, for old rows and
     new alike.
   - The owner checks the labels at `GET /api/stats/debate-labels`.
   - *Since done: `services/judge_record.py`.* `judge_verdicts` keeps every
     verdict past the cache's 30 days. A protection's old state goes into
     `protection_history` before it is lifted or changed; that is a history
     table, not a soft-delete column, so none of the fifteen readers of
     `protected_media` needs a new filter.
   - *Orphan cleanup:* it marks plays `orphaned_at` instead of deleting them
     (`database/orphaned.py`). The Wrap-Up reads them; the live app does not.
4. `User.timezone`. Tiny, and fixes the clock. *Done: `services/user_time.py`. The
   browser reports the zone, and each play is converted with its own date's
   offset.*
5. Credits and companies tables. Unlocks constellations, backfillable from
   the payloads the enricher already fetches.
   *Done: `services/credits_index.py` (`media_works` / `media_credits` /
   `media_companies`), fed daily from the raw cache. TMDB fetches now keep
   person ids, companies, networks and the collection.*
6. Music columns, Plex moods and styles. Partly ⏳, so start now even if the
   2026 music story stays thinner.
   *Done for the Spotify side and the album: `music_skips`; album,
   listened_ms, platform, reason_end and shuffle on `watch_history`; a
   one-time re-read of imported files.*
   *Since done: the Plex player (`services/plex_players.py`) and Plex
   moods/styles (`services/music_tags.py`, read through Plex's filter
   lists, checked against a real server: moods on types 8/9/10 with ~300
   tags each, styles on 8/9, none on tracks).*
7. `taste_snapshots`, nanogenre labels. ⏳; these pay off in 2027.
   *Snapshots done: `services/taste_snapshots.py`. They turned out to be
   back-fillable after all, because item vectors are static, so every past
   month is built from the watch history. Nanogenre labels are still open.*

---

## 4. The v2 masterpiece architecture

### 4.1 From "fact sheet + police" to an editorial pipeline

```
 facts (deterministic)            insights (deterministic)          story (LLM)                 checks
┌───────────────────────┐   ┌──────────────────────────────┐   ┌───────────────────────┐   ┌─────────────────────┐
│ build_wrapup_facts on │──▶│ insight miner: each candidate│──▶│ A. editor pass (JSON  │──▶│ per-sentence claim  │
│ the fixed telemetry   │   │ = claim card + score:        │   │    ok: it's a choice) │   │ binding + 8B        │
│ (Section 3)           │   │ surprise · specificity ·     │   │ B. writer pass (free  │   │ entailment check;   │
└───────────────────────┘   │ contrast · valence · share   │   │    prose, light       │   │ auto-fix → slide-   │
                            │ keep the top ~12, drop rest  │   │    markup, best of 2) │   │ level repair only   │
                            └──────────────────────────────┘   └───────────────────────┘   └─────────────────────┘
```

**Step 1: the insight miner (Python, no LLM).** Every candidate becomes a
*claim card*:

```
I07  kind=contrast  score=0.91
     claim: "Rated The Bear 9; community 6.4 from 2,100 votes"
     values: {you: 9, community: 6.4, votes: 2100}
     say:    {votes: ["2,100", "about two thousand"], gap: ["2.6 points", "two and a half points"]}
     why:    "largest gap of 41 rated titles; top 2% of household gaps"
     refs:   [T3]
```

- **Score** = surprise (|z| against your own past and the household, and
  rarity against library base rates) × specificity (names a title, a day or
  a person) × contrast (two facts in tension) × valence balance.
- Cards below a floor **are not shown anywhere**. That is the "worth saying"
  gate v1 lacks; a weak period gets a shorter story, not padding.
- `say` holds the **pre-approved renderings**, rounding included:
  "58.3 hours" → "58 hours", "nearly 60 hours", "two and a half days". The
  model may round; it just rounds from our list.
- `why` tells the model *why it's interesting* in plain words, which is
  exactly what the flat sheet withheld.

**Step 2A: the editor pass (small JSON, a classification task).**
- The model picks 5–7 card IDs, orders them into an arc (opener → rise →
  twist → court → identity → send-off), and picks the archetype from the
  traits.
- JSON here is fine; Tam et al. found constrained formats *help* selection.

**Step 2B: the writer pass (free prose, no JSON grammar).**
- One `## slide` section per chosen card, in markdown.
- Each sentence ends with its citations in brackets, stripped before render:

  > You walked out of *Megalopolis* after 18 minutes and then defended
  > *Grown Ups 2* to my face. [I04, I11]

- Generate **two drafts** at temperature ~0.9. A third short call (or a
  deterministic rubric: specificity, contrast, cliché density, length)
  picks the better one per slide.
- The custodian pre-generates overnight, so latency is free.

**Step 2C: voice from the user's own record.**
- Give the writer 3–5 **verbatim quotes of the user** from their own
  debates. These are short `ConversationMessage` excerpts tied to the
  chosen cards: thread `deletion_proposal:{id}` for a card that names that
  title.
- Also give their principles in their words, and 2 gold examples of the
  house voice.
- The best line a wrap-up can have is a callback: *"In March you told me
  'nobody needs four Transformers'. You kept three."*
- Own data only, never across household members; an opt-out lives in
  settings.

**Step 3: the checks, rebuilt to be strict on truth and lenient on style.**
- **Claim binding.** The numerals in a sentence must be values or `say`
  renderings *of the cards it cites*, numerals inside cited titles, or
  0–12 in word-like use. "42 hours" citing a card whose hours are 58 fails,
  even if 42 appears elsewhere.
- **Titles.** Every library or artist name detected in a sentence (exact or
  fuzzy match against the period's titles, and a capitalised-span heuristic
  for unknowns) must be cited. An unknown named work fails the sentence.
- **Entailment.** The 8B model answers "does [cards] support [sentence]?
  yes / no / partial" per sentence. That is a cheap classification, and the
  repo already uses an 8B summarizer with grounding checks (`reception.py`).
- **Style is auto-fixed, never fatal.**
  - Mask cited titles and artist names *before* the cliché and `!` checks,
    so *Mamma Mia!* and Journey are safe.
  - A cliché word ("truly") is deleted or swapped from a small map;
    exclamation marks outside titles are replaced by full stops.
- **Repair is per slide.**
  - A failing slide is re-asked with *all* its faults listed, not just the
    first.
  - After two failures **that slide alone** uses its deterministic template
    (each card kind has 3–4 sentence variants); the other slides stay LLM.
  - The whole-story fallback disappears; `story_source` becomes per slide.

### 4.2 Pacing: story first, dashboard second

The default entry is a **full-screen story** (tap or swipe, progress
segments at the top, auto-advance 6–8 s, pause on hold, and
`prefers-reduced-motion` respected). The v1 bento becomes the **"Explore"
appendix** after the last slide, for the curious, the admin and the
deep-dive.

| Beat | Slide | Psychology |
|---|---|---|
| 1 | **Cold open**: one number, huge, alone ("58 hours"), then what it equals in your life | Anchor |
| 2 | **Predict**: "Which one got the most of your time?" | Curiosity gap |
| 3 | **Reveal**: top title with poster, count-up | Payoff #1 |
| 4 | **Rhythm**: the 365-day heatmap fills in; the biggest day lifts out as a vignette | Effort made visible |
| 5 | **The moment**: one specific day, written by the curator | Episodic memory |
| 6 | **The twist**: contrarian or walk-out ("everyone loved it, you left after 18 minutes") | Violation of expectation |
| 7 | **Predict #2**: "How many times did you overrule me?" | Curiosity gap |
| 8 | **The court**: debate scorecard, defiance, the principle you taught me, plus a callback quote | Relationship |
| 9 | **Hoarder or pruner**: GB in vs out; the longest-hoarded title you finally watched | Confession plus pride |
| 10 | **The household** (opt-in): roles, your server-unique claim | Belonging and status |
| 11 | **Who you were**: taste radar, then the archetype title lands *last* | Peak: identity |
| 12 | **Send-off**: the look-ahead challenge (a U# pick) plus a share card | End: forward motion |

- Slides with no card above the floor are skipped, so a quiet month is 6
  slides and a big year is 12.
- Sound is optional and off by default.
- The existing per-tile share and snapshot work turn into **per-slide share
  cards** (9:16 and 1:1).

### 4.3 The eight visualizations to build once the telemetry exists

| # | Visualization | Data (Section 3 row) | Notes |
|---|---|---|---|
| 1 | **365-day activity heatmap** (GitHub-style), one row per medium, streaks outlined, biggest day pinned | Event-level history, timezone | The single highest-value chart; works for any period length. CSP: generate as SVG `<rect>`s with attributes, no inline styles. |
| 2 | **Rhythm clock**: 24 h polar or ring × weekday, split by medium; for music coloured by Plex mood | History, timezone, `track_tags` | Turns "night owl" from a badge into a picture. |
| 3 | **6-axis taste radar**, with last period as a ghost outline and the household median as a dashed line. Axes: *Acclaim* (share of acclaimed hours), *Novelty* (new-title share), *Commitment* (real completion), *Patience* (Resonance-pillar / low words-per-minute share via `MediaSubtitleProfile`), *Era* (catalogue share, film-only), *Contrarian* (mean rating gap) | Real completion, ratings, subtitle signals | Radars are hard to read precisely, so pair it with a bar fallback for screen readers and the Explore view. Fixed axis order across years, so shapes compare. |
| 4 | **Auteur / studio constellation**: force-directed bubbles (director, studio, voice actor) sized by hours, linked when they share titles | `media_credits`, `media_companies` | Hover → titles; tap a studio → its `studio_notes` line. |
| 5 | **Hoarder ↔ Pruner balance**: monthly diverging bars (GB in up, GB out down) with a net line, plus a "procrastination shelf" strip of added→first-watch gaps | `library_events`, `added_at` | Where the curation story becomes visual; exact for 2026 via the arr-history backfill. |
| 6 | **The Court Record / AI debate scorecard**: proposals → kept / deleted / debated; who persuaded whom; a timeline of principles learned; the defiance score as a confidence-weighted gauge | Enriched `CuratorResolutionLog`, `judge_verdicts` | The feature no other recap can copy. |
| 7 | **Taste-drift river**: a stream graph of nanogenre or cluster shares by month, LLM-named clusters labelled in-stream | `facet_labels`, `taste_snapshots` (or chroma drift for 2026) | Replaces the coarse genre tile. |
| 8 | **Consensus scatter**: your rating (or replay-implied score) vs community on every title, the Judge's verdict as marker shape; the contrarian quadrant labelled | Ratings, verdicts | Replaces the one-title "contrarian gem" coin flip with a distribution. |

### 4.4 Rollout before the season (without touching v1's shipping path)

1. **Telemetry PRs (now).** Rows 1–4 of the priority list, each with its
   backfill job and a `facts_version` bump. Ship first; the v1 UI improves
   immediately because its numbers become true.
2. **Insight miner plus the new writer pipeline,** behind the existing
   `/api/wrapup/generate`, keeping `wrapups` as the store. Add
   `insights_json`, and make `story_json` a list of slides.
3. **Story mode** in `wrapup.js`, with the bento demoted to Explore.
4. **Visualizations 1, 3, 5 and 6** for the 2026 season. Constellation,
   river and scatter follow once credits and snapshots have accumulated.

### 4.5 Owner decisions (2026-10-24)

- **Callback quotes from chat: opt-out**, because a quote only ever goes
  back to the person who wrote it, in their own wrap-up.
  - Anything that would let one person's words or numbers reach *another*
    member is **opt-in**: a shared slide, a household card, a share card
    showing a quote.
- **Household comparisons: only with consent.**
  - A member appears in another member's comparison only after they agreed
    (a per-user setting, off by default).
  - Members who have not agreed are left out of medians and ranks, not
    just hidden from them.
- **Story language:** the user's main language. That is the language they
  write most often in their own `ConversationMessage` rows, by message count.
  - Detected deterministically, with no model call.
  - English when there are no messages.
  - Stored per user and recomputed with the wrap-up.
  - The checks need it too: the cliché list, the `say` renderings and the
    slide templates exist per language.
- **Persuader retro-labelling: yes**, once over the existing
  `deletion_proposal:*` threads.
  - It uses the **summarizer** (`SUMMARIZER_MODEL`, base `granite4.1:8b`),
    the same model that classifies keeps in chat today
    (`episodic_memory.detect_and_handle_protection`).
  - The live path gets the same `persuader` field from the same call, so
    labels from the past and from now come from one classifier and can be
    compared.
  - The Judge and the curator are not used for this. They are a party to
    the debate, and they would cost the GPU time the 27–31B models need.
  - Before the backfill, hand-check about 30 threads against the labels;
    below about 85% agreement, fix the prompt before running it on
    everything.
  - Today's delete path takes its stance from keywords
    (`recommendations._curator_stance`). It moves to the same classifier,
    so keep and delete are judged the same way.

---

## Sources

- Tam et al., *Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of LLMs*, EMNLP 2024 Industry — https://arxiv.org/abs/2408.02442
- Spotify Wrapped 2025 (Listening Age, Clubs, fan leaderboard, Wrapped Party, listening archive) — https://www.axios.com/2025/12/03/spotify-wrapped-2025-new-features · https://techcrunch.com/2025/12/03/spotifys-2025-wrapped-becomes-a-multiplayer-experience/
- Letterboxd Wrapped / Year in Review 2025 (films, hours, most-watched actor and director, busiest week and weekday, ≥10 films) — https://rotek.fr/letterboxd-wrapped-2025-comment-acceder-year-in-review/
- Steam Replay 2025 (monthly view, global averages, input devices) — https://www.pcworld.com/article/3015881/steam-replay-2025-is-here-see-your-year-on-steam-stats-and-all.html
- PlayStation Wrap-Up 2025 (≥10 h eligibility, accessories, trophies, reward avatar) — https://www.techloy.com/playstation-2025-wrap-up-is-live-with-stats-on-your-top-games-hours-played-and-more/
- Plex Sonic Analysis (N-dimensional "musical universe", Plex Pass) — https://support.plex.tv/articles/sonic-analysis-music/
- Trakt Year in Review (VIP) — https://apps.apple.com/us/app/-/id1514873602
- Earlier market scan with the full list: `docs/WRAPUP_DESIGN.md` § Sources
