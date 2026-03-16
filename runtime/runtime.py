"""
CJanus runtime: coordinates execution, manages processes, and provides the
debugger loop.
"""
from __future__ import annotations
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .config import cfg
from .dag import DAG, new_dag, DagPNode, PVRunnableError, VarLockRunnableError
from .instset import InstEntry, init_instset
from .label import Labels
from .pc import PC, RET_NORMAL, RET_RESPAWN
from .symtab import Addr, Symtab


@dataclass
class ProcChange:
    term: int
    origin: int


class Runtime(Symtab):
    """CJanus runtime system."""

    def __init__(self, file: list[str]):
        super().__init__()
        self.labels = Labels()
        self.rev = False
        self.instset: list[InstEntry] = []
        self._raw_lines = file   # original lines; load_crl builds padded self.file
        self.file: list[str] = []
        self.dag = new_dag()
        self.rootpc: Optional[PC] = None

        # Execution control (Events for broadcast semantics)
        self.run_active = threading.Event()  # set when processes should run
        self.run_rev = False                  # direction (read-only while run_active is set)
        self.run_stop = threading.Event()    # set when execution is finished or suspended

        # Notification queues (point-to-point)
        self.dagupdate: queue.Queue = queue.Queue(maxsize=16)
        self.newproc:   queue.Queue = queue.Queue(maxsize=16)
        self.termproc:  queue.Queue = queue.Queue(maxsize=16)

        # Execution verification
        self.exmap: dict[tuple[int, int], str] = {}
        self.start_time: float = 0.0
        self.skiplock: int = 0
        self._pidctr = 0
        self._pidctr_lock = threading.Lock()

    def _next_pid(self) -> int:
        with self._pidctr_lock:
            self._pidctr += 1
            return self._pidctr

    # -----------------------------------------------------------------------
    # Child PC management (overrides Symtab._get_child_pc)
    # -----------------------------------------------------------------------

    def _get_child_pc(self, parent: PC, childid: int) -> PC:
        while len(parent.children) <= childid:
            pid = self._next_pid()
            npc = PC()
            npc.pid = pid
            npc.originpid = parent.pid
            npc.pnode = DagPNode(proc=npc, pc=0)
            parent.children.append(npc)
        return parent.children[childid]

    # -----------------------------------------------------------------------
    # Variable locking (delegating to DAG)
    # -----------------------------------------------------------------------

    def try_v(self, p: PC, adr: Addr) -> None:
        if self.read_addr(adr, p) != 0:
            raise PVRunnableError(p)

    def try_p(self, p: PC, adr: Addr) -> None:
        if self.read_addr(adr, p) != 1:
            raise PVRunnableError(p)

    def lock_addr(self, p: PC, a: Addr) -> None:
        e = self.dag.get_entry(a)
        e.lock_node(p)
        if e.try_r_lock():
            return
        raise VarLockRunnableError(p)

    def unlock_addr(self, p: PC, a: Addr) -> None:
        self.dag.get_entry(a).r_unlock()

    def w_lock_addr(self, p: PC, a: Addr) -> None:
        e = self.dag.get_entry(a)
        e.lock_node(p)
        if e.try_w_lock():
            p.dfunc.append(e.w_unlock)
            return
        raise VarLockRunnableError(p)

    # -----------------------------------------------------------------------
    # Process launch
    # -----------------------------------------------------------------------

    def run_pc(self, head: int) -> None:
        if self.rootpc is None:
            self.rootpc = PC()
            self.rootpc.pid = 0
            self.rootpc.originpid = 0
            self.rootpc.pnode = DagPNode(proc=self.rootpc, pc=0)
        self.rootpc.head = head
        self.newproc.put(0)
        rev = self.run_rev
        t = threading.Thread(
            target=self.rootpc.run_loop, args=(self, rev), daemon=True
        )
        t.start()

    def run_pc_call(self, parent: PC, childid: int, head: int, rev: bool,
                    barrier: threading.Barrier, suspended: queue.Queue) -> None:
        p = self._get_child_pc(parent, childid)
        p.head = head
        self.newproc.put(p.pid)

        def run():
            try:
                a = p.run_loop(self, rev)
                if a == 0:
                    suspended.put(True)
            finally:
                try:
                    barrier.wait(timeout=300)
                except threading.BrokenBarrierError:
                    pass
                if cfg.metrics:
                    from .metrics import metrics
                    metrics.record_thread_done()

        t = threading.Thread(target=run, daemon=True)
        if cfg.metrics:
            from .metrics import metrics
            metrics.record_thread_spawned()
        t.start()

    # -----------------------------------------------------------------------
    # Call instruction execution
    # -----------------------------------------------------------------------

    def execute_call(self, p: PC, arglist: str, backward: bool) -> int:
        procnames = [s.strip() for s in arglist.split(",")]

        if cfg.sequential:
            if len(procnames) >= 2:
                raise RuntimeError("sequential mode only allows one call")
            name = procnames[0]
            head = (self.labels.get_end(name) + 1) if backward else self.labels.get_begin(name)
            lasthead = p.head
            p.head = head
            rt = p.run_loop(self, backward)
            p.head = lasthead
            return RET_RESPAWN if rt == 0 else RET_NORMAL

        # Parallel mode
        n = len(procnames)
        barrier = threading.Barrier(n + 1, timeout=300)
        suspended_q: queue.Queue = queue.Queue(maxsize=n)

        for i, v in enumerate(procnames):
            head = (self.labels.get_end(v) + 1) if backward else self.labels.get_begin(v)
            self.run_pc_call(p, i, head, backward, barrier, suspended_q)

        # Release parent locks while children run
        for f in p.dfunc:
            f()
        p.dfunc = []

        # Wait for all children to finish
        try:
            barrier.wait(timeout=300)
        except threading.BrokenBarrierError:
            pass

        return RET_RESPAWN if not suspended_q.empty() else RET_NORMAL

    # -----------------------------------------------------------------------
    # DAG management
    # -----------------------------------------------------------------------

    def reset_dag(self) -> None:
        self.dag = new_dag()
        if self.rootpc:
            self.rootpc.propagate(lambda p: setattr(p.pnode, 'next', None) if p.pnode else None)

    def print_dag(self) -> None:
        skeys = sorted(self.dag.entries.keys(),
                       key=lambda k: (0 if k.is_global else 1, k.idx))
        for k in skeys:
            e = self.dag.entries[k]
            kind = "M" if k.is_global else "V"
            tail = e.tail
            if tail.node is None:
                print(f"Address: {kind}[{k.idx}], Tail at BOT")
            else:
                print(f"Address: {kind}[{k.idx}], Tail at ({tail.node.proc.pid},{tail.node.pc})")
            b = tail
            while b.prev is not None:
                b = b.prev
            while True:
                line = "BOT |" if b.node is None else f"({b.node.proc.pid},{b.node.pc}) |"
                if b.read is not None:
                    a = b.read
                    while True:
                        line += f" ({a.node.proc.pid},{a.node.pc})"
                        if a.next is None:
                            break
                        a = a.next
                print(line)
                if b.next is None:
                    break
                b = b.next
        print()

    # -----------------------------------------------------------------------
    # .crl file loading
    # -----------------------------------------------------------------------

    def load_crl(self) -> None:
        """
        Load and pad the .crl source file, registering all labels.

        Mirrors Go's runDebugger() loader:
        - Empty lines are skipped.
        - Labels are registered at the *current* padded lineno (before padding).
        - If lineno%3==1 and the instruction is end/goto, insert a "skip" first.
        - If lineno%3==2 and the instruction is neither end nor goto, insert an
          implicit block bridge ("-> _k" / "_k <-") first.
        - self.file is replaced with the padded instruction array.
        """
        goreg    = re.compile(
            r'^[\w\d$.:\[\]+\-*/!<=>&|%]*\s*->\s*([\w\d$.:]+)\s*(?:;\s*([\w\d$.:]+)\s*)?')
        comereg  = re.compile(
            r'^\s*([\w\d$.:]+)\s*(?:;\s*([\w\d$.:]+)\s*)?<-[\w\d$.:\[\]+\-*/!<=>&|%]*')
        beginreg = re.compile(r'^begin\s+([\w\d$.:]+)$')
        endreg   = re.compile(r'^end\s+([\w\d$.:]+)$')
        empreg   = re.compile(r'^\s*$')

        f: list[str] = []
        lineno = 0

        for t in self._raw_lines:
            if empreg.match(t):
                continue

            gom = goreg.match(t)
            com = comereg.match(t)
            bgm = beginreg.match(t)
            enm = endreg.match(t)

            # Register goto/comefrom labels at current (pre-padding) lineno
            if gom:
                for i in range(1, 3):
                    s = gom.group(i)
                    if s:
                        self.labels.add_goto(s, lineno)
            if com:
                for i in range(1, 3):
                    s = com.group(i)
                    if s:
                        self.labels.add_come_from(s, lineno)
            if bgm:
                self.labels.add_begin(bgm.group(1), lineno)
            if enm:
                self.labels.add_end(enm.group(1), lineno)

            # Padding rule 1: end/goto at block position 1 → insert skip before it
            if lineno % 3 == 1 and (enm is not None or gom is not None):
                f.append("skip")
                lineno += 1

            # Padding rule 2: ordinary instruction at block position 2
            #   → insert implicit block bridge "-> _k" / "_k <-"
            if lineno % 3 == 2 and enm is None and gom is None:
                s = self.labels.register_new_label(lineno)
                f.append("-> " + s)
                f.append(s + " <-")
                lineno += 2

            f.append(t)
            lineno += 1

        self.file = f

    # -----------------------------------------------------------------------
    # Debug / execution loop
    # -----------------------------------------------------------------------

    def _start_execution(self, rev: bool) -> None:
        """Trigger forward or backward execution."""
        self.run_stop.clear()
        self.run_rev = rev
        st = self.labels.get_begin("main")
        if rev:
            st = self.labels.get_end("main") + 1
        self.run_active.set()
        self.run_pc(st)

    def debug(self, debugsym: queue.Queue, done: threading.Event) -> None:
        """Main debugger loop that coordinates execution."""
        if cfg.timing_debug:
            self.start_time = time.time()

        focus_re = re.compile(r'focus\s+(\d+)')
        focus_tgt = 0
        focus_now = 0

        RUN_RUNNING = 1
        RUN_STOPPED = 0

        # Auto-start forward execution
        self._start_execution(False)
        running = RUN_RUNNING

        while not done.is_set():
            if running == RUN_RUNNING:
                # Check stop signal (execution finished)
                if self.run_stop.is_set():
                    self.run_active.clear()
                    running = RUN_STOPPED
                    print("Debug >> Execution finished")
                    if cfg.timing_debug:
                        elapsed = time.time() - self.start_time
                        print("========Timing Info========")
                        print(f"Elapsed: {elapsed:.6f}s")
                    if cfg.auto:
                        def auto_next():
                            time.sleep(0.1)
                            if cfg.auto_progress == 0:
                                if cfg.metrics:
                                    from .metrics import metrics
                                    metrics.set_phase("bwd")
                                debugsym.put("bwd\n")
                            elif cfg.auto_progress == 1:
                                if cfg.metrics:
                                    from .metrics import metrics
                                    metrics.set_phase("rep")
                                debugsym.put("fwd\n")
                            else:
                                done.set()
                                return
                            cfg.auto_progress += 1
                            time.sleep(0.1)
                            debugsym.put("run\n")
                        threading.Thread(target=auto_next, daemon=True).start()
                    continue

                # Drain newproc / termproc
                try:
                    pid = self.newproc.get_nowait()
                    if pid == focus_tgt and pid != focus_now:
                        focus_now = pid
                except queue.Empty:
                    pass
                try:
                    c = self.termproc.get_nowait()
                    if c.term == focus_now:
                        focus_now = c.origin
                except queue.Empty:
                    pass

                # Check for user interrupt
                try:
                    s = debugsym.get_nowait()
                    if s == "\n":
                        self.run_stop.set()
                        running = RUN_STOPPED
                        print("Debug >> Execution suspended by user")
                    else:
                        print("Debug >> Unknown command (running)")
                except queue.Empty:
                    pass

                time.sleep(1e-4)

            else:  # RUN_STOPPED
                try:
                    s = debugsym.get(timeout=0.1)
                except queue.Empty:
                    try:
                        pid = self.newproc.get_nowait()
                        if pid == focus_tgt:
                            focus_now = pid
                    except queue.Empty:
                        pass
                    try:
                        c = self.termproc.get_nowait()
                        if c.term == focus_now:
                            focus_now = c.origin
                    except queue.Empty:
                        pass
                    continue

                if s == "run\n":
                    if cfg.timing_debug:
                        self.start_time = time.time()
                    self._start_execution(self.rev)
                    running = RUN_RUNNING
                    print("Debug >> Running...")
                elif s == "fwd\n":
                    self.rev = False
                    print("Debug >> Forward mode set")
                elif s == "bwd\n":
                    self.rev = True
                    print("Debug >> Backward mode set")
                elif s == "var\n":
                    self.print_sym()
                elif s == "dag\n":
                    self.print_dag()
                elif s == "deldag\n":
                    self.reset_dag()
                    print("Debug >> Annotation DAG deleted")
                else:
                    m = focus_re.match(s)
                    if m:
                        focus_tgt = int(m.group(1))
                        print(f"Debug >> Focus changed to {focus_tgt}")
                    else:
                        print("Debug >> Unknown command")
