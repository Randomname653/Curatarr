"""The bell says when this server's Ollama stops answering (2026-10-30).

The NAS's Ollama app was down for 21 hours (2026-10-29 19:10 onwards):
embeddings, the summariser and memory extraction failed in log lines only,
while chat kept working on the PC. Fakes only: nothing is called.

    python tests/test_host_watch.py
"""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import host_watch as hw  # noqa: E402
from src.services import notifications as nf  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))


logs = Logs()
hw.logger.addHandler(logs)
hw.logger.setLevel(logging.INFO)
saved = (oh.main_host, oh.split, list(nf.PROVIDERS))
oh.main_host = lambda: SimpleNamespace(url="http://nas:30068")
oh.split = lambda: False
up = lambda url, timeout=None: SimpleNamespace(status_code=200)  # noqa: E731


def down(url, timeout=None):
    raise OSError("All connection attempts failed")


try:
    t0 = datetime(2026, 10, 29, 19, 10)
    hw.check(get=up, now=t0)
    check("an answering host is not down", hw.down_since() is None)
    hw.check(get=down, now=t0)
    hw.check(get=down, now=t0 + timedelta(seconds=30))
    check("two missed probes are not an outage yet (a model loading)", hw.down_since() is None
          and nf._ollama_down_items() == [])
    hw.check(get=down, now=t0 + timedelta(seconds=60))
    check("the third is: down since the first miss, said once in the log",
          hw.down_since() == t0
          and [lv for lv, m in logs.lines if "is not answering" in m] == ["WARNING"])
    hw.check(get=down, now=t0 + timedelta(seconds=90))
    check("...and not again on every later probe",
          sum(1 for _, m in logs.lines if "is not answering" in m) == 1)

    items = nf._ollama_down_items()
    check("the bell shows it as danger with the time, what waits and what to do",
          len(items) == 1 and items[0]["severity"] == "danger" and items[0]["action"] is None
          and "Since 19:10" in items[0]["text"] and "Restart the Ollama app" in items[0]["text"]
          and "memories" in items[0]["text"])
    nf.PROVIDERS[:] = [(p, a) for p, a in nf.PROVIDERS if p is nf._ollama_down_items]
    out = nf.system_notifications(SimpleNamespace(is_admin=True))
    check("...counts in the bell badge, not in the Settings bubble (nothing to decide there)",
          out["needs_action"] == 1 and out["settings_action"] == 0)
    check("...and only admins see it",
          nf.system_notifications(SimpleNamespace(is_admin=False))["items"] == [])

    hw.check(get=up, now=t0 + timedelta(hours=21))
    check("back: the bell clears and the log says since when it was down",
          hw.down_since() is None and nf._ollama_down_items() == []
          and any(lv == "INFO" and "answers again (down since 2026-10-29 19:10)" in m for lv, m in logs.lines))
finally:
    oh.main_host, oh.split, nf.PROVIDERS[:] = saved[0], saved[1], saved[2]

src = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the scheduler probes every 30 s, off the event loop",
      'id="ollama_watch"' in src and "await asyncio.to_thread(host_watch.check)" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
