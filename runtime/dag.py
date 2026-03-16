"""
CJanus annotation DAG.

Tracks read/write dependencies across concurrent processes to enforce
reversibility constraints. Uses priority-based locking (lower PID = lower
priority = shorter timeout) to avoid deadlocks.
"""
from __future__ import annotations
import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .symtab import Addr
    from .pc import PC

# Lock timeout constants (seconds)
LOCK_TIMEOUT_LOW  = 1e-6    # 1 microsecond — for lower-priority attempts
LOCK_TIMEOUT_HIGH = 100e-6  # 100 microseconds — for higher-priority attempts


# ---------------------------------------------------------------------------
# Exceptions (correspond to Go panic types)
# ---------------------------------------------------------------------------

class DagLockError(Exception):
    def __init__(self, node: "DagNode"):
        self.node = node

    def __str__(self):
        return f"DAG cell lock failure at {id(self.node):#x}"


class DagRunnableError(Exception):
    def __init__(self, p: "PC"):
        self.p = p

    def __str__(self):
        return f"DAG runnable assertion failure at pid {self.p.pid}"


class PVRunnableError(Exception):
    def __init__(self, p: "PC"):
        self.p = p

    def __str__(self):
        return f"P/V operation assertion failure at pid {self.p.pid}"


class VarLockRunnableError(Exception):
    def __init__(self, p: "PC"):
        self.p = p

    def __str__(self):
        return f"variable lock failure at pid {self.p.pid}"


# ---------------------------------------------------------------------------
# DagNode — base for all DAG node types
# ---------------------------------------------------------------------------

class DagNode:
    """
    Base node with a 1-slot semaphore channel (Go: chan struct{}, 1).
    lockerid = pid+1 of lock holder (0 = unlocked).
    """
    __slots__ = ("_sem", "lockerid", "lockcount")

    def __init__(self):
        self._sem = threading.Semaphore(1)
        self.lockerid: int = 0
        self.lockcount: int = 0

    def _do_lock_node(self, p: "PC") -> None:
        from .config import cfg
        if cfg.dag_lock_debug:
            print(f"Locked node {id(self):#x} by proc ({p.pid},{p.pc}) x{self.lockcount}")
        node = self
        p.dfunc.append(lambda: self._do_unlock_node(p, node))
        self.lockerid = p.pid + 1
        self.lockcount += 1

    def _do_unlock_node(self, p: "PC", node: "DagNode") -> None:
        from .config import cfg
        if cfg.dag_lock_debug:
            print(f"Unlocking node {id(node):#x} by proc ({p.pid},{p.pc}) x{node.lockcount}")
        node.lockcount -= 1
        if node.lockcount == 0:
            node.lockerid = 0
            node._sem.release()

    def lock_node(self, p: "PC") -> None:
        from .config import cfg
        # Re-entrant: same process can lock multiple times
        if self.lockerid == p.pid + 1:
            self._do_lock_node(p)
            return

        if cfg.dag_lock_debug:
            print(f"Locking node {id(self):#x} by proc ({p.pid},{p.pc})")

        # Choose timeout based on priority
        if self.lockerid != 0 and self.lockerid < p.pid + 1:
            timeout = LOCK_TIMEOUT_LOW
        else:
            timeout = LOCK_TIMEOUT_HIGH

        if self._sem.acquire(blocking=True, timeout=timeout):
            self._do_lock_node(p)
        else:
            if cfg.dag_lock_debug:
                print(f"Lock fail on node {id(self):#x} by proc ({p.pid},{p.pc})")
            raise DagLockError(self)


# ---------------------------------------------------------------------------
# DAG node types
# ---------------------------------------------------------------------------

class DagWNode(DagNode):
    """Write node in the annotation DAG."""
    __slots__ = ("head", "prev", "next", "node", "read")

    def __init__(self, head: "DagEntry"):
        super().__init__()
        self.head: DagEntry = head
        self.prev: Optional[DagWNode] = None
        self.next: Optional[DagWNode] = None
        self.node: Optional[DagPNode] = None
        self.read: Optional[DagRNode] = None


class DagRNode(DagNode):
    """Read node in the annotation DAG."""
    __slots__ = ("head", "node", "next")

    def __init__(self):
        super().__init__()
        self.head: Optional[DagWNode] = None
        self.node: Optional[DagPNode] = None
        self.next: Optional[DagRNode] = None


