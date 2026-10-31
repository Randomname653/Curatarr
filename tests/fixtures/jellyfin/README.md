# Jellyfin fixtures

Payloads for the Jellyfin adapter's contract tests (docs/JELLYFIN_DESIGN.md,
D8). **They were not recorded from a live server.** They are written by hand
in the shape of the Jellyfin 10.9/10.10 OpenAPI spec (`BaseItemDto`,
`VirtualFolderInfo`, `UserDto`, `SessionInfoDto`, `SystemInfo`). When a live
server answers differently, the live answer wins: replace the fixture with the
recorded one and keep the test.

They describe the same library as `../plex/`: the same titles and the same
files, mounted under `/media/...` here and `/data/media/...` there. That is
what lets the contract test check that both adapters arrive at the same
canonical keys, the thing a `rebind-server` run matches on.

Deliberate cases:

| Case | Where |
|---|---|
| a movie with no external id (matched by file stem + size) | `Items_movies_Movie.json`, "Home Movie" |
| a double episode as ONE item with `IndexNumberEnd` (Plex lists two) | `Items_tvshows_Episode.json`, S01E02-03 |
| an episode's `Tvdb` is the episode id, not the series id | `Items_tvshows_Episode.json`, S01E01 |
| a track with no MBID (matched by album + disc/track number) | `Items_music_Audio.json`, "Jóga" |
| a dashed, upper-case GUID (plugins and some endpoints send them) | `Items_music_Audio.json`, Jóga's `AlbumArtists[0].Id` |
| a library with no `CollectionType` (mixed; skipped) | `Library_VirtualFolders.json`, "Mixed" |
| a session with nothing playing (skipped) | `Sessions.json`, the second entry |
| a user's played items with `UserData` (counts, last-played dates, a like, a favourite) | `Items_played_{user id}.json` |
| the Playback Reporting plugin's answer: `colums` (sic), every value as text, two users | `PlaybackReporting_query.json` |
| sign-in results for an admin and a member; a Quick Connect request | `AuthenticationResult_*.json`, `QuickConnect_Initiate.json` |

File names: `{Endpoint}_{library CollectionType}_{IncludeItemTypes}.json` for
library walks, otherwise the endpoint path with `/` as `_`.
