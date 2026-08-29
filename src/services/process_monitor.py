"""
Curatarr - Process Monitor

Detects when VRAM-heavy applications (games) are running so the enrichment
pipeline can pause LLM calls and pre-fetch API data instead.

Detection tiers:
  1. Launcher signals  — GameOverlayUI.exe (Steam in-game), etc.
  2. DB-classified     — processes the user confirmed via the web UI
  3. EXTRA_GAME_PROCESSES config  — comma-separated .env override

Windows system processes and common desktop apps are filtered out before
presenting unknown processes to the user for classification.
"""

import logging
from typing import Optional

import psutil

from src.config import settings

logger = logging.getLogger(__name__)

# ── Tier-1: launcher signals that only appear while a game is running ─────────
# GameOverlayUI.exe is injected by Steam into every game that uses the overlay.
# It is NOT present when Steam is merely open — only when a game is active.
GAME_LAUNCHER_SIGNALS: frozenset[str] = frozenset({
    "gameoverlayui.exe",          # Steam in-game overlay (best signal)
    "gog galaxy notifications.exe",  # GOG Galaxy in-game
    "eaoverlayhost.exe",          # EA overlay (in-game)
})

# ── Windows system + common desktop processes (never shown to user) ───────────
SYSTEM_PROCESSES: frozenset[str] = frozenset({
    # Windows kernel / session
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsm.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "sihost.exe", "taskhostw.exe", "taskmgr.exe", "conhost.exe",
    "dllhost.exe", "rundll32.exe", "msiexec.exe", "spoolsv.exe",
    "ctfmon.exe", "regsvc.exe", "audiodg.exe", "wuauclt.exe",
    "searchindexer.exe", "searchhost.exe", "searchapp.exe",
    "securityhealthservice.exe", "securityhealthsystray.exe",
    "smartscreen.exe", "runtimebroker.exe", "applicationframehost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "textinputhost.exe", "explorer.exe", "backgroundtransferhost.exe",
    "wslhost.exe", "wsl.exe", "wslg.exe",
    # Windows Defender / security
    "msmpeng.exe", "nissrv.exe", "mssense.exe", "mpdefendercoreservice.exe",
    # Device / hardware
    "igfxem.exe", "igfxtray.exe", "igfxhk.exe",
    "nvcontainer.exe", "nvtelemetrycontainer.exe", "nvdisplay.container.exe",
    "nvspcaps64.exe", "nvwmi64.exe", "nvcplui.exe",
    "amdow.exe", "amdrsserv.exe", "radeoninstaller.exe",
    # Launchers (open but NOT in-game — signals handled above)
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "epicgameslauncher.exe", "eacefsubprocess.exe",
    "galaxyclient.exe", "galaxyclient helper.exe",
    "eadesktop.exe", "eabackgroundservice.exe",
    "battle.net.exe", "battlenet.exe",
    "ubisoft connect.exe", "uplaywebcore.exe",
    "origin.exe", "originclientservice.exe",
    "xboxapp.exe", "gamingservices.exe", "gamingservicesnet.exe",
    "gog.com galaxy.exe",
    # Common desktop apps
    "chrome.exe", "firefox.exe", "msedge.exe", "opera.exe",
    "brave.exe", "vivaldi.exe", "iexplore.exe",
    "discord.exe", "discordptb.exe", "discordcanary.exe",
    "slack.exe", "teams.exe", "msteams.exe", "zoom.exe",
    "skype.exe", "telegram.exe", "signal.exe", "whatsapp.exe",
    "spotify.exe", "vlc.exe", "mpc-hc64.exe", "mpc-be64.exe",
    "mpv.exe", "wmplayer.exe",
    "obs64.exe", "obs32.exe", "streamlabs obs.exe",
    "code.exe", "cursor.exe", "windsurf.exe",
    "idea64.exe", "webstorm64.exe", "pycharm64.exe", "rider64.exe",
    "devenv.exe", "notepad++.exe", "notepad.exe", "wordpad.exe",
    "python.exe", "pythonw.exe", "python3.exe",
    "node.exe", "npm.exe", "pnpm.exe", "bun.exe",
    "git.exe", "git-remote-https.exe",
    "7zg.exe", "7z.exe", "winrar.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "wt.exe", "windowsterminal.exe",
    "lghub.exe", "logioverlay.exe",
    "razer synapse 3.exe", "razercentralservice.exe",
    "synapse3.exe", "steelseriessoftware.exe", "steelseriesgge.exe",
    "corsairhid.exe", "icue.exe",
    "msiafterburner.exe", "rivatunerstatisticsserver.exe",
    "glasswire.exe", "nzxt cam.exe",
    "onedrive.exe", "googledrivefs.exe", "dropbox.exe",
    "1password.exe", "bitwarden.exe", "keepass.exe",
    "everything.exe", "powertoys.exe", "flowlauncher.exe",
    # Curatarr itself
    "uvicorn.exe", "curatarr.exe",
})


_cached_targets: Optional[frozenset[str]] = None
_cached_time: float = 0


