"""DAILY_WAGE_STANDARD_DAYS — day wage converted to a monthly structure on
standard days, then prorated on days present (KKNPP_HARI).

Per employee: daily_wage (G), skill_level, PF applicability.
Monthly inputs: present_days (Y; defaults to standard_days - lop_days),
overtime_hours (V), WAGE_ARREAR (AF).

  H  monthly gross   = G * W                     (W = standard_days)
  K  basic           = H * basic_pct
  L  DA              = H * da_pct
  M  HRA             = K * hra_pct_of_basic
  O  special         = special_allowance_per_day * W
  P  transport       = transport_per_day * W      (CTC only)
  Z  prorated gross  = ROUNDUP(H * Y / W)          (bonus base only)
  AA..AD             = ROUND(K|L|M|O * Y / W)
  U  OT rate         = G * ot_multiplier / ot_hours_per_day
  AE OT              = U * V                       (not rounded)
  AG bonus           = MIN(Z * bonus_rate, bonus_wage_ceiling * bonus_rate)  (not rounded)
  AH total gross     = AA + AB + AC + AD
  AI PF              = ROUNDUP(MIN(AH * employee_pf_rate, employee_pf_cap))  if PF
  net = AH - AI + AE + AG + AF                     (net_rounding = NONE)

CTC-only items: the sheet's Employer PF and Bonus columns are written as
IF(H<="No", 15000*12%, H*12%) / IF(F<="NO", 21000*8.33%, ...). Excel compares a
number/text with text, which always evaluates TRUE, so they always produce
employer_pf_wage_base * employer_pf_rate and ctc_bonus_wage_base * bonus_rate.
That effective behaviour is what is implemented here; neither feeds net pay.
"""
from app.services.payroll_engine.pay_groups.base import (
    Ctx,
    ParamField,
    PayGroupStrategy,
    xl_round,
    xl_roundup,
)


class DailyWageStandardDaysStrategy(PayGroupStrategy):
    calc_model = "DAILY_WAGE_STANDARD_DAYS"
    description = "Day wage x standard days split into Basic/DA/HRA/special, prorated on days present."
    PROFILE_REQUIREMENTS = (("daily_wage",),)
    PARAM_SPEC = {
        "standard_days": ParamField("number", "Standard days per month (W, 26)"),
        "basic_pct": ParamField("number", "Basic as a share of monthly gross (0.50)"),
        "da_pct": ParamField("number", "DA as a share of monthly gross (0.25)"),
        "hra_pct_of_basic": ParamField("number", "HRA as a share of basic (0.50)"),
        "special_allowance_per_day": ParamField("number", "Special allowance per standard day (149.73)"),
        "transport_per_day": ParamField("number", "Transport per standard day, CTC only (35.93)"),
        "ot_multiplier": ParamField("number", "Overtime multiplier on the hourly wage (2)"),
        "ot_hours_per_day": ParamField("number", "Hours in a working day for the OT hourly rate (9)"),
        "bonus_rate": ParamField("number", "Statutory bonus rate (0.0833)"),
        "bonus_wage_ceiling": ParamField("number", "Bonus wage ceiling (21000)"),
        "employee_pf_rate": ParamField("number", "Employee PF rate on total gross (0.12)"),
        "employee_pf_cap": ParamField("number", "Employee PF cap (1800)"),
        "employer_pf_wage_base": ParamField("number", "Employer PF wage used for CTC (15000)"),
        "employer_pf_rate": ParamField("number", "Employer PF rate for CTC (0.12)"),
        "ctc_bonus_wage_base": ParamField("number", "Bonus wage used for CTC (21000)"),
    }

    def compute(self, ctx: Ctx) -> None:
        G = ctx.require(ctx.profile.daily_wage, "daily_wage")
        W = ctx.p("standard_days")
        Y = ctx.inputs.present_days
        if Y is None:
            Y = W - ctx.inputs.lop_days
        V = ctx.inputs.overtime_hours
        ctx.paid_days, ctx.lop_days = Y, W - Y

        H = G * W
        K = H * ctx.p("basic_pct")
        L = H * ctx.p("da_pct")
        M = K * ctx.p("hra_pct_of_basic")
        O = ctx.p("special_allowance_per_day") * W
        P = ctx.p("transport_per_day") * W
        ctx.info("MONTHLY_GROSS", "Monthly gross (wage x standard days)", H, f"{G} x {W}")
        ctx.info("DAYS_PRESENT", "Days present", Y, f"of {W} standard days")

        Z = xl_roundup(H * Y / W)
        AA = ctx.earning("BASIC", "Basic", xl_round(K * Y / W))
        AB = ctx.earning("DA", "DA", xl_round(L * Y / W))
        AC = ctx.earning("HRA", "HRA", xl_round(M * Y / W))
        AD = ctx.earning("SPL_ALLOWANCE", "Special allowance", xl_round(O * Y / W))
        AH = AA + AB + AC + AD

        U = G * ctx.p("ot_multiplier") / ctx.p("ot_hours_per_day")
        if V:
            ctx.earning("OT", "Overtime", U * V, f"{V} h x {xl_round(U, 2)}")
        rate = ctx.p("bonus_rate")
        ctx.earning("BONUS", "Bonus (prorated)", min(Z * rate, ctx.p("bonus_wage_ceiling") * rate))

        if ctx.flag("pf"):
            ctx.deduction("PF_EE", "Employee PF", xl_roundup(min(AH * ctx.p("employee_pf_rate"), ctx.p("employee_pf_cap"))))
            ctx.pf_wages = AH

        er_pf = ctx.p("employer_pf_wage_base") * ctx.p("employer_pf_rate")
        ctc_bonus = ctx.p("ctc_bonus_wage_base") * rate
        ctx.employer("PF_ER", "Employer PF", er_pf)
        ctx.info("TRANSPORT_CTC", "Transport (CTC)", P)
        ctx.info("BONUS_CTC", "Bonus (CTC)", ctc_bonus)
        ctx.info("MONTHLY_CTC", "Monthly CTC", xl_round(K + L + M + O + P + er_pf + ctc_bonus))
