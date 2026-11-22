"""Golden verification: re-computes every employee row of the monthly payroll
workbook with the pay groups (seeded params) and compares gross, PF and net
pay with Excel's cached computed values.

    .venv/Scripts/python.exe scripts/verify_pay_groups_against_excel.py <workbook.xlsx>
        [--engine formula|strategy] [--compare-engines] [--show-amounts] [--with-tds]

--engine formula (default) runs each group's FORMULA definition, converted
from the seeded params exactly as the migration/seed do; --engine strategy
runs the legacy fixed strategy. --compare-engines runs both on every row and
reports any line item (code, label, kind, amount, order) or total that
differs; the exit code is 1 when anything differs.

The workbook carries no income tax, so TDS is off by default. --with-tds adds
the TDS line a run would (group's tds_enabled param, New regime, seeded FY
2026-27 rules, steady salary: annual tax on gross x 12, spread over 12
months) to show which rows TDS would change.

Each row is classified:
  MATCH                  net within Rs 1 of Excel
  MISMATCH-EXPLAINED     the row has a hand-typed constant / blank / altered
                         formula (vs the column's dominant formula pattern) in
                         a cell that feeds Net Pay; the overridden columns are named
  MISMATCH-UNEXPLAINED   anything else — the engine is wrong

The workbook holds real payroll data: it is read locally only, nothing is
written, and output carries row numbers, column letters/headers and status
only (amounts only with --show-amounts, for local debugging).
Exit code: 0 when there are no unexplained mismatches, 1 otherwise.
"""
from __future__ import annotations

import argparse
import copy
import sys
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.seed.pay_groups_seed import SEED_CALC_MODEL_BY_CODE, SEED_PARAMS_BY_CODE  # noqa: E402
from app.db.seed.statutory_seed import TDS_SEED  # noqa: E402
from app.services.payroll_engine.formula.history import converted_definition  # noqa: E402
from app.services.payroll_engine.pay_groups import get_strategy  # noqa: E402
from app.services.payroll_engine.statutory.tds import TDS_CODE, compute_annual_tax  # noqa: E402
from app.services.payroll_import.excel_extract import SHEETS, extract_sheet, load_workbooks  # noqa: E402

TOLERANCE = Decimal("1")
TDS_FY = "2026-27"


def add_steady_state_tds(result, params) -> None:
    config, slabs = TDS_SEED[TDS_FY]["New"]
    annual_gross = result.gross_before_round_off() * 12
    taxable = max(Decimal("0"), annual_gross - Decimal(str(config["standard_deduction"])))
    tax = compute_annual_tax(taxable, {"regime": "New", **config}, slabs)["total"]
    monthly = (tax / 12).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if monthly > 0:
        result.add_deduction(TDS_CODE, "Income tax (TDS)", monthly, "steady-state estimate", params["net_rounding"])


def _snapshot(strategy, config, case, with_tds):
    try:
        result = strategy.calculate(case.profile, None, copy.deepcopy(case.inputs), config)
    except Exception as exc:  # noqa: BLE001 - compared, not raised
        return {"error": type(exc).__name__}
    if with_tds and config.get("tds_enabled", True):
        add_steady_state_tds(result, config)
    snap = {f"line {i + 1}": (ln.code, ln.label, ln.kind, ln.amount) for i, ln in enumerate(result.lines)}
    snap.update({k: getattr(result, k) for k in ("gross", "total_deductions", "net_pay", "employer_cost",
                                                 "paid_days", "lop_days", "pf_wages", "esi_wages")})
    snap["warnings"] = tuple(result.warnings)
    return snap


