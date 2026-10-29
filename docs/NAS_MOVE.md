# Moving Curatarr onto a NAS, with the gaming PC as a second host

The end state: Curatarr runs around the clock in a container on the NAS.
The gaming PC's card does the heavy work while the PC is on and nobody is
playing; the PC's tray only reports, it no longer runs Curatarr itself.
Without the PC, chat and enrichment run on the NAS's own Ollama, and
deletion runs wait (their verdicts never come from the smaller model).

Placeholders: `<nas>` and `<pc>` are LAN addresses, `<nas-ollama>` the NAS
Ollama's port (TrueNAS apps live in 30xxx), `<port>` the container's host
port, `<token>` one random secret of 32+ characters. Generate it like this:

```powershell
-join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
```

## A. Before the move (Curatarr still on the PC)

1. **Record the embedding weights.** Run `python build_models.py --check`
   on the PC (start.bat does it on every start). It notes which weights
   the stored vectors were made with, and later compares the NAS's copy
   with them.
2. **The NAS's Ollama.** In the TrueNAS Ollama app, set the server
   environment that `python build_models.py --suggest <GB>` prints for the
   card. For 16 GB that is `OLLAMA_KV_CACHE_TYPE=q8_0`,
   `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_NUM_PARALLEL=1` and
   `OLLAMA_MAX_LOADED_MODELS=1`.
3. **The PC's Ollama on the LAN.** Set `OLLAMA_HOST=0.0.0.0:11434`, then
   quit Ollama and start it from the Start menu. Allow ports 11434 (Ollama)
   and 11435 (the agent) for the NAS only:

   ```powershell
   New-NetFirewallRule -DisplayName "Ollama from NAS" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress <nas> -Action Allow
   New-NetFirewallRule -DisplayName "Curatarr agent from NAS" -Direction Inbound -Protocol TCP -LocalPort 11435 -RemoteAddress <nas> -Action Allow
   ```

4. **The container is deployed** (`deploy/docker-compose.yml`) and answers
   on `http://<nas>:<port>`, still in the setup wizard. Do not run that
   setup against the real Plex: two installs would sync and write twice.

## B. The move

1. **Stop Curatarr on the PC** (tray: Shutdown, or close the start.bat
   console). A clean stop checkpoints the database, so
   `curatarr.db-wal` ends up empty.
2. **Keep a copy** of the PC's `data/` folder and `.env` (a zip) until the
   NAS has run for a week.
3. **Stop the container.** Copy the *contents* of the PC's `data/` into the
   container's volume, and the `.env` into it as `.env` (the container
   reads `/app/data/.env`). Then give the files to the apps user:
   `chown -R 568:568 <dataset>`.
4. **Edit the `.env` in the volume.** Change or add these:

   | key | value |
   |---|---|
   | `OLLAMA_ENDPOINT` | `http://<nas>:<nas-ollama>` |
   | `BASE_CURATOR_MODEL` | the `--suggest` base, e.g. `gemma4:26b-a4b-it-qat` |
   | `LLM_PROFILE`, `CURATOR_NUM_CTX`, `CURATOR_NUM_GPU` | the `--suggest` profile, window and num_gpu |
   | `OLLAMA_PC_ENDPOINT` | `http://<pc>:11434` |
   | `PC_BASE_CURATOR_MODEL` (and `_SUMMARIZER_`, `_PITCHER_`) | what the PC ran before (its old `BASE_*`, or the defaults) |
   | `PC_AGENT_URL` | `http://<pc>:11435` |
   | `PC_AGENT_TOKEN` | `<token>` |
   | `PLEX_REDIRECT_URI` | `http://<nas>:<port>` |

   Everything else stays: Plex, the arrs, API keys, Cloudflare Access.
5. **The PC's `.env`.** Add `PC_AGENT_ONLY=true`, `PC_AGENT_TOKEN=<token>`
   and `PC_AGENT_NOTIFY_URL=http://<nas>:<port>`. Then start the tray
   (`start_tray.bat`) and keep "Start with Windows" on. The tray now runs
   only the agent; start.bat refuses to start a second Curatarr there.
6. **Update the container** (pull the latest image, redeploy) and start it.
   It builds the NAS's missing models by itself, in the background, while
   the server already answers (`AUTO_BUILD_MODELS`, set in the image). Its
   log shows `[models] missing on … - pulling and building now`, and later
   `[models] … ready`. The first build downloads 20 GB and more.

   After that, the log must also say **"embedding weights match the stored
   vectors"**. On `EMBEDDING WEIGHTS DIFFER`, stop: the NAS has other
   weights under the same name, and new vectors would not compare with the
   stored ones. `python build_models.py --check` in the container's shell
   shows the same, for both hosts.
7. **Start the container and watch its log.** Expect
   `[pc] available (agent answered)` and no errors.
   - Open `http://<nas>:<port>` and sign in.
   - Ask the curator something: with the PC on, it answers from the PC.
   - Quit the PC's agent and ask again: now the NAS answers.
8. **Point the reverse proxy** (NPM) for the public name at
   `http://<nas>:<port>`, then check the public address.

## Updating later

Admins see "Update available" under the sidebar once a newer image is
published. Two ways to install it, the data volume stays as it is either way:

- **On demand (the default):** in the TrueNAS shell,
  `docker pull ghcr.io/randomname653/curatarr:latest`, then edit the app and
  save it again. The container is recreated from the new image.
- **Always:** add `pull_policy: always` under `image:` in the app's YAML
  (commented out in `deploy/docker-compose.yml`). Then edit + save alone
  pulls `:latest`. Every redeploy checks the registry, which is why it is
  optional.

TrueNAS's own "update available" badge is unreliable for custom YAML apps,
so neither way depends on it.

## Icon, description and Web UI button

A custom app shows TrueNAS's generic icon and no description.
`deploy/truenas/apply_metadata.py` fills in the app's metadata (icon,
description, links, screenshots, a Web UI button, notes). The steps are in
`deploy/truenas/README.md`.

## Rolling back

Stop the container. On the PC, remove `PC_AGENT_ONLY` (or set it to false)
and start start.bat. Point the proxy back at the PC. The PC's `data/` is
exactly as it was when you stopped it in B.1, so nothing done on the NAS
carries over.
