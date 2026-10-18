# 募集データの取り込み

掲載元から取り込み・表示を許可されたデータだけを使います。物件サイトの本文を自動取得する機能はありません。検索エンジンの索引はリンク探索に限定し、古い価格や現在不明の募集状態を住戸情報に使いません。

## 入力

JSONはレコードの配列、または `{"records":[...]}`。画面からは同じ列名のCSVも読み込めます。CSVの真偽値は `true` / `false`、不明値は空欄です。最大1MB、500件です。

| フィールド | 意味 |
|---|---|
| `title` | 必須。住戸名など |
| `source_name` | 必須。データ提供元 |
| `source_url` | 必須。利用者が最新状態を確認できるHTTPSの掲載元URL |
| `source_record_id` | 提供元の住戸・広告ID。同じURLでもIDが異なれば別住戸として保持 |
| `kind` | 必須。`rent` / `sale` |
| `availability_status` | `confirmed` のみ採用。提供元が募集中と確認した状態を意味する |
| `last_verified_at` | 必須。提供元が募集を確認した日時。タイムゾーン付きISO 8601（例：`2026-10-18T10:00:00+09:00`）。取得時刻への置換は禁止 |
| `rent_yen` / `sale_price_yen` | 家賃または売買価格。円単位 |
| `management_fee_yen` | 管理費・共益費。円/月。0円と不明を区別 |
| `mandatory_fee_yen` | その他の必須月額費用の合計。円/月。0円と不明を区別 |
| `area_m2`, `age`, `layout` | 面積・築年数・間取り。不明なら省略または `null` |
| `address`, `lat`, `lon` | 掲載元の所在地・座標。座標はJSON数値。CSVは数値に変換 |
| `geo_precision` | `exact` / `building` / `approximate` / `unknown`。厳密な距離条件と経路照会に使うのは `exact` のみ |
| `pets`, `parking` | JSONの真偽値。不明なら省略または `null` |
| `description` | 提供許可のある説明文。最大550文字 |

同じ `source_url` と `source_record_id` の重複だけを除きます。異なる広告が同じ住戸かどうかは自動断定しません。住所や料金が足りない場合も推測で埋めません。

## 鮮度

確認から1時間を超えたレコード、未来・タイムゾーン不明の確認日時、募集状況不明・終了データは採用しません。APIには除外件数を返します。確認日時は提供元の申告であり、アプリによる独立した空室照会ではありません。提供元自身が鮮度を保証できないデータを `confirmed` として送らないでください。

## 自動接続

`LISTINGS_FEED_PATH` に許可済みJSONの絶対パス、または `LISTINGS_FEED_URL` にHTTPS JSONフィードのURLを設定してサーバーを起動します。両方設定した場合はファイルを優先します。検索ボタンを押すたびにデータを再取得し、通常検索のキャッシュは使いません。自動ポーリング・大量取得は行いません。

ファイル、URL、秘密情報をGitに追加せず、私有データはリポジトリ外に置いてください。HTTPサーバーは画面用の3ファイルだけを公開し、フィードやサーバーソースは配信しません。

## 保存候補CSVとの違い

既存の「CSV読み込み」は保存候補の形式（`name`, `price` は万円単位）を維持します。新たに `management_fee_yen`, `mandatory_fee_yen`, `geo_precision`, `availability_status`, `last_verified_at`, `source` を保存します。古いCSVは情報を捨てずに保存しますが、募集の確認日時がない場合は一致候補に採用せず、再確認・編集へ表示します。

空の取り込み用CSVの見出し:

```csv
title,source_name,source_url,source_record_id,kind,availability_status,last_verified_at,rent_yen,management_fee_yen,mandatory_fee_yen,area_m2,age,layout,address,lat,lon,geo_precision,pets,parking,description
```
