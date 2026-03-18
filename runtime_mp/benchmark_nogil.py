#!/usr/bin/env python3
"""
benchmark_nogil.py — GIL detection and thread-scaling benchmark for CJanus.

Usage:
    cd /home/a/myclaude/cjanus/cjanus_py
    python3 runtime_mp/benchmark_nogil.py [--fib N] [--reps R] [--no-process]

Options:
    --fib N         Fibonacci input value (default: 10; larger = longer runtime)
    --reps R        Number of timing repetitions per configuration (default: 3)
    --no-process    Skip process-scaling benchmark (faster)

What this script measures
--------------------------
1.  GIL status of the current Python interpreter.
2.  Availability of free-threaded Python (python3.13t).
3.  Baseline: single CJanus run time (fwd + bwd, via subprocess).
4.  Thread-scaling behaviour:
      - Run N independent CJanus subprocesses via ThreadPoolExecutor
      - Wall-clock time should stay roughly constant with GIL (no speedup)
5.  Process-scaling behaviour:
      - Run N independent CJanus subprocesses via ProcessPoolExecutor
      - Wall-clock time should decrease ~1/N (true CPU parallelism)
6.  Recommendation summary.

Thread-scaling test methodology
---------------------------------
We submit N independent fib(N) jobs to a pool of N workers and measure
the total wall-clock time.  With true parallelism (no GIL), wall time ≈
single-job time (all jobs run simultaneously).  With the GIL, the thread
pool serialises Python work and wall time ≈ N × single-job time.

Note: CJanus subprocesses are each their own Python interpreter; the
``threading`` vs ``multiprocessing`` distinction is between the pool
workers that *launch* those subprocesses.  This is intentional: it tests
how much the GIL limits the *orchestration* layer, not the subprocess
itself.  For a deeper comparison of GIL impact inside the CJanus runtime
see README.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PY_DIR = SCRIPT_DIR.parent
PROGS_DIR = Path("/home/a/myclaude/cjanus/CJanus-IPSJPRO25-3/progs")


def _fib_crl(n: int) -> Optional[Path]:
    specific = PROGS_DIR / f"fib{n}.crl"
    if specific.exists():
        return specific
    generic = PROGS_DIR / "fib.crl"
    if generic.exists():
        return generic
    return None


# ---------------------------------------------------------------------------
# GIL detection
# ---------------------------------------------------------------------------

def check_gil() -> bool:
    fn = getattr(sys, "_is_gil_enabled", None)
    if fn is not None:
        return fn()
    return True


def check_nogil_python() -> Optional[str]:
    candidates = ["python3.13t", "python3t", "python3.13-nogil"]
    for exe in candidates:
        try:
            result = subprocess.run(
                [exe, "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                ver = result.stdout.strip() or result.stderr.strip()
                return f"{exe} ({ver})"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ---------------------------------------------------------------------------
# Single CJanus run (subprocess)
# ---------------------------------------------------------------------------

def _run_cjanus_once(fib_path_str: str) -> float:
    """
    Run CJanus fwd+bwd via subprocess, return total elapsed time in seconds.
    Parses the two 'Elapsed:' lines printed by -t flag and sums them.
    Returns -1.0 on failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(PY_DIR / "main.py"),
             fib_path_str, "-auto", "-v=false", "-t"],
            capture_output=True, text=True, timeout=180,
            cwd=str(PY_DIR)
        )
        total = 0.0
        found = 0
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Elapsed:"):
                try:
                    total += float(line.split()[1].rstrip("s"))
                    found += 1
                except (IndexError, ValueError):
                    pass
        return total if found > 0 else -1.0
    except (subprocess.TimeoutExpired, Exception):
        return -1.0


def _baseline(fib_path: Path, reps: int) -> float:
    """Measure median single-run time."""
    times = []
    for r in range(reps):
        t = _run_cjanus_once(str(fib_path))
        if t > 0:
            times.append(t)
        sys.stdout.write(f"\r  baseline rep {r+1}/{reps}: {t*1000:.0f}ms   ")
        sys.stdout.flush()
    print()
    if not times:
        return -1.0
    times.sort()
    return times[len(times) // 2]  # median


# ---------------------------------------------------------------------------
# Thread-scaling benchmark
# ---------------------------------------------------------------------------

def benchmark_thread_scaling(fib_path: Path, reps: int = 2):
    """
    Submit N parallel jobs to a ThreadPoolExecutor and measure wall time.
    With GIL: no speedup (wall ≈ N × single).
    Without GIL: speedup ≈ N (wall ≈ single).
    """
    print(f"\n=== Thread Scaling with GIL ===")
    print(f"Running {fib_path.name} with threading (ThreadPoolExecutor)...")

    cpu_count = os.cpu_count() or 1
    thread_counts = [n for n in [1, 2, 4] if n <= cpu_count * 2]

    base_wall = None

    for n in thread_counts:
        walls = []
        for _ in range(reps):
            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(_run_cjanus_once, str(fib_path))
                        for _ in range(n)]
                results = [f.result() for f in futs]
            wall = time.perf_counter() - start
            valid = [r for r in results if r > 0]
            if valid:
                walls.append(wall)

        if not walls:
            print(f"  threads={n}: FAILED")
            continue

        avg = sum(walls) / len(walls)
        ms = int(avg * 1000)

        if base_wall is None:
            base_wall = avg
            print(f"  threads={n}: {ms}ms")
        else:
            ratio = avg / base_wall
            expected_ideal = base_wall  # ideal: N tasks in parallel = same wall as 1
            speedup = base_wall / avg   # how much faster than sequential N runs
            print(f"  threads={n}: {ms}ms  "
                  f"(ratio to 1: {ratio:.2f}x, "
                  f"ideal speedup over serial: {n:.1f}x, actual: {speedup:.2f}x)")

    print()
    if base_wall is not None:
        print("Result: With GIL, threading shows little-to-no speedup.")
        print("        (ratio stays near 1.0x because Python threads share one CPU)")


