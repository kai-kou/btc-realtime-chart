#!/usr/bin/env bash
# pre-compact.sh
# セッション圧縮（context compaction）が始まる「前」に実行されるフック（PreCompact イベント）
#
# 役割分担（post-compact.sh との整理・E-E #23）:
#   - PreCompact（本スクリプト）: 圧縮が始まる *前* に未コミット変更を WIP コミット＆push する。
#     圧縮処理中の不具合や、圧縮後の SessionStart クリーンアップ（git reset/checkout/clean）で
#     作業が失われる前に、最も早いタイミングで作業を確定させる（L-100 の一次防御）。
#   - PostCompact（post-compact.sh）: 圧縮 *後* の symlink 同期 + 二次的な WIP セーフティネット。
#     PreCompact が確定済みなら working tree は clean になる。ルール再確認リマインダーは
#     SessionStart が担当（PostCompact は stdout 注入非対応・Issue #211）。
#
# 出力: PreCompact の stdout はコンテキストに注入されない
#       （注入されるのは UserPromptSubmit / UserPromptExpansion / SessionStart のみ）。
#       本スクリプトは副作用（コミット）目的。exit 0 で圧縮をブロックしない。
#
# 公式仕様（https://code.claude.com/docs/en/hooks ・2026-06 検証・docs/rules/hook-events-reference.md）:
#   - matcher は "manual" / "auto"（何が圧縮を起動したか）
#   - exit code 2 で圧縮をブロックできるが、本フックはブロックしない（exit 0 固定）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# クラウド環境（CLAUDE_CODE_REMOTE=true）かつ main/master 以外のブランチで
# 未コミット変更がある場合のみ WIP コミットする。ローカルではクリーンアップが
# 走らないため不要。
if [[ "${CLAUDE_CODE_REMOTE:-}" = "true" ]]; then
  _branch=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  if [ -n "$_branch" ] && [ "$_branch" != "main" ] && [ "$_branch" != "master" ]; then
    # マージ済みブランチでは [wip] コミットを積まない（古い版へ巻き戻すバグ防止）
    _default_branch=$(git -C "$REPO_ROOT" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
    # 明示 refspec で remote-tracking ref を確実に更新（Issue #78・素の fetch だと古いまま残りうる）
    git -C "$REPO_ROOT" fetch origin "+${_default_branch}:refs/remotes/origin/${_default_branch}" --quiet 2>/dev/null || true
    _merged=$(git -C "$REPO_ROOT" branch --merged "origin/${_default_branch}" --format='%(refname:short)' 2>/dev/null | grep -Fx "$_branch" || true)
    if [ -n "$_merged" ]; then
      echo "[PreCompact] ブランチ '${_branch}' はマージ済みのため [wip] コミットをスキップします" >&2
    elif [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)" ]; then
      _timestamp=$(TZ="${PROJECT_TZ:-Asia/Tokyo}" date '+%Y-%m-%d %H:%M %Z' 2>/dev/null || date '+%Y-%m-%d %H:%M')
      git -C "$REPO_ROOT" add -A 2>/dev/null || true
      if git -C "$REPO_ROOT" commit -m "[wip] auto-commit before compaction starts (${_timestamp})"; then
        if git -C "$REPO_ROOT" push -u origin "$_branch"; then
          echo "[PreCompact] ✅ 圧縮前に未コミット変更を自動コミット＆プッシュしました（ブランチ: ${_branch}）" >&2
        else
          echo "[PreCompact] ✅ 圧縮前に自動コミット完了（プッシュ失敗 - 次回 git push で再試行）" >&2
        fi
      else
        git -C "$REPO_ROOT" stash -u 2>/dev/null \
          && echo "[PreCompact] ✅ コミット失敗 → git stash で保存（復元: git stash pop）" >&2 \
          || echo "[PreCompact] ⚠️ コミットと stash 両方に失敗。git status で確認してください。" >&2
      fi
    fi
  fi
  unset _branch _timestamp _default_branch _merged 2>/dev/null || true
fi

exit 0
