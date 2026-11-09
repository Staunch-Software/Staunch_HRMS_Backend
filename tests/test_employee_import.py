"""Employee master Excel import: header matching, value normalization,
sheet precedence/dedupe, relieved handling, DB upsert rules and the
preview/commit endpoints. All workbooks are synthetic, built in memory.
"""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.models.employee import Employee, EmployeeBankAccount
from app.models.org import Department, Designation, WorkLocation
from app.services.employee_import import service, values
from app.services.employee_import.workbook import MASTER_LAYOUT, match_header, read_master_workbook
from app.services.excel_import.excel_reader import ExcelParseError
from app.services.org_normalization import (
    infer_state_code,
    normalize_work_location,
    smart_case,
    unique_department_code,
)

MASTER_HEADERS = [
    "S.No", "\nEmployee ID", "Employee Name", "Gender", "Date of Birth", "Mobile Number", "Personal Email ID",
    "Department", "Designation", "Employment Type (Permanent/Contract/Apprentice)", "Date of Joining",
    "Work Location", "Employee Status (Active/Resigned)", "PF Applicable (Yes/No)", "PF UAN Number",
    "ESI Applicable (Yes/No)", "ESI Number", "Bank Name", "Account Number", "IFSC Code",
    "Professional (Certifications)", "Highest Qualification", "Year of Passing", "University/Institution Name",
    "Apprenticeship Type (If Applicable)", "Enrollment ID", "Contract No", "Contract Start Date (If Apprentice)",
    "Contract End Date (If Apprentice)", "Stipend Amount (If Apprentice)", "Remarks", "Permanent Address",
    "Aadhaar Number", "PAN Number", "Father�s / Spouse Name", "Relation", "Emergency Contact Number",
    "Blood Group", "Insurance (Yes/ No)", "Geographic Location", "Client", "Client Branch",
]
RELIEVED_HEADERS = [
    "S.No", "Emp Code", "ESIC No", "UAN NUMBER", "EMPLOYEE NAME ", "DOB", "Father / Husband's Name", "Address",
    "AADHAAR ", "PAN No", "DOJ", "DOR", "Emp Mobile ", "Emergency Contact", "Relation", "Blood Group", "DOL",
    "STATUS", "DESIGNATION", "DEPARTMENT", "GEO", "Client", "Client Branch", "PF (YES/NO)", "ESI (YES/NO)",
    "BANK ", "BANK A/C NO.", "IFSC CODE", "Mail id ",
]


def master_row(code, name, **overrides):
    """A master-sheet row (list in MASTER_HEADERS order) of synthetic data."""
    data = {
        "employee_code": code, "name": name, "gender": "Male", "date_of_birth": "23.08.1990",
        "mobile_number": 9000000001, "personal_email": "someone@example.com", "department": "Software",
        "designation": "Engineer", "employment_type": "Permanent", "date_of_joining": "01.04.2024",
        "work_location": "Chennai", "status": "Active", "pf_applicable": "Yes", "uan_number": 100000000001,
        "esi_applicable": "no", "esi_number": "NA", "bank_name": "Test Bank", "account_number": 123456789012,
        "ifsc_code": "TEST0001234", "aadhaar_number": 111122223333, "pan_number": "ABCDE1234F",
        "geographic_location": "TN",
    }
    data.update(overrides)
    row = [None] * len(MASTER_LAYOUT)
    row[0] = 1
    for index, field_name in enumerate(MASTER_LAYOUT):
        if field_name in data:
            row[index] = data[field_name]
    return row


def relieved_row(code, name, **overrides):
    data = {
        "Emp Code": code, "EMPLOYEE NAME ": name, "DOB": "11.10.1995", "DOJ": "01.01.2023", "DOL": "31.03.25",
        "STATUS": "Resigned", "DESIGNATION": "Technician", "DEPARTMENT": "Maintenance", "GEO": "TN",
        "PF (YES/NO)": "YES", "ESI (YES/NO)": "NO", "BANK ": "Test Bank", "BANK A/C NO.": 998877665544,
        "IFSC CODE": "TEST0009999", "Emp Mobile ": 9000000002,
    }
    data.update(overrides)
    return [data.get(h) for h in RELIEVED_HEADERS]


