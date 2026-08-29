"""Optional self-hosted subtitle service — a generic adapter, not a client
for any particular one.

WHY THIS EXISTS
---------------
Anime is where Curatarr's dialogue evidence is thinnest: only about a quarter
of anime in a typical library carries a subtitle file Plex will hand out (the
rest are embedded, which no Plex endpoint serves), and the public fallback has
little for catalogue titles — the exact obscure entries that dominate deletion
candidates. An operator who runs their own subtitle service can close that gap
for their own library.

WHAT IT IS NOT
--------------
No provider is bundled, named, or assumed, and nothing here fetches from any
public archive. This is an HTTP contract: point it at a service you run, or
leave it unset and nothing happens. Curatarr is fully functional without it —
titles simply carry no dialogue line, which the evidence layer already treats
as "no data" rather than as a finding.

THE CONTRACT
------------
Two endpoints, bearer-authenticated::

    GET {base}/subs?anidb_id=…|anilist_id=…|tvdb_id=…[&ep=N][&lang=en]
        -> JSON list of candidates (diagnostics only; Curatarr rarely uses it)

    GET {base}/subs/best?anidb_id=…|anilist_id=…|tvdb_id=…[&ep=N][&lang=en]
        -> text/plain, the single best subtitle file (SRT or ASS)

``ep`` is optional and defaults to the first episode: Curatarr measures ONE
representative episode per series, so episode-exact resolution is a bonus,
never a requirement. The service is expected to prefer a full dialogue track
over signs/songs/karaoke tracks — Curatarr filters those itself as well, since
neither side can be sure the other got it right.

Failures are TRANSIENT by contract: an unreachable or unconfigured service
must never be recorded as "this title has no subtitles", or a weekend of
downtime would permanently blind the judge to part of the library.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT = 25.0

# A single episode's subtitles are tens of kilobytes; a feature's are under a
# few hundred. Measured against a real service, one response came back at
# 19.5 MB — whatever that file is, it is not one episode's dialogue, and
# parsing it would cost far more than it could ever be worth. Refused by
# Content-Length before the body is read, so the bandwidth is never spent.
_MAX_BYTES = 4_000_000


def _plain(v):
    return v.get_secret_value() if hasattr(v, "get_secret_value") else v


# Default path prefix. An operator may configure a bare host ("http://box:1234")
# or a full base including its own prefix; both must work, because getting this
# wrong is not a harmless mistake — see the HTML check in fetch_best.
_DEFAULT_PREFIX = "/api/v1"


def _conf() -> tuple:
    from src.config import settings
    base = str(_plain(getattr(settings, "SUBTITLE_PROVIDER_URL", "")) or "").rstrip("/")
    if base:
        from urllib.parse import urlsplit
        if not urlsplit(base).path.strip("/"):
            base += _DEFAULT_PREFIX
    return (base,
            str(_plain(getattr(settings, "SUBTITLE_PROVIDER_API_KEY", "")) or ""))


def configured() -> bool:
    """True when an operator has pointed this at a service of their own."""
    base, _key = _conf()
    return bool(base)


def _headers() -> dict:
    _base, key = _conf()
    h = {"Accept": "text/plain, application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


async def fetch_best(*, anidb_id=None, anilist_id=None, tvdb_id=None,
                     episode: Optional[int] = None, lang: str = "en"):
    """Best subtitle text for one title, or the module-wide tri-state.

      str  — the subtitle file's text,
      ""   — the service answered and genuinely has nothing,
      None — TRANSIENT: not configured, unreachable, auth or server error.

    Never raises: this sits in a warm-up loop that one bad title must not
    derail, and the caller distinguishes the three cases to decide whether a
    "checked" stamp is honest.
    """
    base, _key = _conf()
    if not base:
        return None
    params = {"lang": lang}
    for name, val in (("anidb_id", anidb_id), ("anilist_id", anilist_id),
                      ("tvdb_id", tvdb_id)):
        if val:
            params[name] = int(val)
            break                      # one id is enough; the service resolves
    if len(params) < 2:
        return ""                      # nothing to ask with — definitive
    if episode:
        params["ep"] = int(episode)

    import httpx
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT,
                                     follow_redirects=True) as c:
            # Streamed so an implausibly large file can be refused on its
            # headers rather than downloaded and then discarded.
            async with c.stream("GET", f"{base}/subs/best", headers=_headers(),
                                params=params) as resp:
                size = int(resp.headers.get("content-length") or 0)
                if size > _MAX_BYTES:
                    logger.info("[subtitle-provider] refusing a %.1f MB "
                                "response for %s — not one episode's dialogue",
                                size / 1e6, params)
                    return ""          # definitive: this is not usable data
                await resp.aread()
            r = resp
            # A 404 that returns an HTML error PAGE is a misconfigured base
            # URL, not an answer about this title. Treating it as "definitively
            # no subtitles" would stamp the whole library on the strength of a
            # typo — so anything that smells like a web page is transient, and
            # says so loudly enough to be found.
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" in ctype or r.text[:200].lstrip().lower().startswith(
                    ("<!doctype", "<html")):
                logger.warning("[subtitle-provider] %s returned an HTML page, "
                               "not subtitle data — is SUBTITLE_PROVIDER_URL "
                               "(%s) pointing at the right base path?",
                               r.status_code, base)
                return None
            if r.status_code in (404, 204):
                return ""              # asked and answered: nothing on file
            if r.status_code in (401, 403):
                logger.warning("[subtitle-provider] auth rejected (%s) — "
                               "check SUBTITLE_PROVIDER_API_KEY",
                               r.status_code)
                return None
            r.raise_for_status()
            txt = r.text or ""
            if len(txt) > _MAX_BYTES:      # server sent no Content-Length
                logger.info("[subtitle-provider] discarding a %.1f MB body "
                            "for %s", len(txt) / 1e6, params)
                return ""
            # A byte-order mark trips naive parsers; strip it defensively even
            # though a well-behaved service already does.
            if txt.startswith("﻿"):
                txt = txt[1:]
            if len(txt.strip()) < 200:
                return ""              # a stub is not a subtitle
            return txt
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return None
    except httpx.HTTPStatusError as e:
        return None if e.response.status_code >= 500 else ""
    except Exception as e:
        logger.debug("[subtitle-provider] request failed: %s", e)
        return None


def anime_ids_for(title: str, *, tmdb_id=None, tvdb_id=None) -> dict:
    """Dig the anime-specific ids out of the enrichment cache.

    Measured on this library's cached anime records: anilist_id is present for
    ~90%, tvdb_id ~79%, anidb_id only ~68% — so asking with whichever id we
    happen to have beats insisting on one. Returns ``{}`` when nothing is
    cached; the caller then simply has no question to ask.
    """
    out: dict = {}
    try:
        import json
        from src.cache.metadata_cache import MetadataCache
        cache = MetadataCache()
        try:
            cur = cache.conn.cursor()
            keys = [f"v2:raw:anime:{v}" for v in (tmdb_id, tvdb_id) if v]
            if title:
                keys.append(f"v2:raw:anime:{title[:40]}")
            for k in keys:
                cur.execute("SELECT response FROM api_cache WHERE cache_key = ?",
                            (k,))
                row = cur.fetchone()
                if not row or not row[0]:
                    continue
                d = json.loads(row[0])
                for f in ("anidb_id", "anilist_id", "tvdb_id"):
                    if d.get(f) and f not in out:
                        out[f] = d[f]
                if out:
                    break
        finally:
            cache.close()
    except Exception as e:
        logger.debug("[subtitle-provider] id lookup failed for %r: %s",
                     title, e)
    return out
