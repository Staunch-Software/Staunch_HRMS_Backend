"""DAILY_FLAT_RATE — flat per-day salary with a flat PF (TUTICORIN).

Per employee: base per-day rate = daily_wage (or extra.base_day_rate), plus
the group's per_day_allowance -> per-day salary (D). Optional standing
supervisor allowance in extra.supervisor_allowance; a SUPERVISOR_ALLOWANCE
monthly input replaces it for that month.
Monthly inputs: present_days (E; defaults to standard_days).

  F gross = ROUND(E * D)
  G PF    = ROUND(pf_flat_base * pf_rate)  if PF and F > 0
  H ESI   = ROUND(F * employee_esi_rate)   if ESI   (the sheet's rate is 0)
  J supervisor allowance (outside PF/ESI)
  net = ROUND(F - G - H + J)                (net_rounding = ROUND)

Intended behaviour over the sheet's literal formula: the flat PF is skipped
when nothing is earned (0 days present) — the sheet hand-types PF = 0 there.
"""
from app.services.payroll_engine.pay_groups.base import (
    ZERO,
    Ctx,
    ParamField,
    PayGroupStrategy,
    dec,
    xl_round,
)


class DailyFlatRateStrategy(PayGroupStrategy):
    calc_model = "DAILY_FLAT_RATE"
    description = "Per-day salary (employee base rate + group allowance) x days present, flat PF."
    PROFILE_REQUIREMENTS = (("daily_wage", "extra.base_day_rate"),)
    PARAM_SPEC = {
        "standard_days": ParamField("number", "Days paid when no attendance input exists"),
        "per_day_allowance": ParamField("number", "Group allowance added to each employee's base day rate (492.3)"),
        "pf_flat_base": ParamField("number", "Notional PF wage for the flat PF (25000)"),
        "pf_rate": ParamField("number", "PF rate applied to pf_flat_base (0.12)"),
        "employee_esi_rate": ParamField("number", "Employee ESI rate on gross (0 for this site)"),
    }

    def compute(self, ctx: Ctx) -> None:
        base = ctx.profile.daily_wage
        if base is None:
            base = ctx.extra("base_day_rate")
        base = ctx.require(base, "daily_wage (base day rate)")
        D = base + ctx.p("per_day_allowance")
        E = ctx.inputs.present_days
        if E is None:
            E = ctx.p("standard_days") - ctx.inputs.lop_days
        ctx.paid_days, ctx.lop_days = E, ctx.inputs.lop_days
        ctx.info("PER_DAY_SALARY", "Per-day salary", D, f"{base} + {ctx.p('per_day_allowance')}")
        ctx.info("DAYS_PRESENT", "Days present", E)

        F = ctx.earning("GROSS_SALARY", "Gross salary", xl_round(E * D), f"{E} days x {D}")
        if ctx.flag("pf") and F > 0:
            ctx.deduction("PF_EE", "PF", xl_round(ctx.p("pf_flat_base") * ctx.p("pf_rate")), "flat")
            ctx.pf_wages = ctx.p("pf_flat_base")
        if ctx.flag("esi"):
            H = xl_round(F * ctx.p("employee_esi_rate"))
            if H:
                ctx.deduction("ESI_EE", "ESI", H)
            ctx.esi_wages = F

        ctx.consumed_earnings.add("SUPERVISOR_ALLOWANCE")
        J = ctx.inputs.earnings.get("SUPERVISOR_ALLOWANCE")
        if J is None:
            J = dec(ctx.extra("supervisor_allowance"), ZERO)
        if J:
            ctx.earning("SUPERVISOR_ALLOWANCE", "Supervisor allowance", J)
