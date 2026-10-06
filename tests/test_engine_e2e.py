"""End-to-end test of calculate_payroll_run against 3 employees with a real
salary structure (BASIC + HRA + PF_EE + PT), variable inputs, and the seeded
statutory config shape. Expected net pay for each employee is hand-computed
below and asserted exactly (all money math is Decimal, quantized to cents,
so these come out exact — no floating-point slop).

Shared setup: Monthly period 2026-09-01..2026-09-30 (30 calendar days).
Salary structure for every employee: BASIC (Fixed Amount, prorated,
affects_pf=True, affects_esi=True), HRA = "0.4 * BASIC" (Percentage/Formula,
prorated at the BASIC step only — never re-prorated), PF_EE = "0.12 *
PF_WAGES" driven by the dedicated PF calculator (12% employee rate, 15,000
wage ceiling, apply_ceiling=True), PT driven by the dedicated PT calculator
against seeded Tamil Nadu (TN) slabs.

--- Employee 1 (E-EE1): full month, no LOP, BASIC override = 60,000 -------
  factor = 1 (no variable input row at all -> engine defaults to full
  period / 0 LOP, exercising that fallback path)
  BASIC = 60,000.00
  HRA   = 0.4 * 60,000 = 24,000.00
  GROSS = 84,000.00
  PF_WAGES = 60,000 (only BASIC affects_pf) -> capped at ceiling 15,000
  PF_EE  = 15,000 * 0.12 = 1,800.00
  PT: gross 84,000 falls in TN's "75,001+" slab -> 1,250.00
  NET = 84,000 - 1,800 - 1,250 = 80,950.00

--- Employee 2 (E-EE2): 5 LOP days, BASIC override = 60,000 ---------------
  eligible_days = 30 - 5 = 25; factor = 25/30
  BASIC = 60,000 * 25/30 = 50,000.00 (exact)
  HRA   = 0.4 * 50,000 = 20,000.00
  GROSS = 70,000.00
  PF_WAGES = 50,000 -> capped at ceiling 15,000
  PF_EE  = 15,000 * 0.12 = 1,800.00
  PT: gross 70,000 falls in TN's "60,001-75,000" slab -> 1,025.00
  NET = 70,000 - 1,800 - 1,025 = 67,175.00

--- Employee 3 (E-EE3): full month, no LOP, BASIC override = 10,000 -------
  factor = 1
  BASIC = 10,000.00
  HRA   = 0.4 * 10,000 = 4,000.00
  GROSS = 14,000.00
  PF_WAGES = 10,000 -> BELOW the 15,000 ceiling, so uncapped
  PF_EE  = 10,000 * 0.12 = 1,200.00
  PT: gross 14,000 falls in TN's "0-21,000" slab -> 0.00
  NET = 14,000 - 1,200 - 0 = 12,800.00
"""
from datetime import date

from app.db.seed.salary_components_seed import SALARY_COMPONENTS_SEED
from app.models.employee import Employee
from app.models.org import WorkLocation
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.models.salary_component import SalaryComponent
from app.models.salary_structure import EmployeeSalaryStructureAssignment
from app.models.statutory import PFConfig, PTSlab, PTState
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.engine import calculate_payroll_run

PERIOD_START = date(2026, 9, 1)
PERIOD_END = date(2026, 9, 30)


def _seed_components(db_session) -> dict[str, SalaryComponent]:
    by_code = {}
    for data in SALARY_COMPONENTS_SEED:
        if data["code"] not in ("BASIC", "HRA", "PF_EE", "PT"):
            continue
        component = SalaryComponent(**data)
        db_session.add(component)
        db_session.commit()
        db_session.refresh(component)
        by_code[component.code] = component
    return by_code


def _seed_pf_config(db_session) -> None:
    db_session.add(
        PFConfig(
            effective_from=date(2024, 4, 1),
            effective_to=None,
            employee_rate=0.12,
            employer_eps_rate=0.0833,
            employer_epf_rate=0.0367,
            wage_ceiling=15000,
            eps_wage_ceiling=15000,
            apply_ceiling=True,
            is_active=True,
        )
    )
    db_session.commit()


