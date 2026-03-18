"""CJanus memory system: addresses, heap memory, symbol table."""
from __future__ import annotations
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pc import PC


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class HeapMemory:
    """Dynamically-expanding integer array with concurrent access support."""

    def __init__(self):
        self._values: list[int] = []
        self._lock = threading.Lock()

    def get(self, index: int) -> int:
        with self._lock:
            while len(self._values) <= index:
                self._values.append(0)
            return self._values[index]

    def set(self, index: int, value: int) -> None:
        with self._lock:
            while len(self._values) <= index:
                self._values.append(0)
            self._values[index] = value

    @property
    def values(self) -> list[int]:
        with self._lock:
            return list(self._values)


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------

@dataclass
class Addr:
    idx: int
    is_global: bool   # True = M[idx] (heap), False = V[idx] (var)
    sym: str          # original symbol name
    treepid: str      # process tree path

    def equals_mem(self, other: "Addr") -> bool:
        return self.idx == other.idx and self.is_global == other.is_global

    def __str__(self) -> str:
        if self.is_global:
            return f"M[{self.idx}]"
        return f"V[{self.idx}]"

    def to_sym_name(self) -> str:
        if not self.treepid:
            return self.sym
        return f"{self.sym}@{self.treepid}"

    def __hash__(self):
        return hash((self.idx, self.is_global))

    def __eq__(self, other):
        if isinstance(other, Addr):
            return self.equals_mem(other)
        return NotImplemented


# ---------------------------------------------------------------------------
# Compiled regexes for name resolution
# ---------------------------------------------------------------------------

_HEAP_RE = re.compile(r'^M\[([\w\d]+)\]$')
_VAR_RE  = re.compile(r'^(\w[\w\d]*)(?:\[([\w\d]+)\])?$')
_ANNOT_RE = re.compile(r'^([\$\#]?)(\w[\w\d]*)((?:\@\d+)?)$')


# ---------------------------------------------------------------------------
# Symbol Table (mixed into Runtime)
# ---------------------------------------------------------------------------

@dataclass
class LocalAddr:
    addr: Addr
    nest: int


# Sentinel nesting values
HIST_COLON = -1
HIST_AT    = -2


