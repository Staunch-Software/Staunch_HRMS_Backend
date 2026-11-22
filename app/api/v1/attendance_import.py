"""Attendance uploads (daily corrections, monthly muster, site monthly days):
template -> preview (match rows, nothing written) -> commit."""
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.user import AdminUser
from app.services.attendance import imports
from app.services.attendance.import_templates import build_template
from app.services.attendance.imports import AttendanceImportError
from app.services.excel_import import batch_store

router = APIRouter(prefix="/attendance/import", tags=["attendance-import"], dependencies=[Depends(get_current_user)])


class RowDecision(BaseModel):
    row_number: int
    employee_id: Optional[int] = None
    skip: bool = False


class AttendanceImportCommitRequest(BaseModel):
    batch_id: str
    decisions: list[RowDecision] = []


def _kind(kind: str) -> str:
    try:
        return imports.parse_kind(kind)
    except AttendanceImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{kind}/template")
def download_template(kind: str, month: str = Query(..., description="YYYY-MM"), db: Session = Depends(get_db)):
    kind = _kind(kind)
    try:
        month_start, month_end = imports.parse_month(month)
    except AttendanceImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    content, filename = build_template(db, kind, month_start, month_end)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{kind}/preview")
async def preview_import(kind: str, file: UploadFile = File(...), month: str = Form(...),
                         db: Session = Depends(get_db)):
    kind = _kind(kind)
    file_bytes = await file.read()
    try:
        result = imports.preview(db, kind, file_bytes, month)
    except AttendanceImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    batch_rows = result.pop("_batch_rows")
    meta = {"kind": kind, "month_start": result["month_start"].isoformat(),
            "month_end": result["month_end"].isoformat(), "filename": file.filename}
    batch = batch_store.create(run_id=None, mode=imports.batch_mode(kind), raw_rows=batch_rows, meta=meta)
    return {"batch_id": batch.batch_id, **result}


@router.post("/{kind}/commit")
def commit_import(kind: str, payload: AttendanceImportCommitRequest, db: Session = Depends(get_db),
                  user: AdminUser = Depends(get_current_user)):
    kind = _kind(kind)
    batch = batch_store.get(payload.batch_id)
    if not batch or batch.mode != imports.batch_mode(kind):
        raise HTTPException(status_code=404,
                            detail="Upload batch not found or has expired - please upload the file again.")
    decisions = [d.model_dump() for d in payload.decisions]
    for d in decisions:
        if not d["skip"] and d["employee_id"] is None:
            raise HTTPException(status_code=400, detail=f"Decision for row {d['row_number']} needs employee_id or skip.")
    try:
        result = imports.commit(db, kind, payload.batch_id, batch.meta, batch.raw_rows, decisions, user.username)
    except AttendanceImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    batch_store.delete(payload.batch_id)
    return {"batch_id": payload.batch_id, **result}
