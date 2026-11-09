"""Employee master Excel import: upload -> preview -> commit.

Preview parses the whole workbook (all expected sheets combined, de-duplicated
by normalized employee code with sheet precedence), compares it with the
database without writing anything, and stores the parsed records in the
short-lived batch store. Commit re-plans that batch against the current
database state and applies it in one transaction.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.services.employee_import import service
from app.services.employee_import.workbook import read_master_workbook
from app.services.excel_import import batch_store
from app.services.excel_import.excel_reader import ExcelParseError

router = APIRouter(tags=["employee-import"], dependencies=[Depends(get_current_user)])

BATCH_MODE = "employee-master"


class MasterImportCommitRequest(BaseModel):
    batch_id: str


@router.post("/employees/import/master/preview")
async def preview_master_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_bytes = await file.read()
    try:
        parsed = read_master_workbook(file_bytes)
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = service.preview(db, parsed)
    batch = batch_store.create(run_id=None, mode=BATCH_MODE, raw_rows=service.records_to_batch(parsed.records))
    return {"batch_id": batch.batch_id, **result}


@router.post("/employees/import/master/commit")
def commit_master_import(payload: MasterImportCommitRequest, db: Session = Depends(get_db)):
    batch = batch_store.get(payload.batch_id)
    if not batch or batch.mode != BATCH_MODE:
        raise HTTPException(
            status_code=404,
            detail="Import batch not found or has expired - please upload the file again.",
        )
    result = service.commit(db, service.records_from_batch(batch.raw_rows))
    batch_store.delete(payload.batch_id)
    return {"batch_id": payload.batch_id, **result}
