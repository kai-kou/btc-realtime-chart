#!/bin/bash
set -euo pipefail
# PostToolUseFailure hook: プロキシ環境での gh CLI エラー検知・通知
#
# Bash ツールが失敗した時に実行し、gh CLI のプロキシ起因エラーを検出する。
# エラーを検出したら hookSpecificOutput.additionalContext で Claude に修正方法を伝える。
#
# 公式仕様（https://code.claude.com/docs/en/hooks・E-D #19 で裏取り）:
#   - PostToolUseFailure はブロック不可。additionalContext はツール結果の隣に挿入され Claude が読む。
#   - systemMessage は「ユーザー向け」警告であり Claude には届かないため使わない（自己修正させたいので additionalContext を使う）。
#   - exit 0 + JSON stdout で additionalContext を返す。
#
# 対象エラーパターン:
#   - "could not resolve remote" — gh pr create でリモートが検出できない
#   - "none of the git remotes" — git remote がプロキシ経由で解決不可
#   - "no remotes configured" — リモート未設定と誤検出される場合

input=$(cat)

# Bash ツール以外はスキップ
tool_name=$(echo "$input" | jq -r '.tool_name // ""')
if [ "$tool_name" != "Bash" ]; then exit 0; fi

# コマンドとエラー出力を取得
command=$(echo "$input" | jq -r '.tool_input.command // ""')
error_output=$(echo "$input" | jq -r '.tool_response // ""')

# gh コマンドでなければスキップ
if ! echo "$command" | grep -qE '^\s*gh '; then exit 0; fi

# --- egress プロキシの 403 ブロック検出（L-114・2026-07-26 実測: repo REST も 403 へ回帰） ---
# クラウドでは GraphQL・search・非 repo REST・Actions variables/secrets に加え、
# repo スコープ REST（gh api repos/{o}/{r}/...）も 403 になる（原因はリポジトリの API attach 不足で、
# gh の再実行・導入・トークン差し替え・urllib 直叩きのいずれでも解決しない・Issue #338 / #342）。
# シグネチャの SSOT は tools/gh_shim.py の ERROR_GUIDANCE（drift 注意）。ここでは既知文言
# + 汎用 HTTP 403 を検出する（プロキシ文言は変動するため exact-match のみに依存しない）
if echo "$error_output" | grep -qE 'GraphQL proxying is not enabled|GraphQL query is not enabled|connect the Claude GitHub App|GitHub access is not enabled for this session|sessions are bound to their configured repositories|Access to this GitHub Actions path is not permitted|Resource not accessible by integration|HTTP 403'; then
  jq -n --arg cmd "$command" '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUseFailure",
      "additionalContext": ("[proxy-error-detector] クラウドで gh が 403 になりました（L-114）。gh のリトライ・導入・GH_TOKEN 差し替え・urllib/curl 直叩きのいずれでも解決しません（原因はリポジトリの API attach 不足）。\nコマンド: " + $cmd + "\n\n→ mcp__github__* へ切り替えてください（代替表 SSOT: docs/rules/github-mcp-fallback-patterns.md §2）:\n  - Issue/PR: list_issues / issue_read / issue_write / add_issue_comment / list_pull_requests / pull_request_read / create_pull_request / merge_pull_request\n  - ファイル: get_file_contents / create_or_update_file / push_files\n  - 検索・CI: search_issues / search_code / actions_list / get_job_logs\n  - git 操作（clone/fetch/push）は別プロキシで生存しているのでそのまま使える\n  - gh variable/secret はクラウド代替なし（env は Claude.ai 環境設定 / secrets-broker・同 §2.4）")
    }
  }'
  exit 0
fi

# プロキシ起因エラーパターンを検出
if ! echo "$error_output" | grep -qE 'could not resolve remote|none of the git remotes configured|no remotes configured for this repository|does not point to a known GitHub host'; then
  exit 0
fi

# リポジトリ slug を動的導出（bootstrap 未実行でも実リポジトリを案内できるように）
REPO_SLUG="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO_SLUG" ]; then
  REPO_SLUG=$(git config --get remote.origin.url 2>/dev/null \
    | sed -E 's#(git@|https?://)[^/:]+[/:]##; s#\.git$##' 2>/dev/null || true)
fi
[ -z "$REPO_SLUG" ] && REPO_SLUG="kai-kou/btc-realtime-chart"

# クラウドでは gh を直す方向の案内をしない（gh 自体が使えないため・#342）。MCP へ誘導する。
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  jq -n --arg cmd "$command" '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUseFailure",
      "additionalContext": ("[proxy-error-detector] クラウドで gh コマンドが失敗しました。クラウドの GitHub 操作は mcp__github__* が一次経路で、gh のフラグ調整・再実行では解決しません（L-114）。\nコマンド: " + $cmd + "\n\n→ 対応する mcp__github__* ツールに置き換えてください（代替表: docs/rules/github-mcp-fallback-patterns.md §2）。PR 作成は mcp__github__create_pull_request（head/base を明示）。")
    }
  }'
  exit 0
fi

# --- 以下はローカル実行向け（gh が到達できる環境での引数不足エラー）---
# gh pr create のエラー
if echo "$command" | grep -q 'gh pr create'; then
  jq -n --arg repo "$REPO_SLUG" '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUseFailure",
      "additionalContext": ("[proxy-error-detector] gh pr create が失敗しました。\n\n原因: git remote からリポジトリを自動検出できません。\n\n修正方法: 以下のフラグを追加してください\n  --head {現在のブランチ名} --base main\n\n修正例:\n  gh pr create --head claude/BRANCH_NAME --base main -R " + $repo + " ...")
    }
  }'
  exit 0
fi

# その他の gh コマンドのエラー
jq -n --arg cmd "$command" --arg repo "$REPO_SLUG" '{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUseFailure",
    "additionalContext": ("[proxy-error-detector] gh コマンドが失敗しました。\nコマンド: " + $cmd + "\n\n修正チェックリスト:\n1. -R " + $repo + " が付与されているか\n2. gh pr create の場合 --head {ブランチ名} --base main が付与されているか\n3. gh api の場合 repos/" + $repo + "/... のフルパスを使っているか")
  }
}'

exit 0
