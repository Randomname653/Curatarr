# Curatarr in the TrueNAS app catalog: how to submit

`curatarr/` is the app in the layout of the official catalog
([truenas/apps](https://github.com/truenas/apps), `ix-dev/community/<app>/`),
made from the Tautulli community app (one Python container, a web UI, one
data store). Until it is in the catalog, TrueNAS users install the
[custom app](../README.md) instead.

In the catalog TrueNAS shows the real Curatarr version on "Update" instead of
the custom app's own `1.0.0 -> 1.0.0`, and updates come from the catalog.

Everything below is public and done by the owner, or on the owner's go:
the issue, the fork and the pull request carry his account.

## Before

1. **A release.** The catalog pins a fixed image tag: publish one
   (`docs/RELEASING.md`), then set it in `ix_values.yaml` (`images.image.tag`)
   and in `app.yaml` (`app_version`). Both say `1.1.0` now as a placeholder.
2. **Icon and screenshots.** The catalog hosts them on the TrueNAS CDN: attach
   `assets/brand/curatarr-app-icon.png` and three screenshots from
   `docs/screenshots/desktop/` to the pull request; the reviewer uploads them
   and the URLs in `app.yaml` follow their pattern. The logo is the owner's
   (`assets/brand/README.md`): attaching it is his permission for that use.
3. **The proposal.** Open an issue at truenas/apps: what Curatarr is, why
   it is useful on TrueNAS, who maintains it. Wait for the maintainers' go.

## The pull request

In a fork of truenas/apps (the scripts need Docker, Python 3 with `pyyaml
psutil pytest pytest-cov bcrypt pydantic`, and `jq`; or `devbox shell`):

```bash
cp -r <this repo>/deploy/truenas/catalog/curatarr ix-dev/community/curatarr
./.github/scripts/port_validation.py        # is 30800 free? if not, take the next one it prints
devbox run copy-lib                         # vendors the library, fills lib_version_hash
./.github/scripts/generate_metadata.py --app curatarr --train community
./.github/scripts/ci.py --app curatarr --train community --test-file basic-values.yaml --render-only=true
./.github/scripts/ci.py --app curatarr --train community --test-file basic-values.yaml --wait=true
```

The last one starts Curatarr from the rendered compose file and waits for its
health check; open the printed URL: the setup wizard must come up. Then
open the PR against `master` with the icon and screenshots attached.

Check against the current catalog before opening it, the rules move:
`lib_version` is the newest one under `library/` (2.3.18 when this was
written), and `ix_values.yaml`'s `container_utils_image` the tag the other
apps use now.

## Later updates

A new release: Renovate updates `ix_values.yaml` and `app_version` in the
catalog by itself. Changes to the app's form or template: edit here and in
the catalog, bump `version` in `app.yaml` (1.0.0 -> 1.0.1 ...).
