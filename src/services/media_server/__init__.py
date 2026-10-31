"""The media-server seam (docs/JELLYFIN_DESIGN.md §6).

``get_adapter()`` returns the adapter of the ONE media server this install
runs (design D1): ``MEDIA_SERVER_TYPE``, and Plex when it is unset, which is
every install from before Jellyfin support.
"""
from __future__ import annotations

from typing import Optional

from src.services.media_server.base import MediaServerAdapter, ServerKind


class MediaServerNotConfigured(RuntimeError):
    """The chosen media server has no address or credential."""


def active_kind(settings=None) -> ServerKind:
    if settings is None:
        from src.config import settings
    return settings.MEDIA_SERVER_TYPE or "plex"


def _secret(value) -> str:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else (value or "")


def get_adapter(settings=None, *, transport=None, kind: Optional[str] = None) -> MediaServerAdapter:
    """A fresh adapter for the active server, or for ``kind`` when the work
    names its server (a deletion proposal's ``service``). Cheap: adapters
    hold no connection, each call opens its own client. Raises
    MediaServerNotConfigured when the server lacks its address or key."""
    if settings is None:
        from src.config import settings
    kind = kind or active_kind(settings)
    if kind == "jellyfin":
        url: Optional[str] = str(settings.JELLYFIN_URL) if settings.JELLYFIN_URL else None
        key = _secret(settings.JELLYFIN_API_KEY)
        if not url or not key:
            raise MediaServerNotConfigured("Jellyfin needs JELLYFIN_URL and JELLYFIN_API_KEY")
        from src.services.media_server.jellyfin_adapter import JellyfinAdapter
        return JellyfinAdapter(url, key, transport=transport)
    url, token = settings.effective_plex_url, settings.effective_plex_token
    if not url or not token:
        raise MediaServerNotConfigured("Plex needs PLEX_URL and PLEX_TOKEN")
    from src.services.media_server.plex_adapter import PlexAdapter
    return PlexAdapter(url, token, transport=transport)
