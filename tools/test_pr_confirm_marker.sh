#!/usr/bin/env bash
# tools/test_pr_confirm_marker.sh — post-pr-confirm-mark.sh × stop-pr-check.sh の
# マーカー連携 e2e テスト（Issue #543）
#
# 検証する不変条件:
#   1. マーカー無し → stop-pr-check.sh はクラウド分岐で差し戻す（exit 2）
#   2. post-pr-confirm-mark.sh が「現在ブランチの PR 実在」を観測した同一セッションでは、
#      その後の stop-pr-check.sh がマーカーを見て無条件で通す（exit 0）
#   3. 別セッションのマーカーでは抑止しない（他セッションは引き続き exit 2・L-103 防御）
#   4. list_pull_requests の応答が closed かつ未マージのみならマーカーは作られない（exit 2 のまま）
#   5. stop_hook_active=true では常に exit 0（既存の再帰防止動作を壊さない）
#
# 使い方: bash tools/test_pr_confirm_marker.sh
# 終了コード: 0 = 全 PASS / 1 = 1 件以上 FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIRM_HOOK="$REPO_ROOT/.claude/hooks/post-pr-confirm-mark.sh"
CHECK_HOOK="$REPO_ROOT/.claude/hooks/stop-pr-check.sh"
LIB="$REPO_ROOT/.claude/hooks/lib/hook_layer1_common.sh"
[ -f "$CONFIRM_HOOK" ] || { echo "FATAL: フックが見つかりません: $CONFIRM_HOOK"; exit 1; }
[ -f "$CHECK_HOOK" ] || { echo "FATAL: フックが見つかりません: $CHECK_HOOK"; exit 1; }
[ -f "$LIB" ] || { echo "FATAL: 共通ライブラリが見つかりません: $LIB"; exit 1; }
# shellcheck source=.claude/hooks/lib/hook_layer1_common.sh
source "$LIB"
# shellcheck source=tools/lib/test_harness.sh
source "$REPO_ROOT/tools/lib/test_harness.sh"

BRANCH="feat/test"

# テスト用リポジトリ（作業ブランチ + push 先の bare リモート）+ マーカー保存用の一時ディレクトリを作る。
# origin はローカル bare のパスのまま使う（ネットワークに触れない）。post-pr-confirm-mark.sh は
# 実際にこの origin URL から owner/repo を導出するため、テスト側も同じ関数（hook_repo_slug_from_url）
# で同じ値を計算し、tool_input.owner/repo にそのまま使う（owner/repo チェックを本物と揃える）。
setup_repo() {
  WORK=$(mktemp -d)
  init_tmp_git_repo "$WORK"
  echo "base" > "$WORK/repo/base.txt"
  git -C "$WORK/repo" add -A
  git -C "$WORK/repo" commit --quiet -m "base"
  git -C "$WORK/repo" checkout --quiet -b "$BRANCH"
  git -C "$WORK/repo" push --quiet -u origin "$BRANCH" 2>/dev/null || true

  MARKER_DIR=$(mktemp -d)

  REAL_ORIGIN=$(git -C "$WORK/repo" remote get-url origin)
  REAL_SLUG=$(hook_repo_slug_from_url "$REAL_ORIGIN")
  REAL_OWNER="${REAL_SLUG%%/*}"
  REAL_REPO="${REAL_SLUG##*/}"
}

teardown_repo() { teardown_tmp_repo "$WORK" "$MARKER_DIR"; }

# run_check <session_id> [stop_hook_active(true|false)] → CHECK_EXIT / CHECK_ERR に結果を残す
# GITHUB_REPOSITORY はテスト側で固定のダミー値を渡す。stop-pr-check.sh は GITHUB_REPOSITORY が
# 設定されていればそちらを優先し origin URL のパースをスキップするため、origin がローカル bare
# パス（owner 相当にドットを含みうる）でも「owner にドットを含む」ガードに引っかからない。
# マーカー確認ロジック自体は owner/repo を見ないため、この値は post-pr-confirm-mark.sh 側の
# owner/repo（REAL_OWNER/REAL_REPO）と一致している必要はない。
run_check() {
  local sid="$1" active="${2:-false}"
  local err
  err=$(cd "$WORK/repo" && CLAUDE_CODE_REMOTE=true GITHUB_REPOSITORY="acme/widgets" \
    CLAUDE_HOOK_PR_MARKER_DIR="$MARKER_DIR" \
    bash "$CHECK_HOOK" <<< "{\"session_id\":\"${sid}\",\"stop_hook_active\":${active}}" 2>&1 >/dev/null)
  CHECK_EXIT=$?
  CHECK_ERR="$err"
}

# run_confirm_create <session_id> <head> <pr番号>: create_pull_request 成功を模した呼び出し
run_confirm_create() {
  local sid="$1" head="$2" number="$3"
  ( cd "$WORK/repo" && CLAUDE_HOOK_PR_MARKER_DIR="$MARKER_DIR" \
    bash "$CONFIRM_HOOK" <<< \
    "{\"session_id\":\"${sid}\",\"tool_name\":\"mcp__github__create_pull_request\",\"tool_input\":{\"owner\":\"${REAL_OWNER}\",\"repo\":\"${REAL_REPO}\",\"head\":\"${head}\"},\"tool_response\":{\"number\":${number}}}" \
    >/dev/null 2>&1 )
}

