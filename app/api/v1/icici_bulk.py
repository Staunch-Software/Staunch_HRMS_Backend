"""ICICI Composite Pay API integration endpoints for an
`icici_bulk_salary_txt_v1` disbursement batch: config-status (for the
frontend to decide whether to show the submit button as enabled), submit
(Create OTP + Bulk Payment), and status (Reverse MIS).

NOT USABLE FOR REAL TRANSACTIONS until ICICI_CORP_ID/ICICI_USER_ID/
ICICI_AGGR_ID/ICICI_AGGR_NAME/ICICI_URN/ICICI_API_KEY are set — every
endpoint here returns 503 with instructions instead of attempting a
network call if any is missing (see app/core/config.py).

Two-step submit flow (see icici_api/client.py's module docstring and the
project report for why): ICICI's Create OTP API does not return the OTP
value — it's texted to the registered mobile number, so a human has to
read it and submit it back. `POST .../icici/submit` with no `otp` in the
body triggers the OTP SMS and returns `otp_requested: true`; call it again
within 15 minutes with `otp` set to actually submit the bulk payment file.
"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_current_user
from app.models.disbursement import DisbursementBatch
from app.models.payroll_run import PayrollRun
from app.schemas.icici_bulk import (
    ICICIConfigStatus,
    ICICIStatusResponse,
    ICICISubmitRequest,
    ICICISubmitResponse,
)
from app.services.bank_file.generate import GENERATED_FILES_DIR
from app.services.bank_file.icici_bulk_salary_txt_v1 import encode_base64
from app.services.icici_api.client import ICICIClient, missing_config_fields
from app.services.icici_api.exceptions import (
    ICICIBulkPaymentError,
    ICICIConfigurationError,
    ICICIOTPError,
    ICICIStatusCheckError,
)

router = APIRouter(tags=["icici-bulk"], dependencies=[Depends(get_current_user)])

ICICI_FORMAT_KEY = "icici_bulk_salary_txt_v1"

CREDENTIALS_HINT = (
    "ICICI API credentials not configured — obtain CORP_ID/USER_ID/AGGR_ID/AGGR_NAME/URN from your "
    "ICICI relationship manager and set them in backend/.env."
)


def _get_batch_or_404(db: Session, run_id: int, batch_id: int) -> DisbursementBatch:
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    batch = db.get(DisbursementBatch, batch_id)
    if not batch or batch.payroll_run_id != run_id:
        raise HTTPException(status_code=404, detail="Disbursement batch not found for this payroll run")
    if batch.format_key != ICICI_FORMAT_KEY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This batch was generated with format_key='{batch.format_key}', not "
                f"'{ICICI_FORMAT_KEY}' — only an ICICI-format batch can be submitted to the ICICI API."
            ),
        )
    return batch


def _require_configured() -> None:
    if missing_config_fields(settings):
        raise HTTPException(status_code=503, detail=CREDENTIALS_HINT)


@router.get("/icici/config-status", response_model=ICICIConfigStatus)
def get_icici_config_status():
    """Lets the frontend decide whether to show "Submit to ICICI" as
    enabled or greyed-out-with-a-tooltip, without guessing at credentials
    client-side."""
    missing = missing_config_fields(settings)
    return ICICIConfigStatus(
        configured=not missing,
        missing_fields=missing,
        encryption_enabled=settings.ICICI_ENCRYPTION_ENABLED,
        base_url=settings.ICICI_BASE_URL,
    )


@router.post(
    "/payroll-runs/{run_id}/disbursement-batches/{batch_id}/icici/submit",
    response_model=ICICISubmitResponse,
)
def submit_disbursement_batch_to_icici(
    run_id: int, batch_id: int, payload: ICICISubmitRequest, db: Session = Depends(get_db)
):
    batch = _get_batch_or_404(db, run_id, batch_id)
    _require_configured()

    client = ICICIClient()
    try:
        if not payload.otp:
            # Step 1: trigger the OTP SMS. A fresh UNIQUE_ID per OTP
            # request — ICICI rejects a reused one (see error 999936).
            unique_id = f"BATCH{batch.id}-{uuid4().hex[:10].upper()}"
            try:
                client.create_otp(unique_id)
            except ICICIConfigurationError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ICICIOTPError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            batch.icici_pending_unique_id = unique_id
            db.commit()
            return ICICISubmitResponse(
                otp_requested=True,
                unique_id=unique_id,
                message=(
                    "OTP sent to the registered mobile number. It is valid for 15 minutes — call this "
                    "endpoint again with that OTP to submit the bulk payment file."
                ),
            )

        # Step 2: the human has the OTP — submit the actual file.
        if not batch.icici_pending_unique_id:
            raise HTTPException(
                status_code=400,
                detail="No pending OTP request for this batch — call this endpoint without an OTP first.",
            )
        unique_id = batch.icici_pending_unique_id

        file_path: Path = GENERATED_FILES_DIR.parent / batch.file_path
        if not file_path.exists():
            raise HTTPException(status_code=410, detail="The generated file is no longer available on disk.")
        file_content_base64 = encode_base64(file_path.read_bytes())

        try:
            result = client.bulk_payment(
                unique_id=unique_id,
                otp=payload.otp,
                file_name=batch.file_name,
                file_content_base64=file_content_base64,
                file_description=payload.file_description or f"Staunch salary disbursement batch {batch.id}",
            )
        except ICICIConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ICICIBulkPaymentError as exc:
            # A rejected file does not clear icici_pending_unique_id — the
            # same OTP/UNIQUE_ID pair may still be retried within its
            # 15-minute validity window.
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        batch.icici_file_sequence_num = str(result.get("FILE_SEQUENCE_NUM")) if result.get("FILE_SEQUENCE_NUM") else None
        batch.icici_unique_id = unique_id
        batch.icici_pending_unique_id = None
        batch.icici_submitted_at = datetime.now(timezone.utc)
        batch.icici_message = result.get("MESSAGE_DESC")
        batch.status = "Submitted"
        db.commit()

        return ICICISubmitResponse(
            otp_requested=False,
            unique_id=unique_id,
            file_sequence_num=batch.icici_file_sequence_num,
            message=batch.icici_message,
            requires_manual_approval=result.get("requires_manual_approval"),
        )
    finally:
        client.close()


@router.get(
    "/payroll-runs/{run_id}/disbursement-batches/{batch_id}/icici/status",
    response_model=ICICIStatusResponse,
)
def get_disbursement_batch_icici_status(run_id: int, batch_id: int, db: Session = Depends(get_db)):
    batch = _get_batch_or_404(db, run_id, batch_id)
    if not batch.icici_file_sequence_num or not batch.icici_unique_id:
        raise HTTPException(
            status_code=400,
            detail="This batch has not been successfully submitted to ICICI yet — nothing to check.",
        )
    _require_configured()

    client = ICICIClient()
    try:
        try:
            result = client.reverse_mis(unique_id=batch.icici_unique_id, file_seq_num=batch.icici_file_sequence_num)
        except ICICIConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ICICIStatusCheckError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()

    file_status = result["file_status"]
    return ICICIStatusResponse(
        file_sequence_num=result.get("file_sequence_num") and str(result["file_sequence_num"]),
        file_status_code=file_status["code"],
        file_status_description=file_status["description"],
        file_status_outcome=file_status["outcome"],
        transactions=result.get("transactions", []),
    )
