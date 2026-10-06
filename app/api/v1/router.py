from fastapi import APIRouter

from app.api.v1 import (
    auth,
    dashboard,
    disbursement,
    employees,
    icici_bulk,
    org,
    pay_schedules,
    payroll_run_uploads,
    payroll_runs,
    salary_components,
    salary_structure,
    statutory,
    variable_inputs,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(employees.router)
api_router.include_router(org.router)
api_router.include_router(salary_components.router)
api_router.include_router(pay_schedules.router)
api_router.include_router(dashboard.router)
api_router.include_router(salary_structure.router)
api_router.include_router(statutory.router)
api_router.include_router(variable_inputs.router)
api_router.include_router(payroll_runs.router)
api_router.include_router(payroll_run_uploads.router)
api_router.include_router(disbursement.router)
api_router.include_router(icici_bulk.router)
