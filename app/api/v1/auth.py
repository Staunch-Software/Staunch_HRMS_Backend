from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    login_block_reason,
    verify_password,
)
from app.models.user import ROLE_EMPLOYEE, AdminUser
from app.schemas.auth import (
    MIN_PASSWORD_LENGTH,
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
)
from app.services.employee_code import normalize_employee_code
from app.services.reporting import employee_full_name, has_team

router = APIRouter(tags=["auth"])


def _find_login_user(db: Session, username: str) -> Optional[AdminUser]:
    """Exact username first; employee logins (username = employee code) also
    match case-insensitively and in any code spelling ("stemp5", "ST-EMP-05")."""
    username = (username or "").strip()
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if user is not None:
        return user
    candidates = {username.upper(), normalize_employee_code(username)} - {""}
    if not candidates:
        return None
    return (
        db.query(AdminUser)
        .filter(AdminUser.username.in_(candidates), AdminUser.role == ROLE_EMPLOYEE)
        .first()
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = _find_login_user(db, payload.username)
    if not user or not user.is_active or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    reason = login_block_reason(user)
    if reason:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    token = create_access_token(subject=user.username, extra_claims={"role": user.role})
    return TokenResponse(access_token=token, role=user.role, must_change_password=user.must_change_password)


@router.get("/auth/me", response_model=CurrentUserResponse)
def read_current_user(current_user: AdminUser = Depends(get_current_user), db: Session = Depends(get_db)):
    employee = current_user.employee
    return CurrentUserResponse(
        id=current_user.id,
        username=current_user.username,
        is_active=current_user.is_active,
        role=current_user.role,
        employee_id=current_user.employee_id,
        employee_name=employee_full_name(employee),
        employee_code=employee.employee_code if employee else None,
        has_reports=bool(employee) and has_team(db, employee.id),
        must_change_password=current_user.must_change_password,
        last_login_at=current_user.last_login_at,
    )


@router.post("/auth/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: AdminUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    new_password = payload.new_password
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(new_password) > 128:
        raise HTTPException(status_code=400, detail="New password is too long.")
    if new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="New password must be different from the current password.")
    current_user.hashed_password = hash_password(new_password)
    current_user.must_change_password = False
    db.commit()
    return {"success": True}
