"""Monthly payroll workbook import: upload -> preview (match rows to
employees, nothing written) -> commit (pay profiles, variable inputs, one
Draft run per pay group)."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.services.excel_import import batch_store
from app.services.payroll_import import service
from app.services.payroll_import.service import PayrollImportError

router = APIRouter(tags=["payroll-import"], dependencies=[Depends(get_current_user)])

BATCH_MODE = "payroll-excel"


class RowDecision(BaseModel):
    sheet: str
    row_number: int
    employee_id: Optional[int] = None
    skip: bool = False


class PayrollImportCommitRequest(BaseModel):
    batch_id: str
    decisions: list[RowDecision] = []


@router.post("/payroll/import/excel/preview")
async def preview_payroll_import(file: UploadFile = File(...), period: str = Form(...),
                                 db: Session = Depends(get_db)):
    file_bytes = await file.read()
    try:
        result = service.preview(db, file_bytes, period)
    except PayrollImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    batch_rows = result.pop("_batch_rows")
    meta = {"period_start": result["period_start"].isoformat(), "period_end": result["period_end"].isoformat()}
    batch = batch_store.create(run_id=None, mode=BATCH_MODE, raw_rows=batch_rows, meta=meta)
    return {"batch_id": batch.batch_id, **result}


@router.post("/payroll/import/excel/commit")
def commit_payroll_import(payload: PayrollImportCommitRequest, db: Session = Depends(get_db)):
    batch = batch_store.get(payload.batch_id)
    if not batch or batch.mode != BATCH_MODE:
        raise HTTPException(status_code=404,
                            detail="Import batch not found or has expired - please upload the file again.")
    decisions = [d.model_dump() for d in payload.decisions]
    for d in decisions:
        if not d["skip"] and d["employee_id"] is None:
            raise HTTPException(status_code=400,
                                detail=f"Decision for {d['sheet']!r} row {d['row_number']} needs employee_id or skip.")
    try:
        result = service.commit(db, payload.batch_id, batch.meta, batch.raw_rows, decisions)
    except PayrollImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    batch_store.delete(payload.batch_id)
    return {"batch_id": payload.batch_id, **result}
