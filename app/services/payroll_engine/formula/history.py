"""Saving a FORMULA group's definition: every save bumps the version and
keeps a copy in pay_group_definition_versions."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.pay_group import PayGroup, PayGroupDefinitionVersion
from app.services.payroll_engine.formula.compiled import validate_definition
from app.services.payroll_engine.formula.converters_wage import definition_from_params
from app.services.payroll_engine.pay_groups import FORMULA, PayGroupParamsError


def save_definition(db: Session, group: PayGroup, raw: dict, changed_by: Optional[str], note: Optional[str]) -> int:
    """Validates, stores the normalised definition as a new version (flushes,
    does not commit) and returns the version number."""
    result = validate_definition(raw)
    if not result.ok:
        raise PayGroupParamsError("; ".join(result.error_texts()))
    version = (group.definition_version or 0) + 1
    definition = {**result.definition, "version": version}
    group.calc_model = FORMULA
    group.definition = definition
    group.definition_version = version
    if group.id is None:
        db.add(group)
        db.flush()
    db.add(PayGroupDefinitionVersion(pay_group_id=group.id, version=version, definition=definition,
                                     changed_by=changed_by, note=note))
    db.flush()
    return version


def converted_definition(calc_model: str, params: dict, code: Optional[str] = None) -> dict:
    """Normalised FORMULA definition equivalent to a legacy strategy group."""
    result = validate_definition(definition_from_params(calc_model, params, code))
    if not result.ok:  # pragma: no cover - the converters are tested
        raise PayGroupParamsError("; ".join(result.error_texts()))
    return result.definition
