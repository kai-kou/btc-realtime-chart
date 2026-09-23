# pr-review-watcher 詳細リファレンス

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


> `pr-review-watcher` スキルの **詳細手順**（Step 1-7・GraphQL コマンド・トラブルシューティング・
> 実行履歴・セッション復帰の機微）を切り出したもの（progressive disclosure・E-G #26）。
> 日常の概要は `SKILL.md`、フロー全体の正本は `docs/rules/pr-review-flow.md` を参照。
> 本ファイルは実際に監視・対応を行うときに Read する。

> **🔴 外部 AI レビュアー依頼は廃止**: レビューは Claude 自身の Layer 1 セルフレビュー（自前 `code-review`
> スキル・`Skill(code-review)`）が主軸。組み込み `/code-review` は disable-model-invocation で自律起動不可のため、
> 同名 project スキル `.claude/skills/code-review/` が bundled を置換している（自律起動可・#275 → #280）。
> **Copilot 依頼（`request_copilot_review` / `--add-reviewer @copilot`）・Gemini 依頼（`/gemini review`）は行わない。**
> 本ファイル中に残る Copilot / Gemini への依頼・催促・判定の記述は歴史的経緯であり、現行フローでは実行しない
> （SSOT: `docs/rules/ai-reviewer-strategy.md`）。`subscribe_pr_activity` は CI / 人手コメントの任意監視に使う。

---

## セッションタイムアウト対策（多層防御・L-051）

`subscribe_pr_activity` 待機中のタイムアウトは「障害」ではなく「正常な動作」として設計する。

| Layer | 仕組み | 復帰タイミング |
|-------|--------|-------------|
| Layer 1 | `session-start.sh` が開始時に `check_pending_pr_reviews.py` を自動実行 | 手動再開時（即座） |
| Layer 2 | hourly スロットのプリフライトで `check_pending_pr_reviews.py` 実行 | 最大 1 時間以内 |
| Layer 3 | `check_pending_pr_reviews.py` が経過時間・レビュー状態で判定 | 検出後即座 |
| Layer 4 | `stop-pr-check.sh` が PR 未マージ終了を検知 | セッション終了時 |

---

## セッション復帰フロー（クラウド環境）

> **他セッション対応中の PR には介入しない（CP-4・L-109）**: `--actionable-only` は直近 10 分以内に
> 人間側アクティビティ（head へのコミット・非ボットコメント・PR 作成）がある PR を `active_session: true`
> として自動除外する。出力に現れない PR は別セッションが現役対応中であり、催促・指摘対応・マージ・
> subscribe をしてはならない（`--include-active` での強制取得も禁止）。

```bash
python3 tools/check_pending_pr_reviews.py --actionable-only --json
```

| ステータス | 意味 | 対応 |
|-----------|------|------|
| `needs_prompt` | Layer 1 セルフレビュー要実施（アイドル化した自/孤児 PR） | **観点別フレッシュ文脈セルフレビューを実行** → 指摘解消 → 即マージ（❌ 外部レビュアー催促はしない） |
| `awaiting_review` | PR 作成直後（作成セッションが実行中） | 待機（自 PR なら自分でセルフレビュー実行 → マージ） |
| `needs_response` | 未解決スレッドあり（CI 失敗 / 人手コメント等） | 指摘対応（修正 or スキップ + 返信 + Resolve）→ マージ |
| `no_action` | Claude 以外 / 手動 PR | スキップ |

> `ready_to_merge` は待機ゼロで即マージ。外部 AI レビュアーの応答待ちは存在しない。
> 復帰対象は「自分が作成した PR の続き」または「活動が途絶えた孤児 PR の救済」のみ
> （別セッション対応中への介入は CP-4 違反）。

---

## Step 1: レビュー状態の取得

**クラウド（一次経路）**:

```
mcp__github__pull_request_read(method="get_reviews",         owner, repo, pullNumber)  # Approve/Changes Requested/Commented
mcp__github__pull_request_read(method="get_comments",        owner, repo, pullNumber)  # PR コメント（issue comments）
mcp__github__pull_request_read(method="get_review_comments", owner, repo, pullNumber)  # インラインレビューコメント
```

