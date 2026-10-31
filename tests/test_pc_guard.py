"""A call to the gaming PC ends the moment the PC stops taking work
(2026-10-22, NAS move phase 2, PR 4b).

A hard power-off used to leave a request hanging until its read timeout -
5 minutes for a verdict, 10 for a chat reply. services/pc_guard cuts calls to
OLLAMA_PC_ENDPOINT off when services/pc_host reports the PC lost (gone,
draining, or busy with a game) and raises PcLost - an httpx.ConnectError, so
every caller already treats it as "Ollama not reachable".

Real HTTP: a local server that hangs mid-answer plays the PC.

    python tests/test_pc_guard.py
"""
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.config import settings  # noqa: E402
from src.services import pc_guard, pc_host  # noqa: E402

from _checks import check  # noqa: E402


class Hanging(BaseHTTPRequestHandler):
    """/headers-hang: never answers. /body-hang: headers and one chunk, then
    silence. /ok: a quick answer."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/headers-hang":
            time.sleep(8)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        if self.path == "/ok":
            body = b'{"message":{"content":"hi"},"done":true}\n'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        line = b'{"message":{"content":"Hel"}}\n'
        self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
        self.wfile.flush()
        time.sleep(8)

    def log_message(self, *a):
        return


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hanging)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


pc_srv, PC = serve()
other_srv, OTHER = serve()
saved = (settings.OLLAMA_PC_ENDPOINT, settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN)
settings.OLLAMA_PC_ENDPOINT, settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = PC, "http://agent", "t" * 24


def usable():
    pc_host._state.update(available=True, draining=False, busy=False, misses=0)


async def timed(coro_fn, lose, after=0.4):
    """Run coro_fn(); call ``lose`` after ``after`` s. (exception or result, seconds)."""
    async def _lose():
        await asyncio.sleep(after)
        lose()
    loser = asyncio.ensure_future(_lose())
    t0 = time.perf_counter()
    try:
        out = await coro_fn()
    except Exception as e:  # noqa: BLE001 - the test inspects it
        out = e
    loser.cancel()
    return out, time.perf_counter() - t0


async def post(url, path):
    async with pc_guard.ollama_client(timeout=30) as c:
        r = await c.post(f"{url}{path}", json={})
        return r.text


async def stream(url, path):
    got = []
    async with pc_guard.ollama_client(timeout=30) as c:
        async with c.stream("POST", f"{url}{path}", json={}) as r:
            async for line in r.aiter_lines():
                got.append(line)
    return got


try:
    # ── generation: counts the times the PC STOPPED taking work ────────────
    usable()
    g = pc_host.generation()
    pc_host.set_busy(True, "a game")
    pc_host.set_busy(True, "still the game")
    check("a game bumps the generation once", pc_host.generation() == g + 1)
    pc_host.set_busy(False)
    pc_host.goodbye()
    pc_host.goodbye()
    check("a goodbye bumps it once more, a repeat does not", pc_host.generation() == g + 2)
    pc_host.record(None)
    check("a PC that was already gone bumps nothing", pc_host.generation() == g + 2)

    # ── the guard ───────────────────────────────────────────────────────────
    usable()
    out, s = asyncio.run(timed(lambda: post(PC, "/headers-hang"), pc_host.goodbye))
    check(f"waiting for headers: a goodbye ends the call in {s:.1f} s, not the read timeout",
          isinstance(out, pc_guard.PcLost) and s < 2.5)
    check("PcLost is an httpx.ConnectError (every caller handles it as Ollama down)",
          isinstance(out, httpx.ConnectError))

    usable()
    out, s = asyncio.run(timed(lambda: stream(PC, "/body-hang"),
                               lambda: pc_host.set_busy(True, "Fallout4.exe running")))
    check(f"mid-stream: a game starting ends the stream in {s:.1f} s", isinstance(out, pc_guard.PcLost)
          and s < 2.5)
    pc_host.set_busy(False)

    usable()
    out, s = asyncio.run(timed(lambda: post(PC, "/ok"), pc_host.goodbye, after=5))
    check("a call that finishes is untouched", out == '{"message":{"content":"hi"},"done":true}\n')

    usable()
    out, s = asyncio.run(timed(lambda: post(OTHER, "/ok"), pc_host.goodbye, after=0.0))
    check("calls to any other host pass through, PC lost or not",
          out == '{"message":{"content":"hi"},"done":true}\n')

    # ── a verdict on the PC fails closed as an outage ───────────────────────
    from src.services import ollama_hosts, pillars
    from src.services.llm_errors import INFRA, LLMFailure
    usable()
    # post_chat posts to <host>/api/chat: make every path hang for this one
    Hanging.do_POST_orig = Hanging.do_POST

    def hang_api(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(8)
    Hanging.do_POST = hang_api
    try:
        out, s = asyncio.run(timed(
            lambda: pillars.adjudicate("TITLE: X (2001) - movie", skip_priority=True,
                                       host=ollama_hosts.pc_host()),
            pc_host.goodbye))
    finally:
        Hanging.do_POST = Hanging.do_POST_orig
    check(f"a verdict cut off by a goodbye is an outage EVALUATE in {s:.1f} s",
          isinstance(out, dict) and out.get("verdict") == "EVALUATE"
          and LLMFailure(out.get("_error_kind")) in INFRA and s < 2.5)
finally:
    settings.OLLAMA_PC_ENDPOINT, settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = saved
    pc_srv.shutdown()
    other_srv.shutdown()

# ── wiring: every model call uses the guarded client ────────────────────────
import re  # noqa: E402
src = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py")}
# Files that send model work to a routed host must build their Ollama
# clients with ollama_client. Embeddings stay on the main host; /api/ps and
# the unload are management, quick, and harmless when they fail.
routed = sorted(f for f, t in src.items()
                if re.search(r"(ollama_for|host_for)\(\s*\"(chat|curator|judge|enrich)", t)
                or re.search(r"\bhost\.url\b|\b_url\b.*api/chat", t))
# post_chat counts as guarded: it builds its client with ollama_client
# (the check right below pins that), so a file whose only model call goes
# through it is covered.
unguarded = sorted(f for f in routed if "ollama_client(" not in src[f]
                   and "post_chat(" not in src[f]
                   and f not in ("src/services/ollama_hosts.py",))
check(f"every routed model call builds its client with ollama_client ({unguarded or 'all do'})",
      not unguarded)
check("post_chat (the judge's call) is guarded", "ollama_client(" in src["src/services/llm_errors.py"])
chat = src["src/routers/chat.py"]
check("a chat turn retries once on the main host when the PC goes away before the first word",
      "except PcLost:" in chat and "if full_response or _attempt > 1 or _fallback.url == _url:" in chat
      and "_msgs, _ = _fm(messages, _bf(_fallback.num_ctx, _predict))" in chat)

check.done()
