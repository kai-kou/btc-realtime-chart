---
name: pr-review-watcher
description: PR 作成後のレビュー監視・指摘対応・自動マージを自律実行するスキル。「PR を監視して」「レビュー対応して」「PR をマージまで見届けて」「レビュー待ち PR を回収して」と依頼された時、PR 作成フローの直後、またはセッション復帰時に check_pending_pr_reviews.py がレビュー待ち PR を検出した時に必ず使用する。Layer 1 セルフレビュー（自前 code-review スキル。組み込み /code-review を同名 project スキルで置換済み・自律起動可）を必ず実行し、指摘対応 → 自動マージまでを自律実行する。外部 AI レビュアー（Copilot/Gemini）への依頼はしない。CI・人手コメントは subscribe_pr_activity で任意監視する。対応はサイレント（ユーザー報告せず PR スレッド・Issue 記録のみ・L-102）。
effort: medium
---

# PRレビュー監視・自動対応スキル

PR 作成後に **Claude 自身が Layer 1 セルフレビュー（自前 `code-review` スキルを `Skill(code-review)` で
起動）を必ず実行** し、指摘対応 → 自動マージまでをユーザー指示なしで自律実行する。組み込み
`/code-review` は disable-model-invocation で自律起動不可のため、同名 project スキル
`.claude/skills/code-review/` が bundled を置換している（自律起動可・#275 → #280）。
**外部 AI レビュアー（Copilot / Gemini）への
レビュー依頼は行わない**（Copilot 依頼廃止・Gemini は 2026-07-17 廃止済み）。CI 結果・人手コメントは
`subscribe_pr_activity` で任意に監視する。**詳細手順（Step 1-7・GraphQL・トラブルシューティング・
フィードバックループ）は `reference.md`**、フロー全体の正本は **`docs/rules/pr-review-flow.md`**、
レビュアー構成の SSOT は **`docs/rules/ai-reviewer-strategy.md`** を参照。

## トリガー条件

- PR 作成後に AI レビューの到着を待つ時（PR 作成フローの一部として自動開始）
- **セッション復帰時**: `tools/check_pending_pr_reviews.py` がレビュー待ち PR を検出した時

## Layer 2 レビュー自動起動（Issue #97・ネイティブ化 #193）

PR 作成・AI レビュー依頼の直後に `discussion_review_trigger.py`（要否判定器）を呼び出す。
差分 ≥300行 / `type:security`・`type:breaking-change` ラベル / `high_risk` 差分（#627）の PR には
自動的に Layer 2 議論型レビューを追加実行する。

クラウド環境（gh CLI 不可）では `mcp__github__pull_request_read` で取得した値を渡す:

```bash
# クラウド環境: mcp__github__pull_request_read(method="get") の結果を使う
python3 tools/discussion_review_trigger.py \
  --pr {PR番号} \
  --diff-lines {additions + deletions} \
  --labels "{label1},{label2}" \
  --changed-files "{file1.py},{file2.md}"

# ローカル環境（gh CLI 有効時）: PR 番号のみ
python3 tools/discussion_review_trigger.py --pr {PR番号}
```

- Layer 2 不要と判定された場合: `ℹ️ Layer 2 レビュー不要` を出力して skip
- **トリガー該当時**: 実行プラン JSON（id / spec / targets / rounds）が出力される。そのプランに従い
  **`discussion-review` スキル（ネイティブ Agent Teams・既定）** で議論型レビューを実行する
- ネイティブ実行が成立しない場合のみ、プランの `fallback_command`（`--legacy` = 旧 claude -p 経路）へ退避する（理由をログ）
- Layer 2 失敗時: stderr に警告を出力し Layer 0+1 で継続（サイレントフォールバック禁止）
- 詳細: `docs/rules/ai-reviewer-strategy.md` を参照

## 使い方（ユーザー指示不要・自動実行）

PR 作成後、指示を待たずにセルフレビュー → マージまで進める:

