---
name: project-sync
description: リポジトリ衛生管理スキル。Stale Issue（4時間超 in-progress）検出・Orphan PR（24時間超放置）解消・Abandoned ブランチ検出を自動実行する。「project-sync して」「リポジトリを整理して」「/project-sync」で起動する。プロジェクト定義の衛生スロットで自動実行される。受動的な衛生掃除が役割で、Issue 作成・進捗更新など能動的なタスク運用は project-manager を使う。
effort: low
model: haiku
allowed-tools: Bash, Read, Skill, mcp__github__list_issues, mcp__github__issue_read, mcp__github__issue_write, mcp__github__list_issue_fields, mcp__github__list_pull_requests, mcp__github__pull_request_read, mcp__github__merge_pull_request, mcp__github__add_issue_comment, mcp__github__search_issues
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# project-sync スキル

## 目的

GitHub Issues と Projects V2 の整合性を自動修正し、リポジトリの衛生状態を維持するメンテナンス用スキル。
**CP-3（リポジトリの衛生管理）** に基づき、放置された Issue・PR・ブランチを検出・解消する。

以下の問題を定期的に解消する。

| 問題 | 症状 | 影響 |
|------|------|------|
| {ワークフローフェーズ} Issue への {エンティティID} 未採番（任意・採番運用時のみ） | タイトルに `[{エンティティID}]` がない | `{次パイプライン}` が検出できない |
| {バックログ分類}（例: ネタ候補・下書き）に `status:waiting-user` 残留 | ユーザー To-Do ビューにバックログ項目が表示される | ユーザーが不要なタスクを見せられる |
| `Assignee Type` が `status:` ラベルと不整合 | `waiting-claude` なのに Assignee Type = user | ビューの担当者表示が実態と合わない |

## 設計方針

- **ユーザー手動操作ゼロ**: 定期実行で全ての不整合を自動修正する
- **エンティティ ID フィールドはベストエフォート（採番運用するプロジェクトのみ）**: Projects V2 の単一選択フィールドにオプション追加する API が存在しないため、Issue タイトルの `[{エンティティID}]` を権威ソースとする。フィールドは登録済みオプションがあれば設定、なければ静かにスキップ。ID 採番を採用しないプロジェクトでは Step 1 をスキップする
- **Assignee Type は `status:` ラベルに連動**: `waiting-user` → user / `waiting-claude` → claude / `in-progress` → claude / `blocked` → user

## トリガー

- `/project-sync` コマンドで手動実行
- {親スケジューラー}（毎時タスク・プロジェクト定義）に統合済み（自動実行）

## スケジュール設定

**独立したタスクとして登録しない**。
{親スケジューラー}（毎時タスク・プロジェクト定義）のプロンプトに統合されており、以下のようなタイミングで自動実行される（時刻はプロジェクト定義）。

| スロット（例） | 頻度 | 実行内容 |
|------------|------|---------|
| 朝スロット | 毎日 | **workflow-health-check（軽量版）** + ワークフロー開始前の準備（ID 採番・ラベル修正） |
| 昼スロット | 毎日 | **workflow-health-check（軽量版）＋スタック検知** + ワークフロー完了後のクリーンアップ |

統合プロンプトの詳細は {親スケジューラー}の SKILL.md（プロジェクト定義）を参照。

## 実行フロー

> **実行モデル（改訂・#127）**: 全ステップを **Claude が GitHub MCP（`mcp__github__*`）で直接実行する**。
> 中間の Python スクリプト（`tools/sync_project.py` 相当）は存在しない・前提にしない。詳細・gotcha は <!-- refcheck:ignore -->
> **SSOT: `docs/rules/github-mcp-fallback-patterns.md`**（§0〜2.3）を参照。要点:
> - repo スコープの `gh`（REST + GraphQL）はクラウドで 403（L-114）。`gh issue list --jq` 等を一次経路にしない
> - `list_issues(labels=[A,B])` は **OR**（gh の `--label A --label B` は AND）。複数条件は単一ラベルで取得後
>   client-side で AND 絞り込みする（§2.1）
> - `issue_write` の `labels` は **全置換**。現在のラベルを取得してからフルリストを渡す（§2.2）
> - `perPage` は最大 100（gh の `--limit 1000` 相当なし）。100 件超の可能性があれば `pageInfo` でページング（§2.3）
>
> Step 0.5 / 3.5 が参照する `tools/check_pending_pr_reviews.py` は、クラウドで gh 取得自体が失敗した場合
> **exit code 3**（`NO_PENDING_PRS` ではない）で終了する（Issue #130 で計装済み）。exit code 3 のときは
> `mcp__github__list_pull_requests` で直接代替する。

