from pydantic import BaseModel
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
