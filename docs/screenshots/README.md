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
One script builds it and takes the frames, the same every time:

```bash
python scripts/demo/build_demo.py --source <the live install's data folder>
```

1. **This checkout's code** (`git archive HEAD`) in a demo folder
   (`%TEMP%/curatarr-demo` by default; it carries a `.curatarr-demo` marker
   and nothing else is ever deleted).
2. **The live install's newest nightly backup** (`backups/curatarr_*.db`,
   made with SQLite's backup API) as the database, with its caches and
   vector store, so the demo has today's library and today's schema. The
   live install is only read; each copied database is checked.
3. **Its own `.env`:** a fresh JWT secret; from your `.env` only the model
   names, Ollama, the metadata keys, and Radarr/Sonarr (read only, for the
   deletion analysis). The media server, SoulSync and the node are blank,
   so the copy can write to nothing outside itself. No value is printed.
4. **Scrubbed** (`scripts/demo/seed.py`): every row that belongs to a
   person goes (watch history, chats, discussions, proposals, memories,
   principles, swipes, ratings, protections, messages, recommendations,
   taste vectors, Wrap-Ups, any other table with a `user_id`, and the app
   state that names anyone). The two accounts become Jane Doe (the admin)
   and Sam, with no Plex token.
5. **An invented 2026.** Jane: slow-burn horror and sharp satire (films,
   Severance, Dark, Midnight Mass, late-night anime) and trip-hop and dream
   pop; three episodes of The Big Bang Theory, then nothing. Sam: The Big
   Bang Theory from the start, and back to it. Episode lists and runtimes
   from TMDB, tracks from the Plex music index. A title the library no
   longer has is skipped and named.
6. **Served without its scheduler** on loopback, with tokens minted from
   the copy's own secret.
7. **Real output on top,** through the app's own API with the production
   models: nanogenre names, both taste profiles, recommendations, the
   household Wrap-Up 2026 with its story, one chat question ("tell me
   something about the big bang theory"), a deletion analysis.
8. **The frames** below, written into this checkout's `docs/screenshots`.

`--stages serve,capture` shoots again without rebuilding; `--stages serve
--keep-running` leaves the copy running to look at by hand.

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
