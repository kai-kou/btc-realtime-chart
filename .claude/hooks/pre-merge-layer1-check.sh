#!/bin/bash
# pre-merge-layer1-check.sh — マージ直前に Layer 1 セルフレビュー未観測を非ブロッキング警告する
# （Issue #512・post-review-write-mark.sh が立てるマーカーを読むだけの読み取り専用チェック）
#
# 🔴 ハードブロックにしない。マーカーはセッションローカルであり、PR 作成セッション ≠
# マージセッション（pr-review-watcher の復帰・`--mine` 判定）という正当なフローでは
# マーカーが正当に不在になる。exit 2 で止めると復帰フローを誤検知で塞ぐ害の方が大きいため、
# 常に exit 0 で通し、未観測時のみ additionalContext で注意喚起するだけに留める。
#
# 🔴 これが証明するのは手続きであって品質ではない。指摘ゼロの空レビューでもマーカーは立つ。
#
# 射程: PR 作成 → マージが同一セッション内で完結する場合の限定的な nudge。
# fan-out 全体の完了契約の代替ではない（別セッションでのマージは常に警告が出るが、それは
# 「未実施の証拠」ではなく「同一セッション内では確認できない」という意味に過ぎない）。
#
# 入力 (stdin JSON): { "session_id": "...", "tool_name": "...", "tool_input": {...} }
# 自己テスト: bash .claude/hooks/pre-merge-layer1-check.sh --self-test
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_layer1_common.sh
source "$HOOK_DIR/lib/hook_layer1_common.sh"

