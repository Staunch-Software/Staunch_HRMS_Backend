"""Formula language: operators, Excel rounding, functions, errors with
positions; definition validation (references, cycles, forward refs); trace."""
from decimal import Decimal

import pytest

from app.services.payroll_engine.formula.errors import FormulaError, FormulaRuntimeError
from app.services.payroll_engine.formula.evaluator import Evaluator
from app.services.payroll_engine.formula.parser import parse

D = Decimal


class Env:
    def __init__(self, values=None, blanks=(), tables=None):
        self.values = {k: (D(str(v)) if isinstance(v, (int, float)) else v) for k, v in (values or {}).items()}
        self.blanks = set(blanks)
        self.tables = tables or {}

    def value(self, name, pos):
        if name not in self.values:
            raise FormulaRuntimeError(f"unknown variable '{name}'", pos)
        return self.values[name]

    def is_blank(self, name, pos):
        return name in self.blanks

    def lookup(self, table, key, pos, has_default):
        rows, default = self.tables[table]
        if str(key).upper().replace(" ", "_") in rows:
            return rows[str(key).upper().replace(" ", "_")]
        if has_default:
            return None
        if default is None:
            raise FormulaRuntimeError(f"no row for '{key}'", pos)
        return default


def ev(text, **values):
    return Evaluator(Env(values)).eval(parse(text))


@pytest.mark.parametrize("text,expected", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("-2 * 3", -6), ("10 / 4", D("2.5")), ("2 - -3", 5),
    ("12%", D("0.12")), ("25000 * 12%", 3000), ("0.1 + 0.2 = 0.3", 1), ("=1+1", 2), ("1e3 / 8", 125),
    ("3 > 2", 1), ("3 >= 3", 1), ("2 < 1", 0), ("2 <= 2", 1), ("1 = 1", 1), ("1 == 1", 1),
    ("1 <> 2", 1), ("1 != 1", 0), ("'Skilled' = \"SKILLED\"", 1), ("1 = 'a'", 0), ("1 <> 'a'", 1),
    ("1 AND 0", 0), ("1 OR 0", 1), ("NOT 0", 1), ("NOT 1 = 2", 1), ("AND(1, 2, 3)", 1), ("OR(0, 0)", 0),
    ("NOT(5)", 0), ("TRUE + TRUE", 2), ("0 AND 1 / 0", 0), ("OR(1, 1 / 0)", 1), ("1 + 2 > 2 AND 1", 1),
    ("IF(1, 5, 1 / 0)", 5), ("IF(0, 5)", 0), ("IF(2 > 1, 'A', 'B') = 'a'", 1),
    ("MIN(3, 1, 2)", 1), ("MAX(3, 1, 2)", 3), ("ABS(-4.5)", D("4.5")),
    ("CEILING(12.1, 5)", 15), ("CEILING(-1.5)", -1), ("FLOOR(-1.5)", -2), ("FLOOR(7.9, 0.5)", D("7.5")),
    ("MOD(7, 3)", 1), ("MOD(-7, 3)", 2), ("MOD(7, -3)", -2),
    ("'it''s'", "it's"),
])
def test_operators_and_functions(text, expected):
    assert ev(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("ROUND(2.5)", 3), ("ROUND(-2.5)", -3), ("ROUND(2.45, 1)", D("2.5")), ("ROUND(-2.45, 1)", D("-2.5")),
    ("ROUND(1234.5, -2)", 1200), ("ROUND(20184.5, 0)", 20185),
    ("ROUNDUP(1.01)", 2), ("ROUNDUP(-1.01)", -2), ("ROUNDUP(1.001, 2)", D("1.01")), ("ROUNDUP(3)", 3),
    ("ROUNDDOWN(1.99)", 1), ("ROUNDDOWN(-1.99)", -1), ("ROUNDDOWN(1.999, 2)", D("1.99")),
    # Decimal division residue is cleaned before ROUNDUP (Excel's ~15 digits), so it does not overshoot.
    ("ROUNDUP(2238.6 * 7 / 7, 1)", D("2238.6")), ("ROUNDUP(10 / 3 * 3, 0)", 10),
])
def test_excel_rounding(text, expected):
    assert ev(text) == expected


