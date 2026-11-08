# Releasing a version

Every push to `main` publishes `ghcr.io/randomname653/curatarr:latest` (and
`:sha-<commit>`). A **release** adds a fixed tag, `:X.Y.Z`, that never moves:
what a TrueNAS catalog app pins (`deploy/truenas/catalog/`), and what anyone
who wants to choose when to update can write in their compose file.

## The version

`VERSION` in `src/config.py` is the version the app shows in its sidebar.
A release needs a plain `X.Y.Z` there (no `-beta` suffix): the release
workflow refuses anything else, because the TrueNAS catalog's update bot
(Renovate) follows plain semantic versions on its own and would need a
custom rule for anything else. The "beta" badge next to the logo says that
Curatarr is in beta; only the owner sees it (household members get the
logo alone).

- **Patch** (`1.1.1`): fixes only.
- **Minor** (`1.2.0`): new features, settings keep working.
- **Major** (`2.0.0`): an update that needs the user to do something.

## Steps

1. Set `VERSION` in `src/config.py`, move the CHANGELOG's "Unreleased"
   heading to the version with today's date, merge that to `main`.
2. Tag the merge commit and push the tag:

   ```bash
   git tag v1.1.0 <merge-commit>
   git push origin v1.1.0
   ```

3. The `docker` workflow builds and tests the image again, checks that the
   tag is `v` + `VERSION`, and publishes `:1.1.0`.
4. Once the app is in the TrueNAS catalog: its Renovate bot opens the
   catalog's update PR by itself. If it does not within a few days, change
   `app_version` and the image `tag` in the catalog app (and bump its
   `version`) by hand, as in `deploy/truenas/catalog/SUBMITTING.md`.
