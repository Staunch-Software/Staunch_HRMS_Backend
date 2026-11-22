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
from app.models.pay_group import EmployeePayProfile, PayGroup, PayGroupDefinitionVersion  # noqa: F401
from app.models.variable_pay import VariablePayInput  # noqa: F401
from app.models.payroll_run import PayrollRun, PayrollRunLineItem  # noqa: F401
from app.models.company_bank_config import CompanyBankConfig  # noqa: F401
from app.models.disbursement import DisbursementBatch, DisbursementBatchItem  # noqa: F401
from app.models.attendance import (  # noqa: F401
    AttendanceDaily,
    AttendanceDevice,
    AttendanceOverride,
    AttendancePunch,
    AttendanceSyncRun,
)
from app.models.attendance_policy import AttendancePolicy, Holiday, SaturdayOff  # noqa: F401
from app.models.leave import LeaveApplication, LeaveLedger, LeaveType, PermissionRequest  # noqa: F401
from app.models.wfh import WfhRequest  # noqa: F401
from app.models.regularization import AttendanceRegularization  # noqa: F401
from app.models.attendance_month import AttendanceMonthResult  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.mobile_attendance import (  # noqa: F401
    MobileAttendanceSettings,
    MobileDevice,
    MobilePunch,
    OfficeLocation,
)
from app.models.payroll_settings import PayrollRoundingSettings, PayslipSettings  # noqa: F401
from app.models.approval_settings import ApprovalSettings  # noqa: F401

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
    "PayGroup",
    "PayGroupDefinitionVersion",
    "EmployeePayProfile",
    "VariablePayInput",
    "PayrollRun",
    "PayrollRunLineItem",
    "CompanyBankConfig",
    "DisbursementBatch",
    "DisbursementBatchItem",
    "AttendancePunch",
    "AttendanceDaily",
    "AttendanceSyncRun",
    "AttendanceOverride",
    "AttendanceDevice",
    "AttendancePolicy",
    "Holiday",
    "SaturdayOff",
    "LeaveType",
    "LeaveApplication",
    "PermissionRequest",
    "WfhRequest",
    "AttendanceRegularization",
    "LeaveLedger",
    "AttendanceMonthResult",
    "Notification",
    "OfficeLocation",
    "MobileDevice",
    "MobilePunch",
    "MobileAttendanceSettings",
    "PayrollRoundingSettings",
    "PayslipSettings",
    "ApprovalSettings",
]
