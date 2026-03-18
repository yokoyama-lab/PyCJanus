#!/usr/bin/env python3
"""
CJanus Go/Python 比較ベンチマーク
"""
import subprocess
import re
import statistics
import sys
import time
import os

GORUNTIME = "/home/a/myclaude/cjanus/CJanus-IPSJPRO25-3/cjanus-runtime"
PYRUNTIME = "/home/a/myclaude/cjanus/cjanus_py/main.py"
PROGS = "/home/a/myclaude/cjanus/CJanus-IPSJPRO25-3/progs"

ELAPSED_RE = re.compile(r'Elapsed:\s*([0-9.]+)s')


def parse_times(output: str):
    """Extract 3 elapsed times (fwd, bwd, replay) from output."""
    matches = ELAPSED_RE.findall(output)
    if len(matches) >= 3:
        return [float(x) * 1000 for x in matches[:3]]  # → ms
    return None


def run_go(prog: str, procs: int, timeout: int = 120):
    cmd = [
        "bash", "-c",
        f"cat /dev/zero | {GORUNTIME} -silent -v=false -auto -t -procs {procs} '{prog}'"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout + result.stderr
        return parse_times(out)
    except subprocess.TimeoutExpired:
        return None


def run_py(prog: str, timeout: int = 300):
    cmd = ["python3", PYRUNTIME, prog, "-v=false", "-auto", "-t"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout + result.stderr
        return parse_times(out)
    except subprocess.TimeoutExpired:
        return None


def benchmark(name: str, prog: str, go_procs_list: list, py_trials: int = 3,
              go_trials: int = 3, py_timeout: int = 300, go_timeout: int = 120):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    results = {}

    # Go
    for procs in go_procs_list:
        key = f"go_p{procs}"
        times = []
        print(f"  [Go  procs={procs:2d}] ", end="", flush=True)
        for i in range(go_trials):
            t = run_go(prog, procs, go_timeout)
            if t:
                times.append(t)
                print(".", end="", flush=True)
            else:
                print("T", end="", flush=True)
        print()
        results[key] = times

    # Python
    print(f"  [Python      ] ", end="", flush=True)
    py_times = []
    for i in range(py_trials):
        t = run_py(prog, py_timeout)
        if t:
            py_times.append(t)
            print(".", end="", flush=True)
        else:
            print("T", end="", flush=True)
    print()
    results["python"] = py_times

    return results


def median_or_na(times_list, idx):
    vals = [t[idx] for t in times_list if t is not None]
    if not vals:
        return float('nan'), float('nan')
    m = statistics.median(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return m, sd


def fmt_ms(v, sd=None):
    if v != v:  # nan
        return "N/A"
    if v >= 60000:
        return f"{v/1000:.1f}s"
    if v >= 1000:
        return f"{v/1000:.2f}s"
    return f"{v:.1f}ms"


def print_results(name, results, go_procs_list):
    print(f"\n--- {name} ---")
    print(f"{'Implementation':<20} {'Forward':>10} {'Backward':>10} {'Replay':>10}")
    print("-" * 54)

    for procs in go_procs_list:
        key = f"go_p{procs}"
        ts = results.get(key, [])
        fwd, _ = median_or_na(ts, 0)
        bwd, _ = median_or_na(ts, 1)
        rep, _ = median_or_na(ts, 2)
        label = f"Go (procs={procs})"
        print(f"  {label:<18} {fmt_ms(fwd):>10} {fmt_ms(bwd):>10} {fmt_ms(rep):>10}")

    ts = results.get("python", [])
    fwd, _ = median_or_na(ts, 0)
    bwd, _ = median_or_na(ts, 1)
    rep, _ = median_or_na(ts, 2)
    print(f"  {'Python':<18} {fmt_ms(fwd):>10} {fmt_ms(bwd):>10} {fmt_ms(rep):>10}")

    # Slowdown ratio vs Go p1
    go_ts = results.get("go_p1", [])
    if go_ts and ts:
        gf, _ = median_or_na(go_ts, 0)
        pf, _ = median_or_na(ts, 0)
        if gf > 0 and pf == pf:
            print(f"  Python/Go(p=1) ratio: {pf/gf:.1f}x")

    return results


def main():
    all_results = {}

    programs = [
        ("fib(10)",   f"{PROGS}/fib.crl",   [1, 2, 4, 8, 16], 3, 3, 60,  30),
        ("sfib(10)",  f"{PROGS}/sfib.crl",  [1],               3, 3, 120, 30),
        ("sieve(100)",f"{PROGS}/sieve.crl", [1, 2, 4, 8, 16], 3, 3, 120, 60),
        ("fib(15)",   f"{PROGS}/fib15.crl", [1, 2, 4],         1, 3, 300, 60),
    ]

    for name, prog, go_procs, py_trials, go_trials, py_timeout, go_timeout in programs:
        r = benchmark(name, prog, go_procs, py_trials, go_trials, py_timeout, go_timeout)
        all_results[name] = (r, go_procs)
        print_results(name, r, go_procs)

    # Summary table
    print("\n" + "="*70)
    print("  SUMMARY: Forward execution time (median)")
    print("="*70)
    print(f"{'Program':<14} {'Go p=1':>10} {'Go p=4':>10} {'Go p=16':>10} {'Python':>10} {'Ratio(py/go)':>14}")
    print("-"*70)
    for name, prog, go_procs, *_ in programs:
        r, gp = all_results[name]
        g1   = median_or_na(r.get("go_p1", []), 0)[0]
        g4   = median_or_na(r.get("go_p4", []), 0)[0]
        g16  = median_or_na(r.get("go_p16", []), 0)[0]
        py   = median_or_na(r.get("python", []), 0)[0]
        ratio = f"{py/g1:.0f}x" if g1 > 0 and py == py else "N/A"
        print(f"  {name:<12} {fmt_ms(g1):>10} {fmt_ms(g4):>10} {fmt_ms(g16):>10} {fmt_ms(py):>10} {ratio:>14}")

    # Save raw data
    import json
    save = {}
    for name, (r, gp) in all_results.items():
        save[name] = {k: v for k, v in r.items()}
    with open("/home/a/myclaude/cjanus/cjanus_py/benchmark_results.json", "w") as f:
        json.dump(save, f, indent=2)
    print("\n生データ: benchmark_results.json に保存済み")


if __name__ == "__main__":
    main()
