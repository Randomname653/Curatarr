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
    pin: str

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, v):
        if len(v) < 6:
            raise ValueError("PIN must be at least 6 characters")
        return v
