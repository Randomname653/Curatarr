"""The deletion judge without the node (2026-11-01).

A split install's deletion runs waited for the node (the 24 GB PC) forever:
its verdicts never came from the NAS's smaller model. With the PC abroad for
months that means no deletion proposals at all. Now:

- the server remembers when the node last took work (app_state, so a restart
  does not reset the clock);
- after JUDGE_FALLBACK_HOURS (72) without the node free, the NAS judges with
  its own profile; -1 always waits, 0 never does;
- a queued Analyze starts then by itself, and its note says when that is;
- each proposal records the profile that judged it, and the node re-judges
  the server's proposals once it is back (test_deletion_pool covers that).

    python tests/test_judge_fallback.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import time

conn = app_db()
from src.config import settings  # noqa: E402
from src.services import app_state  # noqa: E402
from src.services import deletion_queue as dq  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402
from src.services import pc_host  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402

KEYS = ("OLLAMA_PC_ENDPOINT", "PC_AGENT_URL", "PC_AGENT_TOKEN", "JUDGE_FALLBACK_HOURS")
saved = {k: getattr(settings, k) for k in KEYS}
saved_state, saved_free, saved_kick = dict(pc_host._state), dict(pc_host._free), dq.kick
main = settings.effective_ollama
QUIET = {"v": 1, "processes": [], "gpu": [3000.0, 24564.0, 5.0], "shutting_down": False}
kicks = []


async def fake_kick():
    kicks.append(oh.judge_ready())
    return False


def poll(report):
    async def fetch(url, tok):
        if isinstance(report, Exception):
            raise report
        return report
    asyncio.run(pc_host.poll_once(fetch))


def judge_url():
    try:
        return oh.host_for("judge").url
    except oh.PcUnavailable:
        return None


def away_for(hours):
    pc_host._free["at"] = time.time() - hours * 3600


try:
    dq.kick = fake_kick
    settings.JUDGE_FALLBACK_HOURS = 72
    settings.OLLAMA_PC_ENDPOINT = ""
    check("not split: the server judges, no fallback involved",
          judge_url() == main and oh.judge_ready() and not oh.judge_fallback())

    settings.OLLAMA_PC_ENDPOINT = "http://pc:11434"
    settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = "http://pc:11435", "t" * 24
    pc_host._state.update(available=False, draining=False, misses=0, busy=False, snapshot=None)
    pc_host._free.update(at=0.0, stored=0.0, loaded=False, start=time.time())
    pm._cached_targets, pm._cached_time = frozenset({"fallout4.exe"}), 10 ** 12
    pm._pc_gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
    poll(QUIET)
    check("the node free: it judges", judge_url() == "http://pc:11434" and not oh.judge_fallback())
    stored = float(app_state.get_state(pc_host._FREE_KEY) or 0)
    check("...and the server stores when it last took work", abs(stored - time.time()) < 60)

    poll(OSError("timed out"))
    poll(OSError("timed out"))
    check("the node gone for minutes: the deletion run waits",
          judge_url() is None and not oh.judge_ready())
    check("...every poll asks the queue, the failed ones too (after the second miss "
          "nothing may start)", len(kicks) == 3 and kicks[-1] is False, kicks)

    away_for(71)
    check("71 hours away: still waiting", judge_url() is None and not oh.judge_ready())
    away_for(73)
    h = oh.host_for("judge")
    check("73 hours away: the server judges with its own profile",
          h.url == main and h.profile == oh.main_host().profile
          and oh.judge_fallback() and oh.judge_ready())
    kicks.clear()
    poll(OSError("timed out"))
    check("...and a failed poll now lets a queued Analyze start", kicks == [True])
    check("chat was on the server all along", oh.ollama_for("chat") == main)

    settings.JUDGE_FALLBACK_HOURS = -1
    check("JUDGE_FALLBACK_HOURS=-1: it waits however long", judge_url() is None)
    settings.JUDGE_FALLBACK_HOURS = 0
    pc_host._free["at"] = time.time() - 1
    check("JUDGE_FALLBACK_HOURS=0: it never waits", judge_url() == main)
    settings.JUDGE_FALLBACK_HOURS = "junk"
    away_for(73)
    check("an unreadable setting counts as 72 hours", oh.fallback_hours() == 72.0
          and judge_url() == main)
    settings.JUDGE_FALLBACK_HOURS = 72

    # ── a restart does not reset the clock ──────────────────────────────────
    app_state.set_state(pc_host._FREE_KEY, str(int(time.time() - 80 * 3600)))
    pc_host._free.update(at=0.0, stored=0.0, loaded=False, start=time.time())
    check("restarted, before the first poll: away since the start", judge_url() is None)
    poll(OSError("timed out"))
    check("...the first poll reads the stored time: 80 hours away, the server judges",
          judge_url() == main and 79 < (time.time() - pc_host.last_free()) / 3600 < 81)
    app_state.set_state(pc_host._FREE_KEY, "")
    pc_host._free.update(at=0.0, stored=0.0, loaded=False, start=time.time() - 73 * 3600)
    poll(OSError("timed out"))
    check("never seen at all: away since the server started", judge_url() == main)

    # ── back again ──────────────────────────────────────────────────────────
    pc_host._state.update(misses=0)
    poll(QUIET)
    check("the node answers: it judges again, at once",
          judge_url() == "http://pc:11434" and not oh.judge_fallback())

    # ── the queue's note ────────────────────────────────────────────────────
    pc_host._state.update(available=False)
    away_for(30)
    note = dq.note({"category": "anime"})
    check("a queued Analyze says when the server takes over", "in 42 hours" in note, note)
    away_for(0)
    check("...in days once that is far", "in 3 days" in dq.note({"category": None}))
    settings.JUDGE_FALLBACK_HOURS = -1
    check("...and nothing about it when it always waits",
          "this server judges" not in dq.note({"category": None}))
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    pc_host._state.clear()
    pc_host._state.update(saved_state)
    pc_host._free.clear()
    pc_host._free.update(saved_free)
    dq.kick = saved_kick
    pm._cached_targets = None

# ── the run record ──────────────────────────────────────────────────────────
from src.services import recommendations_engine as eng  # noqa: E402

run = eng._record_deletion_run("anime", judged=5, flagged=2, fallback="Big Rig")
check("the run records the node the server judged without",
      run["fallback"] == "Big Rig" and eng.deletion_run_summary(None)["fallback"] == "Big Rig")
eng.DELETION_RUN_SUMMARY.clear()

# ── the stature floor ───────────────────────────────────────────────────────
# Judges below the large model argued away SpongeBob's Emmys and a Grammy
# winner's stature (frozen evidence, 2026-11-01); any judge's CUT on a title
# with a major honour becomes the owner's call. Narrow: an RT score or "63
# wins" is no major honour - two thirds of the right CUTs carry those.
from src.services import pillars  # noqa: E402

check.table("major honours come from the Significance and Awards lines only", [
    ("Emmys and a BAFTA", "  Awards: Nominated for 10 Primetime Emmys. 63 wins & 70 nominations total\n"
     "  Significance: four Emmy Awards, two BAFTA Children's Awards", ["Bafta", "Emmy"]),
    ("a Grammy", "  Significance: won the 2015 Grammy Award for Best Rock Song", ["Grammy"]),
    ("the Film Registry", "  Significance: Selected for the US National Film Registry",
     ["National Film Registry"]),
    ("scores and plain wins are no major honour",
     "  RT: 91%  METACRITIC: 80\n  Awards: 3 wins & 12 nominations", []),
    ("an Oscar in the plot is no honour", "  Plot: a washed-up actor dreams of the Oscar he never won", []),
    ("no facts", "", []),
], pillars.major_honours)
check("the monologue argues the owner's call, honours included, not the cut",
      "major honours" in pillars._GUARD_STANCE["stature_floor"])

check.done()
