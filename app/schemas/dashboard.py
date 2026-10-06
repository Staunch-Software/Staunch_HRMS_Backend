from pydantic import BaseModel


class PayrollSummaryResponse(BaseModel):
    """Shaped to match what Dashboard.js's payrollSummary state expects
    (totalPayroll/averageSalary/employeesPaid/taxesWithheld/deductions),
    camelCase via alias since the frontend consumes this JSON directly.
    """

    totalPayroll: float = 0
    averageSalary: float = 0
    employeesPaid: int = 0
    taxesWithheld: float = 0
    deductions: float = 0
