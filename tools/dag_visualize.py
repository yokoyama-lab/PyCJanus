#!/usr/bin/env python3
"""
CJanus Annotation DAG Visualizer

Converts a Runtime.dag (annotation DAG) to Graphviz DOT format and optionally
renders it to PNG.

Usage (standalone):
    python3 tools/dag_visualize.py <file.crl> [--output dag.dot] [--render]

API usage:
    from tools.dag_visualize import dump_dag
    dump_dag(runtime, output_path="my_dag.dot", render=False)
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow importing runtime modules when run as a script
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_CJANUS_PY_DIR = os.path.dirname(_TOOLS_DIR)
if _CJANUS_PY_DIR not in sys.path:
    sys.path.insert(0, _CJANUS_PY_DIR)

from runtime.dag import DAG, DagEntry, DagWNode, DagRNode, DagPNode, MAddr

# Maximum number of nodes to emit (prevents unbounded DOT files)
DEFAULT_NODE_LIMIT = 200


# ---------------------------------------------------------------------------
# Unique node ID helpers
# ---------------------------------------------------------------------------

def _wnode_id(addr_key: MAddr, wnode: DagWNode) -> str:
    """Stable string id for a write node."""
    return f"w_{addr_key.is_global}_{addr_key.idx}_{id(wnode)}"


def _rnode_id(rnode: DagRNode) -> str:
    """Stable string id for a read node."""
    return f"r_{id(rnode)}"


def _bot_id(addr_key: MAddr) -> str:
    """Stable string id for a BOT (bottom-of-chain) node."""
    kind = "M" if addr_key.is_global else "V"
    return f"bot_{kind}_{addr_key.idx}"


def _var_label(addr_key: MAddr) -> str:
    kind = "M" if addr_key.is_global else "V"
    return f"{kind}[{addr_key.idx}]"


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def dag_to_dot(dag: DAG, title: str = "CJanus Annotation DAG",
               node_limit: int = DEFAULT_NODE_LIMIT) -> str:
    """
    Convert a Runtime.dag instance to a Graphviz DOT string.

    Parameters
    ----------
    dag         : the DAG instance from Runtime.dag
    title       : graph title shown as the label
    node_limit  : maximum number of graph nodes to emit (0 = unlimited)

    Returns
    -------
    DOT-format string suitable for writing to a .dot file.
    """
    lines: list[str] = []
    lines.append(f'digraph dag {{')
    lines.append(f'  label="{title}";')
    lines.append('  labelloc=t;')
    lines.append('  fontname="Helvetica";')
    lines.append('  node [fontname="Helvetica" fontsize=10];')
    lines.append('  edge [fontname="Helvetica" fontsize=9];')
    lines.append('')

    # Sort entries: globals first (M[*]), then locals (V[*]), by index
    sorted_keys = sorted(dag.entries.keys(),
                         key=lambda k: (0 if k.is_global else 1, k.idx))

    node_count = 0
    truncated = False

    stats_vars = 0
    stats_wnodes = 0
    stats_rnodes = 0
    stats_max_depth = 0
    stats_max_depth_var = ""

    for addr_key in sorted_keys:
        if truncated:
            break

        entry: DagEntry = dag.entries[addr_key]
        var_lbl = _var_label(addr_key)
        stats_vars += 1

        # Walk chain from the beginning (head) to the tail
        # The chain is a doubly-linked list of DagWNode via .prev/.next
        # entry.tail points to the most-recently-added write node (the tail).
        # We walk backwards to find the head (the BOT node, which has node=None).
        tail = entry.tail
        # Walk to the chain head (BOT)
        head_wnode = tail
        while head_wnode.prev is not None:
            head_wnode = head_wnode.prev

        # head_wnode is now the BOT node (node == None)
        # Emit per-variable subgraph for clarity
        lines.append(f'  // Variable {var_lbl}')
        lines.append(f'  subgraph cluster_{_bot_id(addr_key)} {{')
        lines.append(f'    label="{var_lbl}";')
        lines.append('    style=dashed;')
        lines.append('    color=gray;')

        chain_depth = 0
        current = head_wnode
        prev_node_id: str | None = None

        while current is not None:
            if node_limit > 0 and node_count >= node_limit:
                truncated = True
                break

            is_bot = (current.node is None)

            if is_bot:
                nid = _bot_id(addr_key)
                node_label = f"BOT\\n{var_lbl}"
                lines.append(
                    f'    {nid} [label="{node_label}" shape=diamond '
                    f'style=filled fillcolor=lightgray fontsize=9];'
                )
            else:
                pnode: DagPNode = current.node
                pid = pnode.proc.pid
                pc = pnode.pc
                nid = _wnode_id(addr_key, current)
                node_label = f"W({pid},{pc})"
                lines.append(
                    f'    {nid} [label="{node_label}" shape=ellipse '
                    f'style=filled fillcolor=lightblue fontsize=10];'
                )
                stats_wnodes += 1

            node_count += 1
            chain_depth += 1

            # Edge from previous write node to this one
            if prev_node_id is not None:
                lines.append(
                    f'    {prev_node_id} -> {nid} '
                    f'[label="{var_lbl}" style=solid];'
                )

            # Emit read nodes attached to this write node
            rnode = current.read
            while rnode is not None:
                if node_limit > 0 and node_count >= node_limit:
                    truncated = True
                    break
                if rnode.node is not None:
                    rpnode: DagPNode = rnode.node
                    rpid = rpnode.proc.pid
                    rpc = rpnode.pc
                    rid = _rnode_id(rnode)
                    rlabel = f"R({rpid},{rpc})"
                    lines.append(
                        f'    {rid} [label="{rlabel}" shape=box '
                        f'style=filled fillcolor=lightgreen fontsize=10];'
                    )
                    lines.append(
                        f'    {nid} -> {rid} [style=dashed arrowhead=open];'
                    )
                    node_count += 1
                    stats_rnodes += 1
                rnode = rnode.next

            prev_node_id = nid
            current = current.next

        lines.append('  }')
        lines.append('')

        if chain_depth > stats_max_depth:
            stats_max_depth = chain_depth
            stats_max_depth_var = var_lbl

    if truncated:
        lines.append(
            f'  _truncated [label="(truncated: node limit {node_limit} reached)" '
            f'shape=note style=filled fillcolor=yellow];'
        )
        lines.append('')

    lines.append('}')
    lines.append('')

    dot_str = '\n'.join(lines)

    # Attach summary as a comment at the top
    summary_lines = [
        "// DAG Summary:",
        f"//   Variables tracked: {stats_vars}",
        f"//   Total write nodes: {stats_wnodes}",
        f"//   Total read nodes:  {stats_rnodes}",
    ]
    if stats_max_depth_var:
        summary_lines.append(
            f"//   Max chain depth: {stats_max_depth} ({stats_max_depth_var})"
        )
    summary_lines.append("//")
    summary_lines.append("")

    return '\n'.join(summary_lines) + dot_str


# ---------------------------------------------------------------------------
# Summary printer (to stdout)
# ---------------------------------------------------------------------------

def print_summary(dag: DAG, output_path: str, dot_str: str) -> None:
    """Print DAG statistics to stdout."""
    # Parse stats from the embedded comment header
    stats: dict[str, str] = {}
    for line in dot_str.splitlines():
        if line.startswith("// ") and ":" in line:
            key, _, val = line[3:].partition(":")
            stats[key.strip()] = val.strip()

    print("DAG Summary:")
    for key in ["Variables tracked", "Total write nodes", "Total read nodes",
                "Max chain depth"]:
        if key in stats:
            print(f"  {key}: {stats[key]}")

    byte_size = len(dot_str.encode("utf-8"))
    print(f"  Output: {output_path} ({byte_size} bytes)")


# ---------------------------------------------------------------------------
# dump_dag — API for use from other scripts
# ---------------------------------------------------------------------------

def dump_dag(runtime, output_path: str = "dag_output.dot",
             render: bool = False, node_limit: int = DEFAULT_NODE_LIMIT) -> None:
    """
    Save the annotation DAG of an executed Runtime instance to a DOT file.

    Parameters
    ----------
    runtime     : an executed Runtime instance (must have .dag attribute)
    output_path : destination .dot file path
    render      : if True and graphviz is installed, also write a PNG file
    node_limit  : maximum number of graph nodes (0 = unlimited)

    Example
    -------
    from tools.dag_visualize import dump_dag
    dump_dag(my_runtime, output_path="result.dot", render=True)
    """
    dot_str = dag_to_dot(runtime.dag, node_limit=node_limit)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(dot_str)

    print_summary(runtime.dag, output_path, dot_str)

    if render:
        _try_render(output_path)


def _try_render(dot_path: str) -> None:
    """Attempt to render the DOT file to PNG using the graphviz package."""
    try:
        import graphviz  # type: ignore
        base, _ = os.path.splitext(dot_path)
        src = graphviz.Source.from_file(dot_path)
        out = src.render(filename=base, format="png", cleanup=False)
        print(f"  PNG rendered: {out}")
    except ImportError:
        print("  (graphviz Python package not available — skipping PNG render)")
    except Exception as exc:
        print(f"  (PNG render failed: {exc})")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Visualize CJanus Annotation DAG as Graphviz DOT"
    )
    parser.add_argument("crl_file", help="Path to the .crl source file")
    parser.add_argument(
        "--output", default="dag_output.dot",
        help="Output DOT file path (default: dag_output.dot)"
    )
    parser.add_argument(
        "--render", action="store_true",
        help="Also generate PNG (requires graphviz package)"
    )
    parser.add_argument(
        "--node-limit", type=int, default=DEFAULT_NODE_LIMIT,
        help=f"Maximum number of graph nodes (default: {DEFAULT_NODE_LIMIT}, 0=unlimited)"
    )
    parser.add_argument(
        "--silent", action="store_true",
        help="Suppress CJanus runtime output during execution"
    )
    args = parser.parse_args()

    # Read .crl file
    crl_path = os.path.abspath(args.crl_file)
    if not os.path.isfile(crl_path):
        print(f"Error: file not found: {crl_path}", file=sys.stderr)
        sys.exit(1)

    with open(crl_path, encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]

    # Import runtime components
    from runtime.config import cfg
    from runtime.instset import init_instset
    from runtime.runtime import Runtime

    # Suppress runtime output during DAG capture
    cfg.suppress_output = True

    import queue

    rt = Runtime(lines)
    rt.instset = init_instset(rt)
    rt.load_crl()

    # Run forward execution and wait for completion.
    # We must drain the newproc/termproc queues (maxsize=16) continuously,
    # otherwise the process threads will block when trying to enqueue notifications.
    import time as _time

    rt._start_execution(False)

    deadline = _time.monotonic() + 120.0
    while not rt.run_stop.is_set():
        if _time.monotonic() > deadline:
            print("Warning: execution did not complete within 120 seconds", file=sys.stderr)
            break
        # Drain notification queues to prevent blocking
        try:
            rt.newproc.get_nowait()
        except queue.Empty:
            pass
        try:
            rt.termproc.get_nowait()
        except queue.Empty:
            pass
        _time.sleep(1e-4)

    # Final drain
    while True:
        try:
            rt.newproc.get_nowait()
        except queue.Empty:
            break
    while True:
        try:
            rt.termproc.get_nowait()
        except queue.Empty:
            break

    # Generate DOT
    output_path = os.path.abspath(args.output)
    dot_str = dag_to_dot(rt.dag, title=f"CJanus DAG — {os.path.basename(crl_path)}",
                         node_limit=args.node_limit)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(dot_str)

    print_summary(rt.dag, output_path, dot_str)

    if args.render:
        _try_render(output_path)


if __name__ == "__main__":
    main()
