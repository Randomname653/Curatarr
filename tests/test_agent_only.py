"""PC_AGENT_ONLY: the PC after the move (2026-10-23, NAS move step 6).

Once Curatarr runs on the NAS, the gaming PC only lends its card. Its tray
runs just the agent - no server, no database - and a second Curatarr there
(start.bat out of habit) would sync the same Plex and arrs twice, so the
server refuses to start. docs/NAS_MOVE.md walks the move.

    python tests/test_agent_only.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


check("off by default: every install that is not the PC of a split runs as before",
      settings.PC_AGENT_ONLY is False)

# ── the server refuses on an agent-only PC ──────────────────────────────────
import src.main as app_main  # noqa: E402
opened = []
saved = (settings.PC_AGENT_ONLY, app_main.logger.error)
settings.PC_AGENT_ONLY = True
app_main.logger.error = lambda *a, **k: opened.append(a[0])


async def _boot():
    async with app_main.lifespan(app_main.app):
        opened.append("STARTED")

try:
    asyncio.run(_boot())
    check("an agent-only PC does not start a second Curatarr", False)
except SystemExit as e:
    check("an agent-only PC does not start a second Curatarr (start.bat fails loudly)",
          e.code == 1 and "STARTED" not in opened and any("PC_AGENT_ONLY" in m for m in opened))
finally:
    settings.PC_AGENT_ONLY, app_main.logger.error = saved

main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
life = main_src[main_src.index("async def lifespan("):]
check("...refused before anything opens (Syncthing guard, database)",
      life.index("PC_AGENT_ONLY") < life.index("sync_guard"))

# ── the tray in agent-only mode ─────────────────────────────────────────────
tray = (ROOT / "src/tray_app.py").read_text(encoding="utf-8")
agent_only = tray[tray.index("def _agent_only("):tray.index("def main(")]
main_fn = tray[tray.index("def main("):]
check("the tray branches to agent-only before it checks or opens Curatarr's port",
      main_fn.index("return _agent_only(settings, mutex)") < main_fn.index("_port_in_use(settings.PORT)"))
check("agent-only starts no server and opens no database",
      "uvicorn" not in agent_only and "src.main" not in agent_only and "get_db_session" not in agent_only)
check("...it starts the agent, or exits saying why",
      "pc_agent.start_from_settings(settings)" in agent_only and "sys.exit(1)" in agent_only)
check("...a logoff, a shutdown or Quit tells the NAS first",
      "_install_endsession_hook(_stop_for_good, nothing)" in agent_only
      and agent_only.index("agent.announce_shutdown()") < agent_only.index("icon.stop()")
      and 'MenuItem("Quit agent", lambda: _stop_for_good())' in agent_only)
check("...'Open Curatarr' opens the NAS", "webbrowser.open(nas)" in agent_only
      and "PC_AGENT_NOTIFY_URL" in agent_only)
check("...and it warns when this PC's Ollama is not answering (the NAS needs it)",
      "ollama_answers(" in agent_only)

# ── the runbook ─────────────────────────────────────────────────────────────
doc = (ROOT / "docs" / "NAS_MOVE.md").read_text(encoding="utf-8")
check("the runbook records the embedding weights before the move and checks them after",
      doc.index("Record the embedding weights") < doc.index("Embedding weights match"))
check("...stops the PC's Curatarr before copying, and keeps a copy to roll back to",
      doc.index("Stop Curatarr on the PC") < doc.index("Copy the *contents*")
      and "Rolling back" in doc)
import re  # noqa: E402
check("...and names no real address: placeholders, no LAN IPs, no domains",
      "<nas>" in doc and "<pc>" in doc
      and not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", doc.replace("0.0.0.0", ""))
      and not re.search(r"https?://[a-z0-9-]+\.[a-z0-9.-]+\.(?:de|com|net|org)\b", doc))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
