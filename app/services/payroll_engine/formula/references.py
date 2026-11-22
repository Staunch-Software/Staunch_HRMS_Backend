"""Name clashes, formula parsing and reference checks for a normalised
definition. Lines evaluate top to bottom: a line may use any built-in
variable, input code, constant or employee field, and any line above it.
A line may reuse a built-in / input name: from that line on the name means
the line (and an input of the same code is no longer paid automatically)."""
import difflib

from app.services.payroll_engine.formula import catalog as cat
from app.services.payroll_engine.formula.definition import issue
from app.services.payroll_engine.formula.errors import FormulaError
from app.services.payroll_engine.formula.functions import FUNCTIONS
from app.services.payroll_engine.formula.parser import Call, Name, Str, parse, walk

SYSTEM_NAMES = set(cat.BUILTIN_BY_NAME) | set(cat.INPUT_CODES)


def check_names(defn: dict, errors: list) -> None:
    owners: dict[str, str] = {}
    for section in ("constants", "lookups", "employee_fields"):
        for i, item in enumerate(defn[section]):
            code, path = item.get("code"), f"{section}[{i}].code"
            if not code:
                continue
            if code in owners:
                errors.append(issue(path, f"'{code}' is already used by {owners[code]}", code))
            elif code in SYSTEM_NAMES:
                errors.append(issue(path, f"'{code}' is a built-in variable or input code", code))
            elif code in FUNCTIONS or code in cat.KEYWORDS:
                errors.append(issue(path, f"'{code}' is a reserved word", code))
            owners.setdefault(code, f"{section}[{i}]")
    seen_lines: dict[str, int] = {}
    tags: dict[str, str] = {}
    for i, line in enumerate(defn["lines"]):
        code, path = line.get("code"), f"lines[{i}]"
        if not code:
            continue
        if code in seen_lines:
            errors.append(issue(f"{path}.code", f"duplicate line code (also lines[{seen_lines[code]}])", code))
        elif code in owners:
            errors.append(issue(f"{path}.code", f"'{code}' is already used by {owners[code]}", code))
        elif code in FUNCTIONS or code in cat.KEYWORDS or code in cat.RESERVED_LINE_CODES:
            errors.append(issue(f"{path}.code", f"'{code}' is reserved", code))
        seen_lines.setdefault(code, i)
        tag = line.get("statutory_tag")
        if tag in cat.STATUTORY_TAGS:
            kinds = cat.STATUTORY_TAGS[tag][0]
            if line.get("kind") in cat.LINE_KINDS and line["kind"] not in kinds:
                errors.append(issue(f"{path}.statutory_tag", f"tag {tag} needs a {' or '.join(kinds)} line", code))
            if tag in tags:
                errors.append(issue(f"{path}.statutory_tag", f"tag {tag} is already on line {tags[tag]}", code))
            tags.setdefault(tag, code)


def parse_lines(defn: dict, errors: list) -> dict[int, dict]:
    parsed: dict[int, dict] = {}
    for i, line in enumerate(defn["lines"]):
        trees = {}
        for field in ("formula", "condition"):
            text = line.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                trees[field] = parse(text)
            except FormulaError as exc:
                errors.append(issue(f"lines[{i}].{field}", exc.message, line.get("code"), exc.position))
        parsed[i] = trees
    return parsed