**ローカル実行用**:

```bash
# レビュー（Approve/Changes Requested/Commented）
gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews \
  --jq '.[] | {user: .user.login, state, submitted_at, body}'
# PRコメント（issue comments）
gh api repos/{owner}/{repo}/issues/{pr_number}/comments \
  --jq '.[] | {user: .user.login, created_at, body}'
# インラインレビューコメント
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments \
  --jq '.[] | {user: .user.login, created_at, body, path, line}'
```

## Step 2: ステータス判定（外部レビュアー非依存）

レビューは Layer 1 セルフレビュー（自前 `code-review` スキル・`Skill(code-review)`）で完結するため、
**外部 AI レビュアーの応答待ち・催促・実レビュー判定は行わない**。判定対象は **自分のセルフレビュー結果と CI / 人手コメント** のみ。

`check_pending_pr_reviews.py` が emit するステータス（セッション復帰フローの表と同一）で判定する:

| ステータス | 意味 | 対応 |
|-----------|------|------|
| `awaiting_review` | PR 作成直後（作成セッションが実行中） | 自 PR は自分でセルフレビュー実行 → マージ |
| `needs_prompt` | セルフレビュー要実施（アイドル化した自/孤児 PR・未解決なし） | セルフレビューを実行 → 指摘解消 → 即マージ |
| `needs_response` | 未解決スレッド（CI 失敗・人手コメント）あり | 指摘対応（修正 or スキップ + 返信 + Resolve）→ マージ |
| `no_action` | Claude 以外 / 手動 PR | スキップ |

> `check_pending_pr_reviews.py` は **過去 PR 互換** のため Copilot / Gemini bot のレビューも検出するが、
> 現行フローではそれらへの **依頼・催促・応答待ちはしない**（検出値は履歴互換情報として保持するだけ）。

## Step 3: レビュー指摘への自動対応

1. **指摘を精査**: 妥当なコード品質・整合性の指摘は修正コミット。実態と異なるドキュメント指摘は照合して対応 or 対応不要を判断。誤検知・的外れは理由を記載してスキップ
2. **修正する場合**: ファイル編集 → `git add` → `git commit` → `git push`（メッセージ: `fix: {レビュアー}の指摘を反映 — {概要}`）
3. **対応不要の場合**: スレッドに理由を記載
4. **返信・Resolve（必須）**: 対応・スキップいずれもスレッドに返信して Resolve する

**クラウド（一次経路）** — GraphQL を使わずに返信・Resolve まで完結する:

```
# インラインコメントへの返信（commentId は get_review_comments の応答 id）
mcp__github__add_reply_to_pull_request_comment(owner, repo, pullNumber, commentId,
    body="対応しました。{修正概要}（{commit_sha}）")
# スキップ時: body="スキップします。理由: {理由}"

# Resolve（threadId は get_review_comments の応答に含まれるスレッド識別子）
mcp__github__resolve_review_thread(owner, repo, threadId)
# 誤って解決した場合: mcp__github__unresolve_review_thread(owner, repo, threadId)
```

**ローカル実行用**:

```bash
# インラインコメントへの返信
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies \
  --method POST -f body="対応しました。{修正概要}（{commit_sha}）"
# スキップ: -f body="スキップします。理由: {理由}"

# Resolve（thread_id を取得して resolve）
gh api graphql -f query='
  query { repository(owner: "{owner}", name: "{repo}") {
    pullRequest(number: {pr_number}) {
      reviewThreads(first: 100) {  # 100超はページネーション（after カーソル）
        nodes { id isResolved comments(first: 1) { nodes { id } } } } } } }'
gh api graphql -f query='
  mutation { resolveReviewThread(input: {threadId: "{thread_id}"}) { thread { isResolved } } }'
```

> **ひもづけ**: Step 1 のインラインコメント `node_id` と GraphQL `reviewThreads` の
> `comments.nodes[0].id`（node_id）を照合して `thread_id` を特定する。REST には数値 `id` と
> Base64 の `node_id` があるため、照合には必ず `node_id` を使う。
>
> クラウドでは `mcp__github__add_reply_to_pull_request_comment` + `mcp__github__resolve_review_thread`
> で代替できる（thread_id は `mcp__github__pull_request_read` method get_review_comments で取得）。

