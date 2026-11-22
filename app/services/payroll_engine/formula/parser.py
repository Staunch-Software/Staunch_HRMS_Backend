"""Parser for the pay-group formula language.

Precedence, lowest first: OR, AND, NOT, comparison (= <> < <= > >=),
+ -, * /, unary - +, postfix %. AND/OR/NOT also work as functions:
AND(a, b, ...), OR(a, b, ...), NOT(a).
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Union

from app.services.payroll_engine.formula.errors import FormulaError
from app.services.payroll_engine.formula.lexer import END, NAME, NUMBER, OP, STRING, Token, tokenize

COMPARISONS = ("=", "<>", "<", "<=", ">", ">=")
KEYWORDS = ("AND", "OR", "NOT", "TRUE", "FALSE")


@dataclass(frozen=True)
class Num:
    value: Decimal
    pos: int


@dataclass(frozen=True)
class Str:
    value: str
    pos: int


@dataclass(frozen=True)
class Name:
    name: str
    pos: int


@dataclass(frozen=True)
class Unary:
    op: str  # '-', '+', 'NOT', '%'
    operand: "Node"
    pos: int


@dataclass(frozen=True)
class Binary:
    op: str  # + - * / comparisons AND OR
    left: "Node"
    right: "Node"
    pos: int


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple = field(default_factory=tuple)
    pos: int = 0


Node = Union[Num, Str, Name, Unary, Binary, Call]


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def is_op(self, *values) -> bool:
        return self.tok.kind == OP and self.tok.value in values

    def is_word(self, word: str) -> bool:
        return self.tok.kind == NAME and self.tok.value == word

    def expect_op(self, value: str) -> Token:
        if not self.is_op(value):
            raise FormulaError(f"expected '{value}' but found {_describe(self.tok)}", self.tok.position)
        return self.advance()

    def parse(self) -> Node:
        node = self.or_expr()
        if self.tok.kind != END:
            raise FormulaError(f"unexpected {_describe(self.tok)}", self.tok.position)
        return node

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.is_word("OR"):
            pos = self.advance().position
            node = Binary("OR", node, self.and_expr(), pos)
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.is_word("AND"):
            pos = self.advance().position
            node = Binary("AND", node, self.not_expr(), pos)
        return node

    def not_expr(self) -> Node:
        if self.is_word("NOT"):
            pos = self.advance().position
            return Unary("NOT", self.not_expr(), pos)
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        if self.is_op(*COMPARISONS):
            token = self.advance()
            node = Binary(token.value, node, self.additive(), token.position)
            if self.is_op(*COMPARISONS):
                raise FormulaError("comparisons cannot be chained; combine them with AND / OR", self.tok.position)
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.is_op("+", "-"):
            token = self.advance()
            node = Binary(token.value, node, self.multiplicative(), token.position)
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.is_op("*", "/"):
            token = self.advance()
            node = Binary(token.value, node, self.unary(), token.position)
        return node

    def unary(self) -> Node:
        if self.is_op("-", "+"):
            token = self.advance()
            return Unary(token.value, self.unary(), token.position)
        node = self.primary()
        while self.is_op("%"):
            node = Unary("%", node, self.advance().position)
        return node

    def primary(self) -> Node:
        token = self.tok
        if token.kind == NUMBER:
            self.advance()
            return Num(token.value, token.position)
        if token.kind == STRING:
            self.advance()
            return Str(token.value, token.position)
        if token.kind == NAME:
            self.advance()
            if self.is_op("("):
                return self.call(token)
            if token.value in ("TRUE", "FALSE"):
                return Num(Decimal(1 if token.value == "TRUE" else 0), token.position)
            if token.value in KEYWORDS:
                raise FormulaError(f"'{token.value}' needs a value before it", token.position)
            return Name(token.value, token.position)
        if self.is_op("("):
            self.advance()
            node = self.or_expr()
            self.expect_op(")")
            return node
        raise FormulaError(f"expected a value but found {_describe(token)}", token.position)

    def call(self, name_token: Token) -> Node:
        self.expect_op("(")
        args = []
        if not self.is_op(")"):
            args.append(self.or_expr())
            while self.is_op(","):
                self.advance()
                args.append(self.or_expr())
        self.expect_op(")")
        if name_token.value == "NOT" and len(args) == 1:
            return Unary("NOT", args[0], name_token.position)
        return Call(name_token.value, tuple(args), name_token.position)


def _describe(token: Token) -> str:
    if token.kind == END:
        return "the end of the formula"
    if token.kind == STRING:
        return f'text "{token.value}"'
    return f"'{token.value}'"


def parse(text: str) -> Node:
    return _Parser(tokenize(text)).parse()


def walk(node: Node):
    yield node
    if isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Call):
        for arg in node.args:
            yield from walk(arg)
