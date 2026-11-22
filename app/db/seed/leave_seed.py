"""Default attendance policy and leave types (mirrors migrations c90ae2316d3c
and 8675f75c1fcf for create_all() dev databases). CL is credited monthly by
length of service; the other paid types start at 0 days: HR must enter the
real entitlements."""
from decimal import Decimal

from app.models.leave import LeaveType
from app.services.leave.calendar import ensure_default_policy

CL_SERVICE_TIERS = [
    {"min_service_months": 0, "days_per_month": 1, "carry_forward": False, "carry_forward_limit": None},
    {"min_service_months": 12, "days_per_month": 2, "carry_forward": True, "carry_forward_limit": None},
]

DEFAULT_LEAVE_TYPES = [
    {"code": "CL", "name": "Casual Leave", "sort_order": 1, "credit_mode": "MONTHLY_TIERED",
     "service_tiers": CL_SERVICE_TIERS, "joining_cutoff_day": 15},
    {"code": "SL", "name": "Sick Leave", "sort_order": 2},
    {"code": "EL", "name": "Earned Leave", "sort_order": 3, "sandwich_rule": False},
    {"code": "LOP", "name": "Loss of Pay", "sort_order": 4, "is_paid": False, "allow_negative_balance": True},
]


def seed_leave_defaults(db) -> None:
    ensure_default_policy(db)
    for data in DEFAULT_LEAVE_TYPES:
        if db.query(LeaveType).filter(LeaveType.code == data["code"]).first():
            continue
        values = {
            "annual_entitlement": Decimal("0"), "credit_mode": "YEARLY_UPFRONT", "pro_rata_for_joiners": True,
            "carry_forward": False, "is_paid": True, "allow_half_day": True, "sandwich_rule": True,
            "allowed_during_notice": False, "allow_negative_balance": False, "is_active": True,
        }
        values.update(data)
        db.add(LeaveType(**values))
    db.commit()
