"""
RuntimeMP — multiprocessing-based CJanus runtime.

Architecture
------------
The CJanus runtime state is a dense graph of Python objects:

  Runtime (Symtab)
    ├── heapmem / varmem  (HeapMemory — list[int] + threading.Lock)
    ├── dag               (DAG — dict of DagEntry, each with threading.Semaphore
    │                       and threading.Lock objects)
    └── PC tree           (PC — localtable, pnode DagPNode …)

Python's ``multiprocessing.Process`` spawns a *new interpreter* for each
worker.  Objects passed as arguments are *pickled*, which means:

  - threading.Lock / threading.Semaphore → NOT picklable (raise TypeError)
  - The entire DAG, all DagNode subclasses → NOT picklable

Consequence: a direct drop-in replacement of Thread→Process for the
inner ``run_loop`` is **not possible** without a full rewrite of the DAG
layer to use multiprocessing-safe primitives.

This module provides two layers:

Layer 1 (``RuntimeMP``)
    A subclass of Runtime that overrides ``execute_call``.  For the
    common case of a *single* call-arg (no parallelism), it delegates
    to the threading base class.  For *multi-arg* calls it currently
    delegates to the threading base class as well, but with a hook
    (``_mp_enabled``) that can be flipped to enable the experimental
    Layer-2 path once the DAG is ported.

Layer 2 (``_mp_execute_call_stub``)
    Worker function launched as a ``multiprocessing.Process``.  It
    reconstructs a runtime from serialisable snapshot data and runs a
    single child procedure.

    Phase 0: alclimit / pidctr / parent.lastlabel restored to fix
             label-mismatch-after-branch errors.
    Phase 1: heapmem / varmem backed by multiprocessing.SharedMemory so
             that child writes are immediately visible to the parent.
             No result copying needed.
    Phase 2: DagNode / DagEntry / DAG have __getstate__/__setstate__, so
             dag.py classes are pickle-safe.  Each child runs with a
             fresh DAG (new_dag()) so reversibility is enforced locally
             without needing to merge back into the parent DAG.

Usage (experimental MP path)
    Pass ``--mp`` to ``main_mp.py``.  The RuntimeMP class has
    ``_mp_enabled = False`` by default; set it to True to activate the
    Process-based path for multi-proc calls.
"""
from __future__ import annotations

import ctypes
import multiprocessing as _mp
import multiprocessing.shared_memory as _shm
import threading
from typing import Optional

from runtime.config import cfg
from runtime.runtime import Runtime
from runtime.pc import PC, RET_NORMAL, RET_RESPAWN


# ---------------------------------------------------------------------------
# Phase 1: SharedMemory-backed HeapMemory compatible class
# ---------------------------------------------------------------------------

class SharedHeapMemory:
    """
    Drop-in replacement for HeapMemory backed by multiprocessing.SharedMemory.

    In the parent process, ``create=True`` allocates a new shared segment and
    copies existing values into it.  In child processes, ``create=False``
    attaches to an existing segment by name.

    Thread safety: an internal threading.Lock protects reads/writes within a
    single process.  Cross-process safety relies on disjoint address ranges
    (each concurrent call procedure uses its own local variables).
    """

    def __init__(self, name: str, size: int, create: bool = False,
                 initial: Optional[list[int]] = None):
        self._size = max(size, 8)
        nbytes = self._size * ctypes.sizeof(ctypes.c_int64)
        if create:
            self._shm = _shm.SharedMemory(create=True, size=nbytes)
        else:
            self._shm = _shm.SharedMemory(name=name, create=False)
        self._arr = (ctypes.c_int64 * self._size).from_buffer(self._shm.buf)
        self._lock = threading.Lock()
        if create and initial:
            n = min(len(initial), self._size)
            for i in range(n):
                self._arr[i] = initial[i]

    @property
    def name(self) -> str:
        return self._shm.name

    def get(self, index: int) -> int:
        with self._lock:
            if index >= self._size:
                return 0
            return int(self._arr[index])

    def set(self, index: int, value: int) -> None:
        with self._lock:
            if index >= self._size:
                return
            self._arr[index] = value

    @property
    def values(self) -> list[int]:
        with self._lock:
            return [int(self._arr[i]) for i in range(self._size)]

    def close(self):
        """Detach from the shared segment (do not unlink)."""
        self._shm.close()

    def unlink(self):
        """Unlink the shared segment (call once, from the creating process)."""
        self._shm.unlink()


