---
name: workflow-health-check
description: 開発・運用ワークフローの健全性（PR 健全性・Issue 状態・パイプライン整合性・retro-try フィードバックループ・CLAUDE.md/常駐ルール肥大化）を自動監査し、問題を検出したら根本原因を特定して自動修正するスキル。「ワークフロー監査して」「ヘルスチェックして」「/workflow-health-check」と依頼された時に使用する。完全版は手動起動またはプロジェクトの定期ルーティンに組み込んだ週次ゲートから、軽量版（PR 健全性 + Issue 状態のみ）は日次の衛生スロット（project-sync 開始時）から実行される。監査ロジックの主体は本スキルで、project-sync は本スキルの軽量版を呼び出す側（主従関係）。
effort: low
allowed-tools: Bash, Read, mcp__github__list_issues, mcp__github__issue_read, mcp__github__issue_write, mcp__github__add_issue_comment, mcp__github__list_pull_requests, mcp__github__pull_request_read, mcp__github__actions_list, mcp__github__create_pull_request, mcp__github__merge_pull_request, mcp__Claude_Code_Remote__list_triggers
---

# workflow-health-check スキル

## 目的

開発・運用ワークフローの健全性を自動監査し、問題を検出したら根本原因を特定して自動修正する。
定期的なフロー改善サイクル（検出 → 修正 → 記録 → 再発防止）を自律的に継続する。

**詳細手順（完全版限定の Step 3〜6・週次レポート雛形・実行コマンド例）は `reference.md`** を参照。

## 設計方針

- **自律的フロー改善**: 問題を検出するたびに根本原因を分析し、再発防止策を Issue に記録する
- **多層防御**: PR 健全性・Issue 状態・パイプライン整合性の 3 層で独立して監査
- **サーキットブレーカー**: 自動修正は安全な範囲に限定（ラベル変更・コメント・条件付きの PR クローズ〔例: 重複 PR かつ古い方のみ、コメント後 24h 経過など〕）。コード変更・マージ・削除は行わない
- **透明性**: 検出・修正の全アクションを GitHub コメント・Slack 通知で記録する

## トリガー

| モード | 実行タイミング | 実行内容 |
|--------|-------------|---------|
| **完全版**（`/workflow-health-check`） | 手動 or プロジェクトの定期ルーティンに組み込んだ週次ゲート（前回実行から 7 日経過で判定） | Step 1〜6 全て実行（Step 3〜6 は `reference.md`） |
| **軽量版**（`/workflow-health-check --light`） | 日次の衛生スロット（プロジェクト定義・project-sync 開始時） | Step 1〜2 のみ（PR健全性 + Issue状態） |

## 実行フロー

### ルールファイル読み込み（トークン最適化対応）

以下のルールファイルを `docs/rules/` から Read する（`.claude/rules/` から削除済みのため自動読み込みされない）。

- `docs/rules/{プロジェクト定義の成果物バリエーション判定ルール}.md`（あれば・例: 繰り返しコンテンツ判定ルール）
- `docs/rules/self-review-checklist.md`（過去のレビュー学習内容。旧ファイル名は廃止済みのため参照しない）

---

### Step 1: PR 健全性監査

