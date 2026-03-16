#!/usr/bin/env python3
"""
CJanus Python runtime — entry point.

Usage:
    python main.py <file.crl> [options]

Options:
    -auto           Run forward → backward → replay automatically
    -v=false        Disable verbose output (suppress block output)
    -silent         Suppress block execution output
    -t              Enable timing debug
    -seq            Sequential mode (no threads)
    -procs N        Set max thread count
    -dagdebug       Show DAG debug info
    -vardebug       Show variable read/write debug info
    -lockdebug      Show lock debug info
    -daglockdebug   Show DAG lock debug info
    -blockdebug     Show block debug info
    -execdebug      Enable execution verification
    -strict         Strict basic-block atomicity
    -nodag          Disable annotation DAG
    -extexpr        Allow extended expressions
    -metrics        Collect and display runtime metrics
"""
from __future__ import annotations
import queue
import sys
import threading

from runtime.config import cfg
from runtime.instset import init_instset
from runtime.runtime import Runtime


def parse_args(args: list[str]) -> tuple[str, dict]:
    """Parse command-line arguments. Returns (filename, options)."""
    if not args:
        print(__doc__)
        sys.exit(1)

    filename = args[0]
    i = 1
    while i < len(args):
        a = args[i]
        if a == "-auto":
            cfg.auto = True
        elif a == "-v=false":
            cfg.suppress_output = True
        elif a == "-silent":
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
        elif a == "-metrics":
            cfg.metrics = True
        else:
            print(f"Unknown option: {a}")
            sys.exit(1)
        i += 1

    return filename


def main():
    filename = parse_args(sys.argv[1:])

    # Read .crl file
    with open(filename) as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]

    # Create runtime
    r = Runtime(lines)
    r.instset = init_instset(r)
    r.load_crl()

    # Debug channel and done event
    debugsym: queue.Queue[str] = queue.Queue()
    done = threading.Event()

    # Start debugger in a thread
    debug_thread = threading.Thread(target=r.debug, args=(debugsym, done), daemon=True)
    debug_thread.start()

    # Read user input (stdin)
    if cfg.auto:
        # Auto mode: just wait for completion
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

    if cfg.metrics:
        from runtime.metrics import metrics
        metrics.print_summary()


if __name__ == "__main__":
    main()
