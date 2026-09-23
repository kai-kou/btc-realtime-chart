# btc-realtime-chart: リサーチ結果（light）

- 実施日: 2026-09-23 JST
- 深さ: light（要件がユーザーから具体的に提示済みで、論点はデータ源の選定に限られるため）
- 方法: 公式ドキュメントの確認 + 実行環境からの実接続プローブ（REST の HTTP ステータス・WebSocket の受信実測）

## 設問

1. BTC 価格を「よりリアルタイムに」取得できる、認証不要の公開 API はどれか
2. WebSocket で約定・ローソク足を購読でき、初期表示用の過去ローソク足も取れるか
3. 地域制限（geo-block）で利用者が接続できなくなるリスクはあるか

## 結果

| 候補 | 過去足 REST | リアルタイム WS | 実測（2026-09-23） | 備考 |
|---|---|---|---|---|
| Binance（`api.binance.com` / `stream.binance.com`） | klines 最大 1000 本 | kline（2 秒間隔）+ aggTrade（約定ごと） | REST 451・WS 接続失敗 | 実行環境のリージョンで地域制限 |
| Binance 市場データ専用（`data-api.binance.vision` / `data-stream.binance.vision`） | klines 最大 1000 本 | 同上 | REST 200・WS 受信成功 | 市場データ専用の公開エンドポイント（取引 API なし） |
| Coinbase Exchange（`api.exchange.coinbase.com` / `ws-feed.exchange.coinbase.com`） | candles 最大 300 本・4 時間足なし | matches（約定ごと） | REST 200・WS 5 件を 876ms で受信 | 認証不要。ローソク足はクライアントで約定から組み立てる |
| bitFlyer Lightning（`api.bitflyer.com`） | 公式の OHLC REST なし | executions（約定ごと・JSON-RPC） | ticker REST 200 | 円建てだが初期表示の過去足が取れない |

出典:

- Binance Spot WebSocket Streams: https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
- Binance Market Data Only エンドポイント: https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
- Coinbase Exchange WebSocket Feed: https://docs.cdp.coinbase.com/exchange/websocket-feed/overview
- Coinbase Exchange Get product candles: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles

## 影響

- `D-2`: 主データ源は Binance（市場データ専用エンドポイント）の aggTrade + kline 併用。約定ごとに形成中の足を更新でき、確定足は kline で上書きできる
- `D-3`: Coinbase Exchange を自動フェイルオーバー先にする（地域制限・障害時の継続性）
- `Q-1`: 円建て表示（bitFlyer）は過去足の取得手段が無いため今回は見送り