# ---------------------------------------------------------------------------
# Serialisable snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_runtime(r: "Runtime") -> dict:
    """
    Capture the serialisable parts of the runtime for transmission to
    a child process.

    Returns a plain dict that can be pickled.
    Phase 0 additions: alclimit, pidctr.
    """
    return {
        "file":      list(r.file),           # padded instruction list
        "heapmem":   list(r.heapmem.values), # flat int array
        "varmem":    list(r.varmem.values),  # flat int array
        "alloctable": {
            name: (addr.idx, addr.is_global, addr.sym, addr.treepid)
            for name, addr in r.alloctable.items()
        },
        "labels_goto":       dict(r.labels._gotomap),
        "labels_comefrom":   dict(r.labels._comemap),
        "labels_begin":      dict(r.labels._beginmap),
        "labels_end":        dict(r.labels._endmap),
        "labels_counter":    r.labels._tmplabel,
        # Phase 0: preserve allocator state so children don't clobber addresses
        "alclimit":  r._alclimit,
        "pidctr":    r._pidctr,
    }


def _mp_execute_call_stub(
    snapshot: dict,
    parent_pid: int,
    parent_originpid: int,
    parent_nest: int,
    parent_localtable_raw: dict,   # {name: [(idx, is_global, sym, treepid, nest)]}
    parent_lastlabel: str,         # Phase 0: needed for branch validation
    childid: int,
    head: int,
    backward: bool,
    result_q,                      # multiprocessing.Queue
    shm_heap_name: str,            # Phase 1: SharedMemory name for heapmem
    shm_heap_size: int,
    shm_var_name: str,             # Phase 1: SharedMemory name for varmem
    shm_var_size: int,
) -> None:
    """
    Worker function executed in a child process.

    Reconstructs a Runtime from *snapshot* and runs a single child
    procedure starting at *head*.  Results are signalled via *result_q*.

    Phase 1: heapmem / varmem are backed by SharedMemory so writes are
             immediately visible in the parent.
    Phase 2: A fresh DAG is created (cfg.no_dag=False) so reversibility
             is enforced within the child's own execution.
    """
    import sys
    import os

    # Add cjanus_py to sys.path so runtime imports work in child process
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from runtime.config import cfg as child_cfg
    from runtime.dag import new_dag
    from runtime.instset import init_instset
    from runtime.runtime import Runtime
    from runtime.symtab import Addr
    from runtime.pc import PC, EXR_EOF, EXR_PROC_END

    # Silence output in workers
    child_cfg.suppress_output = True
    # Phase 2: enable DAG checking in child (child gets its own fresh DAG)
    child_cfg.no_dag = False

    # Reconstruct runtime from snapshot
    r = Runtime([])
    r.file = snapshot["file"]

    # Phase 1: attach to SharedMemory for heapmem / varmem
    child_heap = SharedHeapMemory(shm_heap_name, shm_heap_size, create=False)
    child_var  = SharedHeapMemory(shm_var_name,  shm_var_size,  create=False)
    r.heapmem = child_heap
    r.varmem  = child_var

    # Restore alloc table
    for name, (idx, is_global, sym, treepid) in snapshot["alloctable"].items():
        r.alloctable[name] = Addr(idx, is_global, sym, treepid)

    # Restore labels
    r.labels._gotomap    = snapshot["labels_goto"]
    r.labels._comemap    = snapshot["labels_comefrom"]
    r.labels._beginmap   = snapshot["labels_begin"]
    r.labels._endmap     = snapshot["labels_end"]
    r.labels._tmplabel   = snapshot["labels_counter"]

    # Phase 0: restore allocator state
    r._alclimit = snapshot["alclimit"]
    # Give each child a distinct PID range to avoid collisions
    r._pidctr   = snapshot["pidctr"] + (childid + 1) * 10000

    # Phase 2: fresh DAG for this child's reversibility tracking
    r.dag = new_dag()

    r.instset = init_instset(r)

    # Build parent PC stub
    from runtime.dag import DagPNode
    parent = PC()
    parent.pid = parent_pid
    parent.originpid = parent_originpid
    parent.nest = parent_nest
    parent.pnode = DagPNode(proc=parent, pc=0)
    # Phase 0: restore lastlabel so branch validation succeeds
    parent.lastlabel = parent_lastlabel

    # Restore local table of parent (needed for variable resolution)
    for name, entries in parent_localtable_raw.items():
        from runtime.symtab import LocalAddr
        parent.localtable[name] = [
            LocalAddr(Addr(idx, ig, sym, tpid), nest)
            for (idx, ig, sym, tpid, nest) in entries
        ]

    # Ensure parent has enough children
    child = r._get_child_pc(parent, childid)
    child.head = head

    # Set run events
    r.run_active.set()
    r.run_rev = backward

    try:
        ret = child.run_loop(r, backward)
    except Exception as exc:
        # Close SharedMemory attachment before reporting error
        child_heap.close()
        child_var.close()
        result_q.put({"error": str(exc), "childid": childid})
        return

    # Phase 1: writes are in SharedMemory — no need to send heapmem/varmem back
    child_heap.close()
    child_var.close()
    result_q.put({
        "childid": childid,
        "ret":     ret,
    })


