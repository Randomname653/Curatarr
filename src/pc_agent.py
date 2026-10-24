"""The PC agent: what the gaming PC tells a Curatarr that runs elsewhere
(2026-10-22, NAS move phase 2).

Curatarr moves onto the always-on NAS and borrows the PC's 24 GB card when
the PC is on and nobody plays. Game detection used to read the processes and
nvidia-smi of the machine Curatarr runs on; on the NAS that is the NAS. This
agent runs on the PC (started by the tray) and reports the raw readings:

    GET /status   ->  {"v": 1, "time": ..., "processes": [{"name", "rss_mb"}],
                        "gpu": [used_mb, total_mb, util] | "missing" | "timeout" | "error",
                        "gpu_procs": [{"name", "pct"}],   # 3D-engine share per program
                        "build": {"head", "latest", "behind", "can_update", "reason"} | null,
                        "node": {"name"},                 # PC_AGENT_NAME, else the computer name
                        "shutting_down": bool}
    POST /update  ->  fast-forward this checkout to origin/main (src/self_update)
                      and restart the tray: {"ok", "updated", "from", "to", "reason"}.
                      Takes no parameters - what to fetch and from where is
                      this checkout's own origin, never the caller's.

It decides nothing. Which process is a game, and whether the card is held,
stays with Curatarr (services/process_monitor, with the owner's classified
games in its database), so that logic is not kept twice.

When the PC goes down - shutdown, logoff, the tray's Shutdown - the agent
says so twice: ``shutting_down`` in the next /status, and right away a POST
to Curatarr's /api/hosts/pc/goodbye (PC_AGENT_NOTIFY_URL), so work stops
being sent before the poll would notice. A hard power-off sends nothing;
Curatarr then sees two missed polls.

Every request carries PC_AGENT_TOKEN in X-Agent-Token (compared in constant
time); without a token configured the agent does not start. The readings are
process names and GPU memory - no paths, no command lines, no user data.
Allow its port in the firewall for the NAS address only.

    python -m src.pc_agent          run it in the foreground (testing)
"""
from __future__ import annotations

import hmac
import json
import logging
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

AGENT_VERSION = 1
TOKEN_HEADER = "X-Agent-Token"
DEFAULT_PORT = 11435
# Programs smaller than this are never games and never worth asking about;
# leaving them out keeps the report small (a desktop runs 300+ processes).
MIN_RSS_MB = 50