## Step 4: Layer 0+1 通過の確認（外部レビュアー催促は廃止）

外部 AI レビュアーへの催促（`/gemini review` / `--add-reviewer @copilot`）は **行わない**。
代わりに、マージ前に以下を確認する:

- Layer 0（機械ゲート）が PASS している（`self_review_check.py` 等・PR 作成前フックで担保済み）
- Layer 1（観点別フレッシュ文脈セルフレビュー）を実行し、指摘を全て解消（対応 or スキップ記録）した
- 条件付き Layer 2 が必要な PR は verdict を解消した

**Layer 1 実施の機械検証（#462）**: Step 1 の実行を自己申告のまま信じず、マージ実行の直前に
`python3 tools/check_pending_pr_reviews.py --verify-layer1 <PR番号>` を必ず通す
（Layer 0 は PR 作成前の静的チェックで、Layer 1 の実施有無までは検出しないため別ゲートとして追加）。

| 終了コード | 意味 | 挙動 |
|---|---|---|
| 0（`LAYER1_VERIFIED`） | レビュー本体 or 行単位インラインコメントが 1 件以上存在 | マージ続行 |
| 1（`LAYER1_MISSING`） | 0 件（Layer 1 未実施の可能性） | **マージをブロック**。`Skill(code-review)` を実行してから再検証する |
| 2（`LAYER1_UNKNOWN`） | gh 到達不可で判定不能（クラウドの 403 等） | この終了コード単体ではブロックしない。`mcp__github__pull_request_read(method="get_reviews")` で件数を直接確認してから判断する |

> **クラウド無人セッションでは常に `LAYER1_UNKNOWN` になる**（gh 未導入が既定・L-114）。
> この場合は必ず `mcp__github__pull_request_read(method="get_reviews")` を実行して現行コミットへの
> レビュー/コメントが 0 件でないかを直接確認し、0 件なら `Skill(code-review)` を再実行してから
> マージする（exit 2 を「マージしてよい」のシグナルとして扱わない）。
>
> `LAYER1_VERIFIED` は PR head の現行コミットに投稿されたレビュー/コメントのみをカウントする
> （`commit_id` 一致判定）。force-push や追加コミット後に残った古いレビューは数えないため、
> 修正コミット後は必ず Layer 1 を再実行してから本チェックを通すこと。

## Step 5: 自動マージ判定（外部レビュアー応答待ちなし）

Layer 0+1（+ 条件付き Layer 2）を通過したら **即マージ可**。25 分タイムアウト待機は廃止。
CI 失敗・人手コメントがある場合のみ対応してからマージする。

## Step 6: AIレビュー完了記録（PR のみ・サイレント / L-102）

全レビュアーが `resolved` / `no_response` になったら **PR スレッドにのみ** サマリーを投稿する。

```markdown
## レビュー完了サマリー
| レビュー | 状態 | 結果 |
|---|---|---|
| Layer 0 機械ゲート | ✅ PASS | — |
| Layer 1 観点別フレッシュ文脈セルフレビュー | ✅ 完了 | 指摘X件対応済み / 指摘なし |
| Layer 2 敵対的議論（該当時のみ） | ✅ 完了 / 該当なし | verdict 解消済み |

すべてのレビュー指摘を解消しました（外部 AI レビュアー依頼なし）。
```

> **サイレント原則（L-102）**: サマリーは PR への記録のみ。チャット・Slack `@mention` には報告しない。
> マージ後の完了報告は `docs/rules/completion-report-rules.md` に従う（「初回指示 → アウトカム」中心。
> 指摘件数・修正サイクルを混ぜない）。例外は A-1〜A-6 のみ。

---

## force push / 修正後の再レビュー

差分が書き換わったら（force push・wip squash 等）、**自前 `code-review` スキル（`Skill(code-review)`）で
セルフレビューを再実行** する（外部レビュアーへの再依頼はしない）。
条件付きで Layer 2（`discussion_review_trigger.py --pr {pr_number}`）も再起動する。
古い outdated インラインスレッドは Resolve してから再レビュー結果に基づき対応する。

