#!/bin/bash
# post-review-write-mark.sh — PR レビュー投稿を検知し Layer 1 実施マーカーを記録する
# （Issue #512・pre-merge-layer1-check.sh とペアで動く非ブロッキング nudge）
#
# 背景: FAIR Layer 1（セルフレビュー）の実施はモデルの自主性に委ねられており、
# ハーネスが機械観測していなかった（`.claude/settings.json` の PreToolUse matcher は
# create_pull_request までで merge_pull_request を含まない）。本フックは
# mcp__github__pull_request_review_write の「提出（レビューが実際に投稿された）」呼び出しを
# 検知し、セッションローカルのマーカーを記録する。
#
# 🔴 これは手続きの観測であって品質の証明ではない。指摘ゼロの空レビュー（event=COMMENT）
# を1件投げるだけでもマーカーは立つ。「hooks で品質担保できている」という誤解をしないこと。
#
# 「提出」の判定: method=create かつ event が指定されている（event 省略時は pending review の
# 作成でしかなく提出ではない）、または method=submit_pending。resolve_thread 等は対象外。
#
# マーカー保存先: git dir 配下（コミット対象外・セッションローカル）にセッション ID + PR 番号を
# キーにしたファイルを touch する。PR 作成セッション ≠ マージセッションのときはマーカーが
# 正当に存在しない（pre-merge-layer1-check.sh 側が非ブロックで扱う）。
#
# 出力経路: PostToolUse はブロック不可（exit 2 に効果がない）。常に exit 0。
#
# 入力 (stdin JSON): { "session_id": "...", "tool_name": "...", "tool_input": {...} }
# 自己テスト: bash .claude/hooks/post-review-write-mark.sh --self-test
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_layer1_common.sh
source "$HOOK_DIR/lib/hook_layer1_common.sh"

