# PRレビューフロー（サマリー版）

> 完全版は `docs/rules/pr-review-flow.md`（マージコンフリクト解決・force push 後の再レビュー・監視方式・パイプライン別チェックリスト）。
> 実行手順そのものは `pr-review-watcher` スキルが持つ。本ファイルは **判断基準と不変の境界** だけを常駐させる。

## フロー概要

```
実装 → セルフレビュー（self-reviewer・Layer 0 機械ゲート + PR 前フレッシュ文脈レビュー #627）→ PR 作成 → Slack 通知
  → Layer 1 観点別フレッシュ文脈セルフレビュー（主軸・全 PR 必須・自己実行・REVIEW.md で較正）
  → 指摘対応（修正コミット or スキップ + 返信 + Resolve）→ Layer 0+1 通過で自動マージ（squash）
  → **公開反映（publish-sync・マージした同一セッションで完遂）** → Slack 完了通知
```

- **🟢 恒久承認**: 実装完了したら確認なしで PR まで進める（SSOT: `CLAUDE.md`「PR 作成の完全自律化」）。「PR 作成してよいですか？」は禁止。
- **🔴 外部 AI レビュアーは廃止**: Copilot / Gemini へのレビュー依頼・催促は行わない。レビューは **Layer 1 セルフレビューで完結** させ、外部応答を待たない（SSOT: `ai-reviewer-strategy.md`）。
- **Layer 1 の標準実行手段は `Skill(code-review)`**（`.claude/skills/code-review/` が組み込みを置換・自律起動可）。
- **公開反映（`publish-sync`）は、公開レーンを持つリポジトリだけの工程**。実体（`publish-sync` スキル・
  `tools/check_publish_drift.py` / `scripts/publish-snapshot.sh`）は配布物には含まれないため、
  下流プロジェクトでは **この工程が存在しないのが正常** である（`post-merge-publish-check.sh` も
  実体不在なら何も出力せず終わる）。下流では本ファイルの `publish-sync` 関連の記述を読み飛ばしてよい。

## PR 作成時の必須事項（コマンド仕様は各ツールの description に従う）

0. **自動保全コミットの書き換え（必須・#483）**: ブランチに `[wip]` 件名の自動保全コミットが残っていたら、PR 作成前に意味のある粒度・メッセージへ書き換える。`pre-pr-create-check.sh` が機械ブロックし、書き換え手順（amend / `reset --soft` 起点の再コミット）をエラーメッセージ内に案内する
1. `mcp__github__create_pull_request`（`head`={作業ブランチ} / `base`=main）。本文に **`Session-Id: $CLAUDE_CODE_SESSION_ID`**・`Sprint Goal:` 1 行・`sp:N` を必ず含める（`--mine` 所有判定と done_sp 計測の前提）
2. **PR 存在確認（必須・L-050）**: `mcp__github__list_pull_requests` で `head` を指定して実在を確認する（作成の成否をレスポンスだけで判断しない）
3. Slack 通知: `python3 tools/slack_notify.py pr --pr-url ... --pr-title "[PR作成] ..." --branch ...`
4. **Layer 1 セルフレビュー**: `Skill(code-review)` を必ず実行 → **CONFIRMED は PR の行単位インラインコメント、PLAUSIBLE と上限超 NIT は本文に集約**（指摘ゼロでも `event="COMMENT"` のレビューを 1 件投稿する・#461 → #627）
5. （任意）`mcp__github__subscribe_pr_activity` + `tools/pr_review_heartbeat.sh` で CI / 人手コメントを監視

> ローカル実行時は `gh pr create --head {branch} --base main -R {owner}/{repo}` でもよい。クラウドでは MCP が一次経路。

## レビュー監視と自動マージ

