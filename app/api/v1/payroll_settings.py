"""HR settings: Rounding Rules and Payslip Settings (company profile, logo,
template, fields shown, PDF password, release rule)."""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_hr
from app.models.user import AdminUser
from app.schemas.payroll_settings import (
    ApplyNetRoundingRequest,
    ApplyNetRoundingResult,
    PayslipSettingsFields,
    PayslipSettingsRead,
    RoundingSettingsFields,
    RoundingSettingsRead,
)
from app.services.payroll_settings import (
    apply_net_rounding,
    ensure_payslip_settings,
    ensure_rounding_settings,
    pay_group_roundings,
    payslip_settings,
)
from app.services.payslip.data import sample_payslip
from app.services.payslip.render import (
    LOGO_MIMES,
    MAX_LOGO_BYTES,
    PayslipRenderError,
    logo_bytes,
    logo_data_uri,
    render_pdf,
)

router = APIRouter(prefix="/settings", tags=["settings"])

IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def _rounding_read(db: Session, row) -> RoundingSettingsRead:
    data = RoundingSettingsFields.model_validate(row, from_attributes=True).model_dump()
    return RoundingSettingsRead(**data, updated_at=row.updated_at, pay_groups=pay_group_roundings(db))


def _payslip_read(row) -> PayslipSettingsRead:
    data = PayslipSettingsFields.model_validate(row, from_attributes=True).model_dump()
    return PayslipSettingsRead(**data, has_custom_logo=bool(row.logo), updated_at=row.updated_at)


@router.get("/rounding", response_model=RoundingSettingsRead)
def get_rounding(db: Session = Depends(get_db)):
    row = ensure_rounding_settings(db)
    db.commit()
    return _rounding_read(db, row)


@router.put("/rounding", response_model=RoundingSettingsRead)
def update_rounding(payload: RoundingSettingsFields, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    row = ensure_rounding_settings(db)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_by = user.id
    db.commit()
    db.refresh(row)
    return _rounding_read(db, row)


@router.post("/rounding/apply-net-rounding", response_model=ApplyNetRoundingResult)
def apply_net_rounding_to_groups(
    payload: Optional[ApplyNetRoundingRequest] = None, db: Session = Depends(get_db),
    user: AdminUser = Depends(require_hr),
):
    mode = (payload.net_rounding if payload else None) or ensure_rounding_settings(db).default_net_rounding
    try:
        result = apply_net_rounding(db, mode, user.username)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return ApplyNetRoundingResult(net_rounding=mode, **result)


@router.get("/payslip", response_model=PayslipSettingsRead)
def get_payslip_settings(db: Session = Depends(get_db)):
    row = ensure_payslip_settings(db)
    db.commit()
    return _payslip_read(row)


@router.put("/payslip", response_model=PayslipSettingsRead)
def update_payslip_settings(
    payload: PayslipSettingsFields, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    row = ensure_payslip_settings(db)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_by = user.id
    db.commit()
    db.refresh(row)
    return _payslip_read(row)


@router.post("/payslip/logo", response_model=PayslipSettingsRead)
async def upload_logo(
    file: UploadFile = File(...), db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=400, detail="The logo must be 1 MB or smaller.")
    mime = next((m for sig, m in IMAGE_SIGNATURES.items() if data.startswith(sig)), None)
    if mime is None or mime not in LOGO_MIMES:
        raise HTTPException(status_code=400, detail="Upload a PNG, JPEG or GIF image.")
    row = ensure_payslip_settings(db)
    row.logo, row.logo_mime, row.updated_by = data, mime, user.id
    db.commit()
    db.refresh(row)
    return _payslip_read(row)


@router.delete("/payslip/logo", response_model=PayslipSettingsRead)
def reset_logo(db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    row = ensure_payslip_settings(db)
    row.logo, row.logo_mime, row.updated_by = None, None, user.id
    db.commit()
    db.refresh(row)
    return _payslip_read(row)


@router.get("/payslip/logo")
def get_logo(db: Session = Depends(get_db)):
    data, mime = logo_bytes(payslip_settings(db))
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/payslip/preview")
def preview_payslip(
    template: Optional[str] = Query(None, pattern="^(CLASSIC|MODERN|COMPACT)$"), db: Session = Depends(get_db)
):
    settings = payslip_settings(db)
    try:
        pdf = render_pdf(sample_payslip(settings, logo_data_uri(settings)), template or settings.template)
    except PayslipRenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    name = f"Payslip_preview_{(template or settings.template).lower()}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})
