# Jellyfin support — design

_Status: **Draft**, 2026-10-26. Nothing here is built. It maps what Curatarr
takes from Plex today, what Jellyfin offers in its place, and the order to
build it in. Endpoint names are from the Jellyfin 10.9/10.10 API and must
be checked against a live server before each phase starts._

---

## 1. Why, and what "supported" means

Curatarr is Plex-only. Jellyfin is the other server a self-hosted \*arr
household runs, and nothing about the curator (enrichment, taste vectors,
search, chat, deletion through the \*arr services, Wrap-Up) depends on
which server plays the files. Only the edges do: who the users are, what
they played, what the library holds, and where Curatarr writes its
playlists and collections.

**Goal:** one install talks to *either* Plex *or* Jellyfin, chosen in the
setup wizard. A Jellyfin household gets the same curator, the same
deletion flow and the same Wrap-Up, with the gaps below named in the UI
instead of hidden.

**Not a goal:**

- Plex and Jellyfin on one install at the same time. Every table keys
  titles by one server's item id; two servers would mean two id spaces in
  every join. One server per install keeps the data model as it is.
- Emby. Its API shares Jellyfin's ancestry but has drifted; it may fall
  out of the adapter for free, but it is not tested or promised.
- Moving an existing Plex install to Jellyfin in place. That is a re-key
  of every table (section 8), a project of its own, and only worth doing
  if someone asks.

## 2. How deep Plex goes today

Measured on `0a518bb`: 82 files under `src/` mention Plex; 45 of them use
`plex_rating_key` or `plex_item_id`. Most of those only *carry* the id.
The code that *talks* to Plex is concentrated:

| Area | Where | What it uses |
|---|---|---|
| Login | `src/routers/auth.py` | plex.tv PIN flow, `/api/v2/user`, server `/accounts` for the membership check |
| Setup | `src/services/setup_wizard.py`, `src/routers/setup.py` | server URL + token, section listing |
| Libraries | `src/routers/libraries.py`, `LibraryConfig` | `/library/sections` → movie / show / artist |
| Watch history | `src/services/plex_sync.py` | `/library/sections/{key}/all`, `/status/sessions/history/all` (one event per play, per account), `/accounts` |
| Devices | `src/services/plex_players.py` | history `deviceID` → player name |
| Ratings | `plex_sync._sync_music_ratings`, `PlexRating` | `userRating` / `lastRatedAt` (PlexAmp stars) |
| Tech profiles | `plex_sync.sync_tech_profiles` | Media / Part / Stream |
| External ids | `plex_sync._parse_guids`, `ExternalIds` | Guid tags (tmdb / tvdb / imdb / mbid) |
| Library ledger | `library_ledger.py`, `LibraryEvent` | `addedAt` |
| Playlists | `src/services/plex_playlists.py` | per-user token writes, "Curatarr Recommended", Wrap-Up playlist |
| Collections | `src/services/plex_collections.py` | section collections from the collection designer |
| Watchlist | `src/services/plex_watchlist.py` | plex.tv Discover, per-user token |
| Subtitles | `subtitle_signals.py` | `plex_sidecar` streams |
| Music tags | `music_tags.py` | Plex moods and styles |
| Orphans | `orphan_repair.py`, `limbo_reconcile.py`, `plex_sync.cleanup_orphan_watch_history` | "is this key still in a section" |

Posters and artwork do not come from Plex (they come from the metadata
sources through `image_proxy`), so images need no Jellyfin path.

Everything downstream of these reads rows, not Plex.

## 3. Mapping, feature by feature