```
1-a: ベースブランチ検証
  └─ オープン PR の base ブランチが main またはプロジェクト定義の「正規ブランチ」（例: content/{ID}-*）か確認
  └─ base が「マージ済み feature ブランチ」（例: claude/add-xxx）の場合 → 警告 + コメント投稿

1-b: マージ可能性チェック
  └─ GitHub REST API の `mergeable_state` または GraphQL の `mergeStateStatus` を取得してマージ可能性を判定
  └─ mergeable_state / mergeStateStatus が dirty / unknown（コンフリクト or 判定不能）の PR を検出
  └─ コンフリクト有り → PR にコメント投稿（解消方法のガイダンス付き）
  └─ 48h 以上放置のコンフリクト → Slack 通知 + status:blocked ラベル付与

1-c: 放置 Draft PR 検出
  └─ 24h 以上更新のない Draft PR を検出
  └─ 作成者に「継続する場合は Draft を解除してください」コメント投稿

1-d: レビュー待ち PR 復帰
  └─ python3 tools/check_pending_pr_reviews.py --actionable-only を実行
  └─ needs_response → pr-review-watcher フローに復帰（指摘対応）
  └─ awaiting_review → 経過時間チェック → 催促 or 問題なし判定 → pr-review-watcher への引き継ぎ（自動マージは行わない）
  └─ ⚠️ クラウドで gh 取得自体が失敗した場合、本スクリプトは `NO_PENDING_PRS`（exit 0）ではなく
     **exit code 3** で終了する（「0件」と誤解釈しない・Issue #130）。exit code 3 のときは
     mcp__github__list_pull_requests(owner, repo, state="open") で直接オープン PR を確認する
     （詳細: docs/rules/github-mcp-fallback-patterns.md §4）
```

### Step 2: Issue 状態監査