def engine_difference(engines, case, with_tds) -> str:
    """'' when both engines agree on every line and total; else which keys differ."""
    a = _snapshot(*engines["strategy"], case, with_tds)
    b = _snapshot(*engines["formula"], case, with_tds)
    return ", ".join(k for k in dict.fromkeys([*a, *b]) if a.get(k) != b.get(k))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbook")
    parser.add_argument("--show-amounts", action="store_true", help="print Excel vs engine figures (local debugging only)")
    parser.add_argument("--with-tds", action="store_true", help="add a steady-state TDS line (the workbook has none)")
    parser.add_argument("--engine", choices=("formula", "strategy"), default="formula")
    parser.add_argument("--compare-engines", action="store_true", help="report any per-line difference between engines")
    args = parser.parse_args()

    wb_f, wb_v = load_workbooks(args.workbook)

    total = Counter()
    summary = []
    compared = differences = 0
    for spec in SHEETS:
        name, code = spec.name, spec.pay_group_code
        legacy = get_strategy(SEED_CALC_MODEL_BY_CODE[code])
        params = SEED_PARAMS_BY_CODE[code]
        definition = converted_definition(legacy.calc_model, params, code)
        engines = {"strategy": (legacy, params), "formula": (get_strategy("FORMULA"), definition)}
        strategy, config = engines[args.engine]
        extracted = extract_sheet(wb_f, wb_v, spec, params)
        counts = Counter()
        side_checks = Counter()
        reasons: list[str] = []
        print(f"\n=== {name} -> {code} ({args.engine} engine: {strategy.calc_model}), {len(extracted.rows)} rows")

        for row in extracted.rows:
            r, case = row.row_number, row.case
            if case is None:
                counts["MISMATCH-UNEXPLAINED"] += 1
                print(f"  row {r}: MISMATCH-UNEXPLAINED {row.error}")
                continue
            if args.compare_engines:
                compared += 1
                diff = engine_difference(engines, case, args.with_tds)
                if diff:
                    differences += 1
                    print(f"  row {r}: ENGINE-DIFFERENCE {diff}")
            try:
                result = strategy.calculate(case.profile, None, copy.deepcopy(case.inputs), config)
                if args.with_tds and config.get("tds_enabled", True):
                    add_steady_state_tds(result, config)
            except Exception as exc:  # noqa: BLE001 - report, keep going
                counts["MISMATCH-UNEXPLAINED"] += 1
                print(f"  row {r}: MISMATCH-UNEXPLAINED engine error: {exc}")
                continue

            gross = result.gross - result.line_amount("ROUND_OFF")
            pf = result.line_amount("PF_EE")
            net_ok = abs(result.net_pay - case.excel_net) <= TOLERANCE
            gross_ok = abs(gross - case.excel_gross) <= TOLERANCE
            pf_ok = abs(pf - case.excel_pf) <= TOLERANCE

            overridden = row.overrides
            override_text = ", ".join(f"{col} ({header}) {desc}" for col, header, desc in overridden)

            if net_ok:
                status = "MATCH"
            elif overridden:
                status = "MISMATCH-EXPLAINED"
            else:
                status = "MISMATCH-UNEXPLAINED"
            counts[status] += 1
            if net_ok and not gross_ok:
                side_checks["gross differs (net matches)"] += 1
            if net_ok and not pf_ok:
                side_checks["PF differs (net matches)"] += 1

            detail = []
            if status != "MATCH":
                detail.append(f"overrides: {override_text or 'none'}")
                if status == "MISMATCH-EXPLAINED":
                    reasons.append(f"row {r}: {override_text}")
            if not gross_ok:
                detail.append("gross differs")
            if not pf_ok:
                detail.append("PF differs")
            detail.extend(case.notes)
            if args.show_amounts:
                detail.append(
                    f"net excel={case.excel_net} engine={result.net_pay}; gross excel={case.excel_gross} "
                    f"engine={gross}; pf excel={case.excel_pf} engine={pf}"
                )
            if detail or status != "MATCH":
                print(f"  row {r}: {status}" + (" | " + " | ".join(detail) if detail else ""))

        total.update(counts)
        line = (f"{name}: {counts['MATCH']} match, {counts['MISMATCH-EXPLAINED']} explained, "
                f"{counts['MISMATCH-UNEXPLAINED']} unexplained")
        if side_checks:
            line += " (" + ", ".join(f"{n} {k}" for k, n in side_checks.items()) + ")"
        summary.append((line, reasons))

    print("\n=== Summary")
    for line, reasons in summary:
        print(line)
        for reason in reasons:
            print(f"    {reason}")
    print(f"TOTAL: {total['MATCH']} match, {total['MISMATCH-EXPLAINED']} explained, "
          f"{total['MISMATCH-UNEXPLAINED']} unexplained")
    if args.compare_engines:
        print(f"ENGINES: {compared} rows compared, {differences} with differences")
    return 1 if total["MISMATCH-UNEXPLAINED"] or differences else 0


if __name__ == "__main__":
    sys.exit(main())
