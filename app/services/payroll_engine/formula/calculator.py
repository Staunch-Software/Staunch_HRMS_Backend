"""Runs a compiled definition for one employee and builds the same PayResult
the strategies produce, optionally with a line-by-line trace."""
from decimal import Decimal
from typing import Any, Optional

from app.services.payroll_engine.formula.compiled import Compiled
from app.services.payroll_engine.formula.errors import FormulaRuntimeError
from app.services.payroll_engine.formula.evaluator import Evaluator
from app.services.payroll_engine.formula.functions import LINE_ROUNDERS
from app.services.payroll_engine.formula.scope import Scope
from app.services.payroll_engine.pay_groups.base import (
    AD_HOC_DEDUCTION_LABELS,
    AD_HOC_EARNING_LABELS,
    DEDUCTION,
    EARNING,
    EMPLOYER_COST,
    INFO,
    ZERO,
    PayInputs,
    PayLine,
    PayResult,
    q2,
    settle_net_pay,
)

KIND_MAP = {"EARNING": EARNING, "DEDUCTION": DEDUCTION, "EMPLOYER": EMPLOYER_COST, "INFO": INFO}
NO_INPUT_WARNING = (
    "No variable input found for this period — defaulted to full standard days "
    "(no LOP, no overtime). Enter variable inputs before calculating for accurate results."
)


def jsonable(value: Any):
    if isinstance(value, Decimal):
        return float(value)
    return value


class FormulaPayLine(PayLine):
    """PayLine plus the definition's payslip flag and statutory tag."""

    def __init__(self, code, label, kind, amount, basis=None, statutory_tag=None, show_on_payslip=True):
        super().__init__(code, label, kind, amount, basis)
        self.statutory_tag = statutory_tag
        self.show_on_payslip = show_on_payslip


def _evaluate_line(evaluator: Evaluator, scope: Scope, line) -> tuple[Decimal, bool, Optional[str], Any]:
    condition_value = None
    if line.condition_tree is not None:
        try:
            condition_value = evaluator.truth(line.condition_tree)
        except FormulaRuntimeError as exc:
            raise exc.at_line(line.code, "condition")
        if not condition_value:
            return ZERO, False, "condition is false", condition_value
    try:
        raw = evaluator.number(line.tree)
        value = LINE_ROUNDERS[line.rounding](raw, line.decimals) if line.rounding != "NONE" else raw
    except FormulaRuntimeError as exc:
        raise exc.at_line(line.code)
    except ArithmeticError:
        raise FormulaRuntimeError("calculation overflow", line=line.code) from None
    if line.omit_if_zero and value == 0:
        return value, False, "zero (hidden when zero)", condition_value
    return value, True, None, condition_value


def calculate(compiled: Compiled, profile, employee, inputs: PayInputs, trace: bool = False):
    """Returns (PayResult, trace dict or None). A FormulaRuntimeError carries
    the partial trace in `.trace` when trace=True."""
    settings = compiled.settings
    scope = Scope(compiled, profile, employee, inputs)
    variables = {k: jsonable(v) for k, v in scope.values.items()} if trace else None
    evaluator = Evaluator(scope)
    lines: list[PayLine] = []
    warnings: list[str] = [] if inputs.has_input_row else [NO_INPUT_WARNING]
    steps: list[dict] = []
    tagged: dict[str, Optional[Decimal]] = {}

    for line in compiled.lines:
        scope.used = {} if trace else None
        try:
            value, included, reason, condition_value = _evaluate_line(evaluator, scope, line)
        except FormulaRuntimeError as exc:
            if trace:
                steps.append({"code": line.code, "name": line.name, "error": str(exc), "variables": _used(scope)})
                exc.trace = {"variables": variables, "lines": steps}
            raise
        scope.set_line(line.code, value, included)
        if line.statutory_tag:
            tagged[line.statutory_tag] = value if included else None
        if included and (line.kind != "INFO" or line.show_on_payslip):
            lines.append(FormulaPayLine(line.code, line.name, KIND_MAP[line.kind], value, line.formula,
                                        line.statutory_tag, line.show_on_payslip))
        if trace:
            steps.append({
                "code": line.code, "name": line.name, "kind": line.kind, "formula": line.formula,
                "condition": line.condition, "condition_result": condition_value, "value": jsonable(value),
                "included": included, "omitted_reason": reason, "shown": included and (
                    line.kind != "INFO" or line.show_on_payslip),
                "statutory_tag": line.statutory_tag, "variables": _used(scope),
            })

    if settings["include_ad_hoc_inputs"]:
        _add_ad_hoc(inputs, compiled.line_codes, lines, warnings)

    employer = sum((ln.amount for ln in lines if ln.kind == EMPLOYER_COST), ZERO)
    gross, deductions, net = settle_net_pay(lines, settings["net_rounding"])
    paid = tagged.get("PAID_DAYS")
    lop = tagged.get("LOP_DAYS")
    result = PayResult(
        gross=gross, total_deductions=deductions, net_pay=net, employer_cost=q2(employer), lines=lines,
        warnings=warnings,
        paid_days=paid if paid is not None else scope.values["PAID_DAYS"],
        lop_days=lop if lop is not None else scope.values["LOP_DAYS"],
        pf_wages=q2(tagged["PF_WAGES"]) if tagged.get("PF_WAGES") is not None else None,
        esi_wages=q2(tagged["ESI_WAGES"]) if tagged.get("ESI_WAGES") is not None else None,
    )
    return result, ({"variables": variables, "lines": steps} if trace else None)


def _used(scope: Scope) -> dict:
    return {k: jsonable(v) for k, v in (scope.used or {}).items()}


def _add_ad_hoc(inputs: PayInputs, consumed: frozenset, lines: list, warnings: list) -> None:
    for code, amount in inputs.earnings.items():
        if code in consumed or amount == 0:
            continue
        label = AD_HOC_EARNING_LABELS.get(code)
        if label is None:
            label = code
            warnings.append(f"Unrecognised ad-hoc earning code '{code}' paid as a plain earning.")
        lines.append(PayLine(code, label, EARNING, amount, "monthly input"))
    for code, amount in inputs.deductions.items():
        if code in consumed or amount == 0:
            continue
        label = AD_HOC_DEDUCTION_LABELS.get(code)
        if label is None:
            label = code
            warnings.append(f"Unrecognised ad-hoc deduction code '{code}' deducted as a plain deduction.")
        lines.append(PayLine(code, label, DEDUCTION, amount, "monthly input"))