```
2-a: スタック Issue 検出（status:in-progress）
  └─ 最終更新から 4h 超の in-progress Issue を検出
  └─ コメント投稿: 「セッション中断の可能性。リセットします。」
  └─ status:in-progress → status:waiting-claude にリセット

2-b: パイプライン中断検出
  └─ 以下のような組み合わせを検出（各フェーズ間の整合性・プロジェクト定義）:
     - 先行フェーズ Issue が waiting-claude なのに後続フェーズ Issue が in-progress
     - 後続フェーズ Issue が waiting-claude なのに先行フェーズ Issue が未クローズ
  └─ 中断疑いの Issue に「前フェーズの完了確認を」コメント + Slack 通知

2-c: ラベル不整合検出
  └─ status: ラベルが 2 つ以上付いている Issue を検出
  └─ 最新の状態に合わせて余分なラベルを除去（古いラベルを削除）

2-d: 長期滞留 Issue 検出
  └─ status:waiting-user で 7 日以上更新なし → Slack メンション通知（1 Issue につき週 1 回まで）
  └─ status:waiting-claude で 24h 以上更新なし → status:waiting-claude のまま Slack 通知

2-e: 外部認証情報（Cookie / トークン）期限切れ検出（完全版のみ・プロジェクト定義。該当しないプロジェクトはスキップ）
  └─ 配信先（プロジェクト定義）の投稿に使う認証情報ファイル（例: ~/.{repo}/*_cookies.json）の最終更新日時を取得する
  └─ ファイル不在 → スキップ（クラウド環境で認証情報がない場合は正常）
  └─ 期限が近い（例: 最終更新から 25 日超）→ Slack 警告: 「⚠️ 認証情報の期限が近い可能性。再認証手順（プロジェクト定義）を実行してください」
  └─ 期限切れ疑い（例: 30 日超）→ Slack 緊急通知: 「🚨 認証情報期限切れの可能性。自動公開が失敗する場合があります」

2-f: 事前生成成果物のタイトル健全性チェック（軽量版でも実行・プロジェクト定義。該当しないプロジェクトはスキップ）
  └─ 配信先のドラフト（例: 記事 H1 / frontmatter）に管理用プレースホルダ（実タイトル未確定の管理タイトル）が
     残っていないか検知する（実タイトル確定済みなのに管理タイトル＝再発）。検査スクリプトはプロジェクト定義（例: tools/fix_*_titles.py --check）
  └─ exit 0（再発なし or 実タイトル未確定の過渡状態のみ）→ 何もしない
  └─ exit 1（実タイトル確定済みなのに管理タイトル）→ Claude が自律修正（CP-1・ユーザー確認不要）:
     プロジェクト定義の修正スクリプトを実行 → コミット & push → PR → 自動マージ。管理タイトルでの公開を未然に防ぐ

2-g: アナリティクス計測停滞検出（軽量版でも実行・プロジェクト定義）
  └─ プロジェクト定義の鮮度チェックスクリプト（例: tools/check_analytics_freshness.py --slack --auto-recover）を実行し、分析スナップショット（例: content/analytics/snapshots/）のファイル名の日付プレフィックスを取得
     （git checkout で揺れる mtime ではなくファイル名の YYYY-MM-DD を権威ソースとする）
  └─ 全配信先（プロジェクト定義）のうち最新ファイルが 7日超 → Slack WARN: 「⚠️ snapshots 7日超未更新。収集スクリプトの定期実行を確認してください」
  └─ 14日超 → Slack CRITICAL: 「🚨 KPI計測パイプライン停止。週次PDCAが CRITICAL 誤判定する可能性」
  └─ 24h 以内に同一 worst_status の通知を出していたら無音スキップ（重複抑制・throttle）
  └─ --auto-recover 指定時はプロジェクト定義の収集スクリプトを 1 回だけ手動実行して復旧を試み、成功したら鮮度を再評価して exit code・report を更新する（成功時は Slack に「自動復旧成功」を報告）
  └─ type:bug Issue の自動起票は本ステップのスコープ外（Slack 通知のみ。Issue 化が必要な場合は手動 or self-improvement-loop で対応）

2-h: owner ロール権限逸脱の事後監査（完全版のみ）
  └─ 対象範囲: 直近 7 日以内に更新されたオープン Issue のコメント
  └─ 上記範囲内のコメントから `(owner)` プレフィックスのコメントを検出する
  └─ 該当コメント本文に `status:` 文字列が含まれる（sp:/priority: 以外のラベル操作を示唆）場合、
     境界逸脱の疑いとして Issue にコメント投稿（「owner ロールの権限境界（sp:/priority: のみ許可）
     を逸脱した可能性。要確認」）
  └─ 本ステップは `.claude/agents/owner.md` の自己申告コメント（操作記録義務）に依拠するため、
     コメントが省略された逸脱は検出できない（既知の限界。機械強制は #150 の議論で却下済み）

2-i: オーファン Issue 検出（どの自動レーンにも拾われない Issue・軽量版でも実行）
  └─ 目的: 自動消化レーンの対象スコープの和集合がオープン Issue 全体を覆っているかを検証する
     （覆えていないと「対応対象ゼロ」と報告されるのに在庫が永久に減らない・#335）
  └─ 手順:
     1. mcp__github__list_issues(state="OPEN") でオープン Issue を全件取得する
     2. 「正当な除外」を先に取り除く: status:in-progress（他セッション対応中）・status:waiting-user
        （A-1〜A-6 でユーザー判断待ち）・status:blocked（手順 2 (b) の再評価レーンが引き取る）。
        これらは意図された除外であり、オーファンではない
     3. docs/routines.md に記載された各ルーティンの対象スコープを読み、和集合を求める。
        スコープの定義を本ファイルに再掲しない（docs/routines.md が唯一の正。ここに写すと
        スコープ変更時に本ステップだけ古くなる）
     4. 残った Issue のうち、どのスコープにも入らないものを「オーファン」として列挙する
        （スコープの和集合が残り全体を覆っていればオーファンはゼロになる。1 件でも出たら
        スコープ定義かラベルのどちらかが壊れている合図）
  └─ オーファンあり → 原因別に自律対処（CP-1・ユーザー確認不要）:
     - type: / sp: ラベル欠落が原因 → ラベルを補完する（R-1 手順 2 (c) と同一ポリシー。
       ポリシー変更時は docs/routines.md と本ステップを同一 PR で更新すること）
     - スコープ定義自体が部分集合 → type:bug Issue を起票し、docs/routines.md の
       カバレッジ不変条件（「各レーンの対象スコープの和集合 ＝ オープン Issue 全体」）違反として記録する
  └─ status:blocked が最終更新から 7 日超 → R-1 手順 2 (b) の再評価が機能しているか確認し、
     R-1 の 2 周期（4 時間 × 2 = 8 時間 + ジッター）を過ぎても再評価コメントが付いていなければ
     Issue にコメントして注意喚起する
  └─ 本ステップは「レーンが仕事をしたか」ではなく「レーンの守備範囲に穴がないか」を見る
     （2-a〜2-d が個別 Issue の状態を見るのに対し、本ステップは構造を見る）
```