| タイミング | アクション |
|---------|-----------|
| PR 作成直後 | Layer 1 セルフレビュー → **指摘を行単位インラインコメントで投稿** → 指摘対応（修正コミット or スキップ + **同一スレッドへの返信** + Resolve） |
| Layer 0+1 通過後 | `mcp__github__merge_pull_request`（`merge_method="squash"`）で即マージ |
| **マージ直後** | **公開リポジトリへ反映（`publish-sync`）**。`post-merge-publish-check.sh` がドリフトを判定して指示を注入する。**配布物リポジトリがクローンされていないセッション** は `[publish-sync]` Issue に記録して終える（沈黙禁止・#449）。判定は **毎回クローン済みかどうかの実測** で行う — sources 登録は毎回届くことまでは保証しない（同じ設定で供給される発火と供給されない発火が混在した実例あり・`L-117` の回避路とその但し書き）→ Slack 完了通知 |
| 任意 | CI 失敗・人手コメントがあれば対応してからマージ |

サーキットブレーカー: 修正サイクル 2 回超で STOP → ユーザー報告（A-4）。

**マージ後のチャット完了報告は `completion-report-rules.md`（SSOT）に従う**: 「ご依頼（初回指示の再掲）→ アウトカム」を冒頭に置き、マージ方法・レビュー往復・指摘件数を主役にしない。「PR #N をマージしました」だけで終わらせない。

## 指摘対応ルール

- **記録先は PR のレビュー（必須・#461 → #627）**: CONFIRMED は行単位インライン化し（NIT は上限 3 件）、対応結論は **同一スレッドへの返信** で残す（新規コメントに分離しない）。PLAUSIBLE と上限超 NIT は本文列挙で記録し返信不要。指摘ゼロでもレビューを 1 件投稿する。手順・テンプレート・フォールバックの SSOT は `.claude/skills/code-review/SKILL.md` Step 3-A
- **サイレント原則（L-102）**: AI レビュー指摘対応は **ユーザーに報告しない**。記録は PR スレッド返信・Resolve・Issue コメントのみ（Slack `--outcome` にセルフレビュー実施・指摘件数を書くのも違反）。チャット逐次報告・Slack `@mention`・完了報告アウトカムへの混入は禁止。例外は A-1〜A-6 のみ
- **`<github-webhook-activity>` は抑制対象ではない（#61・詳細は `pr-review-flow.md`「入力とチャット出力の区別」）**: ハーネスが配信する入力であり L-102 の対象外
- 対応した場合: 「対応しました。{修正概要}（{commit_sha}）」を返信してから Resolve
- スキップした場合: 「スキップします。理由: {理由}」を返信してから Resolve（製品名・API 仕様は公式ドキュメントで確認してから記録する）

## セッション復帰（PR 放置検出）

```bash
python3 tools/check_pending_pr_reviews.py --mine --actionable-only --json   # ① 自 PR を最優先で回収
python3 tools/check_pending_pr_reviews.py --actionable-only --json          # ② 他保護込みの全体ビュー（孤児 PR 救済）
```

**失敗時（`gh` バイナリ不在・403 等で exit code 3・stdout に `GH_UNAVAILABLE:` 行）**: `mcp__github__list_pull_requests(owner, repo, state="open")` へ直接フォールバックする（#645）。ただし `--mine` / `--actionable-only` 相当の絞り込み（Session-Id 突合・アクティブセッション除外）は無いため、取得した PR は目視で状態を確認してから回収する。

`needs_prompt` → Layer 1 セルフレビュー実行 → 指摘解消 → 即マージ / `needs_response` → 指摘対応（CI 失敗・人手コメント）/ `awaiting_review` → 作成セッションが実行中（待機）。**自スコープ優先（#47）・他セッション対応中 PR への不介入（CP-4・L-109）** の判定ロジック全文は `pr-review-flow.md`「セッション復帰フロー」を参照。

**公開反映の回収も復帰時の責務（#449）**: `python3 tools/check_publish_drift.py --quiet` が 1（ドリフトあり）/ 2（判定不能）なら、`publish-sync` スキルで反映まで完遂する。`[publish-sync]` の open Issue は「前のセッションが反映できずに残した積み残し」なので最優先で消化してクローズする。
