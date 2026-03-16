"""
CJanus instruction set.

Each instruction is a (regex, fwd_handler, bwd_handler) triple.
Handlers return (prepare_fn, execute_fn) where:
  prepare_fn() -> RdWtInfo  (collects read/write dependencies)
  execute_fn() -> int        (executes and returns RET_*)
"""
from __future__ import annotations
import re
from typing import TYPE_CHECKING, Callable

from .pc import RdWtInfo, RET_NORMAL, RET_EOF, RET_PROC_END, RET_RESPAWN, RET_BRANCH
from .expr import eval_expr

if TYPE_CHECKING:
    from .runtime import Runtime
    from .pc import PC
    from .symtab import Addr


class InstEntry:
    __slots__ = ("re", "fwd", "bwd")

    def __init__(self, pattern: str, fwd, bwd):
        self.re = re.compile(pattern)
        self.fwd = fwd
        self.bwd = bwd


def init_instset(r: "Runtime") -> list[InstEntry]:
    entries: list[InstEntry] = []

    def add(pattern, fwd, bwd):
        entries.append(InstEntry(pattern, fwd, bwd))

    # ----- x += a / x -= a / x ^= a -----
    add(
        r'^([\w\d$#.:\[\]]+)\s*([+\-^])=\s*([\w\d$#.:\[\]+\-*/!<=>&|%\(\) ]+)\s*$',
        _arith_fwd, _arith_bwd
    )

    # ----- print x -----
    add(
        r'^print\s+([\w\d$#.:\[\]]+)$',
        _print_fwd, _print_bwd
    )

    # ----- skip -----
    add(
        r'^skip',
        _skip, _skip
    )

    # ----- -> L (unconditional goto) -----
    add(
        r'^->\s*([\w\d$#.:]+)$',
        _goto_fwd, _goto_bwd
    )

    # ----- L <- (come-from) -----
    add(
        r'^([\w\d$#.:]+)\s*<-$',
        _comefrom_fwd, _comefrom_bwd
    )

    # ----- a <=> b (swap) -----
    add(
        r'^([\w\d$#.:\[\]]+)\s*<=>\s*([\w\d$#.:\[\]]+)$',
        _swap, _swap
    )

    # ----- expr -> L1;L2 (conditional branch) -----
    add(
        r'^([\w\d$#.:\[\]+\-*/!<=>&|%\(\) ]+)\s*->\s*([\w\d$#.:]+)\s*;\s*([\w\d$#.:]+)\s*$',
        _condbranch_fwd, _condbranch_bwd
    )

    # ----- L1;L2 <- expr (conditional come-from) -----
    add(
        r'^([\w\d$#.:]+)\s*;\s*([\w\d$#.:]+)\s*<-\s*([\w\d$#.:\[\]+\-*/!<=>&|%\(\) ]+)$',
        _condcomefrom_fwd, _condcomefrom_bwd
    )

    # ----- begin <name> -----
    add(
        r'^begin\s+([\w\d]+)',
        _begin_fwd, _begin_bwd
    )

    # ----- end <name> -----
    add(
        r'^end\s+([\w\d$#.:]+)',
        _end_fwd, _end_bwd
    )

    # ----- call proc1, proc2, ... -----
    add(
        r'^call\s+(.*)',
        _call_fwd, _call_bwd
    )

    # ----- push -----
    add(
        r'^push',
        _push_fwd, _push_bwd
    )

    # ----- pop -----
    add(
        r'^pop',
        _pop_fwd, _pop_bwd
    )

    # ----- set dst src -----
    add(
        r'^set\s+([\w\d$#.:\[\]\@]+)\s+([\w\d$#.:\[\]]+)',
        _set_fwd, _set_bwd
    )

    # ----- unset dst src -----
    add(
        r'^unset\s+([\w\d$#.:\[\]\@]+)\s+([\w\d$#.:\[\]]+)',
        _unset_fwd, _unset_bwd
    )

    # ----- V (semaphore signal) -----
    add(
        r'^V\s+([\w\d$#.:\[\]]+)',
        _sem_v_fwd, _sem_v_bwd
    )

    # ----- P (semaphore wait) -----
    add(
        r'^P\s+([\w\d$#.:\[\]]+)',
        _sem_p_fwd, _sem_p_bwd
    )

    # ----- lock var1, var2, ... -----
    add(
        r'^lock\s+([\w\d\s.:\[\],]+)',
        _lock_fwd, _lock_bwd
    )

    # ----- unlock var1, var2, ... -----
    add(
        r'^unlock\s+([\w\d\s.:\[\],]+)',
        _unlock_fwd, _unlock_bwd
    )

    return entries