# ---------------------------------------------------------------------------
# RuntimeMP
# ---------------------------------------------------------------------------

class RuntimeMP(Runtime):
    """
    CJanus runtime with optional multiprocessing-based call execution.

    By default (``_mp_enabled = False``) this is identical to Runtime.
    Set ``_mp_enabled = True`` to activate the experimental Process-based
    path for multi-arg call instructions.

    Phase 0: label mismatch / alclimit / pidctr bugs fixed.
    Phase 1: SharedMemory used so child writes propagate to parent.
    Phase 2: Children run with fresh DAGs; reversibility enforced locally.
    """

    #: Set to True to enable experimental multiprocessing path
    _mp_enabled: bool = False

    def execute_call(self, p: PC, arglist: str, backward: bool) -> int:
        procnames = [s.strip() for s in arglist.split(",")]

        if not self._mp_enabled or len(procnames) <= 1 or cfg.sequential:
            # Fall back to threading-based execution (existing behaviour)
            return super().execute_call(p, arglist, backward)

        return self._mp_execute_call(p, procnames, backward)

    # ------------------------------------------------------------------
    # Experimental multiprocessing path
    # ------------------------------------------------------------------

    def _mp_execute_call(self, p: PC, procnames: list[str], backward: bool) -> int:
        """
        Launch each sub-procedure in a separate OS process.

        Each child process receives:
          - A serialisable snapshot of the runtime (Phase 0: with alclimit/pidctr)
          - SharedMemory names for heapmem / varmem (Phase 1)
        Results are collected via a multiprocessing.Queue.
        Memory writes are propagated via SharedMemory (Phase 1).
        """
        snapshot = _snapshot_runtime(self)
        # Phase 0: add parent's lastlabel for branch validation in child
        snapshot["parent_lastlabel"] = p.lastlabel

        # Phase 1: create SharedMemory for heapmem and varmem
        heap_vals  = snapshot["heapmem"]
        var_vals   = snapshot["varmem"]
        # Allocate generous size: current usage + room for new locals in recursion
        heap_size  = max(len(heap_vals), 256) + 64
        var_size   = self._alclimit + 4096

        shm_heap = SharedHeapMemory(
            name="", size=heap_size, create=True, initial=heap_vals
        )
        shm_var  = SharedHeapMemory(
            name="", size=var_size,  create=True, initial=var_vals
        )

        # Serialise parent local table
        parent_localtable_raw: dict = {}
        for name, entries in p.localtable.items():
            parent_localtable_raw[name] = [
                (la.addr.idx, la.addr.is_global, la.addr.sym,
                 la.addr.treepid, la.nest)
                for la in entries
            ]

        ctx = _mp.get_context("spawn")
        result_q = ctx.Queue(maxsize=len(procnames) + 4)

        processes = []
        for i, v in enumerate(procnames):
            head = (
                self.labels.get_end(v) + 1
                if backward
                else self.labels.get_begin(v)
            )
            proc = ctx.Process(
                target=_mp_execute_call_stub,
                args=(
                    snapshot,
                    p.pid,
                    p.originpid,
                    p.nest,
                    parent_localtable_raw,
                    p.lastlabel,          # Phase 0
                    i,
                    head,
                    backward,
                    result_q,
                    shm_heap.name,        # Phase 1
                    heap_size,
                    shm_var.name,
                    var_size,
                ),
                daemon=True,
            )
            proc.start()
            processes.append(proc)

        # Release parent locks while children run (mirrors threading path)
        for f in p.dfunc:
            f()
        p.dfunc = []

        # Collect results
        results = []
        for _ in processes:
            try:
                res = result_q.get(timeout=300)
                results.append(res)
            except Exception:
                pass

        for proc in processes:
            proc.join(timeout=300)

        # Phase 1: sync SharedMemory back into parent's heapmem / varmem
        # (Only necessary if the parent's heapmem/varmem are plain HeapMemory.
        #  We always sync to be safe.)
        for i, v in enumerate(shm_heap.values):
            self.heapmem.set(i, v)
        for i, v in enumerate(shm_var.values):
            self.varmem.set(i, v)

        # Phase 1: cleanup SharedMemory
        shm_heap.close()
        shm_heap.unlink()
        shm_var.close()
        shm_var.unlink()

        # Check for errors
        for res in results:
            if "error" in res:
                raise RuntimeError(
                    f"Child process {res['childid']} failed: {res['error']}"
                )

        # Check if any child was suspended (ret == 0)
        suspended = any(res.get("ret") == 0 for res in results)
        return RET_RESPAWN if suspended else RET_NORMAL
