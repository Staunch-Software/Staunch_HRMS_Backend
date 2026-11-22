"""Decimal evaluator over the parsed AST. No eval(): only the node types in
parser.py exist. Values are Decimal or text; comparisons/AND/OR give 1 or 0.

`env` supplies names: env.value(name, pos), env.is_blank(name, pos) and
env.lookup(table, key, pos) -> Decimal | None (None = no row, no default).
"""
from decimal import Decimal, InvalidOperation

from app.services.payroll_engine.formula import functions as fn
from app.services.payroll_engine.formula.errors import FormulaRuntimeError
from app.services.payroll_engine.formula.parser import Binary, Call, Name, Num, Str, Unary

ONE, ZERO = Decimal(1), Decimal(0)


def _bool(value: bool) -> Decimal:
    return ONE if value else ZERO


def _text(value) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


class Evaluator:
    def __init__(self, env):
        self.env = env

    def number(self, node, value=None) -> Decimal:
        value = self.eval(node) if value is None else value
        if isinstance(value, str):
            raise FormulaRuntimeError(f"expected a number but got text {_text(value)}", node.pos)
        return value

    def truth(self, node) -> bool:
        value = self.eval(node)
        if isinstance(value, str):
            raise FormulaRuntimeError(f"text {_text(value)} cannot be used as a condition", node.pos)
        return value != 0

    def eval(self, node):
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return node.value
        if isinstance(node, Name):
            return self.env.value(node.name, node.pos)
        if isinstance(node, Unary):
            return self._unary(node)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Call):
            return self._call(node)
        raise FormulaRuntimeError(f"unsupported expression {type(node).__name__}")  # pragma: no cover

    def _unary(self, node: Unary):
        if node.op == "NOT":
            return _bool(not self.truth(node.operand))
        value = self.number(node.operand)
        if node.op == "-":
            return -value
        if node.op == "%":
            return value / 100
        return value

    def _binary(self, node: Binary):
        op = node.op
        if op == "AND":
            return _bool(self.truth(node.left) and self.truth(node.right))
        if op == "OR":
            return _bool(self.truth(node.left) or self.truth(node.right))
        left, right = self.eval(node.left), self.eval(node.right)
        if op in ("=", "<>", "<", "<=", ">", ">="):
            return self._compare(op, left, right, node)
        a, b = self.number(node.left, left), self.number(node.right, right)
        try:
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if b == 0:
                raise FormulaRuntimeError("division by zero", node.pos)
            return a / b
        except InvalidOperation:
            raise FormulaRuntimeError("calculation overflow", node.pos) from None

    @staticmethod
    def _compare(op, left, right, node):
        if isinstance(left, str) != isinstance(right, str):
            if op in ("=", "<>"):
                return _bool(op == "<>")
            raise FormulaRuntimeError(f"cannot compare {_text(left)} with {_text(right)}", node.pos)
        if isinstance(left, str):
            left, right = left.casefold(), right.casefold()
        return _bool({
            "=": left == right, "<>": left != right, "<": left < right,
            "<=": left <= right, ">": left > right, ">=": left >= right,
        }[op])

    def _call(self, node: Call):
        name, args = node.name, node.args
        spec = fn.FUNCTIONS.get(name)
        if spec is None:
            raise FormulaRuntimeError(f"unknown function {name}", node.pos)
        if len(args) < spec.min_args or (spec.max_args is not None and len(args) > spec.max_args):
            raise FormulaRuntimeError(f"wrong number of arguments: {spec.signature}", node.pos)
        if name == "IF":
            if self.truth(args[0]):
                return self.eval(args[1])
            return self.eval(args[2]) if len(args) > 2 else ZERO
        if name == "AND":
            return _bool(all(self.truth(a) for a in args))
        if name == "OR":
            return _bool(any(self.truth(a) for a in args))
        if name == "NOT":
            return _bool(not self.truth(args[0]))
        if name == "ISBLANK":
            return _bool(self.env.is_blank(args[0].name, args[0].pos))
        if name == "ERROR":
            message = self.eval(args[0])
            raise FormulaRuntimeError(str(message), node.pos, user_message=True)
        if name == "LOOKUP":
            return self._lookup(node)
        numbers = [self.number(a) for a in args]
        try:
            if name in fn.ROUNDERS:
                return fn.ROUNDERS[name](*numbers)
            if name == "CEILING":
                return fn.ceiling(*numbers)
            if name == "FLOOR":
                return fn.floor(*numbers)
            if name == "MOD":
                return fn.mod(*numbers)
        except fn.MathError as exc:
            raise FormulaRuntimeError(f"{name}: {exc}", node.pos) from None
        if name == "MIN":
            return min(numbers)
        if name == "MAX":
            return max(numbers)
        if name == "ABS":
            return abs(numbers[0])
        raise FormulaRuntimeError(f"unknown function {name}", node.pos)  # pragma: no cover

    def _lookup(self, node: Call):
        table_node = node.args[0]
        table = table_node.name if isinstance(table_node, Name) else str(self.eval(table_node)).upper()
        key = self.eval(node.args[1])
        value = self.env.lookup(table, key, node.pos, has_default=len(node.args) > 2)
        if value is None:
            return self.eval(node.args[2])
        return value
