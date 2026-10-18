# エージェントから物件を調べる

既存のローカルHTTPバックエンドを、公式Python SDK `mcp==2.3.0` のstdio MCPから呼び出します。MCP側ではスクレイピング、LLM呼び出し、独自の順位付けを行いません。APIキーは不要です。

## 起動

Python 3.11以上を用意し、リポジトリのルートで実行します。

```bash
python3 -m venv tools/mcp/.venv
tools/mcp/.venv/bin/python -m pip install -r tools/mcp/requirements.txt
tools/mcp/.venv/bin/python tools/mcp/server.py
```

Windowsでは `tools/mcp/.venv/bin/python` を `tools\mcp\.venv\Scripts\python.exe` に置き換えます。stdioはMCPクライアントが起動する用途です。ターミナルで直接実行した場合は入力を待ちます。

先に既存バックエンドを起動してください。既定の接続先は `http://127.0.0.1:8773` です。別ポートでは `HOUSING_BACKEND_URL=http://127.0.0.1:8765` のように設定します。バックエンドの起動・設定変更はMCPが行いません。

## ツール

| ツール | バックエンド | 用途 |
| --- | --- | --- |
| `get_capabilities` | `/api/health` | 接続状態、取得可能なソース、調査モード・再利用許諾を確認 |
| `search_properties` | `/api/suumo-search` | 緯度・経度・半径・家賃上限・間取り・ページを指定して検索 |
| `get_property_address` | `/api/suumo-address` | 検索で返った `source_id` の掲載所在地を取得 |
| `search_public_housing` | `/api/public-housing` | 東京都・津幡町の公開住宅データを名称・住所で検索 |

SUUMO検索は対応する東京都内の範囲で、1ページ20件を調べます。絞り込み後は20件未満になることがあります。`max_rent_man=15` は管理費を含めない家賃15万円です。バックエンドがSUUMO地図の家賃選択値を検証します。`has_next=true` なら `page` を1増やします。公開住宅データは返された `next_offset` を次の `offset` に渡します。

`get_property_address` は `/api/suumo-address` を実装したバックエンドが必要です。SUUMO住所拡張が未導入の場合はHTTP 404を返します。SUUMOの取得が無効なバックエンドはHTTP 403を返します。これらを結果0件には変換しません。ローカル調査用の `SUUMO_LOCAL_RESEARCH` は、その設定に対応したバックエンドのみで利用できます。公開再利用の許諾とは別です。

出典URL、取得日時、徒歩情報があればその情報、未確認の値、注記をそのまま返します。空室、半径内、正確な座標、全費用は確認済みと扱わないでください。公開住宅データも現在の募集を保証しません。所在地には番地まで掲載されていない場合があります。

接続先はHTTPのループバックIPまたは `localhost` のみです。`localhost` は `127.0.0.1` に正規化します。URLの認証情報、パス、クエリ、フラグメント、HTTPリダイレクト、環境プロキシは使えません。ツール引数で任意のURLを渡すこともできません。

## Codex設定例

以下は[公式OpenAIドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)に従った設定例です。`/absolute/path/to/search-home` をチェックアウト先に置き換えます。設定の自動インストールは行いません。

```toml
[mcp_servers.local_housing]
command = "/absolute/path/to/search-home/tools/mcp/.venv/bin/python"
args = ["/absolute/path/to/search-home/tools/mcp/server.py"]
startup_timeout_sec = 10
tool_timeout_sec = 60

[mcp_servers.local_housing.env]
HOUSING_BACKEND_URL = "http://127.0.0.1:8773"
```

指示例は「接続状態を確認し、東京駅付近の半径1km、家賃15万円以内の候補を1ページ検索してください。候補1件の所在地と駅徒歩を出典付きで示してください」です。

## 検証

```bash
tools/mcp/.venv/bin/python -m unittest discover -s tools/mcp -p 'test_server.py' -v
tools/mcp/.venv/bin/python tools/mcp/verify_stdio.py
```

単体テストではループバック制限、リダイレクト拒否、不正引数、元データの保持、HTTPエラーのMCPエラーへの伝達を確認します。

`verify_stdio.py` は公式 `Client` で別プロセスのMCPを起動し、初期接続、ツール一覧、実バックエンドでのSUUMO検索・所在地取得・東京都公開住宅データ検索を確認します。外部ソースへの通信が生じます。失敗時は終了コードを非ゼロにし、成功時は件数と候補1件の小さな検証結果を標準出力に返します。写真、認証値、一覧データの保存は行いません。

MCPのJSON-RPC処理は[公式Python SDK](https://github.com/modelcontextprotocol/python-sdk)が担当します。
