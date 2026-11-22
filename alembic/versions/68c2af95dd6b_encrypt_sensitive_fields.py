"""encrypt sensitive fields at rest

Widens employees.aadhaar_number / pan_number, employee_bank_accounts.account_number,
disbursement_batch_items.account_number and company_bank_config.debit_account_number
to Text and Fernet-encrypts existing values in place (app/core/crypto.py). Replaces
the full account number in payroll_run_line_items.employee_snapshot with
account_number_masked + account_hash.

Needs FIELD_ENCRYPTION_KEY set (both directions). Downgrade decrypts the columns and
restores a snapshot's account_number only where its hash still matches one of that
employee's bank accounts; otherwise it is left null.

Revision ID: 68c2af95dd6b
Revises: 908b0564cacd
Create Date: 2026-11-15 00:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core import crypto
from app.models.json_type import JSONType


revision: str = "68c2af95dd6b"
down_revision: Union[str, None] = "908b0564cacd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, original String length, nullable)
COLUMNS = [
    ("employees", "aadhaar_number", 20, True),
    ("employees", "pan_number", 20, True),
    ("employee_bank_accounts", "account_number", 40, False),
    ("disbursement_batch_items", "account_number", 40, False),
    ("company_bank_config", "debit_account_number", 40, False),
]


def _require_key() -> None:
    try:
        crypto.require_key()
    except crypto.EncryptionKeyError as exc:
        raise RuntimeError(
            f"Migration {revision} cannot run without a valid FIELD_ENCRYPTION_KEY. {exc}"
        ) from exc


def _rewrite_column(conn, table: str, column: str, transform) -> None:
    rows = conn.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
    update = sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id")
    for row_id, value in rows:
        new_value = transform(value)
        if new_value != value:
            conn.execute(update, {"value": new_value, "id": row_id})


def _snapshots(conn):
    query = sa.text("SELECT id, employee_id, employee_snapshot FROM payroll_run_line_items").columns(
        employee_snapshot=JSONType()
    )
    for row_id, employee_id, snapshot in conn.execute(query).fetchall():
        if isinstance(snapshot, (str, bytes)):
            snapshot = json.loads(snapshot)
        if isinstance(snapshot, dict):
            yield row_id, employee_id, snapshot


def _save_snapshot(conn, row_id: int, snapshot: dict) -> None:
    update = sa.text("UPDATE payroll_run_line_items SET employee_snapshot = :snapshot WHERE id = :id").bindparams(
        sa.bindparam("snapshot", type_=JSONType())
    )
    conn.execute(update, {"snapshot": snapshot, "id": row_id})


def upgrade() -> None:
    _require_key()
    conn = op.get_bind()

    for table, column, length, nullable in COLUMNS:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column, existing_type=sa.String(length=length), type_=sa.Text(), existing_nullable=nullable
            )

    for table, column, _length, _nullable in COLUMNS:
        _rewrite_column(conn, table, column, crypto.encrypt_str)

    for row_id, _employee_id, snapshot in list(_snapshots(conn)):
        if "account_number" not in snapshot:
            continue
        account = crypto.decrypt_str(snapshot.pop("account_number"))
        snapshot["account_number_masked"] = crypto.mask_tail(account) if account else None
        snapshot["account_hash"] = crypto.hmac_hash(account) if account else None
        _save_snapshot(conn, row_id, snapshot)


def downgrade() -> None:
    _require_key()
    conn = op.get_bind()

    accounts_by_hash: dict[tuple[int, str], str] = {}
    for employee_id, account in conn.execute(
        sa.text("SELECT employee_id, account_number FROM employee_bank_accounts WHERE account_number IS NOT NULL")
    ).fetchall():
        plain = crypto.decrypt_str(account)
        accounts_by_hash[(employee_id, crypto.hmac_hash(plain))] = plain

    for row_id, employee_id, snapshot in list(_snapshots(conn)):
        if "account_hash" not in snapshot and "account_number_masked" not in snapshot:
            continue
        account_hash = snapshot.pop("account_hash", None)
        snapshot.pop("account_number_masked", None)
        snapshot["account_number"] = accounts_by_hash.get((employee_id, account_hash)) if account_hash else None
        _save_snapshot(conn, row_id, snapshot)

    for table, column, _length, _nullable in COLUMNS:
        _rewrite_column(conn, table, column, crypto.decrypt_str)

    for table, column, length, nullable in COLUMNS:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column, existing_type=sa.Text(), type_=sa.String(length=length), existing_nullable=nullable
            )
