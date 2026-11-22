from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup, PayGroupDefinitionVersion
from app.models.user import AdminUser
from app.schemas.pay_group import (
    DefinitionValidateRequest,
    EmployeePayProfileRead,
    EmployeePayProfileUpsert,
    PayGroupCreate,
    PayGroupDuplicate,
    PayGroupRead,
    PayGroupStatusUpdate,
    PayGroupTestRequest,
    PayGroupUpdate,
)
from app.services.payroll_engine.formula import admin
from app.services.payroll_engine.formula.compiled import validate_definition
from app.services.payroll_engine.formula.history import converted_definition, save_definition
from app.services.payroll_engine.formula.reference import formula_reference
from app.services.payroll_engine.formula.tester import TestInputError, month_bounds, run_test
from app.services.payroll_engine.pay_group_runner import resolve_pay_profile
from app.services.payroll_settings import rounding_defaults
from app.services.payroll_engine.pay_groups import (
    FORMULA,
    PayGroupParamsError,
    get_strategy,
    param_spec,
    validate_profile_for_group,
)

router = APIRouter(tags=["pay-groups"], dependencies=[Depends(get_current_user)])


def _group_read(group: PayGroup, member_count: int = 0, detail: bool = False) -> PayGroupRead:
    formula = group.calc_model == FORMULA
    try:
        spec = {} if formula else param_spec(group.calc_model)
    except PayGroupParamsError:
        spec = {}
    params = admin.effective_params(group)
    view = admin.definition_view(group.definition) if formula else {}
    return PayGroupRead(
        id=group.id, code=group.code, name=group.name, calc_model=group.calc_model, params=params,
        description=group.description, is_active=group.is_active, param_spec=spec, member_count=member_count,
        employees_count=member_count, version=group.definition_version or 0,
        attendance_source=params.get("attendance_source"), tds_enabled=params.get("tds_enabled", True),
        net_rounding=params.get("net_rounding"), definition=group.definition if detail and formula else None,
        **view, created_at=group.created_at, updated_at=group.updated_at,
    )


def _read(db: Session, group: PayGroup, detail: bool = True) -> PayGroupRead:
    return _group_read(group, admin.member_counts(db, date.today()).get(group.id, 0), detail)


def _get_group(db: Session, group_id: int) -> PayGroup:
    group = db.get(PayGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Pay group not found")
    return group


def _definition_copy(group: PayGroup) -> dict:
    """The group's definition without its version (a legacy strategy group's
    params are converted)."""
    if group.calc_model == FORMULA:
        return {k: v for k, v in (group.definition or {}).items() if k != "version"}
    try:
        return converted_definition(group.calc_model, group.params or {}, group.code)
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail=f"Pay group {group.code} has no formula definition") from None


def _admin_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except admin.AdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


@router.get("/pay-groups/formula-reference")
def get_formula_reference(db: Session = Depends(get_db)):
    return {**formula_reference(), "rounding_defaults": rounding_defaults(db)}


@router.post("/pay-groups/validate")
def validate_pay_group_definition(payload: DefinitionValidateRequest):
    result = validate_definition(payload.definition)
    return {"valid": result.ok, "errors": result.errors, "warnings": result.warnings}


@router.get("/pay-groups", response_model=list[PayGroupRead])
def list_pay_groups(db: Session = Depends(get_db)):
    counts = admin.member_counts(db, date.today())
    groups = db.query(PayGroup).order_by(PayGroup.id).all()
    return [_group_read(g, counts.get(g.id, 0)) for g in groups]


@router.post("/pay-groups", response_model=PayGroupRead, status_code=status.HTTP_201_CREATED)
def create_pay_group(payload: PayGroupCreate, db: Session = Depends(get_db),
                     user: AdminUser = Depends(get_current_user)):
    if payload.template is not None:
        template = _get_group(db, payload.template)
        definition = _definition_copy(template)
        note = payload.note or f"Created from template {template.code} v{template.definition_version}"
    elif payload.definition is not None:
        definition, note = payload.definition, payload.note or "Created"
    else:
        definition, note = admin.starter_definition(db), payload.note or "Created from the starter definition"
    group = _admin_call(admin.create_group, db, payload.code, payload.name, payload.description, definition,
                        user.username, note, payload.is_active)
    db.commit()
    db.refresh(group)
    return _read(db, group)


@router.get("/pay-groups/{group_id}", response_model=PayGroupRead)
def get_pay_group(group_id: int, db: Session = Depends(get_db)):
    return _read(db, _get_group(db, group_id))


