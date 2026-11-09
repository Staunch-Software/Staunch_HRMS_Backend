"""Golden verification: re-computes every employee row of the monthly payroll
workbook with the pay-group strategies (using the seeded params) and
compares gross, PF and net pay with Excel's cached computed values.

    .venv/Scripts/python.exe scripts/verify_pay_groups_against_excel.py <workbook.xlsx> [--show-amounts]

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
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.seed.pay_groups_seed import SEED_CALC_MODEL_BY_CODE, SEED_PARAMS_BY_CODE  # noqa: E402
from app.services.payroll_engine.pay_groups import get_strategy  # noqa: E402
from app.services.payroll_import.excel_extract import SHEETS, extract_sheet, load_workbooks  # noqa: E402

TOLERANCE = Decimal("1")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbook")
    parser.add_argument("--show-amounts", action="store_true", help="print Excel vs engine figures (local debugging only)")
    args = parser.parse_args()

    wb_f, wb_v = load_workbooks(args.workbook)

    total = Counter()
    summary = []
    for spec in SHEETS:
        name, code = spec.name, spec.pay_group_code
        strategy = get_strategy(SEED_CALC_MODEL_BY_CODE[code])
        params = SEED_PARAMS_BY_CODE[code]
        extracted = extract_sheet(wb_f, wb_v, spec, params)
        counts = Counter()
        side_checks = Counter()
        reasons: list[str] = []
        print(f"\n=== {name} -> {code} ({strategy.calc_model}), {len(extracted.rows)} rows")

        for row in extracted.rows:
            r, case = row.row_number, row.case
            if case is None:
                counts["MISMATCH-UNEXPLAINED"] += 1
                print(f"  row {r}: MISMATCH-UNEXPLAINED {row.error}")
                continue
            try:
                result = strategy.calculate(case.profile, None, case.inputs, params)
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
    return 1 if total["MISMATCH-UNEXPLAINED"] else 0


if __name__ == "__main__":
    sys.exit(main())