def _seed_tn_pt(db_session) -> WorkLocation:
    location = WorkLocation(name="Chennai", state_code="TN", status="Active")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    state = PTState(name="Tamil Nadu", code="TN")
    db_session.add(state)
    db_session.commit()
    db_session.refresh(state)

    for income_min, income_max, tax_amount in [
        (0, 21000, 0),
        (21001, 30000, 135),
        (30001, 45000, 315),
        (45001, 60000, 690),
        (60001, 75000, 1025),
        (75001, None, 1250),
    ]:
        db_session.add(
            PTSlab(
                state_id=state.id,
                effective_from=date(2024, 4, 1),
                effective_to=None,
                income_min=income_min,
                income_max=income_max,
                tax_amount=tax_amount,
                periodicity="HalfYearly",
                is_verified=False,
                notes="test seed",
            )
        )
    db_session.commit()
    return location


def _make_employee(db_session, code, location_id, basic_amount) -> Employee:
    employee = Employee(
        employee_code=code,
        first_name=code,
        status="Active",
        work_location_id=location_id,
        date_of_joining=date(2025, 1, 1),
        basic_salary=basic_amount,
    )
    db_session.add(employee)
    db_session.commit()
    db_session.refresh(employee)
    return employee


def _assign_structure(db_session, employee, components, basic_override):
    db_session.add(
        EmployeeSalaryStructureAssignment(
            employee_id=employee.id,
            salary_component_id=components["BASIC"].id,
            value_override=basic_override,
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
    )
    for code in ("HRA", "PF_EE", "PT"):
        db_session.add(
            EmployeeSalaryStructureAssignment(
                employee_id=employee.id,
                salary_component_id=components[code].id,
                value_override=None,
                effective_from=date(2025, 1, 1),
                effective_to=None,
            )
        )
    db_session.commit()


def test_engine_e2e_three_employees(db_session):
    components = _seed_components(db_session)
    _seed_pf_config(db_session)
    location = _seed_tn_pt(db_session)

    emp1 = _make_employee(db_session, "E-EE1", location.id, 60000)
    emp2 = _make_employee(db_session, "E-EE2", location.id, 60000)
    emp3 = _make_employee(db_session, "E-EE3", location.id, 10000)

    _assign_structure(db_session, emp1, components, 60000)
    _assign_structure(db_session, emp2, components, 60000)
    _assign_structure(db_session, emp3, components, 10000)

    # emp1 deliberately has NO variable input row -> exercises the
    # "default to full period, 0 LOP" fallback path.
    db_session.add(
        VariablePayInput(
            employee_id=emp2.id,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            source="Manual",
            present_days=25,
            lop_days=5,
            status="Draft",
        )
    )
    db_session.add(
        VariablePayInput(
            employee_id=emp3.id,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            source="Manual",
            present_days=30,
            lop_days=0,
            status="Draft",
        )
    )
    db_session.commit()

    schedule = PaySchedule(
        schedule_name="Monthly - All",
        pay_frequency="Monthly",
        payment_day_rule={"type": "LastDayOfMonth"},
        holiday_adjustment="Before",
        is_active=True,
        applies_to=None,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)

    run = PayrollRun(
        pay_schedule_id=schedule.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        pay_date=date(2026, 10, 5),
        run_type="Regular",
        status="Draft",
        source="InAppCalculation",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    calculate_payroll_run(db_session, run)

    assert run.status == "Calculated"
    assert run.employee_count == 3

    line_items = {li.employee_id: li for li in run.line_items}

    li1 = line_items[emp1.id]
    assert li1.gross_earnings == 84000.00
    assert li1.total_deductions == 1800.00 + 1250.00
    assert li1.net_pay == 80950.00

    li2 = line_items[emp2.id]
    assert li2.gross_earnings == 70000.00
    assert li2.total_deductions == 1800.00 + 1025.00
    assert li2.net_pay == 67175.00

    li3 = line_items[emp3.id]
    assert li3.gross_earnings == 14000.00
    assert li3.total_deductions == 1200.00 + 0.00
    assert li3.net_pay == 12800.00

    assert run.total_net == 80950.00 + 67175.00 + 12800.00
