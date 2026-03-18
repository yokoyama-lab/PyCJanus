# runtime_mp — Multiprocessing CJanus Runtime

## 概要

`runtime_mp/` は CJanus Python ランタイムの GIL（Global Interpreter Lock）を回避するための
マルチプロセス版実装です。`runtime/` の threading ベース実装は一切変更しません。

---

## ファイル構成

| ファイル | 説明 |
|---|---|
| `__init__.py` | モジュール定義。`RuntimeMP` をエクスポート |
| `runtime_mp.py` | `RuntimeMP` クラス。`execute_call` をオーバーライド |
| `main_mp.py` | マルチプロセス版エントリポイント |
| `benchmark_nogil.py` | GIL 検出・スレッドスケーリング・プロセス並列ベンチマーク |
| `README.md` | 本ファイル |

---

## アーキテクチャの課題

### Python GIL の影響

```
  1 thread  : 100%
  2 threads : ~100% (GIL により並列化されない)
  4 threads : ~100% (同上)
```

CJanus の `call fib, fib` は 2 つの並行スレッドを生成するが、Python の threading では
GIL により実質的に逐次実行になる。

### multiprocessing への直接移行が困難な理由

CJanus ランタイムは密結合なオブジェクトグラフで構成されている：

```
Runtime (Symtab)
  ├── heapmem / varmem  (HeapMemory — list[int] + threading.Lock)
  ├── dag               (DAG — DagEntry ごとに threading.Semaphore + threading.Lock)
  └── PC ツリー         (PC — localtable, pnode DagPNode ...)
```

`threading.Lock` / `threading.Semaphore` は pickle 不可のため、`Process` に直接渡せない。
完全な移行には DAG 層を `multiprocessing.Lock` / `multiprocessing.Semaphore` で書き直す必要がある。

---

## 現状の実装 (RuntimeMP)

### Layer 1: threading フォールバック（デフォルト）

`_mp_enabled = False`（デフォルト）のとき、`execute_call` は親クラス (`Runtime`) に委譲する。
threading ベースの既存動作と完全に同一。

```python
from runtime_mp import RuntimeMP
r = RuntimeMP(lines)
r._mp_enabled = False  # デフォルト: threading を使用
```

### Layer 2: 実験的マルチプロセスパス (`_mp_enabled = True`)

各サブプロシージャを独立した OS プロセスで実行する。

**動作フロー:**

1. `_snapshot_runtime()` でランタイム状態をシリアライズ可能な dict に変換
   - `file` (命令列), `heapmem`, `varmem`, `alloctable`, `labels`
2. 各子プロセスでスナップショットからランタイムを再構築
3. DAG アノテーションを無効化 (`no_dag = True`) して子プロシージャを実行
4. 子プロセスの結果を `multiprocessing.Queue` で収集

**制限事項:**

- 子プロセスはランタイム状態の**コピー**上で動作する
- 子プロシージャの変数書き込みは親プロセスに反映されない
- DAG アノテーションが無効化される
- 共有変数を持つプログラム（e.g., sieve.crl）では正しい結果が得られない

これらの制限を解消するには以下が必要：

```
future work:
  multiprocessing.shared_memory.SharedMemory で heapmem/varmem を共有
  multiprocessing.Lock で DagEntry._meta_lock を置き換え
  multiprocessing.Semaphore で DagNode._sem を置き換え
```

---

## 使い方

### ベンチマーク実行

```bash
cd /home/a/myclaude/cjanus/cjanus_py
python3 runtime_mp/benchmark_nogil.py
```

オプション:

```bash
# fib(12) でベンチマーク (デフォルト: fib(10))
python3 runtime_mp/benchmark_nogil.py --fib 12

# プロセス並列ベンチマークをスキップ (高速化)
python3 runtime_mp/benchmark_nogil.py --no-process

# 繰り返し回数を変更
python3 runtime_mp/benchmark_nogil.py --reps 5
```

### マルチプロセス版エントリポイント

```bash
# threading モード（デフォルト、main.py と同一）
python3 runtime_mp/main_mp.py progs/fib.crl -auto -v=false -t

# 実験的マルチプロセスモード
python3 runtime_mp/main_mp.py progs/fib.crl -auto -v=false --mp
```

---

## GIL 回避のための推奨アプローチ

| アプローチ | 難易度 | 効果 | 備考 |
|---|---|---|---|
| python3.13t (free-threaded) | ★☆☆ | 高 | このシステムでは未インストール |
| multiprocessing + SharedMemory | ★★★ | 高 | DAG 層の全面書き直しが必要 |
| Cython/C 拡張でホットパスを抜き出す | ★★★ | 中〜高 | GIL 解放可能 |
| Go 版ランタイム (CJanus-IPSJPRO25-3) | ― | 最高 | 既実装。goroutine は GIL なし |

現在のシステム環境では **Go 版を使用する**のが最も確実に真の並列実行を実現できる。
