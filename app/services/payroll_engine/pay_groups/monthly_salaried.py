"""MONTHLY_SALARIED — head-office monthly salary (CHENNAI_HO).

Per employee: monthly_salary (F), PF/ESI applicability, pf_cap (employee-PF
cap; falls back to params.employee_pf_cap_default).
Monthly inputs: lop_days (Z), overtime_days (AA), OTHER_PAYMENT, OTHER_ADVANCE.

  I  statutory gross = 0 if no PF and no ESI else ROUND(F * statutory_gross_pct)
  M  stipend         = F if I == 0 else 0      (no PF + no ESI -> whole salary is stipend)
  N  basic & DA      = ROUND(I * basic_da_pct)
  O  HRA             = ROUND(I * hra_pct)
  P  special         = F - N - O if M == 0 else 0
  AB working days    = standard_days - LOP
  AC..AF             = ROUND(component * AB / standard_days)
  AG OT              = ROUND((I if I != 0 else M) * OT days / standard_days)
  AJ employee PF     = ROUNDUP(MIN(AD * employee_pf_rate, pf_cap))           if PF
  AK employee ESI    = ROUNDUP((AD + AE + AG) * employee_esi_rate)           if ESI
  J  employer ESI    = ROUNDUP(I * employer_esi_rate)                        if ESI (cost only)
  K  employer PF     = ROUNDUP(MIN(I*basic_da_pct*employer_pf_rate, employer_pf_cap)
                               + I*basic_da_pct*employer_pf_admin_rate)      if PF  (cost only)
  net = AC+AD+AE+AF+AG + other payment - (AJ + AK + other advance)
"""
from app.services.payroll_engine.pay_groups.base import (
    ZERO,
    Ctx,
    ParamField,
    PayGroupStrategy,
    dec,
    xl_round,
    xl_roundup,
)


class MonthlySalariedStrategy(PayGroupStrategy):
    calc_model = "MONTHLY_SALARIED"
    description = "Monthly salaried staff: 80% statutory gross split into Basic&DA/HRA, LOP-prorated on standard days."
    PROFILE_REQUIREMENTS = (("monthly_salary",),)
    PARAM_SPEC = {
        "standard_days": ParamField("number", "Divisor for LOP/OT proration (sheet column X)"),
        "statutory_gross_pct": ParamField("number", "Share of monthly salary treated as statutory gross (0.80)"),
        "basic_da_pct": ParamField("number", "Basic & DA as a share of statutory gross (0.70)"),
        "hra_pct": ParamField("number", "HRA as a share of statutory gross (0.30)"),
        "employee_pf_rate": ParamField("number", "Employee PF rate on prorated Basic & DA (0.12)"),
        "employee_pf_cap_default": ParamField("number", "Employee PF cap when the pay profile has no pf_cap"),
        "employee_esi_rate": ParamField("number", "Employee ESI rate on Basic&DA + HRA + OT (0.0075)"),
        "employer_esi_rate": ParamField("number", "Employer ESI rate on statutory gross (0.0325), cost only"),
        "employer_pf_rate": ParamField("number", "Employer PF rate on full-month Basic & DA (0.12), cost only"),
        "employer_pf_cap": ParamField("number", "Cap on the employer PF part before admin charges (3000)"),
        "employer_pf_admin_rate": ParamField("number", "Employer PF admin/EDLI rate on full-month Basic & DA (0.01)"),
    }

    def compute(self, ctx: Ctx) -> None:
        F = ctx.require(ctx.profile.monthly_salary, "monthly_salary")
        esi = ctx.flag("esi")
        pf = ctx.flag("pf")
        X = ctx.p("standard_days")
        Z = ctx.inputs.lop_days
        AA = ctx.inputs.overtime_days
        basic_pct = ctx.p("basic_da_pct")

        I = ZERO if (not esi and not pf) else xl_round(F * ctx.p("statutory_gross_pct"))
        M = F if I == 0 else ZERO
        N = xl_round(I * basic_pct)
        O = xl_round(I * ctx.p("hra_pct"))
        P = F - N - O if M == 0 else ZERO
        AB = X - Z

        ctx.info("MONTHLY_SALARY", "Monthly salary", F)
        ctx.info("STATUTORY_GROSS", "Gross for statutory", I)
        ctx.info("WORKING_DAYS", "Working days", AB, f"{X} standard - {Z} LOP")
        ctx.paid_days, ctx.lop_days = AB, Z

        AC = xl_round(M * AB / X)
        AD = xl_round(N * AB / X)
        AE = xl_round(O * AB / X)
        AF = xl_round(P * AB / X)
        AG = xl_round((I if I != 0 else M) * AA / X)

        if AC:
            ctx.earning("STIPEND", "Stipend / consultant fees", AC)
        ctx.earning("BASIC_DA", "Basic & DA", AD)
        ctx.earning("HRA", "HRA", AE)
        ctx.earning("SPL_ALLOWANCE", "Special allowance", AF)
        if AG:
            ctx.earning("OT", "Overtime", AG, f"{AA} OT days")

        if pf:
            cap = dec(ctx.profile.pf_cap, ctx.p("employee_pf_cap_default"))
            AJ = xl_roundup(min(AD * ctx.p("employee_pf_rate"), cap))
            ctx.deduction("PF_EE", "Employee PF", AJ, f"cap {cap}")
            ctx.pf_wages = AD
        if esi:
            AK = xl_roundup((AD + AE + AG) * ctx.p("employee_esi_rate"))
            ctx.deduction("ESI_EE", "Employee ESI", AK)
            ctx.esi_wages = AD + AE + AG

        J = xl_roundup(I * ctx.p("employer_esi_rate")) if esi else ZERO
        pf_base = I * basic_pct
        K = (
            xl_roundup(min(pf_base * ctx.p("employer_pf_rate"), ctx.p("employer_pf_cap"))
                       + pf_base * ctx.p("employer_pf_admin_rate"))
            if pf else ZERO
        )
        if esi:
            ctx.employer("ESI_ER", "Employer ESI", J)
        if pf:
            ctx.employer("PF_ER", "Employer PF", K)
        ctx.info("MONTHLY_CTC", "Monthly CTC", F + J + K)