# ── 検知: このツール呼び出しは「レビューの提出」か ────────────────────────────
# 標準出力に judgement（submit:<pr番号> / skip:<理由>）を出す。副作用は持たない。
detect_submit() {
  local input="$1" expected_owner="$2" expected_repo="$3"
  local tool_name method event owner repo pull_number

  tool_name=$(printf '%s' "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
  if [[ "$tool_name" != "mcp__github__pull_request_review_write" ]]; then
    echo "skip:not-review-write-tool"; return 0
  fi

  method=$(printf '%s' "$input" | jq -r '.tool_input.method // ""' 2>/dev/null || echo "")
  event=$(printf '%s' "$input" | jq -r '.tool_input.event // ""' 2>/dev/null || echo "")
  if [[ "$method" == "create" && -n "$event" ]]; then
    :
  elif [[ "$method" == "submit_pending" ]]; then
    :
  else
    echo "skip:not-a-submission"; return 0
  fi

  owner=$(printf '%s' "$input" | jq -r '.tool_input.owner // ""' 2>/dev/null || echo "")
  repo=$(printf '%s' "$input" | jq -r '.tool_input.repo // ""' 2>/dev/null || echo "")
  if ! hook_owner_repo_match "$owner" "$repo" "$expected_owner" "$expected_repo"; then
    echo "skip:other-repo"; return 0
  fi

  # pullNumber は数値以外（欠落・不正値）なら対象外にする（ファイルパスへ直接展開するため）
  pull_number=$(printf '%s' "$input" | jq -r '.tool_input.pullNumber // ""' 2>/dev/null || echo "")
  if [[ ! "$pull_number" =~ ^[0-9]+$ ]]; then
    echo "skip:no-pull-number"; return 0
  fi

  echo "submit:${pull_number}"
}

# ── 自己テスト（検知ロジック + マーカー書き込みの往復 + 本体 end-to-end・ネットワーク非依存）────
run_self_test() {
  local pass=0 fail=0
  _case() {
    local desc="$1" want="$2" json="$3"
    local got
    got=$(detect_submit "$json" "acme" "widgets")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }

  _case "event 付き create は提出" "submit:42" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"create","event":"COMMENT","owner":"acme","repo":"widgets","pullNumber":42}}'
  _case "submit_pending は提出" "submit:7" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"submit_pending","owner":"acme","repo":"widgets","pullNumber":7}}'
  _case "event なし create は pending 作成のみ" "skip:not-a-submission" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"create","owner":"acme","repo":"widgets","pullNumber":42}}'
  _case "resolve_thread は対象外" "skip:not-a-submission" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"resolve_thread","threadId":"PRRT_x"}}'
  _case "delete_pending は対象外" "skip:not-a-submission" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"delete_pending","owner":"acme","repo":"widgets","pullNumber":42}}'
  _case "別リポジトリは対象外" "skip:other-repo" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"submit_pending","owner":"acme","repo":"other","pullNumber":42}}'
  _case "add_comment_to_pending_review は対象外" "skip:not-review-write-tool" \
    '{"tool_name":"mcp__github__add_comment_to_pending_review","tool_input":{"pullNumber":42}}'
  _case "pullNumber 欠落は対象外" "skip:no-pull-number" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"submit_pending","owner":"acme","repo":"widgets"}}'
  _case "pullNumber 非数値は対象外" "skip:no-pull-number" \
    '{"tool_name":"mcp__github__pull_request_review_write","tool_input":{"method":"submit_pending","owner":"acme","repo":"widgets","pullNumber":"../../etc/passwd"}}'
  _case "壊れた入力でも落ちない" "skip:not-review-write-tool" '{'

  _slug() {
    local desc="$1" want="$2" url="$3" got
    got=$(hook_repo_slug_from_url "$url")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }
  _slug "https（.git なし）" "acme/widgets" "https://github.com/acme/widgets"
  _slug "ssh 形式" "acme/widgets" "git@github.com:acme/widgets.git"

  # マーカー書き込みの往復テスト（実 git dir を汚さないよう一時ディレクトリで実施）
  local tmp_marker_dir marker_file
  tmp_marker_dir=$(mktemp -d 2>/dev/null || echo "")
  if [[ -n "$tmp_marker_dir" ]]; then
    marker_file="${tmp_marker_dir}/claude-layer1-reviewed-sess123-99"
    : > "$marker_file"
    if [[ -f "$marker_file" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ マーカー touch: ファイルが作られなかった"
    fi
    rm -rf "$tmp_marker_dir"
  fi

  # 本体（stdin → 実処理 → マーカー touch）の end-to-end テスト。実プロセスとして起動し、
  # CLAUDE_HOOK_LAYER1_MARKER_DIR で書き込み先だけ一時ディレクトリへ差し替える（実 git dir は汚さない）。
  # 実リポジトリの origin をそのまま owner/repo チェックに使うため、git 管理下でのみ実施する。
  if git rev-parse --show-toplevel >/dev/null 2>&1; then
    local e2e_dir e2e_out real_origin real_slug real_owner real_repo
    e2e_dir=$(mktemp -d 2>/dev/null || echo "")
    real_origin=$(git remote get-url origin 2>/dev/null || echo "")
    real_slug=$(hook_repo_slug_from_url "$real_origin")
    real_owner="${real_slug%%/*}"
    real_repo="${real_slug##*/}"
    if [[ -n "$e2e_dir" && -n "$real_owner" && -n "$real_repo" ]]; then
      e2e_out=$(CLAUDE_HOOK_LAYER1_MARKER_DIR="$e2e_dir" bash "${BASH_SOURCE[0]}" <<< \
        "{\"session_id\":\"e2e-test-sess\",\"tool_name\":\"mcp__github__pull_request_review_write\",\"tool_input\":{\"method\":\"submit_pending\",\"owner\":\"${real_owner}\",\"repo\":\"${real_repo}\",\"pullNumber\":123456}}")
      if [[ -f "${e2e_dir}/claude-layer1-reviewed-e2e-test-sess-123456" ]]; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
        echo "  ✗ e2e: 本体をサブプロセス起動してもマーカーが作られなかった（出力: ${e2e_out}）"
      fi
    else
      echo "  (e2e スキップ: origin リモートを解決できない環境)"
    fi
    [[ -n "$e2e_dir" ]] && rm -rf "$e2e_dir"
  else
    echo "  (e2e スキップ: git リポジトリ外)"
  fi

  echo "[post-review-write-mark --self-test] PASS=$pass FAIL=$fail"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit $?
fi

# ── 本体 ─────────────────────────────────────────────────────────────
input=$(cat 2>/dev/null || true)

command -v jq >/dev/null 2>&1 || exit 0
if ! git rev-parse --git-dir >/dev/null 2>&1; then exit 0; fi
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" 2>/dev/null || exit 0
GIT_DIR_PATH="$(git rev-parse --git-dir 2>/dev/null || echo "")"
[[ -n "$GIT_DIR_PATH" ]] || exit 0

origin_url=$(git remote get-url origin 2>/dev/null || echo "")
origin_slug=$(hook_repo_slug_from_url "$origin_url")
expected_owner="${origin_slug%%/*}"
expected_repo="${origin_slug##*/}"

verdict=$(detect_submit "$input" "$expected_owner" "$expected_repo")
[[ "$verdict" == submit:* ]] || exit 0
pull_number="${verdict#submit:}"

session_id=$(hook_extract_session_id "$input")
[[ -n "$session_id" ]] || exit 0

marker_dir="${CLAUDE_HOOK_LAYER1_MARKER_DIR:-$GIT_DIR_PATH}"
mkdir -p "$marker_dir" 2>/dev/null || exit 0
: > "${marker_dir}/claude-layer1-reviewed-${session_id}-${pull_number}" 2>/dev/null || true

exit 0
