#!/usr/bin/env python3
"""
main_mp.py — CJanus multiprocessing runtime entry point.

Usage:
    cd /path/to/PyCJanus
    python3 runtime_mp/main_mp.py <file.crl> [options]

Options (same as main.py plus):
    --mp            Enable experimental multiprocessing path for call instructions
                    (default: off; falls back to threading)
    -auto           Run forward → backward → replay automatically
    -v=false        Disable verbose output
    -silent         Suppress block execution output
    -t              Enable timing debug
    -seq            Sequential mode (no threads)
    -procs N        Set max thread count

Notes
-----
With ``--mp`` the RuntimeMP class overrides execute_call to launch each
sub-procedure as a separate OS process (bypassing the GIL).  See
runtime_mp/runtime_mp.py for limitations (shared state not propagated back).

Without ``--mp`` this is functionally identical to main.py.
"""
from __future__ import annotations

import multiprocessing
import queue
import sys
import threading
from pathlib import Path

# Ensure cjanus_py is on sys.path when invoked as a script
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from runtime.config import cfg
from runtime.instset import init_instset
from runtime_mp.runtime_mp import RuntimeMP


def parse_args(args: list[str]) -> tuple[str, bool]:
    """Parse command-line arguments. Returns (filename, mp_enabled)."""
    if not args:
        print(__doc__)
        sys.exit(1)

    filename = args[0]
    mp_enabled = False
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--mp":
            mp_enabled = True
        elif a == "-auto":
            cfg.auto = True
        elif a in ("-v=false", "-silent"):
            cfg.suppress_output = True
        elif a == "-t":
            cfg.timing_debug = True
        elif a == "-seq":
            cfg.sequential = True
        elif a == "-procs" and i + 1 < len(args):
            i += 1
            cfg.max_procs = int(args[i])
        elif a == "-dagdebug":
            cfg.dag_debug = True
        elif a == "-vardebug":
            cfg.var_debug = True
        elif a == "-lockdebug":
            cfg.lock_debug = True
        elif a == "-daglockdebug":
            cfg.dag_lock_debug = True
        elif a == "-blockdebug":
            cfg.block_debug = True
        elif a == "-execdebug":
            cfg.exec_debug = True
        elif a == "-strict":
            cfg.strict = True
        elif a == "-nodag":
            cfg.no_dag = True
        elif a == "-extexpr":
            cfg.ext_expr = True
        else:
            print(f"Unknown option: {a}")
            sys.exit(1)
        i += 1

    return filename, mp_enabled


def main():
    filename, mp_enabled = parse_args(sys.argv[1:])

    with open(filename) as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]

    r = RuntimeMP(lines)
    r._mp_enabled = mp_enabled
    r.instset = init_instset(r)
    r.load_crl()

    if mp_enabled:
        print("[runtime_mp] Multiprocessing mode ENABLED for call instructions.")
        print("[runtime_mp] WARNING: child processes run on a copy of the state;")
        print("[runtime_mp] variable writes are not propagated back to the parent.")
    else:
        print("[runtime_mp] Using threading mode (pass --mp to enable multiprocessing).")

    debugsym: queue.Queue[str] = queue.Queue()
    done = threading.Event()

    debug_thread = threading.Thread(target=r.debug, args=(debugsym, done), daemon=True)
    debug_thread.start()

    if cfg.auto:
        debug_thread.join()
    else:
        try:
            for line in sys.stdin:
                if done.is_set():
                    break
                debugsym.put(line)
        except (EOFError, KeyboardInterrupt):
            pass
        done.set()


if __name__ == "__main__":
    # Required for multiprocessing on some platforms
    multiprocessing.set_start_method("spawn", force=True)
    main()
