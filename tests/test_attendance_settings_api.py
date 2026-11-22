from datetime import datetime
from io import BytesIO

from openpyxl import Workbook

from tests.leave_helpers import API, bearer, make_employee, make_location, make_user, seed_types, token_for


def policy_payload(**overrides):
    payload = {
        "shift_start": "09:30:00", "grace_until": "09:35:00", "required_minutes": 540,
        "weekday_pattern": {"mon": "WORK", "tue": "WORK", "wed": "WORK", "thu": "WORK", "fri": "WORK",
                            "sat": "WORK", "sun": "OFF"},
        "saturdays_off": [2], "saturday_off_mode": "HR_MONTHLY", "free_lates_per_month": 2,
        "late_deduction_days": "0.5", "permission_monthly_minutes": 120,
        "permission_max_per_month": 2, "permission_max_minutes_each": 60, "permission_half_day_above_minutes": 120,
        "permission_full_day_above_minutes": 240, "missing_out_policy": "PRESENT", "min_notice_days": 1,
        "leave_year_start_month": 1, "effective_from": "2026-11-01",
    }
    payload.update(overrides)
    return payload


def test_policy_get_defaults_and_update(client, db_session):
    seed_types(db_session)
    body = client.get(f"{API}/attendance-settings/policy").json()
    default = body["default"]
    assert body["overrides"] == []
    assert default["shift_start"] == "09:30:00" and default["grace_until"] == "09:35:00"
    assert default["required_minutes"] == 540 and default["saturdays_off"] == [2]
    assert default["weekday_pattern"]["sun"] == "OFF" and default["missing_out_policy"] == "PRESENT"
    assert default["saturday_off_mode"] == "HR_MONTHLY" and default["effective_from"] == "2026-11-01"
    assert default["permission_half_day_above_minutes"] == 120 and default["permission_full_day_above_minutes"] == 240
    assert "late_deduction_leave_order" not in default and "short_hours_flag_max_minutes" not in default

    response = client.put(f"{API}/attendance-settings/policy", json=policy_payload(
        grace_until="09:40:00", saturdays_off=[4, 2, 2], saturday_off_mode="fixed_nth",
        permission_half_day_above_minutes=90, missing_out_policy="half_day",
    ))
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["grace_until"] == "09:40:00" and updated["saturdays_off"] == [2, 4]
    assert updated["saturday_off_mode"] == "FIXED_NTH" and updated["missing_out_policy"] == "HALF_DAY"
    assert updated["permission_half_day_above_minutes"] == 90
    assert updated["updated_by"] is not None
    assert client.get(f"{API}/attendance-settings/policy").json()["default"]["grace_until"] == "09:40:00"

    bad = [
        policy_payload(grace_until="09:00:00"),
        policy_payload(saturdays_off=[6]),
        policy_payload(weekday_pattern={"mon": "WORK"}),
        policy_payload(permission_full_day_above_minutes=60),
        policy_payload(saturday_off_mode="SOMETIMES"),
        policy_payload(missing_out_policy="MAYBE"),
    ]
    for payload in bad:
        assert client.put(f"{API}/attendance-settings/policy", json=payload).status_code == 422


def test_location_overrides(client, db_session):
    seed_types(db_session)
    plant = make_location(db_session, "Plant")
    created = client.post(f"{API}/attendance-settings/policy/overrides",
                          json={**policy_payload(required_minutes=480), "work_location_id": plant.id})
    assert created.status_code == 201, created.text
    override = created.json()
    assert override["work_location_name"] == "Plant" and override["required_minutes"] == 480
    duplicate = client.post(f"{API}/attendance-settings/policy/overrides",
                            json={**policy_payload(), "work_location_id": plant.id})
    assert duplicate.status_code == 409
    assert client.post(f"{API}/attendance-settings/policy/overrides",
                       json={**policy_payload(), "work_location_id": 999}).status_code == 400

    updated = client.put(f"{API}/attendance-settings/policy/overrides/{override['id']}",
                         json=policy_payload(required_minutes=500))
    assert updated.status_code == 200 and updated.json()["required_minutes"] == 500
    assert len(client.get(f"{API}/attendance-settings/policy").json()["overrides"]) == 1
    default_id = client.get(f"{API}/attendance-settings/policy").json()["default"]["id"]
    assert client.delete(f"{API}/attendance-settings/policy/overrides/{default_id}").status_code == 404
    assert client.delete(f"{API}/attendance-settings/policy/overrides/{override['id']}").status_code == 200
    assert client.get(f"{API}/attendance-settings/policy").json()["overrides"] == []