def build_workbook(sheets: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for title, (headers, rows) in sheets.items():
        ws = wb.create_sheet(title)
        ws.append(headers)
        for row in rows:
            ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def upload(client, content):
    return client.post(
        "/api/v1/employees/import/master/preview",
        files={"file": ("master.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def rows_by_code(preview):
    return {r["employee_code"]: r for r in preview["rows"] if r["employee_code"]}


# --- Header matching ------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("\nEmployee ID", "employee_code"),
        ("  Employee ID ", "employee_code"),
        ("Emp Code", "employee_code"),
        ("Father�s / Spouse Name", "father_spouse_name"),
        ("Fatherâ€™s / Spouse Name", "father_spouse_name"),
        ("Father's / Spouse Name", "father_spouse_name"),
        ("University/Institution", "university"),
        ("University/Institution Name", "university"),
        ("EMPLOYEE NAME ", "name"),
        ("Insurance (Yes/ No)", "insurance_applicable"),
        ("PF (YES/NO)", "pf_applicable"),
        ("BANK A/C NO.", "account_number"),
        ("Mail id ", "personal_email"),
        ("Today", None),
        ("DOR", None),
    ],
)
def test_header_matching(header, expected):
    assert match_header(header) == expected


def test_sheet_variants_read_by_header_name():
    shuffled = ["Employee Name", "Today", "\nEmployee ID", "Date of Joining", "Work Location"]
    content = build_workbook({
        "Employee Master ": (shuffled, [["Asha K", None, " ST-EMP-05", "01.02.2024", "chennai"]]),
        "Birthday List": (["x"], [["ignored"]]),
    })
    parsed = read_master_workbook(content)
    assert parsed.sheets_used == ["Employee Master"]
    record = parsed.records[0]
    assert record.employee_code == "STEMP0005"
    assert record.fields["first_name"] == "Asha" and record.fields["last_name"] == "K"
    assert record.fields["date_of_joining"] == date(2024, 2, 1)
    assert record.fields["work_location"] == "chennai"


def test_workbook_without_expected_sheets_rejected(client):
    content = build_workbook({"Sheet1": (["a", "b"], [[1, 2]])})
    with pytest.raises(ExcelParseError):
        read_master_workbook(content)
    response = upload(client, content)
    assert response.status_code == 400
    assert "expected employee master sheets" in response.json()["detail"]


def test_not_an_excel_file_rejected(client):
    response = upload(client, b"not a workbook")
    assert response.status_code == 400


# --- Value normalization --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (datetime(2020, 5, 17, 0, 0), date(2020, 5, 17)),
        ("23.08.1978", date(1978, 8, 23)),
        ("31.03.25", date(2025, 3, 31)),
        ("03.08.26", date(2026, 8, 3)),
        ("05.06.98", date(1998, 6, 5)),
        ("17-06-2005", date(2005, 6, 17)),
        ("2.05.2024", date(2024, 5, 2)),
        ("2024-01-15 00:00:00", date(2024, 1, 15)),
    ],
)
def test_parse_date(raw, expected):
    assert values.parse_date(raw) == (expected, None)


def test_parse_date_unparseable_is_warning():
    value, warning = values.parse_date("next monday")
    assert value is None and "not a date" in warning


@pytest.mark.parametrize(
    "raw,expected",
    [("Yes", True), ("yes", True), ("YES", True), ("Yes ", True), ("No", False), ("no", False),
     ("NO", False), ("NA", None), (None, None)],
)
def test_parse_yes_no(raw, expected):
    assert values.parse_yes_no(raw) == (expected, None)


def test_parse_yes_no_unknown_is_warning():
    value, warning = values.parse_yes_no("maybe")
    assert value is None and "Yes/No" in warning


@pytest.mark.parametrize(
    "raw,expected",
    [("15k", Decimal("15000")), ("20K", Decimal("20000")), ("12.5k", Decimal("12500")), (12500, Decimal("12500")),
     ("Rs. 9,000", Decimal("9000"))],
)
def test_parse_stipend(raw, expected):
    assert values.parse_amount(raw) == (expected, None)


@pytest.mark.parametrize(
    "raw,expected",
    [("O+ve", "O+"), ("B +ve", "B+"), ("A1+ ve", "A1+"), ("0-VE", "O-"), ("B+ positive ", "B+"),
     ("AB +ve", "AB+"), ("A1B +", "A1B+"), ("o+ve", "O+")],
)
def test_parse_blood_group(raw, expected):
    assert values.parse_blood_group(raw) == (expected, None)


