#!/bin/bash
# Python版の計測スクリプト
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
PYRUNTIME="python3 main.py"
PROGS="${CJANUS_PROGS:-../CJanus-IPSJPRO25-3/progs}"
TRIALS=3

median_of() {
    python3 -c "
import statistics, sys
vals = [float(x) for x in sys.argv[1:] if x != 'NA']
if vals:
    print(f'{statistics.median(vals):.3f}')
else:
    print('NA')
" "$@"
}

run_one() {
    local prog=$1 tmout=${2:-120}
    local out
    out=$(timeout "$tmout" $PYRUNTIME "$prog" -v=false -auto -t 2>&1) || { echo "TIMEOUT"; return; }
    mapfile -t elapsed < <(echo "$out" | grep -oP 'Elapsed: \K[0-9.]+s' | sed 's/s//')
    if [ ${#elapsed[@]} -ge 3 ]; then
        f=$(python3 -c "print(f'{float(\"${elapsed[0]}\")*1000:.3f}')")
        b=$(python3 -c "print(f'{float(\"${elapsed[1]}\")*1000:.3f}')")
        r=$(python3 -c "print(f'{float(\"${elapsed[2]}\")*1000:.3f}')")
        echo "$f $b $r"
    else
        echo "TIMEOUT"
    fi
}

bench() {
    local name=$1 prog=$2 tmout=${3:-120}
    local trials=${4:-$TRIALS}
    echo "=== $name ==="
    local fwd_vals=() bwd_vals=() rep_vals=()
    for i in $(seq 1 $trials); do
        result=$(run_one "$prog" "$tmout")
        if [ "$result" != "TIMEOUT" ]; then
            read -r f b r <<< "$result"
            fwd_vals+=("$f"); bwd_vals+=("$b"); rep_vals+=("$r")
            echo "  trial $i: fwd=${f}ms bwd=${b}ms rep=${r}ms"
        else
            echo "  trial $i: TIMEOUT"
        fi
    done
    n=${#fwd_vals[@]}
    if [ $n -gt 0 ]; then
        fwd_med=$(median_of "${fwd_vals[@]}")
        bwd_med=$(median_of "${bwd_vals[@]}")
        rep_med=$(median_of "${rep_vals[@]}")
        echo "  MEDIAN: fwd=${fwd_med}ms bwd=${bwd_med}ms rep=${rep_med}ms (n=$n)"
    else
        echo "  MEDIAN: TIMEOUT"
    fi
}

echo "=== Python Benchmark ==="
echo "date: $(date '+%Y-%m-%d %H:%M:%S')"
echo "python: $(python3 --version)"
bench "fib10"     "$PROGS/fib.crl"      60  3
bench "sfib10"    "$PROGS/sfib.crl"     60  3
bench "sieve100"  "$PROGS/sieve.crl"    120 3
bench "fib15"     "$PROGS/fib15.crl"    300 1
bench "sfib15"    "$PROGS/sfib15.crl"   300 1
echo "=== Done ==="
