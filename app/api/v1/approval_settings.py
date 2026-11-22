"""Settings > Approval Workflow: approval chain per request type, the
no-manager fallback and payroll-run maker-checker."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_hr
from app.models.user import AdminUser
from app.schemas.approval_settings import ApprovalSettingsRead, ApprovalSettingsUpdate
from app.services import approval_settings as service
from app.services.leave import workflow

router = APIRouter(prefix="/settings/approvals", tags=["settings"], dependencies=[Depends(require_hr)])


@router.get("", response_model=ApprovalSettingsRead)
def get_approval_settings(db: Session = Depends(get_db)):
    return service.get_settings(db)


@router.put("", response_model=ApprovalSettingsRead)
def update_approval_settings(payload: ApprovalSettingsUpdate, db: Session = Depends(get_db),
                             user: AdminUser = Depends(require_hr)):
    """Saving a chain that now includes a manager (it was HR only) moves that
    type's PENDING_HR requests that skipped the manager to the manager."""
    current = service.get_settings(db)
    before = {kind: getattr(current, field) for kind, field in service.CHAIN_FIELDS.items()}
    try:
        row = service.update_settings(db, payload.model_dump(), user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for kind, field in service.CHAIN_FIELDS.items():
        if service.hr_only(before[kind]) and service.chain_includes_manager(getattr(row, field)):
            workflow.reroute_request_type(db, kind)
    db.commit()
    db.refresh(row)
    return row
