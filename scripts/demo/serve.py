"""The demo copy's server: no scheduler (no syncs, no custodian ticks, no
writes to anything outside this copy), loopback only. Run with the copy as
the working directory; scripts/demo/build_demo.py starts and stops it.

    python scripts/demo/serve.py [port]
"""
import os
import sys
from pathlib import Path

ROOT = Path.cwd()
if not (ROOT / ".curatarr-demo").exists():
    sys.exit("serve.py runs only inside a demo copy (scripts/demo/build_demo.py makes one)")

# Radarr and Sonarr stay (read only: the deletion analysis and the Knowledge
# Base read their libraries; nothing here approves a deletion). No node, no
# Lidarr, no media server, no SoulSync.
for _k in ("LIDARR_URL", "OLLAMA_PC_ENDPOINT", "PC_AGENT_URL", "SOULSYNC_URL", "SHOKO_URL"):
    os.environ[_k] = ""

sys.path.insert(0, str(ROOT))
import src.services.scheduler as sched  # noqa: E402

sched.start_scheduler = lambda *a, **k: None
sched.stop_scheduler = lambda *a, **k: None

import uvicorn  # noqa: E402

uvicorn.run("src.main:app", host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765,
            log_level="info")
