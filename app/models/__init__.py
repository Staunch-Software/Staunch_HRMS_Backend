"""SQLAlchemy models. Import every module here so Base.metadata sees all
tables — this matters for both `create_all()` in db/init_db.py and for
Alembic's `--autogenerate`, which only sees models that have been imported
by the time env.py runs.
"""
from app.models.base import Base  # noqa: F401
from app.models.user import AdminUser  # noqa: F401
from app.models.org import Department, Designation, WorkLocation  # noqa: F401
from app.models.employee import Employee, EmployeeBankAccount  # noqa: F401
from app.models.salary_component import SalaryComponent  # noqa: F401
from app.models.pay_schedule import PaySchedule  # noqa: F401
from app.models.salary_structure import EmployeeSalaryStructureAssignment  # noqa: F401
from app.models.statutory import (  # noqa: F401
    ESIConfig,
    EmployeeTaxDeclaration,
    PFConfig,
    PTSlab,
    PTState,
    TDSConfig,
    TDSSlab,
)
from app.models.variable_pay import VariablePayInput  # noqa: F401
from app.models.payroll_run import PayrollRun, PayrollRunLineItem  # noqa: F401
from app.models.company_bank_config import CompanyBankConfig  # noqa: F401
from app.models.disbursement import DisbursementBatch, DisbursementBatchItem  # noqa: F401

__all__ = [
    "Base",
    "AdminUser",
    "Department",
    "Designation",
    "WorkLocation",
    "Employee",
    "EmployeeBankAccount",
    "SalaryComponent",
    "PaySchedule",
    "EmployeeSalaryStructureAssignment",
    "PFConfig",
    "ESIConfig",
    "PTState",
    "PTSlab",
    "TDSSlab",
    "TDSConfig",
    "EmployeeTaxDeclaration",
    "VariablePayInput",
    "PayrollRun",
    "PayrollRunLineItem",
    "CompanyBankConfig",
    "DisbursementBatch",
    "DisbursementBatchItem",
]
