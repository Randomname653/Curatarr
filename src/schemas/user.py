from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional


class UserBase(BaseModel):
    plex_user_id: str
    plex_username: str


class UserCreate(UserBase):
    pass


class UserUpdate(BaseModel):
    is_active: Optional[bool] = None


class UserResponse(UserBase):
    id: int
    is_admin: bool
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserPinSet(BaseModel):
    """Set or change the user's PIN.

    First-time set: send only ``pin``. Subsequent changes: send both
    ``current_pin`` and ``pin`` (the new one). Server verifies
    ``current_pin`` against the stored PBKDF2 hash before accepting.
    """
    pin: str
    current_pin: Optional[str] = None

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, v):
        if len(v) < 6:
            raise ValueError("PIN must be at least 6 characters")
        if len(v) > 128:
            raise ValueError("PIN must be at most 128 characters")
        return v


class UserPinStatus(BaseModel):
    has_pin: bool
    set_at: Optional[datetime] = None
