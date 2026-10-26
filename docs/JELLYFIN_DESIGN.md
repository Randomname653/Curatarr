# Jellyfin support — design

_Status: **Accepted**, 2026-10-26 (draft of the same day, revised with the
owner's decisions). **Phases 1–3 are built**: the seam and both read
adapters, `server_id_map`, Jellyfin sign-in and setup, and the four-tier
play history. A Plex install behaves as before. Endpoint names are from the Jellyfin 10.9/10.10 API and are pinned
by hand-written fixtures (section 11); they have not met a live Jellyfin
server yet, and anything they do not cover is checked against one before it
ships._

---

## 0. Decisions

| # | Question | Decision (2026-10-26) |
|---|---|---|
| D1 | Plex and Jellyfin at once? | **No.** Exactly one server per running install, `MEDIA_SERVER_TYPE=plex` or `jellyfin`. |
| D2 | Switching an existing install? | **Yes, both ways, without loss:** `rebind-server --to jellyfin` / `--to plex` (section 8), backed by a permanent id memory, `server_id_map`. |
| D3 | Column names | `plex_*` key columns stay and hold the **active server's** key. Every key is a string (Appendix A: they already are). |
| D4 | Minimum version | **Jellyfin 10.9+** (10.9 and 10.10). Header `Authorization: MediaBrowser Token="…"` throughout; no 10.8 path. |
| D5 | History plugins | **None required.** Four tiers: A `/Sessions` polling, B Webhook plugin (optional), C delta poll of `UserData`, D Playback Reporting backfill (optional). |
| D6 | Ratings | `UserData.Likes = true` or `IsFavorite = true` is a positive signal. The rebind may line them up with Plex ratings ≥ 8/10. |
| D7 | Watchlist | A per-user playlist **"Curatarr Watchlist"** on Jellyfin. |
| D8 | Tests | Contract tests on fixtures shaped by the Jellyfin 10.9/10.10 OpenAPI spec (`Items`, `Users`, `Sessions`, `Playlists`, `Collections`, `QuickConnect`). No live server in CI. |

## 1. Goal

Curatarr is Plex-only. Jellyfin is the other server a self-hosted \*arr
household runs, and nothing about the curator (enrichment, taste vectors,
search, chat, deletion through the \*arr services, Wrap-Up) depends on
which server plays the files. Only the edges do: who the users are, what
they played, what the library holds, and where Curatarr writes its
playlists and collections.

**Goal:** an install talks to Plex *or* Jellyfin, chosen in the setup
wizard, and can move between them in either direction keeping its history,
enrichment, vectors, verdicts and proposals. A Jellyfin household gets the
same curator, the same deletion flow and the same Wrap-Up, with the gaps
below named in the UI instead of hidden.

**Not a goal:** two servers live at once (D1); Emby (its API has drifted;
it may work, it is not tested or promised).

## 2. How deep Plex goes today

Measured on `0a518bb`: 82 files under `src/` mention Plex; 45 of them use
`plex_rating_key` or `plex_item_id`. Most of those only *carry* the key.
The code that *talks* to Plex:

| Area | Where | What it uses |
|---|---|---|
| Login | `src/routers/auth.py` | plex.tv PIN flow, `/api/v2/user`, server `/accounts` for the membership check |
| Setup | `src/services/setup_wizard.py`, `src/routers/setup.py` | server URL + token, section listing |
| Libraries | `src/routers/libraries.py`, `src/routers/library.py`, `LibraryConfig` | `/library/sections` → movie / show / artist |
| Watch history | `src/services/plex_sync.py` | `/library/sections/{key}/all`, `/status/sessions/history/all` (one event per play, per account), `/accounts` |
| Devices | `src/services/plex_players.py` | history `deviceID` → `/devices` |
| Ratings | `plex_sync._sync_music_ratings`, `PlexRating` | `userRating` / `lastRatedAt` (PlexAmp stars) |
| Tech profiles | `plex_sync.sync_tech_profiles` | Media / Part / Stream |
| External ids | `plex_sync._parse_guids`, `MediaIdentity` | Guid tags (tmdb / tvdb / imdb / mbid) |
| Library ledger | `library_ledger.py`, `LibraryEvent` | `addedAt` |
| Playlists | `src/services/plex_playlists.py` | per-user token writes, "Curatarr Recommended", Wrap-Up playlist |
| Collections | `src/services/plex_collections.py` | section collections from the collection designer |
| Watchlist | `src/services/plex_watchlist.py` | plex.tv Discover, per-user token |
| Deletion (music) | `src/routers/recommendations.py` (`_plex_delete_artist`), `limbo_reconcile.py` | `DELETE /library/metadata/{key}` for Plex-index artists Lidarr does not own |
| Deep links | `src/routers/recommendations.py` | `…/web/index.html#!/server/{machineId}/details?key=…` |
| Subtitles | `subtitle_signals.py` | sidecar streams |
| Music tags | `music_tags.py` | Plex moods and styles |
| Matching | `music_matcher.py`, `music_rematch.py`, `netflix_match.py` | Plex track / film search |
| Orphans | `orphan_repair.py`, `plex_sync.cleanup_orphan_watch_history` | "is this key still in a section" |

Posters and artwork do not come from Plex (they come from the metadata
sources through `image_proxy`), so images need no Jellyfin path.

## 3. Mapping, feature by feature

| Curatarr needs | Plex today | Jellyfin 10.9+ | Gap |
|---|---|---|---|
| Server auth | URL + `X-Plex-Token` | URL + API key; `Authorization: MediaBrowser Client="Curatarr", Device="…", DeviceId="…", Version="…", Token="…"` | none |
| User sign-in | plex.tv PIN | **Quick Connect**: `POST /QuickConnect/Initiate` → code; poll `GET /QuickConnect/Connect?secret=`; `POST /Users/AuthenticateWithQuickConnect`. Password via `POST /Users/AuthenticateByName` as fallback | Quick Connect is off by default; the wizard checks `GET /QuickConnect/Enabled` |
| "Only this server's people" | `/accounts` + owner check | `GET /Users` with the API key | simpler: no outside accounts exist |
| First account is the owner | plex.tv owner id | `Policy.IsAdministrator` | none |
| Libraries | `/library/sections` | `GET /Library/VirtualFolders` (`CollectionType` movies / tvshows / music, `ItemId`) | mixed libraries have no type; skipped like Plex "other" |
| Library items | `/library/sections/{k}/all` | `GET /Items?ParentId=…&Recursive=true&IncludeItemTypes=…&Fields=ProviderIds,Genres,DateCreated,MediaSources,Path&StartIndex=&Limit=` | none |
| Hierarchy | `parentRatingKey`, `grandparentRatingKey`, `index`, `parentIndex` | `SeriesId`, `SeasonId`, `AlbumId`, `AlbumArtists[].Id`, `IndexNumber`, `ParentIndexNumber` | none |
| External ids | Guid tags | `ProviderIds` (`Tmdb`, `Tvdb`, `Imdb`, `MusicBrainzArtist`, `MusicBrainzReleaseGroup`, `MusicBrainzAlbum`, `MusicBrainzTrack`; `AniList`/`AniDB` with plugins) | **better** than Plex |
| File identity | `Part.file`, `Part.size` | `Path`, `MediaSources[].Size` | none |
| Added date | `addedAt` | `DateCreated` | none |
| Tech profile | Media / Part / Stream | `MediaSources[].MediaStreams[]` (codec, height, channels, `IsExternal`) | none |
| Per-user watched state | per account listing | `GET /Items?userId=…&Filters=IsPlayed&Fields=UserData` (`Played`, `PlayCount`, `LastPlayedDate`, `PlaybackPositionTicks`) | none |
| **One event per play** | `/status/sessions/history/all` | not in core: four tiers, section 4 | the gap |
| Now playing | `/status/sessions` | `GET /Sessions` (`NowPlayingItem`, `PlayState.PositionTicks`, `DeviceName`, `Client`, `UserId`) | none |
| Ratings | `userRating` (PlexAmp) | `UserData.Likes`, `UserData.IsFavorite` | positive signal only (D6) |
| Playlists, per user | user's own plex.tv token | `POST /Playlists {Name, Ids, UserId, MediaType}` with the API key | simpler: no per-user token |
| Collections | section collections | `POST /Collections?Name=…&Ids=…`; `POST/DELETE /Collections/{id}/Items?ids=` | server-wide, not per library |
| Watchlist | plex.tv Discover | none | "Curatarr Watchlist" playlist (D7) |
| Mark watched | `/:/scrobble` | `POST /UserPlayedItems/{id}?userId=` | none |
| Delete an item | `DELETE /library/metadata/{k}` | `DELETE /Items/{id}` (needs "allow deletion" for the key's user) | none |
| Moods / styles | Plex music tags | `Genres` + `Tags` | thinner |
| Deep link | `/web/index.html#!/server/{mid}/details?key=…` | `/web/#/details?id={id}` | none |

10.9 serves `/Items?userId=` and still answers the older `/Users/{userId}/Items`;
the adapter uses only the first (not deprecated).

User accounts on Jellyfin carry no e-mail address. User matching (8.4)
therefore works on names, with an explicit flag for the rest.

## 4. Watch history — four tiers, no plugin required (D5)

Curatarr's history is **one row per play** (Wrap-Up v2's "every play its own
row", `viewing_sessions.py`, rewatch detection, the hour-of-day clock).
Jellyfin core keeps only each item's *current* per-user state; a rewatch
overwrites `LastPlayedDate`. The tiers below rebuild per-play rows from
what the server exposes. All four write through one function,
`record_play(Play)`, which dedups (4.5).

### 4.1 Tier A — `/Sessions` polling (always on, no plugin)

A scheduler task polls `GET /Sessions?activeWithinSeconds=…` every 30–60 s
(setting `JELLYFIN_SESSION_POLL_S`, default 45) and keeps, per
`(session id, item id)`, the highest `PlayState.PositionTicks` seen and the
item's `RunTimeTicks`.

- When a tracked pair disappears (session gone, or another item now plays)
  and its last position was **≥ 80 %** of the runtime, one `Play` is
  written: user, item, `played_at` = the poll that saw it gone, device
  `DeviceName`, client `Client`, `source="jellyfin_session"`.
- `completed` follows the rule the Plex path uses (`completion ≥ 0.9`,
  `plex_sync.py:817`), so an 80–89 % play counts as a play but not as
  finished, exactly as on Plex.
- **Rewind / restart:** when a tracked pair's high-water mark is already
  ≥ 80 % and a later poll finds the position back below 15 % (the song or
  episode started again in the same session), the first run is written as
  a play right there and the high-water mark restarts for the new run.
- Paused sessions stay tracked; a pause longer than 4 h ends the pair.
- The tracker is in memory. A restart loses only plays in progress, and
  tier C picks those up.
- The poll is one cheap request; it skips while no user has played in the
  last 24 h *and* the previous poll saw no session, then backs off to 5 min.

Limit: a play shorter than one poll interval, or a seek straight to the
credits, is seen late or not at all. Tier C covers it.

### 4.2 Tier B — Webhook plugin (optional)

The official `jellyfin-plugin-webhook` posts events as they happen. Curatarr
subscribes to `PlaybackStop` and `UserDataSaved` (names to be checked on the
plugin version in the fixture set) and offers a template to paste.

Route `POST /api/jellyfin/webhook`:

- The body is a **hint, never the truth.** It names a user and an item; the
  handler re-reads that item's `UserData` with the API key and writes a
  play only if the server agrees it advanced. A forged or replayed post can
  at most trigger a harmless re-read.
- Authenticated by a shared secret the wizard generates
  (`X-Curatarr-Secret`, constant-time compare), rate-limited, body capped
  at 64 KB.
- `source="jellyfin_hook"`; device and client from the payload, but only
  when the re-read agreed.

With tier B configured, tier A keeps running as a cross-check; 4.5 merges.

### 4.3 Tier C — delta poll of `UserData` (always on)

The scheduled sync reads each user's played items
(`GET /Items?userId=…&Recursive=true&Filters=IsPlayed&Fields=UserData`)
and diffs against a per-user snapshot of `(PlayCount, LastPlayedDate)`:

- `PlayCount` up by *n* → *n* plays. The newest at `LastPlayedDate`; any
  older ones are `date_only=True` at the previous snapshot's time (the play
  happened between the two syncs, the clock time is unknown).
- First sync of a user: one `source="marked"` row per played item at
  `LastPlayedDate`. Wrap-Up already excludes `marked` rows from
  time-of-day facts (`wrapup_facts.py:127`).
- `source="jellyfin_poll"`; no device.

### 4.4 Tier D — Playback Reporting backfill (optional)

`POST /user_usage_stats/submit_custom_query` over the plugin's
`PlaybackActivity` table (`DateCreated`, `UserId`, `ItemId`, `ItemType`,
`DeviceName`, `ClientName`, `PlayDuration`) — per-play history from before
Curatarr was installed, the counterpart of `import_plex_history_for_user`.
Only fixed, parameter-free `SELECT`s are sent; users are filtered in
Python. `source="jellyfin_report"`.

After a Plex → Jellyfin rebind the whole Curatarr history is already there
(section 8); tier D only adds what was played on Jellyfin before.

### 4.5 As built (phase 3)

`media_server/jellyfin_history.py` (tiers A, C, D, `record_plays`,
`sync_history`, `walk_library`), `media_server/jellyfin_hook.py` and
`routers/jellyfin.py` (tier B), table `server_play_state` (tier C's
snapshot). Where the build differs from the draft above:

- **Tier C reads 24 hours back** past the last poll, not 1: a play started
  before a poll and finished after it can carry a last-played date from
  before that poll. The snapshot keeps the re-read from counting twice.
- **The webhook secret lives in app_state**, not in `.env`: the admin's
  Settings → Integrations → Jellyfin → *Webhook setup* creates it and shows
  URL, header, events and template; *New secret* rotates it. No secret, no
  route (401).
- **The scheduled sync** (`job_plex_sync`, job id unchanged) runs tiers D and
  C on Jellyfin, then a library walk that fills `server_id_map`; tech
  profiles stay Plex-only until phase 6. Startup and the Sync button set
  the custodian's follow-up flag, so the taste work runs as after a Plex
  sync.
- **Tier D counts a report row as a play from 80 % of the runtime**, the
  same bar as tier A; shorter starts are skipped.
- Plays of Jellyfin users who never signed in to Curatarr, and of unmapped
  or ignored libraries, are skipped, as the Plex sync skips them.

### 4.6 One play, many witnesses

`record_play` keys a play on `(user_id, server item key)` and merges any
two reports less than 20 minutes apart. The richer one wins field by
field, in this order: hook > session > report > poll > marked. So a play
seen by A and later by C stays one row, with A's device and C's
confirmation. Plays from Spotify, Netflix or Plex history are never
merged with Jellyfin plays (different `source` families).

The UI shows which tiers a household has ("history: live sessions + webhook"
or "history: live sessions — install the Webhook plugin for exact stop
times").

## 5. Writes

### 5.1 Playlists

Jellyfin playlists belong to the `UserId` they were created with, and the
API key may create them for anyone. One server key writes every member's
"Curatarr Recommended" and Wrap-Up playlists. Reconcile works as today:
`GET /Playlists/{id}/Items`, `POST /Playlists/{id}/Items?ids=`,
`DELETE /Playlists/{id}/Items?entryIds=`.

### 5.2 Collections

Created server-wide; the collection designer's output is unchanged. The
library name is prefixed only if the household has two libraries of the
same type.

### 5.3 "Curatarr Watchlist" (D7)

A per-user playlist (a collection would be server-wide, and a watchlist is
personal). The protection-intent hook in chat and the Swipe view's "want"
write there for in-library titles. A title not in the library is refused
honestly, as the Plex path refuses an ambiguous Discover match.

### 5.4 Ratings (D6)

`Likes = true` or `IsFavorite = true` → one `PlexRating` row with
`rating = 10` and `source = "jellyfin_like"`; both false → the row goes.
`Likes = false` (a thumbs-down) is stored as `rating = 2`, the same value a
1★ PlexAmp rating gets, so it reaches the taste engine as a dislike. Plex
star ratings survive a rebind in the table as they are; 8.6 says what is
written back to the server.

## 6. Code shape

### 6.1 The package

```
src/services/media_server/
  __init__.py       get_adapter() -> MediaServerAdapter, picked by MEDIA_SERVER_TYPE
  base.py           protocol, DTOs, key helpers               ← exists (Phase 1)
  plex_adapter.py   over today's plex_* modules (section 6.3)  ← next, after review
  jellyfin_adapter.py
  jellyfin_auth.py  Quick Connect + password
  jellyfin_history.py  tiers A, C, D and record_play
  jellyfin_hook.py  tier B route handler
  id_map.py         server_id_map reads/writes, canonical keys
  rebind.py         section 8
```

The rule for callers: **outside `media_server/`, nothing builds a server
URL.** `tests/test_media_server_seam.py` will grep `src/` for `/library/`,
`/status/sessions`, `X-Plex-Token`, `/Items`, `MediaBrowser` and allow hits
only in the package and in a shrinking list of not-yet-moved files (a
ratchet, like `test_silent_except_ratchet.py` started out).

### 6.2 `base.py` — the cut

All DTOs are frozen dataclasses with plain types. They carry **every
identity a rebind needs** (section 8): the server key, the parent keys,
the season/episode/track numbers, all external ids, the file path and size.

| Type | Fields (abridged) |
|---|---|
| `ExternalIds` | `tmdb_id`, `tvdb_id`, `imdb_id`, `tvdb_episode_id`, `anilist_id`, `anidb_id`, `mal_id`, `mb_artist_id`, `mb_release_group_id`, `mb_release_id`, `mb_recording_id` |
| `FileRef` | `path`, `size_bytes`; `relative_to(roots)`, `stem_key()` |
| `Library` | `key`, `title`, `kind` (`movie` / `show` / `music`), `roots` |
| `MediaItem` | `key`, `kind` (`movie`, `series`, `season`, `episode`, `artist`, `album`, `track`), `title`, `year`, `library_key`, `parent_key`, `grandparent_key`, `season_number`, `episode_number`, `track_number`, `disc_number`, `external_ids`, `files`, `genres`, `added_at`, `duration_ms` |
| `ServerUser` | `key`, `name`, `is_admin`, `email` (Plex only) |
| `UserItemState` | `user_key`, `item_key`, `played`, `play_count`, `last_played_at`, `position_ms`, `liked`, `favorite`, `rating` |
| `LiveSession` | `session_key`, `user_key`, `item_key`, `position_ms`, `duration_ms`, `paused`, `device`, `client` |
| `Play` | `user_key`, `item_key`, `played_at`, `position_ms`, `duration_ms`, `completed`, `device`, `client`, `source`, `date_only` |
| `Capabilities` | `per_play_history`, `now_playing` (`live_sessions()` answers), `star_ratings`, `native_watchlist`, `music_moods`, `per_user_tokens` |

`MediaServerAdapter` (a `typing.Protocol`, async):

```
kind; capabilities()
ping() -> ServerInfo
libraries(); items(library, *, kinds, since); item(key); children(key); key_exists(key)
users(); user_states(user, *, since)
live_sessions(); plays(user, *, since)
upsert_playlist(user, title, keys) -> key; upsert_collection(library, title, keys) -> key
mark_played(user, key); delete_item(key)
is_server_key(value); deep_link(key)
```

Plus the pure helpers the audit asked for (Appendix A):

- `key_namespace(value)` → `"server"`, `"arr"`, `"spotify"`, `"netflix"`,
  `"ext-script"` or `"unknown"`. It replaces the `isdigit()` guesses.
- `is_plex_key`, `is_jellyfin_key`, `normalize_jellyfin_id` (dashed or
  upper-case GUIDs become 32 lower-case hex; anything else is rejected).
- `canonical_key(item)` → the `server_id_map` key (8.2).

### 6.3 `plex_adapter.py` — the cut

Phase 1 moves **no** Plex logic. `plex_sync.py` is split only afterwards,
function by function, each with its own test run.

**As built in 1a:** the adapter implements the *read* half itself (`ping`,
`libraries`, `items`, `item`, `children`, `key_exists`, `users`,
`is_server_key`, `deep_link`), with a paged walk that raises on any failed
page instead of returning a partial list. Nothing calls it yet; today's
modules keep running, and the seam ratchet (`tests/test_media_server_seam.py`)
counts the direct URL builders that remain. `plays` and `user_states` raise
`NotImplementedError` until phase 3, the writes and `delete_item` until
phase 4. The table is the target of the full move:

| Adapter method | Wraps today | Change needed first |
|---|---|---|
| `ping` | `setup_wizard` connection test | none |
| `libraries` | `routers/libraries.py` section listing | move the HTTP call out of the router |
| `items` | `plex_sync._fetch_section_all` + `_parse_guids` + `_parse_media` | return `MediaItem`; `Part.file`/`size` are read but dropped today — keep them |
| `item`, `key_exists` | `plex_playlists.key_exists`; the `/library/metadata/{key}` read inside `_run_plex_sync` | lift the nested `fetch_one_meta` to module level |
| `users` | `plex_sync._fetch_account_to_user_map` (`/accounts`) | none |
| `plays` | `plex_sync._owner_history_entries`, `_member_history_entries` | return `Play`; keep the watermark logic in `plex_sync` |
| `user_states` | `plex_sync._sync_music_ratings` | split the rating read from the DB write |
| `live_sessions` | none (Plex history covers plays) | `[]`; `capabilities.now_playing = False`. `/status/sessions` can switch it on later for a "now playing" badge |
| `upsert_playlist` | `plex_playlists.create_playlist` / `reconcile_playlist` | needs the user's token: the adapter reads `users.plex_token` |
| `upsert_collection` | `plex_collections.create_collection` | none |
| `mark_played` | nothing: Curatarr never marks Plex items played today | new, `/:/scrobble`; only `rebind --sync-state` (8.6) calls it |
| `delete_item` | `routers/recommendations._plex_delete_artist` | move out of the router; keep the re-read check |
| `deep_link` | `routers/recommendations` link builder | none |

What stays in `plex_sync.py`: watermarks, DB writes, attribution, the
orphan sweep, taste summaries. Those are Curatarr logic, not Plex access,
and become server-agnostic once they consume DTOs.

## 7. Data model

### 7.1 Keys stay strings, in the columns they are in (D3)

The audit (Appendix A) found **no integer key column and no `int` key
type**: every server key is `String`/`VARCHAR` in the schema and `str` in
the code. Plex's `"48291"` and Jellyfin's
`"a1b2c3d4e5f60718293a4b5c6d7e8f90"` fit every column (SQLite does not
enforce `VARCHAR(n)`; the two short declarations are widened anyway for
honesty, A.2).

What does need work is the **shape guessing**: several places tell a
server key from an \*arr id or a placeholder with `isdigit()`, and a
Jellyfin GUID fails that test. Those move to `key_namespace` (A.3).

Jellyfin GUIDs are stored in one form only: 32 lower-case hex, no dashes
(the form `/Items` returns). `normalize_jellyfin_id` enforces it at the
adapter boundary, so equality and joins never meet two spellings.

### 7.2 Additions

- `users.server_user_id` is **not** added: `users.plex_user_id` stays the
  login key and holds the active server's user id (rebind rewrites it).
- `watch_history.source` gains `jellyfin_session`, `jellyfin_hook`,
  `jellyfin_poll`, `jellyfin_report`.
- `deletion_proposals.service` gains `jellyfin` beside `plex` (Plex-index
  artists Lidarr does not own).
- `server_id_map` (8.2).

### 7.3 Settings

```
MEDIA_SERVER_TYPE=plex          # plex | jellyfin; unset + PLEX_URL set → plex (today's installs)
JELLYFIN_URL=
JELLYFIN_API_KEY=               # SecretStr, like PLEX_TOKEN
JELLYFIN_WEBHOOK_SECRET=        # generated by the wizard
JELLYFIN_SESSION_POLL_S=45
```

Plex settings stay in `.env` while Jellyfin is active (and the reverse):
a rebind back needs them.

## 8. `rebind-server` — switching both ways (D2)

```
python scripts/rebind_server.py --to jellyfin                # dry run (default)
python scripts/rebind_server.py --to jellyfin --write
python scripts/rebind_server.py --to plex --map-user anna=Anna_K --write
python scripts/rebind_server.py --undo backups/rebind-20261026-2130/manifest.json
```

(The repo's other tools say `--apply`; this one says `--write` as decided.
Both servers must be configured and reachable; Curatarr's scheduler must
be idle — the tool takes the same state lease the sync takes and refuses
otherwise.)

### 8.1 What it does

1. **Read both sides.** The source side from Curatarr's own tables (and the
   source server when it is still up, for paths and ids the DB lacks); the
   target side from the target adapter: libraries, all items, users.
2. **Match** every key Curatarr holds to a target key: first from
   `server_id_map`, then the cascade (8.3), users by 8.4.
3. **Report** (dry run stops here): match rate per entity type and per
   table, every unmatched key with its title, every conflict.
4. **Write** (`--write`): backup, then re-key every table in Appendix A in
   one SQLite transaction, then the stores outside SQLite (8.5), then flip
   `MEDIA_SERVER_TYPE`. A crash before the flip leaves the old server
   active and the manifest says how far it got; the tool resumes or undoes.

### 8.2 `server_id_map` — the id memory

```
server_id_map
  id               INTEGER PRIMARY KEY
  entity_type      TEXT NOT NULL   -- movie series season episode artist album track library user playlist collection
  canonical_key    TEXT NOT NULL   -- server-independent identity, below
  plex_id          TEXT NULL
  jellyfin_id      TEXT NULL
  match_method     TEXT NOT NULL   -- external_id | file | name | server | user_flag | manual
  last_verified_at DATETIME NOT NULL
  UNIQUE (entity_type, canonical_key)
  INDEX  (entity_type, plex_id)
  INDEX  (entity_type, jellyfin_id)
```

The server-id columns are indexed, **not unique** (changed in phase 1c):
Jellyfin files a double episode as one item (`IndexNumber` 2,
`IndexNumberEnd` 3) that Plex lists as two, so one Jellyfin id legitimately
stands for two canonical keys. The other direction stays one-to-one per
walk: two server ids with one canonical key (a film in a 4K and a 1080p
library) are a duplicate, the first keeps the row, and a rebind matches the
other copy by file.

`canonical_key` is built from the strongest identity the item has
(`base.canonical_key`), most specific first:

| Entity | Canonical key, in order of preference |
|---|---|
| movie | `tmdb:{id}` → `imdb:{tt…}` → `file:{stem}` |
| series | `tvdb:{id}` → `tmdb-tv:{id}` → `imdb:{tt…}` → `file:{stem}` |
| season | `{series canonical}/s{nn}` |
| episode | `tvdb-ep:{id}` → `{series canonical}/s{nn}e{nn}` → `file:{stem}#s{nn}e{nn}` |
| artist | `mb-artist:{mbid}` → `name:{normalized}` |
| album | `mb-rg:{mbid}` → `{artist canonical}/{normalized title}` |
| track | `mb-rec:{mbid}` → `{album canonical}/d{n}t{nn}` → `file:{stem}#d{n}t{nn}` |
| user | `user:{curatarr users.id}` |
| library | `library:{kind}:{normalized title}` |

The `file:` fallback of an episode or a track always carries its number:
a double episode (`S01E01E02.mkv`) or a single-file album is several items
on one file, and the bare stem would make them collide under
`UNIQUE (entity_type, canonical_key)`. (A number-less episode or track keeps
the bare stem.)

The map is written **all the time, not only during a rebind**: every library
walk upserts the active server's id next to the canonical key. On Plex that
is the tech-profile walk (`plex_sync.sync_tech_profiles`, twice a day): its
movies, shows and episodes (seasons derived from the episodes), the
libraries with their roots, and every Curatarr user. Music joins when that
walk runs with `include_music` (off by default; a large music library is
hundreds of thousands of tracks), or with the rebind's own walk. So when a
household switches, most of the target side is the only unknown; and when
it switches back, the old side's ids are still in the map, re-verified
(`key_exists`) instead of re-matched. Rows are never deleted by a rebind;
a key the server no longer knows is cleared on that side only.

### 8.3 Matching cascade (both directions)

1. **External ids.** Movies on `tmdb_id`/`imdb_id`; series on
   `tvdb_id`/`tmdb_id`; seasons on `(series, season_number)`; episodes on
   `tvdb_episode_id`, else `(series, season_number, episode_number)`;
   music on MusicBrainz ids, else artist → album → `(disc, track)`.
2. **File stem.** For an item with no usable id: the path relative to its
   library root (Plex and Jellyfin usually see the same files under
   different mount points, so the comparison drops the root), normalized
   (case, separators, Unicode NFC), plus the file size. Relative path and
   size must both agree; a size mismatch (re-encode, replaced file) is a
   conflict, not a match.
3. **Never on title alone.** A title + year match is listed in the report as
   a *suggestion* and written only with `--accept-suggestions` (the Momoiro
   lesson: same title, other work).

A target key two source keys claim (or the reverse) is a **conflict**: none
of them is re-keyed, all are listed.

### 8.4 Users

1. `server_id_map` (`user:{id}`), when this household switched before.
2. Name: Plex username / title against Jellyfin user `Name`, case- and
   space-insensitive. E-mail is compared only when both sides carry one
   (Jellyfin users do not), so it rarely decides.
3. `--map-user plex_name=jellyfin_name`, repeatable, for the rest; it wins
   over 1 and 2.

An unmatched Curatarr user blocks `--write` (their plays would belong to no
one); `--skip-user name` leaves them on the old server key on purpose.

### 8.5 What is re-keyed

Every column in Appendix A.1, keyed by namespace: only `key_namespace ==
"server"` values are touched; \*arr ids, Spotify URIs, Netflix refs and
`ext-script:` keys are left as they are. Outside SQLite:

- **Chroma vectors** whose doc id is a server key
  (`store_profile_vector`: `plex_rating_key or tmdb_id or …`): the vector is
  copied under the new id with **`upsert`**, never `add` (a resumed run
  meets copies it already made), and the old id deleted after the DB
  commit. Chroma has no rename; copying keeps the embedding, so no model
  runs.
- **Metadata cache** keys that embed a server key (`raw_prefetch:{key}` and
  the dead `enriched:{cat}:{key}` form, ARCHITECTURE §cache): renamed, or
  dropped where they are dead.
- **App state** that names server objects (playlist push snapshots, section
  miss counters, `lib_default:*` per section): dropped. Playlists and
  collections are re-created on the target by the next push.

Unmatched keys stay as they are. Their rows behave as orphans do today
(kept, hidden from reads) and come back if a later rebind, or a rebind
back, resolves them.

### 8.6 Ratings and watched state on the target

Optional, `--sync-state`: after the re-key, the tool writes Curatarr's
knowledge to the target server — played items via `mark_played`, and
Plex ratings ≥ 8/10 as Jellyfin `Likes` (D6); in the other direction
Jellyfin likes become nothing on Plex (no star value is invented). Without
the flag the target server's own state is not touched.

### 8.7 Safety net

- **Dry run by default.** `--write` is required to change anything.
- **Backup** before the first write: the SQLite backup API into
  `backups/rebind-{ts}/curatarr.db`, plus a Chroma snapshot of the touched
  ids.
- **Manifest** `backups/rebind-{ts}/manifest.json`: every `(table, row id,
  column, old, new)`, every Chroma and cache rename, the settings flip.
- **`--undo manifest.json`** replays the manifest backwards, row by row, so
  plays recorded *after* the rebind survive the undo (restoring the backup
  file would drop them). For Chroma it upserts the vector back under the
  old id and then **deletes the copy under the new id** explicitly, so no
  twin vectors remain to surface twice in recommendations. The backup file
  stays as the last resort.
- The tool refuses to run while a manifest without a "completed" or
  "undone" mark exists.

## 9. Security

- The Jellyfin API key is an admin credential. `SecretStr`, never logged,
  never sent to the browser. The URL goes through
  `endpoint_policy.validate_service_url`, and "Test connection" with a
  blank key uses the saved key only against the saved address
  (`secret_for_target`), as for Plex and the arrs.
- Passwords (fallback login) go to the configured server once and are never
  stored. Quick Connect is preferred: no password passes through Curatarr.
- The webhook is the only new unauthenticated route (4.2).
- Playback Reporting queries are fixed strings (4.4).
- **Deletion:** `_require_media_id` accepts only `[1-9][0-9]{0,9}`
  (`recommendations.py:3002`) for \*arr ids *and* Plex keys. It splits:
  \*arr ids keep that check; server keys go through the active adapter's
  `is_server_key`, which for Jellyfin accepts exactly 32 lower-case hex.
  Nothing else changes in the delete path (re-read after delete, limbo).
- The rebind tool runs offline against both servers with keys from `.env`;
  it opens no port.
- [SECURITY.md](../SECURITY.md) has the Jellyfin sign-in rules ("Who can log
  in", since phase 2).

**As built in phase 2:** `media_server/jellyfin_auth.py` and five routes in
`routers/auth.py`: `GET /api/auth/server` (which server, Quick Connect on or
off), `POST /api/auth/jellyfin/quickconnect` and
`GET /api/auth/jellyfin/quickconnect/{login_id}` (nonce in
`X-Jellyfin-Login-Nonce`), `POST /api/auth/jellyfin/login`. The Plex PIN
route answers 404 on a Jellyfin install and the Jellyfin routes 404 on a Plex
one. Each sign-in runs under its own device id, and the session Jellyfin
opens for it is closed right away.

## 10. Build order

Each phase ships on its own and leaves Plex users where they were.

| Phase | Scope | Done when |
|---|---|---|
| **1a — Seam** ✔ | `base.py`; `plex_adapter.py` (read path) as in 6.3; `key_namespace` replaces the `isdigit()` guesses (A.3); seam ratchet test | suite green, no behaviour change |
| **1b — Jellyfin read path** ✔ | `jellyfin_adapter.py`: `ping`, libraries, items, `item`, `children`, `key_exists`, users, `live_sessions`, deep links; contract test over both adapters | contract tests green on the fixture set. The wizard step moves to phase 2, with sign-in |
| **1c — id memory** ✔ | `server_id_map` table + upsert from every tech walk | the map fills on a Plex install with no other change |
| **2 — People** ✔ | Quick Connect + password, membership and admin check; wizard server choice, Jellyfin URL/key step and connection test (refuses < 10.9); library mapping from Jellyfin; Integrations card; SECURITY.md | a member signs in; an account from another server cannot |
| **3 — History** ✔ | tiers A–D, `record_plays` + merge, `server_play_state`, scheduler / startup / Sync button dispatch, the session poller, the webhook route and its setup dialog, the history badge | taste vectors build from Jellyfin plays |
| **4 — Writes** | playlists, collections, Curatarr Watchlist, mark played, delete | "Curatarr Recommended" appears in each member's client |
| **5 — Rebind** | `rebind.py` + `scripts/rebind_server.py`: dry run, write, undo, both directions | round trip Plex → Jellyfin → Plex on a fixture DB ends byte-identical in every re-keyed column |
| **6 — Parity** | likes as ratings, Genres/Tags for moods, sidecar subtitles, deep links | only `Capabilities` gaps remain, each named in the UI |

## 11. Tests

As built in phase 1: `tests/test_media_server_seam.py` (key helpers, the
validator, the URL ratchet, PlexAdapter), `tests/test_server_id_map.py` (the
id memory) and `tests/test_media_server_contract.py`, which walks the Plex
and the Jellyfin fixture sets of one library and requires the same 11
canonical keys from both, the double episode included.

Open points the fixtures cannot settle, to check on live servers:

- which MusicBrainz level Plex's `mbid://` guid names on an album (the
  adapter files it as the release group) and on a track (as the recording);
- that `/Items?Ids=` answers with the API key alone on 10.9 and 10.10;
- the Webhook plugin's notification names (phase 3).

- **Fixtures** under `tests/fixtures/jellyfin/`, one JSON file per endpoint
  and case, shaped by the Jellyfin 10.9/10.10 OpenAPI spec: `Items`
  (movie, series/season/episode, artist/album/track, no-ids, dashed GUID),
  `Users`, `Sessions` (playing, paused, ended), `Playlists`, `Collections`,
  `QuickConnect`, `Library/VirtualFolders`. A live miss becomes a fixture.
- **Contract test:** one assertion set run against both adapters over their
  fixtures (Plex fixtures from the existing tests' payloads).
- **Rebind tests** on an in-memory DB with both fixture sides: match rates,
  conflicts, unmatched users blocking `--write`, undo after new plays, the
  round trip of phase 5.
- Test files follow the repo's style: plain scripts with `check()`, picked
  up by `tests/run_all.py`.

---

## Appendix A — key audit (2026-10-26, `0a518bb`)

Question: where are server keys (`rating_key`, parent keys, section ids,
user ids) stored or treated as integers, and what must change for 32-char
Jellyfin GUIDs and the `server_id_map`?

**Result: no integer storage.** Every key column is `String`, no Pydantic
schema declares a server key as `int`, and no raw SQL casts a key. One type
hint did: `arr_client.PlexClient.get_library_items(section_id: int)`, a
method nothing calls; it is `str` since phase 1a. Parent and grandparent keys are not stored at all (they live only in
the sync's in-memory payloads). The work is in A.2 (two short declarations),
A.3 (shape guessing) and A.4 (keys embedded outside key columns).

### A.1 Columns that hold a server key (rebind targets)

| Table.column | Type | Holds |
|---|---|---|
| `users.plex_user_id` | `String(64)` unique | plex.tv account id → Jellyfin user GUID |
| `watch_history.plex_user_id` | `String(64)` | server account id of the play |
| `watch_history.plex_item_id` | `String(64)` | server item key **or** `spotify:track:…` / `netflix:…` placeholder |
| `plex_ratings.plex_item_id` | `String(64)` | server item key |
| `library_config.plex_section_key` | `String(16)` unique | section key → Jellyfin library `ItemId` |
| `music_tags.section_key` / `.item_key` | `String(32)` / `String(32)` | section key / item key |
| `enrichment_status.plex_rating_key` | `String(64)` unique | server key **or** `{radarr,sonarr,lidarr}:{arr id}` **or** `ext-script:…` |
| `enrichment_findings.plex_rating_key` | `String(64)` | same mix |
| `media_identities.plex_rating_key` | `String(50)` unique | server key (or arr form) |
| `media_tech_profiles.plex_rating_key` | `String(64)` unique | server key |
| `media_subtitle_profiles.plex_rating_key` | `String(64)` unique | server key |
| `library_items.plex_rating_key` | `String(64)` unique | server key |
| `library_events.plex_rating_key` | `String(64)` | server key (or arr form) |
| `cached_recommendations.plex_rating_key` | `String(64)` | real server key |
| `swipe_ratings.doc_id` | `String(128)` | vector doc id: arr form **or** server key |
| `deletion_proposals.media_id` where `service='plex'` | `String(64)` | server key of a Plex-index artist |

`judge_verdicts.item_key` holds a TMDB id or a title, never a server key:
untouched.

### A.2 Declarations to widen

| Column | Now | To | Why |
|---|---|---|---|
| `library_config.plex_section_key` | `String(16)` | `String(64)` | a Jellyfin library id is 32 chars |
| `music_tags.item_key` | `String(32)` | `String(64)` | exactly 32 today, no room for any other form |
| `music_tags.section_key` | `String(32)` | `String(64)` | consistency |
| `media_identities.plex_rating_key` | `String(50)` | `String(64)` | consistency with every other key column |

SQLite ignores the length, so this is a model-only change with no data
migration; it keeps the schema honest if the DB is ever moved.

### A.3 Shape guessing that a GUID breaks

| Where | Today | With a Jellyfin key | Fix |
|---|---|---|---|
| `plex_sync.py:2071` (orphan sweep) | skips keys that are not `isdigit()` | every Jellyfin play is skipped: orphans never marked | `key_namespace(rk) == "server"` |
| `netflix_match.py:44` | only `isdigit()` keys enter the film catalogue | Netflix plays never match a Jellyfin film | same |
| `media_enricher.py:1224` | a non-digit cache id is used as an alternative *title* for the Wikipedia lookup | the GUID is searched as a name | same (a server key is never a title) |
| `routers/recommendations.py:3002` `_MEDIA_ID` | `[1-9][0-9]{0,9}` for arr ids and Plex keys | a Jellyfin artist delete is refused (safe, but broken) | split as in section 9 |
| `limbo_reconcile.py:73` | same validator | same | same |
| `plex_sync.py:1268` | `/devices` ids `isdigit()` | Plex-only code | none (stays in the Plex adapter) |
| `media_enricher.py:2363`, `routers/enrichment.py:1264/1435/1475`, `corpus_repair.py:159` | `partition(":")` + `isdigit()` to find arr ids | a GUID has no `:`, so it is correctly *not* an arr id | none; switch to `key_namespace` for readability |
| `music_matcher.py:531` | `spotify:track:` split | unaffected | none |

### A.4 Server keys outside key columns

| Store | Where the key is | Rebind action |
|---|---|---|
| Chroma doc ids | `media_enricher.store_profile_vector`: `plex_rating_key or tmdb_id or anilist_id or title` | copy vector to new id, delete old (8.5) |
| Metadata cache | `raw_prefetch:{key}`, `raw:music:{key}`, dead `enriched:{cat}:{key}` (`routers/library.py:1000`) | rename / drop |
| App state | playlist push snapshots (`plex_playlists._push_snapshot_key`, `playlist_key`), `plex_section_missing:{key}`, `lib_default:{service}:{key}` | drop; rebuilt on the target |
| Background task names | `library_reenrich:plex:{key}` | none (ephemeral) |