def _process_list() -> list[dict]:
    """Running programs with their resident memory, largest first, one
    entry per name (the largest instance)."""
    import psutil
    best: dict[str, int] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = proc.info.get("name")
            mem = proc.info.get("memory_info")
        except (psutil.Error, AttributeError):
            continue
        if not name or mem is None:
            continue
        mb = int(mem.rss // (1024 * 1024))
        if mb >= MIN_RSS_MB and mb > best.get(name, -1):
            best[name] = mb
    return [{"name": n, "rss_mb": mb} for n, mb in sorted(best.items(), key=lambda kv: -kv[1])]


def _gpu_reading():
    """[used_mb, total_mb, util] (each None where nvidia-smi prints [N/A]),
    or "missing" / "timeout" / "error" - the states process_monitor knows."""
    try:
        from src.winproc import NO_WINDOW
    except ImportError:
        NO_WINDOW = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "timeout"
    except (FileNotFoundError, OSError):
        return "missing"
    if out.returncode != 0 or not out.stdout.strip():
        return "error"

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    first = out.stdout.strip().splitlines()[0]
    parts = [num(p.strip()) for p in first.split(",")]
    return (parts + [None, None, None])[:3]


class Agent:
    """The readings plus the shutting-down flag; ``serve`` puts them on HTTP."""

    def __init__(self, token: str, *, processes: Callable[[], list] = _process_list,
                 gpu: Callable[[], object] = _gpu_reading, notify_url: str = "",
                 post: Optional[Callable[[str, dict, str], None]] = None,
                 gpu_procs: Optional[Callable[[], list]] = None,
                 updates=None, apply_update: Optional[Callable[[], dict]] = None,
                 on_updated: Optional[Callable[[dict], None]] = None, name: str = ""):
        if not token or len(token) < 16:
            raise ValueError("PC_AGENT_TOKEN must be set (16+ characters)")
        self.token = token
        self._processes = processes
        self._gpu = gpu
        if gpu_procs is None:
            from src.gpu_engine import Sampler
            gpu_procs = Sampler().sample
        self._gpu_procs = gpu_procs
        # Self-update (src/self_update): ``updates`` is a Checker for the
        # report, ``on_updated`` restarts the tray after a fast-forward.
        self._updates = updates
        self._apply_update = apply_update
        self._on_updated = on_updated
        self._update_lock = threading.Lock()
        # The node's name as it reports it; the server may rename it.
        self.name = (name or "").strip()[:40] or _computer_name()
        self.notify_url = (notify_url or "").rstrip("/")
        self._post = post or _post_json
        self.shutting_down = False
        self.server: Optional[ThreadingHTTPServer] = None

    def authorized(self, given: Optional[str]) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    def snapshot(self) -> dict:
        try:
            procs = self._processes()
        except Exception as e:  # noqa: BLE001 - a failed walk reports none, never kills the agent
            logger.warning("[agent] process walk failed: %s", e)
            procs = []
        try:
            gpu = self._gpu()
        except Exception as e:  # noqa: BLE001 - same for nvidia-smi
            logger.warning("[agent] GPU reading failed: %s", e)
            gpu = "error"
        try:
            gpu_procs = self._gpu_procs()
        except Exception as e:  # noqa: BLE001 - and for the engine counters
            logger.warning("[agent] per-program GPU reading failed: %s", e)
            gpu_procs = []
        build = None
        if self._updates is not None:
            try:
                build = self._updates.report()
            except Exception as e:  # noqa: BLE001 - the update state is a hint, never fatal
                logger.warning("[agent] update state unreadable: %s", e)
        return {"v": AGENT_VERSION, "time": time.time(), "processes": procs,
                "gpu": gpu, "gpu_procs": gpu_procs, "build": build,
                "node": {"name": self.name}, "shutting_down": self.shutting_down}

    def update(self) -> "tuple[int, dict]":
        """(HTTP status, body) for POST /update. One at a time; a restart
        follows a fast-forward, a second later, after the answer is sent."""
        if self._apply_update is None:
            return 501, {"ok": False, "reason": "this agent cannot update itself"}
        if self.shutting_down:
            return 409, {"ok": False, "reason": "the PC is shutting down"}
        if not self._update_lock.acquire(blocking=False):
            return 409, {"ok": False, "reason": "an update is already running"}
        try:
            res = self._apply_update()
        except Exception as e:  # noqa: BLE001 - reported to the caller
            res = {"ok": False, "updated": False, "reason": f"update failed: {e}"[:200]}
        finally:
            self._update_lock.release()
        if self._updates is not None:
            self._updates.forget()
        if res.get("updated") and self._on_updated is not None:
            logger.info("[agent] updated to %s - restarting", (res.get("to") or "")[:7])
            threading.Timer(1.0, self._on_updated, args=(res,)).start()
        return 200, res

    def announce_shutdown(self) -> None:
        """Mark the PC as going away and tell Curatarr at once. Never raises
        and returns within a few seconds: it runs while Windows ends the
        session."""
        if self.shutting_down:
            return
        self.shutting_down = True
        logger.info("[agent] PC is shutting down - telling Curatarr")
        if not self.notify_url:
            return
        try:
            self._post(f"{self.notify_url}/api/hosts/pc/goodbye", {"reason": "shutdown"},
                       self.token)
        except Exception as e:  # noqa: BLE001 - Curatarr notices the missed polls instead
            logger.warning("[agent] goodbye to %s failed: %s", self.notify_url, e)

    # ── HTTP ────────────────────────────────────────────────────────────────
    def serve(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:  # nosec B104 - the NAS polls it over the LAN; the firewall rule names the NAS
        agent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "curatarr-agent"
            sys_version = ""

            def do_GET(self):  # noqa: N802 - http.server's naming
                if self.path.split("?", 1)[0] != "/status":
                    return self._send(404, {"error": "not found"})
                if not agent.authorized(self.headers.get(TOKEN_HEADER)):
                    return self._send(401, {"error": "token required"})
                return self._send(200, agent.snapshot())

            def do_POST(self):  # noqa: N802 - http.server's naming
                if self.path.split("?", 1)[0] != "/update":
                    return self._send(404, {"error": "not found"})
                if not agent.authorized(self.headers.get(TOKEN_HEADER)):
                    return self._send(401, {"error": "token required"})
                code, body = agent.update()
                return self._send(code, body)

            def _send(self, code: int, body: dict):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):   # quiet: the NAS polls every few seconds
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="curatarr-agent",
                         daemon=True).start()
        logger.info("[agent] reporting on port %d", self.server.server_address[1])
        return self.server

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def _computer_name() -> str:
    try:
        return (socket.gethostname() or "").strip()[:40]
    except OSError:
        return ""


def _post_json(url: str, body: dict, token: str) -> None:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          TOKEN_HEADER: token})
    with urllib.request.urlopen(req, timeout=3) as r:  # nosec B310 - an http(s) URL from .env
        r.read()


def start_from_settings(settings, on_updated: Optional[Callable[[dict], None]] = None
                        ) -> Optional[Agent]:
    """The tray's entry: an agent when PC_AGENT_TOKEN is set, else None.
    ``on_updated`` (the tray's restart) enables POST /update. Never raises -
    the tray works without it."""
    token = (getattr(settings, "PC_AGENT_TOKEN", None) or "")
    token = token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
    if not token.strip():
        return None
    try:
        from src import self_update
        agent = Agent(token.strip(), notify_url=getattr(settings, "PC_AGENT_NOTIFY_URL", "") or "",
                      updates=self_update.Checker(),
                      apply_update=self_update.apply if on_updated else None,
                      on_updated=on_updated,
                      name=getattr(settings, "PC_AGENT_NAME", "") or "")
        agent.serve(port=int(getattr(settings, "PC_AGENT_PORT", DEFAULT_PORT) or DEFAULT_PORT))
        return agent
    except Exception as e:  # noqa: BLE001 - a busy port or a short token must not stop the tray
        logger.warning("[agent] not started: %s", e)
        return None


if __name__ == "__main__":
    from src.config import settings as _settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _agent = start_from_settings(_settings)
    if _agent is None:
        raise SystemExit("PC_AGENT_TOKEN is not set (16+ characters) - nothing to run")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _agent.announce_shutdown()
        _agent.stop()
