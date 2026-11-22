import json

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import settings
from app.models.employee import Employee, EmployeeBankAccount
from app.services.payroll_engine.engine import _employee_snapshot


def test_round_trip_and_prefix():
    token = crypto.encrypt_str("123456789012")
    assert token.startswith("enc:v1:")
    assert "123456789012" not in token
    assert crypto.decrypt_str(token) == "123456789012"
    assert crypto.encrypt_str("123456789012") != token  # non-deterministic
    assert crypto.encrypt_str(token) == token  # never double-encrypted
    assert crypto.encrypt_str(None) is None and crypto.decrypt_str(None) is None


def test_legacy_plaintext_is_returned_unchanged():
    assert crypto.decrypt_str("ABCDE1234F") == "ABCDE1234F"
    assert crypto.decrypt_bytes(b"PK\x03\x04legacy") == b"PK\x03\x04legacy"


def test_bytes_round_trip():
    data = b"FHR|1|some bank file|123456789012\n"
    stored = crypto.encrypt_bytes(data)
    assert stored.startswith(b"enc:v1:") and b"123456789012" not in stored
    assert crypto.decrypt_bytes(stored) == data


def test_masking():
    assert crypto.mask_tail("123456781234") == "XXXXXXXX1234"
    assert crypto.mask_tail("1234") == "XXXX"
    assert crypto.mask_tail(None) is None
    assert crypto.mask_aadhaar("111122223333") == "XXXX XXXX 3333"
    assert crypto.mask_aadhaar("1111 2222 3333") == "XXXX XXXX 3333"
    assert crypto.mask_pan("abcde1234f") == "XXXXXX234F"
    assert crypto.last_digits("123456781234") == "1234"
    assert crypto.last_digits("1234") is None


def test_is_masked():
    assert crypto.is_masked("XXXXXXXX1234")
    assert crypto.is_masked("XXXX XXXX 3333")
    assert crypto.is_masked("XXXXXX234F")
    assert not crypto.is_masked("123456781234")
    assert not crypto.is_masked("AXXPX1234X")  # a real PAN may contain X
    assert not crypto.is_masked(None)


def test_hmac_hash_is_stable_and_keyed(monkeypatch):
    first = crypto.hmac_hash("123456789012")
    assert first == crypto.hmac_hash("123456789012")
    assert first != crypto.hmac_hash("123456789013")
    assert len(first) == 64
    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert crypto.hmac_hash("123456789012") != first


def test_multifernet_rotation(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", old_key)
    old_token = crypto.encrypt_str("ABCDE1234F")

    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", f"{new_key},{old_key}")
    assert crypto.decrypt_str(old_token) == "ABCDE1234F"
    new_token = crypto.encrypt_str("ABCDE1234F")

    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", new_key)
    assert crypto.decrypt_str(new_token) == "ABCDE1234F"
    with pytest.raises(crypto.EncryptionKeyError):
        crypto.decrypt_str(old_token)


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", "")
    with pytest.raises(crypto.EncryptionKeyError, match="generate_encryption_key.py"):
        crypto.encrypt_str("123")
    assert crypto.decrypt_str("legacy") == "legacy"


def test_snapshot_has_no_full_account_number(db_session):
    employee = Employee(employee_code="E-SNAP", first_name="Snap", status="Active")
    db_session.add(employee)
    db_session.commit()
    db_session.add(EmployeeBankAccount(
        employee_id=employee.id, bank_name="SBI", account_holder_name="Snap",
        account_number="123456789012", ifsc_code="SBIN0001234",
    ))
    db_session.commit()

    snapshot = _employee_snapshot(db_session, employee)
    assert "account_number" not in snapshot
    assert "123456789012" not in json.dumps(snapshot)
    assert snapshot["account_number_masked"] == "XXXXXXXX9012"
    assert snapshot["account_hash"] == crypto.hmac_hash("123456789012")
    assert snapshot["ifsc_code"] == "SBIN0001234" and snapshot["bank_name"] == "SBI"
