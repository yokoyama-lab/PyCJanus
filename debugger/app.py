"""CJanus Web Debugger — Flask application.

Usage:
    cd /home/a/myclaude/cjanus/cjanus_py
    python3 debugger/app.py [--port PORT] [--file FILE]

    python3 debugger/app.py
    python3 debugger/app.py --port 5001 --file ../CJanus-IPSJPRO25-3/progs/fib.crl
"""
from __future__ import annotations

import argparse
import io
import os
import queue
import sys
import threading
import time

# Ensure the parent directory is on sys.path so we can import the runtime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from flask import Flask, jsonify, render_template, request
except ImportError:
    print("Flask が必要です: pip install flask --break-system-packages")
    sys.exit(1)

from runtime.config import cfg, Config
from runtime.instset import init_instset
from runtime.runtime import Runtime

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

runtime_state: dict = {
    "runtime": None,
    "debugsym": None,
    "done": None,
    "thread": None,
    "output_buffer": [],   # captured stdout lines
    "crl_file": None,
    "crl_lines": [],       # raw source lines (for display)
    "loaded": False,
    "phase": "stopped",    # "forward" | "backward" | "stopped"
    "running": False,      # True while execution thread is active
    "_lock": threading.Lock(),
    "_stdout_override": None,
}

# ---------------------------------------------------------------------------
# stdout capture helper
# ---------------------------------------------------------------------------

class _CapturingWriter(io.TextIOBase):
    """Wraps sys.stdout: writes to the original stdout AND appends to buffer."""

    def __init__(self, buffer: list[str], original):
        self._buf = buffer
        self._orig = original

    def write(self, s: str) -> int:
        if s:
            self._orig.write(s)
            self._orig.flush()
            # Split on newlines and append non-empty lines
            for part in s.split("\n"):
                stripped = part.rstrip()
                if stripped:
                    with runtime_state["_lock"]:
                        self._buf.append(stripped)
        return len(s)

    def flush(self):
        self._orig.flush()


def _install_capture():
    """Replace sys.stdout with the capturing writer."""
    if runtime_state["_stdout_override"] is None:
        cap = _CapturingWriter(runtime_state["output_buffer"], sys.__stdout__)
        sys.stdout = cap
        runtime_state["_stdout_override"] = cap


def _uninstall_capture():
    """Restore original sys.stdout."""
    if runtime_state["_stdout_override"] is not None:
        sys.stdout = sys.__stdout__
        runtime_state["_stdout_override"] = None


# ---------------------------------------------------------------------------
# Runtime lifecycle helpers
# ---------------------------------------------------------------------------

def _reset_cfg():
    """Reset global config to defaults (re-create the singleton fields)."""
    cfg.slow_debug = 0.0
    cfg.seq_dag = False
    cfg.dag_debug = False
    cfg.var_debug = False
    cfg.block_debug = False
    cfg.exec_debug = False
    cfg.sem_debug = False
    cfg.lock_debug = False
    cfg.dag_lock_debug = False
    cfg.timing_debug = True       # enable timing so we see elapsed output
    cfg.sequential = False
    cfg.suppress_output = True    # suppress block-level output for cleaner UI
    cfg.slow_parse = False
    cfg.strict = False
    cfg.no_dag = False
    cfg.ext_expr = False
    cfg.max_procs = 0
    cfg.auto = False
    cfg.auto_progress = 0
    cfg.step_mode = False
    cfg.trace_cli = False


