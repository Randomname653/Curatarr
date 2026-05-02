"""
ARR Suite LLM - Authentication Router

Implements Plex PIN-based OAuth flow:
  1. POST /api/auth/plex/pin  → get PIN from Plex
  2. Redirect user to https://app.plex.tv/auth?code=<code>
  3. Poll /api/auth/plex/poll/<pin_id> until Plex fills the token
  4. Exchange Plex auth token for local JWT session
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
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
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.effective_jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency – validates Bearer JWT and returns User."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    payload = _decode_jwt(auth[7:])
    user_id = int(payload.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Ensures the user is an admin by checking the database."""
    # Always verify from database to prevent token manipulation
    db_user = user  # Already validated by get_current_user
    if not db_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return db_user


def rate_limit_poll(func):
    """Decorator to rate limit polling requests."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        pin_id = kwargs.get('pin_id')
        if pin_id is None:
            return await func(*args, **kwargs)

        now = time.time()
        if pin_id not in poll_rate_limit:
            poll_rate_limit[pin_id] = []

        poll_rate_limit[pin_id] = [
            req_time for req_time in poll_rate_limit[pin_id]
            if now - req_time < 60
        ]

        if len(poll_rate_limit[pin_id]) >= MAX_POLLS_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Too many requests")

        poll_rate_limit[pin_id].append(now)
        return await func(*args, **kwargs)
    return wrapper


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
@rate_limit_poll
async def poll_plex_pin(pin_id: int, db: Session = Depends(get_db)):
    """Step 2 – Poll until the user has authenticated; returns JWT on success."""
    await asyncio.sleep(0.1 + (hash(str(pin_id)) % 100) / 1000)
    
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
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("New user: %s (admin=%s)", plex_username, first_ever)
    else:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account deactivated")
        user.last_login = datetime.now(timezone.utc)
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
    # In a real implementation, we would invalidate the token
    # For now, we just return a success response
    response.delete_cookie("access_token")
    return {"status": "logged_out"}