```
Step 0: ワークフロー健全性チェック（軽量版）
  └─ /workflow-health-check --light を実行（Step 1 PR健全性 + Step 2 Issue状態監査）。
     検出条件・アクションの仕様は **workflow-health-check/SKILL.md が SSOT**（本ファイルでは再記述しない・#139）

Step 0.5: レビュー待ちPR復帰の再確認（全実行時）
  └─ workflow-health-check Step 1-d と同一仕様（SSOT: workflow-health-check/SKILL.md）。
     `python3 tools/check_pending_pr_reviews.py --actionable-only` → `needs_response` は
     pr-review-watcher フローに復帰、`awaiting_review` は経過時間に応じて催促 or 引き継ぎ（自動マージはしない）。
     Step 0 の `--light` 実行に含まれるため、本ステップは Step 0 未実行・失敗時の保険として機能する

Step 1: エンティティ ID 採番（採番運用するプロジェクトのみ・未採用ならスキップ）
  ├─ {ワークフローフェーズ} + status:waiting-claude のオープン Issue を取得
  │    → mcp__github__list_issues(state="OPEN", labels=["status:waiting-claude"]) で取得後、
  │      応答の labels 配列に {ワークフローフェーズ} も含む Issue だけを client-side で絞り込む
  │      （labels 引数の複数指定は OR のため・github-mcp-fallback-patterns.md §2.1）
  ├─ タイトルに [{エンティティID}] がない Issue を抽出（{バックログ分類}は除外）
  ├─ 全 Issue（open+closed）から最大 ID 番号を取得（タイトル正規表現 `\[.*?-(\d+)\]` 等で抽出。
  │    件数が多い場合は mcp__github__search_issues(query="repo:{owner}/{repo} in:title \"[\"") でも可）
  ├─ 連番で ID を採番
  └─ Issue タイトルを "[{エンティティID}] {ワークフローフェーズ}: {概要}" に更新
       → mcp__github__issue_write(method="update", issue_number=N, title="...")

Step 2: {バックログ分類}ラベル修正
  ├─ {バックログ分類}の Issue を取得（例: 実際のラベルが `phase:1-agenda` 等の具体的な名前であれば
  │    → mcp__github__list_issues(state="OPEN", labels=["phase:1-agenda"]) のように**実在するラベル名を指定**する。
  │    ラベルにワイルドカードは使えないため、タイトルプレフィックス（例: `[ネタ候補]`）で分類する運用の場合は
  │    status:waiting-user 付き Issue を取得してタイトルで client-side フィルタする）
  ├─ status:waiting-user ラベルが付いているものを検出
  └─ status:waiting-user を除去（バックログ項目はユーザー To-Do に表示すべきでない）
       → ⚠️ `issue_write` の `labels` は全置換（github-mcp-fallback-patterns.md §2.2）。
         対象 Issue の現在のラベル一覧（list_issues の応答 or issue_read(get_labels)）から
         "status:waiting-user" を除いた **フルリスト** を組み立てて渡すこと。
         mcp__github__issue_write(method="update", issue_number=N, labels=[残すラベル一覧])

Step 2.5: waiting-user Issue 自動クローズスキャン（公開完了の検知・プロジェクト定義）
  ├─ status:waiting-user のオープン Issue を全件取得（{バックログ分類}は除外）
  ├─ タイトルに [{エンティティID}] が含まれる Issue のみ処理対象
  ├─ 各 Issue についてプロジェクト定義の完了メタ（例: content/meta/{ID}_*.yaml の公開 URL）を確認:
  │   ├─ 公開 URL が設定済み（非 null・非空文字）→ 「公開完了を確認。自動クローズします」コメント → Issue をクローズ
  │   └─ 未設定 or メタ不在 → スキップ
  ├─ 上限: 1 回の実行で最大 5 件まで自動クローズ（サーキットブレーカー）
  └─ 処理結果サマリーを出力: 「auto-close スキャン: クローズ N件 / スキップ N件」

Step 2.6: waiting-user 誤分類リセットスキャン（user-confirmation-minimization.md §5 連携）
  ├─ status:waiting-user のオープン Issue を全件取得
  ├─ 【レポート系の auto-close】タイトルが定期レポート系（例: [週次レポート] / [月次レポート]）で始まり、更新が 7 日以上前
  │   → 「レポートは Slack 通知済み。7日経過のため自動クローズします」コメント → クローズ（上限3件/回）
  ├─ 【ワークフローフェーズ誤ラベル是正】タイトルが自律実行可能なフェーズ（例: リサーチ実行依頼）を含む
  │   → 担当スキル（例: research-runner）が自律実行可能（C カテゴリ）。status:waiting-user を除去し status:waiting-claude を付与
  ├─ 【ローカル実行依頼の再分類フラグ】本文に「ローカル実行が必要」「ローカル環境で」を含む waiting-user Issue を検出
  │   → user-confirmation-minimization.md §4 のクラウド実行可能リソース表に該当するものは
  │     コメントで「§4 によりクラウド実行を再試行すべき候補。ツール改修 Issue（B カテゴリ）への振り替えを検討」と注記（自動クローズはしない）
  └─ 処理結果サマリーを出力: 「誤分類リセット: report-close N件 / phase-reset N件 / local-flag N件」

Step 3: Projects V2 Assignee Type 同期
  ├─ status: ラベル付きの全オープン Issue を取得（{バックログ分類}除外）
  ├─ 事前に mcp__github__list_issue_fields(owner, repo) で "Assignee Type"（プロジェクト定義の
  │    フィールド名・単一選択）のフィールド名とオプション名を確認する（プロジェクトごとに命名が異なりうる）
  ├─ 各 Issue について:
  │   ├─ Projects V2 未登録 → 追加（Projects V2 への新規追加 API は GitHub MCP に専用ツールがないため、
  │   │    未登録 Issue の検出のみ行い、登録自体は `gh project item-add`（ローカル）または手動を案内する）
  │   ├─ Assignee Type を status: ラベルに基づいて設定
  │   │   ├─ status:waiting-user → Assignee Type = user
  │   │   ├─ status:waiting-claude → Assignee Type = claude
  │   │   ├─ status:in-progress → Assignee Type = claude
  │   │   └─ status:blocked → Assignee Type = user
  │   │   → mcp__github__issue_write(method="update", issue_number=N,
  │   │        issue_fields=[{"field_name":"Assignee Type","field_option_name":"claude"}])
  │   └─ エンティティ ID フィールド: オプションが存在すればベストエフォート設定（採番運用時のみ・
  │        同じく issue_fields で field_option_name 指定。存在しなければ静かにスキップ）
  └─ 同期完了レポートを出力

Step 3.5: Orphan PR 検出（全実行時）— CP-3 準拠
  ├─ オープン PR を全取得（gh pr list / mcp__github__list_pull_requests）
  ├─ 最終更新が 24 時間以上前の PR を「Orphan」と判定
  ├─ check_pending_pr_reviews.py の結果と照合
  ├─ needs_response → pr-review-watcher フローに復帰して指摘対応
  ├─ awaiting_review（24h超）→ 催促 or 問題なし判定 → 自動マージ
  └─ 検出なし → 次の Step へ

Step 3.6: pipeline-state ゾンビ検出・清掃（11:00 スロットのみ）— CP-3 / #2746
  ├─ python3 tools/pipeline_state.py --list-stale --json で status=in_progress + finished_at=null
  │   + 最終更新 4 時間超のレコードを取得（4 時間超の慢性ゾンビは「完了し損ねた」ものとみなす）
  ├─ 1件以上ある → python3 tools/pipeline_state.py --cleanup-all-stale で一括清掃
  │     → 各レコードを status=stale_cleaned + finished_at 設定に遷移
  │     → `{プロジェクト定義: hourly-routing 相当}` の discover 系がスキップし続ける TOCTOU 誤検知を解消
  ├─ 清掃件数を Step 3.9 の衛生レポートメトリクスに含める（pipeline_state_stale_cleaned）
  └─ 0件 → 次の Step へ

Step 3.7: Abandoned ブランチ検出（月曜 07:00 のみ）— CP-3 準拠
  ├─ リモートブランチ一覧を取得
  ├─ main にマージ済みのブランチを検出
  ├─ 7 日間コミットがないブランチを検出
  └─ 削除候補リストをログ出力（自動削除はしない。ユーザー判断に委ねる）

Step 3.9: 衛生レポート出力・永続化（全実行時）— CP-3 準拠 / Issue #2080
  ├─ python3 tools/log_hygiene_snapshot.py --slot "{時刻}-project-sync" --slack を実行
  │     → content/pipeline-state/snapshots/health_YYYY-MM-DD.json に衛生指標を永続化
  │     → content/pipeline-state/run_log.jsonl に実行ログを追記（後追い可能化）
  │     → 滞留閾値超過（waiting-claude > 50 / 最古 > 7日 / Orphan PR）で Slack アラート
  ├─ 出力されるメトリクス: オープン Issue 数・ステータス別内訳・カテゴリ別バックログ
  │     ・最古 waiting-claude 滞留日数・オープン PR 数（Orphan 含む）
  └─ gh 取得失敗時は終了コード 1（「0件」と誤報しない・L-074 / L-086）

Step 3.95: lessons Hot 層サイズ + 物理削除チェック（月曜 07:00 のみ）— #1220 / #2667
  ├─ python3 tools/lessons_guard.py check で Hot 層（lessons-core.md）が上限内か検証
  ├─ python3 tools/lessons_guard.py prune で物理削除候補（昇格済み・実装済み・30日経過）を取得
  ├─ python3 tools/lessons_guard.py dedup で重複統合候補を確認
  ├─ check が exit 1（上限超過）または prune 候補ありの場合:
  │     → python3 tools/lessons_guard.py prune --apply で Hot 層から物理削除（git 履歴に残る）
  │     → Slack 通知: 「lessons Hot 層を整理しました（削除 {N} 件・現 {行数} 行）」
  │
  ├─ check OK かつ prune 候補なしの場合:
  │     → ログ出力のみ（「Hot 層は上限内・整理不要」）。Slack 通知は不要
  │     ※ 詳細・運用ルールの SSOT: docs/rules/lessons-management.md
  │
  └─ 出力サマリー:
       lessons Hot 層チェック結果: 物理削除候補 {N} 件（昇格済み・実装済み・30日経過）

Step 4: 後続処理（実行モードによる）
  └─ 手動実行時のみ: 新たに採番された Issue があれば {次パイプライン}を連続実行（任意）
  └─ {親スケジューラー}毎時タスクから呼ばれた場合: {次パイプライン}は実行しない（このタスクの対象外）
```

