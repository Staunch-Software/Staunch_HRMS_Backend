from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.pay_schedule import PaySchedule
from app.schemas.pay_schedule import PayScheduleCreate, PayScheduleRead, PayScheduleUpdate

router = APIRouter(tags=["pay-schedules"], dependencies=[Depends(get_current_user)])


def _normalize_applies_to(applies_to: dict | None) -> dict | None:
    """Mirrors the mock's rule: an appliesTo with no departments/locations
    selected collapses to null (= all employees)."""
    if not applies_to:
        return None
    departments = applies_to.get("departments") or []
    locations = applies_to.get("locations") or []
    if not departments and not locations:
        return None
    return {"departments": departments, "locations": locations}


@router.get("/pay-schedules", response_model=list[PayScheduleRead])
def list_pay_schedules(db: Session = Depends(get_db)):
    return db.query(PaySchedule).order_by(PaySchedule.id).all()


@router.post("/pay-schedules", response_model=PayScheduleRead, status_code=status.HTTP_201_CREATED)
def create_pay_schedule(payload: PayScheduleCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["applies_to"] = _normalize_applies_to(data.get("applies_to"))
    schedule = PaySchedule(**data)
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/pay-schedules/{schedule_id}", response_model=PayScheduleRead)
def update_pay_schedule(schedule_id: int, payload: PayScheduleUpdate, db: Session = Depends(get_db)):
    schedule = db.get(PaySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    data = payload.model_dump()
    data["applies_to"] = _normalize_applies_to(data.get("applies_to"))
    for field, value in data.items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/pay-schedules/{schedule_id}")
def delete_pay_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.get(PaySchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found for deletion")
    db.delete(schedule)
    db.commit()
    return {"success": True}