def _load_runtime(filepath: str) -> str | None:
    """Load a CRL file into a fresh Runtime instance.

    Returns None on success, or an error message string on failure.
    """
    filepath = os.path.abspath(filepath)
    if not os.path.isfile(filepath):
        return f"File not found: {filepath}"

    with runtime_state["_lock"]:
        # Stop any existing execution
        _stop_existing()

        # Reset config
        _reset_cfg()

        # Read source
        with open(filepath) as f:
            raw = f.read()
        raw_lines = [line.rstrip("\n") for line in raw.splitlines() if line.strip()]

        # Clear output buffer
        runtime_state["output_buffer"].clear()

        # Install stdout capture
        _install_capture()

        # Build runtime
        try:
            r = Runtime(raw_lines)
            r.instset = init_instset(r)
            r.load_crl()
        except Exception as exc:
            return f"Failed to load CRL: {exc}"

        debugsym: queue.Queue[str] = queue.Queue()
        done = threading.Event()

        debug_thread = threading.Thread(
            target=_debug_wrapper,
            args=(r, debugsym, done),
            daemon=True,
        )

        runtime_state.update(
            runtime=r,
            debugsym=debugsym,
            done=done,
            thread=debug_thread,
            crl_file=filepath,
            crl_lines=raw.splitlines(),
            loaded=True,
            phase="forward",
            running=True,
        )

        debug_thread.start()

    return None  # success


def _debug_wrapper(r: Runtime, debugsym: queue.Queue, done: threading.Event):
    """Runs r.debug() and updates runtime_state when it finishes."""
    try:
        r.debug(debugsym, done)
    finally:
        with runtime_state["_lock"]:
            runtime_state["running"] = False


def _stop_existing():
    """Signal any active runtime to stop (must be called with _lock held)."""
    done: threading.Event | None = runtime_state.get("done")
    debugsym: queue.Queue | None = runtime_state.get("debugsym")
    if done is not None and not done.is_set():
        done.set()
    # Drain the queue and unblock any blocking get()
    if debugsym is not None:
        try:
            while True:
                debugsym.get_nowait()
        except queue.Empty:
            pass


