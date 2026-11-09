"""DAILY_WAGE_BASIC_DA — day wage on standard days, split into per-day Basic
and DA by skill with the balance as special allowance (BHAVNAGAR).

Per employee: daily_wage (G), skill_level, ESI applicability.
Monthly inputs: lop_days (J) or present_days (K), ADJUSTMENT (manual amount
added inside the net rounding).

  H  monthly gross = G * I                      (I = standard_days)
  K  days          = I - LOP
  L  this month    = ROUNDUP(H * K / I)
  M  basic         = basic_per_day_by_skill[skill] * K
  N  DA            = da_per_day_by_skill[skill] * K
  O  special       = L - (M + N)
  P  gross         = M + N + O
  V  employee PF   = ROUND(MIN(M + N, pf_wage_ceiling) * pf_rate)   if PF (employer PF U: same, cost)
  X  employee ESI  = P * employee_esi_rate                          if ESI (not rounded; employer W = P * employer_esi_rate)
  Z  professional tax = professional_tax_monthly
  Y  insurance     = employer_insurance_monthly (cost only)
  AC bonus         = ROUND(H * bonus_rate)                           (CTC only)
  net = ROUND(P - V - X - Z + adjustment)                            (net_rounding = ROUND)
"""
from app.services.payroll_engine.pay_groups.base import (
    Ctx,
    ParamField,
    PayGroupStrategy,
    skill_rate,
    xl_round,
    xl_roundup,
)


class DailyWageBasicDaStrategy(PayGroupStrategy):
    calc_model = "DAILY_WAGE_BASIC_DA"
    description = "Day wage x standard days, prorated; per-day Basic/DA by skill, balance as special; PF, ESI, PT."
    PROFILE_REQUIREMENTS = (("daily_wage",),)
    USES_SKILL_TABLES = True
    PARAM_SPEC = {
        "standard_days": ParamField("number", "Standard days per month (26)"),
        "basic_per_day_by_skill": ParamField("skill_table", "Basic per day by skill level"),
        "da_per_day_by_skill": ParamField("skill_table", "DA per day by skill level"),
        "pf_rate": ParamField("number", "PF rate, employee and employer (0.12)"),
        "pf_wage_ceiling": ParamField("number", "Ceiling on Basic + DA for PF (25000)"),
        "employee_esi_rate": ParamField("number", "Employee ESI rate on gross (0.0075)"),
        "employer_esi_rate": ParamField("number", "Employer ESI rate on gross (0.0325), cost only"),
        "professional_tax_monthly": ParamField("number", "Professional tax deducted every month (208, Gujarat)"),
        "employer_insurance_monthly": ParamField("number", "Employer-paid insurance per month (350), cost only"),
        "bonus_rate": ParamField("number", "Bonus rate on monthly gross, CTC only (0.0833)"),
    }

    def compute(self, ctx: Ctx) -> None:
        G = ctx.require(ctx.profile.daily_wage, "daily_wage")
        skill = ctx.profile.skill_level
        I = ctx.p("standard_days")
        if ctx.inputs.present_days is not None:
            K = ctx.inputs.present_days
        else:
            K = I - ctx.inputs.lop_days
        ctx.paid_days, ctx.lop_days = K, I - K

        H = G * I
        L = xl_roundup(H * K / I)
        basic_rate = skill_rate(ctx.params["basic_per_day_by_skill"], skill, "basic_per_day_by_skill")
        da_rate = skill_rate(ctx.params["da_per_day_by_skill"], skill, "da_per_day_by_skill")
        ctx.info("MONTHLY_GROSS", "Monthly gross (wage x standard days)", H, f"{G} x {I}")
        ctx.info("DAYS_PAID", "Days paid", K, f"of {I} standard days")

        M = ctx.earning("BASIC", "Basic", basic_rate * K, f"{basic_rate} x {K} days")
        N = ctx.earning("DA", "DA", da_rate * K, f"{da_rate} x {K} days")
        O = ctx.earning("SPL_ALLOWANCE", "Special allowance", L - (M + N))
        P = M + N + O

        er_pf = er_esi = 0
        if ctx.flag("pf"):
            V = xl_round(min(M + N, ctx.p("pf_wage_ceiling")) * ctx.p("pf_rate"))
            ctx.deduction("PF_EE", "Employee PF", V)
            er_pf = ctx.employer("PF_ER", "Employer PF", V)
            ctx.pf_wages = M + N
        if ctx.flag("esi"):
            ctx.deduction("ESI_EE", "Employee ESI", P * ctx.p("employee_esi_rate"))
            er_esi = ctx.employer("ESI_ER", "Employer ESI", P * ctx.p("employer_esi_rate"))
            ctx.esi_wages = P
        pt = ctx.p("professional_tax_monthly")
        if pt:
            ctx.deduction("PT", "Professional tax", pt)
        insurance = ctx.p("employer_insurance_monthly")
        if insurance:
            ctx.employer("INSURANCE_ER", "Insurance (employer)", insurance)
        bonus = xl_round(H * ctx.p("bonus_rate"))
        ctx.info("BONUS_CTC", "Bonus (CTC)", bonus)
        ctx.info("MONTHLY_CTC", "CTC", xl_round(P + er_pf + er_esi + insurance + bonus))
