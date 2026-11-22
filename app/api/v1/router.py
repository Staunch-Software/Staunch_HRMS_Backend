from fastapi import APIRouter, Depends

from app.api.v1 import (
    approval_settings,
    attendance,
    attendance_import,
    attendance_monthly,
    attendance_settings,
    auth,
    dashboard,
    disbursement,
    employee_import,
    employee_logins,
    employees,
    icici_bulk,
    leave_admin,
    me,
    me_leave,
    me_mobile,
    me_regularization,
    me_wfh,
    mobile_attendance_admin,
    notifications,
    org,
    pay_groups,
    pay_schedules,
    payroll_import,
    payroll_run_uploads,
    payroll_runs,
    payroll_settings,
    payslips,
    regularization_admin,
    salary_components,
    salary_structure,
    statutory,
    variable_inputs,
    wfh_admin,
)
from app.core.security import require_hr

# Every router below is HR-only. Each still declares its own
# get_current_user dependency; require_hr adds the role check on top.
HR_ROUTERS = (
    employees,
    employee_import,
    employee_logins,
    org,
    salary_components,
    pay_schedules,
    pay_groups,
    dashboard,
    salary_structure,
    statutory,
    variable_inputs,
    payroll_runs,
    payroll_run_uploads,
    payroll_import,
    disbursement,
    icici_bulk,
    attendance,
    attendance_import,
    attendance_settings,
    attendance_monthly,
    leave_admin,
    wfh_admin,
    regularization_admin,
    mobile_attendance_admin,
    approval_settings,
    payroll_settings,
    payslips,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(me_leave.router)
api_router.include_router(me_wfh.router)
api_router.include_router(me_regularization.router)
api_router.include_router(me_mobile.router)
api_router.include_router(payslips.me_router)
api_router.include_router(notifications.router)
for module in HR_ROUTERS:
    api_router.include_router(module.router, dependencies=[Depends(require_hr)])