| Curatarr needs | Plex today | Jellyfin | Gap |
|---|---|---|---|
| Server identity + auth | URL + `X-Plex-Token` | URL + API key (Dashboard → API Keys); header `Authorization: MediaBrowser Token="…", Client="Curatarr", …` | none |
| User sign-in | plex.tv PIN | **Quick Connect** (`POST /QuickConnect/Initiate` → code, poll `GET /QuickConnect/Connect?secret=`, then `POST /Users/AuthenticateWithQuickConnect`); password via `POST /Users/AuthenticateByName` as fallback | Quick Connect is off by default on the server; the wizard checks `GET /QuickConnect/Enabled` |
| "Only this server's people" | `/accounts` + owner check | `GET /Users` with the API key; the account exists on the server or it does not | simpler than Plex: no outside accounts exist |
| First account is the owner | plex.tv owner id | `Policy.IsAdministrator` | none |
| Libraries | `/library/sections` | `GET /Library/VirtualFolders` (`CollectionType` movies / tvshows / music) | mixed libraries have no type; skip them like Plex "other" |
| Library items | `/library/sections/{k}/all` | `GET /Items?ParentId=…&Recursive=true&IncludeItemTypes=Movie,Series,MusicArtist,MusicAlbum,Audio&Fields=ProviderIds,Genres,DateCreated,MediaSources,Path` | paging via `StartIndex` / `Limit` |
| External ids | Guid tags, partly agent-dependent | `ProviderIds` (`Tmdb`, `Tvdb`, `Imdb`, `MusicBrainzArtist`, `MusicBrainzAlbum`, `MusicBrainzTrack`, `AniList`/`AniDB` with plugins) | **better** than Plex |
| Added date | `addedAt` | `DateCreated` | none |
| Tech profile | Media / Part / Stream | `MediaSources[].MediaStreams[]` (codec, height, channels, `IsExternal` for sidecar subs) | none |
| Per-user watched state | per account listing | `UserData` (`Played`, `PlayCount`, `LastPlayedDate`, `PlaybackPositionTicks`) via `userId=` | none |
| **One event per play** | `/status/sessions/history/all` | **not in core.** See section 4 | **the gap** |
| Device of a play | history `deviceID` | webhook / Playback Reporting carry `DeviceName`, `Client` | only with section 4's sources |
| Star ratings | `userRating` (PlexAmp) | `UserData.Rating` exists but no official client sets it; `IsFavorite`, `Likes` | **ratings shrink to "favourite"**; see 4.4 |
| Playlists, per user | user's own plex.tv token | `POST /Playlists {Name, Ids, UserId, MediaType}` with the API key; owner is `UserId` | **simpler**: no per-user token needed |
| Collections | section collections | `POST /Collections?Name=…&Ids=…`, `POST/DELETE /Collections/{id}/Items` | collections are server-wide, not per library; titles say which library |
| Watchlist | plex.tv Discover | **no watchlist** in Jellyfin | see 5.3 |
| Mark watched | `/:/scrobble` | `POST /UserPlayedItems/{id}?userId=` | none |
| Moods / styles | Plex music tags | `Genres` + `Tags` (from file tags) | thinner; music_tags falls back to Genres/Tags |
| Now playing | `/status/sessions` | `GET /Sessions` | none |

## 4. Watch history — the hard part

Curatarr's watch history is **one row per play** (Wrap-Up v2's "every play
its own row", `viewing_sessions.py`, rewatch detection, the hour-of-day
clock). Plex hands that over in `/status/sessions/history/all`. Jellyfin
core keeps only the *current* per-user state of each item: played or not,
how often, when last. A rewatch overwrites `LastPlayedDate`; the earlier
play is gone.

Three sources, used together, best first:

### 4.1 Live: the Webhook plugin (recommended, required for full parity)

The official `jellyfin-plugin-webhook` posts `PlaybackStop` (with
`PlayedToCompletion`, `PlaybackPositionTicks`, `DeviceName`, `ClientName`,
`UserId`, `ItemId`), `ItemAdded` and `UserDataSaved`.

New route `POST /api/jellyfin/webhook`:

- The body is a **hint, never the truth.** It names a user and an item;
  the handler re-reads that item's `UserData` from the server with the API
  key and writes the play only if the server agrees it advanced. A forged
  or replayed post can at most trigger a harmless re-read.
- Authenticated by a shared secret the wizard generates and shows as the
  header to paste into the plugin's template (`X-Curatarr-Secret`),
  compared in constant time, rate-limited, body size capped.
- Idempotent on `(user, item, played_at)` so the plugin's retries and a
  later poll never double a play.
- `ItemAdded` only nudges the library ledger; the next listing walk stays
  the source of record.

### 4.2 Backfill: the Playback Reporting plugin (optional)

