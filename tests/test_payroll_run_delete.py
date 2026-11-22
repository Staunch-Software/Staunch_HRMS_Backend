"""Deleting a payroll run: line items and generated bank files go with it;
paid runs and runs with a bank file already submitted stay."""
from datetime import date, datetime, timezone

from app.models.disbursement import DisbursementBatch, DisbursementBatchItem
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from tests.leave_helpers import make_employee
from tests.test_payslips import _run
from tests.test_tds import _schedule

API = "/api/v1"


def _batch(db, run, status="Generated", submitted=False):
    item = db.query(PayrollRunLineItem).filter_by(payroll_run_id=run.id).first()
    batch = DisbursementBatch(
        payroll_run_id=run.id, format_key="icici_salary_xlsx_v1", file_name="test.xlsx", status=status,
        total_amount=1000, total_employee_count=1, generated_at=datetime.now(timezone.utc), checksum="x" * 64,
        file_path="generated_files/does-not-exist-test.xlsx",
        icici_submitted_at=datetime.now(timezone.utc) if submitted else None,
    )
    db.add(batch)
    db.flush()
    db.add(DisbursementBatchItem(
        disbursement_batch_id=batch.id, payroll_run_line_item_id=item.id, employee_code="ST0100",
        beneficiary_name="Asha K", account_number="123456789012", ifsc_code="HDFC0000001", bank_name="HDFC Bank",
        amount=1000,
    ))
    db.commit()
    return batch


def _setup(db):
    employee = make_employee(db, "ST0100", first="Asha", joined=date(2024, 6, 1))
    return employee, _schedule(db)


def test_delete_calculated_run_with_generated_bank_file(client, db_session):
    employee, schedule = _setup(db_session)
    run = _run(db_session, schedule, employee, date(2026, 9, 30), "Approved")
    _batch(db_session, run)
    response = client.delete(f"{API}/payroll-runs/{run.id}")
    assert response.status_code == 200, response.text
    assert response.json()["bank_files_removed"] == 1
    db_session.expire_all()
    assert db_session.get(PayrollRun, run.id) is None
    assert db_session.query(PayrollRunLineItem).count() == 0
    assert db_session.query(DisbursementBatch).count() == 0
    assert db_session.query(DisbursementBatchItem).count() == 0


def test_paid_run_cannot_be_deleted(client, db_session):
    employee, schedule = _setup(db_session)
    run = _run(db_session, schedule, employee, date(2026, 9, 30), "Paid")
    assert client.delete(f"{API}/payroll-runs/{run.id}").status_code == 409
    assert db_session.get(PayrollRun, run.id) is not None


def test_run_with_submitted_bank_file_cannot_be_deleted(client, db_session):
    employee, schedule = _setup(db_session)
    run = _run(db_session, schedule, employee, date(2026, 9, 30), "Approved")
    _batch(db_session, run, submitted=True)
    assert client.delete(f"{API}/payroll-runs/{run.id}").status_code == 409
    assert client.delete(f"{API}/payroll-runs/999999").status_code == 404
