"""System notifications for the bell: things the app wants the user to act on.

The bell started as the curator's proactive messages plus learned
principles (both assembled in the browser). System-level notices — first of
all "a model you run has new weights" — come from here instead: one list,
built server-side from small providers, so adding a kind is one function
and the frontend renders every kind the same way.

An item: ``{"id", "kind", "severity": "info"|"warn"|"danger", "title",
"text", "action": {"key", "label"} | None}``. ``action.key`` names a
frontend handler from a fixed map (notifications.js SYS_ACTIONS) — the
server never sends a function name to call.

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


# (provider, admin_only) — append new kinds here.
PROVIDERS = [(_model_update_items, True)]


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
            "needs_action": sum(1 for i in items if i.get("action"))}
