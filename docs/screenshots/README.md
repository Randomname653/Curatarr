# Screenshots

The images the README shows, and how to make them again.

```
docs/screenshots/
├── desktop/        1440×900 at 1x       scripts/capture_screenshots.py
└── mobile/         390×844, stored at 1.5x   scripts/capture_screenshots.py
```

## The demo household

The README shows a demo household on a real library, not anyone's real
viewing: Jane Doe, the fictional owner of
[CURATOR_EXAMPLES.md](../CURATOR_EXAMPLES.md), and Sam, who lives with her.
The 2026-10-25 set was made like this:

1. **A copy, never the live data.** The database, the vector store and the
   enrichment caches copied into a separate checkout. Its own `.env`: the
   metadata keys and the local models stay; Plex, SoulSync and the PC agent
   are blanked, so the copy can write to nothing outside itself. Radarr and
   Sonarr were added back, read only, for one deletion analysis.
2. **Scrubbed.** Every row that belongs to a person goes: watch history,
   chats, discussions, proposals, memories, principles, swipes, ratings,
   protections, messages, recommendations, taste vectors, and the app state
   that names anyone. The two accounts become Jane Doe (the admin) and Sam,
   with no Plex token.
3. **An invented 2026.** Jane: slow-burn horror and sharp satire (films,
   Severance, Dark, Midnight Mass, late-night anime) and trip-hop and dream
   pop; three episodes of The Big Bang Theory, then nothing. Sam: The Big
   Bang Theory from the start, and back to it. Episode lists and runtimes
   from TMDB, tracks from the Plex music index.
4. **Real output on top.** Taste vectors, nanogenres, both Wrap-Ups with
   their stories, recommendations, a deletion analysis and one chat
   question ("tell me something about the big bang theory"), each run by
   the app itself with the production models.
5. **Served without its scheduler** on loopback, with a session token
   minted from the copy's own fresh `JWT_SECRET`.

## Capturing

You need a configured instance: libraries mapped and history synced. Until
then, the admin's onboarding takes the page and no view can open. Use an
admin session, because Deletions and Curation are admin views.

```bash
pip install playwright && python -m playwright install chromium
# Browser console on the running Curatarr, signed in as the admin:
#   localStorage.curatarr_token
export CURATARR_TOKEN=...            # keeps it out of the shell history
python scripts/capture_screenshots.py --url http://localhost:8000
python scripts/capture_screenshots.py --only phone      # one viewport
python scripts/capture_screenshots.py --only wrapup     # one view, both viewports
```

The script is read-only: it navigates, opens the More drawer, the install
dialog and the Wrap-Up story, and never approves, rejects, sends or starts
anything. Every run gives the same frames:

- fixed viewports;
- dark scheme and reduced motion, so springs and fades are captured at
  rest;
- posters that are on screen are loaded before each shot;
- the Wrap-Up on the calendar year 2026; the bento with its story closed,
  the story shots stepped to the slide they name;
- the account name replaced with `--name` (default "Jane Doe").

It exits non-zero when any shot fails, names the shot and the reason (a
refused token, a view that did not open), and still writes the others.
The phone frames are captured at 3x and stored at 1.5x: the README shows
them 200 px wide.

## The shot list

| File | View | What it shows off | Used in |
|---|---|---|---|
| `desktop/wrapup.png` | `#wrapup`, year | Hero archetype over the bento grid of earned tiles | README → Wrap-Up |
| `mobile/story-clock.png` | story → clock | The hours round the clock | README → Wrap-Up |
| `mobile/story-river.png` | story → river | Nanogenre hours month by month | README → Wrap-Up |
| `mobile/story-consensus.png` | story → consensus | Your hours against the community's score | README → Wrap-Up |
| `mobile/story-pair.png` | story → roast and toast | The curator's two lines | README → Wrap-Up |
| `desktop/chat.png` | `#chat` | The curator's answer from the viewing record | README → Ask about anything |
| `desktop/deletions.png` | `#deletions` | Proposal cards with verdict, case and Why? | README → Deletion proposals |
| `mobile/chat.png` | `#chat` | The floating glass dock with the raised Chat button | README → On a phone |
| `mobile/swipe.png` | `#swipe` | One title at a time | README → On a phone |
| `mobile/deletions.png` | `#deletions` | Proposals as cards | README → On a phone |
| `mobile/bottom-sheet.png` | `#chat` + install dialog | A dialog as a glass bottom sheet | README → On a phone |
| `desktop/recommendations.png` | `#recs` → Movies | Pitches with posters | README → More |
| `desktop/knowledge-base.png` | `#enrich` | Coverage per library | README → More |
| `mobile/history-cards.png` | `#history` | History and taste on a phone | README → More |
| `mobile/wrapup.png` | `#wrapup`, year | The bento in two columns | README → More |
| `mobile/more-drawer.png` | `#recs` + More | The drawer behind More | README → More |

Breakpoints, from `frontend/css/app.css`: the phone shell (glass dock,
bottom sheets, drawer) starts at **≤ 768 px**. Card-stack tables and the
two-column bento start at **≤ 640 px**. The 390 px frame is below both.

## Before you commit a capture

- [ ] Every title on screen is one you are happy to publish. The frames
      show the real library.
- [ ] No real household member's name is visible: chat text, discussion
      threads, Activity rows, the bell. Only the account pill is replaced
      automatically.
- [ ] No LAN address, token or path: avoid Settings → Integrations and the
      arr views' root folders.
- [ ] Posters loaded. A grey `?` tile means the image proxy could not reach
      TMDB at capture time; capture that shot again.
