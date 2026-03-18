"""CJanus 基本プログラム統合テスト

各テストは CRL プログラムを PyCJanus ランタイムで実行し、
計算結果と可逆性（逆方向実行でメモリ初期化）を検証する。

テスト一覧:
  1. test_fib_n10              : fib.crl (n=10)  並行Fibonacci、順方向M[1]=55
  2. test_fib_n5               : fib5.crl (n=5)  並行Fibonacci小規模、順方向M[1]=5
  3. test_tri_n5               : tri.crl (n=5)   並行三角数、順方向M[1]=15
  4. test_stri_n5_reversible   : stri.crl (n=5)  逐次三角数、exec_debug で命令レベル可逆性検証
  5. test_backward_restores    : fib.crl (n=10)  逆方向実行でメモリが [0,0] に戻ることを直接確認
"""
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

# --- パス設定 ---
TESTS_DIR  = Path(__file__).parent
CJANUS_PY  = TESTS_DIR.parent
PROGS_DIR  = CJANUS_PY.parent / "CJanus-IPSJPRO25-3" / "progs"

sys.path.insert(0, str(CJANUS_PY))


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def _reset_cfg():
    """グローバル Config をデフォルト値にリセットする。"""
    from runtime.config import cfg
    cfg.slow_debug     = 0.0
    cfg.dag_debug      = False
    cfg.var_debug      = False
    cfg.block_debug    = False
    cfg.exec_debug     = False
    cfg.sem_debug      = False
    cfg.lock_debug     = False
    cfg.dag_lock_debug = False
    cfg.timing_debug   = False
    cfg.sequential     = False
    cfg.suppress_output = True
    cfg.slow_parse     = False
    cfg.strict         = False
    cfg.no_dag         = False
    cfg.ext_expr       = False
    cfg.max_procs      = 0
    cfg.auto           = False
    cfg.auto_progress  = 0
    cfg.step_mode      = False
    cfg.trace_cli      = False


def _build_runtime(crl_path):
    """CRL ファイルを読み込んで Runtime インスタンスを返す。"""
    from runtime.instset import init_instset
    from runtime.runtime import Runtime

    with open(crl_path) as f:
        lines = [line.rstrip() for line in f if line.strip()]
    r = Runtime(lines)
    r.instset = init_instset(r)
    r.load_crl()
    return r


def _run_auto(crl_path, *, timeout=30.0, exec_debug=False):
    """
    CRL ファイルを forward→backward→replay の auto モードで完全実行し、
    完了後の heapmem.values を返す。
    exec_debug=True を指定すると命令レベルの fwd/bwd 整合性を検証する。
    """
    from runtime.config import cfg

    _reset_cfg()
    cfg.auto         = True
    cfg.auto_progress = 0
    cfg.exec_debug   = exec_debug

    r = _build_runtime(crl_path)
    debugsym: queue.Queue = queue.Queue()
    done = threading.Event()
    errors: list = []

    def _run():
        try:
            r.debug(debugsym, done)
        except Exception as exc:
            errors.append(exc)
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    completed = done.wait(timeout=timeout)

    if errors:
        raise errors[0]
    if not completed:
        done.set()
        pytest.fail(f"auto モード タイムアウト ({timeout}s): {Path(crl_path).name}")

    return list(r.heapmem.values)


def _run_fwd_then_bwd(crl_path, *, timeout=30.0):
    """
    CRL ファイルを順方向実行→逆方向実行の 2 フェーズで手動制御し、
    各フェーズ終了後の heapmem.values を返す。
    戻り値: (mem_after_fwd, mem_after_bwd)
    """
    from runtime.config import cfg

    _reset_cfg()
    # auto は使わず debug ループを手動制御する
    cfg.auto = False

    r = _build_runtime(crl_path)
    debugsym: queue.Queue = queue.Queue()
    done = threading.Event()
    errors: list = []

    def _run():
        try:
            r.debug(debugsym, done)
        except Exception as exc:
            errors.append(exc)
            done.set()

    threading.Thread(target=_run, daemon=True).start()

    # --- 順方向実行完了を待つ ---
    deadline = time.time() + timeout
    while not r.run_stop.is_set():
        if time.time() > deadline:
            done.set()
            pytest.fail(f"順方向タイムアウト ({timeout}s): {Path(crl_path).name}")
        time.sleep(0.02)

    time.sleep(0.15)  # debug ループが RUN_STOPPED に遷移するのを待つ
    if errors:
        raise errors[0]
    mem_fwd = list(r.heapmem.values)

    # --- 逆方向実行を開始 ---
    debugsym.put("bwd\n")
    time.sleep(0.05)
    debugsym.put("run\n")

    # run_stop が一旦クリアされる（実行開始）のを待ってから、再セットされるのを待つ
    cleared_deadline = time.time() + 5.0
    while r.run_stop.is_set():
        if time.time() > cleared_deadline:
            done.set()
            pytest.fail("逆方向実行の開始を検出できませんでした")
        time.sleep(0.01)

    deadline = time.time() + timeout
    while not r.run_stop.is_set():
        if time.time() > deadline:
            done.set()
            pytest.fail(f"逆方向タイムアウト ({timeout}s): {Path(crl_path).name}")
        time.sleep(0.02)

    time.sleep(0.15)
    if errors:
        raise errors[0]
    mem_bwd = list(r.heapmem.values)

    done.set()
    return mem_fwd, mem_bwd


