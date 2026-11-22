"""Approval workflow settings: read with defaults, update, and the chain
for a request type. The routing that uses them is in
services/leave/workflow.py."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.approval_settings import (
    CHAIN_HR_ONLY,
    CHAIN_MANAGER_HR,
    CHAIN_MANAGER_ONLY,
    CHAINS,
    FALLBACK_HR,
    FALLBACK_REPORTING_MANAGER,
    FALLBACKS,
    ApprovalSettings,
)

REQUEST_LEAVE = "LEAVE"
REQUEST_PERMISSION = "PERMISSION"
REQUEST_WFH = "WFH"
REQUEST_REGULARIZATION = "REGULARIZATION"
REQUEST_TYPES = (REQUEST_LEAVE, REQUEST_PERMISSION, REQUEST_WFH, REQUEST_REGULARIZATION)

CHAIN_FIELDS = {
    REQUEST_LEAVE: "leave_chain",
    REQUEST_PERMISSION: "permission_chain",
    REQUEST_WFH: "wfh_chain",
    REQUEST_REGULARIZATION: "regularization_chain",
}


def _defaults() -> ApprovalSettings:
    return ApprovalSettings(
        leave_chain=CHAIN_MANAGER_HR, permission_chain=CHAIN_MANAGER_HR, wfh_chain=CHAIN_MANAGER_HR,
        regularization_chain=CHAIN_MANAGER_HR, no_manager_fallback=FALLBACK_HR, payroll_maker_checker=False,
    )


def get_settings(db: Session) -> ApprovalSettings:
    """The saved row, or unsaved defaults when HR has never saved one."""
    row = db.query(ApprovalSettings).order_by(ApprovalSettings.id).first()
    return row if row is not None else _defaults()


def update_settings(db: Session, values: dict, user_id: Optional[int] = None) -> ApprovalSettings:
    row = db.query(ApprovalSettings).order_by(ApprovalSettings.id).first()
    if row is None:
        row = _defaults()
        db.add(row)
    for field, value in values.items():
        if field in CHAIN_FIELDS.values() and value not in CHAINS:
            raise ValueError(f"Unknown approval chain '{value}'.")
        if field == "no_manager_fallback" and value not in FALLBACKS:
            raise ValueError(f"Unknown fallback '{value}'.")
        setattr(row, field, value)
    row.updated_by = user_id
    db.flush()
    return row


def chain_for(db: Session, request_type: str) -> str:
    field = CHAIN_FIELDS.get(request_type)
    if field is None:
        raise ValueError(f"Unknown request type '{request_type}'.")
    return getattr(get_settings(db), field) or CHAIN_MANAGER_HR


def chain_includes_manager(chain: str) -> bool:
    return chain in (CHAIN_MANAGER_HR, CHAIN_MANAGER_ONLY)


def manager_is_final(chain: str) -> bool:
    return chain == CHAIN_MANAGER_ONLY


def hr_only(chain: str) -> bool:
    return chain == CHAIN_HR_ONLY


def maker_checker_enabled(db: Session) -> bool:
    return bool(get_settings(db).payroll_maker_checker)
