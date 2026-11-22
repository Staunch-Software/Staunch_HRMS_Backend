"""mobile attendance: office geofences, registered phones, mobile punches

Adds office_locations, mobile_devices, mobile_punches and the
mobile_attendance_settings singleton, plus attendance_punches.source
(MATRIX | MOBILE; existing rows backfilled to MATRIX) and
attendance_punches.mobile_punch_id. Hand-written.

Revision ID: e3b9f2a6c418
Revises: b82530ba4b90
Create Date: 2026-11-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e3b9f2a6c418'
down_revision: Union[str, None] = 'b82530ba4b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("attendance_punches", sa.Column("source", sa.String(length=10), nullable=True))
    op.add_column("attendance_punches", sa.Column("mobile_punch_id", sa.Integer(), nullable=True))
    op.create_index("ix_attendance_punches_mobile_punch_id", "attendance_punches", ["mobile_punch_id"])
    op.execute("UPDATE attendance_punches SET source = 'MATRIX' WHERE source IS NULL")

    op.create_table(
        "office_locations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("work_location_id", sa.Integer(),
                  sa.ForeignKey("work_locations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("wifi_bssids", _json(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(length=150), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_office_locations_work_location_id", "office_locations", ["work_location_id"])

    op.create_table(
        "mobile_devices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_uuid", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("platform", sa.String(length=30), nullable=True),
        sa.Column("device_token_hash", sa.String(length=128), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=150), nullable=True),
    )
    op.create_index("ix_mobile_devices_employee_id", "mobile_devices", ["employee_id"])
    op.create_index(
        "uq_mobile_device_active_employee", "mobile_devices", ["employee_id"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"), sqlite_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "mobile_punches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_uuid", sa.String(length=64), nullable=False, unique=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_code", sa.String(length=30), nullable=False),
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("mobile_devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("punch_type", sa.String(length=3), nullable=False),
        sa.Column("device_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trusted_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("punch_time", sa.DateTime(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("is_mock", sa.Boolean(), nullable=False),
        sa.Column("clock_suspect", sa.Boolean(), nullable=False),
        sa.Column("wifi_bssid", sa.String(length=17), nullable=True),
        sa.Column("wifi_match", sa.Boolean(), nullable=False),
        sa.Column("office_location_id", sa.Integer(),
                  sa.ForeignKey("office_locations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("captured_offline", sa.Boolean(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("flags", _json(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attendance_punch_id", sa.Integer(),
                  sa.ForeignKey("attendance_punches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("review_action", sa.String(length=10), nullable=True),
        sa.Column("reviewed_by", sa.String(length=150), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_mobile_punches_employee_id", "mobile_punches", ["employee_id"])
    op.create_index("ix_mobile_punches_punch_time", "mobile_punches", ["punch_time"])
    op.create_index("ix_mobile_punches_status", "mobile_punches", ["status"])

    op.create_table(
        "mobile_attendance_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_upload_age_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("max_accuracy_m", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("clock_tolerance_minutes", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("allow_web_punch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=150), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mobile_attendance_settings")
    op.drop_index("ix_mobile_punches_status", table_name="mobile_punches")
    op.drop_index("ix_mobile_punches_punch_time", table_name="mobile_punches")
    op.drop_index("ix_mobile_punches_employee_id", table_name="mobile_punches")
    op.drop_table("mobile_punches")
    op.drop_index("uq_mobile_device_active_employee", table_name="mobile_devices")
    op.drop_index("ix_mobile_devices_employee_id", table_name="mobile_devices")
    op.drop_table("mobile_devices")
    op.drop_index("ix_office_locations_work_location_id", table_name="office_locations")
    op.drop_table("office_locations")
    op.drop_index("ix_attendance_punches_mobile_punch_id", table_name="attendance_punches")
    op.drop_column("attendance_punches", "mobile_punch_id")
    op.drop_column("attendance_punches", "source")
