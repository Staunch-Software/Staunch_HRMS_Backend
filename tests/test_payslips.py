"""Payslips: data builder, amount in words, PDF rendering for every template,
HR single / ZIP download, settings + logo, and the /me release rule."""
import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from app.models.attendance_month import AttendanceMonthResult
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.payroll_settings import PayslipSettings
from app.services.payroll_settings import ensure_payslip_settings, payslip_settings
from app.services.payslip.data import build_payslip
from app.services.payslip.render import DEFAULT_LOGO, logo_data_uri
from app.services.payslip.words import indian_amount, rupees_in_words
from tests.leave_helpers import bearer, credit, make_employee, make_user, seed_types, token_for
from tests.test_tds import _schedule

API = "/api/v1"


def _breakdown(basic, hra, pf, tds=0):
    return [
        {"component_code": "_PAY_GROUP", "component_name": "Pay group", "component_type": "Info", "resolved_amount": 0},
        {"component_code": "WORKING_DAYS", "component_name": "Working days", "component_type": "Info",
         "resolved_amount": 28},
        {"component_code": "BASIC_DA", "component_name": "Basic & DA", "component_type": "Earning",
         "resolved_amount": basic},
        {"component_code": "HRA", "component_name": "HRA", "component_type": "Earning", "resolved_amount": hra},
        {"component_code": "HIDDEN", "component_name": "Hidden earning", "component_type": "Earning",
         "resolved_amount": 100, "show_on_payslip": False},
        {"component_code": "OT", "component_name": "Overtime", "component_type": "Earning", "resolved_amount": 0},
        {"component_code": "PF_EE", "component_name": "Employee PF", "component_type": "Deduction",
         "resolved_amount": pf},
        {"component_code": "TDS", "component_name": "Income tax (TDS)", "component_type": "Deduction",
         "resolved_amount": tds},
        {"component_code": "PF_ER", "component_name": "Employer PF", "component_type": "EmployerCost",
         "resolved_amount": 1800},
    ]


def _run(db, schedule, employee, period_end, status, basic=20000, hra=8000, pf=1800, tds=500):
    run = PayrollRun(pay_schedule_id=schedule.id, period_start=period_end.replace(day=1), period_end=period_end,
                     pay_date=period_end, status=status)
    db.add(run)
    db.flush()
    gross = basic + hra + 100
    db.add(PayrollRunLineItem(
        payroll_run_id=run.id, employee_id=employee.id,
        employee_snapshot={"employee_code": employee.employee_code, "name": "Asha K", "department": "Software",
                           "designation": "Engineer", "work_location": "Chennai", "bank_name": "HDFC Bank",
                           "account_number_masked": "XXXXXX4321", "ifsc_code": "HDFC0000001"},
        gross_earnings=gross, total_deductions=pf + tds, total_reimbursements=0, net_pay=gross - pf - tds,
        paid_days=28, lop_days=2, status="Included", computation_breakdown=_breakdown(basic, hra, pf, tds),
    ))
    db.commit()
    return run


@pytest.fixture()
def setup(db_session):
    types = seed_types(db_session)
    employee = make_employee(db_session, "ST0100", first="Asha", joined=date(2024, 6, 1))
    employee.pan_number = "ABCDE1234F"
    employee.uan_number = "100200300400"
    employee.date_of_birth = date(1995, 3, 7)
    db_session.commit()
    credit(db_session, employee, types["CL"], 6, 2026)
    schedule = _schedule(db_session)
    august = _run(db_session, schedule, employee, date(2026, 8, 31), "Paid", basic=19000)
    september = _run(db_session, schedule, employee, date(2026, 9, 30), "Calculated")
    db_session.add(AttendanceMonthResult(
        employee_id=employee.id, month="2026-09", status="SENT", lop_days=Decimal("2"),
        present_days=Decimal("24"), paid_leave_days=Decimal("1"), totals={"working_days": 26}, days=[], warnings=[],
    ))
    db_session.commit()
    return {"employee": employee, "schedule": schedule, "august": august, "september": september}


def test_amount_in_words_and_indian_format():
    assert rupees_in_words(0) == "Rupees Zero Only"
    assert rupees_in_words(100000) == "Rupees One Lakh Only"
    assert rupees_in_words(Decimal("12345678.50")) == (
        "Rupees One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight and Fifty Paise Only")
    assert rupees_in_words(25700) == "Rupees Twenty Five Thousand Seven Hundred Only"
    assert indian_amount(1234567.5) == "12,34,567.50"
    assert indian_amount(999) == "999.00"
    assert indian_amount(-150000) == "-1,50,000.00"