# ── 検知: このツール呼び出しは「本リポジトリの PR マージ」か ─────────────────
detect_merge_attempt() {
  local input="$1" expected_owner="$2" expected_repo="$3"
  local tool_name owner repo pull_number

  tool_name=$(printf '%s' "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
  if [[ "$tool_name" != "mcp__github__merge_pull_request" ]]; then
    echo "skip:not-merge-tool"; return 0
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

  echo "check:${pull_number}"
}

# ── 自己テスト（検知ロジック + マーカー有無判定 + 本体 end-to-end・ネットワーク非依存）────
run_self_test() {
  local pass=0 fail=0
  _case() {
    local desc="$1" want="$2" json="$3"
    local got
    got=$(detect_merge_attempt "$json" "acme" "widgets")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }

  _case "本リポジトリの merge は検知対象" "check:42" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets","pullNumber":42}}'
  _case "別リポジトリは対象外" "skip:other-repo" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"other","pullNumber":42}}'
  _case "merge 以外のツールは対象外" "skip:not-merge-tool" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets"}}'
  _case "pullNumber 欠落は対象外" "skip:no-pull-number" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets"}}'
  _case "pullNumber 非数値は対象外" "skip:no-pull-number" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets","pullNumber":"../../etc/passwd"}}'
  _case "壊れた入力でも落ちない" "skip:not-merge-tool" '{'

  # マーカー有無判定の往復テスト（実 git dir を汚さないよう一時ディレクトリで実施）
  local tmp_marker_dir
  tmp_marker_dir=$(mktemp -d 2>/dev/null || echo "")
  if [[ -n "$tmp_marker_dir" ]]; then
    : > "${tmp_marker_dir}/claude-layer1-reviewed-sess123-42"
    if [[ -f "${tmp_marker_dir}/claude-layer1-reviewed-sess123-42" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ マーカーあり判定: ファイルが見つからなかった"
    fi
    if [[ ! -f "${tmp_marker_dir}/claude-layer1-reviewed-sess123-999" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ マーカーなし判定: 存在しないはずのファイルが見つかった"
    fi
    rm -rf "$tmp_marker_dir"
  fi

  # 本体（stdin → 実処理 → additionalContext 出力）の end-to-end テスト。実プロセスとして起動し、
  # CLAUDE_HOOK_LAYER1_MARKER_DIR で参照先だけ一時ディレクトリへ差し替える（実 git dir は汚さない）。
  if git rev-parse --show-toplevel >/dev/null 2>&1; then
    local e2e_dir e2e_out real_origin real_slug real_owner real_repo
    e2e_dir=$(mktemp -d 2>/dev/null || echo "")
    real_origin=$(git remote get-url origin 2>/dev/null || echo "")
    real_slug=$(hook_repo_slug_from_url "$real_origin")
    real_owner="${real_slug%%/*}"
    real_repo="${real_slug##*/}"
    if [[ -n "$e2e_dir" && -n "$real_owner" && -n "$real_repo" ]]; then
      # マーカーなし → additionalContext 警告 JSON が出る
      e2e_out=$(CLAUDE_HOOK_LAYER1_MARKER_DIR="$e2e_dir" bash "${BASH_SOURCE[0]}" <<< \
        "{\"session_id\":\"e2e-no-marker\",\"tool_name\":\"mcp__github__merge_pull_request\",\"tool_input\":{\"owner\":\"${real_owner}\",\"repo\":\"${real_repo}\",\"pullNumber\":223456}}")
      if printf '%s' "$e2e_out" | jq -e '.hookSpecificOutput.additionalContext | length > 0' >/dev/null 2>&1; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
        echo "  ✗ e2e マーカーなし: additionalContext 警告が出なかった（出力: ${e2e_out}）"
      fi

      # マーカーあり → 無出力
      : > "${e2e_dir}/claude-layer1-reviewed-e2e-with-marker-223457"
      e2e_out=$(CLAUDE_HOOK_LAYER1_MARKER_DIR="$e2e_dir" bash "${BASH_SOURCE[0]}" <<< \
        "{\"session_id\":\"e2e-with-marker\",\"tool_name\":\"mcp__github__merge_pull_request\",\"tool_input\":{\"owner\":\"${real_owner}\",\"repo\":\"${real_repo}\",\"pullNumber\":223457}}")
      if [[ -z "$e2e_out" ]]; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
        echo "  ✗ e2e マーカーあり: 無出力のはずが出力があった（出力: ${e2e_out}）"
      fi
    else
      echo "  (e2e スキップ: origin リモートを解決できない環境)"
    fi
    [[ -n "$e2e_dir" ]] && rm -rf "$e2e_dir"
  else
    echo "  (e2e スキップ: git リポジトリ外)"
  fi

  echo "[pre-merge-layer1-check --self-test] PASS=$pass FAIL=$fail"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit $?
fi

# ── 本体 ─────────────────────────────────────────────────────────────
input=$(cat 2>/dev/null || true)

# jq / git が使えない環境では判定不能なので黙って通す（フェイルオープン。ブロックしない設計と整合）
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

verdict=$(detect_merge_attempt "$input" "$expected_owner" "$expected_repo")
[[ "$verdict" == check:* ]] || exit 0
pull_number="${verdict#check:}"

session_id=$(hook_extract_session_id "$input")
[[ -n "$session_id" ]] || exit 0

marker_dir="${CLAUDE_HOOK_LAYER1_MARKER_DIR:-$GIT_DIR_PATH}"
marker_file="${marker_dir}/claude-layer1-reviewed-${session_id}-${pull_number}"

[[ -f "$marker_file" ]] && exit 0

msg="[pre-merge-layer1-check] このセッション内で PR #${pull_number} への Layer 1 セルフレビュー投稿（mcp__github__pull_request_review_write の提出）を観測できませんでした。
PR 作成セッションと別セッションでのマージ（pr-review-watcher の復帰フロー等）なら正当な状態です。
未実施なら Skill(code-review) で PR 差分をレビューし、指摘は行単位インラインコメントで記録してからマージしてください（FAIR Layer 1・docs/rules/ai-reviewer-strategy.md）。これはブロックではありません。"

jq -n --arg ctx "$msg" \
  '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}'
exit 0
