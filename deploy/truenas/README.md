# Curatarr on TrueNAS: icon, description, Web UI button

A custom app (Apps → Discover Apps → Custom App → Install via YAML) shows
TrueNAS's generic icon, an empty description and no "Web UI" button: the
compose file has no place for an icon, and TrueNAS reads a portal from it
only at the first install. The app's metadata file holds all of it, and
`apply_metadata.py` fills it in:

- the icon: Curatarr's mark, cut out, with the app icon's ruby diamond
  (`frontend/icons/icon-transparent-512.png`), like the other apps' icons
- the description, the home page and the sources (this repository and the
  container image)
- the screenshots (`docs/screenshots/desktop/`), keywords and category
- a **Web UI** button that opens Curatarr on its port
- short notes: first start, Ollama, where updates are described

TrueNAS has no official way to do this yet. It is a community workaround
that the TrueNAS forum reports working from 24.10 on. The script keeps
every field it does not set, so TrueNAS's own entries (version, train, the
`custom-app` name) stay as they were.

## Steps

In the TrueNAS shell (System → Shell, or SSH), as root:

```bash
cd /tmp
curl -fsSLO https://raw.githubusercontent.com/Randomname653/Curatarr/main/deploy/truenas/apply_metadata.py
less apply_metadata.py                      # read what it does first
sudo python3 apply_metadata.py --dry-run    # shows the new metadata, writes nothing
sudo python3 apply_metadata.py              # writes it (a .bak copy of the old file stays)
```

The defaults are the app name `curatarr` and port `30800`, as in
`deploy/docker-compose.yml`. Other values:
`sudo python3 apply_metadata.py <app name> --port <host port>`.

Then reload the Apps page with **Ctrl+Shift+R**. TrueNAS's app service
picks the file up when it next refreshes. If nothing shows yet, save any app
once (open it → Edit → Save, without changing anything) and reload again.

## Things to know

- **After an update or an edit of the app,** TrueNAS can rewrite the file,
  and the generic icon is back. Run the script again; it is safe to repeat.
- **Which file:** the script writes
  `/mnt/.ix-apps/app_configs/<app>/metadata.yaml` and, when it exists, the
  app's entry in `/mnt/.ix-apps/metadata.yaml`. Depending on the version,
  TrueNAS shows one or the other.
- **A fresh install** gets the Web UI button from the `x-portals` block in
  `deploy/docker-compose.yml` already. TrueNAS reads it only when the app
  is created, not on a later edit. The icon and the description still need
  the script.
- **Undo:** copy the `.bak-…` file next to `metadata.yaml` back over it.

The icon and the logo are Curatarr's own and not covered by the AGPL-3.0
(see `assets/brand/README.md`). Linking to them for your own install is
what they are there for.
