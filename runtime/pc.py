"""
CJanus program counter (PC) and basic block execution.
"""
from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from .symtab import Addr
    from .dag import DagPNode
    from .runtime import Runtime

# ---------------------------------------------------------------------------
# Execution result codes
# ---------------------------------------------------------------------------
EXR_NORMAL   = 0  # execution completed normally
EXR_EOF      = 1  # reached end of main
EXR_PROC_END = 2  # non-main process terminated

# Instruction return codes
RET_NORMAL   = 0
RET_EOF      = 1
RET_PROC_END = 2
RET_RESPAWN  = 3  # respawn needed (don't modify head)
RET_BRANCH   = 4  # branch taken (head already updated)


# ---------------------------------------------------------------------------
# RdWtInfo — read/write/lock dependency info
# ---------------------------------------------------------------------------

class RdWtInfo:
    __slots__ = ("wt", "rd", "v", "p", "lock", "unlock")

    def __init__(self):
        self.wt:     list[Addr] = []
        self.rd:     list[Addr] = []
        self.v:      list[Addr] = []
        self.p:      list[Addr] = []
        self.lock:   list[Addr] = []
        self.unlock: list[Addr] = []

    def add(self, other: "RdWtInfo") -> None:
        self.wt     += other.wt
        self.rd     += other.rd
        self.v      += other.v
        self.p      += other.p
        self.lock   += other.lock
        self.unlock += other.unlock

    def compact(self) -> None:
        """Remove duplicates and reads that are also writes."""
        def dedup(lst: list[Addr]) -> list[Addr]:
            seen: list[Addr] = []
            for a in lst:
                if not any(a.equals_mem(s) for s in seen):
                    seen.append(a)
            return seen

        self.wt     = dedup(self.wt)
        self.v      = dedup(self.v)
        self.p      = dedup(self.p)
        self.lock   = dedup(self.lock)
        self.unlock = dedup(self.unlock)
        self.rd     = dedup(self.rd)
        # Remove reads that are also writes
        self.rd = [a for a in self.rd if not any(a.equals_mem(w) for w in self.wt)]


# ---------------------------------------------------------------------------
# LocalAddr
# ---------------------------------------------------------------------------

@dataclass
class LocalAddr:
    addr: "Addr"
    nest: int


# ---------------------------------------------------------------------------
# PC — program counter / process state
# ---------------------------------------------------------------------------