def test_parse_identifiers():
    assert values.parse_ifsc(" test0001234 ") == ("TEST0001234", None)
    assert values.parse_ifsc("TEST001234")[0] is None
    assert values.parse_pan("abcde 1234f") == ("ABCDE1234F", None)
    assert values.parse_aadhaar("1111 2222 3333") == ("111122223333", None)
    assert values.parse_phone("90000 00001") == ("9000000001", None)
    assert values.parse_phone("9000000001/9000000002") == ("9000000001 / 9000000002", None)
    assert values.parse_account_number(123456789012.0) == ("123456789012", None)


@pytest.mark.parametrize(
    "raw,canonical,state",
    [
        ("chennai", "Chennai", "TN"), ("CHENNAI", "Chennai", "TN"), ("Chennai - HO", "Chennai HO", "TN"),
        ("HO", "Chennai HO", "TN"), ("Hybrid - Chennai", "Chennai (Hybrid)", "TN"),
        ("kudankulam", "Kudankulam", "TN"), ("KKNPP", "Kudankulam", "TN"), ("KKNP", "Kudankulam", "TN"),
        ("TPTS(Lower level control system) both unit1&2 KKNP", "Kudankulam", "TN"),
        ("synergy", "Synergy", None), ("Tuticorin", "Tuticorin", "TN"), ("Perungudi", "Perungudi", "TN"),
        ("Hyderabad", "Hyderabad", "TG"), ("Surat", "Surat", "GJ"), ("Bhavnagar", "Bhavnagar", "GJ"),
        ("KLA", "KLA", None),
    ],
)
def test_work_location_normalization(raw, canonical, state):
    assert normalize_work_location(raw) == canonical
    assert infer_state_code(raw) == state


def test_state_code_falls_back_to_geographic_location():
    assert infer_state_code("Ozellar", "TN") == "TN"
    assert infer_state_code("Ozellar", "Telangana") == "TG"
    assert infer_state_code("Ozellar", "AP") == "AP"
    assert infer_state_code("Ozellar", "Kallakurichi") == "TN"
    assert infer_state_code("Ozellar", "Mars") is None
    assert infer_state_code("Ozellar") is None


def test_smart_case_and_department_codes():
    assert smart_case("software ") == "Software"
    assert smart_case("MAINTENANCE") == "Maintenance"
    assert smart_case("CMU") == "CMU"
    assert smart_case("KKNPP") == "KKNPP"
    assert smart_case("Sr.Technician") == "Sr.Technician"
    assert unique_department_code("E&I", set()) == "EI"
    assert unique_department_code("Software", {"SOFTWARE"}) == "SOFTWARE2"


# --- Workbook layout handling ---------------------------------------------------


def test_relieved_rows_with_shifted_or_master_columns_are_realigned():
    master_layout_row = master_row("STEMP0300", "Pasted Person", date_of_birth="01.01.1991", status="Active")
    shifted = relieved_row("STEMP0301", "Shifted Person")
    del shifted[RELIEVED_HEADERS.index("DOL")]  # a block with the DOL column missing
    shifted.append(None)
    content = build_workbook({"Relieved Employees": (RELIEVED_HEADERS, [master_layout_row, shifted])})
    records = {r.employee_code: r for r in read_master_workbook(content).records}

    pasted = records["STEMP0300"]
    assert pasted.fields["first_name"] == "Pasted"
    assert pasted.fields["date_of_birth"] == date(1991, 1, 1)
    assert pasted.fields["ifsc_code"] == "TEST0001234"
    assert any("standard master column order" in n for n in pasted.notes)

    moved = records["STEMP0301"]
    assert moved.fields["ifsc_code"] == "TEST0009999"
    assert moved.fields["pf_applicable"] is True
    assert moved.fields["geographic_location"] == "TN"


# --- Plan / commit against the DB ------------------------------------------------


def full_workbook():
    return build_workbook({
        "Employee Master ": (MASTER_HEADERS, [
            master_row("STEMP0001", "Asha Kumar", stipend_amount="15k", employment_type="Apprentice"),
            master_row("ST-EMP-02", "Ravi S", work_location="kudankulam", department="Technical ",
                       designation="TECHNICIAN", geographic_location="TN"),
            master_row("STEMP0004", "Bad Ifsc", ifsc_code="TEST001234", pf_applicable="maybe",
                       date_of_birth="99.99.9999"),
            master_row(None, "No Id Person"),
        ]),
        "NATS Employee Master": (MASTER_HEADERS, [
            master_row("STEMP0001", "Asha Kumar Old", blood_group="O+ve", client="kknpp"),
            master_row("STEMP0003", "Only In Nats", work_location="Hyderabad", geographic_location="Telangana",
                       department="technical", designation="Technician"),
        ]),
        "NATS": (MASTER_HEADERS, [master_row("STEMP0003", "Lower Precedence")]),
        "Staff & Internship Staffs": (MASTER_HEADERS, [master_row(None, "Intern One"), master_row(None, "Intern Two")]),
        "Relieved Employees": (RELIEVED_HEADERS, [
            relieved_row("STEMP0002", "Ravi S"),
            relieved_row("STEMP0010", "Gone Person"),
        ]),
        "Sheet1": (["total"], [["x"]]),
    })


