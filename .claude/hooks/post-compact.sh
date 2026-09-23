#!/usr/bin/env bash
# post-compact.sh
# セッション圧縮（context compaction）完了後に実行されるフック
#
# 目的:
#   圧縮直後に未コミット変更を自動保存し、次セッションの SessionStart クリーンアップで
#   作業内容が失われるのを防ぐ。あわせて .claude/rules/ の symlink 同期を自動修正する。
#
# 出力内容（Issue #211・#202 同型修正）:
#   PostCompact は stdout 注入・hookSpecificOutput.additionalContext とも非対応の
#   side-effect 専用イベント（公式仕様・docs/rules/hook-events-reference.md §2）。
#   出力はすべて stderr のログに統一する（Claude には届かない前提で書く）。
#   圧縮後のルール再確認リマインダーは SessionStart フック（compact 再開時も発火・
#   stdout 注入が有効）が担当するため、本フックでは出さない。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# ルール同期状態のチェック（漏れがあれば自動修正）
if [[ -x "$REPO_ROOT/tools/check_rules_sync.sh" ]]; then
  sync_result=$("$REPO_ROOT/tools/check_rules_sync.sh" --fix 2>&1 || true)
  if echo "$sync_result" | grep -q "FIXED"; then
    echo "[PostCompact] シンボリックリンクを自動修正しました:" >&2
    echo "$sync_result" >&2
  fi
fi

# --- 未コミット変更の自動保存（圧縮→新セッション時のファイルリセット防止）---
# 対象: クラウド環境（CLAUDE_CODE_REMOTE=true）かつ main/master 以外のブランチで
#       未コミット変更がある場合のみ。ローカル環境では SessionStart のクリーンアップが
#       走らないため自動コミットは不要。
if [[ "${CLAUDE_CODE_REMOTE:-}" = "true" ]]; then
  _branch=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  if [ -n "$_branch" ] && [ "$_branch" != "main" ] && [ "$_branch" != "master" ]; then
    # マージ済みブランチでは [wip] コミットを積まない（古い版へ巻き戻すバグ防止）
    _default_branch=$(git -C "$REPO_ROOT" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
    # 明示 refspec で remote-tracking ref（origin/<default>）を確実に更新する（Issue #78）。
    # 素の `git fetch origin <branch>` は構成次第で origin/<branch> が古いまま残り、
    # squash マージ後にマージ判定・merge-base がズレて二重 diff の原因になる。
    git -C "$REPO_ROOT" fetch origin "+${_default_branch}:refs/remotes/origin/${_default_branch}" --quiet 2>/dev/null || true
    _merged=$(git -C "$REPO_ROOT" branch --merged "origin/${_default_branch}" --format='%(refname:short)' 2>/dev/null | grep -Fx "$_branch" || true)
    if [ -n "$_merged" ]; then
      echo "[PostCompact] ブランチ '${_branch}' はマージ済み（origin/${_default_branch} 基準）のため [wip] コミットをスキップします" >&2
      echo "[PostCompact] → 新規作業は 'git checkout ${_default_branch} && git pull && git checkout -b <new>' で新ブランチを切ってください" >&2
      _skip_wip_commit=true
    fi

    if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)" ] && [ "${_skip_wip_commit:-false}" != "true" ]; then
      _timestamp=$(TZ="${PROJECT_TZ:-Asia/Tokyo}" date '+%Y-%m-%d %H:%M %Z' 2>/dev/null || date '+%Y-%m-%d %H:%M')
      git -C "$REPO_ROOT" add -A 2>/dev/null || true
      if git -C "$REPO_ROOT" commit -m "[wip] auto-commit before compaction (${_timestamp})"; then
        if git -C "$REPO_ROOT" push -u origin "$_branch"; then
          echo "[PostCompact] ✅ 未コミット変更を自動コミット＆プッシュしました（ブランチ: ${_branch}）" >&2
        else
          echo "[PostCompact] ✅ 自動コミット完了（プッシュ失敗 - 次回 git push で再試行してください）" >&2
        fi
      else
        if git -C "$REPO_ROOT" stash -u 2>/dev/null; then
          echo "[PostCompact] ✅ コミット失敗 → git stash で変更を保存しました（復元: git stash pop）" >&2
        else
          echo "[PostCompact] ⚠️ コミットと stash 両方に失敗しました。git status で確認してください。" >&2
        fi
      fi
    fi
  fi
  unset _branch _timestamp _default_branch _merged _skip_wip_commit 2>/dev/null || true
fi

# 圧縮後のルール再確認リマインダーは SessionStart フックが担当（stdout 注入が有効なのは
# SessionStart/UserPromptSubmit/UserPromptExpansion のみ。PostCompact の stdout は Claude に
# 届かないため、ここで出していた旧リマインダーは一度も機能していなかった・Issue #211）。
echo "[PostCompact] 完了（WIP 保全 + symlink 同期）。ルール再確認は SessionStart 経路が担当" >&2

exit 0