def test_holiday_crud(client, db_session):
    chennai = make_location(db_session, "Chennai")
    created = client.post(f"{API}/attendance-settings/holidays", json={"date": "2027-01-26", "name": "Republic Day"})
    assert created.status_code == 201, created.text
    holiday = created.json()
    assert holiday["work_location_id"] is None and holiday["is_active"] is True
    assert client.post(f"{API}/attendance-settings/holidays",
                       json={"date": "2027-01-26", "name": "Again"}).status_code == 409
    local = client.post(f"{API}/attendance-settings/holidays",
                        json={"date": "2027-01-26", "name": "Local", "work_location_id": chennai.id})
    assert local.status_code == 201 and local.json()["work_location_name"] == "Chennai"
    client.post(f"{API}/attendance-settings/holidays", json={"date": "2026-12-25", "name": "Christmas"})

    assert [h["name"] for h in client.get(f"{API}/attendance-settings/holidays", params={"year": 2027}).json()] == [
        "Republic Day", "Local"]
    updated = client.put(f"{API}/attendance-settings/holidays/{holiday['id']}",
                         json={"date": "2027-01-26", "name": "Republic Day (India)", "is_active": False})
    assert updated.status_code == 200 and updated.json()["is_active"] is False
    assert client.delete(f"{API}/attendance-settings/holidays/{holiday['id']}").status_code == 200
    assert client.delete(f"{API}/attendance-settings/holidays/{holiday['id']}").status_code == 404


def build_holiday_workbook(rows, headers=("Date", "Holiday Name", "Location")):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(client, content):
    return client.post(
        f"{API}/attendance-settings/holidays/import",
        files={"file": ("holidays.xlsx", content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def test_holiday_import(client, db_session):
    make_location(db_session, "Chennai")
    client.post(f"{API}/attendance-settings/holidays", json={"date": "2027-08-15", "name": "Independence Day"})
    content = build_holiday_workbook([
        (datetime(2027, 1, 1), "New Year", None),
        ("26-01-2027", "Republic Day", "All"),
        ("2027-01-15", "Pongal", "chennai"),
        ("15/08/2027", "Independence Day", ""),  # already exists
        ("2027-01-01", "New Year again", None),  # duplicate within file
        ("not a date", "Broken", None),
        ("2027-10-02", None, None),
        ("2027-11-01", "Kannada Rajyotsava", "Bengaluru"),
    ])
    response = upload(client, content)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] == 3 and result["skipped"] == 2
    assert [(e["row_number"], "date" in e["message"].lower()) for e in result["errors"]][0] == (7, True)
    assert len(result["errors"]) == 3 and "Bengaluru" in result["errors"][2]["message"]

    holidays = client.get(f"{API}/attendance-settings/holidays", params={"year": 2027}).json()
    by_name = {h["name"]: h for h in holidays}
    assert by_name["Pongal"]["work_location_name"] == "Chennai"
    assert by_name["Republic Day"]["date"] == "2027-01-26" and by_name["Republic Day"]["work_location_id"] is None

    # Re-importing the same file creates nothing new.
    again = upload(client, content).json()
    assert again["created"] == 0 and again["skipped"] == 5

    missing = upload(client, build_holiday_workbook([("2027-01-01", "x")], headers=("Day", "Holiday Name")))
    assert missing.status_code == 400
    assert upload(client, b"not an excel file").status_code == 400


def test_employee_holidays_and_settings_forbidden(client, db_session):
    chennai = make_location(db_session, "Chennai")
    employee = make_employee(db_session, "E1", location=chennai)
    make_user(db_session, "emp", employee)
    client.post(f"{API}/attendance-settings/holidays", json={"date": "2027-01-26", "name": "Republic Day"})
    client.post(f"{API}/attendance-settings/holidays",
                json={"date": "2027-01-15", "name": "Pongal", "work_location_id": chennai.id})
    client.post(f"{API}/attendance-settings/holidays",
                json={"date": "2027-01-14", "name": "Bhogi", "work_location_id": None, "is_active": False})
    token = token_for(client, "emp")
    mine = client.get(f"{API}/me/holidays", headers=bearer(token), params={"year": 2027}).json()
    assert mine == [{"date": "2027-01-15", "name": "Pongal"}, {"date": "2027-01-26", "name": "Republic Day"}]
    assert client.put(f"{API}/attendance-settings/policy", headers=bearer(token),
                      json=policy_payload()).status_code == 403
    rules = client.get(f"{API}/me/leave-rules", headers=bearer(token)).json()
    assert rules["min_notice_days"] == 1 and rules["has_manager"] is False
