# Media servers: what works where

Curatarr runs on **one** media server at a time: Plex or Jellyfin today,
Emby planned. Settings -> Media server moves an install between Plex and
Jellyfin in both directions without losing its history. This page lists
what every server gets, what each one adds or lacks, and the optional
extras that close a gap.

_Last checked: 2026-11-08, against Plex Media Server and Jellyfin 12.4 (a
test server with no plugins)._

## The same everywhere

The curator itself does not depend on the server. On any of them you get:

- enrichment from TMDB, OMDb, AniList, MusicBrainz, Last.fm, Wikipedia and
  Wikidata; the taste vectors, the search and the chat;
- deletion proposals with the curator's verdicts, carried out through
  Radarr, Sonarr and Lidarr (or, for music from the server's own index,
  on the server);
- the Wrap-Up for every member of the household;
- per-member "Curatarr Recommended" playlists (movies, shows, anime,
  music) and a Wrap-Up playlist, each in the member's own account;
- the curator's themed collections, rotated every week and edited in
  place (a shelf keeps its spot, only its name and titles change);
- sign-in for every member with their own server account, and a
  household that only sees what this server shares with it.

## Plex

Everything above, plus:

| Feature | Notes |
|---|---|
| One event per play | Plex keeps every play with time, device and account, so plays while Curatarr was off arrive complete with the next sync. |
| Star ratings | PlexAmp stars feed the music taste. |
| Watchlist | Plex's own Discover watchlist. |
| Music moods and styles | Plex's tags feed the Wrap-Up's moods by time of day. |
| Collections on the home screen | `COLLECTIONS_ON_HOME`: `all` (the owner's home and every other account's), `owner`, or `off`. |

Limits:

- **No live view.** Curatarr shows no "now playing" for Plex. It looks at
  Plex's sessions every 5 minutes while something played in the last hour
  (every 15 otherwise, `PLEX_SESSION_POLL_S`) and fetches a play's history
  a few minutes after it ends.
- **Collections are for the whole household.** Plex shows a collection on
  the owner's home and on "everyone else's" home; it has no choice per
  account. Personal picks therefore live in each member's playlists.
- Plex Home members: Curatarr writes with the server's own token for each
  account (from plex.tv), never with the account's sign-in token, which
  the server would read as the owner's.

## Jellyfin (10.9 and later)

Everything above, plus:

| Feature | Notes |
|---|---|
| Live sessions | What plays right now is seen within a minute (`JELLYFIN_SESSION_POLL_S`), with its device. |
| Better ids | Jellyfin carries MusicBrainz ids per track, album and artist. |
| Sign-in | Quick Connect (a code), or name and password. |

Limits, and how far Curatarr closes them **without any plugin** (the way
the project runs Jellyfin itself):

| Gap | Without a plugin | With an optional plugin |
|---|---|---|
| Jellyfin keeps no history, only each title's current state (played, how often, when last) | Curatarr builds the history itself: live sessions while it runs, and the play counts when it comes back. A title played three times while Curatarr was off counts three times. | [Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting): every play with its time and device, also from before Curatarr. |
| Times of plays while Curatarr was off | The last play has its time. Earlier ones get theirs from Jellyfin's own activity log (Dashboard -> Activity, 30 days) when start to stop covers 80 % of the runtime; the rest are dated by day. | Playback Reporting has every time. |
| The device of plays while Curatarr was off | Unknown. | Playback Reporting, or the [Webhook](https://github.com/jellyfin/jellyfin-plugin-webhook) plugin (it reports as the play ends). |
| Ratings | Likes and favourites count as a positive signal; there are no stars. | - |
| Watchlist | A per-member playlist "Curatarr Watchlist". | - |
| Music moods | Only Jellyfin's genres and tags; thinner than Plex's moods. | - |
| Collections on the home screen | Jellyfin's home screen has no row for one collection, and its collections span every library. The shelves are in the Collections view, and each one says so in its description. | The [Home Screen Sections](https://github.com/IAmParadox27/jellyfin-plugin-home-sections) plugin (with File Transformation and Plugin Pages, version-bound to Jellyfin) can show rows a plugin registers; Curatarr would need a small plugin of its own for that. Not built. |

**Run Curatarr at least once every two weeks** when it does not run all
the time (a PC that is on for a few hours, a NAS that sleeps). Nothing is
lost after a longer break: every play still counts. But the times of
repeat plays come from Jellyfin's activity log, which keeps 30 days by
default (Dashboard -> Activity -> settings), and two weeks leave room to
spare. A break longer than that is logged when Curatarr comes back.

## Emby (planned, not built)

Jellyfin started as a fork of Emby, so most of the Jellyfin support carries
over: libraries, items, users, playlists, collections, live sessions and the
delta poll. What Emby needs on its own, and what it would bring:

| Topic | Emby |
|---|---|
| Item ids | Plain numbers, which look like Plex keys. The id memory that lets an install switch servers would have to store the server with every key first. |
| Routes | Emby speaks the older routes (`/Users/{id}/Items`, `/Users/{id}/PlayedItems/{id}`); the Jellyfin adapter deliberately uses only the 10.9 ones. |
| Sign-in | No Quick Connect: name and password only. |
| History | The same gap as Jellyfin. Emby's own webhooks need an Emby Premiere licence; Playback Reporting exists for Emby too. |
| Collections on the home screen | Emby 4.10 and later can show a single collection or a single playlist as a home section, per account. No documented API sets those sections yet, so a member would add the row once by hand. |

Emby is built only against a real Emby server to test with.

## Choosing

- **Plex** if you want complete history without any extra, and collections
  on everyone's home screen.
- **Jellyfin** if you want open source and the live view. Without plugins
  you lose only the device of plays made while Curatarr was off, and the
  time of repeat plays if it stays off for longer than about two weeks;
  Playback Reporting brings both back.

See [JELLYFIN_DESIGN.md](JELLYFIN_DESIGN.md) for how the Jellyfin support is
built.
