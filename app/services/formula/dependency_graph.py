"""Builds a dependency graph between formula-type salary components (e.g.
HRA = "0.4 * BASIC" depends on BASIC) and topologically sorts them so the
engine evaluates each component only after every component its formula
references. Raises ValueError on a circular dependency.
"""
import re

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Pseudo-variables that are always available in the evaluation namespace and
# never point at another salary component, so they never participate in the
# dependency graph as a "depends on this component" edge.
PSEUDO_VARIABLES = {"PF_WAGES", "ESI_WAGES", "GROSS_EARNINGS"}


def extract_referenced_codes(formula: str, known_codes: set[str]) -> set[str]:
    """Tokenizes `formula` and returns the subset of identifiers that are
    known salary-component codes (ignores numbers, operators, function names
    like min/max/round, and the PF_WAGES/ESI_WAGES/GROSS_EARNINGS pseudo-vars).
    """
    if not formula:
        return set()
    tokens = set(_IDENTIFIER_RE.findall(formula))
    return {t for t in tokens if t in known_codes and t not in PSEUDO_VARIABLES}


def build_dependency_graph(components: list[dict]) -> dict[str, set[str]]:
    """`components` is a list of dicts with at least `code`, `calculation_method`,
    `value`. Returns {code: set(codes it depends on)} for every component,
    formula or not (non-formula components simply have an empty dependency set).
    """
    known_codes = {c["code"] for c in components}
    graph: dict[str, set[str]] = {}
    for c in components:
        code = c["code"]
        if c.get("calculation_method") == "Percentage/Formula" and c.get("value"):
            graph[code] = extract_referenced_codes(c["value"], known_codes) - {code}
        else:
            graph[code] = set()
    return graph


def topological_sort(graph: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. Raises ValueError naming the cycle's members if the
    graph is not a DAG.
    """
    in_degree = {node: 0 for node in graph}
    dependents: dict[str, list[str]] = {node: [] for node in graph}

    for node, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                # Referenced code isn't a known component (e.g. a typo or a
                # code that was deleted) — treat as having no further deps so
                # the evaluator surfaces a clearer "unknown name" error later.
                continue
            in_degree[node] += 1
            dependents[dep].append(node)

    queue = sorted([node for node, deg in in_degree.items() if deg == 0])
    ordered: list[str] = []

    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
        queue.sort()

    if len(ordered) != len(graph):
        remaining = sorted(set(graph) - set(ordered))
        raise ValueError(
            f"circular formula dependency involving {', '.join(remaining)}"
        )

    return ordered
