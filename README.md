# PyCJanus (Python版 CJanus 処理系)

PyCJanusは、並行・可逆な命令型プログラミング言語 **Concurrent Janus** (CJanus) のPython実装です。

CJanusは、可逆言語Janusを並行拡張した言語です。本リポジトリは、Go言語によるリファレンス実装をPythonに移植したもので、研究室内の演習やデバッグ、動作解析のために作成されました。

## 主な機能

- **CRLインタプリタ** — CJanusの中間言語である CRL (Concurrent Reversible Language) プログラムを解釈・実行します。
- **3つの実行モード** — 順実行 (Forward)、逆実行 (Backward/Undo)、および決定論的再実行 (Replay) をサポートします。
- **アノテーションDAG** — 並行実行の履歴を管理し、並行プログラムの正しい逆実行を実現します。
- **排他制御** — セマフォを用いた `lock`/`unlock` 命令による共有変数の保護をサポートします。
- **メトリクス計測** — `-metrics` フラグにより、実行ステップ数、リトライ回数、最大スレッド数、待機時間などを表示します。
- **Webデバッガ** — ブラウザ上で動作するステップ実行デバッガ (`debugger/app.py`) を提供します。
- **静的リンター** — CRLプログラムの構造的な正当性（対称性など）を確認します (`tools/linter.py`)。
- **DAG可視化** — アノテーションDAGをGraphviz形式で出力し、依存関係を可視化できます (`tools/dag_visualize.py`)。

## 動作環境

- Python 3.10 以上
- [lark](https://github.com/lark-parser/lark) (`pip install lark`)
- Flask (Webデバッガを使用する場合のみ — `pip install flask`)

```bash
pip install -r requirements.txt
```

## 使い方 (演習用)

CRLプログラムは、CJanusコンパイラが出力する中間言語です。
サンプルプログラムは、[CJanus-IPSJPRO25-3](https://github.com/yokoyama-lab/CJanus-IPSJPRO25-3) リポジトリの `progs/` ディレクトリにあります。

```bash
# 基本的な順実行
python3 main.py path/to/fib.crl

# 順実行 → 逆実行 → 再実行 を自動で行い、実行時間を表示
python3 main.py path/to/fib.crl -v=false -auto -t

# 詳細な実行統計を表示
python3 main.py path/to/fib.crl -v=false -auto -t -metrics
```

## 主要なコマンドラインオプション

| フラグ | デフォルト | 内容 |
|--------|------------|------|
| `-v=false` | verbose ON | 各基本ブロックの実行ログを非表示にする |
| `-auto` | OFF | 順実行・逆実行・再実行を連続して自動実行する |
| `-t` | OFF | 各フェーズの実行時間を表示する |
| `-metrics` | OFF | ステップ数、リトライ回数、スレッド統計を表示する |
| `-procs N` | 1 | ワーカースレッド数を指定 (PythonのGILにより並列性能は制限されます) |
| `-execdebug` | OFF | 逆実行が順実行を正しく戻しているか厳密に検証する |
| `-silent` | OFF | 実行ログを一切出力しない |

## ディレクトリ構成

```
PyCJanus/
├── main.py                  # エントリポイント
├── runtime/
│   ├── runtime.py           # ランタイム本体、CRLローダー、実行制御
│   ├── pc.py                # プログラムカウンタ、基本ブロック実行
│   ├── dag.py               # アノテーションDAG（依存関係、ロック管理）
│   ├── instset.py           # CRL命令セットの定義
│   ├── symtab.py            # シンボルテーブル
│   ├── label.py             # ラベル解決
│   ├── expr.py              # 式評価器
│   ├── config.py            # 設定フラグ管理
│   └── metrics.py           # 実行統計コレクタ
├── tools/
│   ├── linter.py            # CRL静的リンター
│   ├── dag_visualize.py     # アノテーションDAG → Graphviz出力
│   └── gen_fib.py           # fib(n) プログラム生成ツール
├── debugger/
│   ├── app.py               # FlaskベースのWebデバッガ
│   └── templates/index.html # デバッガUI
├── runtime_mp/
│   ├── runtime_mp.py        # マルチプロセス版（実験的）
│   └── benchmark_nogil.py   # GIL/no-GIL性能ベンチマーク
└── benchmark_py.sh          # Python版ベンチマークスクリプト
```

## Webデバッガの起動

ブラウザを使用して、プログラムの実行を1ステップずつ追跡できます。

```bash
pip install flask
python3 debugger/app.py --port 5000 --file path/to/program.crl
# http://localhost:5000 を開く
```

## ツール群

### CRLリンター
プログラムの構造的な誤り（ラベルの対応、スタックの整合性、対称性など）をチェックします。

```bash
python3 tools/linter.py path/to/program.crl
```

### DAG可視化ツール
実行後に生成されたアノテーションDAGを画像化し、プロセス間の依存関係を確認できます。

```python
from tools.dag_visualize import dump_dag
# ランタイムインスタンス rt の実行後:
dump_dag(rt, "output.dot")
# 変換: dot -Tsvg output.dot -o dag.svg
```

## 背景

**Janus** は、可逆な命令型プログラミング言語です。
**CJanus** は、Janusに並行手続呼出し (`call p, q`) と分散ロックを導入し、
アノテーションDAGを用いることで、並行プログラムの順実行と逆実行を可能にした言語です。

## ライセンス

MITライセンス。詳細は [LICENSE](LICENSE) を参照してください。