def _send_command(cmd: str) -> dict:
    """Send a command string to the running debugger.

    Returns a dict with {"ok": bool, "error": str|None}.
    """
    with runtime_state["_lock"]:
        if not runtime_state["loaded"]:
            return {"ok": False, "error": "No CRL file loaded"}
        debugsym: queue.Queue = runtime_state["debugsym"]
        done: threading.Event = runtime_state["done"]
        if done.is_set():
            return {"ok": False, "error": "Runtime has stopped"}

        # Update local phase tracking
        if cmd == "fwd":
            runtime_state["phase"] = "forward"
        elif cmd == "bwd":
            runtime_state["phase"] = "backward"
        elif cmd in ("run", "step"):
            runtime_state["running"] = True

        debugsym.put(cmd + "\n")

    return {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/load", methods=["POST"])
def load():
    data = request.get_json(force=True, silent=True) or {}
    filepath = data.get("file", "").strip()
    if not filepath:
        return jsonify({"ok": False, "error": "Missing 'file' parameter"}), 400

    err = _load_runtime(filepath)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    return jsonify({"ok": True, "file": runtime_state["crl_file"]})


@app.route("/command", methods=["POST"])
def command():
    data = request.get_json(force=True, silent=True) or {}
    cmd = data.get("cmd", "").strip()
    arg = data.get("arg", "").strip()

    valid_no_arg = {"run", "fwd", "bwd", "var", "dag", "deldag", "step", "ps", "breaks"}
    valid_with_arg = {"watch", "unwatch", "break", "unbreak", "trace", "focus"}
    if cmd not in valid_no_arg and cmd not in valid_with_arg:
        return jsonify({"ok": False, "error": f"Unknown command: {cmd}"}), 400

    cmd_str = f"{cmd} {arg}".strip() if arg else cmd
    result = _send_command(cmd_str)
    if not result["ok"]:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/state")
def state():
    with runtime_state["_lock"]:
        output_snapshot = list(runtime_state["output_buffer"])

    r: Runtime | None = runtime_state["runtime"]
    # "executing" is True while CJanus worker threads are actually running
    executing = False
    if r is not None and r.run_active.is_set() and not r.run_stop.is_set():
        executing = True

    return jsonify({
        "loaded": runtime_state["loaded"],
        "phase": runtime_state["phase"],
        "running": runtime_state["running"],   # debug loop alive
        "executing": executing,                # CJanus worker threads active
        "output": output_snapshot,
        "crl_file": runtime_state["crl_file"],
    })


@app.route("/vars")
def vars_endpoint():
    """Return variable values as a JSON object.

    Captures the output of print_sym() and parses it.
    """
    if not runtime_state["loaded"]:
        return jsonify({"ok": False, "error": "No CRL file loaded"}), 400

    r: Runtime | None = runtime_state["runtime"]
    if r is None:
        return jsonify({"ok": False, "error": "Runtime not initialised"}), 400

    # Capture print_sym() output into a local buffer
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        r.print_sym()
    finally:
        sys.stdout = old_stdout
        # Also restore capture writer if it was active
        if runtime_state["_stdout_override"] is not None:
            sys.stdout = runtime_state["_stdout_override"]

    raw = buf.getvalue()
    # Also append to global output_buffer so it appears in /state
    with runtime_state["_lock"]:
        for line in raw.splitlines():
            if line.strip():
                runtime_state["output_buffer"].append(line)

    # Parse memory status:
    # ---Memory Status---
    # [0, 0, 55, ...]
    heap_values: list[int] = []
    alloc_table: dict[str, str] = {}

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            # Parse the list representation
            inner = line[1:-1].strip()
            if inner:
                try:
                    heap_values = [int(x.strip()) for x in inner.split(",")]
                except ValueError:
                    pass

    # Also expose the alloctable (variable name → address mapping)
    with runtime_state["_lock"]:
        pass  # just to ensure we have the lock for any read
    for name, addr in r.alloctable.items():
        idx = addr.idx
        is_global = addr.is_global
        if is_global:
            val = r.heapmem.get(idx)
        else:
            val = r.varmem.get(idx)
        alloc_table[name] = {"addr": str(addr), "value": val}

    return jsonify({
        "ok": True,
        "heap": heap_values,
        "vars": alloc_table,
    })


@app.route("/processes")
def processes():
    """Return all live process states as JSON."""
    r: Runtime | None = runtime_state.get("runtime")
    if r is None:
        return jsonify({"ok": False, "error": "No runtime"}), 400
    procs: list = []
    if r.rootpc:
        r.rootpc.propagate(lambda p: procs.append({
            "pid": p.pid,
            "pc": p.pc,
            "head": p.head,
            "executing": p.executing,
            "nest": p.nest,
            "line": r.file[p.head] if 0 <= p.head < len(r.file) else "?",
        }))
    return jsonify({"ok": True, "processes": sorted(procs, key=lambda p: p["pid"])})


@app.route("/breakpoints", methods=["GET", "POST", "DELETE"])
def breakpoints_endpoint():
    """GET: list breakpoints. POST: set breakpoint {target}. DELETE: remove breakpoint {target}."""
    r: Runtime | None = runtime_state.get("runtime")
    if r is None:
        return jsonify({"ok": False, "error": "No runtime"}), 400

    if request.method == "GET":
        bps = []
        for bp in sorted(r.breakpoints):
            ln = r.file[bp] if 0 <= bp < len(r.file) else "?"
            bps.append({"line": bp, "content": ln})
        return jsonify({"ok": True, "breakpoints": bps})

    data = request.get_json(force=True, silent=True) or {}
    target = str(data.get("target", "")).strip()
    if not target:
        return jsonify({"ok": False, "error": "Missing 'target' parameter"}), 400

    if request.method == "POST":
        result = _send_command(f"break {target}")
    else:  # DELETE
        result = _send_command(f"unbreak {target}")
    return jsonify(result)


@app.route("/source")
def source():
    """Return the CRL source lines as JSON."""
    return jsonify({
        "file": runtime_state["crl_file"],
        "lines": runtime_state["crl_lines"],
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CJanus Web Debugger")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--file", default="", help="CRL file to load on startup")
    args = parser.parse_args()

    if args.file:
        err = _load_runtime(args.file)
        if err:
            print(f"Warning: could not load {args.file}: {err}", file=sys.stderr)

    print(f"CJanus Web Debugger running at http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