# ---------------------------------------------------------------------------
# テスト 1: fib.crl (n=10) — 並行 Fibonacci
# ---------------------------------------------------------------------------

def test_fib_n10():
    """fib.crl (n=10): 並行 Fibonacci の auto 実行、M[0]=10, M[1]=55 を確認。"""
    crl = PROGS_DIR / "fib.crl"
    if not crl.exists():
        pytest.skip(f"テストファイルが見つかりません: {crl}")

    mem = _run_auto(crl, timeout=30.0)

    assert mem[0] == 10, f"M[0] 期待値=10, 実際={mem[0]}"
    assert mem[1] == 55, f"M[1] 期待値=55 (fib(10)), 実際={mem[1]}"


# ---------------------------------------------------------------------------
# テスト 2: fib5.crl (n=5) — 並行 Fibonacci 小規模
# ---------------------------------------------------------------------------

def test_fib_n5():
    """fib5.crl (n=5): 並行 Fibonacci 小規模の auto 実行、M[0]=5, M[1]=5 を確認。"""
    crl = PROGS_DIR / "fib5.crl"
    if not crl.exists():
        pytest.skip(f"テストファイルが見つかりません: {crl}")

    mem = _run_auto(crl, timeout=20.0)

    assert mem[0] == 5, f"M[0] 期待値=5, 実際={mem[0]}"
    assert mem[1] == 5, f"M[1] 期待値=5 (fib(5)), 実際={mem[1]}"


# ---------------------------------------------------------------------------
# テスト 3: tri.crl (n=5) — 並行三角数 T(5) = 15
# ---------------------------------------------------------------------------

def test_tri_n5():
    """tri.crl (n=5): 並行三角数の auto 実行、M[0]=5, M[1]=15 を確認。"""
    crl = PROGS_DIR / "tri.crl"
    if not crl.exists():
        pytest.skip(f"テストファイルが見つかりません: {crl}")

    mem = _run_auto(crl, timeout=20.0)

    assert mem[0] == 5,  f"M[0] 期待値=5, 実際={mem[0]}"
    assert mem[1] == 15, f"M[1] 期待値=15 (1+2+3+4+5), 実際={mem[1]}"


# ---------------------------------------------------------------------------
# テスト 4: stri.crl (n=5) — 逐次三角数、exec_debug で可逆性検証
# ---------------------------------------------------------------------------

def test_stri_n5_reversible():
    """
    stri.crl (n=5): 逐次三角数の auto 実行。
    exec_debug=True により命令レベルの fwd/bwd 整合性も検証する。
    M[0]=5, M[1]=15 を確認。
    """
    crl = PROGS_DIR / "stri.crl"
    if not crl.exists():
        pytest.skip(f"テストファイルが見つかりません: {crl}")

    # stri は逐次（単プロセス）なので exec_debug が安全に使用可能
    mem = _run_auto(crl, timeout=20.0, exec_debug=True)

    assert mem[0] == 5,  f"M[0] 期待値=5, 実際={mem[0]}"
    assert mem[1] == 15, f"M[1] 期待値=15 (1+2+3+4+5), 実際={mem[1]}"


# ---------------------------------------------------------------------------
# テスト 5: fib.crl (n=10) — 逆方向実行でメモリが初期状態に戻ることを確認
# ---------------------------------------------------------------------------

def test_backward_restores_memory():
    """
    fib.crl (n=10): 順方向実行後の M=[10,55] が、
    逆方向実行後に M=[0,0]（初期状態）へ完全復元されることを直接確認。
    """
    crl = PROGS_DIR / "fib.crl"
    if not crl.exists():
        pytest.skip(f"テストファイルが見つかりません: {crl}")

    mem_fwd, mem_bwd = _run_fwd_then_bwd(crl, timeout=30.0)

    # 順方向: Fibonacci(10) = 55
    assert mem_fwd[0] == 10, f"fwd M[0] 期待値=10, 実際={mem_fwd[0]}"
    assert mem_fwd[1] == 55, f"fwd M[1] 期待値=55, 実際={mem_fwd[1]}"

    # 逆方向: すべての書き込みが打ち消され初期値に戻る
    assert mem_bwd[0] == 0, f"bwd M[0] 期待値=0 (n が初期化), 実際={mem_bwd[0]}"
    assert mem_bwd[1] == 0, f"bwd M[1] 期待値=0 (r が初期化), 実際={mem_bwd[1]}"