class Symtab:
    """Manages global and local variable allocation and lookup."""

    def __init__(self):
        self.alloctable: dict[str, Addr] = {}
        self._alloclock = threading.RLock()
        self.heapmem = HeapMemory()
        self.varmem  = HeapMemory()
        self._alclimit = 1
        self._alclimlock = threading.Lock()

    # --- Memory selection ---

    def get_mem(self, is_global: bool) -> HeapMemory:
        return self.heapmem if is_global else self.varmem

    # --- Allocation ---

    def alloc_sym(self, p: "PC", is_global: bool, s: str, size: int) -> int:
        with self._alclimlock:
            l = self._alclimit
            self._alclimit += size
        with self._alloclock:
            self.alloctable[s] = Addr(l, False, s, p.stringify_pid())
        for i in range(size):
            self.varmem.set(l + size - i - 1, 0)
        return l

    def get_new_local_addr(self, sym: str, p: "PC") -> Addr:
        with self._alclimlock:
            a = self._alclimit
            self._alclimit += 1
        return Addr(a, False, sym, p.stringify_pid())

    # --- Read/Write ---

    def read_addr(self, a: Addr, p: "PC") -> int:
        from .config import cfg
        mem = self.get_mem(a.is_global)
        v = mem.get(a.idx)
        if cfg.var_debug:
            print(f"ReadOp: {a} -> {v}")
        return v

    def write_addr(self, a: Addr, val: int, p: "PC") -> None:
        from .config import cfg
        mem = self.get_mem(a.is_global)
        watched = getattr(self, 'watched_vars', set())
        do_watch = bool(watched) and a.sym in watched
        if do_watch:
            old = mem.get(a.idx)
        mem.set(a.idx, val)
        if cfg.var_debug:
            print(f"WriteOp: {a} -> {val}")
        if do_watch:
            print(f"[WATCH] {a.sym}({a}) : {old} -> {val}")

    def read_sym(self, s: str, p: "PC") -> int:
        adr = self.get_addr(s, p)
        return self.read_addr(adr, p)

    def write_sym(self, s: str, value: int, p: "PC") -> None:
        adr = self.get_addr(s, p)
        self.write_addr(adr, value, p)

    # --- Name resolution ---

    def get_addr(self, name: str, p: "PC") -> Addr:
        m = _HEAP_RE.match(name)
        if m:
            index = int(m.group(1))
            return Addr(index, True, "M", p.stringify_pid())

        m = _VAR_RE.match(name)
        if m:
            index = 0
            if m.group(2):
                try:
                    index = int(m.group(2))
                except ValueError:
                    index = self.read_sym(m.group(2), p)
            adr = self.get_addr_of_name(m.group(1), p)
            return Addr(adr.idx + index, adr.is_global, m.group(1), p.stringify_pid())

        return self.get_addr_of_name(name, p)

    def get_addr_of_name(self, name: str, p: "PC") -> Addr:
        ads = p.localtable.get(name)
        if ads:
            return ads[-1].addr
        with self._alloclock:
            ad = self.alloctable.get(name)
        if ad is not None:
            return ad
        raise KeyError(f"tried to reference nonexistent variable: {name}")

    def get_addr_of_allocable(self, name: str, p: "PC") -> tuple[Addr, callable]:
        """Resolve a name that may have $/#/@N prefixes. Returns (addr, cleanup_fn)."""
        m = _ANNOT_RE.match(name)
        if m is None:
            return self.get_addr(name, p), lambda: None

        prefix, varname, suffix = m.group(1), m.group(2), m.group(3)

        if not suffix and not prefix:
            return self.get_addr(name, p), lambda: None

        target_p = p
        if suffix:
            spid = suffix.lstrip("@")
            pid = int(spid)
            target_p = self._get_child_pc(p, pid)

        if prefix == "$":
            if self.is_allocated_local(varname, target_p):
                ads = target_p.localtable.get(varname)
                if ads:
                    return ads[-1].addr, lambda: None
                raise RuntimeError(f"deallocation of local variable failed: {varname}")
            ad = self.get_new_local_addr(varname, target_p)
            self.register_local(varname, ad, target_p)
            return ad, lambda: None

        elif prefix == "#":
            if self.is_allocated_global(varname):
                with self._alloclock:
                    ad = self.alloctable.get(varname)
                if ad is not None:
                    return ad, lambda: None
                raise RuntimeError(f"deallocation of global variable failed: {varname}")
            ad = Addr(0, False, varname, "")
            self.register_global(varname, ad)
            return ad, lambda: None

        else:
            return self.get_addr_of_name(varname, target_p), lambda: None

    def _get_child_pc(self, parent: "PC", childid: int) -> "PC":
        """Returns (or creates) the childid-th child PC of parent."""
        # Delegate to the runtime; overridden in Runtime subclass
        raise NotImplementedError("_get_child_pc must be provided by Runtime")

    # --- Allocation registration ---

    def is_allocated_global(self, name: str) -> bool:
        with self._alloclock:
            return name in self.alloctable

    def register_global(self, name: str, addr: Addr) -> None:
        with self._alloclock:
            if name in self.alloctable:
                raise RuntimeError(f"tried to create already existing global variable: {name}")
            self.alloctable[name] = addr

    def delete_global(self, name: str) -> None:
        with self._alloclock:
            if name not in self.alloctable:
                raise RuntimeError(f"tried to delete nonexistent global variable: {name}")
            del self.alloctable[name]

    def is_allocated_local(self, name: str, p: "PC") -> bool:
        ads = p.localtable.get(name)
        if ads:
            return ads[-1].nest == p.nest
        return False

    def register_local(self, name: str, addr: Addr, p: "PC") -> None:
        ads = p.localtable.get(name, [])
        if ads and ads[-1].nest == p.nest:
            raise RuntimeError(f"tried to create already existing local variable: {name}")
        p.localtable.setdefault(name, [])
        p.localtable[name].append(LocalAddr(addr, p.nest))

    def delete_local(self, name: str, p: "PC") -> None:
        ads = p.localtable.get(name)
        if not ads or ads[-1].nest != p.nest:
            raise RuntimeError(f"tried to delete nonexistent local variable: {name}")
        p.localtable[name].pop()

    # --- Set/Unset address binding ---

    def set_addr(self, name: str, ad: Addr, p: "PC") -> None:
        m = _ANNOT_RE.match(name)
        if m is None:
            return
        prefix, varname, suffix = m.group(1), m.group(2), m.group(3)
        target_p = p
        if suffix:
            pid = int(suffix.lstrip("@"))
            target_p = self._get_child_pc(p, pid)
        if prefix == "$":
            if not self.is_allocated_local(varname, target_p):
                self.register_local(varname, ad, target_p)
        elif prefix == "#":
            if not self.is_allocated_global(varname):
                self.register_global(varname, ad)
        else:
            raise RuntimeError("set instruction requires $ or # prefix for allocation")

    def unset_addr(self, name: str, ad: Addr, p: "PC") -> None:
        m = _ANNOT_RE.match(name)
        if m is None:
            raise RuntimeError("unset instruction requires annotated variable name")
        prefix, varname, suffix = m.group(1), m.group(2), m.group(3)
        target_p = p
        if suffix:
            pid = int(suffix.lstrip("@"))
            target_p = self._get_child_pc(p, pid)
        if prefix == "$":
            if self.is_allocated_local(varname, target_p):
                return
            raise RuntimeError(f"unset of local variable failed: not allocated: {varname}")
        elif prefix == "#":
            if self.is_allocated_global(varname):
                val = self.read_addr(ad, p)
                if val != 0:
                    raise RuntimeError(f"unset of non-zero global variable: {varname}")
                return
            raise RuntimeError(f"unset of global variable failed: not allocated: {varname}")
        else:
            raise RuntimeError("unset instruction requires $ or # prefix")

    def set_sym(self, dst: str, src: str, p: "PC") -> None:
        srca, df = self.get_addr_of_allocable(src, p)
        self.set_addr(dst, srca, p)
        df()

    def unset_sym(self, dst: str, src: str, p: "PC") -> None:
        srca, df = self.get_addr_of_allocable(src, p)
        self.unset_addr(dst, srca, p)
        df()

    def print_sym(self) -> None:
        print("\n---Memory Status---")
        print(self.heapmem.values)
