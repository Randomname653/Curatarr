# Releasing a version

Two image tracks:

- **`:main`** (and `:sha-<commit>`): every push to `main`, every merge. For
  the maintainer's own NAS and anyone who wants changes first.
- **`:latest`** and **`:X.Y.Z`**: a **release**. `:X.Y.Z` never moves; it is
  what a TrueNAS catalog app pins (`deploy/truenas/catalog/`). `:latest`
  moves with each release, so whoever pulls `:latest` gets only what was
  released on purpose, never a merge in between.

Each image knows its track (`CURATARR_BUILD_TRACK`): "Update available"
under the sidebar offers a `:latest` install the next release and a `:main`
install the next merge, each with the tag to pull.

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
   tag is `v` + `VERSION`, and publishes `:1.1.0` and `:latest`.
4. Once the app is in the TrueNAS catalog: its Renovate bot opens the
   catalog's update PR by itself. If it does not within a few days, change
   `app_version` and the image `tag` in the catalog app (and bump its
   `version`) by hand, as in `deploy/truenas/catalog/SUBMITTING.md`.