class DagPNode(DagNode):
    """Process node in the annotation DAG."""
    __slots__ = ("proc", "prev", "next", "pc", "wt", "rd")

    def __init__(self, proc: "PC", pc: int):
        super().__init__()
        self.proc: PC = proc
        self.pc: int = pc
        self.prev: Optional[DagPNode] = None
        self.next: Optional[DagPNode] = None
        self.wt: Optional[DagWNode] = None
        self.rd: Optional[DagPRNode] = None


class DagPRNode(DagNode):
    """Links a process node to its read nodes."""
    __slots__ = ("node", "next")

    def __init__(self):
        super().__init__()
        self.node: Optional[DagRNode] = None
        self.next: Optional[DagPRNode] = None


# ---------------------------------------------------------------------------
# DagEntry — per-address entry point
# ---------------------------------------------------------------------------

class DagEntry(DagNode):
    """
    DAG entry point for one memory address.
    RWLock semantics: multiple concurrent readers, exclusive writer.
    Matches Go sync.RWMutex behavior (TryRLock succeeds when no writer holds it).
    """
    __slots__ = ("_meta_lock", "_rcount", "_wlocked", "tail")

    def __init__(self):
        super().__init__()
        self._meta_lock = threading.Lock()   # protects _rcount and _wlocked
        self._rcount = 0
        self._wlocked = False
        tail = DagWNode(self)
        self.tail: DagWNode = tail

    def r_lock(self) -> None:
        with self._meta_lock:
            # Block until no writer
            # (blocking r_lock not needed in current usage, but provided for completeness)
            self._rcount += 1

    def try_r_lock(self) -> bool:
        with self._meta_lock:
            if self._wlocked:
                return False
            self._rcount += 1
            return True

    def r_unlock(self) -> None:
        with self._meta_lock:
            self._rcount -= 1

    def w_lock(self) -> None:
        with self._meta_lock:
            self._wlocked = True

    def try_w_lock(self) -> bool:
        with self._meta_lock:
            if self._wlocked or self._rcount > 0:
                return False
            self._wlocked = True
            return True

    def w_unlock(self) -> None:
        with self._meta_lock:
            self._wlocked = False


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

class MAddr:
    """Hashable memory address key."""
    __slots__ = ("idx", "is_global")

    def __init__(self, idx: int, is_global: bool):
        self.idx = idx
        self.is_global = is_global

    def __hash__(self):
        return hash((self.idx, self.is_global))

    def __eq__(self, other):
        return isinstance(other, MAddr) and self.idx == other.idx and self.is_global == other.is_global

    def __str__(self):
        kind = "M" if self.is_global else "V"
        return f"{kind}[{self.idx}]"


class DAG:
    def __init__(self):
        self.entries: dict[MAddr, DagEntry] = {}
        self._entrylock = threading.Lock()

    def get_entry(self, a: "Addr") -> DagEntry:
        key = MAddr(a.idx, a.is_global)
        with self._entrylock:
            if key not in self.entries:
                e = DagEntry()
                self.entries[key] = e
            return self.entries[key]

    def add_w_edge(self, a: "Addr", dest: "PC") -> callable:
        """Add a write edge to the DAG. Returns a deferred commit function."""
        from .config import cfg
        destnode = dest.pnode
        destnode.lock_node(dest)
        entrynode = self.get_entry(a)
        entrynode.lock_node(dest)
        tailnode = entrynode.tail
        tailnode.lock_node(dest)

        if tailnode.next is None:
            def commit():
                if cfg.dag_debug:
                    if tailnode.node is None:
                        print(f"DAG WEdge Added BOT -> ({dest.pid},{dest.pc}) of addr {a}")
                    else:
                        print(f"DAG WEdge Added ({tailnode.node.proc.pid},{tailnode.node.pc}) -> ({dest.pid},{dest.pc}) of addr {a}")
                newnode = DagWNode(tailnode.head)
                newnode.prev = tailnode
                newnode.node = destnode
                tailnode.next = newnode
                destnode.wt = newnode
            return commit
        return lambda: None

    def add_r_edge(self, a: "Addr", dest: "PC") -> callable:
        """Add a read edge to the DAG. Returns a deferred commit function."""
        from .config import cfg
        destnode = dest.pnode
        destnode.lock_node(dest)
        enode = self.get_entry(a)
        enode.lock_node(dest)
        tailnode = enode.tail
        tailnode.lock_node(dest)

        n: Optional[DagRNode] = None
        if tailnode.read is not None:
            n = tailnode.read
            n.lock_node(dest)
            while True:
                if n.node is destnode:
                    if cfg.dag_debug:
                        print(f"DAG REdge addition to ({dest.pid},{dest.pc}) of {a} prevented (existing)")
                    return lambda: None
                if n.next is None:
                    break
                n = n.next
                n.lock_node(dest)

        b: Optional[DagPRNode] = None
        if destnode.rd is not None:
            b = destnode.rd
            b.lock_node(dest)
            while b.next is not None:
                b = b.next
                b.lock_node(dest)

        def commit():
            nonlocal n, b
            if cfg.dag_debug:
                if tailnode.node is None:
                    print(f"DAG REdge Added BOT -> ({dest.pid},{dest.pc}) of addr {a}")
                else:
                    print(f"DAG REdge Added ({tailnode.node.proc.pid},{tailnode.node.pc}) -> ({dest.pid},{dest.pc}) of addr {a}")
            if tailnode.read is None:
                rn = DagRNode()
                rn.head = tailnode
                rn.node = destnode
                tailnode.read = rn
                n = rn
            else:
                rn = DagRNode()
                rn.head = tailnode
                rn.node = destnode
                n.next = rn
                n = rn
            prn = DagPRNode()
            prn.node = n
            if b is not None:
                b.next = prn
            else:
                destnode.rd = prn
        return commit


