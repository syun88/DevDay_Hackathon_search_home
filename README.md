# Search Home — 地図 × 物件検索 × 通勤圏

住所・会社名・駅名から起点を検索し、指定した徒歩時間の道路網と周辺の建物・物件候補を地図上で比較するローカルWebアプリ。**実物件を架空データで補完しません。**

ヘッダーの **EN / JP** ボタンで表示言語を切り替えられます。選択はブラウザーに保存されます。住所・掲載元の文章・検索条件は元の言語を維持します。

Use **EN / JP** in the header to switch the interface language. Your choice is remembered in this browser; addresses and source listing text stay in their original language.

## 起動

Python 3.10+ が必要（追加パッケージ不要）。

```bash
python3 server.py
# http://127.0.0.1:8765/
```

Linux: `./start-linux.sh` / Windows: `start-windows.bat` でも起動可能。

任意のAPIキー:

```bash
BRAVE_SEARCH_API_KEY=... OPENAI_API_KEY=... python3 server.py
```

- **APIキーなし**: 住所・駅・会社検索（Nominatim/GSI）、OSMタイル、Valhallaによる徒歩道路網・到達圏、Overpassによる建物探索、2地点の直線距離、候補CSV・絞り込み、Bing RSSによる掲載URL探索（ベストエフォート）
- **Braveのキーあり**: 安定的なWeb掲載ページの発見（課金・クォータ依存）。SUUMO／HOME'S／at homeの本文スクレイピングは行わない。
- **OpenAIのキーあり**: 検索で見つかった掲載ページの種類をボタンで分類（Decisions API / `gpt-6-luna`）。利用者が操作したときのみ呼び出し、空室確認には使わない。

**重要**: 検索エンジンの抜粋から推定した家賃・間取り・住所は未確認。住所検索ピンは概略位置となる場合があります。OSMの建物データは募集中の住戸ではありません。実際の募集・所在地・家賃は必ずリンク先で確認してください。検索結果が0件の場合は0件と表示します。無許可の物件サイト自動クロールは行いません。

## 徒歩範囲の表示

- 初期表示は**徒歩15分**。5〜60分のスライダーと5・10・15・30・60分のボタンで変更できます。「範囲の指定方法」で従来の直線半径にも切り替えられます。
- **色付きの線は、OSMの道路・歩道に沿って時間内に到達した区間**です。Valhallaの `pedestrian` 探索から、確定した区間のうち累積所要時間が指定時間以内のものだけを表示します。川・堀・線路敷などを含めた外周全体の塗りつぶしは行いません。橋や地下道など、地図に徒歩経路が登録されている区間は表示します。フェリーへの乗船は検索の上限60分を超える設定で除外し、起点をフェリー航路へ接続しないようにしています。線幅は視認性のためのもので、道路の実幅ではありません。
- 破線は等時間圏を補間した**外周の参考線**です。保存候補・建物・掲載ページの範囲条件はこの外周内かどうかで概略判定します。個々の建物入口までの徒歩時間の確定判定ではありません。地点Bも有効にするとA・B両方の外周内にある候補を表示します。
- 道路の総延長を地図上に、外周の参考面積をサイドバーに表示します。参考面積は川・建物等を除いた「歩ける地面の面積」ではありません。徒歩速度は4.8km/h（80m/分）。信号待ち・最新の通行規制・建物入口などにより実際の所要時間は変わります。候補カードの距離・近い順は直線距離です。
- 地点・時間の変更時は古い表示を直ちに無効化します。取得失敗時は再取得ボタンを表示し、未取得の範囲を一致扱いにしません。道路網が取得できない場合に円や外周の塗りつぶしへ自動置換することはありません。

徒歩機能には同梱サーバーとインターネット接続が必要です。既定では[FOSSGISの公開Valhallaデモサーバー](https://github.com/valhalla/valhalla#demo-server)を使用するため、利用制限・停止の影響を受けます。60分圏など広い範囲の道路網取得には1分以上かかる場合があります。操作を450msまとめ、サーバーからの照会を直列化して1秒以上間隔を空け、同一条件を15分キャッシュします。取得済みの条件への切り替えは広域照会の完了を待ちません。本番公開時は提供者のfair-use方針・アプリの連絡依頼を確認し、自前のValhallaなどへ切り替えてください。

```bash
VALHALLA_URL=http://127.0.0.1:8002 python3 server.py
```

連携仕様：[Isochrone API](https://valhalla.github.io/valhalla/api/isochrone/)、[Expansion API](https://valhalla.github.io/valhalla/api/expansion/)。Expansion APIはBETAです。建物探索は負荷を抑えるため周辺最大5km・取得最大220件に制限し、取得範囲が足りない場合は画面に表示します。

## 構成

- `index.html`: レスポンシブ地図UI（依存ライブラリなし）、地点A/B、徒歩道路網・距離円、外部検索、候補保存、CSV
- `walking.js`: GeoJSON描画・範囲判定・道路延長・参考面積、取得の間引きと古い応答の排除
- `server.py`: localhost専用HTTPサーバー、徒歩API・キャッシュ、Bing RSS/Brave検索、正規化と出典、任意Decisions API分類
- `filter_core.py`: 複数地点AND/ORと月額費用の厳密フィルタ参照実装（不明値は除外）
- `ARCHITECTURE.md`: 設計全体、データ源・公式API、PostGIS／経路／ハザード／通知への拡張方針
- `tests/`: 抽出、URL制約、距離、未知値・境界条件、AI通信モック検証

## API

- `GET /api/health`: 機能・APIキー設定状態（キー自体は返さない）
- `GET /api/walking-area?lat=35.681236&lon=139.767125&minutes=15`: 外周のPolygon/MultiPolygonと時間内に到達した道路のMultiLineString、徒歩速度・起点・出典。緯度±85度、経度±180度、時間5〜60分の整数に制限。不正入力は400、上流通信・応答の異常は502。
- `GET /api/listings?place=渋谷区&kind=rent&provider=auto&layout=1K`: 公開検索エンジンのリンクと未検証フィールド
- `POST /api/classify` (`{"title":"...","description":"..."}`): 任意AI分類。`OPENAI_API_KEY`がある場合のみ

## テスト

```bash
python3 -m unittest discover -s tests -v
python3 filter_core.py
node --test tests/*.cjs
```

地図のホイール操作と徒歩範囲の回帰テストには Node.js 18+ が必要です。アプリの起動には不要です。自動テストは公開の経路サービスへ通信しません。

## 今後の優先度

1. 物件オーナー・管理会社等の**許可された正式な募集物件フィード**を接続し、空室ステータスを保証できるデータを増やす。
2. Google Routes／駅すぱあとによる移動時間条件、国交省 不動産情報ライブラリによるハザード・生活環境レイヤー、重複住戸統合、費用内訳を追加する。
3. PostgreSQL/PostGIS、差分通知、検索条件保存、AIによる希望条件の補助採点へ拡張する。

詳細なAPI URL・利用条件は [ARCHITECTURE.md](./ARCHITECTURE.md) を参照。OSMタイル・Nominatim・Overpassの利用規約、各掲載サイトの利用規約を守ってください。