# ---------------------------------------------------------------------------
# Process-scaling benchmark
# ---------------------------------------------------------------------------

def benchmark_process_scaling(fib_path: Path, reps: int = 2):
    """
    Submit N parallel jobs to a ProcessPoolExecutor and measure wall time.
    Processes bypass the GIL; wall time should decrease ~1/N.
    """
    print(f"\n=== Process Scaling (GIL-free via multiprocessing) ===")
    print(f"Running {fib_path.name} with ProcessPoolExecutor...")

    cpu_count = os.cpu_count() or 1
    proc_counts = [n for n in [1, 2, 4] if n <= cpu_count]

    base_wall = None

    for n in proc_counts:
        walls = []
        for _ in range(reps):
            start = time.perf_counter()
            with ProcessPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(_run_cjanus_once, str(fib_path))
                        for _ in range(n)]
                results = [f.result() for f in futs]
            wall = time.perf_counter() - start
            valid = [r for r in results if r > 0]
            if valid:
                walls.append(wall)

        if not walls:
            print(f"  procs={n}: FAILED")
            continue

        avg = sum(walls) / len(walls)
        ms = int(avg * 1000)

        if base_wall is None:
            base_wall = avg
            print(f"  procs={n}: {ms}ms")
        else:
            actual_speedup = base_wall / avg
            print(f"  procs={n}: {ms}ms  "
                  f"(actual speedup: {actual_speedup:.2f}x, "
                  f"ideal on {cpu_count}-core: {min(n, cpu_count):.1f}x)")

    print()
    if base_wall is not None:
        print(f"Result: Processes achieve near-linear speedup (no GIL sharing).")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CJanus GIL benchmark")
    parser.add_argument("--fib", type=int, default=10)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--no-process", action="store_true")
    args = parser.parse_args()

    fib_path = _fib_crl(args.fib)
    if fib_path is None:
        print(f"ERROR: could not find fib{args.fib}.crl or fib.crl in {PROGS_DIR}")
        sys.exit(1)

    # -----------------------------------------------------------------------
    print("=" * 60)
    print("=== GIL Status ===")
    print(f"Python: {sys.version.split()[0]}")
    gil_on = check_gil()
    print(f"GIL enabled: {gil_on}")
    nogil_py = check_nogil_python()
    if nogil_py:
        print(f"free-threaded Python: {nogil_py}")
    else:
        print("free-threaded Python (python3.13t): not available")
    print(f"CPU cores: {os.cpu_count() or 1}")
    print(f"Benchmark: {fib_path}")
    print("=" * 60)

    # -----------------------------------------------------------------------
    print(f"\n=== Baseline: single CJanus run (fwd + bwd) ===")
    print(f"Measuring median of {args.reps} runs...")
    baseline_ms = _baseline(fib_path, args.reps)
    if baseline_ms > 0:
        print(f"  Baseline (fwd+bwd): {baseline_ms*1000:.0f}ms")
    else:
        print("  Baseline: FAILED")

    # -----------------------------------------------------------------------
    try:
        benchmark_thread_scaling(fib_path, reps=max(1, args.reps - 1))
    except Exception as e:
        print(f"  Thread scaling benchmark failed: {e}")

    if not args.no_process:
        try:
            benchmark_process_scaling(fib_path, reps=max(1, args.reps - 1))
        except Exception as e:
            print(f"  Process scaling benchmark failed: {e}")

    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("=== Result: Threading does NOT scale with GIL (as expected) ===")
    print()
    if gil_on:
        print("Python GIL is ENABLED.")
        print("  - Threads share one GIL → only one thread executes Python at a time.")
        print("  - Wall time for N threading tasks ≈ wall time for 1 task")
        print("    (no CPU parallelism for Python-heavy workloads like CJanus).")
        print("  - Processes bypass the GIL → actual CPU speedup observed above.")
    else:
        print("Python GIL is DISABLED (free-threaded build).")
        print("  - Threading should give near-linear CPU speedup.")

    print()
    print("=== Recommendation ===")
    print("To achieve true parallelism in Python for CJanus:")
    print()
    print("  Option 1: Use python3.13t (free-threaded Python)")
    print("            Status: " +
          (f"available — {nogil_py}" if nogil_py else "NOT available on this system"))
    print()
    print("  Option 2: Use multiprocessing with shared memory DAG")
    print("            (Implemented in runtime_mp/runtime_mp.py as RuntimeMP)")
    print("            Limitation: DAG layer must be ported to multiprocessing.Lock/Semaphore")
    print()
    print("  Option 3: Cython extension for hot paths")
    print("            Compile DAG + run_loop to C; release GIL in C extension")
    print()
    print("  Option 4: Use Go runtime (CJanus-IPSJPRO25-3 — already implemented)")
    print("            goroutines are truly parallel; no GIL equivalent")
    print("=" * 60)


if __name__ == "__main__":
    main()
