"""
CJanus fib(n) program generator.

Usage:
    python3 tools/gen_fib.py <n> [output_file]

Examples:
    python3 tools/gen_fib.py 12            # generates fib12.crl in current dir
    python3 tools/gen_fib.py 20 /tmp/fib20.crl  # specify output path
    python3 tools/gen_fib.py --batch 5 20  # generate fib5.crl through fib20.crl
    python3 tools/gen_fib.py --scaling     # generate n=5,7,10,12,15,18,20 for scaling experiment
"""

import sys
import re
from pathlib import Path

TEMPLATE_PATH = Path("/home/a/myclaude/cjanus/CJanus-IPSJPRO25-3/progs/fib.crl")
DEFAULT_OUTPUT_DIR = Path("/home/a/myclaude/cjanus/CJanus-IPSJPRO25-3/progs")

SCALING_VALUES = [5, 7, 10, 12, 15, 18, 20]


def load_template() -> str:
    """Load the fib.crl template file."""
    if not TEMPLATE_PATH.exists():
        print(f"Error: template not found: {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)
    return TEMPLATE_PATH.read_text()


def generate(n: int, template: str) -> str:
    """Replace the N in '$tmp0 += 10' with the given n."""
    result = re.sub(r'(\$tmp0 \+= )\d+', rf'\g<1>{n}', template)
    return result


def write_file(content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"Generated: {path}")


def default_output_path(n: int) -> Path:
    return DEFAULT_OUTPUT_DIR / f"fib{n}.crl"


def cmd_single(n: int, output_path: Path | None) -> None:
    template = load_template()
    content = generate(n, template)
    out = output_path if output_path is not None else default_output_path(n)
    write_file(content, out)


def cmd_batch(n_min: int, n_max: int) -> None:
    template = load_template()
    for n in range(n_min, n_max + 1):
        content = generate(n, template)
        write_file(content, default_output_path(n))


def cmd_scaling() -> None:
    template = load_template()
    for n in SCALING_VALUES:
        content = generate(n, template)
        write_file(content, default_output_path(n))


def usage() -> None:
    print(__doc__)
    sys.exit(1)


def main() -> None:
    args = sys.argv[1:]

    if not args:
        usage()

    if args[0] == "--batch":
        if len(args) != 3:
            print("Error: --batch requires n_min and n_max", file=sys.stderr)
            usage()
        try:
            n_min, n_max = int(args[1]), int(args[2])
        except ValueError:
            print("Error: n_min and n_max must be integers", file=sys.stderr)
            usage()
        if n_min > n_max:
            print("Error: n_min must be <= n_max", file=sys.stderr)
            sys.exit(1)
        cmd_batch(n_min, n_max)

    elif args[0] == "--scaling":
        cmd_scaling()

    else:
        try:
            n = int(args[0])
        except ValueError:
            print(f"Error: '{args[0]}' is not a valid integer", file=sys.stderr)
            usage()
        output_path = Path(args[1]) if len(args) >= 2 else None
        cmd_single(n, output_path)


if __name__ == "__main__":
    main()
