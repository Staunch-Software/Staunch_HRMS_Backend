"""Seeds pf_configs / esi_configs (long-stable central-India rules, seeded
is_active=True) and pt_states / pt_slabs / tds_slabs / tds_config.

*** IMPORTANT — READ BEFORE USING FOR REAL PAYROLL ***
Every PT slab and every TDS slab/config row seeded here is marked
`is_verified=False` with a `notes` disclaimer. These figures come from the
seeder's general training knowledge, NOT a live/current lookup of state PT
Acts or the current Finance Act — Indian PT slabs change per state
government on no fixed schedule, and TDS slabs/rebates change every Union
Budget. CONFIRM EVERY ROW AGAINST CURRENT LAW BEFORE RUNNING LIVE PAYROLL.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.models.statutory import ESIConfig, PFConfig, PTSlab, PTState, TDSConfig, TDSSlab

SEED_EFFECTIVE_FROM = date(2024, 4, 1)

PT_UNVERIFIED_NOTE = (
    "Seeded from general knowledge, not verified against a live source as of "
    "this build — confirm against the current {state} Professional Tax Act "
    "before running live payroll."
)

TDS_UNVERIFIED_NOTE = (
    "Seeded from general knowledge, not verified against the current Finance "
    "Act as of this build — confirm against the latest Union Budget / CBDT "
    "notification before running live payroll."
)

# state_code -> (state name, [(income_min, income_max, tax_amount)], periodicity)
PT_SLAB_SEED = {
    "TN": (
        "Tamil Nadu",
        [
            (0, 21000, 0),
            (21001, 30000, 135),
            (30001, 45000, 315),
            (45001, 60000, 690),
            (60001, 75000, 1025),
            (75001, None, 1250),
        ],
        "HalfYearly",
    ),
    "MH": (
        "Maharashtra",
        [
            (0, 7500, 0),
            (7501, 10000, 175),
            (10001, None, 200),
        ],
        "Monthly",
    ),
    "KA": (
        "Karnataka",
        [
            (0, 24999, 0),
            (25000, None, 200),
        ],
        "Monthly",
    ),
    "DL": (
        # Delhi currently levies no Professional Tax — seeded as an explicit
        # zero-tax marker (not simply omitted) so PT resolves to a verified 0
        # rather than "no state config found".
        "Delhi",
        [
            (0, None, 0),
        ],
        "Monthly",
    ),
}

# financial_year -> [(income_from, income_to, rate)] — New regime only.
TDS_SLAB_SEED = {
    "2026-27": [
        (0, 300000, 0.0),
        (300000, 600000, 0.05),
        (600000, 900000, 0.10),
        (900000, 1200000, 0.15),
        (1200000, 1500000, 0.20),
        (1500000, None, 0.30),
    ]
}

TDS_CONFIG_SEED = {
    "financial_year": "2026-27",
    "standard_deduction": 75000,
    "cess_rate": 0.04,
    "rebate_87a_income_limit": 700000,
    "rebate_87a_max_amount": 25000,
}


def seed_pf_config(db: Session) -> None:
    if db.query(PFConfig).filter(PFConfig.is_active.is_(True)).first():
        return
    db.add(
        PFConfig(
            effective_from=SEED_EFFECTIVE_FROM,
            effective_to=None,
            employee_rate=0.12,
            employer_eps_rate=0.0833,
            employer_epf_rate=0.0367,
            wage_ceiling=15000,
            eps_wage_ceiling=15000,
            apply_ceiling=True,
            is_active=True,
        )
    )
    db.commit()


def seed_esi_config(db: Session) -> None:
    if db.query(ESIConfig).filter(ESIConfig.is_active.is_(True)).first():
        return
    db.add(
        ESIConfig(
            effective_from=SEED_EFFECTIVE_FROM,
            effective_to=None,
            employee_rate=0.0075,
            employer_rate=0.0325,
            wage_eligibility_threshold=21000,
            is_active=True,
        )
    )
    db.commit()


def seed_pt(db: Session) -> None:
    for code, (name, slabs, periodicity) in PT_SLAB_SEED.items():
        state = db.query(PTState).filter(PTState.code == code).first()
        if not state:
            state = PTState(name=name, code=code)
            db.add(state)
            db.commit()
            db.refresh(state)

        existing_slabs = db.query(PTSlab).filter(PTSlab.state_id == state.id).count()
        if existing_slabs:
            continue

        for income_min, income_max, tax_amount in slabs:
            db.add(
                PTSlab(
                    state_id=state.id,
                    effective_from=SEED_EFFECTIVE_FROM,
                    effective_to=None,
                    income_min=income_min,
                    income_max=income_max,
                    tax_amount=tax_amount,
                    periodicity=periodicity,
                    is_verified=False,
                    notes=PT_UNVERIFIED_NOTE.format(state=name),
                )
            )
        db.commit()


def seed_tds(db: Session) -> None:
    for financial_year, slabs in TDS_SLAB_SEED.items():
        existing = (
            db.query(TDSSlab)
            .filter(TDSSlab.financial_year == financial_year, TDSSlab.regime == "New")
            .count()
        )
        if existing:
            continue
        for income_from, income_to, rate in slabs:
            db.add(
                TDSSlab(
                    regime="New",
                    financial_year=financial_year,
                    income_from=income_from,
                    income_to=income_to,
                    rate=rate,
                    is_verified=False,
                    notes=TDS_UNVERIFIED_NOTE,
                )
            )
        db.commit()

    if not db.query(TDSConfig).filter(TDSConfig.financial_year == TDS_CONFIG_SEED["financial_year"]).first():
        db.add(
            TDSConfig(
                **TDS_CONFIG_SEED,
                is_active=True,
                is_verified=False,
                notes=TDS_UNVERIFIED_NOTE,
            )
        )
        db.commit()


def seed_statutory_all(db: Session) -> None:
    seed_pf_config(db)
    seed_esi_config(db)
    seed_pt(db)
    seed_tds(db)
