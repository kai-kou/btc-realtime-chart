---
name: claude-code-spec-sync
description: Claude Code 本体のバージョンアップ（changelog/releases）を定期キャッチアップし、本プロジェクトのルール・スキル定義・ハーネス（hooks）・設定を最新仕様に追随させるレーンのオーケストレーター。破壊的変更（これまでのやり方がエラーになる類）は検知したセッションが即対応（影響調査→修正→PR→マージ）、新機能・新設定は検証・検討フェーズ（Issue）を経てから反映する。「Claude Code の仕様変更に追随して」「Claude Code の最新情報をキャッチアップして」「spec-sync 実行して」「/claude-code-spec-sync」と依頼された時、または lane:claude-code-spec Issue を検知した定期スロット（R-1 ルーティンのプリフライト）で使用する。
compatibility: Python3（tools/check_claude_code_updates.py）, Grep/Glob（影響調査）, WebFetch/context7（公式 Docs 裏取り）, discussion-review スキル（影響大の採否議論）
effort: medium
disallowed-tools: AskUserQuestion
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# claude-code-spec-sync スキル

> **必読（起動直後）**: `docs/rules/claude-code-spec-sync.md`（SSOT）を Read してから実行する。
> 分類基準・レーン境界・ガードレールは同ファイルが正本。

## ルールファイル読み込み（トークン最適化対応）

- `docs/rules/claude-code-spec-sync.md`（本レーンの SSOT）
- `docs/rules/session-concurrency-rules.md`（マルチセッション競合防止）

## 設計思想

Claude Code はアップデート頻度が高く、破壊的変更で既存のフック・スキル・設定が
サイレントに壊れることがある（実例: L-101 ツールコールパース退行、L-106 CLAUDE_ENV_FILE 肥大、
Opus 4.8 の effort デフォルト変更）。なお **新モデル世代のリリース自体は原則として追随作業が不要** （運用ファイルのモデル指定はエイリアス既定・`agent-team.md`「モデル指定の方針」）。追随が要るのは エイリアスの解決先を記した参考表と、effort 既定や API 仕様の破壊的変更のみ。本レーンは検知（`tools/check_claude_code_updates.py`・定期）と
対応（本スキル）を分離し、**2つの速度** で追随する:

- **破壊的変更 → 即対応**: 検知したその日のセッションが影響調査→修正→PR→マージまで完遂する
- **新機能・新設定 → 検証・検討フェーズ**: Issue 化し、公式裏取り→適用価値評価→（影響大なら議論型レビュー）→採用/見送り判定を経てから反映する。**検知即反映はしない**

本レーンは **プロジェクト内部資産の仕様追随** を専門に担う。発信はしない。

## 起動トリガー

1. 定期スロット（R-1 ルーティンのプリフライト。スロット定義は開発リポジトリの運用メモが保持）: `check_claude_code_updates.py --create-issue` が新規検知
   → `BREAKING_DETECTED` 出力あり = 即対応フロー / 検証 Issue のみ = 検証・検討フローを1件消化
2. 手動: `/claude-code-spec-sync` /「Claude Code の仕様変更に追随して」
3. オープンの `lane:claude-code-spec` Issue が残っている（過去スロットの取りこぼし）

---

## 実行ステップ

### Step 0: 検知 + 対象 Issue の確認

```bash
python3 tools/check_claude_code_updates.py --create-issue --json
# exit 0 = 新規検知（破壊的変更/新機能があれば起票済み。バグ修正のみなら起票なし＝正常）
# exit 10 = 新規なし / JSON の "breaking_detected": true = 破壊的変更あり
# （--json なしのプレーン出力では代わりに "BREAKING_DETECTED" 行が出る）
# 起票失敗・上限超過分は state から自動除外され次回リトライされる（永久喪失しない）
gh issue list --label "lane:claude-code-spec" --state open --json number,title,labels,createdAt --limit 10
# gh 不可なら MCP: mcp__github__list_issues(labels=["lane:claude-code-spec"], state="open")
```