def test_preview_precedence_dedupe_conflicts_and_skips(client, db_session):
    response = upload(client, full_workbook())
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["summary"]
    assert summary["new"] == 5
    assert summary["skipped_no_id"] == 3
    assert summary["conflicts"] == 1
    assert summary["inactive"] == 1

    rows = rows_by_code(body)
    asha = rows["STEMP0001"]
    assert asha["source_sheet"] == "Employee Master" and asha["name"] == "Asha Kumar"
    assert asha["action"] == "new"
    assert rows["STEMP0003"]["source_sheet"] == "NATS Employee Master"
    assert rows["STEMP0003"]["name"] == "Only In Nats"

    ravi = rows["STEMP0002"]
    assert ravi["status"] == "Active"
    assert any(i["level"] == "conflict" for i in ravi["issues"])

    gone = rows["STEMP0010"]
    assert gone["status"] == "Inactive" and gone["source_sheet"] == "Relieved Employees"

    skipped = [r for r in body["rows"] if r["action"] == "skipped"]
    assert len(skipped) == 3
    assert all(r["issues"][0]["message"] == "skipped: no employee ID" for r in skipped)

    bad = rows["STEMP0004"]
    messages = " | ".join(i["message"] for i in bad["issues"])
    assert "IFSC" in messages and "bank account not imported" in messages
    assert "PF Applicable" in messages and "Date of Birth" in messages

    assert db_session.query(Employee).count() == 0  # preview writes nothing


