"""Tests for the Part 3 integration endpoints in app/api/v1/icici_bulk.py:
GET /icici/config-status, POST .../icici/submit (two-step OTP flow), and
GET .../icici/status. ICICIClient's network methods are monkeypatched —
these tests never construct a real httpx transport, so there is no
possibility of a real call reaching ICICI even indirectly.
"""
from datetime import date

import pytest

from app.core.config import settings as app_settings
from app.models.company_bank_config import CompanyBankConfig
from app.models.disbursement import DisbursementBatch
from app.models.employee import Employee, EmployeeBankAccount
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.services.icici_api.client import ICICIClient
from app.services.icici_api.exceptions import ICICIBulkPaymentError, ICICIOTPError

PERIOD_START = date(2026, 9, 1)
PERIOD_END = date(2026, 9, 30)

REQUIRED_ICICI_FIELDS = [
    "ICICI_API_KEY", "ICICI_CORP_ID", "ICICI_USER_ID", "ICICI_AGGR_ID", "ICICI_AGGR_NAME", "ICICI_URN",
]


@pytest.fixture()
def configured_icici(monkeypatch):
    for field in REQUIRED_ICICI_FIELDS:
        monkeypatch.setattr(app_settings, field, f"test-{field.lower()}")
    yield app_settings


def _seed_company_bank_config(db_session) -> CompanyBankConfig:
    config = CompanyBankConfig(
        company_code="STAUNCH",
        debit_account_number="000705000961",
        debit_account_bank_name="ICICI Bank",
        is_active=True,
    )
    db_session.add(config)
    db_session.commit()
    return config