# ===========================================================================
# Instruction handlers
# ===========================================================================

# ----- Arithmetic: x += a / x -= a / x ^= a -----

def _arith_fwd(r, p, arg):
    state = {}
    expr_str = arg[3]
    dest_name = arg[1]
    op = arg[2]

    def prepare():
        ev, rd = eval_expr(expr_str, r, p)
        dest, dest_fn = r.get_addr_of_allocable(dest_name, p)
        state["eval"] = ev
        state["dest"] = dest
        state["dest_fn"] = dest_fn
        info = RdWtInfo()
        info.rd += rd
        info.wt.append(dest)
        return info

    if op == "+":
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v + state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL
    elif op == "-":
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v - state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL
    else:  # ^
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v ^ state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL

    return prepare, execute


def _arith_bwd(r, p, arg):
    state = {}
    expr_str = arg[3]
    dest_name = arg[1]
    op = arg[2]

    def prepare():
        ev, rd = eval_expr(expr_str, r, p)
        dest, dest_fn = r.get_addr_of_allocable(dest_name, p)
        state["eval"] = ev
        state["dest"] = dest
        state["dest_fn"] = dest_fn
        info = RdWtInfo()
        info.rd += rd
        info.wt.append(dest)
        return info

    # Reverse the operation
    if op == "+":
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v - state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL
    elif op == "-":
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v + state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL
    else:  # ^ (XOR is self-inverse)
        def execute():
            v = r.read_addr(state["dest"], p)
            r.write_addr(state["dest"], v ^ state["eval"](), p)
            state["dest_fn"]()
            return RET_NORMAL

    return prepare, execute


# ----- Print -----

def _print_fwd(r, p, arg):
    return (lambda: RdWtInfo(),
            lambda: (print(f">>out: {arg[1]} {r.read_sym(arg[1], p)}"), RET_NORMAL)[1])

def _print_bwd(r, p, arg):
    return (lambda: RdWtInfo(),
            lambda: (print(f"<<out: {arg[1]} {r.read_sym(arg[1], p)}"), RET_NORMAL)[1])


# ----- Skip -----

def _skip(r, p, arg):
    return (lambda: RdWtInfo(), lambda: RET_NORMAL)


# ----- Goto / Come-from -----

def _goto_fwd(r, p, arg):
    def execute():
        p.lastlabel = arg[1]
        p.head = r.labels.get_come_from(arg[1])
        return RET_BRANCH
    return (lambda: RdWtInfo(), execute)

def _goto_bwd(r, p, arg):
    def execute():
        if p.lastlabel != arg[1]:
            raise RuntimeError(f"label mismatch after branch: expected {arg[1]}, got {p.lastlabel}")
        p.lastlabel = ""
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _comefrom_fwd(r, p, arg):
    def execute():
        if p.lastlabel != arg[1]:
            raise RuntimeError(f"come-from label mismatch: expected {arg[1]}, got {p.lastlabel}")
        p.lastlabel = ""
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _comefrom_bwd(r, p, arg):
    def execute():
        p.lastlabel = arg[1]
        p.head = r.labels.get_goto(arg[1]) + 1
        return RET_BRANCH
    return (lambda: RdWtInfo(), execute)


# ----- Swap -----