def test_commit_creates_employees_orgs_and_bank_accounts(client, db_session):
    db_session.add(Department(name="Human Resources", code="HR"))
    db_session.commit()
    preview = upload(client, full_workbook()).json()
    response = client.post("/api/v1/employees/import/master/commit", json={"batch_id": preview["batch_id"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] == 5
    assert result["bank_accounts_created"] == 4  # STEMP0004 has an invalid IFSC

    employees = {e.employee_code: e for e in db_session.query(Employee).all()}
    assert set(employees) == {"STEMP0001", "STEMP0002", "STEMP0003", "STEMP0004", "STEMP0010"}

    asha = employees["STEMP0001"]
    assert (asha.first_name, asha.last_name) == ("Asha", "Kumar")
    assert asha.date_of_birth == date(1990, 8, 23)
    assert asha.stipend_amount == Decimal("15000")
    assert asha.pf_applicable is True and asha.esi_applicable is False and asha.esi_number is None
    assert asha.blood_group == "O+"  # blank in the main list, filled from the NATS Employee Master row
    assert asha.client == "KKNPP" or asha.client == "Kknpp"
    assert asha.aadhaar_number == "111122223333" and asha.pan_number == "ABCDE1234F"
    assert asha.bank_accounts[0].account_holder_name == "Asha Kumar"

    ravi = employees["STEMP0002"]
    assert ravi.status == "Active"
    assert ravi.work_location.name == "Kudankulam" and ravi.work_location.state_code == "TN"

    bad = employees["STEMP0004"]
    assert bad.bank_accounts == []
    assert bad.date_of_birth is None and bad.pf_applicable is None

    gone = employees["STEMP0010"]
    assert gone.status == "Inactive" and gone.date_of_leaving == date(2025, 3, 31)

    hyderabad = db_session.query(WorkLocation).filter_by(name="Hyderabad").one()
    assert hyderabad.state_code == "TG"
    technical = db_session.query(Department).filter_by(name="Technical").all()
    assert len(technical) == 1 and technical[0].code == "TECHNICAL"
    technicians = db_session.query(Designation).filter_by(department_id=technical[0].id).all()
    assert [d.name for d in technicians] == ["Technician"]
    assert ravi.designation_id == technicians[0].id == employees["STEMP0003"].designation_id
    assert db_session.query(Department).filter_by(code="HR").count() == 1

    # A second identical import changes nothing.
    again = upload(client, full_workbook()).json()
    assert again["summary"]["new"] == 0 and again["summary"]["updated"] == 0
    assert again["summary"]["unchanged"] == 5
    assert again["summary"]["new_departments"] == 0 and again["summary"]["new_work_locations"] == 0


def test_update_only_overwrites_with_non_blank_values(client, db_session):
    existing = Employee(
        employee_code="STEMP0001", first_name="Asha", last_name="Kumar", status="Active",
        mobile_number="9111111111", remarks="keep me", pan_number="ZZZZZ9999Z", work_email="asha@corp.test",
    )
    untouched = Employee(employee_code="E9001", first_name="Manual", status="Active")
    db_session.add_all([existing, untouched])
    db_session.flush()
    db_session.add(EmployeeBankAccount(employee_id=existing.id, bank_name="Old", account_holder_name="Asha Kumar",
                                       account_number="1111", ifsc_code="OLDB0000001"))
    db_session.commit()

    content = build_workbook({"Employee Master ": (MASTER_HEADERS, [
        master_row("ST-EMP-1", "Asha Kumar", mobile_number=None, pan_number=None, remarks=None,
                   pf_applicable="no"),
    ])})
    preview = upload(client, content).json()
    row = rows_by_code(preview)["STEMP0001"]
    assert row["action"] == "update"
    assert "pf_applicable" in row["changed_fields"] and "mobile_number" not in row["changed_fields"]
    assert row["bank_account"] == "update"

    client.post("/api/v1/employees/import/master/commit", json={"batch_id": preview["batch_id"]})
    db_session.expire_all()
    employee = db_session.query(Employee).filter_by(employee_code="STEMP0001").one()
    assert employee.mobile_number == "9111111111"
    assert employee.remarks == "keep me"
    assert employee.pan_number == "ZZZZZ9999Z"
    assert employee.work_email == "asha@corp.test"
    assert employee.pf_applicable is False
    assert employee.date_of_birth == date(1990, 8, 23)
    accounts = db_session.query(EmployeeBankAccount).filter_by(employee_id=employee.id).all()
    assert len(accounts) == 1 and accounts[0].ifsc_code == "TEST0001234"
    manual = db_session.query(Employee).filter_by(employee_code="E9001").one()
    assert manual.first_name == "Manual" and manual.date_of_birth is None


def test_relieved_only_existing_employee_becomes_inactive(client, db_session):
    db_session.add(Employee(employee_code="STEMP0010", first_name="Gone", status="Active"))
    db_session.commit()
    content = build_workbook({"Relieved Employees": (RELIEVED_HEADERS, [relieved_row("STEMP0010", "Gone Person")])})
    preview = upload(client, content).json()
    assert rows_by_code(preview)["STEMP0010"]["action"] == "update"
    client.post("/api/v1/employees/import/master/commit", json={"batch_id": preview["batch_id"]})
    db_session.expire_all()
    employee = db_session.query(Employee).filter_by(employee_code="STEMP0010").one()
    assert employee.status == "Inactive" and employee.date_of_leaving == date(2025, 3, 31)


def test_commit_unknown_batch_404(client):
    response = client.post("/api/v1/employees/import/master/commit", json={"batch_id": "nope"})
    assert response.status_code == 404


def test_import_endpoints_require_auth(client):
    response = client.post(
        "/api/v1/employees/import/master/commit", json={"batch_id": "x"}, headers={"Authorization": ""}
    )
    assert response.status_code == 401


def test_employee_api_round_trips_master_fields(client):
    payload = {
        "employee_code": "E2001", "first_name": "Field", "date_of_birth": "1990-01-02", "pan_number": "ABCDE1234F",
        "aadhaar_number": "111122223333", "employment_type": "Contract", "pf_applicable": True,
        "stipend_amount": 15000, "address_line1": "1 Test Street", "city": "Chennai", "blood_group": "O+",
    }
    created = client.post("/api/v1/employees", json=payload).json()
    for key in ("date_of_birth", "pan_number", "employment_type", "pf_applicable", "address_line1", "city"):
        assert created[key] == payload[key]
    # A PUT that omits the new fields must not wipe them.
    updated = client.put(f"/api/v1/employees/{created['id']}", json={"employee_code": "E2001", "first_name": "Renamed"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["first_name"] == "Renamed" and body["pan_number"] == "ABCDE1234F" and body["city"] == "Chennai"


def test_service_records_round_trip_through_batch():
    parsed = read_master_workbook(full_workbook())
    restored = service.records_from_batch(service.records_to_batch(parsed.records))
    assert [r.employee_code for r in restored] == [r.employee_code for r in parsed.records]
