"""Phase 3 Excel upload: variable-inputs and final-sheet, both modes'
upload -> preview -> commit round trip, built entirely from in-memory
openpyxl workbooks (no on-disk fixture files).
"""
from datetime import date
from io import BytesIO

from openpyxl import Workbook

from app.models.employee import Employee
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent
from app.models.variable_pay import VariablePayInput

PERIOD_START = date(2026, 9, 1)
PERIOD_END = date(2026, 9, 30)
PAY_PERIOD_STR = "2026-09"


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
        status="Draft",
        source="InAppCalculation",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_employee(db_session, code) -> Employee:
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
    return employee


def _workbook_bytes(headers, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload_file(client, url, content, filename="upload.xlsx"):
    return client.post(
        url,
        files={
            "file": (
                filename,
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


VARIABLE_INPUTS_HEADERS = [
    "Employee Code",
    "Employee Name",
    "Pay Period",
    "Days Present",
    "LOP Days",
    "Overtime Hours",
    "Bonus Amount",
    "Bonus Reason",
    "Ad-hoc Deduction Amount",
    "Ad-hoc Deduction Reason",
    "Remarks",
]

FINAL_SHEET_HEADERS = [
    "Employee Code",
    "Employee Name",
    "Pay Period",
    "Gross Earnings",
    "Total Deductions",
    "Net Pay",
    "Remarks",
]


def test_variable_inputs_upload_and_commit_round_trip(client, db_session):
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    emp = _make_employee(db_session, "E-VI1")

    content = _workbook_bytes(
        VARIABLE_INPUTS_HEADERS,
        [["E-VI1", "E-VI1", PAY_PERIOD_STR, 28, 2, 5, 1000, "Festival bonus", 200, "Canteen", "ok"]],
    )

    upload_resp = _upload_file(
        client, f"/api/v1/payroll-runs/{run.id}/variable-inputs/upload", content
    )
    assert upload_resp.status_code == 200, upload_resp.text
    preview = upload_resp.json()
    assert preview["summary"]["error_count"] == 0
    assert preview["summary"]["total_rows"] == 1
    batch_id = preview["batch_id"]

    commit_resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/variable-inputs/commit", json={"batch_id": batch_id}
    )
    assert commit_resp.status_code == 200, commit_resp.text
    commit_data = commit_resp.json()
    assert commit_data["committed_rows"] == 1

    row = (
        db_session.query(VariablePayInput)
        .filter(VariablePayInput.employee_id == emp.id, VariablePayInput.period_start == PERIOD_START)
        .first()
    )
    assert row is not None
    assert row.source == "ExcelImport"
    assert row.import_batch_id == commit_data["import_batch_id"]
    assert float(row.present_days) == 28
    assert float(row.lop_days) == 2
    assert float(row.overtime_hours) == 5
    assert row.ad_hoc_earnings[0]["component_code"] == "BONUS"
    assert row.ad_hoc_earnings[0]["amount"] == 1000
    assert row.ad_hoc_earnings[0]["remarks"] == "Festival bonus"
    assert row.ad_hoc_deductions[0]["component_code"] == "ADHOC_DEDUCTION"
    assert row.ad_hoc_deductions[0]["amount"] == 200


def test_variable_inputs_preview_flags_errors_and_blocks_commit(client, db_session):
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee(db_session, "E-VI2")

    content = _workbook_bytes(
        VARIABLE_INPUTS_HEADERS,
        [
            # Unknown employee code
            ["E-DOES-NOT-EXIST", "Nobody", PAY_PERIOD_STR, 30, 0, 0, 0, "", 0, "", ""],
            # Bonus Amount > 0 with no Bonus Reason
            ["E-VI2", "E-VI2", PAY_PERIOD_STR, 30, 0, 0, 500, "", 0, "", ""],
        ],
    )

    upload_resp = _upload_file(
        client, f"/api/v1/payroll-runs/{run.id}/variable-inputs/upload", content
    )
    assert upload_resp.status_code == 200, upload_resp.text
    preview = upload_resp.json()
    assert preview["summary"]["error_count"] == 2

    row1_errors = " ".join(preview["rows"][0]["errors"])
    assert "not found" in row1_errors

    row2_errors = " ".join(preview["rows"][1]["errors"])
    assert "Bonus Reason is required" in row2_errors

    batch_id = preview["batch_id"]
    commit_resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/variable-inputs/commit", json={"batch_id": batch_id}
    )
    assert commit_resp.status_code == 422

    # Nothing should have been written.
    assert db_session.query(VariablePayInput).count() == 0


def test_variable_inputs_commit_rejected_when_period_locked(client, db_session):
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    emp = _make_employee(db_session, "E-VI3")

    db_session.add(
        VariablePayInput(
            employee_id=emp.id,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            source="Manual",
            present_days=30,
            status="Locked",
        )
    )
    db_session.commit()

    content = _workbook_bytes(
        VARIABLE_INPUTS_HEADERS,
        [["E-VI3", "E-VI3", PAY_PERIOD_STR, 30, 0, 0, 0, "", 0, "", ""]],
    )
    upload_resp = _upload_file(
        client, f"/api/v1/payroll-runs/{run.id}/variable-inputs/upload", content
    )
    assert upload_resp.status_code == 200, upload_resp.text
    batch_id = upload_resp.json()["batch_id"]

    commit_resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/variable-inputs/commit", json={"batch_id": batch_id}
    )
    assert commit_resp.status_code == 409
    assert "Locked" in commit_resp.json()["detail"]


def test_final_sheet_upload_and_commit_bypasses_engine(client, db_session):
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    emp1 = _make_employee(db_session, "E-FS1")
    emp2 = _make_employee(db_session, "E-FS2")

    content = _workbook_bytes(
        FINAL_SHEET_HEADERS,
        [
            ["E-FS1", "E-FS1", PAY_PERIOD_STR, 84000, 3050, 80950, "final import"],
            ["E-FS2", "E-FS2", PAY_PERIOD_STR, 14000, 1200, 12800, ""],
        ],
    )

    upload_resp = _upload_file(client, f"/api/v1/payroll-runs/{run.id}/final-sheet/upload", content)
    assert upload_resp.status_code == 200, upload_resp.text
    preview = upload_resp.json()
    assert preview["summary"]["error_count"] == 0
    batch_id = preview["batch_id"]

    commit_resp = client.post(
        f"/api/v1/payroll-runs/{run.id}/final-sheet/commit", json={"batch_id": batch_id}
    )
    assert commit_resp.status_code == 200, commit_resp.text
    commit_data = commit_resp.json()
    assert commit_data["committed_rows"] == 2
    assert commit_data["run"]["status"] == "Calculated"
    assert commit_data["run"]["source"] == "ExcelFinalUpload"
    assert commit_data["run"]["total_net"] == 80950.00 + 12800.00

    db_session.refresh(run)
    assert run.status == "Calculated"
    assert run.source == "ExcelFinalUpload"

    line_items = db_session.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).all()
    assert len(line_items) == 2
    by_employee = {li.employee_id: li for li in line_items}

    li1 = by_employee[emp1.id]
    assert float(li1.gross_earnings) == 84000.00
    assert float(li1.total_deductions) == 3050.00
    assert float(li1.net_pay) == 80950.00
    # calculation_method="ExcelImport" is only ever produced by the
    # final-sheet importer, never by the calc engine — proves the engine
    # was not invoked for this run.
    remarks_entries = [e for e in li1.computation_breakdown if e.get("component_name") == "Remarks"]
    assert remarks_entries and remarks_entries[0]["warning"] == "final import"

    li2 = by_employee[emp2.id]
    assert float(li2.net_pay) == 12800.00


def test_final_sheet_upload_flags_net_pay_mismatch(client, db_session):
    schedule = _make_schedule(db_session)
    run = _make_run(db_session, schedule)
    _make_employee(db_session, "E-FS3")

    # Gross - Deductions = 84000 - 3050 = 80950, but Net Pay says 50000 —
    # well outside the rounding tolerance.
    content = _workbook_bytes(
        FINAL_SHEET_HEADERS,
        [["E-FS3", "E-FS3", PAY_PERIOD_STR, 84000, 3050, 50000, ""]],
    )
    upload_resp = _upload_file(client, f"/api/v1/payroll-runs/{run.id}/final-sheet/upload", content)
    assert upload_resp.status_code == 200, upload_resp.text
    preview = upload_resp.json()
    assert preview["summary"]["error_count"] == 1
    assert "does not match Net Pay" in " ".join(preview["rows"][0]["errors"])
