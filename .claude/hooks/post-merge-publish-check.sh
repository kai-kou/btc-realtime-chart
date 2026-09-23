#!/bin/bash
# post-merge-publish-check.sh — main へのマージ直後に公開リポジトリへの反映を起動する
# （Issue #449・publish-sync レーンの「マージイベント束縛」ハーネス）
#
# 目的: 公開リポジトリ（kai-kou/tsukuru）への反映トリガーを
# 「セッション終了時（stop-publish-check.sh）」「4 時間周期の R-1」ではなく
# **main へマージした瞬間** に束縛する。反映が遅れる根本原因は、ドリフトが生まれる瞬間
# （マージ）と反映が試みられる瞬間（セッション終了 / 定期ルーティン）が切り離されていたこと
# だった（#449 の RC-A）。本フックはその 2 つを同一イベントに束ねる。
#
# 動作: PostToolUse（matcher: mcp__github__merge_pull_request）でマージ成功を検知したら
#   1. マージ先が本リポジトリか判定する（別リポジトリのマージなら何もしない）
#   2. python3 tools/check_publish_drift.py --quiet でドリフトを判定する
#   3. ドリフトあり（1）/ 判定不能（2）なら additionalContext で publish-sync の実行を指示する
# 反映（push）そのものは行わない。push は publish-sync スキルが検証成功マーカー（G-1）を
# 経由してのみ実行する。ハーネスが push しないのは stop-publish-check.sh と同じ方針。
#
# 出力経路: PostToolUse は stdout JSON の hookSpecificOutput.additionalContext に対応する
# （docs/rules/hook-events-reference.md §2）。exit 2（ブロック）は使わない — マージは
# 既に成功しており、ブロックすべき事象ではないため（知らせるだけで止めない）。
#
# 既知の適用範囲: matcher が MCP ツールのため、ローカル実行での `gh pr merge`（Bash 経由）は
# 本フックでは捕捉しない。その経路は次のセッション冒頭（session-start.sh の回収指示）と
# R-1 のプリフライト（docs/routines.md §2.0）が拾う。セッション終了時の backstop フック
# （stop-publish-check.sh）は本リポジトリには無い（クラウドでは MCP が一次経路・L-114）。
#
# 下流（公開レーンを持たないプロジェクト）での挙動: 本フックは配布物に含まれるが、
# tools/check_publish_drift.py が無い環境では何も出力せず exit 0 する（無害に不発する）。
#
# 入力 (stdin JSON): { "tool_name": "...", "tool_input": {...}, "tool_response": {...} }
# 自己テスト: bash .claude/hooks/post-merge-publish-check.sh --self-test
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 検知ロジックだけを評価して結果を標準出力に出し、ドリフト判定を行わないモード（--self-test 用）
DETECT_ONLY="${CLAUDE_HOOK_PUBLISH_MERGE_DETECT_ONLY:-}"

# ── origin リモート URL から owner/repo を取り出す ─────────────────────────
# 注: ERE には遅延量指定子（`+?`）が無く、`([^/]+/[^/]+?)(\.git)?$` は `.git` を貪欲に
# repo 名側へ取り込んでしまう（`<repo>.git` になり照合が必ず外れる）。
# 先に `.git` を落としてから owner/repo を切り出す。
repo_slug_from_url() {
  printf '%s' "$1" | sed -E 's#/+$##; s#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#'
}

