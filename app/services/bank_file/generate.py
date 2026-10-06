"""Orchestrates bank disbursement file generation for an Approved payroll
run: loads the run's line items, re-validates each employee's bank details
(defensively — Phase 3's Approve step only front-end-checks this), and on a
clean batch calls the registry's formatter and persists the result.

File storage choice: generated files are written to disk under
backend/generated_files/ rather than a LargeBinary DB column. Reasoning:
this project already runs against SQLite in dev and Postgres in
prod/live-data (see app/models/json_type.py's dialect-switching for the
same concern) — storing large binary blobs in SQLite rows is workable but
awkward for backups/inspection, and keeping generated files as plain
.xlsx files on disk means they can be opened directly for a spot-check
without going through the API. `DisbursementBatch.file_path` records where
the file lives and `checksum` is the guarantee that a re-download hasn't
silently changed: the file is written once and never rewritten.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.company_bank_config import CompanyBankConfig
from app.models.disbursement import DisbursementBatch, DisbursementBatchItem
from app.models.employee import EmployeeBankAccount
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.services.bank_file.registry import get_file_extension, get_formatter

# Matches the frontend's PaymentMethod.js validation regex exactly.
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

# backend/generated_files — this file lives at
# backend/app/services/bank_file/generate.py, so parents[3] is backend/.
GENERATED_FILES_DIR = Path(__file__).resolve().parents[3] / "generated_files"


@dataclass
class BankFileGenerationResult:
    success: bool = False
    batch: Optional[DisbursementBatch] = None
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _get_primary_bank_account(db: Session, employee_id: int) -> Optional[EmployeeBankAccount]:
    return (
        db.query(EmployeeBankAccount)
        .filter(EmployeeBankAccount.employee_id == employee_id, EmployeeBankAccount.is_active.is_(True))
        .order_by(EmployeeBankAccount.id.desc())
        .first()
    )


def _get_active_company_bank_config(db: Session) -> Optional[CompanyBankConfig]:
    return (
        db.query(CompanyBankConfig)
        .filter(CompanyBankConfig.is_active.is_(True))
        .order_by(CompanyBankConfig.id.desc())
        .first()
    )


def _next_version(db: Session, payroll_run_id: int, format_key: str) -> int:
    latest = (
        db.query(DisbursementBatch)
        .filter(DisbursementBatch.payroll_run_id == payroll_run_id, DisbursementBatch.format_key == format_key)
        .order_by(DisbursementBatch.version.desc())
        .first()
    )
    return (latest.version + 1) if latest else 1


def generate_bank_file(
    db: Session,
    payroll_run: PayrollRun,
    format_key: str = "generic_neft_xlsx_v1",
    generated_by: Optional[str] = None,
) -> BankFileGenerationResult:
    if payroll_run.status != "Approved":
        raise ValueError(
            f"Cannot generate a disbursement batch for a run in status '{payroll_run.status}' — "
            "it must be Approved first."
        )

    # Raises ValueError (-> 400 at the API layer) for an unknown format_key.
    formatter = get_formatter(format_key)

    company_config = _get_active_company_bank_config(db)
    if company_config is None:
        return BankFileGenerationResult(
            blocking_issues=[
                "No active company bank (debit account) config found — set one via "
                "PUT /company-bank-config before generating a disbursement file."
            ]
        )

    line_items = (
        db.query(PayrollRunLineItem)
        .filter(PayrollRunLineItem.payroll_run_id == payroll_run.id, PayrollRunLineItem.status == "Included")
        .order_by(PayrollRunLineItem.employee_id)
        .all()
    )

    blocking_issues: list[str] = []
    warnings: list[str] = []
    rows: list[dict] = []
    accounts_seen: dict[str, list[str]] = {}

    for line_item in line_items:
        snapshot = line_item.employee_snapshot or {}
        display_name = snapshot.get("name") or f"Employee #{line_item.employee_id}"
        employee_code = snapshot.get("employee_code") or str(line_item.employee_id)
        net_pay = Decimal(str(line_item.net_pay))

        if net_pay <= 0:
            blocking_issues.append(
                f"{display_name} ({employee_code}): net pay is {net_pay} — must be greater than 0."
            )
            continue

        # Fetched live (not from the frozen employee_snapshot) so a bank
        # detail change made after approval is caught here, defensively.
        bank = _get_primary_bank_account(db, line_item.employee_id)
        if bank is None:
            blocking_issues.append(f"{display_name} ({employee_code}): no active bank account on file.")
            continue
        if not bank.account_number:
            blocking_issues.append(f"{display_name} ({employee_code}): missing bank account number.")
            continue
        if not bank.ifsc_code or not IFSC_RE.match(bank.ifsc_code):
            blocking_issues.append(
                f"{display_name} ({employee_code}): invalid IFSC code '{bank.ifsc_code}'."
            )
            continue

        accounts_seen.setdefault(bank.account_number, []).append(display_name)

        rows.append(
            {
                "employee_code": employee_code,
                # NOT the employee display name — see generic_neft_xlsx_v1's
                # docstring on why this must be the bank's account holder name.
                "beneficiary_name": bank.account_holder_name,
                "account_number": bank.account_number,
                "ifsc_code": bank.ifsc_code,
                "bank_name": bank.bank_name,
                "amount": net_pay,
                "narration": None,
                "line_item_id": line_item.id,
            }
        )

    # Duplicate account numbers are a WARNING, not a blocker — a shared
    # family bank account can be a legitimate arrangement.
    for account_number, names in accounts_seen.items():
        if len(names) > 1:
            warnings.append(f"Account number {account_number} is shared by: {', '.join(names)} (not blocking).")

    if blocking_issues:
        # Refuse generation entirely — no partial file, nothing persisted.
        return BankFileGenerationResult(blocking_issues=blocking_issues, warnings=warnings)

    if not rows:
        return BankFileGenerationResult(blocking_issues=["No eligible line items to disburse for this run."])

    period_yyyymm = payroll_run.period_start.strftime("%Y%m")
    generated_at = datetime.now(timezone.utc)
    version = _next_version(db, payroll_run.id, format_key)

    file_extension = get_file_extension(format_key)
    file_name = (
        f"{company_config.company_code}_SALARY_{format_key}_{period_yyyymm}_"
        f"{payroll_run.id}_{generated_at.strftime('%Y%m%d%H%M%S')}_v{version}{file_extension}"
    )

    run_metadata = {
        "period_yyyymm": period_yyyymm,
        "pay_date": payroll_run.pay_date.isoformat() if payroll_run.pay_date else None,
        "run_id": payroll_run.id,
    }
    debit_account = {
        "debit_account_number": company_config.debit_account_number,
        "debit_account_bank_name": company_config.debit_account_bank_name,
        # Stand-in remitter/payee display name for the ICICI bulk format's
        # PAYEE_NAME_DR field — company_code is the only free-text company
        # identifier this schema currently holds. Confirm with the ICICI RM
        # whether this should instead be the company's full registered CIB
        # name before relying on it for a real file.
        "debit_account_company_name": company_config.company_code,
    }

    file_bytes = formatter(rows, debit_account, run_metadata)
    checksum = hashlib.sha256(file_bytes).hexdigest()

    GENERATED_FILES_DIR.mkdir(parents=True, exist_ok=True)
    absolute_path = GENERATED_FILES_DIR / file_name
    absolute_path.write_bytes(file_bytes)
    # Stored relative to backend/ so it stays portable across machines/envs.
    relative_path = str(absolute_path.relative_to(GENERATED_FILES_DIR.parent))

    total_amount = sum((row["amount"] for row in rows), Decimal("0"))

    batch = DisbursementBatch(
        payroll_run_id=payroll_run.id,
        format_key=format_key,
        file_name=file_name,
        status="Generated",
        total_amount=float(total_amount),
        total_employee_count=len(rows),
        generated_at=generated_at,
        generated_by=generated_by,
        version=version,
        checksum=checksum,
        file_path=relative_path,
    )
    db.add(batch)
    db.flush()

    for row in rows:
        db.add(
            DisbursementBatchItem(
                disbursement_batch_id=batch.id,
                payroll_run_line_item_id=row["line_item_id"],
                employee_code=row["employee_code"],
                beneficiary_name=row["beneficiary_name"],
                account_number=row["account_number"],
                ifsc_code=row["ifsc_code"],
                bank_name=row["bank_name"],
                amount=float(row["amount"]),
                status="Pending",
                failure_reason=None,
            )
        )

    db.commit()
    db.refresh(batch)

    return BankFileGenerationResult(success=True, batch=batch, warnings=warnings)