# run_confirm_list_closed_unmerged <session_id> <head>: closed かつ未マージのみの list 応答
run_confirm_list_closed_unmerged() {
  local sid="$1" head="$2"
  ( cd "$WORK/repo" && CLAUDE_HOOK_PR_MARKER_DIR="$MARKER_DIR" \
    bash "$CONFIRM_HOOK" <<< \
    "{\"session_id\":\"${sid}\",\"tool_name\":\"mcp__github__list_pull_requests\",\"tool_input\":{\"owner\":\"${REAL_OWNER}\",\"repo\":\"${REAL_REPO}\",\"head\":\"${head}\"},\"tool_response\":[{\"state\":\"closed\",\"merged_at\":null}]}" \
    >/dev/null 2>&1 )
}

marker_exists() { # marker_exists <session_id>
  local sid="$1" key
  key=$(hook_branch_key "$BRANCH")
  [ -f "${MARKER_DIR}/claude-pr-confirmed-${sid}-${key}" ]
}

echo "[ケース 1] マーカー無し → 差し戻す（exit 2）"
setup_repo
run_check "sessA"
[ "$CHECK_EXIT" -eq 2 ] \
  && report ok "マーカー無しは exit 2" \
  || report ng "マーカー無しなのに exit ${CHECK_EXIT}"
printf '%s' "$CHECK_ERR" | grep -q "PR 存在確認" \
  && report ok "差し戻しメッセージに「PR 存在確認」を含む" \
  || report ng "差し戻しメッセージに「PR 存在確認」が無い（出力: ${CHECK_ERR}）"

echo "[ケース 2] 同一セッションで PR 確認済み → 通す（exit 0）"
run_confirm_create "sessA" "$BRANCH" 101
marker_exists "sessA" \
  && report ok "post-pr-confirm-mark.sh がマーカーを作成した" \
  || report ng "マーカーが作成されなかった"
run_check "sessA"
[ "$CHECK_EXIT" -eq 0 ] \
  && report ok "マーカーありの同一セッションは exit 0" \
  || report ng "マーカーがあるのに exit ${CHECK_EXIT}（出力: ${CHECK_ERR}）"

echo "[ケース 3] 別セッションのマーカーでは抑止しない"
run_check "sessB"
[ "$CHECK_EXIT" -eq 2 ] \
  && report ok "別セッション（sessB）は exit 2（他セッションのマーカーを流用しない）" \
  || report ng "別セッションなのに exit ${CHECK_EXIT}"

echo "[ケース 4] list_pull_requests closed 未マージのみ → マーカーが作られない"
run_confirm_list_closed_unmerged "sessC" "$BRANCH"
marker_exists "sessC" \
  && report ng "closed 未マージのみなのにマーカーが作られた（L-103 防御違反）" \
  || report ok "closed 未マージのみではマーカーが作られない"
run_check "sessC"
[ "$CHECK_EXIT" -eq 2 ] \
  && report ok "マーカー未作成のため sessC は exit 2" \
  || report ng "マーカー未作成のはずが exit ${CHECK_EXIT}"

echo "[ケース 5] stop_hook_active=true は常に exit 0"
run_check "sessD" true
[ "$CHECK_EXIT" -eq 0 ] \
  && report ok "stop_hook_active=true は exit 0（再帰防止）" \
  || report ng "stop_hook_active=true なのに exit ${CHECK_EXIT}"

echo "[ケース 6] stop-router.sh 経由: 差し戻し時だけ [continuation] を 1 回付記する"
# stop-router.sh は全サブフックを直列実行する。一時リポジトリでは tools/ が無いため
# cost 集計・publish 判定は自動スキップされ、tree が clean なので WIP 自動コミットも走らない。
ROUTER_HOOK="$REPO_ROOT/.claude/hooks/stop-router.sh"
run_router() { # run_router <session_id> → ROUTER_EXIT / ROUTER_ERR
  local sid="$1" err
  err=$(cd "$WORK/repo" && CLAUDE_CODE_REMOTE=true GITHUB_REPOSITORY="acme/widgets" \
    CLAUDE_HOOK_PR_MARKER_DIR="$MARKER_DIR" CLAUDE_HOOK_REPORT_MARKER_DIR="$MARKER_DIR" \
    CLAUDE_HOOK_SKIP_PUBLISH_CHECK=true \
    bash "$ROUTER_HOOK" <<< "{\"session_id\":\"${sid}\",\"stop_hook_active\":false}" 2>&1 >/dev/null)
  ROUTER_EXIT=$?
  ROUTER_ERR="$err"
}
run_router "sessE"
[ "$ROUTER_EXIT" -eq 2 ] \
  && report ok "マーカー無しの Stop は router 全体で exit 2" \
  || report ng "マーカー無しなのに router が exit ${ROUTER_EXIT}（出力: ${ROUTER_ERR}）"
cont_count=$(printf '%s' "$ROUTER_ERR" | grep -c '\[continuation\]' || true)
[ "$cont_count" -eq 1 ] \
  && report ok "差し戻し時に [continuation] が 1 回だけ付記される" \
  || report ng "[continuation] の出現回数が ${cont_count}（期待 1）（出力: ${ROUTER_ERR}）"
run_router "sessA"
[ "$ROUTER_EXIT" -eq 0 ] \
  && report ok "マーカーありのセッションは router 全体で exit 0" \
  || report ng "マーカーがあるのに router が exit ${ROUTER_EXIT}（出力: ${ROUTER_ERR}）"
printf '%s' "$ROUTER_ERR" | grep -q '\[continuation\]' \
  && report ng "差し戻していないのに [continuation] が付記された" \
  || report ok "差し戻しが無いときは [continuation] を付記しない"

teardown_repo

echo
echo "結果: PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
