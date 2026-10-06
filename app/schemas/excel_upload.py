from typing import Any, Optional

from pydantic import BaseModel

from app.schemas.payroll_run import PayrollRunRead


class RowPreview(BaseModel):
    row_number: int
    data: dict[str, Any]
    errors: list[str] = []
    warnings: list[str] = []


class UploadPreviewSummary(BaseModel):
    total_rows: int
    error_count: int
    warning_count: int


class UploadPreviewResponse(BaseModel):
    batch_id: str
    rows: list[RowPreview]
    summary: UploadPreviewSummary


class BatchCommitRequest(BaseModel):
    batch_id: str


class VariableInputsCommitResponse(BaseModel):
    batch_id: str
    committed_rows: int
    import_batch_id: str


class FinalSheetCommitResponse(BaseModel):
    committed_rows: int
    run: PayrollRunRead