def _swap(r, p, arg):
    def execute():
        a = r.read_sym(arg[1], p)
        b = r.read_sym(arg[2], p)
        r.write_sym(arg[1], b, p)
        r.write_sym(arg[2], a, p)
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)


# ----- Conditional branch: expr -> L1;L2 -----

def _condbranch_fwd(r, p, arg):
    state = {}
    def prepare():
        ev, rd = eval_expr(arg[1], r, p)
        state["eval"] = ev
        info = RdWtInfo()
        info.rd += rd
        return info
    def execute():
        if state["eval"]() != 0:
            p.lastlabel = arg[2]
            p.head = r.labels.get_come_from(arg[2])
        else:
            p.lastlabel = arg[3]
            p.head = r.labels.get_come_from(arg[3])
        return RET_BRANCH
    return prepare, execute

def _condbranch_bwd(r, p, arg):
    state = {}
    def prepare():
        ev, rd = eval_expr(arg[1], r, p)
        state["eval"] = ev
        info = RdWtInfo()
        info.rd += rd
        return info
    def execute():
        if state["eval"]() != 0:
            if p.lastlabel != arg[2]:
                raise RuntimeError("label mismatch after branch")
        else:
            if p.lastlabel != arg[3]:
                raise RuntimeError("label mismatch after branch")
        p.lastlabel = ""
        return RET_NORMAL
    return prepare, execute


# ----- Conditional come-from: L1;L2 <- expr -----

def _condcomefrom_fwd(r, p, arg):
    state = {}
    def prepare():
        ev, rd = eval_expr(arg[3], r, p)
        state["eval"] = ev
        info = RdWtInfo()
        info.rd += rd
        return info
    def execute():
        if state["eval"]() != 0:
            if p.lastlabel != arg[1]:
                raise RuntimeError("label mismatch after branch")
        else:
            if p.lastlabel != arg[2]:
                raise RuntimeError("label mismatch after branch")
        p.lastlabel = ""
        return RET_NORMAL
    return prepare, execute

def _condcomefrom_bwd(r, p, arg):
    state = {}
    def prepare():
        ev, rd = eval_expr(arg[3], r, p)
        state["eval"] = ev
        info = RdWtInfo()
        info.rd += rd
        return info
    def execute():
        if state["eval"]() != 0:
            p.lastlabel = arg[1]
            p.head = r.labels.get_goto(arg[1]) + 1
        else:
            p.lastlabel = arg[2]
            p.head = r.labels.get_goto(arg[2]) + 1
        return RET_BRANCH
    return prepare, execute


# ----- Begin / End -----

def _begin_fwd(r, p, arg):
    from .config import cfg
    def execute():
        if not cfg.sequential and p.nest != 0:
            raise RuntimeError("process started at non-zero nest depth")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _begin_bwd(r, p, arg):
    from .config import cfg
    def execute():
        if not cfg.sequential and p.nest != 0:
            raise RuntimeError("process ended at non-zero nest depth")
        if arg[1] == "main":
            return RET_EOF
        return RET_PROC_END
    return (lambda: RdWtInfo(), execute)

def _end_fwd(r, p, arg):
    from .config import cfg
    def execute():
        if not cfg.sequential and p.nest != 0:
            raise RuntimeError("process ended at non-zero nest depth")
        if arg[1] == "main":
            return RET_EOF
        return RET_PROC_END
    return (lambda: RdWtInfo(), execute)

def _end_bwd(r, p, arg):
    from .config import cfg
    def execute():
        if not cfg.sequential and p.nest != 0:
            raise RuntimeError("process started at non-zero nest depth")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)


# ----- Call -----

def _call_fwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: r.execute_call(p, arg[1], False))

def _call_bwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: r.execute_call(p, arg[1], True))


# ----- Push / Pop -----

