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
    Proof-of-concept worker function that can be launched as a
    ``multiprocessing.Process``.  It reconstructs a *minimal* runtime
    from serialisable snapshot data (flat integer arrays for heapmem /
    varmem, the parsed instruction list, labels) and runs a single
    child procedure in that fresh interpreter.  DAG dependency tracking
    is skipped (``-nodag`` style).  This is sufficient to demonstrate
    GIL-free execution and measure raw CPU throughput without the DAG
    bottleneck.

    Limitations:
      - Inter-process variable writes are NOT propagated back to the
        parent after the worker returns (the child runs on a copy).
      - DAG annotation is disabled in worker processes.
      - Shared-memory propagation (using multiprocessing.shared_memory)
        is left as future work (see ``README.md``).

Usage (experimental MP path)
    Pass ``--mp`` to ``main_mp.py``.  The RuntimeMP class has
    ``_mp_enabled = False`` by default; set it to True to activate the
    Process-based path for multi-proc calls.
"""
from __future__ import annotations

import multiprocessing as _mp
import queue
import threading
from typing import Optional

from runtime.config import cfg
from runtime.runtime import Runtime
from runtime.pc import PC, RET_NORMAL, RET_RESPAWN


# ---------------------------------------------------------------------------
# Serialisable snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_runtime(r: "Runtime") -> dict:
    """
    Capture the serialisable parts of the runtime for transmission to
    a child process.

    Returns a plain dict that can be pickled.
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
    }


def _mp_execute_call_stub(
    snapshot: dict,
    parent_pid: int,
    parent_originpid: int,
    parent_nest: int,
    parent_localtable_raw: dict,   # {name: [(idx, is_global, sym, treepid, nest)]}
    childid: int,
    head: int,
    backward: bool,
    result_q,                      # multiprocessing.Queue
) -> None:
    """
    Worker function executed in a child process.

    Reconstructs a minimal Runtime from *snapshot* and runs a single
    child procedure starting at *head*.  Results (varmem, heapmem
    deltas) are placed into *result_q*.

    Note: DAG annotation is disabled (cfg.no_dag = True style).
          This is intentional — the full DAG cannot be shared across
          processes without a deeper refactor.
    """
    import sys
    import os

    # Add cjanus_py to sys.path so runtime imports work in child process
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from runtime.config import cfg as child_cfg
    from runtime.instset import init_instset
    from runtime.runtime import Runtime
    from runtime.symtab import Addr
    from runtime.pc import PC, EXR_EOF, EXR_PROC_END

    # Silence output in workers
    child_cfg.suppress_output = True
    child_cfg.no_dag = True

    # Reconstruct runtime from snapshot
    r = Runtime([])
    r.file = snapshot["file"]

    # Restore memory
    for i, v in enumerate(snapshot["heapmem"]):
        r.heapmem.set(i, v)
    for i, v in enumerate(snapshot["varmem"]):
        r.varmem.set(i, v)

    # Restore alloc table
    for name, (idx, is_global, sym, treepid) in snapshot["alloctable"].items():
        r.alloctable[name] = Addr(idx, is_global, sym, treepid)

    # Restore labels
    r.labels._gotomap    = snapshot["labels_goto"]
    r.labels._comemap    = snapshot["labels_comefrom"]
    r.labels._beginmap   = snapshot["labels_begin"]
    r.labels._endmap     = snapshot["labels_end"]
    r.labels._tmplabel   = snapshot["labels_counter"]

    r.instset = init_instset(r)

    # Build parent PC stub
    from runtime.dag import DagPNode
    parent = PC()
    parent.pid = parent_pid
    parent.originpid = parent_originpid
    parent.nest = parent_nest
    parent.pnode = DagPNode(proc=parent, pc=0)

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
        result_q.put({"error": str(exc), "childid": childid})
        return

    result_q.put({
        "childid":  childid,
        "ret":      ret,
        "heapmem":  list(r.heapmem.values),
        "varmem":   list(r.varmem.values),
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

    WARNING: The MP path runs children on a *copy* of the runtime state.
    Variable writes in children are not merged back into the parent.
    This makes it unsuitable for programs that depend on shared-memory
    communication via the DAG.  It is useful for CPU-bound, data-parallel
    workloads where sub-procedures operate on disjoint memory regions.
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

        Each child process receives a serialisable snapshot of the
        runtime.  Results are collected via a multiprocessing.Queue.
        Memory writes from children are NOT merged back (limitation).
        """
        snapshot = _snapshot_runtime(self)

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
                    i,
                    head,
                    backward,
                    result_q,
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

        # Check for errors
        for res in results:
            if "error" in res:
                raise RuntimeError(
                    f"Child process {res['childid']} failed: {res['error']}"
                )

        # Check if any child was suspended (ret == 0)
        suspended = any(res.get("ret") == 0 for res in results)
        return RET_RESPAWN if suspended else RET_NORMAL
