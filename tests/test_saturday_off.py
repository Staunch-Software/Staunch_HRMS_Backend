"""HR-chosen Saturday off (HR_MONTHLY) vs the fixed nth-Saturday rule, in the
calendar, leave day counting, monthly processing and the settings API.
November 2026 Saturdays: 7, 14, 21, 28."""
from datetime import date
from decimal import Decimal

from app.models.attendance_policy import SaturdayOff
from app.services.attendance import monthly
from app.services.leave.calendar import KIND_WEEKLY_OFF, KIND_WORK, day_kinds, ensure_default_policy
from app.services.leave.days import count_leave_days
from tests.leave_helpers import API, make_employee, make_location, seed_types
from tests.test_attendance_monthly import TODAY, _groups, _profile, d, full_month, on

WARNING = "Saturday off for 2026-11 not chosen — assumed 2nd Saturday (14 Nov)"


def choose(db, day, location=None):
    db.add(SaturdayOff(month=day.strftime("%Y-%m"), off_date=day, work_location_id=location.id if location else None))
    db.commit()


def kinds(db, emp):
    return {day: kind for day, (kind, _) in day_kinds(db, emp, d(1), d(30)).items()}


def test_chosen_first_saturday_is_off_and_second_is_working(db_session):
    seed_types(db_session)
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    choose(db_session, d(7))
    k = kinds(db_session, emp)
    assert k[d(7)] == KIND_WEEKLY_OFF and k[d(14)] == KIND_WORK and k[d(21)] == KIND_WORK

    full_month(db_session, emp)
    result = monthly.resolve_month(db_session, emp, "2026-11", today=TODAY)
    assert on(result, d(7)).status == monthly.ST_OFF
    assert on(result, d(14)).status == monthly.ST_PRESENT
    assert result.totals["working_days"] == 24 and Decimal(result.totals["total_lop"]) == 0
    assert not any(w.startswith("Saturday off") for w in result.warnings)
    assert result.totals["saturday_off_assumed"] is False


def test_unset_month_assumes_second_saturday_with_warning(db_session):
    seed_types(db_session)
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    k = kinds(db_session, emp)
    assert k[d(14)] == KIND_WEEKLY_OFF and k[d(7)] == KIND_WORK
    result = monthly.resolve_month(db_session, emp, "2026-11", today=TODAY)
    assert result.warnings[0] == WARNING
    assert result.totals["saturday_off_assumed"] is True


def test_location_choice_wins_over_all_locations(db_session):
    plant = make_location(db_session, "Plant")
    office = make_employee(db_session, "ST0001")
    factory = make_employee(db_session, "ST0002", location=plant)
    choose(db_session, d(7))
    choose(db_session, d(21), plant)
    assert kinds(db_session, office)[d(7)] == KIND_WEEKLY_OFF
    assert kinds(db_session, office)[d(21)] == KIND_WORK
    assert kinds(db_session, factory)[d(21)] == KIND_WEEKLY_OFF
    assert kinds(db_session, factory)[d(7)] == KIND_WORK
    assert kinds(db_session, factory)[d(14)] == KIND_WORK


def test_fixed_nth_mode_ignores_choices(db_session):
    seed_types(db_session)
    policy = ensure_default_policy(db_session)
    policy.saturday_off_mode = "FIXED_NTH"
    policy.saturdays_off = [2, 4]
    db_session.commit()
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    choose(db_session, d(7))
    k = kinds(db_session, emp)
    assert k[d(7)] == KIND_WORK and k[d(14)] == KIND_WEEKLY_OFF and k[d(28)] == KIND_WEEKLY_OFF
    result = monthly.resolve_month(db_session, emp, "2026-11", today=TODAY)
    assert not any(w.startswith("Saturday off") for w in result.warnings)


def test_leave_day_counting_uses_the_chosen_saturday(db_session):
    el = seed_types(db_session)["EL"]  # no sandwich rule
    emp = make_employee(db_session, "ST0001")
    assert count_leave_days(db_session, emp, el, d(6), "FULL", d(9), "FULL") == 3  # 7th assumed working
    choose(db_session, d(7))
    assert count_leave_days(db_session, emp, el, d(6), "FULL", d(9), "FULL") == 2
    assert count_leave_days(db_session, emp, el, d(13), "FULL", d(16), "FULL") == 3  # 14th now working


def test_saturday_off_api(client, db_session):
    plant = make_location(db_session, "Plant")
    year = client.get(f"{API}/attendance-settings/saturday-off", params={"year": 2026}).json()
    assert year["saturday_off_mode"] == "HR_MONTHLY" and len(year["months"]) == 12
    nov = year["months"][10]
    assert nov["month"] == "2026-11" and nov["off_date"] is None and nov["assumed_date"] == "2026-11-14"
    assert nov["saturdays"] == ["2026-11-07", "2026-11-14", "2026-11-21", "2026-11-28"]

    url = f"{API}/attendance-settings/saturday-off/2026-11"
    assert client.put(url, json={"off_date": "2026-11-09"}).status_code == 400  # a Monday
    assert client.put(url, json={"off_date": "2026-12-05"}).status_code == 400  # other month
    saved = client.put(url, json={"off_date": "2026-11-07"})
    assert saved.status_code == 200 and saved.json()["effective_date"] == "2026-11-07"
    assert client.put(url, json={"off_date": "2026-11-21"}).json()["off_date"] == "2026-11-21"  # upsert
    assert db_session.query(SaturdayOff).count() == 1

    scoped = client.get(f"{API}/attendance-settings/saturday-off",
                        params={"year": 2026, "work_location_id": plant.id}).json()["months"][10]
    assert scoped["off_date"] is None and scoped["inherited_date"] == "2026-11-21"
    client.put(url, json={"off_date": "2026-11-28", "work_location_id": plant.id})
    assert client.delete(url, params={"work_location_id": plant.id}).status_code == 200
    assert client.delete(url).status_code == 200
    assert client.delete(url).status_code == 404


def test_monthly_listing_warns_until_chosen(client, db_session, seeded_admin):
    seed_types(db_session)
    chennai = _groups(db_session)["CHENNAI_HO"]
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    _profile(db_session, emp, chennai)
    monthly.process_month(db_session, "2026-11", chennai.id, seeded_admin, today=TODAY)
    db_session.commit()
    params = {"month": "2026-11", "pay_group_id": chennai.id}
    listing = client.get(f"{API}/attendance/monthly", params=params).json()
    assert listing["saturday_off_warnings"] == [WARNING] and listing["warnings"][0] == WARNING

    client.put(f"{API}/attendance-settings/saturday-off/2026-11", json={"off_date": "2026-11-07"})
    stale = client.get(f"{API}/attendance/monthly", params=params).json()["saturday_off_warnings"]
    assert len(stale) == 1 and "process the month again" in stale[0]