def _push_fwd(r, p, arg):
    from .config import cfg
    def execute():
        if cfg.block_debug: print(f"bin{p.get_block()}f")
        p.indent()
        if cfg.block_debug: print(f"bou{p.get_block()}f")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _push_bwd(r, p, arg):
    from .config import cfg
    def execute():
        if cfg.block_debug: print(f"bin{p.get_block()}f")
        p.unindent()
        if cfg.block_debug: print(f"bou{p.get_block()}f")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _pop_fwd(r, p, arg):
    from .config import cfg
    def execute():
        if cfg.block_debug: print(f"bin{p.get_block()}f")
        p.unindent()
        if cfg.block_debug: print(f"bou{p.get_block()}f")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)

def _pop_bwd(r, p, arg):
    from .config import cfg
    def execute():
        if cfg.block_debug: print(f"bin{p.get_block()}f")
        p.indent()
        if cfg.block_debug: print(f"bou{p.get_block()}f")
        return RET_NORMAL
    return (lambda: RdWtInfo(), execute)


# ----- Set / Unset -----

def _set_fwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: (r.set_sym(arg[1], arg[2], p), RET_NORMAL)[1])

def _set_bwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: (r.unset_sym(arg[1], arg[2], p), RET_NORMAL)[1])

def _unset_fwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: (r.unset_sym(arg[1], arg[2], p), RET_NORMAL)[1])

def _unset_bwd(r, p, arg):
    return (lambda: RdWtInfo(), lambda: (r.set_sym(arg[1], arg[2], p), RET_NORMAL)[1])


# ----- Semaphore V/P -----

def _sem_v_fwd(r, p, arg):
    state = {}
    def prepare():
        a = r.get_addr(arg[1], p)
        state["a"] = a
        info = RdWtInfo()
        info.wt.append(a)
        info.v.append(a)
        return info
    def execute():
        r.write_addr(state["a"], 1, p)
        return RET_NORMAL
    return prepare, execute

def _sem_v_bwd(r, p, arg):
    state = {}
    def prepare():
        a = r.get_addr(arg[1], p)
        state["a"] = a
        info = RdWtInfo()
        info.wt.append(a)
        info.p.append(a)
        return info
    def execute():
        r.write_addr(state["a"], 0, p)
        return RET_NORMAL
    return prepare, execute

def _sem_p_fwd(r, p, arg):
    state = {}
    def prepare():
        a = r.get_addr(arg[1], p)
        state["a"] = a
        info = RdWtInfo()
        info.wt.append(a)
        info.p.append(a)
        return info
    def execute():
        r.write_addr(state["a"], 0, p)
        return RET_NORMAL
    return prepare, execute

def _sem_p_bwd(r, p, arg):
    state = {}
    def prepare():
        a = r.get_addr(arg[1], p)
        state["a"] = a
        info = RdWtInfo()
        info.wt.append(a)
        info.v.append(a)
        return info
    def execute():
        r.write_addr(state["a"], 1, p)
        return RET_NORMAL
    return prepare, execute


# ----- Lock / Unlock -----

def _lock_fwd(r, p, arg):
    def prepare():
        info = RdWtInfo()
        for v in arg[1].split(","):
            v = v.strip()
            if v:
                a = r.get_addr(v, p)
                info.rd.append(a)
                info.lock.append(a)
        return info
    return prepare, lambda: RET_NORMAL

def _lock_bwd(r, p, arg):
    def prepare():
        info = RdWtInfo()
        for v in arg[1].split(","):
            v = v.strip()
            if v:
                a = r.get_addr(v, p)
                info.rd.append(a)
                info.unlock.append(a)
        return info
    return prepare, lambda: RET_NORMAL

def _unlock_fwd(r, p, arg):
    def prepare():
        info = RdWtInfo()
        for v in arg[1].split(","):
            v = v.strip()
            if v:
                a = r.get_addr(v, p)
                info.rd.append(a)
                info.unlock.append(a)
        return info
    return prepare, lambda: RET_NORMAL

def _unlock_bwd(r, p, arg):
    def prepare():
        info = RdWtInfo()
        for v in arg[1].split(","):
            v = v.strip()
            if v:
                a = r.get_addr(v, p)
                info.rd.append(a)
                info.lock.append(a)
        return info
    return prepare, lambda: RET_NORMAL