def test_variables_text_and_isblank():
    env = Env({"SKILL": "HIGHLY_SKILLED", "PF_CAP": 0, "DAY_WAGE": 500}, blanks={"PF_CAP"})
    assert Evaluator(env).eval(parse("IF(ISBLANK(PF_CAP), 1800, PF_CAP)")) == 1800
    assert Evaluator(env).eval(parse("skill = 'highly_skilled' AND day_wage * 2 = 1000")) == 1


def test_lookup_row_default_argument_and_table_default():
    env = Env({"SKILL": "SKILLED", "OTHER": "EXPERT"},
              tables={"RATE": ({"SKILLED": D("144.73")}, None), "TRANSPORT": ({}, D("0"))})
    run = lambda text: Evaluator(env).eval(parse(text))  # noqa: E731
    assert run("LOOKUP(RATE, SKILL)") == D("144.73")
    assert run("LOOKUP('rate', 'Skilled')") == D("144.73")
    assert run("LOOKUP(RATE, OTHER, 99)") == 99
    assert run("LOOKUP(TRANSPORT, OTHER)") == 0
    with pytest.raises(FormulaRuntimeError, match="no row for 'EXPERT'"):
        run("LOOKUP(RATE, OTHER)")


@pytest.mark.parametrize("text,message,position", [
    ("1 +", "expected a value but found the end of the formula", 4),
    ("(1 + 2", "expected ')' but found the end of the formula", 7),
    ("1 $ 2", "unexpected character '$'", 3),
    ("'abc", "text is missing its closing quote", 1),
    ("1 < 2 < 3", "comparisons cannot be chained", 7),
    ("1 2", "unexpected '2'", 3),
    ("AND", "'AND' needs a value before it", 1),
    ("", "formula is empty", None),
])
def test_syntax_errors_have_positions(text, message, position):
    with pytest.raises(FormulaError) as exc:
        parse(text)
    assert message in exc.value.message
    assert exc.value.position == position


@pytest.mark.parametrize("text,message,position", [
    ("1 / (2 - 2)", "division by zero", 3),
    ("BASIC * 2", "unknown variable 'BASIC'", 1),
    ("ROUND(1, 2, 3)", "wrong number of arguments: ROUND(x, decimals=0)", 1),
    ("ROUND(1.5, 0.5)", "ROUND: the number of decimals must be a whole number", 1),
    ("CEILING(5, 0)", "CEILING: the multiple must be greater than 0", 1),
    ("MOD(5, 0)", "MOD: division by zero", 1),
    ("'a' + 1", "expected a number but got text \"a\"", 1),
    ("IF('yes', 1, 2)", "text \"yes\" cannot be used as a condition", 4),
    ("'a' < 1", "cannot compare", 5),
    ("FOO(1)", "unknown function FOO", 1),
])
def test_runtime_errors_have_positions(text, message, position):
    with pytest.raises(FormulaRuntimeError) as exc:
        ev(text)
    assert message in exc.value.message
    assert exc.value.position == position


def test_error_function_raises_the_message_verbatim():
    with pytest.raises(FormulaRuntimeError) as exc:
        ev('IF(1, ERROR("Pay profile has no daily_wage."), 0)')
    assert str(exc.value) == "Pay profile has no daily_wage."


# --- definitions -----------------------------------------------------------------

from app.services.payroll_engine.formula.calculator import calculate  # noqa: E402
from app.services.payroll_engine.formula.compiled import compile_definition, validate_definition  # noqa: E402
from app.services.payroll_engine.pay_groups import PayInputs, PayProfileData  # noqa: E402


def ln(code, formula, kind="EARNING", **kw):
    return {"code": code, "name": code.title(), "kind": kind, "formula": formula, **kw}


def defn(*lines, **kw):
    return {"standard_days": 26, "lines": list(lines), **kw}


def messages(definition):
    return [(e["line"], e["message"], e["position"]) for e in validate_definition(definition).errors]


