# Search Home — 地図 × 物件検索 × 通勤圏

住所・会社名・駅名から起点を検索し、周辺の建物と、掲載元が1時間以内に募集確認した候補を地図上で比較するローカルWebアプリ。**実物件を架空データで補完しません。** 通常の地図クリックでは起点を動かさず、地点指定モードで変更します。

ヘッダーの **EN / JP** ボタンで表示言語を切り替えられます。選択はブラウザーに保存されます。住所・掲載元の文章・検索条件は元の言語を維持します。

Use **EN / JP** in the header to switch the interface language. Your choice is remembered in this browser; addresses and source listing text stay in their original language.

## 会話で住まいを探す / Chat with your home-search agent

EN/JPのチャットで、場所・予算・間取り・こだわりを伝えて検索できます。「予算だけ下げて」「職場の近く」「2番目を保存」「さっきの変更を戻して」と会話を続けられます。条件チップと詳細設定も同じ検索状態を使います。

Describe your location, budget and preferences in English or Japanese. The assistant can update criteria, resolve places, search real listing pages, compare/save retrieved candidates and undo a criteria change. Manual controls remain under **Advanced**.

- **Required for chat:** `OPENAI_API_KEY` with Agents API read/write and model inference access. The app uses the [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api/quickstart) directly through HTTPS, with `environment: none`; no sandbox or extra Python package is needed. `OPENAI_AGENT_MODEL` defaults to `gpt-6.1-sol` for new conversations.
- **Memory:** The same remote agent session retains conversation context. Explicit preferences and named places (such as your office), local message history, up to five result snapshots, saved candidates and twenty criteria revisions persist in `.local/sessions.sqlite3`. Reloading or restarting the server restores the conversation. Saved legacy manual candidates retain their existing browser storage.
- **Actions and recovery:** Messages use stable submission IDs. Tool side effects and their results are journaled together so recovery does not apply a change twice. Stop cancels the remote turn. Retry resumes uncertain work; failed actions are never presented as successful. Manual changes are protected by revision checks.
- **Privacy controls:** “Clear chat & memory” deletes the remote session and local conversation/memory, retaining criteria and saved candidates. “New search” also resets criteria. These operations complete asynchronously; a failed remote deletion is shown as an error rather than a successful clear.
- **Limits:** Maps, geocoding and Bing listing discovery work without extra keys; Brave is optional. Listing snippets and approximate locations are unverified, and zero results remain zero. Rent caps exclude management fees. Train/walking commute times and verified vacancies require additional data services and are not claimed by this app.
- **Local app:** Browser profiles receive separate HttpOnly, SameSite=Strict session cookies; tabs share one session. This is not an authenticated multi-user service. Cookies expire after 30 days; automatic database retention cleanup is not implemented. Environment and database files are ignored by Git and blocked from HTTP access.

例 / Example: “Remember that I have a cat named Mochi. Find a 1K rental near Tokyo Station under ¥100,000 in listed rent.” Then: “Change only the budget to ¥85,000,” “What is my cat’s name?” or “Undo the last change.” Pet/cat permission remains unverified until supported by the source.

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

- **APIキーなし**: 住所・駅・会社検索（Nominatim/GSI）、OSMタイル、Overpassによる建物探索、2地点の直線距離、許可済み募集JSON/CSVの取り込み、管理費・その他必須費用込みの条件判定、Bing RSSによる掲載元リンク探索（ベストエフォート）、徒歩・自転車・車の経路時間照会。
- **Braveのキーあり**: 安定的なWeb掲載ページの発見（課金・クォータ依存）。SUUMO／HOME'S／at homeの本文スクレイピングは行わない。
- **OpenAIのキーあり**: 検索で見つかった掲載ページの種類をボタンで分類（Decisions API / `gpt-6-luna`）。利用者が操作したときのみ呼び出し、空室確認には使わない。

**募集情報の鮮度**: 検索索引の取得時刻では掲載元の募集状況を確認できないため、検索結果の家賃・間取り・住所は募集候補に採用せず、掲載元リンクだけを表示します。候補に採用するのは、出典URLと提供元の募集確認日時を持ち、確認から1時間以内のデータです。不明・未来日時・募集終了・古いデータは除外し、表示中も30秒ごとに期限を再判定します。確認日時は提供元の申告であり、リアルタイム空室の保証ではありません。掲載元で最終確認してください。

**取り込み**: 「許可済みJSON / CSVを取り込む」から利用許可を確認して読み込めます。自動連携には `LISTINGS_FEED_PATH`（JSONファイル）または `LISTINGS_FEED_URL`（許可済みHTTPS JSONフィード）を設定します。形式は [募集データ仕様](docs/listing-feed.md) を参照。未設定の場合、現在募集中の物件を自動取得できません。データを捏造して補完しません。

**条件判定**: 賃貸の予算は家賃＋管理費＋その他必須月額費用です。費用不明を0円にはせず、必須条件不明の候補は初期状態で除外します。「条件が未確認の候補も表示」を有効にしても、不一致・古い募集は採用しません。概略座標は厳密な距離条件に合格させません。保存済みの古い候補は再確認・編集から更新できます。

