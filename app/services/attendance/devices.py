"""Matrix devices and how their punches become entry / exit.

A device row may be missing (new device, or a create_all database): its mode
is then the DEFAULT_DEVICES one, else IN_OUT. ensure_devices adds rows for
every device name seen so HR can review them in settings.
"""
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.attendance import (
    DEFAULT_DEVICES,
    DEVICE_MODE_IN_OUT,
    AttendanceDevice,
    AttendancePunch,
)

MOBILE_APP_LABEL = "Mobile app"


def device_key(name: Optional[str]) -> str:
    return (name or "").strip()


def default_mode(name: str) -> str:
    seeded = DEFAULT_DEVICES.get(device_key(name))
    return seeded[1] if seeded else DEVICE_MODE_IN_OUT


def default_label(name: str) -> str:
    key = device_key(name)
    seeded = DEFAULT_DEVICES.get(key)
    if seeded:
        return seeded[0]
    return key or MOBILE_APP_LABEL


def device_modes(db: Session) -> dict[str, str]:
    """device_name -> mode for every stored device (inactive ones too)."""
    return {device_key(name): mode for name, mode in db.execute(select(AttendanceDevice.device_name, AttendanceDevice.mode))}


def mode_of(modes: dict[str, str], name: Optional[str]) -> str:
    key = device_key(name)
    return modes.get(key) or default_mode(key)


def device_labels(db: Session) -> dict[str, str]:
    labels = {}
    for name, display in db.execute(select(AttendanceDevice.device_name, AttendanceDevice.display_name)):
        labels[device_key(name)] = (display or "").strip() or default_label(name)
    return labels


def label_of(labels: dict[str, str], name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    key = device_key(name)
    return labels.get(key) or default_label(key)


def ensure_devices(db: Session, names: Iterable[Optional[str]] = ()) -> int:
    """Adds the seeded devices and every name in `names` that has no row yet
    (seeded mode, else IN_OUT). Returns the number added. Does not commit."""
    existing = {device_key(n) for n in db.scalars(select(AttendanceDevice.device_name))}
    wanted = set(DEFAULT_DEVICES) | {device_key(n) for n in names}
    added = 0
    for key in sorted(wanted - existing):
        db.add(AttendanceDevice(device_name=key, display_name=default_label(key), mode=default_mode(key),
                                is_active=True))
        added += 1
    if added:
        db.flush()
    return added


def list_devices(db: Session) -> list[AttendanceDevice]:
    """Every device, including names only seen in punches (rows are created
    for them). Does not commit."""
    seen = db.scalars(select(AttendancePunch.device_name).distinct()).all()
    ensure_devices(db, seen)
    return db.scalars(select(AttendanceDevice).order_by(AttendanceDevice.device_name)).all()
