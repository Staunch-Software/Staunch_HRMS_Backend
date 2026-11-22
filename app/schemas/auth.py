from datetime import datetime
from typing import Optional

from pydantic import BaseModel

MIN_PASSWORD_LENGTH = 8


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Optional[str] = None
    must_change_password: bool = False


class CurrentUserResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    role: str
    employee_id: Optional[int] = None
    employee_name: Optional[str] = None
    employee_code: Optional[str] = None
    has_reports: bool = False
    must_change_password: bool = False
    last_login_at: Optional[datetime] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