- 新規検知なし かつ オープン lane Issue なし → **即終了**（担当タスクへ）
- 🔴 **新バージョンを検知したら（exit 0）、分類結果（破壊的 / 新機能 / その他）に関わらず権限プローブを実行する**
  （キーワード辞書では拾えない挙動変化の実測検知・L-127 訂正の再発防止。v2.1.260 の "Reverted ..." 行が
  「その他」に落ちて影響なしと記録された見落としが起点・議論記録 `auto-mode-prompt-root-cause-20260904`）:
  ```bash
  bash tools/probe_permission_prompts.sh          # 0=OK / 1=検知 / 2=判定不能（サイレントに成功扱いしない）
  ```
  exit 1 → `[CC-Sync][破壊的変更] 権限プローブ検知 v{バージョン}` として Issue 化し Step 1 へ（恒久的なハーネス回避策は作らず、
  公式 CHANGELOG で revert / fix 済みかを先に確認する）。exit 2 → 陽性対照不成立を Issue 本文に明記して `status:waiting-claude`
  で残す（環境要因の切り分けが先）。所要は sonnet × 6 呼び出し程度
- `[CC-Sync][破壊的変更]` Issue あり → **Step 1（即対応）を最優先**
- `[CC-Sync][検証]` Issue のみ → Step 2（検証・検討）を **1スロット1件** 消化

### Step 1: 破壊的変更の即対応フロー

1. **Issue ロック（CP-4・最初のアクション）**: `status:waiting-claude` → `status:in-progress`
2. **公式裏取り**: changelog 原文（Issue 内リンク）+ 公式 Docs（`code.claude.com/docs`・context7）で
   変更の正確な仕様・移行方法を確定する（推測で修正しない）
3. **影響調査（横断 Grep）**: 変更に含まれるコマンド名・フラグ名・設定キー・フック名・環境変数名で
   以下を横断検索する:
   - `CLAUDE.md` / `docs/rules/*.md`（ルール）
   - `.claude/skills/*/SKILL.md` / `.claude/commands/`（スキル定義）
   - `.claude/hooks/*.sh` / `.claude/settings.json` / `.claude/output-styles/` / `.claude/agents/`（ハーネス・設定）
   - `tools/*.py` の `claude -p` 呼び出し（headless 経路）
   - 🔴 **起動経路の移動**（`disable-model-invocation` の新規適用・bundled skill ⇄ workflow ⇄ built-in command の
     再分類）は「これまでのやり方がエラーになる」典型。`tools/native_capabilities.json` の `native.routes` を
     突き合わせ、`python3 tools/native_fallback.py routes --json` の ladder が現行仕様と一致するか確認する
     （不一致なら台帳の `status` を更新し、閉じた経路は削除せず経緯を残す・`native-fallback-rules.md` §2.5・L-123）
   - Issue 本文に **「⚠️ 辞書には未一致だが影響領域ヒントに一致した『その他』の変更（要精読）」** セクションが
     あれば、各行を確認する。辞書に追加すべき言い換えパターンがあれば `config/claude_code_spec_sync.yaml` へ
     追記し、追加不要と判断した場合もその理由を Issue コメントに残す（`others_hinted`・#561。ここを読み飛ばすと
     「その他」行の見落としを後から発見できる形で残した意味が失われる）
4. **対応**:
   - 影響あり → **最小差分で修正**（intent-gate 遵守・要求外リファクタ禁止）→ 検証
     （該当ツールの `--self-test`/`--dry-run`、フックは手動実行、settings は `claude config` 系で確認）
     → PR 作成 → L1 セルフレビュー（自前 `code-review` スキル・`Skill(code-review)`。組み込みを置換済み・#280） → squash マージ（`pr-review-flow-summary.md`）
   - 影響なし → Issue に判定理由（調査した対象と根拠）をコメントしてクローズ
5. **記録**: 対応内容を `docs/rules/claude-code-optimization.md` の「バージョン差分ログ」へ追記
   （修正 PR に含める）。同じ破壊的変更カテゴリが 2 回以上再発したら Lv3 フック昇格を検討
   （`harness-escalation.md`）