def test_data_builder(db_session, setup):
    run, employee = setup["september"], setup["employee"]
    item = db_session.query(PayrollRunLineItem).filter_by(payroll_run_id=run.id).one()
    settings = payslip_settings(db_session)
    p = build_payslip(db_session, run, item, settings, logo_data_uri(settings))

    assert p["month_label"] == "September 2026"
    assert [r["code"] for r in p["earnings"]] == ["BASIC_DA", "HRA"]
    assert [r["code"] for r in p["deductions"]] == ["PF_EE", "TDS"]
    assert p["employer"] == []  # employer contributions are off by default
    assert len(p["rows"]) == 2
    assert p["totals"]["gross"] == "28,100.00"
    assert p["totals"]["net"] == "25,800.00"
    assert p["totals"]["net_words"] == "Rupees Twenty Five Thousand Eight Hundred Only"
    assert p["employee"]["pan"] == "XXXXXX234F"
    assert p["employee"]["account"] == "XXXXXX4321"
    assert ("UAN", "100200300400") in p["employee_fields"]
    assert p["attendance"]["paid_days"] == "28" and p["attendance"]["lop_days"] == "2"
    assert p["attendance"]["present_days"] == "24" and p["attendance"]["working_days"] == 26
    assert p["leave"] == [{"code": "CL", "name": "Casual Leave", "credited": "6", "used": "0", "balance": "6"}]
    # YTD = Paid August + this (Calculated) September run
    assert p["ytd"]["gross"] == "55,200.00"
    basic = next(r for r in p["earnings"] if r["code"] == "BASIC_DA")
    assert basic["ytd_text"] == "39,000.00"


def test_data_builder_respects_switches(db_session, setup):
    row = ensure_payslip_settings(db_session)
    for key in ("show_pan", "show_uan", "show_bank", "show_attendance", "show_leave_balance", "show_ytd"):
        setattr(row, key, False)
    row.show_employer_contrib = True
    db_session.commit()
    run = setup["september"]
    item = db_session.query(PayrollRunLineItem).filter_by(payroll_run_id=run.id).one()
    p = build_payslip(db_session, run, item, row, logo_data_uri(row))
    labels = {label for label, _ in p["employee_fields"]}
    assert not labels & {"PAN", "UAN", "Bank", "Account no."}
    assert p["attendance"] is None and p["leave"] == [] and p["ytd"] is None
    assert [r["code"] for r in p["employer"]] == ["PF_ER"]


@pytest.mark.parametrize("template", ["CLASSIC", "MODERN", "COMPACT"])
def test_pdf_renders_for_every_template(client, db_session, setup, template):
    preview = client.get(f"{API}/settings/payslip/preview", params={"template": template})
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "application/pdf"
    assert preview.content.startswith(b"%PDF")

    row = ensure_payslip_settings(db_session)
    row.template = template
    db_session.commit()
    run, employee = setup["september"], setup["employee"]
    response = client.get(f"{API}/payroll-runs/{run.id}/payslips/{employee.id}.pdf")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
    assert "september 2026" in text.lower() and "25,800.00" in text
    assert "ABCDE1234F" not in text


def test_run_zip_and_errors(client, db_session, setup):
    run, employee = setup["september"], setup["employee"]
    other = make_employee(db_session, "ST0200", first="Ravi")
    db_session.add(PayrollRunLineItem(
        payroll_run_id=run.id, employee_id=other.id, employee_snapshot={"employee_code": "ST0200", "name": "Ravi"},
        gross_earnings=1000, total_deductions=0, total_reimbursements=0, net_pay=1000, status="Included",
        computation_breakdown=[{"component_code": "BASIC", "component_name": "Basic", "component_type": "Earning",
                                "resolved_amount": 1000}],
    ))
    db_session.commit()
    response = client.get(f"{API}/payroll-runs/{run.id}/payslips.zip")
    assert response.status_code == 200, response.text
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert sorted(names) == ["Payslip_ST0100_September_2026.pdf", "Payslip_ST0200_September_2026.pdf"]

    assert client.get(f"{API}/payroll-runs/{run.id}/payslips/99999.pdf").status_code == 404
    draft = PayrollRun(pay_schedule_id=setup["schedule"].id, period_start=date(2026, 10, 1),
                       period_end=date(2026, 10, 31), pay_date=date(2026, 10, 31), status="Draft")
    db_session.add(draft)
    db_session.commit()
    assert client.get(f"{API}/payroll-runs/{draft.id}/payslips.zip").status_code == 409


