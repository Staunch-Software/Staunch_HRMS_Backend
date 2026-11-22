"""Approved WFH in monthly attendance processing and the dashboard."""
from datetime import date
from decimal import Decimal

from app.models.attendance_policy import Holiday
from app.services.attendance import monthly
from app.services.dashboard.attendance import attendance_overview
from app.services.leave.wfh import record_wfh_on_behalf
from app.services.leave.workflow import record_leave_on_behalf
from tests.leave_helpers import credit, make_employee, seed_types
from tests.test_attendance_monthly import d, full_month, on, punch, resolve, total

TODAY = date(2026, 11, 2)


def record(db, hr, emp, start, end=None, half="FULL"):
    request, _ = record_wfh_on_behalf(db, hr_user=hr, employee_id=emp.id, from_date=start, to_date=end or start,
                                      half=half, reason=None, today=TODAY)
    db.commit()
    return request


def test_wfh_days_in_monthly_processing(db_session, seeded_admin):
    db, hr = db_session, seeded_admin
    seed_types(db)
    emp = make_employee(db, "ST0001", joined=date(2025, 1, 1))
    record(db, hr, emp, d(10))  # full day, no punch
    record(db, hr, emp, d(11), half="FIRST_HALF")  # afternoon punched in late: no late mark
    record(db, hr, emp, d(12), half="SECOND_HALF")  # morning not punched: 0.5 absent
    record(db, hr, emp, d(13))  # also punched late: still WFH
    record(db, hr, emp, d(16))  # declared a holiday afterwards
    db.add(Holiday(date=d(16), name="Local festival", is_active=True))
    db.commit()
    full_month(db, emp, skip={d(10), d(11), d(12), d(13)})
    punch(db, emp, d(11), "14:00", "18:30")
    punch(db, emp, d(13), "10:15", "18:30")

    result = resolve(db, emp)
    full, first, second, punched, holiday = (on(result, day) for day in (d(10), d(11), d(12), d(13), d(16)))
    assert full.status == monthly.ST_WFH and full.present == 1 and full.lop == 0 and full.wfh == 1
    assert first.status == monthly.ST_PRESENT and first.wfh == Decimal("0.5") and not first.late
    assert monthly.FLAG_WFH in first.flags and first.wfh_half == "FIRST_HALF"
    assert second.status == monthly.ST_HALF_DAY and second.lop == Decimal("0.5")
    assert punched.status == monthly.ST_WFH and not punched.late
    assert any("also punched" in n for n in punched.notes)
    assert holiday.status == monthly.ST_HOLIDAY and holiday.wfh == 0
    assert any("WFH ignored" in n for n in holiday.notes)
    assert full.to_json()["wfh"] == "1" and full.to_json()["status"] == "WFH"

    assert total(result, "wfh_days") == Decimal("3")
    assert total(result, "present_days") == Decimal("22.5")  # 23 working days, half a day absent
    assert total(result, "total_lop") == Decimal("0.5")
    assert result.totals["late_count"] == 0


def test_wfh_next_to_half_day_leave(db_session, seeded_admin):
    db, hr = db_session, seeded_admin
    types = seed_types(db)
    emp = make_employee(db, "ST0002", joined=date(2025, 1, 1))
    credit(db, emp, types["CL"], 5, 2026)
    record_leave_on_behalf(db, hr_user=hr, employee_id=emp.id, leave_type_id=types["CL"].id, from_date=d(10),
                           from_half="FIRST_HALF", to_date=d(10), to_half="FIRST_HALF", reason=None, today=TODAY)
    record(db, hr, emp, d(10), half="SECOND_HALF")
    full_month(db, emp, skip={d(10)})
    day = on(resolve(db, emp), d(10))
    assert day.lop == 0 and day.wfh == Decimal("0.5") and day.present == Decimal("0.5")
    assert day.leave[0].paid_days == Decimal("0.5")


def test_dashboard_counts_approved_wfh_as_present(db_session, seeded_admin):
    db, hr = db_session, seeded_admin
    seed_types(db)
    office = make_employee(db, "E1", "Office")
    home = make_employee(db, "E2", "Home")
    punch(db, office, d(18), "09:20", "18:30")
    record(db, hr, home, d(18))
    today, _ = attendance_overview(db, d(18))
    assert today["present"] == 2 and today["expected"] == 2 and today["percent"] == 100.0
