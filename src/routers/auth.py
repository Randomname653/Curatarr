"""
ARR Suite LLM - Authentication Router

Implements Plex PIN-based OAuth flow:
  1. POST /api/auth/plex/pin  → get PIN from Plex
  2. Redirect user to https://app.plex.tv/auth?code=<code>
  3. Poll /api/auth/plex/poll/<pin_id> until Plex fills the token
  4. Exchange Plex auth token for local JWT session
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
import jwt
from jwt import PyJWTError
from sqlalchemy.orm import Session

from src.config import settings
from src.database import get_db
from src.database.models import User

logger = logging.getLogger(__name__)
router = APIRouter()

PLEX_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    "X-Plex-Product": "Curatarr",
    "X-Plex-Version": "1.0",
}

# Simple in-memory rate limiting (in production, use Redis or similar)
poll_rate_limit = {}
MAX_POLLS_PER_MINUTE = 5


def _create_jwt(user_id: int, is_admin: bool) -> str:
    payload = {
        "sub": str(user_id),
        "admin": is_admin,
        # 7 days, not 24h: the old value logged the owner out mid-click
        # exactly one day after each login (observed live: a 200 and a 401
        # six seconds apart on the same connection). Paired with the
        # rolling-refresh middleware, a session now lives as long as it is
        # USED and dies after a week idle — the right shape for a trusted-
        # LAN household app.
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.effective_jwt_secret, algorithms=["HS256"])
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency – validates Bearer JWT and returns User."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = _decode_jwt(auth[7:])
    sub = payload.get("sub")
    try:
        user_id = int(sub) if sub is not None else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    if user_id <= 0:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Ensures the user is an admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _no_admin_exists(db: Session) -> bool:
    return db.query(User).filter(User.is_admin == True).count() == 0


def require_admin_or_first_run(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Allow access if (a) no admin exists yet (first-run / onboarding), OR
    (b) the caller is an authenticated admin.

    Used by the setup wizard: any client can drive setup BEFORE the first
    admin account is established, but post-setup the same endpoints become
    admin-only (no SSRF or .env-overwrite by anonymous callers).
    """
    if _no_admin_exists(db):
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = _decode_jwt(auth[7:])
    sub = payload.get("sub")
    try:
        user_id = int(sub) if sub is not None else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _enforce_poll_rate_limit(pin_id: int) -> None:
    """In-process rate limiter for ``GET /plex/poll/{pin_id}``.

    Sliding 60s window, ``MAX_POLLS_PER_MINUTE`` polls per ``pin_id``. Trims
    expired entries on every call so the dict stays bounded by active PINs.
    """
    now = time.time()
    bucket = [t for t in poll_rate_limit.get(pin_id, []) if now - t < 60]
    if len(bucket) >= MAX_POLLS_PER_MINUTE:
        poll_rate_limit[pin_id] = bucket
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now)
    poll_rate_limit[pin_id] = bucket
    # GC: drop entries whose newest request is older than the window
    for stale_id in [k for k, v in poll_rate_limit.items() if v and now - v[-1] >= 60]:
        poll_rate_limit.pop(stale_id, None)

    # Cap the dictionary at 1000 entries to prevent memory exhaustion DoS
    if len(poll_rate_limit) > 1000:
        # Sort by the most recent timestamp in the bucket, drop the oldest half
        sorted_items = sorted(
            poll_rate_limit.items(),
            key=lambda item: item[1][-1] if item[1] else 0
        )
        for k, _ in sorted_items[:500]:
            poll_rate_limit.pop(k, None)