`POST /user_usage_stats/submit_custom_query` runs SQL over its
`PlaybackActivity` table (`DateCreated`, `UserId`, `ItemId`, `ItemType`,
`DeviceName`, `ClientName`, `PlayDuration`). That is per-play history from
before Curatarr was installed — the equivalent of today's Plex history
import (`import_plex_history_for_user`). Its retention is short by default
(see [WRAPUP_DESIGN.md](WRAPUP_DESIGN.md), Jellyfin Wrapped): import once,
early, and say in the wizard how far back it reached.

Only fixed, parameter-free `SELECT`s are sent; the user id is filtered in
Python, never interpolated into the query string.

### 4.3 Fallback: polling `UserData` (always on)

Without either plugin, the scheduled sync diffs each user's `UserData`
against the last snapshot: a higher `PlayCount` or a newer
`LastPlayedDate` becomes one play at `LastPlayedDate`. This is honest but
lossy: two plays between syncs become one, the device is unknown, and the
history before the first sync is a single "watched, date unknown" mark
per item (stored like today's `source="marked"` rows, which Wrap-Up
already excludes from time-of-day facts).

The UI says which source a household has ("history: live" / "history:
polled — install the Webhook plugin for per-play history").

### 4.4 Ratings

Jellyfin has no star rating that a stock client sets. `IsFavorite` becomes
the only explicit signal and maps to the top of the `PlexRating` scale for
music (`rating = 10`, `source = "favorite"`); un-favouriting removes the
row. No negative signal exists; the taste engine already treats missing
ratings as missing, not as dislike.

## 5. Writes

### 5.1 Playlists

Jellyfin playlists belong to the `UserId` they were created with, and the
API key may create them for anyone. So the per-user `users.plex_token`
column is not needed: one server key writes every member's "Curatarr
Recommended" and Wrap-Up playlists. Reconcile works as today (list items,
add / remove the difference) via `GET /Playlists/{id}/Items`,
`POST /Playlists/{id}/Items?ids=`, `DELETE /Playlists/{id}/Items?entryIds=`.

### 5.2 Collections

Created server-wide; the collection designer's output is unchanged. The
library name is prefixed only if the household has two libraries of the
same type.

### 5.3 Watchlist

Jellyfin has none, and its favourites mean "loved", not "want to watch" —
writing intent there would poison 4.4's only rating signal. Instead each
user gets a Curatarr-owned playlist **"Watchlist"** (5.1). The protection-
intent hook in chat writes there for in-library titles. Titles not in the
library are refused honestly, as the Plex path refuses an ambiguous
Discover match; adding them through the \*arr services stays the
recommendations flow's job.

## 6. Code shape

### 6.1 One seam: `src/services/media_server/`

```
media_server/
  __init__.py      get_server() -> MediaServer, picked by settings.MEDIA_SERVER
  base.py          MediaServer Protocol + plain dataclasses
  plex.py          adapter over today's plex_* modules (moved, not rewritten)
  jellyfin.py      new adapter
  jellyfin_auth.py Quick Connect + password login
  jellyfin_hook.py the webhook route's handler (4.1)
```

`base.py` speaks Curatarr's own terms:

```python
class MediaServer(Protocol):
    kind: Literal["plex", "jellyfin"]
    async def libraries(self) -> list[Library]: ...
    async def items(self, library: Library, *, since: datetime | None = None) -> AsyncIterator[Item]: ...
    async def item(self, key: str) -> Item | None: ...
    async def users(self) -> list[ServerUser]: ...
    async def plays(self, user: ServerUser, *, since: datetime | None) -> list[Play]: ...
    async def key_exists(self, key: str) -> bool: ...
    async def upsert_playlist(self, user: ServerUser, title: str, keys: list[str]) -> str: ...
    async def upsert_collection(self, library: Library, title: str, keys: list[str]) -> str: ...
    async def mark_played(self, user: ServerUser, key: str) -> None: ...
    def capabilities(self) -> Capabilities: ...
```

`Item` carries the server key, type, title, year, genres, `external_ids`,
`added_at` and an optional tech profile. `Play` carries user, key,
`played_at`, completion, device. `Capabilities` lists what the server can
do (`per_play_history`, `ratings`, `watchlist`, `moods`) so the UI and the
scheduler branch on facts, not on `kind`.

The rule for callers: **outside `media_server/`, nothing builds a server
URL.** `tests/test_media_server_seam.py` greps `src/` for `/library/`,
`/status/sessions`, `X-Plex-Token`, `/Items`, `MediaBrowser` and fails on
any hit outside the package, like the existing ratchet tests.

