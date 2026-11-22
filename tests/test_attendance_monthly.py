"""Monthly attendance processing: day resolution, LOP, process / send to
payroll and the employee view.

November 2026 under the default policy: Sundays 1, 8, 15, 22, 29 and the
2nd Saturday (14) are off -> 24 working days of 30. Service calls pass
today=TODAY so the whole month is in the past.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.models.attendance_month import AttendanceMonthResult
from app.models.attendance_policy import Holiday
from app.models.leave import STATUS_APPROVED, LeaveLedger, PermissionRequest
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.variable_pay import VariablePayInput
from app.services.attendance import monthly
from app.services.leave import workflow
from app.services.leave.calendar import KIND_WORK, day_kinds, ensure_default_policy
from tests.leave_helpers import API, bearer, credit, make_employee, make_user, seed_types, token_for

NOV = "2026-11"
TODAY = date(2026, 12, 31)


def d(day: int, month: int = 11) -> date:
    return date(2026, month, day)


def at(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)))


def punch(db, emp, day, t_in="09:25", t_out="18:35"):
    p_in = at(day, t_in) if t_in else None
    p_out = at(day, t_out) if t_out else None
    worked = int((p_out - p_in).total_seconds() // 60) if p_in and p_out else None
    db.add(AttendanceDaily(
        employee_code=emp.employee_code, employee_id=emp.id, work_date=day, punch_in=p_in, punch_out=p_out,
        punch_in_source="IN" if p_in else None, punch_out_source="OUT" if p_out else None, worked_minutes=worked,
        punch_count=(1 if p_in else 0) + (1 if p_out else 0), username="device user",
    ))
    db.commit()


def working_days(db, emp, first=d(1), last=d(30)):
    return [day for day, (kind, _) in day_kinds(db, emp, first, last).items() if kind == KIND_WORK]


def colleague_punch(db, day):
    """Someone else punched that day, so the office was open (see no_punch_dates)."""
    if not db.query(AttendanceDaily).filter_by(employee_code="COLLEAGUE", work_date=day).first():
        db.add(AttendanceDaily(employee_code="COLLEAGUE", work_date=day, punch_in=at(day, "09:20"),
                               punch_in_source="IN", punch_count=1, username="colleague"))
        db.commit()


def full_month(db, emp, skip=(), first=d(1), last=d(30)):
    for day in working_days(db, emp, first, last):
        if day not in skip:
            punch(db, emp, day)
        else:
            colleague_punch(db, day)


def status(db, emp, day, code, leave=None, half="FULL"):
    db.add(AttendanceOverride(
        employee_id=emp.id, employee_code=emp.employee_code, work_date=day, kind="STATUS", status_code=code,
        leave_type_code=leave, half=half, reason="test", source="UPLOAD_MUSTER", is_active=True,
    ))
    db.commit()


def hr_leave(db, hr, emp, leave_type, start, end=None, half="FULL"):
    application, warnings = workflow.record_leave_on_behalf(
        db, hr_user=hr, employee_id=emp.id, leave_type_id=leave_type.id, from_date=start, from_half=half,
        to_date=end or start, to_half=half if end is None else "FULL", reason="test", today=TODAY,
    )
    db.commit()
    return application, warnings


def permission(db, emp, day, minutes):
    db.add(PermissionRequest(
        employee_id=emp.id, date=day, from_time=time(9, 30),
        to_time=(at(day, "09:30") + timedelta(minutes=minutes)).time(), minutes=minutes, status=STATUS_APPROVED,
        reason="test",
    ))
    db.commit()


def resolve(db, emp):
    return monthly.resolve_month(db, emp, NOV, today=TODAY)


def total(result, key) -> Decimal:
    return Decimal(result.totals[key])


def on(result, day: date):
    return next(x for x in result.days if x.date == day)


@pytest.fixture()
def types(db_session):
    return seed_types(db_session)


@pytest.fixture()
def emp(db_session):
    return make_employee(db_session, "ST0001", joined=date(2025, 1, 1))


@pytest.fixture()
def hr(seeded_admin):
    return seeded_admin


# --- day resolution -----------------------------------------------------------------

def test_full_month_present_has_no_lop(db_session, types, emp):
    full_month(db_session, emp)
    result = resolve(db_session, emp)
    assert total(result, "total_lop") == 0
    assert result.totals["working_days"] == 24
    assert total(result, "present_days") == 24
    assert result.totals["weekly_offs"] == 6
    assert result.totals["late_count"] == 0
    assert total(result, "payroll_present_days") == 30
    assert on(result, d(1)).status == monthly.ST_OFF
    assert on(result, d(14)).status == monthly.ST_OFF  # 2nd Saturday
    assert on(result, d(7)).status == monthly.ST_PRESENT  # 1st Saturday works
    assert on(result, d(21)).status == monthly.ST_PRESENT


def test_holiday_is_paid_off(db_session, types, emp):
    db_session.add(Holiday(date=d(4), name="Deepavali", is_active=True))
    db_session.commit()
    full_month(db_session, emp)
    result = resolve(db_session, emp)
    assert on(result, d(4)).status == monthly.ST_HOLIDAY
    assert on(result, d(4)).holiday_name == "Deepavali"
    assert result.totals["holidays"] == 1
    assert result.totals["working_days"] == 23
    assert total(result, "total_lop") == 0


def test_unauthorized_absence_is_one_lop(db_session, types, emp):
    full_month(db_session, emp, skip={d(10)})
    result = resolve(db_session, emp)
    assert on(result, d(10)).status == monthly.ST_ABSENT
    assert total(result, "absent_lop") == 1
    assert total(result, "total_lop") == 1
    assert total(result, "payroll_present_days") == 29


def _three_lates(db, emp):
    late_days = {d(3), d(4), d(5)}
    full_month(db, emp, skip=late_days)
    for day in sorted(late_days):
        punch(db, emp, day, "09:50", "18:50")
    return late_days


def test_third_late_is_straight_lop_and_balance_untouched(db_session, types, emp, hr):
    credit(db_session, emp, types["CL"], 5, 2026)
    credit(db_session, emp, types["EL"], 5, 2026)
    _three_lates(db_session, emp)
    result = resolve(db_session, emp)
    assert result.totals["late_count"] == 3
    assert result.totals["chargeable_lates"] == 1
    assert total(result, "late_lop") == Decimal("0.5")
    assert total(result, "total_lop") == Decimal("0.5")
    assert "late_debits" not in result.totals and result.totals["debits"] == []
    assert on(result, d(3)).status == monthly.ST_LATE
    assert monthly.FLAG_LATE_CHARGED in on(result, d(5)).flags
    assert monthly.FLAG_LATE_CHARGED not in on(result, d(4)).flags

    chennai = _groups(db_session)["CHENNAI_HO"]
    _profile(db_session, emp, chennai)
    monthly.process_month(db_session, NOV, chennai.id, hr, today=TODAY)
    monthly.send_to_payroll(db_session, NOV, chennai.id, hr)
    db_session.commit()
    assert _attendance_ledger(db_session, emp) == []
    from app.services.leave.balance import balance
    assert balance(db_session, emp.id, types["CL"].id, 2026) == Decimal("5")
    vi = db_session.query(VariablePayInput).filter(VariablePayInput.employee_id == emp.id).one()
    assert Decimal(vi.lop_days) == Decimal("0.5")


def test_two_lates_are_free(db_session, types, emp):
    full_month(db_session, emp, skip={d(3), d(4)})
    punch(db_session, emp, d(3), "09:50", "18:50")
    punch(db_session, emp, d(4), "09:36", "18:40")
    result = resolve(db_session, emp)
    assert result.totals["late_count"] == 2
    assert total(result, "late_deduction_days") == 0


def _lates(db, emp, count):
    late_days = working_days(db, emp)[:count]
    full_month(db, emp, skip=set(late_days))
    for day in late_days:
        punch(db, emp, day, "09:36", "18:40")
    return late_days


@pytest.mark.parametrize("lates, lop, starts", [
    (2, "0", []), (3, "0.5", [3]), (5, "0.5", [3]), (6, "1", [3, 6]), (8, "1", [3, 6]),
    (9, "1.5", [3, 6, 9]), (11, "1.5", [3, 6, 9]),
])
def test_late_blocks(db_session, types, emp, lates, lop, starts):
    late_days = _lates(db_session, emp, lates)
    result = resolve(db_session, emp)
    assert result.totals["late_count"] == lates
    assert result.totals["late_block_size"] == 3
    assert result.totals["late_blocks"] == len(starts)
    assert total(result, "late_lop") == Decimal(lop) == total(result, "total_lop")
    started = [i for i, day in enumerate(late_days, start=1)
               if monthly.FLAG_LATE_BLOCK_START in on(result, day).flags]
    assert started == starts
    charged = [i for i, day in enumerate(late_days, start=1) if monthly.FLAG_LATE_CHARGED in on(result, day).flags]
    assert charged == list(range(3, lates + 1))
    if lates >= 4:
        assert any("started by late #3" in n for n in on(result, late_days[3]).notes)


def test_late_block_size_from_policy_and_grace_boundary(db_session, types, emp):
    policy = ensure_default_policy(db_session)
    policy.late_block_size = 1
    db_session.commit()
    late_days = _lates(db_session, emp, 5)
    db_session.query(AttendanceDaily).filter(AttendanceDaily.work_date == late_days[-1]).delete()
    punch(db_session, emp, late_days[-1], "09:35", "18:40")  # on the grace time: not late
    result = resolve(db_session, emp)
    assert result.totals["late_count"] == 4
    assert total(result, "late_lop") == Decimal("1")  # lates 3 and 4, 0.5 each


def test_late_excused_by_permission(db_session, types, emp):
    late_days = _three_lates(db_session, emp)
    for day in sorted(late_days):
        permission(db_session, emp, day, 20)
    result = resolve(db_session, emp)
    assert result.totals["late_count"] == 0
    assert result.totals["late_excused_days"] == 3
    assert monthly.FLAG_LATE_EXCUSED in on(result, d(3)).flags

    # permission shorter than the delay does not excuse it
    permission(db_session, emp, d(6), 10)
    db_session.query(AttendanceDaily).filter(AttendanceDaily.work_date == d(6)).delete()
    punch(db_session, emp, d(6), "09:50", "18:50")
    assert resolve(db_session, emp).totals["late_count"] == 1


def test_worked_hours_never_cause_lop(db_session, types, emp):
    full_month(db_session, emp, skip={d(10), d(11)})
    punch(db_session, emp, d(10), "09:30", "15:30")  # 6 hours
    punch(db_session, emp, d(11), "09:30", "11:00")  # 1.5 hours
    result = resolve(db_session, emp)
    for day in (on(result, d(10)), on(result, d(11))):
        assert day.status == monthly.ST_PRESENT and day.lop == 0 and day.present == 1
    assert on(result, d(10)).worked_minutes == 360
    assert total(result, "total_lop") == 0
    assert "short_hours_lop" not in result.totals and "shortfall_minutes" not in on(result, d(10)).to_json()


@pytest.mark.parametrize("minutes, lop", [(90, "0"), (120, "0"), (180, "0.5"), (240, "0.5"), (300, "1")])
def test_permission_lop_by_duration(db_session, types, emp, minutes, lop):
    full_month(db_session, emp)
    permission(db_session, emp, d(11), minutes)
    result = resolve(db_session, emp)
    day = on(result, d(11))
    assert day.lop == Decimal(lop) and total(result, "permission_lop") == Decimal(lop)
    assert day.present == 1 - Decimal(lop)
    assert (monthly.FLAG_PERMISSION_LOP in day.flags) == (Decimal(lop) > 0)
    assert total(result, "total_lop") == Decimal(lop)


def test_permissions_are_judged_one_by_one(db_session, types, emp):
    full_month(db_session, emp)
    permission(db_session, emp, d(11), 90)
    permission(db_session, emp, d(11), 90)
    permission(db_session, emp, d(12), 180)
    permission(db_session, emp, d(12), 150)
    result = resolve(db_session, emp)
    assert on(result, d(11)).lop == 0
    assert on(result, d(12)).lop == 1 and on(result, d(12)).present == 0
    assert on(result, d(12)).status == monthly.ST_PRESENT  # attended; the permissions cost the day
    assert total(result, "permission_lop") == 1 and result.totals["permission_lop_days"] == 1


def test_permission_thresholds_follow_the_policy(db_session, types, emp):
    policy = ensure_default_policy(db_session)
    policy.permission_half_day_above_minutes = 60
    policy.permission_full_day_above_minutes = 150
    db_session.commit()
    full_month(db_session, emp)
    permission(db_session, emp, d(11), 90)
    permission(db_session, emp, d(12), 180)
    result = resolve(db_session, emp)
    assert (on(result, d(11)).lop, on(result, d(12)).lop) == (Decimal("0.5"), 1)


def test_permission_on_absent_day_adds_no_extra_lop(db_session, types, emp):
    full_month(db_session, emp, skip={d(11)})
    permission(db_session, emp, d(11), 300)
    result = resolve(db_session, emp)
    assert on(result, d(11)).lop == 1 and on(result, d(11)).lop_reasons == {monthly.LOP_ABSENT: 1}


@pytest.mark.parametrize("rule, lop", [("PRESENT", "0"), ("HALF_DAY", "0.5"), ("ABSENT", "1")])
def test_missing_out_policy(db_session, types, emp, rule, lop):
    policy = ensure_default_policy(db_session)
    policy.missing_out_policy = rule
    db_session.commit()
    full_month(db_session, emp, skip={d(10)})
    punch(db_session, emp, d(10), "09:25", None)
    result = resolve(db_session, emp)
    assert on(result, d(10)).lop == Decimal(lop)
    assert monthly.FLAG_MISSING_OUT in on(result, d(10)).flags
    assert result.totals["missing_punch_days"] == 1
    assert total(result, "missing_punch_lop") == Decimal(lop)
    assert any("missing punch" in w for w in result.warnings)


def test_half_day_leave_with_half_present(db_session, types, emp, hr):
    credit(db_session, emp, types["CL"], 5, 2026)
    full_month(db_session, emp, skip={d(10), d(11)})
    hr_leave(db_session, hr, emp, types["CL"], d(10), half="FIRST_HALF")
    punch(db_session, emp, d(10), "14:00", "18:35")  # 275 worked, 270 required for the half
    hr_leave(db_session, hr, emp, types["CL"], d(11), half="SECOND_HALF")  # no punches: other half absent
    result = resolve(db_session, emp)
    first = on(result, d(10))
    assert first.status == monthly.ST_HALF_DAY
    assert (first.present, first.lop) == (Decimal("0.5"), 0)
    assert not first.late  # the morning is on leave
    second = on(result, d(11))
    assert second.status == monthly.ST_LEAVE
    assert second.lop == Decimal("0.5")
    assert result.totals["paid_leave_by_type"] == {"CL": "1"}
    assert total(result, "total_lop") == Decimal("0.5")


def test_unpaid_leave_type_is_lop(db_session, types, emp, hr):
    full_month(db_session, emp, skip={d(10)})
    hr_leave(db_session, hr, emp, types["LOP"], d(10))
    result = resolve(db_session, emp)
    assert on(result, d(10)).status == monthly.ST_LEAVE
    assert total(result, "unpaid_leave_lop") == 1
    assert total(result, "paid_leave_days") == 0
    assert total(result, "total_lop") == 1


def test_muster_status_codes(db_session, types, emp):
    credit(db_session, emp, types["CL"], 1, 2026)
    special = {d(2): ("A", None), d(3): ("HD", None), d(4): ("HD", "CL"), d(5): ("OD", None), d(6): ("WO", None)}
    for day in working_days(db_session, emp):
        code, leave = special.get(day, ("P", None))
        status(db_session, emp, day, code, leave)
    result = resolve(db_session, emp)
    assert on(result, d(2)).lop == 1 and on(result, d(2)).status == monthly.ST_ABSENT
    assert on(result, d(3)).lop == Decimal("0.5") and on(result, d(3)).status == monthly.ST_HALF_DAY
    hd_cl = on(result, d(4))
    assert (hd_cl.present, hd_cl.lop) == (Decimal("0.5"), 0)
    assert on(result, d(5)).status == monthly.ST_PRESENT
    assert on(result, d(6)).status == monthly.ST_OFF and on(result, d(6)).lop == 0
    assert result.totals["override_leave_debits"] == {"CL": "0.5"}
    assert total(result, "total_lop") == Decimal("1.5")
    assert result.totals["corrected_days"] == 24
    assert result.totals["working_days"] == 23


def test_leave_override_debits_balance_and_excess_is_lop(db_session, types, emp):
    credit(db_session, emp, types["CL"], 1, 2026)
    full_month(db_session, emp, skip={d(10), d(11)})
    status(db_session, emp, d(10), "LEAVE", "CL")
    status(db_session, emp, d(11), "LEAVE", "CL")
    result = resolve(db_session, emp)
    assert result.totals["override_leave_debits"] == {"CL": "1"}
    assert total(result, "no_balance_lop") == 1
    assert total(result, "total_lop") == 1
    assert result.totals["paid_leave_by_type"] == {"CL": "1"}


def test_application_wins_over_leave_override(db_session, types, emp, hr):
    credit(db_session, emp, types["EL"], 3, 2026)
    credit(db_session, emp, types["CL"], 3, 2026)
    full_month(db_session, emp, skip={d(10)})
    hr_leave(db_session, hr, emp, types["EL"], d(10))
    status(db_session, emp, d(10), "LEAVE", "CL")
    result = resolve(db_session, emp)
    day = on(result, d(10))
    assert [(p.code, p.source) for p in day.leave] == [("EL", monthly.LEAVE_FROM_APPLICATION)]
    assert any("ignored" in note for note in day.notes)
    assert result.totals["override_leave_debits"] == {}
    assert total(result, "total_lop") == 0


def _sat_mon_leave(db, hr, emp, leave_type):
    full_month(db, emp, skip={d(21), d(23)})
    hr_leave(db, hr, emp, leave_type, d(21))
    hr_leave(db, hr, emp, leave_type, d(23))


def test_sandwich_across_two_applications_cl(db_session, types, emp, hr):
    credit(db_session, emp, types["CL"], 3, 2026)
    _sat_mon_leave(db_session, hr, emp, types["CL"])
    result = resolve(db_session, emp)
    sunday = on(result, d(22))
    assert sunday.status == monthly.ST_LEAVE
    assert monthly.FLAG_SANDWICH in sunday.flags
    assert result.totals["sandwich_days"] == 1
    assert result.totals["sandwich_debits"] == {"CL": "1"}
    assert result.totals["paid_leave_by_type"] == {"CL": "3"}
    assert total(result, "total_lop") == 0


def test_no_sandwich_for_el(db_session, types, emp, hr):
    credit(db_session, emp, types["EL"], 3, 2026)
    _sat_mon_leave(db_session, hr, emp, types["EL"])
    result = resolve(db_session, emp)
    assert on(result, d(22)).status == monthly.ST_OFF
    assert result.totals["sandwich_days"] == 0
    assert result.totals["sandwich_debits"] == {}


def test_sandwich_without_balance_is_lop(db_session, types, emp, hr):
    credit(db_session, emp, types["CL"], 2, 2026)
    _sat_mon_leave(db_session, hr, emp, types["CL"])
    result = resolve(db_session, emp)
    assert on(result, d(22)).lop == 1
    assert total(result, "no_balance_lop") == 1
    assert result.totals["sandwich_debits"] == {}


def test_sandwich_between_leave_overrides(db_session, types, emp):
    credit(db_session, emp, types["CL"], 3, 2026)
    full_month(db_session, emp, skip={d(21), d(23)})
    status(db_session, emp, d(21), "LEAVE", "CL")
    status(db_session, emp, d(22), "WO")
    status(db_session, emp, d(23), "LEAVE", "CL")
    result = resolve(db_session, emp)
    assert result.totals["override_leave_debits"] == {"CL": "2"}
    assert result.totals["sandwich_debits"] == {"CL": "1"}
    assert total(result, "total_lop") == 0


def test_joining_and_leaving_mid_month(db_session, types):
    joiner = make_employee(db_session, "ST0002", joined=d(16))
    full_month(db_session, joiner, first=d(16))
    result = resolve(db_session, joiner)
    assert on(result, d(1)).status == monthly.ST_NOT_EMPLOYED
    assert total(result, "not_employed_days") == 15
    assert total(result, "total_lop") == 15
    assert total(result, "payroll_present_days") == 15

    leaver = make_employee(db_session, "ST0003", joined=date(2025, 1, 1))
    leaver.date_of_leaving = d(20)
    db_session.commit()
    full_month(db_session, leaver, last=d(20))
    result = resolve(db_session, leaver)
    assert on(result, d(21)).status == monthly.ST_NOT_EMPLOYED
    assert total(result, "not_employed_days") == 10


def test_notice_period_recorded_leave_is_lop(db_session, types, hr):
    emp = make_employee(db_session, "ST0004", joined=date(2025, 1, 1), notice=d(20))
    credit(db_session, emp, types["CL"], 5, 2026)
    full_month(db_session, emp, skip={d(23)})
    _, warnings = hr_leave(db_session, hr, emp, types["CL"], d(23))
    assert warnings
    result = resolve(db_session, emp)
    day = on(result, d(23))
    assert day.status == monthly.ST_LEAVE
    assert day.lop_reasons == {monthly.LOP_NOTICE_LEAVE: 1}
    assert total(result, "notice_leave_lop") == 1
    assert total(result, "paid_leave_days") == 0


def test_worked_on_off_day_is_flagged(db_session, types, emp):
    full_month(db_session, emp)
    punch(db_session, emp, d(8))
    result = resolve(db_session, emp)
    assert on(result, d(8)).status == monthly.ST_OFF
    assert result.totals["worked_on_off_days"] == 1
    assert total(result, "total_lop") == 0


def test_days_after_today_are_upcoming(db_session, types, emp):
    full_month(db_session, emp, last=d(15))
    result = monthly.resolve_month(db_session, emp, NOV, today=d(15))
    assert on(result, d(16)).status == monthly.ST_UPCOMING
    assert total(result, "total_lop") == 0
    assert result.totals["upcoming_days"] == 13
    assert any("in progress" in w for w in result.warnings)


def test_day_nobody_punched_is_not_lop(db_session, types, emp):
    days = working_days(db_session, emp)
    for day in days:
        if day != d(9):
            punch(db_session, emp, day)
    result = resolve(db_session, emp)
    assert on(result, d(9)).status == monthly.ST_NO_DATA
    assert total(result, "total_lop") == 0
    assert result.totals["no_punch_dates"] == ["2026-11-09"]
    assert any("Nobody punched on 09 Nov" in w for w in result.warnings)


def test_absent_while_colleagues_worked_is_lop(db_session, types, emp):
    full_month(db_session, emp, skip={d(9)})
    result = resolve(db_session, emp)
    assert on(result, d(9)).status == monthly.ST_ABSENT
    assert total(result, "absent_lop") == 1


def test_today_without_punches_is_upcoming_not_absent(db_session, types, emp):
    full_month(db_session, emp, last=d(13))
    result = monthly.resolve_month(db_session, emp, NOV, today=d(16))
    assert on(result, d(16)).status == monthly.ST_UPCOMING
    assert on(result, d(16)).lop == 0
    assert total(result, "absent_lop") == 0


# --- process / send to payroll --------------------------------------------------------

def _groups(db):
    seed_pay_groups(db)
    return {g.code: g for g in db.query(PayGroup).all()}


def _profile(db, emp, group, effective_from=date(2026, 1, 1), effective_to=None):
    db.add(EmployeePayProfile(
        employee_id=emp.id, pay_group_id=group.id, monthly_salary=30000, daily_wage=1000, skill_level="Skilled",
        extra={}, effective_from=effective_from, effective_to=effective_to,
    ))
    db.commit()


def _results(db, month=NOV):
    return {r.employee_id: r for r in db.query(AttendanceMonthResult).filter(AttendanceMonthResult.month == month)}


def _attendance_ledger(db, emp, month=NOV):
    return db.query(LeaveLedger).filter(
        LeaveLedger.employee_id == emp.id, LeaveLedger.reference_type == "attendance_month",
        LeaveLedger.reference_id == month,
    ).all()


def test_process_month_scope_is_the_chosen_group(db_session, types, hr):
    groups = _groups(db_session)
    chennai, cctv = groups["CHENNAI_HO"], groups["CCTV"]
    a = make_employee(db_session, "ST0010", joined=date(2025, 1, 1))
    b = make_employee(db_session, "ST0011", joined=date(2025, 1, 1))
    c = make_employee(db_session, "ST0012", joined=date(2025, 1, 1), status="Inactive")
    moved = make_employee(db_session, "ST0013", joined=date(2025, 1, 1))
    _profile(db_session, a, chennai)
    _profile(db_session, b, cctv)
    _profile(db_session, c, chennai)
    _profile(db_session, moved, cctv, effective_to=d(9))
    _profile(db_session, moved, chennai, effective_from=d(10))
    full_month(db_session, a)

    summary = monthly.process_month(db_session, NOV, chennai.id, hr, today=TODAY)
    db_session.commit()
    assert summary["processed"] == 2
    results = _results(db_session)
    assert set(results) == {a.id, moved.id}
    assert results[a.id].lop_days == 0
    assert results[a.id].status == "PROCESSED"
    assert results[moved.id].lop_days == 24
    assert any("No attendance data" in w for w in results[moved.id].warnings)

    with pytest.raises(monthly.MonthlyError) as exc:
        monthly.process_month(db_session, NOV, cctv.id, hr, today=TODAY)
    assert exc.value.status_code == 400
    assert "INPUT" in exc.value.message


def test_send_to_payroll_writes_inputs_and_ledger(db_session, types, hr):
    chennai = _groups(db_session)["CHENNAI_HO"]
    emp = make_employee(db_session, "ST0020", joined=date(2025, 1, 1))
    _profile(db_session, emp, chennai)
    credit(db_session, emp, types["CL"], 5, 2026)
    full_month(db_session, emp, skip={d(3), d(4), d(5), d(10), d(11)})
    for day in (d(3), d(4), d(5)):
        punch(db_session, emp, day, "09:50", "18:50")
    status(db_session, emp, d(10), "LEAVE", "CL")
    db_session.add(VariablePayInput(
        employee_id=emp.id, period_start=d(1), period_end=d(30), lop_days=0, paid_leave_days=0, overtime_hours=0,
        overtime_days=2, ad_hoc_earnings=[{"component_code": "OTHER_PAYMENT", "amount": 500}], ad_hoc_deductions=[],
        source="Manual", status="Draft",
    ))
    db_session.commit()

    monthly.process_month(db_session, NOV, chennai.id, hr, today=TODAY)
    summary = monthly.send_to_payroll(db_session, NOV, chennai.id, hr)
    db_session.commit()
    # d(11) absent (1) + the 3rd late (0.5, straight LOP)
    assert (summary["sent"], summary["total_lop"], summary["inputs_updated"]) == (1, Decimal("1.5"), 1)

    vi = db_session.query(VariablePayInput).filter(VariablePayInput.employee_id == emp.id).one()
    assert (Decimal(vi.lop_days), Decimal(vi.present_days), Decimal(vi.paid_leave_days)) == (
        Decimal("1.5"), Decimal("28.5"), 1)
    assert vi.source == "Attendance"
    assert Decimal(vi.overtime_days) == 2 and vi.ad_hoc_earnings[0]["amount"] == 500

    rows = sorted((r.entry_type, Decimal(r.days)) for r in _attendance_ledger(db_session, emp))
    assert rows == [("LEAVE_DEBIT", Decimal("-1"))]
    from app.services.leave.balance import balance
    assert balance(db_session, emp.id, types["CL"].id, 2026) == Decimal("4")
    assert _results(db_session)[emp.id].status == "SENT"

    # re-send and an unchanged re-process: no duplicate ledger rows, still SENT
    monthly.send_to_payroll(db_session, NOV, chennai.id, hr)
    monthly.process_month(db_session, NOV, chennai.id, hr, today=TODAY)
    db_session.commit()
    assert len(_attendance_ledger(db_session, emp)) == 1
    assert balance(db_session, emp.id, types["CL"].id, 2026) == Decimal("4")
    row = _results(db_session)[emp.id]
    assert row.status == "SENT" and not monthly.changed_since_sent(row)

    # a change after sending -> PROCESSED, changed since sent
    db_session.query(AttendanceDaily).filter(
        AttendanceDaily.work_date == d(12), AttendanceDaily.employee_id == emp.id).delete()
    db_session.commit()
    colleague_punch(db_session, d(12))
    monthly.process_month(db_session, NOV, chennai.id, hr, today=TODAY)
    db_session.commit()
    row = _results(db_session)[emp.id]
    assert row.status == "PROCESSED" and monthly.changed_since_sent(row)
    assert row.lop_days == Decimal("2.5")

    # Locked inputs: 409 and nothing changes
    vi.status = "Locked"
    db_session.commit()
    with pytest.raises(monthly.MonthlyError) as exc:
        monthly.send_to_payroll(db_session, NOV, chennai.id, hr)
    assert exc.value.status_code == 409
    assert exc.value.detail["locked_employees"] == ["ST0020"]
    db_session.rollback()
    assert Decimal(db_session.get(VariablePayInput, vi.id).lop_days) == Decimal("1.5")


# --- API ------------------------------------------------------------------------------

OCT = "2026-10"


def _october_setup(client, db):
    groups = _groups(db)
    chennai = groups["CHENNAI_HO"]
    chennai.definition = {**chennai.definition, "tds_enabled": False}
    db.commit()
    emp = make_employee(db, "ST0030", joined=date(2025, 1, 1))
    resp = client.put(f"{API}/employees/{emp.id}/pay-profile", json={
        "pay_group_id": chennai.id, "monthly_salary": 30000, "effective_from": "2026-01-01",
    })
    assert resp.status_code == 200, resp.text
    full_month(db, emp, skip={d(6, 10), d(7, 10)}, first=d(1, 10), last=d(31, 10))
    return groups, emp


def test_api_process_send_and_chennai_payroll_uses_lop(client, db_session):
    groups, emp = _october_setup(client, db_session)
    chennai = groups["CHENNAI_HO"]

    resp = client.post(f"{API}/attendance/monthly/process", json={"month": OCT, "pay_group_id": chennai.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] == 1 and body["attendance_source"] == "BIOMETRIC"
    row = body["results"][0]
    assert (row["employee_code"], Decimal(row["lop_days"]), row["status"]) == ("ST0030", 2, "PROCESSED")
    assert Decimal(row["totals"]["absent_lop"]) == 2

    listing = client.get(f"{API}/attendance/monthly", params={"month": OCT, "pay_group_id": chennai.id}).json()
    assert [r["employee_id"] for r in listing["results"]] == [emp.id]
    detail = client.get(f"{API}/attendance/monthly/{emp.id}", params={"month": OCT}).json()
    assert len(detail["days"]) == 31 and detail["status"] == "PROCESSED"
    assert next(x for x in detail["days"] if x["date"] == "2026-10-06")["status"] == "ABSENT"

    sent = client.post(f"{API}/attendance/monthly/send-to-payroll", json={"month": OCT, "pay_group_id": chennai.id})
    assert sent.status_code == 200, sent.text
    assert (sent.json()["sent"], Decimal(sent.json()["total_lop"])) == (1, 2)

    schedule = PaySchedule(schedule_name="Monthly", pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
                           holiday_adjustment="Before", is_active=True, applies_to=None)
    db_session.add(schedule)
    db_session.commit()
    run = client.post(f"{API}/payroll-runs", json={
        "pay_schedule_id": schedule.id, "pay_group_id": chennai.id, "period_start": "2026-10-01",
        "period_end": "2026-10-31", "pay_date": "2026-10-31",
    })
    assert run.status_code == 201, run.text
    calc = client.post(f"{API}/payroll-runs/{run.json()['id']}/calculate")
    assert calc.status_code == 200, calc.text
    item = client.get(f"{API}/payroll-runs/{run.json()['id']}/line-items").json()[0]
    assert item["lop_days"] == 2 and item["paid_days"] == 28
    assert item["net_pay"] == 28000  # 30000 x (30 - 2) / 30

    vi = db_session.query(VariablePayInput).filter(VariablePayInput.employee_id == emp.id).one()
    vi.status = "Locked"
    db_session.commit()
    locked = client.post(f"{API}/attendance/monthly/send-to-payroll", json={"month": OCT, "pay_group_id": chennai.id})
    assert locked.status_code == 409
    assert locked.json()["detail"]["locked_employees"] == ["ST0030"]


def test_api_input_group_is_not_processed(client, db_session):
    cctv = _groups(db_session)["CCTV"]
    resp = client.post(f"{API}/attendance/monthly/process", json={"month": OCT, "pay_group_id": cctv.id})
    assert resp.status_code == 400
    assert "INPUT" in resp.json()["detail"]
    groups = client.get(f"{API}/pay-groups").json()
    sources = {g["code"]: g["attendance_source"] for g in groups}
    assert sources["CHENNAI_HO"] == "BIOMETRIC" and sources["CCTV"] == "INPUT"
    assert all(g["params"]["attendance_source"] == g["attendance_source"] for g in groups)
    reference = client.get(f"{API}/pay-groups/formula-reference").json()
    assert reference["attendance_sources"] == ["BIOMETRIC", "MUSTER", "INPUT"]


def test_attendance_source_default_when_key_missing(client, db_session):
    groups = _groups(db_session)
    for group in groups.values():
        group.definition = {k: v for k, v in group.definition.items() if k != "attendance_source"}
        group.params = {k: v for k, v in group.params.items() if k != "attendance_source"}
    db_session.commit()
    sources = {g["code"]: g["attendance_source"] for g in client.get(f"{API}/pay-groups").json()}
    assert sources["CHENNAI_HO"] == "BIOMETRIC"
    assert {v for k, v in sources.items() if k != "CHENNAI_HO"} == {"INPUT"}


def test_me_attendance_returns_only_own_data(client, db_session):
    seed_types(db_session)
    alice = make_employee(db_session, "ST0040", joined=date(2025, 1, 1))
    bob = make_employee(db_session, "ST0041", joined=date(2025, 1, 1))
    make_user(db_session, "alice", employee=alice)
    full_month(db_session, alice, first=d(1, 10), last=d(31, 10))
    full_month(db_session, bob, skip={d(6, 10)}, first=d(1, 10), last=d(31, 10))
    token = token_for(client, "alice")

    resp = client.get(f"{API}/me/attendance", params={"month": OCT}, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["employee_code"] == "ST0040"
    assert len(body["days"]) == 31 and Decimal(body["totals"]["total_lop"]) == 0
    assert "salary" not in resp.text.lower()

    assert client.get(f"{API}/me/attendance", params={"month": "2026-13"}, headers=bearer(token)).status_code == 400
    hr_only = client.get(f"{API}/attendance/monthly/{bob.id}", params={"month": OCT}, headers=bearer(token))
    assert hr_only.status_code == 403