def test_settings_and_logo(client, db_session):
    body = client.get(f"{API}/settings/payslip").json()
    assert body["template"] == "CLASSIC" and body["release_on"] == "APPROVED" and body["has_custom_logo"] is False
    payload = {**{k: body[k] for k in body if k not in ("has_custom_logo", "updated_at")},
               "company_name": "  Staunch Software  ", "company_gstin": " ", "template": "MODERN",
               "pdf_password": "DOB"}
    saved = client.put(f"{API}/settings/payslip", json=payload).json()
    assert saved["company_name"] == "Staunch Software" and saved["company_gstin"] is None
    assert saved["template"] == "MODERN" and saved["pdf_password"] == "DOB"

    default_logo = client.get(f"{API}/settings/payslip/logo")
    assert default_logo.content == DEFAULT_LOGO.read_bytes()
    bad = client.post(f"{API}/settings/payslip/logo", files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400
    png = DEFAULT_LOGO.read_bytes()
    uploaded = client.post(f"{API}/settings/payslip/logo", files={"file": ("logo.png", png, "image/png")})
    assert uploaded.status_code == 200 and uploaded.json()["has_custom_logo"] is True
    assert db_session.query(PayslipSettings).one().logo_mime == "image/png"
    assert client.delete(f"{API}/settings/payslip/logo").json()["has_custom_logo"] is False


def _employee_token(client, db, employee, username="asha"):
    make_user(db, username, employee)
    return bearer(token_for(client, username))


def test_me_payslips_release_rule(client, db_session, setup):
    employee, september = setup["employee"], setup["september"]
    headers = _employee_token(client, db_session, employee)

    rows = client.get(f"{API}/me/payslips", headers=headers).json()
    assert [r["run_id"] for r in rows] == [setup["august"].id]  # September is only Calculated
    assert rows[0]["month_label"] == "August 2026"
    assert client.get(f"{API}/me/payslips/{september.id}.pdf", headers=headers).status_code == 404

    september.status = "Approved"
    db_session.commit()
    assert [r["run_id"] for r in client.get(f"{API}/me/payslips", headers=headers).json()] == [
        september.id, setup["august"].id]
    pdf = client.get(f"{API}/me/payslips/{september.id}.pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    row = ensure_payslip_settings(db_session)
    row.release_on = "PAID"
    db_session.commit()
    assert [r["run_id"] for r in client.get(f"{API}/me/payslips", headers=headers).json()] == [setup["august"].id]
    assert client.get(f"{API}/me/payslips/{september.id}.pdf", headers=headers).status_code == 404


def test_me_payslip_is_own_only_and_hr_routes_forbidden(client, db_session, setup):
    other = make_employee(db_session, "ST0300", first="Other")
    headers = _employee_token(client, db_session, other, "other")
    assert client.get(f"{API}/me/payslips", headers=headers).json() == []
    assert client.get(f"{API}/me/payslips/{setup['august'].id}.pdf", headers=headers).status_code == 404
    run, employee = setup["august"], setup["employee"]
    assert client.get(f"{API}/payroll-runs/{run.id}/payslips/{employee.id}.pdf", headers=headers).status_code == 403
    assert client.get(f"{API}/payroll-runs/{run.id}/payslips.zip", headers=headers).status_code == 403


def test_me_payslip_pdf_password(client, db_session, setup):
    row = ensure_payslip_settings(db_session)
    row.pdf_password = "DOB"
    db_session.commit()
    headers = _employee_token(client, db_session, setup["employee"])
    pdf = client.get(f"{API}/me/payslips/{setup['august'].id}.pdf", headers=headers).content
    reader = PdfReader(io.BytesIO(pdf))
    assert reader.is_encrypted
    assert reader.decrypt("07031995")
    assert "august 2026" in reader.pages[0].extract_text().lower()

    row.pdf_password = "PAN"
    db_session.commit()
    reader = PdfReader(io.BytesIO(client.get(f"{API}/me/payslips/{setup['august'].id}.pdf", headers=headers).content))
    assert reader.is_encrypted and reader.decrypt("ABCDE1234F")
