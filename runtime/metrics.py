"""CJanus runtime metrics collector."""
import threading
import time
from dataclasses import dataclass, field
from typing import List


class MetricsCollector:
    """Thread-safe metrics collector for CJanus runtime."""

    def __init__(self):
        self._lock = threading.Lock()
        # ブロック実行数（フェーズ別）
        self.blocks_fwd = 0
        self.blocks_bwd = 0
        self.blocks_rep = 0
        # リトライ数（ロック競合）
        self.retries_total = 0
        self.retries_pv = 0    # P/V操作のリトライ
        self.retries_dag = 0   # DAG ロックのリトライ
        # スレッド数の最大値・現在値
        self.max_threads = 0
        self.thread_count = 0
        # 待機時間の合計（フェーズ別）
        self.wait_time_fwd = 0.0
        self.wait_time_bwd = 0.0
        self.wait_time_rep = 0.0
        # 現在のフェーズ: 'fwd' / 'bwd' / 'rep'
        self._current_phase = "fwd"

    # ------------------------------------------------------------------
    # フェーズ管理
    # ------------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        """現在のフェーズを設定（'fwd' / 'bwd' / 'rep'）。"""
        with self._lock:
            self._current_phase = phase

    # ------------------------------------------------------------------
    # ブロック実行記録
    # ------------------------------------------------------------------

    def record_block(self, phase: str) -> None:
        """フェーズ（fwd/bwd/rep）のブロック実行を記録。"""
        with self._lock:
            if phase == "fwd":
                self.blocks_fwd += 1
            elif phase == "bwd":
                self.blocks_bwd += 1
            elif phase == "rep":
                self.blocks_rep += 1

    # ------------------------------------------------------------------
    # リトライ記録
    # ------------------------------------------------------------------

    def record_retry(self, kind: str, wait_time: float) -> None:
        """リトライを記録（kind: 'dag' or 'pv'）。"""
        with self._lock:
            self.retries_total += 1
            if kind == "pv":
                self.retries_pv += 1
            else:
                self.retries_dag += 1
            # 現在のフェーズに応じて待機時間を加算
            phase = self._current_phase
            if phase == "fwd":
                self.wait_time_fwd += wait_time
            elif phase == "bwd":
                self.wait_time_bwd += wait_time
            elif phase == "rep":
                self.wait_time_rep += wait_time

    # ------------------------------------------------------------------
    # スレッド数記録
    # ------------------------------------------------------------------

    def record_thread_spawned(self) -> None:
        """スレッド生成を記録。"""
        with self._lock:
            self.thread_count += 1
            if self.thread_count > self.max_threads:
                self.max_threads = self.thread_count

    def record_thread_done(self) -> None:
        """スレッド終了を記録。"""
        with self._lock:
            self.thread_count -= 1

    # ------------------------------------------------------------------
    # サマリ出力
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        """メトリクスサマリを出力。"""
        blocks_fwd = self.blocks_fwd
        blocks_bwd = self.blocks_bwd
        blocks_rep = self.blocks_rep
        retries_total = self.retries_total
        retries_dag = self.retries_dag
        retries_pv = self.retries_pv
        wait_fwd = self.wait_time_fwd * 1000
        wait_bwd = self.wait_time_bwd * 1000
        wait_rep = self.wait_time_rep * 1000
        wait_total = wait_fwd + wait_bwd + wait_rep
        max_threads = self.max_threads
        blocks_total = blocks_fwd + blocks_bwd + blocks_rep

        # リトライ数はフェーズ別に分けて保持していないため total を均等には見せず、
        # フェーズごとのwaitから推定する（表示のみ）
        # retries per phase: 待機時間に比例して按分
        if wait_total > 0:
            r_fwd = int(retries_total * wait_fwd / wait_total + 0.5)
            r_bwd = int(retries_total * wait_bwd / wait_total + 0.5)
            r_rep = retries_total - r_fwd - r_bwd
        elif blocks_total > 0:
            r_fwd = int(retries_total * blocks_fwd / blocks_total + 0.5) if blocks_fwd else 0
            r_bwd = int(retries_total * blocks_bwd / blocks_total + 0.5) if blocks_bwd else 0
            r_rep = retries_total - r_fwd - r_bwd
        else:
            r_fwd = r_bwd = r_rep = 0

        retry_rate = retries_total / blocks_total if blocks_total > 0 else 0.0

        print()
        print("=== CJanus Runtime Metrics ===")
        print(f"{'Phase':<14} {'Blocks':>10} {'Retries':>10} {'Wait(ms)':>10}")
        print(f"{'Forward':<14} {blocks_fwd:>10,} {r_fwd:>10,} {wait_fwd:>10.1f}")
        print(f"{'Backward':<14} {blocks_bwd:>10,} {r_bwd:>10,} {wait_bwd:>10.1f}")
        print(f"{'Replay':<14} {blocks_rep:>10,} {r_rep:>10,} {wait_rep:>10.1f}")
        print()
        print(f"Peak threads:  {max_threads:>6}")
        print(f"Total retries: {retries_total:>6,}  (DAG: {retries_dag:,} / PV: {retries_pv:,})")
        print(f"Total wait:    {wait_total:>6.1f} ms")
        print(f"Retry rate:    {retry_rate:>6.1f} retries/block")


# グローバルインスタンス
metrics = MetricsCollector()