@router.put("/pay-groups/{group_id}", response_model=PayGroupRead)
def update_pay_group(group_id: int, payload: PayGroupUpdate, db: Session = Depends(get_db),
                     user: AdminUser = Depends(get_current_user)):
    group = _get_group(db, group_id)
    data = payload.model_dump(exclude_unset=True)
    if payload.expected_version is not None and payload.expected_version != (group.definition_version or 0):
        raise HTTPException(status_code=409, detail=(
            f"Pay group {group.code} is now at version {group.definition_version}; reload before saving"))
    if "name" in data and not (data["name"] or "").strip():
        raise HTTPException(status_code=400, detail="name cannot be blank")
    if data.get("params") is not None and group.calc_model == FORMULA:
        raise HTTPException(status_code=400, detail=(
            f"Pay group {group.code} is calculated by formulas: edit its definition instead of params"))
    if "params" in data:
        if data["params"] is None:
            raise HTTPException(status_code=400, detail="params cannot be null")
        errors = get_strategy(group.calc_model).validate_params(data["params"])
        if errors:
            raise HTTPException(status_code=400, detail=(
                f"Invalid params for pay group {group.code} ({group.calc_model}): " + "; ".join(errors)))
        group.params = data["params"]
    if payload.definition is not None:
        result = validate_definition(payload.definition)
        if not result.ok:
            raise HTTPException(status_code=400, detail={
                "message": "The definition has errors", "errors": result.errors, "warnings": result.warnings})
        save_definition(db, group, payload.definition, user.username, payload.note)
    for key in ("name", "description"):
        if key in data:
            setattr(group, key, data[key].strip() if key == "name" else data[key])
    if data.get("is_active") is not None:
        group.is_active = data["is_active"]
    db.commit()
    db.refresh(group)
    return _read(db, group)


@router.post("/pay-groups/{group_id}/duplicate", response_model=PayGroupRead, status_code=status.HTTP_201_CREATED)
def duplicate_pay_group(group_id: int, payload: PayGroupDuplicate, db: Session = Depends(get_db),
                        user: AdminUser = Depends(get_current_user)):
    source = _get_group(db, group_id)
    definition = _definition_copy(source)
    note = payload.note or f"Duplicated from {source.code} v{source.definition_version}"
    description = payload.description if payload.description is not None else source.description
    group = _admin_call(admin.create_group, db, payload.code, payload.name, description, definition,
                        user.username, note)
    db.commit()
    db.refresh(group)
    return _read(db, group)


@router.patch("/pay-groups/{group_id}/status", response_model=PayGroupRead)
def set_pay_group_status(group_id: int, payload: PayGroupStatusUpdate, db: Session = Depends(get_db)):
    group = _get_group(db, group_id)
    if not payload.is_active and group.is_active and not payload.force:
        count = admin.open_profile_count(db, group.id, date.today())
        if count:
            raise HTTPException(status_code=409, detail={
                "message": (f"{count} employee(s) have pay profiles in {group.code} in force now or later; move "
                            "them to another group first, or deactivate with force=true"),
                "employees_count": count,
            })
    group.is_active = payload.is_active
    db.commit()
    db.refresh(group)
    return _read(db, group)


def _version_row(row: PayGroupDefinitionVersion, with_definition: bool) -> dict:
    out = {"version": row.version, "changed_by": row.changed_by, "changed_at": row.changed_at, "note": row.note,
           "lines_count": len((row.definition or {}).get("lines", []))}
    if with_definition:
        out["definition"] = row.definition
    return out


@router.get("/pay-groups/{group_id}/versions")
def list_pay_group_versions(group_id: int, include_definition: bool = False, db: Session = Depends(get_db)):
    _get_group(db, group_id)
    rows = (db.query(PayGroupDefinitionVersion).filter(PayGroupDefinitionVersion.pay_group_id == group_id)
            .order_by(PayGroupDefinitionVersion.version.desc()).all())
    return [_version_row(r, include_definition) for r in rows]


@router.get("/pay-groups/{group_id}/versions/{version}")
def get_pay_group_version(group_id: int, version: int, db: Session = Depends(get_db)):
    row = (db.query(PayGroupDefinitionVersion)
           .filter(PayGroupDefinitionVersion.pay_group_id == group_id, PayGroupDefinitionVersion.version == version)
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return _version_row(row, True)


@router.post("/pay-groups/{group_id}/test")
def test_pay_group(group_id: int, payload: PayGroupTestRequest, db: Session = Depends(get_db)):
    """Calculates one employee/profile with a line-by-line trace. Uses
    payload.definition (unsaved edits) when given, else the saved definition."""
    group = _get_group(db, group_id)
    definition = payload.definition if payload.definition is not None else group.definition
    if definition is None:
        raise HTTPException(status_code=400, detail=f"Pay group {group.code} has no formula definition")
    employee = stored = None
    try:
        if payload.employee_id is not None:
            employee = _require_employee(db, payload.employee_id)
            stored = resolve_pay_profile(db, employee.id, month_bounds(payload.month)[1])
        out = run_test(db, definition, employee, stored, payload.profile, payload.inputs, payload.month,
                       payload.include_tds)
    except TestInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    out["definition_version"] = None if payload.definition is not None else group.definition_version
    out["profile_source"] = ("pay_profile" if stored is not None else "none") + (
        "+overrides" if payload.profile else "")
    return out


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
    never edited in place, so past runs stay reproducible. For a FORMULA group
    the employee custom fields (extra) are checked against the definition and a
    full month is test-calculated: any error is reported."""
    _require_employee(db, employee_id)
    group = db.get(PayGroup, payload.pay_group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Pay group not found")
    if not group.is_active:
        raise HTTPException(status_code=400, detail=f"Pay group {group.code} is inactive")

    new_profile = EmployeePayProfile(employee_id=employee_id, effective_to=None, **payload.model_dump())
    errors = validate_profile_for_group(group, new_profile)
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
