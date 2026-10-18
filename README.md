# Search Home — 地図 × 物件検索 × 通勤圏

住所・会社名・駅名から起点を検索し、指定半径内の建物・物件候補を地図上で比較するローカルWebアプリ。**実物件を架空データで補完しません。**

ヘッダーの **EN / JP** ボタンで表示言語を切り替えられます。選択はブラウザーに保存されます。住所・掲載元の文章・検索条件は元の言語を維持します。

Use **EN / JP** in the header to switch the interface language. Your choice is remembered in this browser; addresses and source listing text stay in their original language.

## 会話UIの土台 / Conversation foundation

検索条件は画面上部のチップから編集できます。従来の設定は「検索条件を編集・詳細設定」に残っています。条件とチャットの下書きはサーバー側のSQLiteに保存され、再読み込み後も復元されます。**AIチャット送信・自然言語からの条件抽出は次の段階です。現時点の入力欄は下書き保存のみです。**

Edit the condition chips above the collapsed advanced controls. Criteria and the chat draft persist on the server and restore after reload. **AI messaging and natural-language actions are not connected yet; the composer saves a draft only.** No additional API key is needed for this foundation.

- Persistence: `.local/sessions.sqlite3` (ignored by Git and blocked from HTTP access). The browser holds an HttpOnly session cookie; saved property candidates retain their existing browser storage.
- Updates contain `expected_revision` and a partial criteria object. Stale updates return HTTP 409 and require explicitly loading the saved version. Failed saves retain edits and offer Retry.
- Prices in the shared state are whole yen; the advanced form still displays multiples of ¥10,000. Rent excludes management fees. Distances remain straight-line.
- This is a local, single-user application. Separate browser profiles receive separate sessions; tabs in one profile share a session. This is not authenticated multi-user storage. Cookies expire after 30 days; the SQLite file currently has no automatic retention cleanup.

## 起動

Python 3.10+ が必要（追加パッケージ不要）。

```bash
python3 server.py
# http://127.0.0.1:8765/
```

Linux: `./start-linux.sh` / Windows: `start-windows.bat` でも起動可能。

任意のAPIキーは `.env.example` を `.env` にコピーして設定できます。サーバーは起動時に `.env` を読み込みます（環境変数の指定を優先）。編集後はサーバーを再起動してください。`.env` はGit管理対象外です。

Optional keys: copy `.env.example` to `.env`, fill in your keys, then restart the server. The file loads automatically; existing environment variables take priority. `.env` is ignored by Git.

任意のAPIキー:

```bash
BRAVE_SEARCH_API_KEY=... OPENAI_API_KEY=... python3 server.py
```

- **APIキーなし**: 住所・駅・会社検索（Nominatim/GSI）、OSMタイル、Overpassによる建物探索、2地点の直線距離、候補CSV・絞り込み、Bing RSSによる掲載URL探索（ベストエフォート）
- **Braveのキーあり**: 安定的なWeb掲載ページの発見（課金・クォータ依存）。SUUMO／HOME'S／at homeの本文スクレイピングは行わない。
- **OpenAIのキーあり**: 検索で見つかった掲載ページの種類をボタンで分類（Decisions API / `gpt-6-luna`）。利用者が操作したときのみ呼び出し、空室確認には使わない。

**重要**: 検索エンジンの抜粋から推定した家賃・間取り・住所は未確認。住所検索ピンは概略位置となる場合があります。OSMの建物データは募集中の住戸ではありません。実際の募集・所在地・家賃は必ずリンク先で確認してください。検索結果が0件の場合は0件と表示します。無許可の物件サイト自動クロールは行いません。

## 構成

- `session_store.py`: shared criteria schema, SQLite sessions and atomic revision checks
- `session.js`: queued session updates, draft persistence, conflict/error recovery
- `workspace.js` / `workspace.css`: bilingual chat shell, editable chips and advanced panel
- `index.html`: レスポンシブ地図UI（依存ライブラリなし）、地点A/B、距離円、外部検索、候補保存、CSV
- `server.py`: localhost専用HTTPサーバー、Bing RSS/Brave検索、正規化と出典、任意Decisions API分類
- `filter_core.py`: 複数地点AND/ORと月額費用の厳密フィルタ参照実装（不明値は除外）
- `ARCHITECTURE.md`: 設計全体、データ源・公式API、PostGIS／経路／ハザード／通知への拡張方針
- `tests/`: 抽出、URL制約、距離、未知値・境界条件、AI通信モック検証

## API

- `GET /api/health`: 機能・APIキー設定状態（キー自体は返さない）
- `GET /api/listings?place=渋谷区&kind=rent&provider=auto&layout=1K`: 公開検索エンジンのリンクと未検証フィールド
- `POST /api/classify` (`{"title":"...","description":"..."}`): 任意AI分類。`OPENAI_API_KEY`がある場合のみ

- `GET /api/session`: create or restore the cookie-bound search session (no API key)
- `POST /api/session`: update with `{"expected_revision":0,"criteria":{"max_price_yen":100000}}`; returns 409 if another update has already advanced the revision
- `GET /api/session/schema`: versioned criteria and partial-update schemas

## テスト

```bash
python3 -m unittest discover -s tests -v
python3 filter_core.py
node --test tests/session_client.test.cjs
```

## 今後の優先度

1. 物件オーナー・管理会社等の**許可された正式な募集物件フィード**を接続し、空室ステータスを保証できるデータを増やす。
2. Google Routes／駅すぱあとによる移動時間条件、国交省 不動産情報ライブラリによるハザード・生活環境レイヤー、重複住戸統合、費用内訳を追加する。
3. PostgreSQL/PostGIS、差分通知、検索条件保存、AIによる希望条件の補助採点へ拡張する。

詳細なAPI URL・利用条件は [ARCHITECTURE.md](./ARCHITECTURE.md) を参照。OSMタイル・Nominatim・Overpassの利用規約、各掲載サイトの利用規約を守ってください。
