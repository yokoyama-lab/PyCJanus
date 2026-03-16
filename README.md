# PyCJanus

A Python implementation of **Concurrent Janus** (CJanus) — a concurrent, reversible imperative programming language.

CJanus is a concurrent extension of the reversible language Janus.
This repository provides a Python port of the Go reference runtime, together with analysis tools.

## Features

- **Full CRL interpreter** — parses and executes CRL (Concurrent Reversible Language) intermediate programs
- **Three execution modes** — Forward, Backward (reversible undo), and Replay (deterministic re-execution)
- **Annotation DAG** — tracks execution history to enable correct reversal of concurrent programs
- **Distributed locking** — `lock`/`unlock` semaphore instructions for shared-variable concurrency
- **`-metrics` flag** — per-phase block counts, retry counts, peak thread count, and wait times
- **Web debugger** — browser-based step debugger (`debugger/app.py`)
- **Static linter** — structural checks for CRL programs (`tools/linter.py`)
- **DAG visualizer** — exports the Annotation DAG to Graphviz DOT format (`tools/dag_visualize.py`)

## Requirements

- Python 3.10 or later
- [lark](https://github.com/lark-parser/lark) (`pip install lark`)
- Flask (only for the web debugger — `pip install flask`)

```
pip install -r requirements.txt
```

## Quick start

CRL programs are the intermediate language of CJanus.
Sample programs are available in the [CJanus-IPSJPRO25-3](https://github.com/yokoyama-lab/CJanus-IPSJPRO25-3) repository.

```bash
# forward execution
python3 main.py path/to/fib.crl

# forward + backward + replay (auto mode), suppress verbose output, show timing
python3 main.py path/to/fib.crl -v=false -auto -t

# collect execution metrics
python3 main.py path/to/fib.crl -v=false -auto -t -metrics
```

## Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `-v=false` | verbose on | Suppress per-block output |
| `-auto` | off | Run forward → backward → replay automatically |
| `-t` | off | Print elapsed time for each phase |
| `-metrics` | off | Print block/retry/thread statistics |
| `-procs N` | 1 | Number of worker threads (note: Python GIL limits true parallelism) |
| `-execdebug` | off | Verify that backward execution exactly reverses forward |
| `-silent` | off | Suppress block execution output |

## Repository layout

```
PyCJanus/
├── main.py                  # entry point
├── runtime/
│   ├── runtime.py           # top-level runtime, CRL loader, phase controller
│   ├── pc.py                # program counter and block execution
│   ├── dag.py               # Annotation DAG (write/read nodes, locking)
│   ├── instset.py           # CRL instruction set
│   ├── symtab.py            # symbol table
│   ├── label.py             # label resolution
│   ├── expr.py              # expression evaluator
│   ├── config.py            # runtime configuration flags
│   └── metrics.py           # execution metrics collector
├── tools/
│   ├── linter.py            # CRL static linter
│   ├── dag_visualize.py     # Annotation DAG → Graphviz DOT exporter
│   └── gen_fib.py           # fib(n) CRL program generator
├── debugger/
│   ├── app.py               # Flask web debugger
│   └── templates/index.html # debugger UI
├── runtime_mp/
│   ├── runtime_mp.py        # multiprocessing variant (experimental)
│   └── benchmark_nogil.py   # GIL / no-GIL scaling benchmark
└── benchmark_py.sh          # Python benchmark script
```

## Web debugger

```bash
pip install flask
python3 debugger/app.py --port 5000 --file path/to/program.crl
# open http://localhost:5000
```

## Tools

### CRL linter

```bash
python3 tools/linter.py path/to/program.crl
```

Checks begin/end pairs, goto/comefrom symmetry, call target existence, push/pop balance, lock/unlock balance, and set/unset balance.

### DAG visualizer

```python
from tools.dag_visualize import dump_dag
# after executing a runtime instance `rt`:
dump_dag(rt, "output.dot")
# render: dot -Tsvg output.dot -o dag.svg
```

### fib(n) generator

```bash
# generate fib12.crl
python3 tools/gen_fib.py 12

# generate scaling set: n = 5, 7, 10, 12, 15, 18, 20
python3 tools/gen_fib.py --scaling
```

Set the environment variable `CJANUS_PROGS` to point to a directory containing `fib.crl` as the template:

```bash
export CJANUS_PROGS=/path/to/CJanus-IPSJPRO25-3/progs
python3 tools/gen_fib.py --scaling
```

## Background

**Janus** is a reversible imperative programming language.
**CJanus** extends Janus with concurrent procedure calls (`call p, q`) and distributed locking,
enabling both forward and backward execution of concurrent programs via an Annotation DAG.

This Python port was developed for research and educational purposes at the
[Yokoyama Laboratory](https://yokoyama-lab.org/), Nanzan University.

## License

MIT License. See [LICENSE](LICENSE) for details.
