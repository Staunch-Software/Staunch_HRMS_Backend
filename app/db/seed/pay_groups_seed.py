"""Seeds the seven location/client-contract pay groups. Idempotent: an
existing group (matched on code) is left untouched, because HR may have
revised its params (minimum wages change twice a year) since it was seeded.
"""
from sqlalchemy.orm import Session

from app.models.pay_group import PayGroup

_COMMON_DAILY_WAGE = {
    "bonus_rate": 0.0833,
    "bonus_wage_ceiling": 21000,
    "ot_multiplier": 2,
}

PAY_GROUPS_SEED = [
    {
        "code": "CHENNAI_HO",
        "name": "Chennai HO — monthly salaried",
        "calc_model": "MONTHLY_SALARIED",
        "description": "Head-office monthly salary: 80% statutory gross, Basic&DA 70% / HRA 30%, LOP on 30 days.",
        "params": {
            "pf_applicable_default": False,
            "esi_applicable_default": False,
            "net_rounding": "NONE",
            "standard_days": 30,
            "statutory_gross_pct": 0.80,
            "basic_da_pct": 0.70,
            "hra_pct": 0.30,
            "employee_pf_rate": 0.12,
            "employee_pf_cap_default": 3000,
            "employee_esi_rate": 0.0075,
            "employer_esi_rate": 0.0325,
            "employer_pf_rate": 0.12,
            "employer_pf_cap": 3000,
            "employer_pf_admin_rate": 0.01,
        },
    },
    {
        "code": "TUTICORIN",
        "name": "Tuticorin — daily flat rate",
        "calc_model": "DAILY_FLAT_RATE",
        "description": "Per-day salary (base rate + 492.30 allowance) x days present, flat PF on 25,000.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "ROUND",
            "standard_days": 30,
            "per_day_allowance": 492.3,
            "pf_flat_base": 25000,
            "pf_rate": 0.12,
            "employee_esi_rate": 0,
        },
    },
    {
        "code": "KKNPP_HARI",
        "name": "KKNPP (HARI)",
        "calc_model": "DAILY_WAGE_STANDARD_DAYS",
        "description": "Day wage x 26 split Basic 50% / DA 25% / HRA 50% of basic + special, prorated on days present.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "NONE",
            "standard_days": 26,
            "basic_pct": 0.50,
            "da_pct": 0.25,
            "hra_pct_of_basic": 0.50,
            "special_allowance_per_day": 149.73,
            "transport_per_day": 35.93,
            "ot_hours_per_day": 9,
            "employee_pf_rate": 0.12,
            "employee_pf_cap": 1800,
            "employer_pf_wage_base": 15000,
            "employer_pf_rate": 0.12,
            "ctc_bonus_wage_base": 21000,
            **_COMMON_DAILY_WAGE,
        },
    },
    {
        "code": "MCBS",
        "name": "MCBS",
        "calc_model": "DAILY_WAGE_ATTENDANCE",
        "description": "Wage x days present + 149.73/day allowance, OT at 2x (wage/9), bonus, PF flat above 15,000.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "ROUNDUP",
            "standard_days": 26,
            "allowance_per_day_by_skill": {"DEFAULT": 149.73},
            "transport_per_day_by_skill": {"DEFAULT": 0},
            "ot_hours_per_day": 9,
            "pf_rate": 0.12,
            "pf_threshold": 15000,
            "pf_flat_base": 25000,
            "ctc_fixed_addon": 0,
            **_COMMON_DAILY_WAGE,
        },
    },
    {
        "code": "CCTV",
        "name": "CCTV",
        "calc_model": "DAILY_WAGE_ATTENDANCE",
        "description": "Wage x days present + skill allowance, OT at 2x (wage/9), bonus, PF flat above 25,000, transport outside PF.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "ROUNDUP",
            "standard_days": 26,
            "allowance_per_day_by_skill": {"HIGHLY_SKILLED": 149.73, "SKILLED": 144.73, "SEMI_SKILLED": 136.6},
            "transport_per_day_by_skill": {"HIGHLY_SKILLED": 35.93, "SKILLED": 34.73, "SEMI_SKILLED": 32.78},
            "ot_hours_per_day": 9,
            "pf_rate": 0.12,
            "pf_threshold": 25000,
            "pf_flat_base": 25000,
            "ctc_fixed_addon": 0,
            **_COMMON_DAILY_WAGE,
        },
    },
    {
        "code": "ESFAS",
        "name": "ESFAS",
        "calc_model": "DAILY_WAGE_ATTENDANCE",
        "description": "Wage x days present + skill allowance, OT at 2x (wage/8), bonus, PF flat above 25,000; fixed-amount engineers.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "ROUNDUP",
            "standard_days": 26,
            "allowance_per_day_by_skill": {"HIGHLY_SKILLED": 149.73, "SKILLED": 144.73, "SEMI_SKILLED": 136.6},
            "transport_per_day_by_skill": {"DEFAULT": 0},
            "ot_hours_per_day": 8,
            "pf_rate": 0.12,
            "pf_threshold": 25000,
            "pf_flat_base": 25000,
            "ctc_fixed_addon": 3600,
            **_COMMON_DAILY_WAGE,
        },
    },
    {
        "code": "BHAVNAGAR",
        "name": "Bhavnagar",
        "calc_model": "DAILY_WAGE_BASIC_DA",
        "description": "Day wage x 26 prorated; per-day Basic/DA by skill, balance special; PF, ESI, Gujarat PT 208.",
        "params": {
            "pf_applicable_default": True,
            "esi_applicable_default": False,
            "net_rounding": "ROUND",
            "standard_days": 26,
            "basic_per_day_by_skill": {"SKILLED": 579, "SEMI_SKILLED": 494},
            "da_per_day_by_skill": {"SKILLED": 339, "SEMI_SKILLED": 287},
            "pf_rate": 0.12,
            "pf_wage_ceiling": 25000,
            "employee_esi_rate": 0.0075,
            "employer_esi_rate": 0.0325,
            "professional_tax_monthly": 208,
            "employer_insurance_monthly": 350,
            "bonus_rate": 0.0833,
        },
    },
]

SEED_PARAMS_BY_CODE = {g["code"]: g["params"] for g in PAY_GROUPS_SEED}
SEED_CALC_MODEL_BY_CODE = {g["code"]: g["calc_model"] for g in PAY_GROUPS_SEED}


def seed_pay_groups(db: Session) -> None:
    created = 0
    for data in PAY_GROUPS_SEED:
        if db.query(PayGroup).filter(PayGroup.code == data["code"]).first():
            continue
        db.add(PayGroup(is_active=True, **{**data, "params": dict(data["params"])}))
        created += 1
    db.commit()
    print(f"Pay groups: {created} created, {len(PAY_GROUPS_SEED) - created} already present.")
