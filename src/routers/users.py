"""
ARR Suite LLM - User Management Router
Admin-only: activate/deactivate users and view list (no sensitive data).
Self-service: the account itself and notification preferences.

The "encryption passphrase" (PIN) endpoints are gone (2026-10-10): nothing
was ever encrypted with it, so it only suggested a protection that did not
exist while storing a hash a short PIN cannot survive offline.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.models import User
from src.routers.auth import get_current_user, require_admin
from src.schemas.user import UserCreate, UserUpdate, UserResponse

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 100,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(User).offset(skip).limit(limit).all()
    return [UserResponse.from_orm(u) for u in users]


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user: UserCreate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # ⚡ Bolt: Fast existence check avoiding full ORM object instantiation
    if db.query(User.id).filter(User.plex_user_id == user.plex_user_id).first() is not None:
        raise HTTPException(status_code=400, detail="User already exists")
    db_user = User(
        plex_user_id=user.plex_user_id,
        plex_username=user.plex_username,
        is_admin=False,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return UserResponse.from_orm(db_user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    update: UserUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    if update.is_active is not None:
        db_user.is_active = update.is_active
        if not update.is_active:
            # Every token this user holds dies now, not at its 7-day expiry.
            db_user.token_version = (db_user.token_version or 0) + 1
    db.commit()
    db.refresh(db_user)
    return UserResponse.from_orm(db_user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete (deactivate) a user.

    A hard ``DELETE`` would orphan FK-referencing rows (watch_history,
    taste_vectors, memories, etc.) since the schema has no
    ``ondelete="CASCADE"``. Soft-disabling preserves the data so the same
    Plex account can be re-enabled (or re-attributed) later.
    """
    if user_id == _admin.id:
        raise HTTPException(
            status_code=400,
            detail="Admins cannot delete themselves. Promote another user first.",
        )
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    if db_user.is_admin:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an admin account. Demote them first.",
        )
    db_user.is_active = False
    # Same as PATCH deactivation: every token this user holds dies now. The
    # is_active check alone would lapse the moment the account is re-enabled,
    # reviving whatever tokens were out there when it was deleted.
    db_user.token_version = (db_user.token_version or 0) + 1
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# SELF-SERVICE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return UserResponse.from_orm(user)


@router.get("/me/notification-preferences")
async def get_notification_preferences(user: User = Depends(get_current_user)):
    """Return the catalogue of proactive triggers + this user's enabled state.

    UI renders one toggle per item; ``enabled`` defaults to ``True`` for any
    trigger the user hasn't explicitly disabled.
    """
    from src.services.proactive_messages import TRIGGER_TYPES, get_disabled_triggers
    disabled = get_disabled_triggers(user.id)
    return {
        "triggers": [
            {**t, "enabled": t["type"] not in disabled}
            for t in TRIGGER_TYPES
        ],
    }


class NotificationPreferenceUpdate(BaseModel):
    trigger_type: str
    enabled: bool


@router.post("/me/notification-preferences")
async def set_notification_preference(
    body: NotificationPreferenceUpdate,
    user: User = Depends(get_current_user),
):
    """Toggle a single proactive-trigger on or off for the current user.

    The set of disabled triggers lives in app_state under the per-user key
    ``notif_disabled:user_id=<id>``. Returns the same structure as
    ``GET /me/notification-preferences`` so the UI can re-sync without a
    second round-trip.
    """
    from src.services.proactive_messages import (
        TRIGGER_TYPES, TRIGGER_TYPE_NAMES,
        get_disabled_triggers, set_disabled_triggers,
    )
    if body.trigger_type not in TRIGGER_TYPE_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown trigger_type: {body.trigger_type}")
    disabled = get_disabled_triggers(user.id)
    if body.enabled:
        disabled.discard(body.trigger_type)
    else:
        disabled.add(body.trigger_type)
    set_disabled_triggers(user.id, disabled)
    return {
        "triggers": [
            {**t, "enabled": t["type"] not in disabled}
            for t in TRIGGER_TYPES
        ],
    }
