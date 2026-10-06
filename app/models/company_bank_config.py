from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CompanyBankConfig(Base, TimestampMixin):
    """The company's own debit/current account used as the source account on
    every generated disbursement file. Singleton-style settings table — only
    one row is expected to have is_active=True at a time (same convention as
    EmployeeBankAccount.is_primary: "most recent active row wins", not
    enforced by a DB constraint).

    seed_all.py seeds one row with obviously-fake placeholder values
    (account "0000000000"). That row MUST be replaced with the company's
    real ICICI current account details (via PUT /company-bank-config)
    before generating a bank file used for an actual salary disbursement.
    """
    __tablename__ = "company_bank_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Short internal code used in generated filenames, e.g. "STAUNCH".
    company_code: Mapped[str] = mapped_column(String(30), nullable=False)
    debit_account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    debit_account_bank_name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
