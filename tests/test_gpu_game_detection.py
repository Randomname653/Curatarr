"""A new game on the PC is caught by what it does to the card (2026-10-23).

- The PC agent reports each program's share of the card's 3D engine
  (src/gpu_engine.py, the Windows "GPU Engine" counters).
- An unclassified program that draws 30 %+ on two polls in a row (~10 s)
  takes the PC out at once - not after the pressure gate's 45 s. Ollama's
  runners, Windows itself and programs answered "not a game" never count.
- The "is this a game?" question is about the PC's programs whenever an
  agent is configured (a split install asked about the NAS container), and
  it waits in the bell until answered; a program that drew on the card is
  asked first.

Fakes only: no agent, no database, no Ollama.

    python tests/test_gpu_game_detection.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import gpu_engine as ge  # noqa: E402
from src import pc_agent  # noqa: E402
from src.services import notifications as nt  # noqa: E402
from src.services import pc_host  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402

from _checks import check  # noqa: E402


# ── the counters ────────────────────────────────────────────────────────────
check("instance names parse to (pid, engine)",
      ge.parse_instance("pid_4242_luid_0x00000000_0x0000D1A5_phys_0_eng_0_engtype_3D") == (4242, "3D")
      and ge.parse_instance("pid_7_luid_0x0_0x1_phys_0_eng_3_engtype_VideoDecode") == (7, "VideoDecode")
      and ge.parse_instance("_Total") is None and ge.parse_instance("pid_x_engtype_3D") is None)

names = {4242: "NewGame.exe", 4243: "NewGame.exe", 99: "llama-server.exe", 5: "browser.exe"}
rows = [("pid_4242_luid_a_phys_0_eng_0_engtype_3D", 60.0),
        ("pid_4243_luid_a_phys_0_eng_0_engtype_3D", 70.0),       # second process, same program
        ("pid_4242_luid_a_phys_0_eng_4_engtype_Copy", 40.0),     # not the 3D engine
        ("pid_99_luid_a_phys_0_eng_0_engtype_3D", 97.0),
        ("pid_5_luid_a_phys_0_eng_0_engtype_3D", 0.4),           # idle tab
        ("pid_31337_luid_a_phys_0_eng_0_engtype_3D", 50.0)]      # gone meanwhile
out = ge.per_process(rows, names.get)
check("3D only, summed per program and capped at 100, idle and vanished ones left out",
      out == [{"name": "NewGame.exe", "pct": 100.0}, {"name": "llama-server.exe", "pct": 97.0}])

agent = pc_agent.Agent("t" * 24, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0],
                       gpu_procs=lambda: [{"name": "NewGame.exe", "pct": 88.0}])
check("the agent reports them", agent.snapshot()["gpu_procs"] == [{"name": "NewGame.exe", "pct": 88.0}])
broken = pc_agent.Agent("t" * 24, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0],
                        gpu_procs=lambda: 1 / 0)
check("...and a failing counter reports none, never kills the snapshot",
      broken.snapshot()["gpu_procs"] == [])
check("off Windows the sampler reports nothing", sys.platform == "win32" or ge.Sampler().sample() == [])

# ── the NAS's judgement ─────────────────────────────────────────────────────
saved = (pm._classified, pm._game_targets, pc_host.configured, pc_host.snapshot)
classified = {"names": frozenset(), "ignored": frozenset()}
pm._classified = lambda: (classified["names"], classified["ignored"])
pm._game_targets = lambda: frozenset({"fallout4.exe"})


def report(*procs, gpu=(2000.0, 24564.0, 20.0), processes=()):
    return {"gpu": list(gpu), "gpu_procs": [{"name": n, "pct": p} for n, p in procs],
            "processes": [{"name": n, "rss_mb": m} for n, m in processes]}


try:
    pm._gpu_proc_streak.clear()
    pm._gpu_drawing_seen.clear()
    first = pm.pc_busy(report(("NewGame.exe", 85.0)))
    second = pm.pc_busy(report(("NewGame.exe", 85.0)))
    check("one poll is not enough (a video spike must not cut off a chat)", first == (False, ""))
    check("two polls in a row: the PC is busy at once, and says why",
          second[0] and "NewGame.exe draws on the card" in second[1])
    check("...it stops drawing: free again on the next poll",
          pm.pc_busy(report()) == (False, ""))
    check("...and the streak starts over", pm.pc_busy(report(("NewGame.exe", 85.0))) == (False, ""))

    pm._gpu_proc_streak.clear()
    for _ in range(3):
        r = pm.pc_busy(report(("llama-server.exe", 99.0), ("ollama.exe", 40.0), ("dwm.exe", 45.0),
                              ("browser.exe", 12.0)))
    check("Ollama's runners, Windows itself and light use never count", r == (False, ""))

    classified["names"] = frozenset({"blender.exe"})
    classified["ignored"] = frozenset({pm.process_family("blender.exe")})
    for _ in range(3):
        r = pm.pc_busy(report(("blender.exe", 95.0)))
    check("a program answered 'not a game' is exempt (the pressure gate still guards a full card)",
          r == (False, ""))
    classified["names"] = classified["ignored"] = frozenset()

    pm._gpu_proc_streak.clear()
    pm.pc_busy(report(("NewGame.exe", 85.0), processes=[("Fallout4.exe", 9000)]))
    r = pm.pc_busy(report(("NewGame.exe", 85.0), processes=[("Fallout4.exe", 9000)]))
    check("a classified game is named first", r == (True, "Fallout4.exe running"))

    check("no report: busy, and the streak is dropped",
          pm.pc_busy(None)[0] and pm._gpu_proc_streak == {})

    # ── whose programs are asked about ──────────────────────────────────────
    pc_host.configured = lambda: True
    pc_host.snapshot = lambda: report(("NewGame.exe", 85.0),
                                      processes=[("Editor.exe", 900), ("NewGame.exe", 300),
                                                 ("tiny.exe", 60)])
    pm._gpu_drawing_seen.clear()
    pm.unknown_gpu_user(pc_host.snapshot())
    unknown = pm.get_unknown_processes()
    check("with an agent the question is about the PC's programs, the card-drawing one first "
          "(asked even below the memory floor)",
          [u["name"] for u in unknown] == ["NewGame.exe", "Editor.exe"]
          and unknown[0]["gpu"] is True and unknown[1]["gpu"] is False)
    pc_host.snapshot = lambda: None
    check("PC unavailable: nothing to ask", pm.get_unknown_processes() == [])
    check("the split install no longer reads the NAS container for the question",
          "def _question_report" in (ROOT / "src/services/process_monitor.py").read_text(encoding="utf-8"))

    # ── the bell ────────────────────────────────────────────────────────────
    pc_host.snapshot = lambda: report(processes=[("Editor.exe", 900), ("NewGame.exe", 300)])
    pm._gpu_drawing_seen.clear()
    pm.unknown_gpu_user(report(("NewGame.exe", 85.0)))
    nt.forget_game_questions()
    saved_prov = nt.PROVIDERS
    nt.PROVIDERS = [(nt._game_question_items, True)]
    n = nt.system_notifications(type("U", (), {"is_admin": True})())
    items = n["items"]
    check("the bell asks, the card-drawing program first, with both answers",
          [i["title"] for i in items] == ["Is NewGame.exe a game?", "Is Editor.exe a game?"]
          and items[0]["severity"] == "warn" and "draws on the graphics card" in items[0]["text"]
          and items[0]["action"] == {"key": "game_yes", "label": "Yes, a game", "arg": "NewGame.exe"}
          and items[0]["secondary"]["key"] == "game_no")
    check("...counted for the bell, not for the Settings bubble",
          n["needs_action"] == 2 and n["settings_action"] == 0)
    check("...members never see it",
          nt.system_notifications(type("U", (), {"is_admin": False})())["items"] == [])
    pc_host.snapshot = lambda: report(processes=[("Editor.exe", 900)])
    check("...cached between bell reads", len(nt._game_question_items()) == 2)
    nt.forget_game_questions()
    check("...and asked again after an answer", len(nt._game_question_items()) == 1)
    pc_host.configured = lambda: False
    nt.forget_game_questions()
    check("no agent (a desktop install): the toast on that machine asks, the bell does not",
          nt._game_question_items() == [])
    nt.PROVIDERS = saved_prov
finally:
    pm._classified, pm._game_targets, pc_host.configured, pc_host.snapshot = saved
    pm._gpu_proc_streak.clear()
    pm._gpu_drawing_seen.clear()
    nt.forget_game_questions()

# ── wiring ──────────────────────────────────────────────────────────────────
notif_js = (ROOT / "frontend/js/notifications.js").read_text(encoding="utf-8")
game_js = (ROOT / "frontend/js/game.js").read_text(encoding="utf-8")
app_js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
router = (ROOT / "src/routers/process_monitor.py").read_text(encoding="utf-8")
check("the bell's two answers are fixed handlers that pass the name as data",
      "game_yes: a => act('answerGameQuestion', a.arg, true)" in notif_js
      and "game_no: a => act('answerGameQuestion', a.arg, false)" in notif_js
      and "run2(n.secondary)" in notif_js)
check("...registered, and an answer refreshes the bell",
      "  answerGameQuestion,\n" in app_js and "loadUnreadMessages()" in game_js)
check("an answer (toast or bell) forgets the cached question", "forget_game_questions()" in router)

check.done()