def check_references(defn: dict, parsed: dict[int, dict], errors: list, warnings: list) -> None:
    lines = defn["lines"]
    line_index = {}
    for i, line in enumerate(lines):
        line_index.setdefault(line.get("code"), i)
    group_names = {item["code"] for s in ("constants", "employee_fields") for item in defn[s] if item.get("code")}
    lookups = {item["code"] for item in defn["lookups"] if item.get("code")}
    deps = {i: _names(trees) for i, trees in parsed.items()}
    line_codes = set(line_index)
    consumed = line_codes & set(cat.INPUT_CODES)

    for i, trees in parsed.items():
        code = lines[i].get("code")
        for field, tree in trees.items():
            path = f"lines[{i}].{field}"
            for node in walk(tree):
                if isinstance(node, Call):
                    _check_call(node, path, code, lookups, errors)
            for name, pos in _name_nodes(tree):
                visible = name in SYSTEM_NAMES or name in group_names or line_index.get(name, i) < i
                if visible:
                    if (name in cat.INPUT_CODES and name not in consumed and defn["include_ad_hoc_inputs"]
                            and line_index.get(name, i) >= i):
                        warnings.append(issue(path, f"input {name} is used here and is also paid/deducted "
                                                    f"automatically as its own line; name a line {name} to "
                                                    "take it over", code, pos))
                    continue
                if name in lookups:
                    message = f"'{name}' is a lookup table; use LOOKUP({name}, key)"
                elif name == code:
                    message = f"line {code} refers to itself"
                elif name in line_index:
                    cycle = _cycle(i, line_index[name], deps, line_index)
                    message = (f"circular reference: {' -> '.join(lines[k]['code'] for k in cycle)}" if cycle else
                               f"'{name}' is line {line_index[name] + 1}, below this one; a line can only use "
                               "lines above it")
                else:
                    message = f"unknown variable '{name}'" + _suggest(name, SYSTEM_NAMES | group_names | line_codes)
                errors.append(issue(path, message, code, pos))


def _check_call(node: Call, path: str, code, lookups: set, errors: list) -> None:
    spec = FUNCTIONS.get(node.name)
    if spec is None:
        errors.append(issue(path, f"unknown function {node.name}" + _suggest(node.name, set(FUNCTIONS)), code,
                            node.pos))
        return
    count = len(node.args)
    if count < spec.min_args or (spec.max_args is not None and count > spec.max_args):
        errors.append(issue(path, f"{node.name} takes {_arity(spec)} argument(s): {spec.signature}", code, node.pos))
        return
    if node.name == "ISBLANK" and not isinstance(node.args[0], Name):
        errors.append(issue(path, "ISBLANK needs a variable name, e.g. ISBLANK(PF_CAP)", code, node.pos))
    if node.name == "LOOKUP":
        table = node.args[0]
        name = table.name if isinstance(table, Name) else table.value.upper() if isinstance(table, Str) else None
        if name is None:
            errors.append(issue(path, "LOOKUP's first argument must be a lookup table name", code, node.pos))
        elif name not in lookups:
            errors.append(issue(path, f"unknown lookup table '{name}'" + _suggest(name, lookups), code, table.pos))


def _arity(spec) -> str:
    if spec.max_args is None:
        return f"at least {spec.min_args}"
    return str(spec.min_args) if spec.min_args == spec.max_args else f"{spec.min_args} to {spec.max_args}"


def _name_nodes(tree):
    """Variable references, skipping LOOKUP's table argument."""
    skip = set()
    for node in walk(tree):
        if isinstance(node, Call) and node.name == "LOOKUP" and node.args and isinstance(node.args[0], Name):
            skip.add(id(node.args[0]))
    return [(n.name, n.pos) for n in walk(tree) if isinstance(n, Name) and id(n) not in skip]


def _names(trees: dict) -> set[str]:
    return {name for tree in trees.values() for name, _ in _name_nodes(tree)}


def _cycle(start: int, target: int, deps: dict, line_index: dict) -> list[int] | None:
    """Path start -> target -> ... -> start through line references, if any."""
    stack, seen = [(target, [start, target])], set()
    while stack:
        node, path = stack.pop()
        for name in deps.get(node, ()):
            nxt = line_index.get(name)
            if nxt is None:
                continue
            if nxt == start:
                return path + [start]
            if nxt not in seen and nxt > node:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def _suggest(name: str, candidates: set) -> str:
    close = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.75)
    return f" (did you mean {close[0]}?)" if close else ""
