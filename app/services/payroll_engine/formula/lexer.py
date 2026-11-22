"""Tokenizer for the pay-group formula language (Excel-like syntax)."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.services.payroll_engine.formula.errors import FormulaError

NUMBER, STRING, NAME, OP, END = "NUMBER", "STRING", "NAME", "OP", "END"

_TWO_CHAR_OPS = ("<=", ">=", "<>", "!=", "==")
_ONE_CHAR_OPS = "+-*/%(),=<>"


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    position: int  # 1-based


def tokenize(text: str) -> list[Token]:
    if text is None or not str(text).strip():
        raise FormulaError("formula is empty")
    text = str(text)
    tokens: list[Token] = []
    i, n = 0, len(text)
    stripped = text.lstrip()
    if stripped.startswith("=") and not stripped.startswith("=="):
        i = len(text) - len(stripped) + 1  # Excel habit: a leading '=' is ignored
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        start = i
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            while i < n and (text[i].isdigit() or text[i] == "."):
                i += 1
            if i < n and text[i] in "eE" and i + 1 < n and (text[i + 1].isdigit() or text[i + 1] in "+-"):
                i += 2
                while i < n and text[i].isdigit():
                    i += 1
            raw = text[start:i]
            try:
                tokens.append(Token(NUMBER, Decimal(raw), start + 1))
            except InvalidOperation:
                raise FormulaError(f"invalid number '{raw}'", start + 1) from None
            continue
        if ch.isalpha() or ch == "_":
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append(Token(NAME, text[start:i].upper(), start + 1))
            continue
        if ch in "\"'":
            quote, i, chars = ch, i + 1, []
            while True:
                if i >= n:
                    raise FormulaError("text is missing its closing quote", start + 1)
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:  # doubled quote = literal quote
                        chars.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(text[i])
                i += 1
            tokens.append(Token(STRING, "".join(chars), start + 1))
            continue
        pair = text[i:i + 2]
        if pair in _TWO_CHAR_OPS:
            tokens.append(Token(OP, {"!=": "<>", "==": "="}.get(pair, pair), start + 1))
            i += 2
            continue
        if ch in _ONE_CHAR_OPS:
            tokens.append(Token(OP, ch, start + 1))
            i += 1
            continue
        raise FormulaError(f"unexpected character '{ch}'", start + 1)
    tokens.append(Token(END, None, n + 1))
    return tokens
