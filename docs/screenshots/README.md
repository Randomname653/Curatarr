# Screenshots

The images the README shows, and how to make them again.

```
docs/screenshots/
├── desktop/        1440×900 at 1x       scripts/capture_screenshots.py
├── mobile/         390×844 at 3x        scripts/capture_screenshots.py
├── chat.png        legacy: pre-glass UI, hand-captured (prep_screenshots.py)
├── knowledge-base.png   legacy
└── bell.png        legacy
```

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

The script is read-only: it navigates, opens the More drawer and the
install dialog, and never approves, rejects, sends or starts anything.
Every run gives the same frames:

- fixed viewports;
- dark scheme and reduced motion, so springs and fades are captured at
  rest;
- posters that are on screen are loaded before each shot;
- the account name replaced with `--name` (default "Jane Doe", the
  fictional owner of [CURATOR_EXAMPLES.md](../CURATOR_EXAMPLES.md)).

It exits non-zero when any shot fails, names the shot and the reason (a
refused token, a view that did not open), and still writes the others.

Hand-made captures of a desktop window still go through
`python scripts/prep_screenshots.py shot.png …`. It blurs the account pill,
trims dead space and downscales.

## The shot list

| File | View | What it shows off | Used in |
|---|---|---|---|
| `desktop/wrapup.png` | `#wrapup` | Hero archetype, the bento grid of earned tiles, glass surfaces | README → Preview (hero image) |
| `desktop/deletions.png` | `#deletions` | Proposal cards with the poster-tinted glow, verdicts and the Why? line | README → Preview |
| `desktop/chat.png` | `#chat` | The curator's answer, the composer floating as a glass bar | README → Preview |
| `desktop/recommendations.png` | `#recs` | Pitches with posters | spare |
| `desktop/knowledge-base.png` | `#enrich` | Coverage per library | spare (replaces the legacy one) |
| `mobile/chat.png` | `#chat` | The floating glass dock with the raised Chat button | README → On a phone |
| `mobile/wrapup.png` | `#wrapup` | The bento in two columns | README → On a phone |
| `mobile/deletions.png` | `#deletions` | Proposals as a card stack | README → On a phone |
| `mobile/history-cards.png` | `#history` | Tables turned into cards (≤ 640 px) | spare |
| `mobile/more-drawer.png` | `#recs` + More | The drawer behind More | spare |
| `mobile/bottom-sheet.png` | `#chat` + install dialog | A dialog as a glass bottom sheet with a grab handle | README → On a phone |

Breakpoints, from `frontend/css/app.css`: the phone shell (glass dock,
bottom sheets, drawer) starts at **≤ 768 px**. Card-stack tables and the
two-column bento start at **≤ 640 px**. The 390 px frame is below both.

## Before you commit a capture

- [ ] Every title on screen is one you are happy to publish. The frames
      show the real library.
- [ ] No household member's name is visible: chat text, discussion threads,
      Activity rows, the bell. Only the account pill is replaced
      automatically.
- [ ] No LAN address, token or path: avoid Settings → Integrations and the
      arr views' root folders.
- [ ] Posters loaded. A grey `?` tile means the image proxy could not reach
      TMDB at capture time; capture that shot again.
- [ ] Then uncomment the screenshot block in the README's Preview section,
      and delete the three legacy images and their captions.