def _make_schedule(db_session) -> PaySchedule:
    schedule = PaySchedule(
        schedule_name="Monthly - All",
        pay_frequency="Monthly",
        payment_day_rule={"type": "LastDayOfMonth"},
        holiday_adjustment="Before",
        is_active=True,
        applies_to=None,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    return schedule


def _make_run(db_session, schedule) -> PayrollRun:
    run = PayrollRun(
        pay_schedule_id=schedule.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        pay_date=date(2026, 10, 5),
        run_type="Regular",
        status="Approved",
        source="InAppCalculation",
        total_gross=0,
        total_deductions=0,
        total_net=0,
        employee_count=0,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_employee_with_line_item(db_session, run, code, account_number, ifsc, net_pay):
    employee = Employee(
        employee_code=code,
        first_name=code,
        status="Active",
        date_of_joining=date(2025, 1, 1),
        basic_salary=50000,
    )
    db_session.add(employee)
    db_session.commit()
    db_session.refresh(employee)

    db_session.add(
        EmployeeBankAccount(
            employee_id=employee.id,
            bank_name="ICICI Bank",
            account_holder_name=f"{code} Holder",
            account_number=account_number,
            ifsc_code=ifsc,
            branch_name="Main Branch",
            account_type="Savings",
            is_primary=True,
            is_active=True,
        )
    )
    db_session.commit()

    db_session.add(
        PayrollRunLineItem(
            payroll_run_id=run.id,
            employee_id=employee.id,
            employee_snapshot={"employee_code": code, "name": code, "status": "Active"},
            gross_earnings=net_pay,
            total_deductions=0,
            total_reimbursements=0,
            net_pay=net_pay,
            status="Included",
            computation_breakdown=[],
            error_message=None,
        )
    )
    db_session.commit()


def _generate_icici_batch(client, run_id):
    resp = client.post(
        f"/api/v1/payroll-runs/{run_id}/disbursement-batches", json={"format_key": "icici_bulk_salary_txt_v1"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_config_status_reports_missing_fields_when_unset(client):
    resp = client.get("/api/v1/icici/config-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False
    assert set(data["missing_fields"]) == set(REQUIRED_ICICI_FIELDS)


def test_config_status_reports_configured_when_set(client, configured_icici):
    resp = client.get("/api/v1/icici/config-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["missing_fields"] == []


def test_submit_returns_503_when_not_configured(client, db_session):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC1", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit", json={}
    )
    assert resp.status_code == 503
    assert "relationship manager" in resp.json()["detail"]


def test_submit_rejects_wrong_format_key(client, db_session, configured_icici):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC2", "1111111111", "HDFC0001234", 50000)

    resp = client.post(f"/api/v1/payroll-runs/{run.id}/disbursement-batches", json={})  # generic_neft_xlsx_v1
    assert resp.status_code == 201
    batch = resp.json()

    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit", json={}
    )
    assert resp.status_code == 400
    assert "icici_bulk_salary_txt_v1" in resp.json()["detail"]


def test_submit_two_step_otp_flow_then_status_check(client, db_session, configured_icici, monkeypatch):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC3", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    captured_otp_unique_id = {}

    def fake_create_otp(self, unique_id):
        captured_otp_unique_id["value"] = unique_id
        return {"MESSAGE": "Success", "ResponseCode": "0000", "RESPONSE": "Success"}

    monkeypatch.setattr(ICICIClient, "create_otp", fake_create_otp)

    # Step 1: no OTP yet -> triggers Create OTP, returns otp_requested=True.
    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit", json={}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["otp_requested"] is True
    assert data["unique_id"] == captured_otp_unique_id["value"]

    db_batch = db_session.query(DisbursementBatch).filter(DisbursementBatch.id == int(batch["id"])).first()
    db_session.refresh(db_batch)
    assert db_batch.icici_pending_unique_id == captured_otp_unique_id["value"]

    captured_bulk_payment_call = {}

    def fake_bulk_payment(self, **kwargs):
        captured_bulk_payment_call.update(kwargs)
        return {
            "FILE_SEQUENCE_NUM": "999111",
            "MESSAGE_DESC": "The file is uploaded successfully. File Sequence No: [999111]",
            "requires_manual_approval": False,
        }

    monkeypatch.setattr(ICICIClient, "bulk_payment", fake_bulk_payment)

    # Step 2: submit with the OTP -> Bulk Payment call happens.
    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit",
        json={"otp": "123456"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["otp_requested"] is False
    assert data["file_sequence_num"] == "999111"
    assert data["requires_manual_approval"] is False
    assert captured_bulk_payment_call["unique_id"] == captured_otp_unique_id["value"]
    assert captured_bulk_payment_call["otp"] == "123456"
    assert captured_bulk_payment_call["file_name"] == batch["file_name"]

    db_session.refresh(db_batch)
    assert db_batch.icici_file_sequence_num == "999111"
    assert db_batch.icici_pending_unique_id is None
    assert db_batch.icici_unique_id == captured_otp_unique_id["value"]
    assert db_batch.status == "Submitted"

    # Status check against the now-submitted batch.
    def fake_reverse_mis(self, **kwargs):
        assert kwargs["unique_id"] == db_batch.icici_unique_id
        assert kwargs["file_seq_num"] == "999111"
        return {
            "file_sequence_num": 999111,
            "file_status": {"code": "STS", "description": "Success", "outcome": "success"},
            "response": "SUCCESS",
            "transactions": [
                {
                    "transaction_type": "PMT",
                    "network_id": "WIB",
                    "credit_account_number": "000405011155",
                    "debit_account_number": "000705000961",
                    "ifsc_code": "ICIC0000011",
                    "currency": "INR",
                    "total_amount": "50000.00",
                    "host_reference_number": "REF1",
                    "host_response_code": "000",
                    "host_response_message": "Payment Success",
                    "transaction_remarks": "SALARY202609",
                    "transaction_status": "SUC",
                }
            ],
        }

    monkeypatch.setattr(ICICIClient, "reverse_mis", fake_reverse_mis)

    resp = client.get(f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/status")
    assert resp.status_code == 200, resp.text
    status_data = resp.json()
    assert status_data["file_status_code"] == "STS"
    assert status_data["file_status_outcome"] == "success"
    assert len(status_data["transactions"]) == 1
    assert status_data["transactions"][0]["transaction_status"] == "SUC"


def test_status_check_requires_prior_submission(client, db_session, configured_icici):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC4", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    resp = client.get(f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/status")
    assert resp.status_code == 400
    assert "not been successfully submitted" in resp.json()["detail"]


def test_submit_without_pending_otp_is_rejected(client, db_session, configured_icici):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC5", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit",
        json={"otp": "999999"},
    )
    assert resp.status_code == 400
    assert "without an OTP first" in resp.json()["detail"]


def test_bulk_payment_api_rejection_surfaces_as_502(client, db_session, configured_icici, monkeypatch):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC6", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    monkeypatch.setattr(
        ICICIClient,
        "create_otp",
        lambda self, unique_id: {"RESPONSE": "Success"},
    )
    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit", json={}
    )
    assert resp.status_code == 200

    def fake_bulk_payment(self, **kwargs):
        raise ICICIBulkPaymentError(
            "Transactions already processed with same unique ID, please use exclusive unique id for each "
            "transaction.",
            error_code="999936",
        )

    monkeypatch.setattr(ICICIClient, "bulk_payment", fake_bulk_payment)
    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit",
        json={"otp": "123456"},
    )
    assert resp.status_code == 502
    assert "999936" in resp.json()["detail"]


def test_create_otp_failure_surfaces_as_502(client, db_session, configured_icici, monkeypatch):
    _seed_company_bank_config(db_session)
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee_with_line_item(db_session, run, "E-IC7", "000405011155", "ICIC0005678", 50000)
    batch = _generate_icici_batch(client, run.id)

    def fake_create_otp(self, unique_id):
        raise ICICIOTPError("Invalid Corp ID or User ID or Aggregator ID is passed.", error_code="9906")

    monkeypatch.setattr(ICICIClient, "create_otp", fake_create_otp)
    resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/disbursement-batches/{batch['id']}/icici/submit", json={}
    )
    assert resp.status_code == 502
    assert "9906" in resp.json()["detail"]
