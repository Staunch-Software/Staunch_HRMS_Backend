"""Field encryption at rest (Fernet), keyed hashing and display masking.

Keys come from settings.FIELD_ENCRYPTION_KEY: a comma-separated list of
Fernet keys, the first one primary. Older keys stay in the list only so
values written under them can still be read (MultiFernet).
"""
import hashlib
import hmac
import re
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

PREFIX = "enc:v1:"
_PREFIX_BYTES = PREFIX.encode("ascii")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


class EncryptionKeyError(RuntimeError):
    pass


def _key_list() -> tuple[str, ...]:
    keys = tuple(k.strip() for k in (settings.FIELD_ENCRYPTION_KEY or "").split(",") if k.strip())
    if not keys:
        raise EncryptionKeyError(
            "FIELD_ENCRYPTION_KEY is not set. Run `python scripts/generate_encryption_key.py`, "
            "set FIELD_ENCRYPTION_KEY=<key> in backend/.env and back the key up safely - "
            "without it every encrypted field and bank file is unreadable."
        )
    return keys


@lru_cache(maxsize=8)
def _build(keys: tuple[str, ...]) -> tuple[MultiFernet, bytes]:
    try:
        fernet = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyError(f"FIELD_ENCRYPTION_KEY contains an invalid Fernet key: {exc}") from exc
    hash_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"staunch-hrms-field-hash"
    ).derive(keys[0].encode("ascii"))
    return fernet, hash_key


def _fernet() -> MultiFernet:
    return _build(_key_list())[0]


def require_key() -> None:
    _build(_key_list())


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_str(value) -> Optional[str]:
    if value is None:
        return None
    value = value if isinstance(value, str) else str(value)
    if value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(value) -> Optional[str]:
    if value is None or not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionKeyError(
            "Could not decrypt a stored field - FIELD_ENCRYPTION_KEY does not contain the key it was "
            "encrypted with."
        ) from exc


def encrypt_bytes(data: bytes) -> bytes:
    if data.startswith(_PREFIX_BYTES):
        return data
    return _PREFIX_BYTES + _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypts bytes written by encrypt_bytes; anything else (a legacy
    plaintext file) is returned unchanged."""
    if not data.startswith(_PREFIX_BYTES):
        return data
    try:
        return _fernet().decrypt(data[len(_PREFIX_BYTES):])
    except InvalidToken as exc:
        raise EncryptionKeyError(
            "Could not decrypt a stored file - FIELD_ENCRYPTION_KEY does not contain the key it was "
            "encrypted with."
        ) from exc


def hmac_hash(value) -> Optional[str]:
    if value is None:
        return None
    hash_key = _build(_key_list())[1]
    return hmac.new(hash_key, str(value).strip().encode("utf-8"), hashlib.sha256).hexdigest()


def mask_tail(value, keep: int = 4) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) <= keep:
        return "X" * len(text)
    return "X" * (len(text) - keep) + text[-keep:]


def last_digits(value, keep: int = 4) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[-keep:] if len(text) > keep else None


def mask_aadhaar(value) -> Optional[str]:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) <= 4:
        return mask_tail(value)
    return f"XXXX XXXX {digits[-4:]}"


def mask_pan(value) -> Optional[str]:
    if value is None:
        return None
    return mask_tail(str(value).strip().upper())


def is_masked(value) -> bool:
    """True for a value produced by the masking helpers above. Real Aadhaar
    and account numbers are digits only; a real PAN may contain 'X' but then
    still matches the PAN format, which a masked PAN never does."""
    if not isinstance(value, str):
        return False
    text = value.strip().upper()
    return "X" in text and not _PAN_RE.match(text.replace(" ", ""))


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        return decrypt_str(value)