### Step 3〜6（完全版のみ・週次ゲート通過時に限る）

パイプライン整合性監査・根本原因記録・フィードバックループ健全性チェック・CLAUDE.md/常駐ルール肥大化監査は
`reference.md` に定義する（軽量版では読み込まない）。完全版起動時は `reference.md` を Read してから
Step 3 以降を実行する。週次レポートフォーマット・実行コマンド例も同ファイルを参照。

## 自動修正の安全範囲（全ステップ共通・軽量版の Step 1〜2 にも適用）

| 操作 | 許可 | 理由 |
|------|------|------|
| ラベル追加・削除 | ✅ | リバーシブル |
| GitHub コメント投稿 | ✅ | 情報提供のみ |
| Slack 通知送信 | ✅ | 情報提供のみ |
| Phase 移行 Issue 作成 | ✅ | 漏れ補完 |
| retro-try Issue 作成 | ✅ | 記録のみ |
| 重複 retro-try Issue の duplicate クローズ | ✅ | メイン Issue にコメント追記後にクローズ（Step 5-b・`reference.md`） |
| 候補台帳 Issue（`[Retro][ledger]`）のクローズ・統合 | ❌ | `retrospective` が管理（health-check は Step 5-e で存在・重複を報告するのみ） |
| PR クローズ | ⚠️ 条件付き | 重複 PR かつ古い方のみ。コメント投稿後 24h 経過が条件 |
| コード変更・コミット | ❌ | セルフレビューまたはユーザーが対応 |
| PR マージ | ❌ | pr-review-watcher が担当 |
| Issue クローズ | ❌ | ユーザーまたは各パイプラインが担当（例外: 重複 retro-try の duplicate クローズは上記の通り許可） |

## サーキットブレーカー

- **1 回の実行で作成する Issue は最大 5 件** まで（過剰なノイズ防止）
- **Slack 通知は同一 Issue につき 24h に 1 回まで**
- **連続失敗 3 回でスキップ**（API エラー等）: 「ヘルスチェック一時停止」を Slack 通知して終了
- **スタック Issue の自動リセットは 1 回の実行で最大 10 件** まで
- **重複 Issue の統合は 1 回の実行で最大 3 グループ** まで（Step 5-b・`reference.md`）

## 統合先

> レーン境界の SSOT は `docs/rules/improvement-lane-map.md`。本スキルは **監査・衛生レーン** の
> 監査ロジック本体で、検出した課題の **実装** は改善 Issue レーン（`self-improvement-loop`）が担う。

| 実行タイミング | 統合先スキル | 実行モード |
|-------------|------------|----------|
| 週 1 回（前回実行から 7 日経過をゲート判定） | プロジェクトの定期ルーティンに組み込む（構成記録はプロジェクト側に置く） | 完全版 |
| 日次の衛生スロット（プロジェクト定義） | project-sync（Step 0 として） | 軽量版 |
| 改善 Issue レーンの発見モード（Step 0.5） | self-improvement-loop | 軽量版 |
| 手動 | `/workflow-health-check` コマンド | 完全版 |

## 禁止事項

- PR のコードを自動的に修正してコミットしない
- マージ済みブランチを削除しない
- Issue を自動クローズしない（ユーザーまたは各パイプラインが担当）
- Slack 通知を 24h 以内に同一内容で重複送信しない