### 6.2 Data model: keep the column names

`plex_rating_key`, `plex_item_id`, `plex_section_key` stay as they are and
are documented as "the server's item key". Renaming 45 files' worth of
columns buys nothing a one-server install can see and costs a migration on
every existing database. Jellyfin item ids are 32 hex characters and fit
every existing `String(50)` / `String(64)` column.

Two additions:

- `users.server_user_id` — the Jellyfin user GUID. `plex_user_id` stays
  the unique login key and holds the same GUID on a Jellyfin install;
  `plex_token` stays NULL.
- `watch_history.source` gains `"jellyfin_hook"`, `"jellyfin_report"`,
  `"jellyfin_poll"` (4.1–4.3), so Wrap-Up can tell a precise play from a
  reconstructed one.

### 6.3 Settings

```
MEDIA_SERVER=plex            # plex | jellyfin
JELLYFIN_URL=
JELLYFIN_API_KEY=            # SecretStr, like PLEX_TOKEN
JELLYFIN_WEBHOOK_SECRET=     # generated by the wizard
```

The setup wizard asks for the server first and shows only that server's
steps. An install with `PLEX_URL` set and no `MEDIA_SERVER` keeps behaving
as today.

## 7. Security

- The API key is an admin credential for the media server. It is a
  `SecretStr`, never logged, never sent to the browser. The Jellyfin URL
  goes through `endpoint_policy.validate_service_url`, and "Test
  connection" with a blank key uses the saved key only against the saved
  address (`secret_for_target`), exactly as for Plex and the arrs.
- User passwords (password fallback) are sent to the configured server
  once and never stored; only the returned access token is used, and only
  for the session check, then dropped. Quick Connect is preferred because
  no password ever passes through Curatarr.
- The webhook route is the only new unauthenticated endpoint: shared
  secret, constant-time compare, size cap, rate limit, and a payload that
  is only ever a hint (4.1).
- Playback Reporting queries are fixed strings (4.2).
- [SECURITY.md](../SECURITY.md) gets a Jellyfin section in the same change
  as phase 2.

## 8. Build order

Each phase ships on its own and leaves Plex users exactly where they were.

| Phase | Scope | Done when |
|---|---|---|
| **0 — Seam** | `media_server/` with the Plex adapter over the existing modules; callers switched; seam ratchet test | full test suite green, no behaviour change, seam test passes |
| **1 — Read path** | Jellyfin adapter: libraries, items, external ids, tech profiles, added dates, orphan checks; wizard step for URL + key | a Jellyfin library enriches, embeds and searches end to end |
| **2 — People** | Quick Connect + password login, membership and admin check, `server_user_id` | a household member signs in; a stranger with a valid account on another server cannot |
| **3 — History** | 4.3 polling, then 4.1 webhook, then 4.2 import; `source` values; UI badge | taste vectors build from Jellyfin plays; webhook plays carry device and exact time |
| **4 — Writes** | playlists, collections, watchlist playlist, mark played | "Curatarr Recommended" appears in each member's Jellyfin client |
| **5 — Parity** | favourites as ratings, Genres/Tags for music moods, sidecar subtitles, now-playing | `Capabilities` gaps are the only differences left, each named in the UI |
| *later* | Plex → Jellyfin re-key via external ids | only on request |

Tests per phase follow the repo's habit: recorded Jellyfin responses as
fixtures under `tests/fixtures/jellyfin/`, one contract test that runs the
same assertions against both adapters, and every live miss turned into a
fixture.

## 9. Open questions for the owner

1. **Minimum version.** 10.9 moved several user endpoints
   (`/Users/{id}/Items` → `/Items?userId=`). Support 10.9+ only, or keep a
   10.8 path?
2. **Webhook plugin required or optional?** Optional (4.3 fallback) is
   friendlier; required gives every household Wrap-Up's per-play facts.
3. **Who tests it?** The owner runs Plex. Phase 1 needs a Jellyfin
   instance with a real library; a container on the NAS pointed at the
   same media folders would do, read-only.
4. **Phase 0 timing.** The seam is a large, behaviour-neutral refactor of
   `plex_sync.py` (2,800 lines). Worth doing even if Jellyfin never ships?
   It does make the Plex code easier to test.
