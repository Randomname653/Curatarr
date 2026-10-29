"""History tier B: the Jellyfin Webhook plugin (docs/JELLYFIN_DESIGN.md §4.2).

The plugin posts as things happen; Curatarr listens for ``PlaybackStop`` and
``UserDataSaved``. A post is a HINT, never the truth: it names a user and an
item, and the play is written only when the server itself, re-read with the
API key, says the item is played and was last played within the last half
hour. A forged or replayed post can at most cause that harmless re-read.

The shared secret lives in app_state (generated on the admin's first look at
the setup card, rotatable there) and travels in the ``X-Curatarr-Secret``
header the plugin template sets. Without a secret the route is off.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from src.services.media_server.base import Play, normalize_jellyfin_id

logger = logging.getLogger(__name__)

SECRET_KEY = "jellyfin_hook_secret"
SEEN_KEY = "jellyfin_hook_seen"
HEADER = "X-Curatarr-Secret"
EVENTS = ("PlaybackStop", "UserDataSaved")
FRESH = timedelta(minutes=30)
MAX_BODY = 64 * 1024

# What the plugin's "Generic Destination" sends, Handlebars-style. Only the
# ids decide anything; the device and client names ride along for the row.
TEMPLATE = json.dumps({
    "NotificationType": "{{NotificationType}}", "UserId": "{{UserId}}", "ItemId": "{{ItemId}}",
    "DeviceName": "{{DeviceName}}", "ClientName": "{{ClientName}}",
    "PlaybackPositionTicks": "{{PlaybackPositionTicks}}",
}, indent=2)


async def current_secret(*, create: bool = False) -> Optional[str]:
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state, set_state
    s = await run_read(get_state, SECRET_KEY)
    if not s and create:
        s = secrets.token_urlsafe(32)
        await run_write(set_state, SECRET_KEY, s)
    return s or None


async def rotate_secret() -> str:
    from src.database.runner import run_write
    from src.services.app_state import set_state
    s = secrets.token_urlsafe(32)
    await run_write(set_state, SECRET_KEY, s)
    return s


def _ticks_ms(value) -> Optional[int]:
    try:
        return int(value) // 10_000 if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def handle(payload: dict, *, adapter=None, now: Optional[datetime] = None) -> dict:
    """One post from the plugin. Returns what happened, for the log and the
    plugin's own delivery log; never raises on a payload it does not like."""
    from src.database.runner import run_write
    from src.services.app_state import set_state
    from src.services.media_server.jellyfin_history import record_plays
    now = now or datetime.utcnow()
    kind = str(payload.get("NotificationType") or "")
    if kind not in EVENTS:
        return {"ignored": "event"}
    user_key = normalize_jellyfin_id(payload.get("UserId"))
    item_key = normalize_jellyfin_id(payload.get("ItemId"))
    if not (user_key and item_key):
        return {"ignored": "ids"}
    await run_write(set_state, SEEN_KEY, datetime.utcnow().isoformat())   # when a post arrived, for the badge
    if adapter is None:
        from src.services.media_server import get_adapter
        adapter = get_adapter()
    state = await adapter.user_state(user_key, item_key)
    if state is None or not state.played or not state.last_played_at:
        return {"ignored": "not played"}
    if abs(now - state.last_played_at) > FRESH:
        return {"ignored": "stale"}       # a replay, or a toggle of an old item
    play = Play(user_key=user_key, item_key=item_key, played_at=state.last_played_at,
                source="jellyfin_hook", completed=True,
                position_ms=_ticks_ms(payload.get("PlaybackPositionTicks")),
                device=str(payload.get("DeviceName") or "")[:64] or None,
                client=str(payload.get("ClientName") or "")[:64] or None)
    res = await record_plays([play], adapter)
    # The counts only: nothing the adapter (which holds the API key) handed over.
    logger.info("[jellyfin] webhook %s for %s: %d new, %d merged, %d skipped", kind, item_key,
                int(res.get("inserted", 0)), int(res.get("merged", 0)), int(res.get("skipped", 0)))
    return res
