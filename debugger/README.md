# CJanus Web Debugger

ブラウザから CJanus (CRL) プログラムをステップ実行できる Flask ベースのデバッガです。

## 起動方法

```bash
cd /path/to/PyCJanus

# デフォルト (ポート 5000)
python3 debugger/app.py

# ポート変更
python3 debugger/app.py --port 5001

# ファイルを起動時に読み込む
python3 debugger/app.py --file ../CJanus-IPSJPRO25-3/progs/fib.crl

# ポートとファイルを両方指定
python3 debugger/app.py --port 5001 --file fib.crl
```

ブラウザで `http://localhost:5000/` を開きます。

## 依存関係

```
Flask>=3.0
```

インストール済みでない場合:
```bash
pip install flask --break-system-packages
```

## 使い方

1. **ファイル入力欄** に CRL ファイルのパスを入力して **[Load]** を押す
2. **[Forward]** ボタンで順方向実行
3. **[Backward]** ボタンで逆方向実行
4. **[Run]** ボタンで現在の方向で再実行
5. **[Vars]** ボタンで変数・ヒープの現在値を更新
6. **[DAG]** ボタンでアノテーション DAG を出力ログに表示
7. **[Del DAG]** ボタンでアノテーション DAG を削除

## REST API

| メソッド | パス | 説明 |
|---------|------|------|
| `GET` | `/` | デバッガ UI |
| `POST` | `/load` | CRL ファイルをロード `{"file": "path"}` |
| `POST` | `/command` | コマンド送信 `{"cmd": "fwd"\|"bwd"\|"run"\|"var"\|"dag"\|"deldag"}` |
| `GET` | `/state` | 現在の状態を JSON で返す |
| `GET` | `/vars` | 変数・ヒープ値を JSON で返す |
| `GET` | `/source` | CRL ソース行を JSON で返す |

### `/state` レスポンス例

```json
{
  "loaded": true,
  "phase": "forward",
  "running": false,
  "output": ["Debug >> Execution finished", "Elapsed: 0.123456s"],
  "crl_file": "/home/a/.../fib.crl"
}
```

### `/vars` レスポンス例

```json
{
  "ok": true,
  "heap": [0, 0, 55, 0],
  "vars": {
    "n": {"addr": "V[1]", "value": 10},
    "result": {"addr": "V[2]", "value": 55}
  }
}
```

## アーキテクチャ

```
ブラウザ
  │  fetch API (ポーリング 800ms)
  ▼
Flask app (debugger/app.py)
  │  queue.Queue ("fwd\n", "bwd\n", "run\n", ...)
  ▼
Runtime.debug() — 別スレッドで動作
  │  stdout をキャプチャして output_buffer に蓄積
  ▼
CJanus Runtime (runtime/)
```

`_CapturingWriter` が `sys.stdout` を差し替えて Runtime の `print()` 出力をバッファに蓄積し、
`/state` エンドポイント経由でブラウザに返します。

## ファイル構成

```
debugger/
  __init__.py       パッケージ初期化
  app.py            Flask アプリ本体
  templates/
    index.html      デバッガ UI
  static/
    style.css       スタイルシート
  README.md         本ファイル
```