def test_forward_reference_self_reference_and_cycle():
    errors = messages(defn(ln("A", "B + 1"), ln("B", "A * 2"), ln("C", "C + 1"), ln("D", "E"), ln("E", "1")))
    assert ("A", "circular reference: A -> B -> A", 1) in errors
    assert ("C", "line C refers to itself", 1) in errors
    assert ("D", "'E' is line 5, below this one; a line can only use lines above it", 1) in errors
    assert not any(e[0] == "B" for e in errors)


def test_unknown_names_functions_and_lookups_reported_together():
    definition = defn(
        ln("A", "DAY_WAGES * 2 + ROUND(1, 2, 3)"), ln("B", "LOOKUP(RATES, SKILL) + RATE"),
        ln("C", "ROUNDUPP(1) + ISBLANK(1 + 2)"),
        lookups=[{"code": "RATE", "rows": [{"key": "Skilled", "value": 1}]}],
    )
    errors = messages(definition)
    assert ("A", "unknown variable 'DAY_WAGES' (did you mean DAY_WAGE?)", 1) in errors
    assert ("A", "ROUND takes 1 to 2 argument(s): ROUND(x, decimals=0)", 17) in errors
    assert ("B", "unknown lookup table 'RATES' (did you mean RATE?)", 8) in errors
    assert ("B", "'RATE' is a lookup table; use LOOKUP(RATE, key)", 24) in errors
    assert ("C", "unknown function ROUNDUPP (did you mean ROUNDUP?)", 1) in errors
    assert ("C", "ISBLANK needs a variable name, e.g. ISBLANK(PF_CAP)", 15) in errors


def test_schema_errors_codes_tags_and_reserved_names():
    definition = {
        "standard_days": 0, "net_rounding": "CEIL", "bogus": 1,
        "constants": [{"code": "rate", "value": "x"}, {"code": "DAY_WAGE", "value": 1}],
        "employee_fields": [{"code": "RATE", "type": "select"}],
        "lines": [ln("1BAD", "1"), ln("TDS", "1"), ln("X", "1", kind="BONUS"), ln("Y", "1", statutory_tag="PF_EE"),
                  ln("Y", "2", rounding="UP", decimals=9), ln("ROUND", "1")],
    }
    text = " | ".join(f"{e['path']}: {e['message']}" for e in validate_definition(definition).errors)
    for expected in (
        "bogus: unknown setting", "standard_days: must be a number greater than 0", "net_rounding: must be one of",
        "constants[0].value: must be a number", "constants[1].code: 'DAY_WAGE' is a built-in variable",
        "employee_fields[0].options: a select field needs a list", "employee_fields[0].code: 'RATE' is already used",
        "lines[0].code: must start with a letter", "lines[1].code: 'TDS' is reserved", "lines[2].kind: must be one of",
        "lines[3].statutory_tag: tag PF_EE needs a DEDUCTION line", "lines[4].code: duplicate line code",
        "lines[4].rounding: must be one of", "lines[4].decimals: must be a whole number",
        "lines[5].code: 'ROUND' is reserved",
    ):
        assert expected in text, expected


def test_shadowing_input_consumption_and_warning():
    definition = defn(
        ln("DAYS_PRESENT", "DAYS_PRESENT", kind="INFO", show_on_payslip=True, statutory_tag="PAID_DAYS"),
        ln("WAGES", "DAY_WAGE * DAYS_PRESENT"),
        ln("SUPERVISOR_ALLOWANCE", "IF(ISBLANK(SUPERVISOR_ALLOWANCE), 100, SUPERVISOR_ALLOWANCE)"),
        ln("BONUS", "OTHER_PAYMENT * 10%"),
    )
    result = validate_definition(definition)
    assert result.ok, result.errors
    assert [w["line"] for w in result.warnings] == ["BONUS"]
    inputs = PayInputs(present_days=D(20), earnings={"SUPERVISOR_ALLOWANCE": D(250), "OTHER_PAYMENT": D(500)})
    pay, trace = calculate(compile_definition(definition), PayProfileData(daily_wage=100), None, inputs, trace=True)
    assert [(x.code, x.amount) for x in pay.lines] == [
        ("DAYS_PRESENT", 20), ("WAGES", 2000), ("SUPERVISOR_ALLOWANCE", 250), ("BONUS", 50), ("OTHER_PAYMENT", 500)]
    assert pay.paid_days == 20 and pay.net_pay == 2800
    assert trace["lines"][2]["variables"] == {"ISBLANK(SUPERVISOR_ALLOWANCE)": 0, "SUPERVISOR_ALLOWANCE": 250.0}


