from datetime import date
from decimal import Decimal

import pytest

from app.models.attendance_policy import Holiday
from app.services.leave.days import LeaveDaysError, count_leave_days, half_slots, leave_day_breakdown
from tests.leave_helpers import make_employee, seed_types

FRI, SAT2, SUN, MON, TUE, WED = (date(2026, 11, d) for d in (13, 14, 15, 16, 17, 18))


@pytest.fixture()
def setup(db_session):
    types = seed_types(db_session)
    employee = make_employee(db_session, "E1")
    return db_session, employee, types


def count(db, employee, leave_type, start, end, from_half="FULL", to_half="FULL"):
    return count_leave_days(db, employee, leave_type, start, from_half, end, to_half)


def test_sandwich_rule_on_and_off(setup):
    db, employee, types = setup
    # Fri -> Mon across the 2nd Saturday and Sunday.
    assert count(db, employee, types["CL"], FRI, MON) == Decimal("4")
    assert count(db, employee, types["EL"], FRI, MON) == Decimal("2")
    breakdown = leave_day_breakdown(db, employee, types["CL"], FRI, "FULL", MON, "FULL")
    assert [d.sandwiched for d in breakdown] == [False, True, True, False]


def test_sandwiched_holiday(setup):
    db, employee, types = setup
    db.add(Holiday(date=TUE, name="Festival", is_active=True))
    db.commit()
    assert count(db, employee, types["CL"], MON, WED) == Decimal("3")
    assert count(db, employee, types["EL"], MON, WED) == Decimal("2")
    assert count(db, employee, types["CL"], TUE, TUE) == Decimal("0")


def test_half_days(setup):
    db, employee, types = setup
    assert count(db, employee, types["CL"], MON, MON, "FIRST_HALF") == Decimal("0.5")
    assert count(db, employee, types["CL"], MON, MON, "SECOND_HALF", "FULL") == Decimal("0.5")
    assert count(db, employee, types["CL"], FRI, MON, "SECOND_HALF", "FIRST_HALF") == Decimal("3")
    assert count(db, employee, types["EL"], FRI, MON, "SECOND_HALF", "FIRST_HALF") == Decimal("1")
    assert count(db, employee, types["CL"], MON, TUE, "SECOND_HALF", "FIRST_HALF") == Decimal("1")
    with pytest.raises(LeaveDaysError):
        count(db, employee, types["CL"], MON, TUE, "FIRST_HALF", "FULL")
    with pytest.raises(LeaveDaysError):
        count(db, employee, types["CL"], MON, TUE, "FULL", "SECOND_HALF")
    with pytest.raises(LeaveDaysError):
        count(db, employee, types["CL"], TUE, MON)


def test_leading_and_trailing_offs_never_count(setup):
    db, employee, types = setup
    assert count(db, employee, types["CL"], SAT2, MON) == Decimal("1")
    assert count(db, employee, types["CL"], FRI, SUN) == Decimal("1")
    assert count(db, employee, types["CL"], SAT2, SUN) == Decimal("0")
    # Sun 8 .. Mon 16: Sun 8 leading; Sat 14 and Sun 15 sandwiched.
    assert count(db, employee, types["CL"], date(2026, 11, 8), date(2026, 11, 16)) == Decimal("8")
    # Sat 7 is the 1st Saturday (working), so Sun 8 is sandwiched too.
    assert count(db, employee, types["CL"], date(2026, 11, 7), date(2026, 11, 16)) == Decimal("10")


def test_half_slots():
    assert half_slots(MON, "FULL", MON, "FULL") == {(MON, 1), (MON, 2)}
    assert half_slots(MON, "SECOND_HALF", TUE, "FIRST_HALF") == {(MON, 2), (TUE, 1)}
    assert not half_slots(MON, "FIRST_HALF", MON, "FIRST_HALF") & half_slots(MON, "SECOND_HALF", MON, "SECOND_HALF")
