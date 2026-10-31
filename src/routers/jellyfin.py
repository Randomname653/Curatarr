"""
Curatarr - Jellyfin routes (docs/JELLYFIN_DESIGN.md §4).

  POST /api/jellyfin/webhook          the Webhook plugin's posts (history tier B);
                                      the shared secret is the only credential
  GET  /api/jellyfin/webhook/setup    admin: URL, header, secret and template to
                                      paste into the plugin (creates the secret)
  POST /api/jellyfin/webhook/rotate   admin: a new secret
  GET  /api/jellyfin/history-sources  which history witnesses this install has

Every route answers 404 on a Plex install.
"""
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.database.models import User
from src.routers.auth import get_current_user, require_admin
from src.services import rate_limit
from src.services.media_server import jellyfin_hook as hook

logger = logging.getLogger(__name__)
router = APIRouter()

HOOK_POSTS_PER_MINUTE = 120


def _require_jellyfin() -> None:
    from src.services.media_server import active_kind
    if active_kind() != "jellyfin":
        raise HTTPException(status_code=404, detail="Not a Jellyfin install")


@router.post("/webhook")
async def webhook(request: Request):
    """Unauthenticated by design - the plugin has no Curatarr account - so:
    shared secret (constant-time), a body cap, a per-client budget, and a
    payload that only ever triggers a re-read (services/media_server/jellyfin_hook)."""
    _require_jellyfin()
    client = (request.client.host if request.client else "") or "?"
    rate_limit.enforce("jellyfin-hook", client, HOOK_POSTS_PER_MINUTE, 60, detail="Too many requests")
    secret = await hook.current_secret()
    given = request.headers.get(hook.HEADER, "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Wrong or missing webhook secret")
    try:
        if int(request.headers.get("content-length") or 0) > hook.MAX_BODY:
            raise HTTPException(status_code=413, detail="Body too large")
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad Content-Length")
    body = await request.body()
    if len(body) > hook.MAX_BODY:
        raise HTTPException(status_code=413, detail="Body too large")
    try:
        import json
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body is not a JSON object")
    try:
        return await hook.handle(payload)
    except Exception as e:  # noqa: BLE001 - the plugin retries on 5xx; the cause stays in our log
        logger.warning("[jellyfin] webhook not handled: %s", e)
        raise HTTPException(status_code=503, detail="Jellyfin could not be re-read - try again")


@router.get("/webhook/setup")
async def webhook_setup(request: Request, _admin: User = Depends(require_admin)):
    """What to paste into the plugin (Dashboard -> Plugins -> Webhook ->
    Add Generic Destination). Creates the secret on first look."""
    _require_jellyfin()
    secret = await hook.current_secret(create=True)
    return {"path": "/api/jellyfin/webhook", "header": hook.HEADER, "secret": secret,
            "events": list(hook.EVENTS), "template": hook.TEMPLATE}


@router.post("/webhook/rotate")
async def webhook_rotate(_admin: User = Depends(require_admin)):
    _require_jellyfin()
    return {"secret": await hook.rotate_secret()}


@router.get("/history-sources")
async def history_sources(_user: User = Depends(get_current_user)):
    """For the history badge: live sessions always; the webhook when a post
    arrived in the last week; Playback Reporting when a backfill found it."""
    _require_jellyfin()
    from src.services.media_server.jellyfin_history import history_sources as sources
    return await sources()