def test_trace_conditions_omitted_lines_and_hidden_helpers():
    definition = defn(
        ln("BASE", "MONTHLY_SALARY * 80%", kind="INFO"),
        ln("BASIC", "BASE * PAID_DAYS / STANDARD_DAYS", rounding="ROUND", statutory_tag="BASIC"),
        ln("PF_EE", "MIN(BASIC * 12%, 1800)", kind="DEDUCTION", condition="PF_APPLICABLE", statutory_tag="PF_EE"),
        ln("OT", "OT_HOURS * 100", omit_if_zero=True),
        ln("CHECK", "ISBLANK(PF_EE)", kind="INFO", show_on_payslip=True),
        net_rounding="ROUNDUP",
    )
    profile = PayProfileData(monthly_salary=30000, pf_applicable=False)
    pay, trace = calculate(compile_definition(definition), profile, None, PayInputs(lop_days=D(2)), trace=True)
    assert [x.code for x in pay.lines] == ["BASIC", "CHECK"]
    assert pay.line_amount("BASIC") == 22154 and pay.line_amount("CHECK") == 1  # ROUND(24000 * 24 / 26)
    steps = {s["code"]: s for s in trace["lines"]}
    assert steps["BASE"]["shown"] is False and steps["BASE"]["value"] == 24000
    assert steps["BASIC"]["variables"] == {"BASE": 24000.0, "PAID_DAYS": 24.0, "STANDARD_DAYS": 26.0}
    assert steps["PF_EE"]["included"] is False and steps["PF_EE"]["omitted_reason"] == "condition is false"
    assert steps["OT"]["omitted_reason"] == "zero (hidden when zero)"
    assert trace["variables"]["MONTHLY_SALARY"] == 30000 and trace["variables"]["PAID_DAYS"] == 24
    assert pay.pf_wages is None and pay.lop_days == 2


def test_runtime_error_names_the_line_and_keeps_partial_trace():
    definition = defn(ln("A", "10"), ln("B", "A / (DAYS_PRESENT - 26)"))
    with pytest.raises(FormulaRuntimeError) as exc:
        calculate(compile_definition(definition), PayProfileData(), None, PayInputs(), trace=True)
    assert str(exc.value) == "Line B, position 3: division by zero"
    assert [s["code"] for s in exc.value.trace["lines"]] == ["A", "B"]


def test_lookup_keys_match_numbers_and_spelling_variants():
    definition = defn(
        ln("A", "LOOKUP(BY_YEARS, YEARS) + LOOKUP(BY_SKILL, SKILL) + LOOKUP(BY_SKILL, 'unknown', 0.5)"),
        lookups=[{"code": "BY_YEARS", "rows": [{"key": 5, "value": 100}, {"key": "10", "value": 200}]},
                 {"code": "BY_SKILL", "rows": [{"key": "Highly Skilled", "value": 7}]}],
        employee_fields=[{"code": "YEARS", "type": "number"}],
    )
    profile = PayProfileData(skill_level="highly-skilled", extra={"YEARS": "5.00"})
    pay, _ = calculate(compile_definition(definition), profile, None, PayInputs())
    assert pay.line_amount("A") == D("107.5")
    duplicate = defn(ln("A", "1"), lookups=[{"code": "T", "rows": [{"key": 5, "value": 1}, {"key": "5.0", "value": 2}]}])
    assert any("duplicate key" in e[1] for e in messages(duplicate))
