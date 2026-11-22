from datetime import date

from app.models.attendance_policy import AttendancePolicy, Holiday
from app.services.leave.calendar import (
    KIND_HOLIDAY,
    KIND_WEEKLY_OFF,
    KIND_WORK,
    day_kind,
    day_kinds,
    default_policy,
    ensure_default_policy,
    holiday_on,
    is_weekly_off,
    policy_for,
)
from tests.leave_helpers import make_employee, make_location


def test_default_policy_without_a_saved_row(db_session):
    policy = default_policy(db_session)
    assert policy.id is None
    assert policy.shift_start.strftime("%H:%M") == "09:30" and policy.grace_until.strftime("%H:%M") == "09:35"
    assert policy.required_minutes == 540 and policy.saturdays_off == [2]
    assert policy.saturday_off_mode == "HR_MONTHLY"
    assert (policy.permission_half_day_above_minutes, policy.permission_full_day_above_minutes) == (120, 240)


def test_weekly_off_sunday_and_second_saturday(db_session):
    policy = ensure_default_policy(db_session)
    assert is_weekly_off(policy, date(2026, 11, 8))  # Sunday
    assert is_weekly_off(policy, date(2026, 11, 14))  # 2nd Saturday
    assert not is_weekly_off(policy, date(2026, 11, 7))  # 1st Saturday
    assert not is_weekly_off(policy, date(2026, 11, 21))  # 3rd Saturday
    assert not is_weekly_off(policy, date(2026, 11, 9))  # Monday

    policy.saturday_off_mode = "FIXED_NTH"
    policy.saturdays_off = [2, 4]
    assert is_weekly_off(policy, date(2026, 11, 28))  # 4th Saturday
    policy.weekday_pattern = {**policy.weekday_pattern, "sat": "OFF"}
    assert is_weekly_off(policy, date(2026, 11, 7))


def test_holidays_company_wide_and_by_location(db_session):
    chennai = make_location(db_session, "Chennai")
    mumbai = make_location(db_session, "Mumbai", "MH")
    at_chennai = make_employee(db_session, "E1", location=chennai)
    at_mumbai = make_employee(db_session, "E2", location=mumbai)
    db_session.add_all([
        Holiday(date=date(2026, 11, 16), name="Company Day", work_location_id=None, is_active=True),
        Holiday(date=date(2026, 11, 17), name="Chennai Day", work_location_id=chennai.id, is_active=True),
        Holiday(date=date(2026, 11, 18), name="Old Holiday", work_location_id=None, is_active=False),
        Holiday(date=date(2026, 11, 8), name="Sunday Holiday", work_location_id=None, is_active=True),
    ])
    db_session.commit()
    policy = default_policy(db_session)

    assert holiday_on(db_session, date(2026, 11, 16), mumbai.id).name == "Company Day"
    assert holiday_on(db_session, date(2026, 11, 17), chennai.id).name == "Chennai Day"
    assert holiday_on(db_session, date(2026, 11, 17), mumbai.id) is None
    assert holiday_on(db_session, date(2026, 11, 17), None) is None

    assert day_kind(db_session, policy, at_chennai, date(2026, 11, 17)) == KIND_HOLIDAY
    assert day_kind(db_session, policy, at_mumbai, date(2026, 11, 17)) == KIND_WORK
    assert day_kind(db_session, policy, at_mumbai, date(2026, 11, 18)) == KIND_WORK  # inactive holiday
    assert day_kind(db_session, policy, at_mumbai, date(2026, 11, 14)) == KIND_WEEKLY_OFF
    assert day_kind(db_session, policy, at_mumbai, date(2026, 11, 8)) == KIND_HOLIDAY  # holiday wins

    kinds = day_kinds(db_session, at_chennai, date(2026, 11, 14), date(2026, 11, 17))
    assert [k for k, _ in kinds.values()] == [KIND_WEEKLY_OFF, KIND_WEEKLY_OFF, KIND_HOLIDAY, KIND_HOLIDAY]
    assert kinds[date(2026, 11, 17)][1].name == "Chennai Day"


def test_location_override_policy(db_session):
    chennai = make_location(db_session, "Chennai")
    plant = make_location(db_session, "Plant", "TN")
    office = make_employee(db_session, "E1", location=chennai)
    factory = make_employee(db_session, "E2", location=plant)
    nowhere = make_employee(db_session, "E3")
    default = ensure_default_policy(db_session)
    override = AttendancePolicy(
        work_location_id=plant.id, shift_start=default.shift_start, grace_until=default.grace_until,
        required_minutes=480, weekday_pattern={**default.weekday_pattern, "sat": "OFF"}, saturdays_off=[],
        free_lates_per_month=2, late_deduction_days=default.late_deduction_days,
        permission_monthly_minutes=120, permission_max_per_month=2, permission_max_minutes_each=60,
        permission_half_day_above_minutes=120, permission_full_day_above_minutes=240,
        missing_out_policy="PRESENT", min_notice_days=1, leave_year_start_month=1,
        effective_from=date(2026, 11, 1),
    )
    db_session.add(override)
    db_session.commit()

    assert policy_for(db_session, factory).id == override.id
    assert policy_for(db_session, office).id == default.id
    assert policy_for(db_session, nowhere).id == default.id
    assert day_kind(db_session, policy_for(db_session, factory), factory, date(2026, 11, 7)) == KIND_WEEKLY_OFF
    assert day_kind(db_session, policy_for(db_session, office), office, date(2026, 11, 7)) == KIND_WORK
    # The override has no 2nd-Saturday rule but all Saturdays are off anyway.
    assert day_kind(db_session, policy_for(db_session, factory), factory, date(2026, 11, 14)) == KIND_WEEKLY_OFF