**無料の通勤時間**: 各候補の「通勤時間を取得」で、住戸から勤務地A（Bの絞り込みを有効にした場合はBも）への道路経路を [FOSSGIS / OSRM](https://routing.openstreetmap.de/about.html) に照会します。APIキー・課金設定は不要。リクエストは利用者の操作時のみ、プロセス全体で1.1秒以上の間隔を設け、取得結果は同じ座標・移動手段に対して5分間だけ判定に使用します。道路の位置合わせは100m以内に限定し、経路なし・失敗時に直線距離から時間を捏造しません。公開サービスの利用制限・停止時には未判定と表示します。

道路経路の時間は推定値で、リアルタイムの渋滞・運休や現在の道路規制を保証しません。**電車の時刻・所要時間はアプリ内で取得せず、候補のGoogle Maps外部リンクで確認します。電車時間による絞り込みは未対応です。** Google Routes APIは呼び出しません。大規模公開・大量照会には公開OSRMを使わず、別途運用可能な経路基盤を用意してください。

## 構成

- `home_agent.py`: Agents API client, durable conversation worker, validated search/memory/save/Undo tools
- `chat.js`: chat history, progress, cancellation, recovery and memory controls
- `session_store.py`: shared criteria schema, SQLite sessions and atomic revision checks
- `session.js`: queued session updates, draft persistence, conflict/error recovery
- `workspace.js` / `workspace.css`: bilingual chat shell, editable chips and advanced panel
- `index.html`: レスポンシブ地図UI（依存ライブラリなし）、地点A/B、距離円、外部検索、候補保存、CSV
- `server.py`: localhost専用HTTPサーバー、Bing RSS/Brave検索、正規化と出典、任意Decisions API分類
- `listing_feed.py`: 許可済み募集データの正規化、鮮度・出典・位置精度の確認
- `property_core.js`: 保存候補と取り込み候補に共通の費用・不明値・距離・通勤時間の判定
- `filter_core.py`: 複数地点AND/ORと月額費用の厳密フィルタ参照実装（不明値は除外）
- `ARCHITECTURE.md`: 設計全体、データ源・公式API、PostGIS／経路／ハザード／通知への拡張方針
- `tests/`: 抽出、URL制約、距離、未知値・境界条件、AI通信モック検証

## API

- `GET /api/health`: 機能・APIキー設定状態（キー自体は返さない）
- `GET /api/listings?place=渋谷区&kind=rent&provider=auto&layout=1K`: 設定済みフィードの新しい募集、または検索エンジンの掲載元リンク。検索索引から抽出した価格は返さない
- `POST /api/classify` (`{"title":"...","description":"..."}`): 任意AI分類。`OPENAI_API_KEY`がある場合のみ
- `POST /api/import` (`{"permitted":true,"records":[...]}`): 許可済みデータを検証・正規化。最大1MB・500件
- `POST /api/route` (`{"origin":{"lat":35,"lon":139},"destination":{"lat":35.01,"lon":139},"mode":"foot"}`): 無料道路経路。`foot` / `bike` / `car`。経路なしの場合は `duration_seconds:null`

- `GET /api/session`: create or restore the cookie-bound search session (no API key)
- `POST /api/session`: update with `{"expected_revision":0,"criteria":{"max_price_yen":100000}}`; returns 409 if another update has already advanced the revision
- `GET /api/session/schema`: versioned criteria and partial-update schemas (schema version 2)
- `POST /api/chat`: submit `{request_id,message,expected_revision,language}`; returns a persisted running session immediately
- `GET /api/session`: poll progress, messages, memory and result snapshots; resumes interrupted workers
- `POST /api/chat/resume`: resume an uncertain turn with its `request_id`
- `POST /api/chat/cancel`: stop the current `request_id`
- `POST /api/chat/reset`: clear remote/local chat and memory with `{expected_revision,reset_search}`; `reset_search: true` also resets criteria

## テスト

```bash
python3 -m unittest discover -s tests -v
python3 filter_core.py
node --test tests/*.cjs
```

地図のホイール操作の回帰テストには Node.js 18+ が必要です。アプリの起動には不要です。

## 今後の優先度

1. 物件オーナー・管理会社等の**許可された正式な募集物件フィード**を用意し、実データの更新頻度・確認日時を検証する。接続機能のテストと実在募集データの検証は区別する。
2. 電車の時間条件（料金・提供条件を確認できるデータ源が必要）、ハザード・生活環境レイヤー、複数広告にまたがる同一住戸の照合を追加する。
3. PostgreSQL/PostGIS、差分通知、検索条件保存、AIによる希望条件の補助採点へ拡張する。

詳細なAPI URL・利用条件は [ARCHITECTURE.md](./ARCHITECTURE.md) を参照。OSMタイル・Nominatim・Overpassの利用規約、各掲載サイトの利用規約を守ってください。