```
1. PR 作成（本文に Session-Id: $CLAUDE_CODE_SESSION_ID を必ず記載・#47 所有判定の前提）
2. Layer 1 セルフレビューを必ず実行: 自前 code-review スキルを Skill(code-review) で起動する
   （.claude/skills/code-review/ が組み込みを置換・観点別ファインダー並列 → 敵対的検証 → 報告）
   ❌ Copilot 依頼（request_copilot_review / --add-reviewer @copilot）・Gemini 依頼（/gemini review）はしない
3. 指摘対応（修正コミット or スキップ + 返信 + Resolve）→ `check_pending_pr_reviews.py --verify-layer1 <PR番号>` で Layer 1 投稿済みを機械検証（#462）→ Layer 0+1 通過で自動マージ
4. （任意）subscribe_pr_activity で CI / 人手コメントを監視
5. セッションが切れたら → 次セッションで check_pending_pr_reviews.py --mine が自 PR を識別 → 復帰
```

> **自セッション作成 PR の回収（#47）**: 復帰時はまず `check_pending_pr_reviews.py --mine --actionable-only --json` で
> **自セッションが作成した PR のみ** を最優先で責任継続する（Session-Id トレーラーによる積極的所有判定）。
> 自 PR は時間ベースの `active_session` 除外を受けないため、10 分超アイドル・再起動・圧縮後でも確実に回収できる。
> その後に `--mine` なしの共有スコープで孤児 PR を救済する。詳細は `docs/rules/session-concurrency-rules.md` レイヤー 6。

> 監視中はタスク依存ルールを `docs/rules/` から Read する: `docs/rules/self-review-checklist.md`（セルフレビュー観点）/
> `docs/rules/lessons/pr-review.md`（PR レビュー・マージの過去ミスパターン）。

## 監視方式: subscribe_pr_activity + ハートビート（推奨）

`subscribe_pr_activity` でイベント購読し、同時に `pr_review_heartbeat.sh` をバックグラウンド起動して
`Monitor` でストリームする（クラウドの 10 分タイムアウトを防止）。

```
# ① イベント購読を開始（イベント駆動）
mcp__github__subscribe_pr_activity(owner="kai-kou", repo="tsukuru-studio", pull_number={pr_number})
# ② ハートビートをバックグラウンド起動（セッション維持・5分間隔）
Bash(run_in_background=true): bash tools/pr_review_heartbeat.sh {pr_number} 30  # → PID を控える
# ③ Monitor でハートビート出力をストリーム（各行が通知としてセッションを維持）
Monitor(pid={HEARTBEAT_PID}, description="PR #{pr_number} ハートビート")
```

- ハートビートは 5 分ごとに `check_pending_pr_reviews.py` で状態確認し stdout 出力（アイドルタイムをリセット）
- `ready_to_merge` 検出で 🚀、マージ済みで ✅ を出力して自動終了、`max_minutes`（既定 30）経過で ⏰ 終了
- `<github-webhook-activity>` タグのレビューイベントとハートビート通知を並行処理する
- sleep ポーリングは禁止（イベント駆動 + 定期出力で両立）

> subscribe が使えない環境のフォールバック: `bash tools/poll_pr_reviews.sh kai-kou/btc-realtime-chart {pr_number} /tmp/pr_review_{pr_number}.json`

## 監視タイムライン（PR 作成時刻基準）

```
0分   : PR 作成 → Layer 1 観点別フレッシュ文脈セルフレビューを必ず実行
        指摘対応（修正コミット or スキップ + 返信 + Resolve）
Layer 0+1 通過後 : 即自動マージ（外部レビュアー応答待ちなし）
任意   : subscribe_pr_activity で CI / 人手コメントを監視。あれば対応してからマージ
        └─ A-1〜A-6 該当（サーキットブレーカー発動等）時のみユーザー報告
```

## ステップ概要（詳細は reference.md）

