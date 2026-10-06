"""Short-lived server-side store for parsed-but-not-yet-committed upload
batches, keyed by a generated batch_id (UUID4).

Deliberately an in-memory process-local dict for this phase, not a DB-backed
staging table: batches are ephemeral (preview -> commit-or-discard within a
single user session, typically under a minute), never need to survive a
process restart, and never need to be queried/audited after the fact — once
committed, the real audit trail is the `variable_pay_inputs`/
`payroll_run_line_items` rows themselves (via `import_batch_id`/`source`).
A DB-backed staging table would only matter behind multiple app-server
processes/workers where an upload's preview and commit calls could land on
different instances; this app runs as a single FastAPI process today, so
that isn't a real constraint yet. Flagged in the report as a nice-to-have
if/when this moves to a multi-worker deployment.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

_TTL_SECONDS = 3600  # stale/abandoned preview batches are dropped after an hour


@dataclass
class UploadBatch:
    batch_id: str
    run_id: int
    mode: str  # "variable-inputs" | "final-sheet"
    raw_rows: list[dict]
    created_at: float = field(default_factory=time.time)


_BATCHES: dict[str, UploadBatch] = {}


def _evict_expired() -> None:
    now = time.time()
    expired = [batch_id for batch_id, batch in _BATCHES.items() if now - batch.created_at > _TTL_SECONDS]
    for batch_id in expired:
        _BATCHES.pop(batch_id, None)


def create(run_id: int, mode: str, raw_rows: list[dict]) -> UploadBatch:
    _evict_expired()
    batch = UploadBatch(batch_id=str(uuid.uuid4()), run_id=run_id, mode=mode, raw_rows=raw_rows)
    _BATCHES[batch.batch_id] = batch
    return batch


def get(batch_id: str) -> Optional[UploadBatch]:
    _evict_expired()
    return _BATCHES.get(batch_id)


def delete(batch_id: str) -> None:
    _BATCHES.pop(batch_id, None)
