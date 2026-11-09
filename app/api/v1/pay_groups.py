from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.schemas.pay_group import (
    EmployeePayProfileRead,
    EmployeePayProfileUpsert,
    PayGroupRead,
    PayGroupUpdate,
)
from app.services.payroll_engine.pay_group_runner import resolve_pay_profile
from app.services.payroll_engine.pay_groups import PayGroupParamsError, get_strategy, param_spec

router = APIRouter(tags=["pay-groups"], dependencies=[Depends(get_current_user)])


def _member_counts(db: Session, as_of: date) -> dict[int, int]:
    rows = (
        db.query(EmployeePayProfile.pay_group_id, func.count(func.distinct(EmployeePayProfile.employee_id)))
        .filter(
            EmployeePayProfile.effective_from <= as_of,
            (EmployeePayProfile.effective_to.is_(None)) | (EmployeePayProfile.effective_to >= as_of),
        )
        .group_by(EmployeePayProfile.pay_group_id)
        .all()
    )
    return dict(rows)


def _group_read(group: PayGroup, member_count: int = 0) -> PayGroupRead:
    try:
        spec = param_spec(group.calc_model)
    except PayGroupParamsError:
        spec = {}
    return PayGroupRead(
        id=group.id, code=group.code, name=group.name, calc_model=group.calc_model, params=group.params or {},
        description=group.description, is_active=group.is_active, param_spec=spec, member_count=member_count,
        created_at=group.created_at, updated_at=group.updated_at,
    )


@router.get("/pay-groups", response_model=list[PayGroupRead])
def list_pay_groups(db: Session = Depends(get_db)):
    counts = _member_counts(db, date.today())
    groups = db.query(PayGroup).order_by(PayGroup.id).all()
    return [_group_read(g, counts.get(g.id, 0)) for g in groups]


@router.get("/pay-groups/{group_id}", response_model=PayGroupRead)
def get_pay_group(group_id: int, db: Session = Depends(get_db)):
    group = db.get(PayGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Pay group not found")
    return _group_read(group, _member_counts(db, date.today()).get(group.id, 0))


@router.put("/pay-groups/{group_id}", response_model=PayGroupRead)
def update_pay_group(group_id: int, payload: PayGroupUpdate, db: Session = Depends(get_db)):
    group = db.get(PayGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Pay group not found")
    data = payload.model_dump(exclude_unset=True)
    if "params" in data:
        if data["params"] is None:
            raise HTTPException(status_code=400, detail="params cannot be null")
        errors = get_strategy(group.calc_model).validate_params(data["params"])
        if errors:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid params for pay group {group.code} ({group.calc_model}): " + "; ".join(errors),
            )
    if "name" in data and not (data["name"] or "").strip():
        raise HTTPException(status_code=400, detail="name cannot be blank")
    for key, value in data.items():
        if key == "is_active" and value is None:
            continue
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return _group_read(group, _member_counts(db, date.today()).get(group.id, 0))


def _profile_read(profile: EmployeePayProfile) -> EmployeePayProfileRead:
    group = profile.pay_group

    def f(value):
        return float(value) if value is not None else None

    return EmployeePayProfileRead(
        id=profile.id, employee_id=profile.employee_id, pay_group_id=profile.pay_group_id,
        pay_group_code=group.code if group else None, pay_group_name=group.name if group else None,
        calc_model=group.calc_model if group else None,
        monthly_salary=f(profile.monthly_salary), daily_wage=f(profile.daily_wage), skill_level=profile.skill_level,
        pf_applicable=profile.pf_applicable, esi_applicable=profile.esi_applicable, pf_cap=f(profile.pf_cap),
        fixed_monthly_amount=f(profile.fixed_monthly_amount), extra=profile.extra or {},
        effective_from=profile.effective_from, effective_to=profile.effective_to,
        created_at=profile.created_at, updated_at=profile.updated_at,
    )


def _require_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


@router.get("/employees/{employee_id}/pay-profile", response_model=EmployeePayProfileRead)
def get_current_pay_profile(employee_id: int, as_of: date | None = None, db: Session = Depends(get_db)):
    _require_employee(db, employee_id)
    profile = resolve_pay_profile(db, employee_id, as_of or date.today())
    if not profile:
        raise HTTPException(status_code=404, detail="No pay profile in force for this employee")
    return _profile_read(profile)


@router.get("/employees/{employee_id}/pay-profiles", response_model=list[EmployeePayProfileRead])
def list_pay_profiles(employee_id: int, db: Session = Depends(get_db)):
    _require_employee(db, employee_id)
    profiles = (
        db.query(EmployeePayProfile)
        .filter(EmployeePayProfile.employee_id == employee_id)
        .order_by(EmployeePayProfile.effective_from.desc(), EmployeePayProfile.id.desc())
        .all()
    )
    return [_profile_read(p) for p in profiles]


@router.put("/employees/{employee_id}/pay-profile", response_model=EmployeePayProfileRead)
def set_pay_profile(employee_id: int, payload: EmployeePayProfileUpsert, db: Session = Depends(get_db)):
    """Creates a new effective-dated profile. The profile currently open (no
    effective_to) is closed the day before the new one starts; history is
    never edited in place, so past runs stay reproducible."""
    _require_employee(db, employee_id)
    group = db.get(PayGroup, payload.pay_group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Pay group not found")
    if not group.is_active:
        raise HTTPException(status_code=400, detail=f"Pay group {group.code} is inactive")

    new_profile = EmployeePayProfile(employee_id=employee_id, effective_to=None, **payload.model_dump())
    errors = get_strategy(group.calc_model).validate_profile(new_profile, group.params or {})
    if errors:
        raise HTTPException(status_code=400, detail=f"Invalid pay profile for {group.code}: " + "; ".join(errors))

    open_profiles = (
        db.query(EmployeePayProfile)
        .filter(EmployeePayProfile.employee_id == employee_id, EmployeePayProfile.effective_to.is_(None))
        .all()
    )
    for previous in open_profiles:
        if payload.effective_from < previous.effective_from:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"effective_from {payload.effective_from} is before the current profile's "
                    f"effective_from {previous.effective_from}"
                ),
            )
        # Same-day replacement leaves the old row as an empty range (kept for audit).
        previous.effective_to = payload.effective_from - timedelta(days=1)

    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)
    return _profile_read(new_profile)
