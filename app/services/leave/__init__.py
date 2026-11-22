"""Attendance policy calendar, leave/permission workflow and leave balances.

Contract for monthly attendance processing (day-status resolution, LOP):

calendar (app.services.leave.calendar)
    policy_for(db, employee) -> AttendancePolicy
        Location override (AttendancePolicy.work_location_id == employee's)
        else the default row; an unsaved default if none was ever saved.
    is_weekly_off(policy, day: date, saturday_offs=None) -> bool
        weekday_pattern {"mon".."sun": "WORK"|"OFF"} plus, by
        policy.saturday_off_mode: HR_MONTHLY -> the month's SaturdayOff
        choice (saturday_offs = saturday_off_choices(...); the 2nd Saturday
        when none), FIXED_NTH -> the nth Saturdays in saturdays_off
        (nth = (day.day - 1) // 7 + 1).
    saturday_off_choices(db, work_location_id, date_from, date_to)
        -> dict["YYYY-MM", date]   (a location's choice wins over all-locations)
    saturday_off_gaps(db, policy, work_location_id, date_from, date_to)
        -> [(month, assumed 2nd Saturday)] for months HR has not chosen
    holiday_on(db, day: date, work_location_id: int | None) -> Holiday | None
    day_kind(db, policy, employee, day: date) -> "WORK" | "WEEKLY_OFF" | "HOLIDAY"
        HOLIDAY wins over WEEKLY_OFF.
    day_kinds(db, employee, date_from, date_to, policy=None)
        -> dict[date, tuple[kind, Holiday | None]]   (range version)
    leave_year_of(policy, day) / leave_year_bounds(policy, year)

queries (app.services.leave.queries)
    approved_leave_by_day(db, employee_id, date_from, date_to)
        -> dict[date, list[LeaveDayEntry(leave_type, fraction, half)]]
        Only APPROVED leave; only days that count as leave (working days,
        plus sandwiched offs for types with sandwich_rule). fraction is
        Decimal 1 or 0.5; half is FULL | FIRST_HALF | SECOND_HALF.
        A type with allowed_during_notice False on/after the employee's
        notice_start_date (HR-recorded only) was NOT debited from the
        balance: treat those days as LOP. LeaveType.is_paid False (LOP) is
        unpaid leave and never has ledger rows.
    approved_permission_minutes_by_day(db, employee_id, date_from, date_to)
        -> dict[date, int]   (sum of APPROVED permission minutes)
    approved_permissions_by_day(db, employee_id, date_from, date_to)
        -> dict[date, list[int]]   (minutes of each APPROVED permission)

balance (app.services.leave.balance)
    balance(db, employee_id, leave_type_id, year) -> Decimal
    leave_year_for(db, day) -> int   (company-wide, default policy)
    allocate_from_balances(db, employee_id, year, days, codes)
        -> (list[(LeaveType, Decimal)], lop_days: Decimal)
    Late marks are straight LOP (no LATE_DEBIT ledger rows are written any
    more; old ones still show in the balance summary).
    Policy settings used by processing live on AttendancePolicy:
        shift_start, grace_until, free_lates_per_month, late_deduction_days,
        late_block_size (every started block of chargeable lates costs
        late_deduction_days), wfh_min_notice_days,
        permission_half_day_above_minutes, permission_full_day_above_minutes,
        missing_out_policy (PRESENT | HALF_DAY | ABSENT), saturday_off_mode.
        required_minutes is informational only.
"""
from app.services.leave.calendar import (  # noqa: F401
    day_kind,
    day_kinds,
    holiday_on,
    is_weekly_off,
    policy_for,
)
from app.services.leave.queries import (  # noqa: F401
    LeaveDayEntry,
    approved_leave_by_day,
    approved_permission_minutes_by_day,
)