6. 🔴 **既知化（必須・再検知ループ防止）**: クラウドセッションでは `gh`/REST が両方使えず
   `--create-issue` の起票が失敗するため、Issue 作成が成功したかどうかに関わらず（`mcp__github__*`
   で out-of-band に対応した場合を含む）、対応完了後に必ず
   `python3 tools/check_claude_code_updates.py --mark-known {バージョン}` を実行し、
   修正コミットに `config/claude_code_spec_state.json` <!-- refcheck:ignore --> の変更を含める。省略すると次回以降の
   R-1 が同じバージョンを "BREAKING_DETECTED" として無限に再検知し、既に完了した対応を
   繰り返し調査してしまう（実例: v2.1.225・2026-08-08・#457）。

### Step 2: 新機能・新設定の検証・検討フロー（1スロット1件）

1. **対象選定**: オープンの `[CC-Sync][検証]` Issue の最古 1 件をロック（`status:in-progress`）
2. **検証**（Issue 内チェックリストを順に消化・チェックを付けてコメントで記録）:
   - 公式 Docs / changelog 原文で仕様・前提条件・制約を確認
   - 本プロジェクトへの適用価値を評価 — 判断基準: **CP-5 貢献**（`docs/project-mission.md` への効果）/
     **CP-6 自律性向上**（ユーザー介入削減）/ **コスト**（トークン・時間・保守）/ **リスク**（退行・L-101 型の既知バグ）
   - Issue 本文に「⚠️ 辞書には未一致だが影響領域ヒントに一致した『その他』の変更（要精読）」セクションが
     あれば、Step 1 項目 3 と同じ要領で確認する（`others_hinted`・#561）
3. **判定**（3択）:
   - **採用** → rules/skills/hooks/settings へ反映（最小差分）+
     `docs/rules/claude-code-optimization.md` へ記録 → PR → L1 レビュー → マージ → Issue クローズ
   - **要議論**（挙動・アーキテクチャへの影響が大きい / settings.json の permissions・モデル・フック構成を変える）→
     `discussion-review` スキル（議題 ID: `ccs-{バージョン}-{機能slug}`。ネイティブ失敗時は
     `python3 tools/run_discussion_review.py --id ccs-{バージョン}-{機能slug} --spec tools/discussion_specs/example_debate.json --targets "{対象パス}" --rounds 2`）
     → verdict PASS なら採用フロー / FAIL なら見送り
   - **見送り** → 理由（適用価値が低い・リスク高・時期尚早等）をコメントしてクローズ。
     時期尚早の場合は「再検討条件」を明記する（例: stable 化後・公式バグ修正後）
4. 残りの検証 Issue は次スロット以降が消化する（一括消化しない・スロット圧迫防止）

### Step 3: 完了処理

- state ファイル（`config/claude_code_spec_state.json`。初回実行時に無ければ自動生成される） <!-- refcheck:ignore -->
  の変更を **ワークフロー完了コミットに含める**（dedup の鮮度が必要なため破棄しない）
- Step 1・Step 2 のいずれでも、対応完了バージョンに `--mark-known` が未実行なら Step 1 項目 6 の
  手順で既知化してから state 変更をコミットする（Step 2 の検証 Issue も `--create-issue` 経由の
  起票である点は同じで、同じ再検知ループが起こりうる）
- 修正 PR を伴った場合は `pr-review-flow-summary.md` に従いマージまで完遂してから終了する

## ガードレール（不変）

- `.claude/settings.json` の `permissions.deny` / A-1〜A-6 境界に触れる変更は **本レーンで自動反映しない**
  （検証 Issue に `status:waiting-user` を付けてユーザー判断へ）
- main 直 push 禁止・PR 経由のみ（破壊的変更の即対応でも同じ）
- サーキットブレーカー: 修正サイクル 2 回超で STOP → ユーザー報告（A-4）
- 公式一次ソース（changelog / code.claude.com/docs / anthropic.com）で裏取りできない変更は反映しない
