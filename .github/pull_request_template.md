<!-- Thanks! Two things matter here: -->

## What & why

<!-- One paragraph. If it fixes a silent failure, say what made it silent. -->

## Checks

- [ ] `python tests/run_all.py` is green (every suite runs on its own temp stores, so the app may keep running)
- [ ] Behaviour changes come with a test that would have caught the old behaviour
- [ ] No personal data, tokens or hardcoded local paths in the diff
- [ ] Based on current `main` (history was rewritten 2026-08 — rebase, don't merge old bases)
