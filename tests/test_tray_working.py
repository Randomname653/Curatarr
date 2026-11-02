"""The agent's tray shows when Curatarr works on this PC's card (2026-11-02).

The web logo's diamond flares ruby while a model is at work; the tray of the
PC agent stayed amber however hard the NAS used the card. Each poll now says
X-Curatarr-Working: 1 while Curatarr has a call on the node's lane, the agent
remembers it (15 s at most without a poll) and the tray recolours the
diamond of its mark.

    python tests/test_tray_working.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import urllib.request

from PIL import Image

from src import pc_agent  # noqa: E402
from src import tray_app  # noqa: E402
from src.services import pc_host  # noqa: E402

TOKEN = "t" * 24
agent = pc_agent.Agent(TOKEN, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0], gpu_procs=lambda: [])
srv = agent.serve("127.0.0.1", 0)
url = f"http://127.0.0.1:{srv.server_address[1]}/status"


def poll(working=None):
    headers = {pc_agent.TOKEN_HEADER: TOKEN}
    if working is not None:
        headers[pc_agent.WORKING_HEADER] = working
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
        return r.status


try:
    check("a fresh agent: nothing is working on its card", not agent.working())
    poll("1")
    check("a poll that says 1: working", agent.working())
    poll()
    check("an older Curatarr says nothing: the state stands", agent.working())
    poll("0")
    check("a poll that says 0: back at once", not agent.working())
    poll("1")
    agent._working_at -= pc_agent.WORKING_TTL_S + 1
    check("no poll for WORKING_TTL_S: back by itself", not agent.working())

    # pc_host's real poll carries the header.
    saved = pc_host.card_working
    try:
        pc_host.card_working = lambda: True
        asyncio.run(pc_host._fetch(url, TOKEN))
        on = agent.working()
        pc_host.card_working = lambda: False
        asyncio.run(pc_host._fetch(url, TOKEN))
        off = agent.working()
    finally:
        pc_host.card_working = saved
    check("Curatarr's own poll tells the agent, both ways", on and not off)
finally:
    agent.stop()

# card_working reads the node's lane in llm_priority.
from src.config import settings  # noqa: E402
from src.services import llm_priority  # noqa: E402

saved_ep = settings.OLLAMA_PC_ENDPOINT
try:
    settings.OLLAMA_PC_ENDPOINT = "http://pc.test:11434"
    ln = llm_priority.lane("http://pc.test:11434")
    ln.active = 1
    busy = pc_host.card_working()
    ln.active = 0
    idle = pc_host.card_working()
    settings.OLLAMA_PC_ENDPOINT = ""
    nonode = pc_host.card_working()
finally:
    settings.OLLAMA_PC_ENDPOINT = saved_ep
    llm_priority._reset_lanes()
check("working = a call on the node's lane; no node, never", busy and not idle and not nonode)

# The ruby diamond.
img = Image.new("RGBA", (3, 1))
img.putpixel((0, 0), (*tray_app._DIAMOND, 255))
img.putpixel((1, 0), (*tray_app._AMBER, 255))
img.putpixel((2, 0), (250, 210, 120, 128))   # an anti-aliased diamond edge
out = tray_app._ruby_diamond(img)
check("the diamond turns ruby, the amber stays, an edge keeps its alpha",
      out.getpixel((0, 0)) == (*tray_app._RUBY, 255) and out.getpixel((1, 0)) == (*tray_app._AMBER, 255)
      and out.getpixel((2, 0)) == (*tray_app._RUBY, 128) and img.getpixel((0, 0))[0] == 255)
for classic in (False, True):
    saved_c = tray_app._classic_icon
    tray_app._classic_icon = lambda _i=None, c=classic: c
    try:
        work, rest = tray_app._brand_icon(64, working=True), tray_app._brand_icon(64)
    finally:
        tray_app._classic_icon = saved_c
    ruby = sum(1 for y in range(64) for x in range(64) if work.getpixel((x, y))[:3] == tray_app._RUBY)
    check(f"{'classic' if classic else 'modern'} mark: a ruby diamond while working, none at rest",
          ruby > 20 and not any(rest.getpixel((x, y))[:3] == tray_app._RUBY
                                for y in range(64) for x in range(64)), ruby)

check.done()