## Claude Code による実行手順

### 手動実行

`/project-sync` または「project-sync して」で起動する。**単一のエントリースクリプトは無い**（#127・
実行モデルは冒頭「実行フロー」節を参照）。Claude が Step 0〜3.95 を順に、MCP（`mcp__github__*`）で
直接実行する。ドライラン（変更を加えず検出結果のみ提示）が必要な場合は「project-sync をドライランで」
と明示すれば、Claude が書き込み系ツール呼び出し（`issue_write` 等）をスキップし検出結果のみ報告する。

### 後続処理（手動実行時のみ）

Step 1 で新たにエンティティ ID が採番された Issue がある場合:
1. 採番結果サマリーを確認する
2. 次工程の準備が整った Issue があれば `{次パイプライン}` を実行して後続処理を開始する（手動オペレーション時のみ）

> **注意**: {親スケジューラー}毎時タスクから呼ばれる場合は、Step 4 の {次パイプライン}連続実行は行わない。スケジューラーのプロンプトに「{次パイプライン}を呼び出さないこと」と明記されており、そちらが優先される。

## 検知・修正の対象外

以下は本スキルの対象外。レーン境界の SSOT は `docs/rules/improvement-lane-map.md`
（本スキルは **監査・衛生レーン** の実行側。改善課題の起票・棚卸し・実装は
`self-improvement-loop`、`type:retro-try` の実装は `retro-try-handler` が担当する）。

| ケース | 対応方法 |
|--------|---------|
| バックログ分類の昇格（例: phase:1-* → phase:2-*） | `/refinement` 等のスキルを使う（プロジェクト定義） |
| Issue の Status フィールド更新 | 各パイプラインスキルが担当（プロジェクト定義） |

## サーキットブレーカー

- エンティティ ID 採番は1回の実行で最大 **10 件** まで
- 10 件を超える場合はエラーを出力してスキップし、ユーザーに報告する
- 同一 Issue への重複採番を防ぐため、実行前に最大 ID を必ず再取得する
