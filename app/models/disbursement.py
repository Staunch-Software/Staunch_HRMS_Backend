from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class DisbursementBatch(Base, TimestampMixin):
    """One generated bank disbursement file for an Approved payroll run.

    Persisted, not generate-on-demand: the actual file bytes are written
    once (see app/services/bank_file/generate.py) and never regenerated on
    download — `checksum` plus the stored file are how a re-download is
    guaranteed to return exactly what was originally submitted to the bank,
    even if employee bank details change afterwards.
    """
    __tablename__ = "disbursement_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payroll_run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), nullable=False, index=True)
    format_key: Mapped[str] = mapped_column(String(50), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Generated|Submitted|Confirmed|Failed|PartiallyFailed
    status: Mapped[str] = mapped_column(String(20), default="Generated", nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    total_employee_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    # Increments per regeneration for the same run + format_key, starting at 1.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    # Path to the stored .xlsx, relative to backend/ (e.g.
    # "generated_files/<file_name>") — see generate.py's module docstring for
    # why on-disk storage was chosen over a LargeBinary DB column.
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)

    # ICICI Composite Pay API submission state (only populated for
    # format_key="icici_bulk_salary_txt_v1" batches submitted via
    # POST .../icici/submit — see app/api/v1/icici_bulk.py). NULL for every
    # other batch, and for an icici_bulk_salary_txt_v1 batch that was only
    # generated/downloaded but never submitted through the API.
    icici_file_sequence_num: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    icici_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The raw MESSAGE_DESC from the Bulk Payment API response — distinguishes
    # "sent for corporate approval" (maker-checker still required in CIB)
    # from "file is uploaded successfully" (STP account).
    icici_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ICICI's Create OTP API does not return the OTP value — it's texted to
    # the registered mobile, so a human has to read it and submit it back.
    # icici_pending_unique_id holds the UNIQUE_ID from a Create-OTP call
    # that hasn't yet been completed with a matching Bulk Payment call
    # (cleared once that follow-up call succeeds or is abandoned).
    # icici_unique_id is the UNIQUE_ID that the eventually-successful Bulk
    # Payment call used — kept permanently, since Reverse MIS status checks
    # are keyed on the same UNIQUEID.
    icici_pending_unique_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    icici_unique_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    payroll_run = relationship("PayrollRun")
    items: Mapped[list["DisbursementBatchItem"]] = relationship(
        back_populates="disbursement_batch", cascade="all, delete-orphan"
    )


class DisbursementBatchItem(Base, TimestampMixin):
    __tablename__ = "disbursement_batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disbursement_batch_id: Mapped[int] = mapped_column(
        ForeignKey("disbursement_batches.id"), nullable=False, index=True
    )
    payroll_run_line_item_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_run_line_items.id"), nullable=False, index=True
    )
    # Snapshotted at generation time (from EmployeeBankAccount, as it was at
    # that moment) so a later bank-detail edit never silently changes what a
    # historical disbursement file said.
    employee_code: Mapped[str] = mapped_column(String(30), nullable=False)
    beneficiary_name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    ifsc_code: Mapped[str] = mapped_column(String(11), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(150), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="Pending", nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    disbursement_batch = relationship("DisbursementBatch", back_populates="items")
