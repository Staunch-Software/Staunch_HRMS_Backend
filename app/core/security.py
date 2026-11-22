import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models.user import ROLE_EMPLOYEE, ROLE_HR, AdminUser

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_PREFIX}/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    subject: str, expires_delta: Optional[timedelta] = None, extra_claims: Optional[dict] = None
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {**(extra_claims or {}), "sub": subject, "exp": expire}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> str:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except JWTError:
        raise credentials_exception


TEMP_PASSWORD_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in "0O1lI"
)


def generate_temporary_password(length: int = 10) -> str:
    while True:
        password = "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in password) and any(c.isalpha() for c in password):
            return password


def login_block_reason(user: AdminUser) -> Optional[str]:
    """Why an otherwise valid login must be refused: an employee login whose
    employee record is gone or no longer Active. HR logins without an
    employee link are never blocked here."""
    if user.employee_id is None:
        return "This login is not linked to an employee. Contact HR." if user.role == ROLE_EMPLOYEE else None
    employee = user.employee
    if employee is None or (employee.status or "").strip().lower() != "active":
        return "Your employee record is inactive. Contact HR."
    return None


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> AdminUser:
    username = decode_access_token(token)
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if user is None or not user.is_active or login_block_reason(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _require_password_current(user: AdminUser) -> None:
    if user.must_change_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password change required")


def require_hr(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    if user.role != ROLE_HR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="HR access required")
    _require_password_current(user)
    return user


def require_employee_user(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    """Any login linked to an employee record (EMPLOYEE role, or an HR user
    who is also an employee)."""
    if user.employee_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employee access required")
    _require_password_current(user)
    return user
