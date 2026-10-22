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
import re
from typing import Optional

import psutil

from src.config import settings
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

# ── Tier-1: launcher signals that only appear while a game is running ─────────
# GameOverlayUI.exe is injected by Steam into every game that uses the overlay.
# It is NOT present when Steam is merely open — only when a game is active.
GAME_LAUNCHER_SIGNALS: frozenset[str] = frozenset({
    "gameoverlayui.exe",          # Steam in-game overlay (best signal)
    "gameoverlayui64.exe",        # the same since Steam went 64-bit (2026-10-07:
                                  # it sat in the "not a game" list, no signal)
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
_cached_game_pid: Optional[int] = None

_cached_classified: Optional[tuple] = None   # (classified names, ignored families)
_cached_classified_time: float = 0


def invalidate_process_cache() -> None:
    """Invalidate process monitor caches (e.g. after user classifies a game)."""
    global _cached_targets, _cached_time, _cached_classified, _cached_classified_time
    _cached_targets = None
    _cached_time = 0
    _cached_classified = None
    _cached_classified_time = 0


# ── Foreign GPU load (2026-09-15) ───────────────────────────────────────────
# The owner's image-generation job held 23.9 of 24.5 GB on the 4090 at 100 %
# while game_active stayed 0: this module knew game process NAMES, not GPU
# pressure, so the enrichment ran into summariser timeouts for an hour and
# the custodian kept starting LLM work. A saturated GPU that Ollama itself is
# not using counts as a game now; every LLM-heavy path already yields to
# is_game_running() / the game_active flag. Two looks 30 s apart must agree
# before it counts (a model load spikes the GPU for seconds), and a host
# without nvidia-smi is asked again every ten minutes only.
_GPU_UTIL_BUSY = 85          # percent
_GPU_FREE_MB_MIN = 2500      # below this there is no room even for the summariser
_GPU_CACHE_S = 30
_GPU_HOLD_S = 45             # pressure must persist this long before it counts
_gpu_cache = {"at": 0.0, "busy": False, "since": 0.0, "reason": ""}
_gpu_unavailable_until = 0.0
_smi_state = "ok"            # last nvidia-smi outcome, so each change is logged once

# What _nvidia_smi_read returns besides a stats tuple. A timeout used to read
# as "no nvidia-smi": exactly when another process saturates the card the
# query is slowest, and the gate then switched itself off for ten minutes.
SMI_MISSING = None           # no nvidia-smi on this host
SMI_TIMEOUT = "timeout"
SMI_ERROR = "error"          # non-zero exit or nothing parseable


def _num(x: str) -> "float | None":
    try:
        return float(x.strip())
    except ValueError:       # "[N/A]", "[Not Supported]"
        return None


def _nvidia_smi_read():
    """(used MB, total MB, utilization %) — any of them None when nvidia-smi
    prints [N/A] for it — or SMI_MISSING / SMI_TIMEOUT / SMI_ERROR."""
    import subprocess
    from src.winproc import NO_WINDOW
    try:
        # NO_WINDOW: under the tray (pythonw) every call opened a console
        # window — every ~30 s, during the very games this gate protects.
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return SMI_TIMEOUT
    except (FileNotFoundError, OSError):
        return SMI_MISSING
    if out.returncode != 0:
        logger.debug("nvidia-smi exit %s: %s", out.returncode, (out.stderr or out.stdout or "")[:200])
        return SMI_ERROR
    lines = (out.stdout or "").strip().splitlines()
    vals = [_num(x) for x in lines[0].split(",")[:3]] if lines else []
    if len(vals) != 3 or all(v is None for v in vals):
        return SMI_ERROR
    return tuple(vals)


def _nvidia_smi() -> "tuple | None":
    """(memory.used MB, memory.total MB, utilization %) or None — the plain
    reading for callers that need the memory figures (model_check)."""
    s = _nvidia_smi_read()
    if not isinstance(s, tuple) or s[0] is None or s[1] is None:
        return None
    return s


def _note_smi(state: str, detail: str = "") -> None:
    """Log an nvidia-smi outcome once per change, not every 30 s tick."""
    global _smi_state
    if state == _smi_state:
        return
    prev, _smi_state = _smi_state, state
    if state == "timeout":
        logger.warning("[gpu] nvidia-smi timed out — counted as GPU pressure until it answers")
    elif state == "error":
        logger.warning("[gpu] nvidia-smi failed or printed nothing readable — "
                       "GPU-pressure gate off, asked again in ten minutes")
    elif state == "util_unknown":
        logger.warning("[gpu] nvidia-smi reports no GPU utilization (%s) — the gate "
                       "judges by free memory alone", detail)
    elif state == "missing":
        logger.debug("[gpu] no nvidia-smi on this host — GPU-pressure gate off")
    elif prev in ("timeout", "error", "util_unknown"):
        logger.info("[gpu] nvidia-smi readable again")


def _ollama_loaded_models(url: "str | None" = None) -> "list | None":
    """The model names Ollama holds in VRAM (/api/ps) - this install's
    Ollama, or the one at ``url``; None when it does not answer."""
    # httpx, not urllib.request: the latter also opens file:// and ftp://
    # (Bandit B310), and the unload path below already uses httpx anyway.
    import httpx
    try:
        r = httpx.get(f"{url or settings.effective_ollama}/api/ps", timeout=2)
        r.raise_for_status()
        return [m.get("name") for m in (r.json().get("models") or [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return None   # Ollama down or an odd reply: "unknown", the callers handle None
    except Exception:
        _warn_rate_limited("ollama_ps", "[gpu] /api/ps probe crashed - read as unknown")
        return None


_warned_at: dict = {}


def _warn_rate_limited(key: str, msg: str, every_s: float = 600.0) -> None:
    """WARNING with traceback at most every ``every_s`` per key: the game
    watcher probes every 30 s, so a persistent bug must not flood the log."""
    import time
    now = time.monotonic()
    if now - _warned_at.get(key, -every_s - 1) > every_s:
        _warned_at[key] = now
        logger.warning(msg, exc_info=True)


def _on_loop() -> bool:
    """True on a thread that is running an asyncio loop. The probes below
    take nvidia-smi (up to its 4 s timeout), an HTTP call to Ollama and a
    walk over every process: on the loop thread they froze every request,
    and every LLM call asks (ollama_options -> llm_lane). There they answer
    from the last verdict instead; the game watcher probes on a worker
    every 30 s (scheduler.job_game_watcher)."""
    import asyncio
    return asyncio._get_running_loop() is not None


def gpu_pressure(*, smi=None, ps=None, now: "float | None" = None,
                 cache: "dict | None" = None) -> bool:
    """True when the GPU has been saturated for _GPU_HOLD_S by something that
    is not Ollama. ``smi`` / ``ps`` are injectable for tests - and carry the
    PC agent's reading. ``cache``: the hold state of another card (the PC's,
    pc_busy) so its verdict never mixes with this machine's. On the event
    loop: the last verdict, never a probe (see _on_loop)."""
    import time
    global _gpu_unavailable_until
    local = cache is None
    c = _gpu_cache if local else cache
    if not getattr(settings, "GPU_PRESSURE_GATE", True):
        return False
    now = time.time() if now is None else now
    injected = smi is not None or ps is not None
    if not injected and _on_loop():
        return c["busy"]
    if not injected and now - c["at"] < _GPU_CACHE_S:
        return c["busy"]
    if not injected and now < _gpu_unavailable_until:
        return False
    stats = (smi or _nvidia_smi_read)()
    if stats is SMI_MISSING or stats == SMI_ERROR:
        if local:
            _note_smi("missing" if stats is SMI_MISSING else "error")
            _gpu_unavailable_until = now + 600
        c.update(at=now, busy=False, since=0.0, reason="")
        return False
    if stats == SMI_TIMEOUT:
        # Fails closed for this tick, through the same hold as a reading:
        # a saturated card is when the query is slowest.
        if local:
            _note_smi("timeout")
        used = total = util = None
        pressed = True
    else:
        used, total, util = stats
        # [N/A] utilization (some cards never report it) used to disable the
        # gate for good; free memory still decides then.
        if local:
            _note_smi("ok" if util is not None else "util_unknown",
                      f"{used}/{total} MB" if util is None else "")
        pressed = ((util is not None and util >= _GPU_UTIL_BUSY)
                   or (used is not None and total is not None and (total - used) < _GPU_FREE_MB_MIN))
    if pressed and (ps or _ollama_loaded_models)():
        pressed = False                       # Ollama's own work: that is us
    since = c["since"] or now if pressed else 0.0
    busy = pressed and now - since >= _GPU_HOLD_S
    if not busy:
        reason = ""
    elif stats == SMI_TIMEOUT:
        reason = "nvidia-smi not answering"
    else:
        mem = f"{used:.0f}/{total:.0f} MB" if used is not None and total is not None else "memory n/a"
        reason = f"{mem}, {util:.0f} %" if util is not None else mem
    if busy != c["busy"]:
        logger.info("[%s] %s", "gpu" if local else "pc gpu",
                    f"held by another process ({reason}) — LLM work yields" if busy else "free again")
    c.update(at=now, busy=busy, since=since, reason=reason)
    return busy


def gpu_pressure_reason() -> str:
    """What the last pressure read saw, e.g. "17467/24564 MB, 90 %". Empty
    while nothing is holding the card. Call gpu_pressure() first — this only
    reports the cached verdict."""
    return str(_gpu_cache.get("reason") or "")


_last_game = {"running": False}


# ── the PC agent (NAS move, 2026-10-22) ──────────────────────────────────────
# With a PC agent configured (services/pc_host), the machine that games and
# lends its card is the PC, not the one Curatarr runs on: the process list
# and the GPU reading come from the agent's last report instead.
_LOCAL = object()


def _pc_report():
    """The PC agent's last report when one is configured (None while the PC
    is unavailable), else _LOCAL - read this machine.

    On a SPLIT install (OLLAMA_PC_ENDPOINT set) the PC is a host of its own:
    this machine's game flag reads this machine, and the PC is judged by
    pc_busy() for the routing (services/ollama_hosts)."""
    from src.services import pc_host
    from src.services.ollama_hosts import split
    if not pc_host.configured() or split():
        return _LOCAL
    return pc_host.snapshot()


_pc_gpu_cache = {"at": 0.0, "busy": False, "since": 0.0, "reason": ""}


def pc_busy(report: "dict | None") -> "tuple[bool, str]":
    """(busy, why) for the gaming PC on a split install, from its agent's
    report: a classified game runs there, or something other than Ollama
    holds its card for _GPU_HOLD_S. No report fails closed."""
    if not report:
        return True, "no report from the PC"
    from src.services.ollama_hosts import pc_host
    targets = _game_targets()
    game = next((p.get("name") for p in report.get("processes") or []
                 if (p.get("name") or "").lower() in targets), None)
    pc = pc_host()
    pressed = gpu_pressure(smi=lambda: _smi_from_report(report.get("gpu")),
                           ps=lambda: _ollama_loaded_models(pc.url if pc else None),
                           cache=_pc_gpu_cache)
    if game:
        return True, f"{game} running"
    if pressed:
        return True, f"card held: {_pc_gpu_cache.get('reason') or 'busy'}"
    return False, ""


def _smi_from_report(gpu):
    """The agent's GPU field as _nvidia_smi_read would have returned it."""
    if isinstance(gpu, (list, tuple)) and len(gpu) == 3:
        return tuple(gpu)
    return {"missing": SMI_MISSING, "timeout": SMI_TIMEOUT}.get(gpu, SMI_ERROR)


def pc_unreachable() -> bool:
    """This install's only Ollama is the PC's (agent configured, not split)
    and the PC is not answering (off, asleep, shutting down): the game
    watcher then has nothing to evict. On a split install this machine's
    own Ollama is always there to evict from."""
    from src.services import pc_host
    from src.services.ollama_hosts import split
    return pc_host.configured() and not split() and not pc_host.available()


def game_process_running() -> bool:
    """A known game or launcher process is running. Split out of
    is_game_running so the LLM lane can tell the two causes apart: a game
    owns the whole box (the CPU lane stays off), a GPU-hungry batch job
    owns only the card (summariser work moves to the processor). On the
    event loop: the last verdict, never a process walk (see _on_loop).
    With a PC agent: the PC's processes (an unavailable PC runs no game)."""
    if _on_loop():
        return _last_game["running"]
    pc = _pc_report()
    if pc is _LOCAL:
        running = _probe_game_process()
    else:
        targets = _game_targets()
        running = bool(pc) and any((p.get("name") or "").lower() in targets
                                   for p in pc.get("processes") or [])
    _last_game["running"] = running
    return running


def _game_targets() -> frozenset:
    """Launcher signals, the owner's classified games and EXTRA_GAME_PROCESSES,
    lower-case, cached 60 s."""
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
                    process_name.lower()
                    for (process_name,) in db.query(GameProcess.process_name)
                    .filter(GameProcess.is_game == True)
                    .all()
                }
                targets.update(games)
        except Exception as e:
            # Was DEBUG, and only the launcher signals were cached for 60 s:
            # every confirmed game read as "no game" and the LLM loaded
            # mid-match. The last list read is kept instead.
            logger.warning("process_monitor: reading the classified games failed (%s) — "
                           "using the last list read", e)
            if _cached_targets:
                targets.update(_cached_targets)

        extra = getattr(settings, "EXTRA_GAME_PROCESSES", "")
        if extra:
            targets.update(p.strip().lower() for p in extra.split(",") if p.strip())

        _cached_targets = frozenset(targets)
        _cached_time = now

    return _cached_targets


def _probe_game_process() -> bool:
    global _cached_game_pid
    targets = _game_targets()

    # ⚡ Bolt: Fast path - avoid iterating running processes if we have no targets
    if not targets:
        return False

    # ⚡ Bolt: Check cached PID first to avoid OS-level process iteration overhead
    if _cached_game_pid is not None:
        try:
            p = psutil.Process(_cached_game_pid)
            if p.name().lower() in targets:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        # Clear if process is dead or no longer a target
        _cached_game_pid = None

    # ⚡ Bolt: Early stop - check targets during iteration rather than
    # building a full set of all running process names first
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
            if name and name.lower() in targets:
                _cached_game_pid = proc.pid
                return True
        except (psutil.Error, AttributeError):
            continue

    return False


def is_game_running() -> bool:
    """Return True when a known game or game-launcher signal is detected, or
    when something other than Ollama has been holding the GPU. Off the loop
    both probes run (no short circuit), so the verdicts the loop reads stay
    fresh for each cause.

    With a PC agent both read the PC's report. No report (the PC is off,
    asleep or shutting down) fails closed, like an unreadable game flag:
    nobody can say that card is free."""
    pc = _pc_report()
    if pc is not _LOCAL:
        if pc is None:
            return True
        pressed = gpu_pressure(smi=lambda: _smi_from_report(pc.get("gpu")))
        game = game_process_running()
        return pressed or game
    pressed = gpu_pressure()
    game = game_process_running()
    return pressed or game


async def _our_models() -> list[str]:
    """Every model Curatarr may hold in VRAM, for unload_llm_models."""
    try:
        from src.database.runner import run_read
        from src.services.embed_service import effective_embedding_model
        embs = [await run_read(effective_embedding_model)]   # reads app_state
    except Exception:
        # Which embedder is active is unknown: unload the default profile's
        # model AND the legacy one, or a game may start beside the real one.
        _warn_rate_limited("embedding_model", "[gpu] active embedding model unreadable - "
                           "unloading the default profile model and the legacy one")
        from src.services.embedding_migration import V2_PROFILE
        embs = [V2_PROFILE.get("model"), settings.EMBEDDING_MODEL]
    return list(dict.fromkeys(m for m in (
        settings.CURATOR_MODEL,
        settings.SUMMARIZER_MODEL,
        *embs,   # the ACTIVE profile model, not the legacy v1 default
        (settings.PITCHER_MODEL or "").strip(),  # two-bake split (empty = off)
    ) if m))


async def unload_llm_models(url: "str | None" = None) -> list[str]:
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

    ``url``: another host's Ollama (the gaming PC on a split install);
    default this install's. Returns the model names that were actually
    unloaded.
    """
    import httpx
    ollama_url = url or settings.effective_ollama
    ours = await _our_models()
    unloaded: list[str] = []
    async with httpx.AsyncClient(verify=tls_context(), timeout=10) as client:
        # Which of our models are currently loaded?
        probe_ok = False
        resident: set[str] = set()
        try:
            ps = await client.get(f"{ollama_url}/api/ps")
            if ps.status_code < 400:
                resident = {m.get("name", "") for m in ps.json().get("models", [])}
                probe_ok = True
        except httpx.HTTPError as e:
            logger.debug("Ollama /api/ps probe failed: %s", e)
        except Exception as e:
            # Falls back to the unconditional unload below either way.
            logger.warning("Ollama /api/ps probe failed: %s", e)
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
                else:
                    # Unlogged before: the game watcher believed VRAM was freed.
                    logger.warning("Model unload for %s: Ollama answered HTTP %s — it may still "
                                   "hold VRAM", model, r.status_code)
            except Exception as e:
                logger.warning("Model unload failed for %s: %s", model, e)
    return unloaded


# ── Asking "is this a game?" (2026-10-07) ───────────────────────────────────
# Every running exe nobody had classified got a toast, whether it touched the
# card or not: 530 "not a game" answers by October, 95 of them versions of
# seven programs (68 InstallShield extractors _is####.exe alone) and 56 more
# installers, updaters and crash handlers, plus awk.exe, cut.exe, ffmpeg.exe.
# A game holds gigabytes; a program this small needs no card freed for it,
# and a busy card is caught by the GPU-pressure gate whatever its name.
PROMPT_MIN_RSS_MB = 500
_NEVER_ASK = re.compile(r"setup|install|update|unins|redist|(?:^|[^a-z])patch"
                        r"|crash(?:pad|handler|_handler|report)|^_is")
_HEXRUN = re.compile(r"[0-9a-f]*\d[0-9a-f]*")
_HEXSEQ = re.compile(r"#(?:[._\-]?#)+")


def process_family(name: str) -> str:
    """The name with version numbers and hashes folded: _is4993.exe and
    _is3b83.exe are _is#.exe, vortex-setup-2.0.1.exe is vortex-setup-#.exe.
    A "not a game" answer covers its whole family; games stay exact, since
    fallout4.exe and fallout76.exe fold alike."""
    s = (name or "").lower()
    if s.endswith(".exe"):
        s = s[:-4]
    return _HEXSEQ.sub("#", _HEXRUN.sub("#", s)) + ".exe"


def _classified() -> "tuple | None":
    """(every classified name, the families of the "not a game" ones),
    cached 60 s; invalidate_process_cache() drops it after an answer.
    None when the DB read failed (not cached, retried next call)."""
    import time
    global _cached_classified, _cached_classified_time
    now = time.time()
    if _cached_classified is None or now - _cached_classified_time > 60:
        try:
            from src.database.connection import get_db_session
            from src.database.models import GameProcess
            with get_db_session() as db:
                rows = db.query(GameProcess.process_name, GameProcess.is_game).all()
        except Exception as e:
            # An empty set here made every running exe "unknown" and flooded
            # the owner with "is this a game?" for programs already answered.
            logger.warning("process_monitor: reading the classified processes failed (%s) — "
                           "no game prompts this round", e)
            return None
        _cached_classified = (frozenset(n.lower() for n, _ in rows),
                              frozenset(process_family(n) for n, g in rows if not g))
        _cached_classified_time = now
    return _cached_classified


def get_unknown_processes() -> list[dict]:
    """Running programs worth the question "is this a game?": not a Windows
    or desktop process, not classified (a "not a game" answer covers the
    whole family), not an installer, updater or crash handler, and holding
    at least PROMPT_MIN_RSS_MB of memory."""
    got = _classified()
    if got is None:
        return []
    classified, ignored_families = got

    extra = getattr(settings, "EXTRA_GAME_PROCESSES", "")
    extra_set = {p.strip().lower() for p in extra.split(",") if p.strip()} if extra else set()

    known = SYSTEM_PROCESSES | GAME_LAUNCHER_SIGNALS | classified | extra_set
    min_rss = PROMPT_MIN_RSS_MB * 1024 * 1024

    seen: set[str] = set()
    skipped: set[str] = set()
    unknown: list[dict] = []
    pc = _pc_report()
    if pc is not _LOCAL:
        # The PC's programs, from its agent (rss already in MB there).
        for p in (pc or {}).get("processes") or []:
            name = p.get("name") or ""
            nl = name.lower()
            if (not nl or nl in known or nl in seen or nl in skipped
                    or not nl.endswith(".exe")):
                continue
            if _NEVER_ASK.search(nl) or process_family(nl) in ignored_families:
                skipped.add(nl)
                continue
            if (p.get("rss_mb") or 0) * 1024 * 1024 < min_rss:
                continue
            seen.add(nl)
            unknown.append({"name": name, "pid": None})
        return unknown
    try:
        # No "exe" in the attrs: the loop filters on the NAME alone, and
        # resolving each process's executable path is an extra OS call per
        # process that nothing here reads.
        # ⚡ Bolt: Defer memory_info lookup to only the tiny subset of processes
        # that survive the name filter, rather than querying it for every running
        # process on the system (which takes ~2x longer).
        for proc in psutil.process_iter(["name", "pid"]):
            name = proc.info.get("name")
            if not name:
                continue
            nl = name.lower()
            # Only surface .exe files (Windows executables, not background helpers)
            if nl in known or nl in seen or nl in skipped or not nl.endswith(".exe"):
                continue
            if _NEVER_ASK.search(nl) or process_family(nl) in ignored_families:
                skipped.add(nl)
                continue
            try:
                mem = proc.memory_info()    # None when access is denied
            except psutil.Error:
                mem = None
            if mem is None or mem.rss < min_rss:
                continue                          # another instance may be bigger
            seen.add(nl)
            unknown.append({"name": name, "pid": proc.info.get("pid")})
    except Exception as e:
        # Was DEBUG: a broken scan looked exactly like "nothing to ask".
        logger.warning("get_unknown_processes scan failed: %s", e)

    return unknown
