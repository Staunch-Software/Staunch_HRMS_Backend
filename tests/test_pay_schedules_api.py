MONTHLY_SCHEDULE = {
    "schedule_name": "Monthly - End of Month",
    "pay_frequency": "Monthly",
    "payment_day_rule": {"type": "LastDayOfMonth"},
    "holiday_adjustment": "Before",
    "processing_day_offset": 3,
    "is_active": True,
    "first_effective_pay_date": "2025-04-30",
    "description": "Standard monthly schedule for all employees.",
    "applies_to": None,
}


def test_create_and_list_pay_schedule(client):
    response = client.post("/api/v1/pay-schedules", json=MONTHLY_SCHEDULE)
    assert response.status_code == 201
    body = response.json()
    assert body["schedule_name"] == "Monthly - End of Month"
    assert body["payment_day_rule"] == {"type": "LastDayOfMonth"}

    list_response = client.get("/api/v1/pay-schedules")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1


def test_applies_to_collapses_to_null_when_empty(client):
    data = dict(MONTHLY_SCHEDULE, applies_to={"departments": [], "locations": []})
    response = client.post("/api/v1/pay-schedules", json=data)
    assert response.status_code == 201
    assert response.json()["applies_to"] is None


def test_applies_to_kept_when_specific(client):
    data = dict(MONTHLY_SCHEDULE, applies_to={"departments": ["1"], "locations": []})
    response = client.post("/api/v1/pay-schedules", json=data)
    assert response.status_code == 201
    assert response.json()["applies_to"] == {"departments": ["1"], "locations": []}


def test_update_and_delete_pay_schedule(client):
    create_resp = client.post("/api/v1/pay-schedules", json=MONTHLY_SCHEDULE)
    schedule_id = create_resp.json()["id"]

    updated = dict(MONTHLY_SCHEDULE, is_active=False)
    update_resp = client.put(f"/api/v1/pay-schedules/{schedule_id}", json=updated)
    assert update_resp.status_code == 200
    assert update_resp.json()["is_active"] is False

    delete_resp = client.delete(f"/api/v1/pay-schedules/{schedule_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"success": True}

    delete_again = client.delete(f"/api/v1/pay-schedules/{schedule_id}")
    assert delete_again.status_code == 404