---

## トラブルシューティング（CI / 人手コメント・PR コメントに記録・L-102）

> **🔴 外部 AI レビュアー（Copilot / Gemini）への依頼・催促・再依頼の手順は廃止に伴い削除済み。**
> レビューは Claude 自身の Layer 1 セルフレビュー（自前 `code-review` スキル・`Skill(code-review)`）で完結するため、
> 外部レビュアーの未応答・未アサイン・クォータ超過をトラブルシュートする必要はない（SSOT: `docs/rules/ai-reviewer-strategy.md`）。
> `gh pr edit --add-reviewer @copilot` / `mcp__github__request_copilot_review` / `gh api .../requested_reviewers` /
> `/gemini review` は **使用しない**。

対応が必要なのは CI 失敗・人手コメントのみ。ユーザー報告はユーザー操作必須（A-6: GitHub App 再インストール・
課金等）に該当する場合のみ。

| 事象 | 確認 | 対応 |
|------|------|------|
| CI チェック失敗 | **クラウド（一次経路）**: `mcp__github__actions_list` / `get_job_logs` / `get_check_run`（`gh pr checks` は GraphQL 依存でクラウド不可・§0）。（以下はローカル実行用）`gh pr checks` / Actions ログ | 根本原因を特定して修正コミット（L-023・確認不要） |
| 人手レビューコメント | PR の未解決スレッド | 指摘対応（修正 or スキップ + 返信 + Resolve） |
| 対象外ファイルのみの変更 | 変更が Markdown 等のみ | セルフレビューで対象外と判定 → 問題なし |

---

## 実行履歴の記録（Scheduled Tasks 可観測性）

AIレビュー全指摘の解消後・自動マージ後に、対象 Issue へ実行サマリーをコメントする。

```markdown
## AIレビュー完了サマリー（{YYYY-MM-DD HH:MM JST}）
- **PR番号**: #{pr_number}
- **レビュアー**: {使用したレビュアー}
- **指摘件数**: {total}件（Error {error} / Warning {warning}）
- **対応済み**: {resolved}件 / スキップ: {skipped}件
- **修正サイクル数**: {cycle}回 / **所要時間**: {elapsed}分
### 対応した指摘 / スキップした指摘（理由付き）
```

重要な指摘パターンは `docs/rules/lessons/pr-review.md` に教訓として転記する（`lessons-management.md` に従う・下記 Step 7）。
同一パターンが累計 2 回以上でハーネスエスカレーション（Lv2→Lv3）を提案する。

---

## Step 7: フィードバックループ（マージ後・必須）

自動マージ後、`docs/rules/lessons/pr-review.md`（教訓の Warm 層・`lessons-management.md` が SSOT）に
今回の指摘から得た再発防止策を追記する。

- **F-1 収集・分類**: 今回の指摘を 対応済み / スキップ / パターン化可能 に分類
- **F-2 更新判定**: `docs/rules/lessons/pr-review.md` の既存エントリと照合
  - 既存エントリと一致 → 発生 PR を追記。同一パターンが 3 回以上ならセルフレビューチェック追加・Lv3 フック昇格を提案
  - 新規パターン → `lessons-management.md` の記法で新エントリを追加
  - スキップ指摘のみ（誤検知・プロジェクト固有事情）→ 記録不要
- **F-3 反映検討**: 頻出パターンは `.claude/skills/self-reviewer/SKILL.md` のチェック項目 or
  `docs/rules/self-review-checklist.md` に反映。3 回以上は Lv3 フック（`post-tool-use-validate.sh`）昇格を提案

> F-2/F-3 の更新はコミットに含める（例: `docs: PR レビュー教訓を追記（PR#{pr_number} 反映）`）。
> 注: 一部プロジェクトには独自の学習ログ（プロジェクト固有ファイル名）がある場合がある。
> その場合はプロジェクト側のファイルに従い、本ベースの既定は `lessons/pr-review.md` とする。
> ドメイン固有の指摘優先順位（プロジェクトの品質ゲート）は各プロジェクトの mission/ルールに従う。
