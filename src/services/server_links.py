"""Curatarr — a link that opens a title on the member's own media server.

Both adapters build deep links (media_server/*_adapter.deep_link), but only
the owner's duplicate and music views used them; a member could talk about a
title, see it recommended or swipe it, and never open it from Curatarr (UX
audit 2026-11-08, C6). One helper for every member surface: the Tonight
card, the chat's action row, the recommendation cards.

Plex goes through app.plex.tv with the server's machine id, so the link
works for every account the server is shared with, at home or away (the
server's own LAN address would not); Jellyfin through the adapter's
``deep_link`` on the server's web app.
"""
import logging

logger = logging.getLogger(__name__)

PLEX_WEB = "https://app.plex.tv/desktop/#!/server/{mid}/details?key=%2Flibrary%2Fmetadata%2F{key}"


async def attach_links(items: list, key_field: str = "key") -> None:
    """Set ``item["link"]`` on every item whose ``key_field`` is a key of the
    active server. Never raises: a surface works without links."""
    from src.services.media_server import active_kind
    from src.services.media_server.base import is_jellyfin_key, is_plex_key
    try:
        if active_kind() == "jellyfin":
            from src.services.media_server import get_adapter
            jf = get_adapter()
            for it in items:
                if is_jellyfin_key(it.get(key_field)):
                    it["link"] = jf.deep_link(str(it[key_field]))
            return
        from src.services.plex_playlists import get_machine_identifier
        mid = await get_machine_identifier()
        if not mid:
            return
        for it in items:
            if is_plex_key(it.get(key_field)):
                it["link"] = PLEX_WEB.format(mid=mid, key=it[key_field])
    except Exception as e:  # noqa: BLE001 - links are a convenience; the surface stands without them
        logger.warning("[links] no server links: %s", e)
