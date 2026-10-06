"""Orchestrates all Phase 1 seed data: admin user, departments/designations/
work-locations, and salary components. Safe to re-run — every step checks
for existing rows before inserting.
"""
import secrets
import string

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.company_bank_config import CompanyBankConfig
from app.models.org import Department, Designation, WorkLocation
from app.models.user import AdminUser
from app.db.seed.salary_components_seed import seed_salary_components
from app.db.seed.statutory_seed import seed_statutory_all

DEPARTMENTS_SEED = [
    {"name": "Engineering", "code": "ENG", "description": "Software & Hardware", "status": "Active"},
    {"name": "Sales", "code": "SALES", "description": "Revenue & Customer Acquisition", "status": "Active"},
    {"name": "Marketing", "code": "MKT", "description": "Sales & Promotion", "status": "Active"},
    {"name": "Human Resources", "code": "HR", "description": "People Operations", "status": "Active"},
    {"name": "Operations", "code": "OPS", "description": "Business Operations", "status": "Active"},
]

# state_code matters for Phase 2's Professional Tax lookup — get it right.
WORK_LOCATIONS_SEED = [
    {"name": "Chennai", "state_code": "TN", "status": "Active"},
    {"name": "Mumbai (HQ)", "state_code": "MH", "status": "Active"},
    {"name": "Delhi", "state_code": "DL", "status": "Active"},
    {"name": "Bengaluru", "state_code": "KA", "status": "Active"},
]


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def seed_admin_user(db) -> None:
    existing = db.query(AdminUser).filter(AdminUser.username == "admin").first()
    if existing:
        print("Admin user 'admin' already exists — skipping (password unchanged).")
        return
    generated_password = _generate_password()
    admin = AdminUser(
        username="admin",
        hashed_password=hash_password(generated_password),
        is_active=True,
    )
    db.add(admin)
    db.commit()
    print("=" * 72)
    print("Seeded admin user:")
    print("  username: admin")
    print(f"  password: {generated_password}")
    print("Store this password now — it is not saved anywhere else in plaintext.")
    print("=" * 72)


def seed_departments(db) -> dict[str, Department]:
    result = {}
    for data in DEPARTMENTS_SEED:
        dept = db.query(Department).filter(Department.code == data["code"]).first()
        if not dept:
            dept = Department(**data)
            db.add(dept)
            db.commit()
            db.refresh(dept)
        result[dept.code] = dept
    return result


def seed_designations(db, departments: dict[str, Department]) -> None:
    designations_seed = [
        {"name": "Frontend Developer", "department_code": "ENG", "status": "Active"},
        {"name": "Backend Developer", "department_code": "ENG", "status": "Active"},
        {"name": "QA Engineer", "department_code": "ENG", "status": "Active"},
        {"name": "Sales Executive", "department_code": "SALES", "status": "Active"},
        {"name": "Recruiter", "department_code": "HR", "status": "Active"},
    ]
    for data in designations_seed:
        dept = departments.get(data["department_code"])
        if not dept:
            continue
        exists = (
            db.query(Designation)
            .filter(Designation.name == data["name"], Designation.department_id == dept.id)
            .first()
        )
        if exists:
            continue
        db.add(Designation(name=data["name"], department_id=dept.id, status=data["status"]))
    db.commit()


def seed_work_locations(db) -> None:
    for data in WORK_LOCATIONS_SEED:
        exists = db.query(WorkLocation).filter(WorkLocation.name == data["name"]).first()
        if exists:
            continue
        db.add(WorkLocation(**data))
    db.commit()


def seed_company_bank_config(db) -> None:
    """Phase 4: the company's own debit/current account used on every
    generated bank disbursement file. Seeds ONE obviously-fake placeholder
    row — a real account number is never invented/guessed here."""
    existing = db.query(CompanyBankConfig).filter(CompanyBankConfig.is_active.is_(True)).first()
    if existing:
        print("Company bank config already seeded — skipping.")
        return
    db.add(
        CompanyBankConfig(
            company_code="STAUNCH",
            debit_account_number="0000000000",
            debit_account_bank_name="ICICI Bank (PLACEHOLDER)",
            is_active=True,
        )
    )
    db.commit()
    print(
        "Seeded a PLACEHOLDER company_bank_config row (account 0000000000, "
        "company_code STAUNCH) — this MUST be replaced with the company's "
        "real ICICI current account details (PUT /company-bank-config) "
        "before generating a real salary disbursement file."
    )


def seed_all() -> None:
    db = SessionLocal()
    try:
        seed_admin_user(db)
        departments = seed_departments(db)
        seed_designations(db, departments)
        seed_work_locations(db)
        seed_salary_components(db)
        seed_statutory_all(db)
        seed_company_bank_config(db)
        print("Seed complete.")
        print(
            "NOTE: all seeded Professional Tax and TDS slabs are marked "
            "is_verified=False — confirm against current state/central law "
            "before running live payroll."
        )
        print(
            "NOTE: the seeded bank disbursement format (generic_neft_xlsx_v1) "
            "is a placeholder, not ICICI-verified, and the seeded company "
            "debit-account details are fake placeholders — replace both "
            "before running a real salary disbursement."
        )
    finally:
        db.close()


if __name__ == "__main__":
    import app.models  # noqa: F401  (register models before querying)

    seed_all()