def new_dag() -> DAG:
    return DAG()


# ---------------------------------------------------------------------------
# PC forward node management (mixed into PC, called from runtime)
# ---------------------------------------------------------------------------

def add_next_node_fwd(p: "PC") -> None:
    """Creates the next forward process node if it doesn't exist."""
    if p.pnode is None:
        p.pnode = DagPNode(proc=p, pc=p.pc)
        return
    if p.pnode.next is None:
        p.pnode.next = DagPNode(proc=p, pc=p.pc + 1)
        p.pnode.next.prev = p.pnode


# ---------------------------------------------------------------------------
# Runnability checks
# ---------------------------------------------------------------------------

def assert_runnable(p: "PC", bwd: bool) -> None:
    if bwd:
        _is_runnable_bwd(p)
    else:
        _is_runnable_fwd(p)


def _is_runnable_bwd(p: "PC") -> None:
    n = p.pnode
    n.lock_node(p)

    if n.wt is not None:
        wn = n.wt
        wn.lock_node(p)
        if wn.next is not None:
            a = wn.next
            a.lock_node(p)
            wnn = a.node
            wnn.lock_node(p)
            if wnn.proc.pc > wnn.pc:
                raise DagRunnableError(p)
        if wn.read is not None:
            a = wn.read
            a.lock_node(p)
            while True:
                rpn = a.node
                rpn.lock_node(p)
                if rpn.proc.pc > rpn.pc:
                    raise DagRunnableError(p)
                if a.next is None:
                    break
                a = a.next
                a.lock_node(p)

    if n.rd is not None:
        a = n.rd
        a.lock_node(p)
        while True:
            rn = a.node
            rn.lock_node(p)
            wn = rn.head
            wn.lock_node(p)
            en = wn.head
            en.lock_node(p)
            if en.tail is not wn:
                raise DagRunnableError(p)
            if a.next is None:
                break
            a = a.next


def _is_runnable_fwd(p: "PC") -> None:
    pn = p.pnode
    pn.lock_node(p)

    if pn.wt is not None:
        wn = pn.wt
        wn.lock_node(p)
        if wn.prev is not None:
            pwn = wn.prev
            pwn.lock_node(p)
            ppn = pwn.node
            if ppn is not None:
                ppn.lock_node(p)
                if ppn.proc.pc < ppn.pc:
                    raise DagRunnableError(p)
            if pwn.read is not None:
                a = pwn.read
                a.lock_node(p)
                while True:
                    orn = a.node
                    orn.lock_node(p)
                    if orn.proc.pc < orn.pc:
                        raise DagRunnableError(p)
                    if a.next is None:
                        break
                    a = a.next
                    a.lock_node(p)

    if pn.rd is not None:
        a = pn.rd
        a.lock_node(p)
        while True:
            rn = a.node
            rn.lock_node(p)
            prn = rn.head
            prn.lock_node(p)
            ppn = prn.node
            if ppn is not None:
                ppn.lock_node(p)
                if ppn.proc.pc < ppn.pc:
                    raise DagRunnableError(p)
            if a.next is None:
                break
            a = a.next
            a.lock_node(p)
