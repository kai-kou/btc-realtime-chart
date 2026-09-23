# BTC Realtime Chart

BTC の価格変動をリアルタイムに確認できる、トレーディング参考用の Web アプリ。

- 公開 URL: https://btc-realtime-chart.kinamocchi-tech.workers.dev
- ローソク足 + ボリンジャーバンド（20, 2σ）+ 出来高 + RSI(14)
- 確定足で判定する売買シグナル（BUY / SELL マーカー）。判定ルールは画面の「シグナルの判定ルール」と `docs/product-brief.md` `D-6`
- 取引所の WebSocket に直接接続し、約定ごとに形成中の足を更新する（Binance 市場データ専用エンドポイント。使えないときは Coinbase へ自動で切り替える）
- 最新の足への自動追従・「最新へ」ボタン・自動再接続（無通信監視つき）・タブ復帰時の欠損足の取り直し・画面スリープ防止

> 本ツールはトレーディングの参考情報であり、投資助言ではありません。

## 開発

```bash
npm ci
npm test          # ドメイン層・再接続・フェイルオーバーのユニットテスト（node:test）
npm run build     # dist/ に静的アセットを生成（esbuild）
npm run deploy    # Cloudflare Workers へデプロイ（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID が必要）
node e2e/smoke.mjs https://btc-realtime-chart.kinamocchi-tech.workers.dev/   # 受け入れ基準の E2E 確認（Playwright）
```

## ドキュメント

- 要件: `docs/product-brief.md`
- アーキテクチャ（層構造・依存規則・用語）: `docs/architecture.md`
- データ源の調査: `content/research/btc-realtime-chart_deep_research.md`

本リポジトリは [kai-kou/tsukuru](https://github.com/kai-kou/tsukuru) を `apply-to-repo.sh` で適用した下流プロジェクトです。
