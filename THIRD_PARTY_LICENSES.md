# Third-party licenses

Curatarr is licensed under the GNU AGPL-3.0 (see [LICENSE](LICENSE)).
The components listed below were ported or derived from third-party
projects and remain under their original licenses, which are compatible
with the AGPL-3.0.

## Data sources (fetched at runtime, not redistributed here)

- [Anime-Lists](https://github.com/Anime-Lists/anime-lists) — cross-service
  anime id mapping (`src/services/anime_mapping.py`)
- [manami-project anime-offline-database](https://github.com/manami-project/anime-offline-database)
  — offline anime metadata (`src/services/anime_offline.py`)
- TMDB, OMDb, AniList, MusicBrainz, Last.fm, Spotify, Discogs, Wikipedia —
  live metadata APIs, used per their respective terms with API keys the
  operator supplies.

## SoulSync (MIT)

Portions of Curatarr are ported from or derived from
[SoulSync](https://github.com/Nezreka/SoulSync):

- `src/services/stale_guard.py` — implausible-mass-staleness guard
- `src/services/plex_playlists.py` — playlist reconcile mode (delta add/remove instead of delete+recreate)
- `src/database/models.py` / `src/services/media_enricher.py` / `src/routers/recommendations.py` — owner match-override layer ("Fix match")
- `src/routers/enrichment.py` — corrupt-source-id detector (dedupe_source_ids)

```
MIT License

Copyright (c) 2025 SoulSync

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