def is_game_running() -> bool:
    """Return True when a known game or game-launcher signal is detected."""
    import time
    from src.database.connection import get_db_session
    from src.database.models import GameProcess

    global _cached_targets, _cached_time
    now = time.time()

    # Refresh cache every 60 seconds
    if _cached_targets is None or now - _cached_time > 60:
        targets = set(GAME_LAUNCHER_SIGNALS)
        try:
            with get_db_session() as db:
                games = {
                    row.process_name.lower()
                    for row in db.query(GameProcess)
                    .filter(GameProcess.is_game == True)
                    .all()
                }
                targets.update(games)
        except Exception as e:
            logger.debug("process_monitor DB read failed: %s", e)

        extra = getattr(settings, "EXTRA_GAME_PROCESSES", "")
        if extra:
            targets.update(p.strip().lower() for p in extra.split(",") if p.strip())

        _cached_targets = frozenset(targets)
        _cached_time = now

    targets = _cached_targets

    # ⚡ Bolt: Fast path - avoid iterating running processes if we have no targets
    if not targets:
        return False

    # ⚡ Bolt: Early stop - check targets during iteration rather than
    # building a full set of all running process names first
    for proc in psutil.process_iter():
        try:
            name = proc.name()
            if name and name.lower() in targets:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return False


async def unload_llm_models() -> list[str]:
    """
    Evict Curatarr's models from VRAM (Ollama keep_alive=0).

    Pass 75: checks ``/api/ps`` first and only unloads models that are
    ACTUALLY resident. This matters because the game watcher
    (scheduler.job_game_watcher) calls this every ~30 s — firing
    ``keep_alive=0`` at an already-unloaded model makes Ollama
    load-then-unload it, the exact VRAM thrash we are trying to avoid. With
    the ps check, a tick where VRAM is already clear costs one cheap GET and
    zero unload calls. If the ps probe itself fails, we fall back to an
    unconditional unload — better than leaving VRAM occupied.

    Returns the model names that were actually unloaded.
    """
    import httpx
    ollama_url = settings.effective_ollama
    try:
        from src.services.embed_service import effective_embedding_model
        _emb = effective_embedding_model()
    except Exception:
        _emb = settings.EMBEDDING_MODEL
    ours = [m for m in (
        settings.CURATOR_MODEL,
        settings.SUMMARIZER_MODEL,
        _emb,   # the ACTIVE profile model, not the legacy v1 default
        (settings.PITCHER_MODEL or "").strip(),  # two-bake split (empty = off)
    ) if m]
    unloaded: list[str] = []
    async with httpx.AsyncClient(timeout=10) as client:
        # Which of our models are currently loaded?
        probe_ok = False
        resident: set[str] = set()
        try:
            ps = await client.get(f"{ollama_url}/api/ps")
            if ps.status_code < 400:
                resident = {m.get("name", "") for m in ps.json().get("models", [])}
                probe_ok = True
        except Exception as e:
            logger.debug("Ollama /api/ps probe failed: %s", e)
        for model in ours:
            # When the probe worked, only unload resident models (no
            # load-then-unload thrash). /api/ps reports "name:tag" — match
            # loosely against the configured model string. When the probe
            # failed, fall through and unload unconditionally.
            if probe_ok and not any(
                model == r or model in r or r in model for r in resident
            ):
                continue
            try:
                r = await client.post(
                    f"{ollama_url}/api/generate",
                    json={"model": model, "keep_alive": 0},
                )
                if r.status_code < 400:
                    unloaded.append(model)
                    logger.info("Unloaded model from VRAM: %s", model)
            except Exception as e:
                logger.debug("Model unload failed for %s: %s", model, e)
    return unloaded


def get_unknown_processes() -> list[dict]:
    """
    Return running processes that are neither in SYSTEM_PROCESSES nor
    previously classified by the user. These are candidates to show in
    the "Is this a game?" UI prompt.
    """
    from src.database.connection import get_db_session
    from src.database.models import GameProcess

    try:
        with get_db_session() as db:
            classified = {
                row.process_name.lower()
                for row in db.query(GameProcess).all()
            }
    except Exception:
        classified = set()

    extra = getattr(settings, "EXTRA_GAME_PROCESSES", "")
    extra_set = {p.strip().lower() for p in extra.split(",") if p.strip()} if extra else set()

    known = SYSTEM_PROCESSES | GAME_LAUNCHER_SIGNALS | classified | extra_set

    seen: set[str] = set()
    unknown: list[dict] = []
    try:
        # No "exe" in the attrs: the loop filters on the NAME alone, and
        # resolving each process's executable path is an extra OS call per
        # process that nothing here reads.
        for proc in psutil.process_iter():
            try:
                name = proc.name()
                if not name:
                    continue
                nl = name.lower()
                if nl in known or nl in seen:
                    continue
                # Only surface .exe files (Windows executables, not background helpers)
                if not nl.endswith(".exe"):
                    continue
                seen.add(nl)
                unknown.append({"name": name, "pid": proc.pid})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.debug("get_unknown_processes scan failed: %s", e)

    return unknown