async def _bootstrap_user_data(user_id: int) -> None:
    """Auto-onboard a freshly-arrived user — no admin button required.

    When a second person logs in, their Plex plays live under their own Plex
    account (and, for plays in an old/removed library section, carry no
    ratingKey at all — re-attribution can't touch them). This pulls their
    history straight from Plex by their account, resolves each title against
    the shared enrichment knowledge base, and writes their watch_history
    rows, then builds their taste vector — so the moment they land they get
    *their* data. Best-effort and idempotent; each step logs and swallows its
    own errors so one failing doesn't block the other. Runs in the background
    after the login response returns.
    """
    try:
        from src.services.plex_sync import import_plex_history_for_user
        res = await import_plex_history_for_user(user_id)
        logger.info("[auto-onboard] history import for user %s: %s", user_id, res)
    except Exception as e:
        logger.warning("[auto-onboard] history import failed for user %s: %s", user_id, e)
    try:
        from src.services.taste_engine import compute_all_taste_vectors
        await compute_all_taste_vectors(user_id)
        logger.info("[auto-onboard] taste vectors built for user %s", user_id)
    except Exception as e:
        logger.warning("[auto-onboard] taste compute failed for user %s: %s", user_id, e)


# ─────────────────────────────────────────────────────────────────────────────
# PLEX PIN OAUTH
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/plex/pin")
async def request_plex_pin():
    """Step 1 – Request a fresh PIN from Plex."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://plex.tv/api/v2/pins",
            headers=PLEX_HEADERS,
            params={"strong": "true"},
            timeout=10,
        )
    if resp.status_code != 201:
        raise HTTPException(status_code=502, detail=f"Plex PIN request failed: {resp.text}")

    data = resp.json()
    pin_id = data["id"]
    code = data["code"]
    auth_url = (
        f"https://app.plex.tv/auth#?clientID={settings.PLEX_CLIENT_ID}"
        f"&code={code}&forwardUrl={settings.PLEX_REDIRECT_URI}"
    )
    return {"pin_id": pin_id, "code": code, "auth_url": auth_url}


@router.get("/plex/poll/{pin_id}")
async def poll_plex_pin(pin_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Step 2 – Poll until the user has authenticated; returns JWT on success."""
    _enforce_poll_rate_limit(pin_id)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers=PLEX_HEADERS,
            timeout=10,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Plex poll failed")

    data = resp.json()
    auth_token = data.get("authToken")
    if not auth_token:
        return {"status": "pending"}

    # Fetch Plex account details
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://plex.tv/api/v2/user",
            headers={**PLEX_HEADERS, "X-Plex-Token": auth_token},
            timeout=10,
        )
    if user_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch Plex user info")

    plex_user = user_resp.json()
    plex_id = str(plex_user.get("id", ""))
    plex_username = plex_user.get("username") or plex_user.get("email", "unknown")

    # Upsert user
    user = db.query(User).filter(User.plex_user_id == plex_id).first()
    if not user:
        first_ever = db.query(User).first() is None
        user = User(
            plex_user_id=plex_id,
            plex_username=plex_username,
            is_admin=first_ever,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            # Store the user's OWN plex.tv token — per-account Plex writes
            # ("Curatarr Recommended" playlists) are impossible with the
            # owner token alone. Every login re-stores the newest token,
            # which self-heals tokens revoked via plex.tv sign-out.
            plex_token=auth_token,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("New user: %s (admin=%s)", plex_username, first_ever)
        # A secondary user just arrived → set up their data automatically:
        # re-attribute their Plex plays off the admin + build their taste
        # vector in the background. No admin button, no manual step. (The
        # first-ever user is the admin / data source, so they're skipped.)
        if not first_ever:
            background_tasks.add_task(_bootstrap_user_data, user.id)
    else:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account deactivated")
        user.last_login = datetime.now(timezone.utc)
        user.plex_token = auth_token   # refresh on every login (see above)
        db.commit()

    token = _create_jwt(user.id, user.is_admin)
    return {
        "status": "ok",
        "token": token,
        "user": {"id": user.id, "username": user.plex_username, "is_admin": user.is_admin},
    }


@router.get("/status")
async def auth_status(user: User = Depends(get_current_user)):
    return {
        "authenticated": True,
        "user_id": user.id,
        "username": user.plex_username,
        "is_admin": user.is_admin,
    }


@router.post("/logout")
async def logout(response: Response):
    """Client-side logout signal.

    JWTs are stateless and remain valid until they expire (24h). The frontend
    is expected to drop the token from localStorage when this is called. We
    also clear any legacy access_token cookie. A real server-side revocation
    list is intentionally out of scope for the current threat model
    (single-tenant home use).
    """
    response.delete_cookie("access_token")
    return {"status": "logged_out"}