class PC:
    def __init__(self):
        self.head:      int = 0       # line index of next instruction
        self.pc:        int = 0       # step counter (increases by 1 per exec)
        self.pid:       int = 0
        self.originpid: int = 0       # caller's PID
        self.executing: bool = False

        self.nest:       int = 0
        self.localtable: dict[str, list[LocalAddr]] = {}

        self.pnode:      Optional["DagPNode"] = None
        self.dfunc:      list[Callable] = []
        self.lastlabel:  str = ""
        self.children:   list["PC"] = []

    def stringify_pid(self) -> str:
        return str(self.pid)

    def indent(self):   self.nest += 1
    def unindent(self): self.nest -= 1

    def get_block(self) -> str:
        return f":{self.nest}"

    def propagate(self, f: Callable[["PC"], None]) -> None:
        f(self)
        for c in self.children:
            c.propagate(f)

    # -----------------------------------------------------------------------
    # Main execution loop
    # -----------------------------------------------------------------------

    def run_loop(self, r: "Runtime", rev: bool) -> int:
        """
        Inner execution loop.
        Runs blocks until EOF, PROC_END, or stop signal.
        Returns: 0=suspended/stopped, 1=EOF, 2=proc_ended
        """
        from .config import cfg
        from .dag import add_next_node_fwd

        while True:
            # Check stop signal (broadcast Event)
            if r.run_stop.is_set():
                return 0

            self.dfunc = []
            if rev:
                self.pc -= 1
                if self.pnode and self.pnode.prev:
                    self.pnode = self.pnode.prev

            try:
                ret = self.execute_block(r, rev)
            except Exception:
                raise

            # Drain notifications
            try:
                r.dagupdate.get_nowait()
            except Exception:
                pass

            if not rev:
                add_next_node_fwd(self)
                if self.pnode and self.pnode.wt:
                    self.pnode.wt.head.tail = self.pnode.wt
                self.pc += 1
                if self.pnode:
                    self.pnode = self.pnode.next
            else:
                if self.pnode and self.pnode.wt:
                    self.pnode.wt.head.tail = self.pnode.wt.prev

            for f in self.dfunc:
                f()

            try:
                r.dagupdate.put_nowait(True)
            except Exception:
                pass

            if cfg.slow_debug > 0:
                time.sleep(cfg.slow_debug)

            if cfg.step_mode:
                cfg.step_mode = False
                r.run_stop.set()

            if ret == EXR_EOF:
                r.run_stop.set()
                return 1
            elif ret == EXR_PROC_END:
                from .runtime import ProcChange
                r.termproc.put(ProcChange(self.pid, self.originpid))
                return 2

    # -----------------------------------------------------------------------
    # Instruction dispatch
    # -----------------------------------------------------------------------

    def getinst(self, r: "Runtime", inst: str, rev: bool):
        """Returns (prepare_fn, execute_fn) for the given instruction."""
        import re
        for entry in r.instset:
            m = entry.re.fullmatch(inst) or entry.re.match(inst)
            if m:
                groups = list(m.groups())
                args = [inst] + groups
                if rev:
                    return entry.bwd(r, self, args)
                return entry.fwd(r, self, args)
        raise RuntimeError(f"unknown instruction: [{inst!r}]")

    # -----------------------------------------------------------------------
    # Block execution (3 instructions atomically)
    # -----------------------------------------------------------------------

    def execute_block(self, r: "Runtime", rev: bool) -> int:
        from .config import cfg

        self.executing = True
        # Breakpoint check
        if r.breakpoints and self.head in r.breakpoints:
            ln = r.file[self.head] if 0 <= self.head < len(r.file) else "?"
            print(f"\n[BREAK] P{self.pid} hit breakpoint at line {self.head}: {ln!r}")
            r.run_stop.set()
        try:
            lines = r.file
            insts: list[str] = []
            insf: list[Callable[[], RdWtInfo]] = []
            insa: list[Callable[[], int]] = []
            rt = EXR_NORMAL

            if rev:
                if self.head - 2 < 0:
                    self.head = 0
                    return EXR_EOF
                for i in range(3):
                    line = lines[self.head - i - 1]
                    insts.append(line)
                    f, a = self.getinst(r, line, rev)
                    insf.append(f)
                    insa.append(a)
            else:
                if len(lines) <= self.head:
                    self.head = len(lines) - 1
                    return EXR_EOF
                for i in range(3):
                    line = lines[self.head + i]
                    insts.append(line)
                    f, a = self.getinst(r, line, rev)
                    insf.append(f)
                    insa.append(a)

            # Collect read/write info from all 3 instructions
            rdwtall = RdWtInfo()
            for i in range(3):
                rdwtall.add(insf[i]())
            rdwtall.compact()

            dagf = self.wait_runnable(r, rev, rdwtall)

            if cfg.slow_parse:
                m = 0
                while m <= 500000:
                    m += 1

            if not cfg.strict:
                r.skiplock += 1

            # Execute all 3 instructions
            for i in range(3):
                ret = insa[i]()
                if ret == RET_NORMAL:
                    self.head += -1 if rev else 1
                elif ret == RET_EOF:
                    self.head += -1 if rev else 1
                    rt = EXR_EOF
                    break
                elif ret == RET_PROC_END:
                    self.head += -1 if rev else 1
                    rt = EXR_PROC_END
                    break
                elif ret in (RET_RESPAWN, RET_BRANCH):
                    pass  # head already updated or no update needed

            if not cfg.suppress_output:
                print(f"P{self.pid},{self.pc}>")
                print(" ".join(insts))

            dagf()

            if cfg.trace_cli:
                with r._trace_lock:
                    r.trace_log.append({
                        "pid": self.pid, "pc": self.pc, "head": self.head,
                        "insts": " | ".join(insts), "rev": rev,
                    })

            if cfg.exec_debug:
                key = (self.pid, self.pc)
                if not rev:
                    r.exmap[key] = "".join(insts)
                else:
                    s = "".join(reversed(insts))
                    if s != r.exmap.get(key, ""):
                        raise RuntimeError(
                            f"unmatched execution: P{self.pid},{self.pc}\n"
                            f"expected:\n{r.exmap.get(key)}\ngot:\n{s}"
                        )

            if cfg.metrics:
                from .metrics import metrics
                metrics.record_block(metrics._current_phase)

            return rt
        finally:
            self.executing = False

    # -----------------------------------------------------------------------
    # Runability check with retry
    # -----------------------------------------------------------------------

    def try_runnable(self, r: "Runtime", rev: bool, rdwts: RdWtInfo, failctr: int):
        """
        Try to acquire all locks needed for execution.
        Returns (success: bool, cleanup_fn: callable).
        Uses exception-based retry (mimicking Go panic/recover).
        """
        from .config import cfg
        from .dag import (DagLockError, DagRunnableError,
                          PVRunnableError, VarLockRunnableError,
                          assert_runnable)

        saved_dfunc = self.dfunc[:]
        df: list[Callable] = []
        try:
            if cfg.dag_lock_debug:
                print(f"Runnable check started by proc ({self.pid},{self.pc})")

            assert_runnable(self, rev)

            if not rev:
                for v in rdwts.wt:
                    df.append(r.dag.add_w_edge(v, self))
                for v in rdwts.rd:
                    df.append(r.dag.add_r_edge(v, self))

            for v in rdwts.wt:
                r.w_lock_addr(self, v)
            for v in rdwts.v:
                r.try_v(self, v)
            for v in rdwts.p:
                r.try_p(self, v)
            for v in rdwts.lock:
                r.lock_addr(self, v)
            for v in rdwts.unlock:
                a = v
                df.append(lambda _a=a: r.unlock_addr(self, _a))

            if cfg.dag_lock_debug:
                print(f"Runnable check passed by proc ({self.pid},{self.pc})")

            return True, lambda: [fn() for fn in df]

        except (DagLockError, DagRunnableError, PVRunnableError, VarLockRunnableError) as e:
            if cfg.dag_lock_debug:
                print(f"Check fail, recover started by proc ({self.pid},{self.pc})")
            # Release all locks acquired in this attempt
            for f in self.dfunc:
                f()
            self.dfunc = saved_dfunc
            if cfg.dag_lock_debug:
                print(f"Recover ended by proc ({self.pid},{self.pc})")

            if isinstance(e, PVRunnableError):
                wait = 5 * failctr * 1e-6
                time.sleep(wait)
                if cfg.metrics:
                    from .metrics import metrics
                    metrics.record_retry("pv", wait)
            else:
                wait = failctr * 1e-6
                time.sleep(wait)
                if cfg.metrics:
                    from .metrics import metrics
                    metrics.record_retry("dag", wait)

            return False, lambda: None

    def wait_runnable(self, r: "Runtime", rev: bool, rdwts: RdWtInfo) -> Callable:
        """Spin until runnable, checking for stop signal."""
        failctr = 0
        while True:
            if r.run_stop.is_set():
                return lambda: None
            s, f = self.try_runnable(r, rev, rdwts, failctr)
            if s:
                return f
            failctr += 1
            self.dfunc = []


# ---------------------------------------------------------------------------
# Channel helpers (Python queue → Go channel semantics)
# ---------------------------------------------------------------------------

def _try_recv(q) -> bool:
    """Non-blocking receive from a queue. Returns True if something was received."""
    try:
        q.get_nowait()
        return True
    except Exception:
        return False


def _drain(q) -> None:
    """Drain all items from a queue."""
    while True:
        try:
            q.get_nowait()
        except Exception:
            break
