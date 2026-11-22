from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EmployeeLoginRow(BaseModel):
    employee_id: int
    employee_code: str
    name: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    employee_status: Optional[str] = None
    has_login: bool = False
    user_id: Optional[int] = None
    username: Optional[str] = None
    role: Optional[str] = None
    login_enabled: Optional[bool] = None
    must_change_password: Optional[bool] = None
    last_login_at: Optional[datetime] = None


class BulkCreateLoginsRequest(BaseModel):
    employee_ids: list[int] = []
    all_active: bool = False


class TemporaryCredential(BaseModel):
    """Returned once, at creation/reset time. The password is never stored
    in plain text and cannot be retrieved again."""

    employee_id: int
    employee_code: str
    name: Optional[str] = None
    username: str
    temporary_password: str


class SkippedLogin(BaseModel):
    employee_id: int
    employee_code: Optional[str] = None
    name: Optional[str] = None
    reason: str


class BulkCreateLoginsResponse(BaseModel):
    created: list[TemporaryCredential]
    skipped: list[SkippedLogin]
