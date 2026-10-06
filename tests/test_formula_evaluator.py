import pytest

from app.services.formula.dependency_graph import (
    build_dependency_graph,
    extract_referenced_codes,
    topological_sort,
)
from app.services.formula.evaluator import FormulaEvaluationError, evaluate_formula


def test_evaluate_simple_formula():
    assert evaluate_formula("0.4 * BASIC", {"BASIC": 60000}) == 24000.0


def test_evaluate_formula_with_allowed_functions():
    assert evaluate_formula("min(BASIC, 15000)", {"BASIC": 60000}) == 15000.0
    assert evaluate_formula("max(BASIC, 15000)", {"BASIC": 5000}) == 15000.0
    assert evaluate_formula("round(BASIC / 3, 2)", {"BASIC": 100}) == pytest.approx(33.33)


def test_unknown_name_raises_clear_error():
    with pytest.raises(FormulaEvaluationError):
        evaluate_formula("0.4 * NOT_A_COMPONENT", {"BASIC": 60000})


def test_disallowed_function_rejected():
    with pytest.raises(FormulaEvaluationError):
        evaluate_formula("__import__('os').system('echo hi')", {})


def test_attribute_access_rejected():
    with pytest.raises(FormulaEvaluationError):
        evaluate_formula("BASIC.__class__", {"BASIC": 60000})


def test_empty_formula_rejected():
    with pytest.raises(FormulaEvaluationError):
        evaluate_formula("", {"BASIC": 60000})


def test_extract_referenced_codes_ignores_pseudo_vars_and_functions():
    known_codes = {"BASIC", "HRA", "PF_EE"}
    refs = extract_referenced_codes("min(0.12 * PF_WAGES, round(BASIC))", known_codes)
    assert refs == {"BASIC"}


def test_build_dependency_graph_and_topological_sort():
    components = [
        {"code": "BASIC", "calculation_method": "Fixed Amount", "value": None},
        {"code": "HRA", "calculation_method": "Percentage/Formula", "value": "0.4 * BASIC"},
        {"code": "PF_EE", "calculation_method": "Percentage/Formula", "value": "0.12 * PF_WAGES"},
    ]
    graph = build_dependency_graph(components)
    assert graph["HRA"] == {"BASIC"}
    assert graph["PF_EE"] == set()  # PF_WAGES is a pseudo-var, not a component dependency

    order = topological_sort(graph)
    assert order.index("BASIC") < order.index("HRA")


def test_circular_dependency_raises_value_error():
    components = [
        {"code": "A", "calculation_method": "Percentage/Formula", "value": "0.5 * B"},
        {"code": "B", "calculation_method": "Percentage/Formula", "value": "0.5 * A"},
    ]
    graph = build_dependency_graph(components)
    with pytest.raises(ValueError, match="circular formula dependency"):
        topological_sort(graph)