# ── 検知: このツール呼び出しは「本リポジトリの PR マージ成功」か ─────────────
# 標準出力に judgement（match / skip:<理由>）を出す。副作用は持たない。
detect_merge() {
  local input="$1" expected_owner="$2" expected_repo="$3"
  local tool_name merged owner repo

  tool_name=$(printf '%s' "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
  if [[ "$tool_name" != "mcp__github__merge_pull_request" ]]; then
    echo "skip:not-merge-tool"; return 0
  fi

  # マージ結果の確認。GitHub API は成功時に merged=true を返す。
  # フィールドが取れない場合（レスポンス形式の差異）は PostToolUse が成功時にのみ発火する
  # 公式仕様に従い「成功」とみなす（取りこぼしより空振りを選ぶ）。
  # `// ` は false を falsy として読み飛ばすため使わない（merged=false を取りこぼす）。
  # レスポンスの入れ子構造に差異があっても拾えるよう再帰的に "merged" キーを探す。
  merged=$(printf '%s' "$input" | jq -r '
    [(.tool_response // {}) | .. | objects | select(has("merged")) | .merged]
    | if length > 0 then (.[0] | tostring) else "unknown" end
  ' 2>/dev/null || echo "unknown")
  if [[ "$merged" == "false" ]]; then
    echo "skip:not-merged"; return 0
  fi

  # マージ先リポジトリの一致確認（別リポジトリの PR をマージしただけなら本レーンは無関係）
  owner=$(printf '%s' "$input" | jq -r '.tool_input.owner // ""' 2>/dev/null || echo "")
  repo=$(printf '%s' "$input" | jq -r '.tool_input.repo // ""' 2>/dev/null || echo "")
  if [[ -n "$owner" && -n "$expected_owner" && "$owner" != "$expected_owner" ]] \
     || [[ -n "$repo" && -n "$expected_repo" && "$repo" != "$expected_repo" ]]; then
    echo "skip:other-repo"; return 0
  fi

  echo "match"
}

# ── 自己テスト（検知ロジックのみ・ネットワーク非依存）──────────────────────
run_self_test() {
  local pass=0 fail=0
  _case() {
    local desc="$1" want="$2" json="$3"
    local got
    got=$(detect_merge "$json" "acme" "widgets")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }

  _case "本リポジトリのマージ成功" "match" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets","pullNumber":1},"tool_response":{"merged":true}}'
  _case "merged フィールド不在でも成功扱い" "match" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets"},"tool_response":{}}'
  _case "merged=false は対象外" "skip:not-merged" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets"},"tool_response":{"merged":false}}'
  _case "別リポジトリのマージは対象外" "skip:other-repo" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets-dist"},"tool_response":{"merged":true}}'
  _case "別オーナーのマージは対象外" "skip:other-repo" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"someone-else","repo":"widgets"},"tool_response":{"merged":true}}'
  _case "マージ以外のツールは対象外" "skip:not-merge-tool" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets"}}'
  _case "PR 作成後の Bash は対象外" "skip:not-merge-tool" \
    '{"tool_name":"Bash","tool_input":{"command":"gh pr merge 1 --squash"}}'
  _case "壊れた入力でも落ちない" "skip:not-merge-tool" '{'

  # origin URL のバリエーション（`.git` 付きで repo 名がずれると本フックは永久に不発する）
  _slug() {
    local desc="$1" want="$2" url="$3" got
    got=$(repo_slug_from_url "$url")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }
  _slug "https（.git なし）" "acme/widgets" "https://github.com/acme/widgets"
  _slug "https（.git あり）" "acme/widgets" "https://github.com/acme/widgets.git"
  _slug "ssh 形式" "acme/widgets" "git@github.com:acme/widgets.git"
  _slug "末尾スラッシュ" "acme/widgets" "https://github.com/acme/widgets/"

  echo "[post-merge-publish-check --self-test] PASS=$pass FAIL=$fail"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit $?
fi

# ── 本体 ─────────────────────────────────────────────────────────────
input=$(cat 2>/dev/null || true)

# トグル（stop-publish-check.sh と共有。publish レーン全体を止めたいときに使う）
if [[ "${CLAUDE_HOOK_SKIP_PUBLISH_CHECK:-}" == "true" ]]; then exit 0; fi

command -v jq >/dev/null 2>&1 || exit 0
if ! git rev-parse --git-dir >/dev/null 2>&1; then exit 0; fi
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" 2>/dev/null || exit 0

# 本リポジトリの owner/repo を origin リモートから解決する（ハードコードしない）
origin_url=$(git remote get-url origin 2>/dev/null || echo "")
origin_slug=$(repo_slug_from_url "$origin_url")
expected_owner="${origin_slug%%/*}"
expected_repo="${origin_slug##*/}"

verdict=$(detect_merge "$input" "$expected_owner" "$expected_repo")
if [[ -n "$DETECT_ONLY" ]]; then echo "$verdict"; exit 0; fi
[[ "$verdict" == "match" ]] || exit 0

# CLAUDE_PLUGIN_ROOT 分離の対象外（#539 レビューで確認）: check_publish_drift.py は
# REPO_ROOT = Path(__file__).resolve().parent.parent で「自分自身が置かれた場所」を
# 開発リポジトリとみなし、そこから scripts/publish-snapshot.sh を実行して比較材料を作る
# 設計のため、コピー先から実行すると消費先プロジェクトではなくコピー先自身を比較してしまい
# 誤判定になる。publish-sync レーンは本ベース自身の開発→公開リポジトリ同期専用機能で、
# 第三者プロジェクトへの一般化を想定しないため、探索先は REPO_ROOT 固定のままでよい。
DRIFT_SCRIPT="$REPO_ROOT/tools/check_publish_drift.py"
if [[ ! -f "$DRIFT_SCRIPT" ]] || ! command -v python3 >/dev/null 2>&1; then exit 0; fi

drift_exit=0
timeout 60s python3 "$DRIFT_SCRIPT" --quiet >/dev/null 2>&1 || drift_exit=$?

# 同期済み（0）なら何も言わない。それ以外（ドリフトあり=1・判定不能=2・タイムアウト=124）は
# 反映を指示する。判定不能を「反映不要」と読み替えないのが本レーンの安全側（publish-sync-rules.md §5）。
[[ "$drift_exit" -eq 0 ]] && exit 0

if [[ "$drift_exit" -eq 1 ]]; then
  reason="公開リポジトリへ未反映の差分を検知しました（ドリフトあり）。"
else
  reason="公開リポジトリとのドリフト判定が完了しませんでした（exit ${drift_exit}）。安全側に倒して反映を試みてください。"
fi

msg="📦 main へのマージを検知しました。${reason}
このマージで生まれた差分を放置せず、**このセッションの中で** \`publish-sync\` スキル（.claude/skills/publish-sync/SKILL.md）を実行して公開リポジトリ kai-kou/tsukuru への反映まで完遂してください。ユーザー確認は不要です（publish-sync-rules.md §2 の恒久委任）。
反映できない事情（配布物が本セッションへ供給されていない・\`add_repo\` が提供されない自動タスク実行モード等・L-117）があるときは、黙って終わらせず SKILL.md §3 に従って \`[publish-sync]\` Issue に失敗段階とエラー全文を記録してください。マージした本セッションが記録を残さないと、次にインタラクティブセッションが実行されるまで反映が滞留します（Issue #449 の再発）。"

jq -n --arg ctx "$msg" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
exit 0
