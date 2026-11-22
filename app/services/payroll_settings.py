"""Rounding rules and payslip settings: reading with defaults, the rounding
helpers the payroll / TDS / attendance code calls, and "apply net rounding
to every pay group" (a new definition version per group)."""
import copy
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.orm import Session

from app.models.pay_group import PayGroup
from app.models.payroll_settings import (
    PAYSLIP_DEFAULTS,
    ROUNDING_DEFAULTS,
    TDS_NEAREST_10,
    PayrollRoundingSettings,
    PayslipSettings,
)

ONE = Decimal("1")
TEN = Decimal("10")


def rounding_settings(db: Session):
    """The settings row, or an unsaved object with the defaults (read-only
    callers in the calculation paths never write)."""
    row = db.query(PayrollRoundingSettings).order_by(PayrollRoundingSettings.id).first()
    return row if row is not None else SimpleNamespace(**ROUNDING_DEFAULTS)


def ensure_rounding_settings(db: Session) -> PayrollRoundingSettings:
    row = db.query(PayrollRoundingSettings).order_by(PayrollRoundingSettings.id).first()
    if row is None:
        row = PayrollRoundingSettings(**ROUNDING_DEFAULTS)
        db.add(row)
        db.flush()
    return row


def payslip_settings(db: Session):
    row = db.query(PayslipSettings).order_by(PayslipSettings.id).first()
    return row if row is not None else SimpleNamespace(**PAYSLIP_DEFAULTS, logo=None, logo_mime=None)


def ensure_payslip_settings(db: Session) -> PayslipSettings:
    row = db.query(PayslipSettings).order_by(PayslipSettings.id).first()
    if row is None:
        row = PayslipSettings(**PAYSLIP_DEFAULTS)
        db.add(row)
        db.flush()
    return row


def round_tds(amount: Decimal, mode: Optional[str]) -> Decimal:
    if mode == TDS_NEAREST_10:
        return (amount / TEN).quantize(ONE, rounding=ROUND_HALF_UP) * TEN
    return amount.quantize(ONE, rounding=ROUND_HALF_UP)


def round_days(value: Decimal, step: Optional[str]) -> Decimal:
    """Rounds a day count to the nearest step (0.5 / 0.25), half up; NONE
    leaves it unchanged."""
    if not step or step == "NONE":
        return value
    size = Decimal(step)
    return (value / size).quantize(ONE, rounding=ROUND_HALF_UP) * size


def day_step(db: Session) -> str:
    return rounding_settings(db).attendance_day_step


def rounding_defaults(db: Session) -> dict:
    """Defaults the pay group builder uses for new groups and lines."""
    s = rounding_settings(db)
    return {
        "net_rounding": s.default_net_rounding,
        "line_rounding": s.default_line_rounding,
        "line_decimals": s.default_line_decimals,
    }


def pay_group_roundings(db: Session) -> list[dict]:
    from app.services.payroll_engine.pay_groups import FORMULA

    rows = []
    for group in db.query(PayGroup).order_by(PayGroup.code).all():
        defn = group.definition or {}
        rows.append({
            "id": group.id, "code": group.code, "name": group.name, "is_active": group.is_active,
            "calc_model": group.calc_model, "definition_version": group.definition_version,
            "net_rounding": defn.get("net_rounding") or (group.params or {}).get("net_rounding") or "NONE",
            "editable": group.calc_model == FORMULA,
        })
    return rows


def apply_net_rounding(db: Session, mode: str, user: Optional[str]) -> dict:
    """Sets net_rounding on every FORMULA pay group whose definition differs,
    by saving a new definition version (history is never edited)."""
    from app.services.payroll_engine.formula.history import save_definition
    from app.services.payroll_engine.pay_groups import FORMULA

    updated, unchanged, skipped = [], [], []
    for group in db.query(PayGroup).order_by(PayGroup.code).all():
        if group.calc_model != FORMULA or not group.definition:
            skipped.append(group.code)
            continue
        if (group.definition or {}).get("net_rounding") == mode:
            unchanged.append(group.code)
            continue
        definition = copy.deepcopy(group.definition)
        definition.pop("version", None)
        definition["net_rounding"] = mode
        version = save_definition(db, group, definition, user, f"Net pay rounding set to {mode} from Rounding Rules")
        updated.append({"code": group.code, "version": version})
    return {"updated": updated, "unchanged": unchanged, "skipped": skipped}
