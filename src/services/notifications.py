"""System notifications for the bell: things the app wants the user to act on.

The bell started as the curator's proactive messages plus learned
principles (both assembled in the browser). System-level notices — first of
all "a model you run has new weights" — come from here instead: one list,
built server-side from small providers, so adding a kind is one function
and the frontend renders every kind the same way.

An item: ``{"id", "kind", "severity": "info"|"warn"|"danger", "title",
"text", "action": {"key", "label", "arg"?} | None, "secondary"?: {...}}``.
``action.key`` names a frontend handler from a fixed map (notifications.js
SYS_ACTIONS) — the server never sends a function name to call; ``arg`` is
the one value that handler gets (data, escaped like any other).

``needs_action`` counts the items that wait for a decision; that number is
the bell badge's share and the Settings bubble in the sidebar.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _model_update_items() -> list[dict]:
    from src.routers.model_updates import _running
    from src.services import model_updates as mu
    items = []
    for m in mu.public_view(mu.load_state()):
        role, base = m["role"], m["base"]
        nid = f"model-update:{role}:{(m.get('remote') or '')[7:19]}"
        if _running.get("role") == role:
            review = _running.get("kind") == "review"
            items.append({"id": nid, "kind": "model_update", "severity": "info",
                          "title": (f"The current model reviews the {base} test" if review
                                    else f"Testing the new {base}"),
                          "text": ("Blind, both orders, then a recommendation — progress in Activity."
                                   if review else
                                   "Old against new on ten of your titles — progress in Activity."),
                          "action": None})
        elif m.get("comparison") and m.get("eval_error") and not m["comparison"].get("evaluation"):
            items.append({"id": nid, "kind": "model_update", "severity": "danger",
                          "title": f"Review failed: {base}",
                          "text": f"The test result stands; the review broke off ({m['eval_error'][:160]}).",
                          "action": {"key": "model_updates", "label": "Open"}})
        elif m.get("comparison"):
            s = m["comparison"].get("summary") or {}
            ev = m["comparison"].get("evaluation") or {}
            rec = ({"update": "The current model recommends the update.",
                    "stay": "The current model recommends staying.",
                    "either": "The current model sees no clear winner."}.get(ev.get("recommendation"), "")
                   + (" (Hard rule: " + ev["guards"][0] + ".)" if ev.get("guards") else ""))
            items.append({"id": nid, "kind": "model_update", "severity": "warn",
                          "title": f"Test result ready: {base}",
                          "text": (f"{s.get('verdicts_same')} of {s.get('verdicts_compared')} verdicts unchanged, "
                                   f"wording {s.get('wording_similarity_pct')} % alike. "
                                   f"{rec or 'Take the update or stay on the pinned version.'}"),
                          "action": {"key": "model_updates", "label": "See the result"}})
        elif m.get("compare_error") and m["status"] == "update":
            items.append({"id": nid, "kind": "model_update", "severity": "danger",
                          "title": f"Update test failed: {base}",
                          "text": m["compare_error"][:240],
                          "action": {"key": "model_updates", "label": "Open"}})
        elif m["status"] == "update":
            how = ("Test old against new on your titles, then decide."
                   if m.get("comparable") else
                   "Update by hand when you want it — no test for this role yet.")
            items.append({"id": nid, "kind": "model_update", "severity": "warn",
                          "title": f"New weights for {base} ({role})",
                          "text": f"Your models still run the pinned version. {how}",
                          "action": {"key": "model_updates",
                                     "label": "Test old vs new" if m.get("comparable") else "Details"}})
    return items


_GAME_Q_MAX = 3
_game_q_cache: dict = {"at": 0.0, "items": []}


def _game_question_items() -> list[dict]:
    """The "is this a game?" question for the PC's unclassified programs, kept in the bell
    until answered: the toast only reached an admin who had the app open
    while the program ran. A program that drew on the card comes first and
    says so - it keeps Curatarr off the PC until it is answered. Cached 30 s:
    every open client polls the bell.

    Only with a PC agent: the programs are the PC's, and the admin is
    usually somewhere else (the NAS's app, a phone). On a desktop install
    the toast comes up on the machine the program runs on."""
    from src.services import pc_host
    if not pc_host.configured():
        return []
    import time
    now = time.monotonic()
    if now - _game_q_cache["at"] < 30:
        return _game_q_cache["items"]
    from src.services.process_monitor import get_unknown_processes
    items = []
    for u in get_unknown_processes()[:_GAME_Q_MAX]:
        name = u["name"]
        node = pc_host.node_name()
        items.append({"id": f"game-question:{name.lower()}", "kind": "game_question",
                      "severity": "warn" if u.get("gpu") else "info",
                      "title": f"Is {name} a game?",
                      "text": (f"It draws on the graphics card of {node}: Curatarr keeps its "
                               "work off that card while it runs. Your answer decides next time."
                               if u.get("gpu") else
                               f"A large program on {node}. A game frees the card for it; "
                               "anything else is never asked again."),
                      "action": {"key": "game_yes", "label": "Yes, a game", "arg": name},
                      "secondary": {"key": "game_no", "label": "Not a game", "arg": name}})
    _game_q_cache.update(at=now, items=items)
    return items


def forget_game_questions() -> None:
    """An answer came in: the next bell read asks again."""
    _game_q_cache["at"] = 0.0


_pc_update_hidden = {"until": 0.0}
_BELL_ONLY = frozenset({"game_question", "pc_update"})


def _pc_update_items() -> list[dict]:
    """The node runs an older Curatarr than main (its agent checks every
    6 h and reports it). One click updates it; when it cannot fast-forward,
    the bell says why and how instead."""
    import time
    from src.services import pc_host
    if not pc_host.configured() or time.time() < _pc_update_hidden["until"]:
        return []
    b = pc_host.build()
    if not b or not b.get("behind"):
        return []
    n = int(b["behind"])
    behind = f"{n} commit{'s' if n != 1 else ''} behind main"
    title = f"{pc_host.node_name(cap=True)} runs an older Curatarr"
    if b.get("can_update"):
        return [{"id": f"pc-update:{(b.get('latest') or '')[:12]}", "kind": "pc_update",
                 "severity": "warn", "title": title,
                 "text": f"It is {behind}. Updating restarts its agent: work on its card "
                         f"moves to this server for a few seconds.",
                 "action": {"key": "pc_update", "label": f"Update {pc_host.node_name()}"}}]
    return [{"id": f"pc-update:{(b.get('latest') or '')[:12]}", "kind": "pc_update",
             "severity": "info", "title": title,
             "text": f"It is {behind}, and cannot update itself: {b.get('reason') or 'unknown'}. "
                     f"Run git pull in its Curatarr folder.",
             "action": None}]


def forget_pc_update() -> None:
    """An update was asked for: hide the item until the restarted agent has
    reported (it re-checks on start)."""
    import time
    _pc_update_hidden["until"] = time.time() + 120


# (provider, admin_only) — append new kinds here.
PROVIDERS = [(_model_update_items, True), (_game_question_items, True),
             (_pc_update_items, True)]


def system_notifications(user) -> dict:
    items = []
    for provider, admin_only in PROVIDERS:
        if admin_only and not getattr(user, "is_admin", False):
            continue
        try:
            items += provider()
        except Exception as e:   # one broken source must not empty the bell
            logger.warning("[notifications] %s failed: %s", provider.__name__, e)
    return {"items": items,
            "needs_action": sum(1 for i in items if i.get("action")),
            # The sidebar's Settings bubble: decisions that live in Settings
            # (a game question or the PC's update is answered in the bell).
            "settings_action": sum(1 for i in items
                                   if i.get("action") and i["kind"] not in _BELL_ONLY)}
