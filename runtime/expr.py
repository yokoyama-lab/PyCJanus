"""
CJanus expression evaluator.

Parses and evaluates CJanus integer expressions like:
  n, 1, n+1, tmp1-tmp2, x<=y, !z, (a+b)*c

Returns (eval_fn, read_addrs) where eval_fn() -> int and
read_addrs is the list of Addr objects read by the expression.
"""
from __future__ import annotations
import re
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .symtab import Addr
    from .pc import PC

# Token types
_TOK_INT  = "INT"
_TOK_NAME = "NAME"
_TOK_OP   = "OP"
_TOK_LPAREN = "LPAREN"
_TOK_RPAREN = "RPAREN"
_TOK_EOF  = "EOF"

_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(\d+)'                          # INT
    r'|([A-Za-z_]\w*(?:\[[\w\d]+\])?)' # NAME (with optional array index)
    r'|(<<=?|>>=?|<=|>=|==|!=|&&|\|\||[-+*/%^&|!<>()])'  # operators/parens
    r')\s*'
)


class _Token:
    __slots__ = ("type", "val")
    def __init__(self, t, v):
        self.type = t
        self.val  = v


def _tokenize(s: str) -> list[_Token]:
    tokens = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            break
        if m.group(1) is not None:
            tokens.append(_Token(_TOK_INT, int(m.group(1))))
        elif m.group(2) is not None:
            tokens.append(_Token(_TOK_NAME, m.group(2)))
        elif m.group(3) is not None:
            ch = m.group(3)
            if ch == "(":
                tokens.append(_Token(_TOK_LPAREN, ch))
            elif ch == ")":
                tokens.append(_Token(_TOK_RPAREN, ch))
            else:
                tokens.append(_Token(_TOK_OP, ch))
        pos = m.end()
    tokens.append(_Token(_TOK_EOF, None))
    return tokens


# Operator precedence (higher = binds tighter)
_PREC = {
    "||": 1,
    "&&": 2,
    "|":  3,
    "^":  4,
    "&":  5,
    "==": 6, "!=": 6,
    "<":  7, ">": 7, "<=": 7, ">=": 7,
    "+":  8, "-":  8,
    "*":  9, "/":  9, "%":  9,
}


class _Parser:
    def __init__(self, tokens: list[_Token], r, p: "PC"):
        self._tok = tokens
        self._pos = 0
        self._r = r
        self._p = p
        self._reads: list[Addr] = []

    def _peek(self) -> _Token:
        return self._tok[self._pos]

    def _consume(self) -> _Token:
        t = self._tok[self._pos]
        self._pos += 1
        return t

    def parse(self) -> Callable[[], int]:
        fn = self._parse_expr(0)
        return fn

    def _parse_expr(self, min_prec: int) -> Callable[[], int]:
        lhs = self._parse_unary()

        while True:
            t = self._peek()
            if t.type != _TOK_OP:
                break
            prec = _PREC.get(t.val, -1)
            if prec < min_prec:
                break
            op = self._consume().val
            rhs = self._parse_expr(prec + 1)
            lhs = self._make_binop(op, lhs, rhs)

        return lhs

    def _make_binop(self, op: str, lhs, rhs) -> Callable[[], int]:
        if op == "+":   return lambda: lhs() + rhs()
        if op == "-":   return lambda: lhs() - rhs()
        if op == "*":   return lambda: lhs() * rhs()
        if op == "/":   return lambda: lhs() // rhs()
        if op == "%":   return lambda: lhs() % rhs()
        if op == "<":   return lambda: 1 if lhs() < rhs() else 0
        if op == ">":   return lambda: 1 if lhs() > rhs() else 0
        if op == "<=":  return lambda: 1 if lhs() <= rhs() else 0
        if op == ">=":  return lambda: 1 if lhs() >= rhs() else 0
        if op == "==":  return lambda: 1 if lhs() == rhs() else 0
        if op == "!=":  return lambda: 1 if lhs() != rhs() else 0
        if op == "&":   return lambda: lhs() & rhs()
        if op == "|":   return lambda: lhs() | rhs()
        if op == "^":   return lambda: lhs() ^ rhs()
        if op == "&&":  return lambda: 1 if (lhs() and rhs()) else 0
        if op == "||":  return lambda: 1 if (lhs() or rhs()) else 0
        raise ValueError(f"Unknown operator: {op}")

    def _parse_unary(self) -> Callable[[], int]:
        t = self._peek()
        if t.type == _TOK_OP and t.val == "!":
            self._consume()
            inner = self._parse_unary()
            return lambda: 0 if inner() else 1
        if t.type == _TOK_OP and t.val == "-":
            self._consume()
            inner = self._parse_unary()
            return lambda: -inner()
        return self._parse_primary()

    def _parse_primary(self) -> Callable[[], int]:
        t = self._peek()
        if t.type == _TOK_INT:
            self._consume()
            v = t.val
            return lambda: v
        if t.type == _TOK_LPAREN:
            self._consume()
            fn = self._parse_expr(0)
            self._consume()  # RPAREN
            return fn
        if t.type == _TOK_NAME:
            self._consume()
            name = t.val
            adr = self._r.get_addr(name, self._p)
            self._reads.append(adr)
            r = self._r
            p = self._p
            return lambda: r.read_addr(adr, p)
        raise ValueError(f"Unexpected token: {t.type}={t.val!r}")


def eval_expr(expr: str, r, p: "PC") -> tuple[Callable[[], int], list[Addr]]:
    """
    Parse and return (eval_fn, read_addrs).
    eval_fn() evaluates the expression at call time.
    read_addrs is the list of addresses read by the expression.
    """
    tokens = _tokenize(expr.strip())
    parser = _Parser(tokens, r, p)
    fn = parser.parse()
    return fn, parser._reads