| Step | 内容 |
|------|------|
| 1 | Layer 1 セルフレビュー実行（自前 `code-review` スキル・`Skill(code-review)`）+ 既存レビュー状態の取得。**CONFIRMED は PR の行単位インラインコメントで記録し（NIT は上限 3 件）、PLAUSIBLE と上限超 NIT は本文に集約する。指摘ゼロでも `event="COMMENT"` のレビューを 1 件投稿する**（#461 → #627・手順は code-review スキル Step 3-A） |
| 2 | 指摘の分類（修正対象 / スキップ）。CI 失敗・人手コメントの有無を確認 |
| 3 | 指摘への自動対応（修正コミット or スキップ → スレッド返信 → **Resolve 必須**） |
| 4 | Layer 0（機械ゲート）+ Layer 1 通過の確認。**`check_pending_pr_reviews.py --verify-layer1 <PR番号>` で Layer 1 投稿済みかを機械検証**（#462・挙動は reference.md Step 4） |
| 5 | 自動マージ（squash・外部レビュアー応答待ちなし） |
| 6 | **公開リポジトリへの反映（`publish-sync`）**。マージで生まれた差分をこのセッション内で反映まで完遂する。詳細は下記 |
| 7 | レビュー完了サマリーを **PR スレッドのみ** に記録（サイレント・L-102） |
| 8 | マージ後フィードバックループ → `docs/rules/lessons/pr-review.md` に教訓追記（必須・`lessons-management.md` に従う） |

### Step 6: マージ直後の公開反映（#449）

マージした瞬間が、公開リポジトリとのドリフトが生まれる瞬間である。**反映をセッション終了時や次回の
定期ルーティンに先送りしない**（先送りが実際に 13〜17 時間の滞留を生んだ・#449）。

- `post-merge-publish-check.sh`（PostToolUse・matcher `mcp__github__merge_pull_request`）が
  マージ成功を検知してドリフトを判定し、反映が要るときだけ指示を注入する。指示が来たら
  `publish-sync` スキルに従って **push まで完遂する**（ユーザー確認は不要）。
- **判定は「クローン済みか」で行う**（実行モードでも sources の設定値でも決め打ちしない）。配布物リポジトリが
  sources に登録されていれば、scheduled trigger のような自動タスク実行モードでも push できる場合がある。
  `add_repo` が無いこと（`L-117`）は **実行中の動的追加** の制約であって、事前登録済みリポジトリへの到達を
  必ずしも妨げない。🔴 **ただし sources 登録は「毎回届くこと」までは保証しない** — 同じ設定のまま、供給される
  発火と供給されない発火が混在した実例がある（本リポジトリの R-1・`#96`）。設定を根拠にせず作業ディレクトリを見る。
- **反映できないセッションでも黙って終わらせない**。作業ディレクトリに配布物リポジトリが無い
  （sources 未登録・クローン失敗）ときは、**push を試して 403 を踏む前に** `publish-sync` スキルの
  §3（反映できなかったときの記録）に従って `[publish-sync]` Issue に失敗段階を記録する。
- ローカル実行で `gh pr merge` を使った場合はフックの matcher（MCP ツール）に掛からない。
  その経路は **次のセッション冒頭**（`session-start.sh` が注入する回収指示）と **R-1 のプリフライト**
  （`docs/routines.md` §2.0）が拾う。セッション終了時の backstop フックは本リポジトリには無い。

## サイレント原則（L-102・最重要）

AIレビュー指摘対応は **ユーザーに報告しない**。記録は PR スレッド返信・Resolve・Issue コメントのみ。
チャット逐次報告・Slack `@mention`・完了報告アウトカムへのレビュー対応混入は禁止。
例外は A-1〜A-6（サーキットブレーカー発動・ファクト致命的 NG 等）のみ。
マージ後の完了報告は `docs/rules/completion-report-rules.md`（SSOT）に従う。

## 注意事項

- 修正コミット後の再レビューに備え、`resolved` 後も監視を継続する
- 全体タイムアウトは 30 分。経過時は現状を PR コメントに記録（サイレント）
- 他セッション対応中の PR（`active_session: true`・`--actionable-only` 出力に現れない）には介入しない（CP-4・L-109）
