"""DAILY_WAGE_ATTENDANCE — day wage x days present with a per-day allowance,
bonus, OT and a threshold PF (MCBS, CCTV, ESFAS share this model; their
differences are all params).

Per employee: daily_wage (D), skill_level, optional fixed_monthly_amount
(engineers on a fixed monthly figure instead of wage x days).
Monthly inputs: present_days (H/F; defaults to standard_days - lop_days),
overtime_hours (L/J), SPECIAL_ALLOWANCE / PAID_LEAVE (inside the total),
LOAN, EXCESS_WAGE_DEDUCTION, OTHER_ADVANCE.

  amount     = fixed_monthly_amount if set else D * days
  allowance  = allowance_per_day_by_skill[skill] * days        (0 on a fixed amount)
  OT rate    = D / ot_hours_per_day * ot_multiplier;  OT = rate * OT hours
  bonus      = MIN(amount * bonus_rate, bonus_wage_ceiling * bonus_rate)   (0 on a fixed amount)
  total      = amount + allowance + OT + bonus + special/paid leave
  PF         = pf_flat_base * pf_rate if PF wage > pf_threshold else PF wage * pf_rate
               (PF wage = D * days, or the fixed amount when no day wage is set)
  transport  = transport_per_day_by_skill[skill] * days, paid outside the
               total and outside PF                          (0 on a fixed amount)
  net = ROUNDUP(total + transport - PF - loan - excess wage - advance)

Intended behaviour over the sheets' literal formulas: the CCTV and ESFAS net
formulas leave the Loan column out (it is 0 every month so far); loans are
deducted here.
"""
from app.services.payroll_engine.pay_groups.base import (
    ZERO,
    Ctx,
    ParamField,
    PayGroupStrategy,
    dec,
    skill_rate,
    xl_round,
)


class DailyWageAttendanceStrategy(PayGroupStrategy):
    calc_model = "DAILY_WAGE_ATTENDANCE"
    description = "Day wage x days present + skill allowance, OT, capped bonus, threshold PF, transport outside PF."
    PROFILE_REQUIREMENTS = (("daily_wage", "fixed_monthly_amount"),)
    USES_SKILL_TABLES = True
    PARAM_SPEC = {
        "standard_days": ParamField("number", "Days paid when no attendance input exists; also the CTC month (26)"),
        "allowance_per_day_by_skill": ParamField(
            "skill_table", "Per-day allowance by skill level ('25% of wage 2016'); DEFAULT key applies to any skill"
        ),
        "transport_per_day_by_skill": ParamField(
            "skill_table", "Per-day transport by skill level, paid outside total and PF; DEFAULT key applies to any skill"
        ),
        "ot_hours_per_day": ParamField("number", "Divisor turning the day wage into an hourly wage (9, or 8 for ESFAS)"),
        "ot_multiplier": ParamField("number", "Overtime multiplier on the hourly wage (2)"),
        "bonus_rate": ParamField("number", "Statutory bonus rate (0.0833)"),
        "bonus_wage_ceiling": ParamField("number", "Bonus wage ceiling (21000)"),
        "pf_rate": ParamField("number", "PF rate (0.12)"),
        "pf_threshold": ParamField("number", "PF wage above which the flat PF applies (15000 MCBS, 25000 CCTV/ESFAS)"),
        "pf_flat_base": ParamField("number", "Notional PF wage used above the threshold (25000)"),
        "ctc_fixed_addon": ParamField("number", "Fixed monthly amount added to CTC only (3600 for ESFAS, else 0)"),
    }

    def compute(self, ctx: Ctx) -> None:
        params = ctx.params
        fixed = ctx.profile.fixed_monthly_amount
        fixed = dec(fixed) if fixed not in (None, "") else None
        D = dec(ctx.profile.daily_wage) if ctx.profile.daily_wage not in (None, "") else None
        if fixed is None and D is None:
            ctx.require(None, "daily_wage or fixed_monthly_amount")
        skill = ctx.profile.skill_level

        days = ctx.inputs.present_days
        if days is None:
            days = ctx.p("standard_days") - ctx.inputs.lop_days
        ctx.paid_days, ctx.lop_days = days, ctx.inputs.lop_days
        ctx.info("DAYS_PRESENT", "Days present", days)

        if fixed is not None:
            amount = ctx.earning("FIXED_AMOUNT", "Fixed monthly amount", fixed)
            allowance = bonus = transport = ZERO
        else:
            amount = ctx.earning("WAGES", "Wages", D * days, f"{D} x {days} days")
            allowance_rate = skill_rate(params["allowance_per_day_by_skill"], skill, "allowance_per_day_by_skill")
            allowance = ctx.earning("ALLOWANCE", "Allowance (25% of wage 2016)", allowance_rate * days,
                                    f"{allowance_rate} x {days} days")
            rate = ctx.p("bonus_rate")
            bonus = ctx.earning("BONUS", "Bonus (8.33% of wage)", min(amount * rate, ctx.p("bonus_wage_ceiling") * rate))
            transport_rate = skill_rate(params["transport_per_day_by_skill"], skill, "transport_per_day_by_skill")
            transport = transport_rate * days
            if transport:
                ctx.earning("TRANSPORT", "Transport", transport, f"{transport_rate} x {days} days")

        hours = ctx.inputs.overtime_hours
        if D is not None and hours:
            ot_rate = D / ctx.p("ot_hours_per_day") * ctx.p("ot_multiplier")
            ctx.earning("OT", "Overtime", ot_rate * hours, f"{hours} h x {xl_round(ot_rate, 2)}")

        if ctx.flag("pf"):
            pf_wage = D * days if D is not None else fixed
            if pf_wage > ctx.p("pf_threshold"):
                pf = ctx.p("pf_flat_base") * ctx.p("pf_rate")
            else:
                pf = pf_wage * ctx.p("pf_rate")
            ctx.deduction("PF_EE", "PF", pf)
            ctx.pf_wages = pf_wage

        ctc_base = fixed if fixed is not None else D * days
        ctx.info("MONTHLY_CTC", "CTC", ctc_base + allowance + bonus + ctx.p("ctc_fixed_addon"))
