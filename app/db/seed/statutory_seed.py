"""Seeds pf_configs / esi_configs (long-stable central-India rules, seeded
is_active=True) and pt_states / pt_slabs / tds_slabs / tds_config.

*** IMPORTANT — READ BEFORE USING FOR REAL PAYROLL ***
Every PT slab seeded here is marked `is_verified=False` with a `notes`
disclaimer: the figures come from general knowledge, not a live lookup of
state PT Acts, which change per state on no fixed schedule. Confirm them
against current law before running live payroll.

The FY 2026-27 TDS rows (both regimes) are `is_verified=True`: they match the
incometax.gov.in rate table for AY 2026-27, which Budget 2026-27 left
unchanged. Add a new financial_year entry to TDS_SEED after every Budget.
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

TDS_VERIFIED_NOTE = (
    "Verified against the incometax.gov.in tax-rate table for AY 2026-27; Union "
    "Budget 2026-27 left the salary slabs, standard deduction, rebate, surcharge "
    "and cess unchanged for tax year 2026-27. Individual below 60 years."
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

# financial_year -> regime -> (config, [(income_from, income_to, rate)])
TDS_SEED = {
    "2026-27": {
        "New": (
            {
                "standard_deduction": 75000,
                "cess_rate": 0.04,
                "rebate_87a_income_limit": 1200000,
                "rebate_87a_max_amount": 60000,
                "surcharge_bands": [
                    {"above": 5000000, "rate": 0.10},
                    {"above": 10000000, "rate": 0.15},
                    {"above": 20000000, "rate": 0.25},
                ],
            },
            [
                (0, 400000, 0.0),
                (400000, 800000, 0.05),
                (800000, 1200000, 0.10),
                (1200000, 1600000, 0.15),
                (1600000, 2000000, 0.20),
                (2000000, 2400000, 0.25),
                (2400000, None, 0.30),
            ],
        ),
        "Old": (
            {
                "standard_deduction": 50000,
                "cess_rate": 0.04,
                "rebate_87a_income_limit": 500000,
                "rebate_87a_max_amount": 12500,
                "surcharge_bands": [
                    {"above": 5000000, "rate": 0.10},
                    {"above": 10000000, "rate": 0.15},
                    {"above": 20000000, "rate": 0.25},
                    {"above": 50000000, "rate": 0.37},
                ],
            },
            [
                (0, 250000, 0.0),
                (250000, 500000, 0.05),
                (500000, 1000000, 0.20),
                (1000000, None, 0.30),
            ],
        ),
    }
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
    """Per (financial_year, regime): slabs and config are each skipped when
    rows already exist, so HR edits are never overwritten."""
    for financial_year, regimes in TDS_SEED.items():
        for regime, (config, slabs) in regimes.items():
            has_slabs = (
                db.query(TDSSlab)
                .filter(TDSSlab.financial_year == financial_year, TDSSlab.regime == regime)
                .count()
            )
            if not has_slabs:
                for income_from, income_to, rate in slabs:
                    db.add(
                        TDSSlab(
                            regime=regime,
                            financial_year=financial_year,
                            income_from=income_from,
                            income_to=income_to,
                            rate=rate,
                            is_verified=True,
                            notes=TDS_VERIFIED_NOTE,
                        )
                    )
            has_config = (
                db.query(TDSConfig)
                .filter(TDSConfig.financial_year == financial_year, TDSConfig.regime == regime)
                .first()
            )
            if not has_config:
                db.add(
                    TDSConfig(
                        financial_year=financial_year,
                        regime=regime,
                        **config,
                        is_active=True,
                        is_verified=True,
                        notes=TDS_VERIFIED_NOTE,
                    )
                )
            db.commit()


def seed_statutory_all(db: Session) -> None:
    seed_pf_config(db)
    seed_esi_config(db)
    seed_pt(db)
    seed_tds(db)
