"""Safe formula evaluation for salary component `value` strings like
"0.4 * BASIC" or "0.12 * PF_WAGES". No `eval()` — uses `simpleeval`'s
restricted-AST evaluator with an explicit namespace and a tiny allowed
function set (min, max, round only). No attribute access, no name lookups
outside the namespace that is explicitly built per employee.
"""
from simpleeval import EvalWithCompoundTypes, InvalidExpression, NameNotDefined

# Only these three builtins are exposed to formulas — nothing that could
# reach the filesystem, network, or other Python internals.
ALLOWED_FUNCTIONS = {
    "min": min,
    "max": max,
    "round": round,
}


class FormulaEvaluationError(ValueError):
    pass


def evaluate_formula(expression: str, namespace: dict) -> float:
    """Evaluate `expression` against `namespace` (a flat dict of
    component_code -> numeric value, plus PF_WAGES/ESI_WAGES/GROSS_EARNINGS
    pseudo-variables). Returns a float. Raises FormulaEvaluationError on any
    problem (unknown name, syntax error, disallowed construct).
    """
    if not expression or not expression.strip():
        raise FormulaEvaluationError("Formula is empty.")

    evaluator = EvalWithCompoundTypes(names=dict(namespace), functions=dict(ALLOWED_FUNCTIONS))
    # Belt-and-braces: simpleeval already disallows attribute/subscript access
    # by default unless explicitly enabled, but clear these out defensively in
    # case a future simpleeval version changes its defaults.
    evaluator.ATTR_INDEX_FALLBACK = False

    try:
        result = evaluator.eval(expression)
    except NameNotDefined as exc:
        raise FormulaEvaluationError(f"Unknown component code referenced in formula: {exc}") from exc
    except InvalidExpression as exc:
        raise FormulaEvaluationError(f"Invalid formula '{expression}': {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - simpleeval raises many exception types
        raise FormulaEvaluationError(f"Could not evaluate formula '{expression}': {exc}") from exc

    try:
        return float(result)
    except (TypeError, ValueError) as exc:
        raise FormulaEvaluationError(f"Formula '{expression}' did not evaluate to a number.") from exc
