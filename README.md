# Staunch Payroll — Backend (Phase 1)

FastAPI + SQLAlchemy + PostgreSQL backend for the Staunch Payroll frontend.
Phase 1 covers: auth, employees + bank accounts, departments/designations/
work-locations, salary components, pay schedules, and a dashboard summary
endpoint. Salary structure assignment, the calc engine, Excel upload, and
the ICICI bank file are later phases (see the project plan).

## Setup

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then edit .env if you need non-default values
```

### Database

By default `DATABASE_URL` in `.env.example` points at a local SQLite file
(`payroll_dev.db`) so the project runs with zero external services. This is
fine for quick local testing but not for real usage.

For real usage, install PostgreSQL locally (or point at an existing server)
and set in `.env`:

```
DATABASE_URL=postgresql+psycopg2://payroll_user:payroll_pass@localhost:5432/staunch_payroll
```

Create the database/role first, e.g.:

```sql
CREATE ROLE payroll_user WITH LOGIN PASSWORD 'payroll_pass';
CREATE DATABASE staunch_payroll OWNER payroll_user;
```

### Migrations

This project uses **Alembic**, not just `create_all()` — a deliberate
deviation from the "auto-create via models, never standalone migrations"
convention used elsewhere, because this is financial data that will
accumulate real rows fast; `create_all()` can't alter an existing table
(add a column, tighten a constraint) once there's live data, and the
statutory tables (Phase 2+) need auditable *schema* history alongside
their already-versioned *data* history. Flagging this explicitly per the
plan — confirm/override if you'd rather stick with the create_all-only
convention for this project too.

Apply migrations:

```bash
alembic upgrade head
```

Generate a new migration after changing models:

```bash
alembic revision --autogenerate -m "describe the change"
```

`db/init_db.py` is kept as a fast dev-only alternative (`create_all()` +
seed) for quick iteration, but Alembic is the real path once anything is
deployed with live data.

### Seed data

```bash
python -m app.db.seed.seed_all
```

This seeds (idempotently — safe to re-run):
- An admin user (`username: admin`). **The password is randomly generated
  and printed once to the console on first run** — copy it immediately,
  it is never stored in plaintext anywhere. If you lose it, delete the
  `admin_users` row and re-run the seed, or reset it directly in the DB.
- 5 departments (Engineering, Sales, Marketing, Human Resources, Operations)
  and a few designations.
- 4 work locations with their state codes (Chennai/TN, Mumbai HQ/MH,
  Delhi/DL, Bengaluru/KA) — the state code matters for Phase 2's
  Professional Tax slab lookup.
- The 6 salary components ported verbatim from the frontend mock (BASIC,
  HRA, PF_EE, PT, CANTEEN, TRAVEL_REIM).

### Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

- Health check: `GET http://localhost:8000/health`
- API base: `http://localhost:8000/api/v1`
- Interactive docs: `http://localhost:8000/docs`

CORS is open to `http://localhost:3000` by default (the CRA dev server) —
change `CORS_ORIGINS` in `.env` for other environments.

### Tests

```bash
pytest
```

Tests run against an in-memory SQLite database (see `tests/conftest.py`) —
sufficient for Phase 1 since there's no JSONB-heavy logic yet. Later
phases with heavier Postgres-specific behavior (JSONB queries, etc.) may
need a real Postgres test schema instead.

## Known gap: bank account number is not encrypted at rest

`EmployeeBankAccount.account_number` is stored as a plain string column in
Phase 1 (see the `TODO` in `app/models/employee.py`). This was a deliberate
scope decision to keep Phase 1 contained — **encrypt this before any real
production data goes in**, e.g. via Postgres `pgcrypto` or an
application-level Fernet key. IFSC code and account number format are
validated in the Pydantic schema (`app/schemas/employee.py`) but that does
not substitute for encryption at rest.

## Auth note

JWT auth (`POST /auth/login`, `GET /auth/me`) is implemented and working,
but the CRUD endpoints (`employees`, `salary-components`, `pay-schedules`,
`org`, `payroll-summary`) are **not currently gated** behind
`get_current_user`. This is because the Phase 1 frontend scope does not
wire `Admin_Login.js` to the new backend or attach a bearer token to
existing calls — gating those endpoints now would break the working Pay
Schedule / Salary Components / Employee pages with no way to authenticate
from the UI yet. Add `Depends(get_current_user)` to each router once the
login page is wired up (a good candidate for the start of Phase 2).
