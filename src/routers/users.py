"""
ARR Suite LLM - User Management Router
Admin-only: activate/deactivate users and view list (no sensitive data).
Self-service: PIN management.
"""

import hashlib
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.models import User, UserPinHash
from src.routers.auth import get_current_user, require_admin
from src.schemas.user import UserCreate, UserUpdate, UserResponse, UserPinSet

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
    if db.query(User).filter(User.plex_user_id == user.plex_user_id).first():
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
    db.commit()
    db.refresh(db_user)
    return UserResponse.from_orm(db_user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(db_user)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# SELF-SERVICE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    return UserResponse.from_orm(user)


@router.post("/me/pin")
async def set_pin(
    body: UserPinSet,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Derive and store PBKDF2 hash of user PIN.
    The PIN is used client-side to derive AES-256 encryption keys – it is
    never used to log in.  We store only a hash so we can verify later that
    the PIN hasn't changed under us.
    """
    salt = os.urandom(32).hex()
    pin_hash = hashlib.pbkdf2_hmac(
        "sha256", body.pin.encode(), salt.encode(), 100_000
    ).hex()

    existing = db.query(UserPinHash).filter(UserPinHash.user_id == user.id).first()
    if existing:
        existing.pin_hash = pin_hash
        existing.salt = salt
        existing.last_updated = datetime.utcnow()
    else:
        db.add(UserPinHash(user_id=user.id, pin_hash=pin_hash, salt=salt))
    db.commit()
    return {"status": "pin_set"}
