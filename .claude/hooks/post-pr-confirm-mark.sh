#!/bin/bash
# post-pr-confirm-mark.sh — 「PR 確認済み」を観測しセッションローカルのマーカーを記録する
# （Issue #543・stop-pr-check.sh とペアで動く非ブロッキング nudge の抑止経路）
#
# 背景: クラウド（CLAUDE_CODE_REMOTE=true）では stop-pr-check.sh がハーネスから PR の有無を
# 判定できず（L-114）、feature ブランチ上では毎ターン「📋 PR 存在確認をお願いします」で
# 差し戻す設計になっている。Claude が実際に mcp__github__create_pull_request /
# mcp__github__list_pull_requests で確認済みでも、その事実はハーネス側から見えないため、
# 続行ターンで同じ差し戻しが繰り返され完了報告の重複を招く（#543）。
#
# 本フックは PostToolUse で「現在ブランチの PR が実在すると確認できた」呼び出しを検知し、
# セッションローカルのマーカーを touch する。stop-pr-check.sh はこのマーカーがあれば
# クラウド分岐（📋 PR 存在確認）に入る前に exit 0 で通す。
#
# 🔴 これは「確認したという事実の観測」であって PR の状態を保証しない。マーカーは
# セッション + ブランチ単位でしか有効でない（他セッション・他ブランチのマーカーでは抑止しない）。
# 🔴 confirm 判定は false negative（confirm し損ねて毎回差し戻される）の方を false positive
#   （実在しない PR を確認済み扱いする）より安全側とみなし、疑わしい入力は必ず skip 側に倒す
#   （L-103 防御が最重要不変条件）。
#
# 検知条件（detect_confirm）:
#   - mcp__github__create_pull_request: tool_input.head（"owner:branch" 形式可）が現在ブランチと
#     一致し、tool_response に number（数値）または /pull/N を含む html_url / url があるとき
#     （実測 2026-09-03: MCP の create_pull_request は {"id":"…","url":"https://github.com/o/r/pull/545"}
#     しか返さない。number/html_url だけを見ると実運用で一度もマークされない・#543）
#   - mcp__github__list_pull_requests: tool_input.head の ":" 以降が現在ブランチと一致し、
#     tool_response（MCP コンテンツブロック配列 / 配列直値 / {"pull_requests":[...]} ラッパー /
#     文字列化 JSON のいずれか）に state=="open" または merged_at!=null または merged==true の
#     要素が 1 件以上あるとき（実測: fields 指定時は merged_at が落ちて merged だけになる）
#     （実測 2026-09-05: 上記のいずれの形も tool_response の実体は
#     `[{"type":"text","text":"<JSON文字列>"}]` という MCP コンテンツブロック配列で渡ってくる。
#     create/list とも unwrap_mcp（_RESPONSE_NORMALIZE 内）で text を JSON として解釈してから
#     上記の判定にかける。この形を見落とすと常に skip に落ち一度もマークされない・#581）
#   - それ以外・owner/repo 不一致・壊れた JSON は skip（confirm しない）
#
# 出力経路: PostToolUse はブロック不可（exit 2 に効果がない）。常に exit 0。
#
# 入力 (stdin JSON): { "session_id", "tool_name", "tool_input", "tool_response" }
# 自己テスト: bash .claude/hooks/post-pr-confirm-mark.sh --self-test
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_layer1_common.sh
source "$HOOK_DIR/lib/hook_layer1_common.sh"

# tool_response を正規化して jq に渡す共通フィルタ（#581）。
# 実測（2026-09-05・e2e ダンプ）: MCP ツールの tool_response は素の配列/オブジェクトではなく、
# MCP コンテンツブロック配列 `[{"type":"text","text":"<JSON文字列>"}]` として渡ってくる
# （list の空応答は `[{"type":"text","text":"[]"}]`）。unwrap_mcp でこの形を検出したら
# 各ブロックの text を連結し、それを実ペイロードとして扱う。旧来の「オブジェクト直値 /
# 文字列化 JSON」形（自己テストの合成ケース・将来ハーネスが変わった場合）はこの条件に
# 一致しないため素通りし、後段の fromjson 試行に委ねる（後方互換を壊さない）。
_RESPONSE_NORMALIZE='
  def unwrap_mcp:
    if (type == "array") and (length > 0)
       and (all(.[]; (type == "object") and ((.type? // "") == "text") and ((.text? // null) | type) == "string"))
    then ([.[] | .text] | join(""))
    else . end;
  def norm:
    unwrap_mcp
    | if type == "string" then (try fromjson catch null) else . end;
'

# ── 検知: このツール呼び出しは「現在ブランチの PR が実在すると確認できた」か ──────────
# 標準出力に judgement（confirm:<branch> / skip:<理由>）を出す。副作用は持たない。
detect_confirm() {
  local input="$1" expected_owner="$2" expected_repo="$3" current_branch="$4"
  local tool_name owner repo head_raw head

  tool_name=$(printf '%s' "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")

  if [[ "$tool_name" != "mcp__github__create_pull_request" && "$tool_name" != "mcp__github__list_pull_requests" ]]; then
    echo "skip:not-pr-confirm-tool"; return 0
  fi

  owner=$(printf '%s' "$input" | jq -r '.tool_input.owner // ""' 2>/dev/null || echo "")
  repo=$(printf '%s' "$input" | jq -r '.tool_input.repo // ""' 2>/dev/null || echo "")
  if ! hook_owner_repo_match "$owner" "$repo" "$expected_owner" "$expected_repo"; then
    echo "skip:other-repo"; return 0
  fi

  head_raw=$(printf '%s' "$input" | jq -r '.tool_input.head // ""' 2>/dev/null || echo "")
  head="${head_raw##*:}"
  if [[ -z "$head" || "$head" != "$current_branch" ]]; then
    echo "skip:branch-mismatch"; return 0
  fi

  if [[ "$tool_name" == "mcp__github__create_pull_request" ]]; then
    local has_pr
    has_pr=$(printf '%s' "$input" | jq -r "
      ${_RESPONSE_NORMALIZE}
      (.tool_response | norm) as \$r
      | if (\$r | type) == \"object\"
           and (((\$r.number? | type) == \"number\")
                or (((\$r.html_url? // \"\") | tostring) | test(\"/pull/[0-9]+\"))
                or (((\$r.url? // \"\") | tostring) | test(\"/pull/[0-9]+\")))
        then \"yes\" else \"no\" end
    " 2>/dev/null || echo "no")
    if [[ "$has_pr" == "yes" ]]; then
      echo "confirm:${current_branch}"
    else
      echo "skip:no-pr-in-response"
    fi
    return 0
  fi

  # mcp__github__list_pull_requests
  local has_match
  has_match=$(printf '%s' "$input" | jq -r "
    ${_RESPONSE_NORMALIZE}
    (.tool_response | norm) as \$r
    | (if (\$r | type) == \"array\" then \$r
       elif (\$r | type) == \"object\" and ((\$r.pull_requests?) | type) == \"array\" then \$r.pull_requests
       else [] end) as \$arr
    | if (\$arr | map(select((.state? == \"open\") or ((.merged_at? // null) != null) or (.merged? == true))) | length) > 0
      then \"yes\" else \"no\" end
  " 2>/dev/null || echo "no")
  if [[ "$has_match" == "yes" ]]; then
    echo "confirm:${current_branch}"
  else
    echo "skip:no-match-in-response"
  fi
}

# ── 自己テスト（検知ロジック + branch_key サニタイズ + マーカー往復 + 本体 e2e）────────
run_self_test() {
  local pass=0 fail=0
  _case() {
    local desc="$1" want="$2" branch="$3" json="$4"
    local got
    got=$(detect_confirm "$json" "acme" "widgets" "$branch")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }

  _case "create 成功（number 応答）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":{"number":42}}'
  _case "create 成功（owner:branch head + html_url 応答）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":{"html_url":"https://github.com/acme/widgets/pull/42"}}'
  _case "create 成功（実測形: id + url のみ）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x","base":"main"},"tool_response":{"id":"4430777819","url":"https://github.com/acme/widgets/pull/545"}}'
  _case "create 応答の url が PR でない（issues）は skip" "skip:no-pr-in-response" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":{"id":"1","url":"https://github.com/acme/widgets/issues/9"}}'
  _case "list merged==true（fields 指定で merged_at 無し）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x","state":"all"},"tool_response":[{"html_url":"https://github.com/acme/widgets/pull/5","merged":true,"number":5,"state":"closed"}]}'
  _case "create head 不一致は skip" "skip:branch-mismatch" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/other"},"tool_response":{"number":42}}'
  _case "create 応答に PR 情報なしは skip" "skip:no-pr-in-response" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":{"message":"error"}}'
  _case "list open ありは confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"state":"open","merged_at":null}]}'
  _case "list merged ありは confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"state":"closed","merged_at":"2026-09-01T00:00:00Z"}]}'
  _case "list closed 未マージのみは skip" "skip:no-match-in-response" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"state":"closed","merged_at":null}]}'
  _case "list 空配列は skip" "skip:no-match-in-response" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[]}'
  _case "list head 不一致は skip" "skip:branch-mismatch" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/other"},"tool_response":[{"state":"open"}]}'
  _case "list 文字列化 JSON（ラッパー付き）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":"{\"pull_requests\":[{\"state\":\"open\"}]}"}'
  _case "create 文字列化 JSON は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":"{\"number\":42}"}'
  # 実測形（#581・2026-09-05）: MCP ツールの tool_response は
  # `[{"type":"text","text":"<JSON文字列>"}]` というコンテンツブロック配列で渡ってくる
  # （素の配列/オブジェクト直値ではない）。この形を検出できないと list/create とも
  # 常に skip に落ち、マーカーが一度も作られなかった（実セッションで確認済み）。
  _case "list 空応答（実測の MCP コンテンツブロック形）は skip" "skip:no-match-in-response" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"type":"text","text":"[]"}]}'
  _case "list open あり（実測の MCP コンテンツブロック形）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"type":"text","text":"[{\"state\":\"open\"}]"}]}'
  _case "create 成功（実測の MCP コンテンツブロック形・number 応答）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":[{"type":"text","text":"{\"number\":42}"}]}'
  _case "create 成功（実測の MCP コンテンツブロック形・url 応答）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__create_pull_request","tool_input":{"owner":"acme","repo":"widgets","head":"feat/x"},"tool_response":[{"type":"text","text":"{\"id\":\"1\",\"url\":\"https://github.com/acme/widgets/pull/545\"}"}]}'
  # Layer 1 レビュー指摘（#585）: unwrap_mcp は複数ブロックを join("") で連結する実装だが、
  # 単一ブロックのケースしか自己テストしていなかった。複数ブロック（末尾が空文字列）でも
  # join 結果が正しい JSON になり confirm へ到達することを固定する（回帰ロック）。
  _case "list 複数コンテンツブロック（末尾空文字列）は confirm" "confirm:feat/x" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"type":"text","text":"[{\"state\":\"open\"}]"},{"type":"text","text":""}]}'
  # type が text 以外のブロックが混ざる場合は unwrap_mcp の all() 条件を満たさず素通りし
  # （コンテンツブロックとして解釈しない＝安全側の skip）、誤って confirm しないことを固定する。
  _case "list 非 text 型ブロック混在は skip（安全側）" "skip:no-match-in-response" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"widgets","head":"acme:feat/x"},"tool_response":[{"type":"image","text":"open"}]}'
  _case "別リポジトリは skip" "skip:other-repo" "feat/x" \
    '{"tool_name":"mcp__github__list_pull_requests","tool_input":{"owner":"acme","repo":"other","head":"acme:feat/x"},"tool_response":[{"state":"open"}]}'
  _case "対象外ツールは skip" "skip:not-pr-confirm-tool" "feat/x" \
    '{"tool_name":"mcp__github__merge_pull_request","tool_input":{"owner":"acme","repo":"widgets"}}'
  _case "壊れた JSON でも落ちない" "skip:not-pr-confirm-tool" "feat/x" '{'

  _bk() { # $2 は期待する正規表現（ハッシュ成分は固定値で比較しない）
    local desc="$1" want="$2" input="$3" got
    got=$(hook_branch_key "$input")
    if [[ "$got" =~ $want ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }
  _bk_distinct() {
    local desc="$1" a="$2" b="$3" ka kb
    ka=$(hook_branch_key "$a"); kb=$(hook_branch_key "$b")
    if [[ "$ka" != "$kb" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: 同一キー $ka"
    fi
  }
  _bk "通常のブランチ名（スラッシュ含む）はサニタイズ接頭辞 + 12 桁ハッシュになる" "^featx-[0-9a-f]{12}$" "feat/x"
  _bk "ドット・アンダースコア・ハイフンは許可文字として残る" "^feat_x-1\.2-[0-9a-f]{12}$" "feat_x-1.2"
  _bk "危険文字（; とスペース）は除去される" "^featxrm-rf-[0-9a-f]{12}$" 'feat/x; rm -rf'
  _bk "許可文字ゼロのブランチ名でも空にならない" "^branch-[0-9a-f]{12}$" "認証"
  _bk_distinct "除去対象文字だけが異なるブランチは別キーになる（衝突防止）" "feat/認証機能" "feat/決済機能"
  _bk_distinct "スラッシュ位置だけが異なるブランチは別キーになる" "fe/atx" "feat/x"

  # マーカー書き込みの往復テスト（実 git dir を汚さないよう一時ディレクトリで実施）
  local tmp_marker_dir marker_file
  tmp_marker_dir=$(mktemp -d 2>/dev/null || echo "")
  if [[ -n "$tmp_marker_dir" ]]; then
    marker_file="${tmp_marker_dir}/claude-pr-confirmed-sess123-featx"
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
  # CLAUDE_HOOK_PR_MARKER_DIR で書き込み先だけ一時ディレクトリへ差し替える（実 git dir は汚さない）。
  if git rev-parse --show-toplevel >/dev/null 2>&1; then
    local e2e_dir e2e_out real_origin real_slug real_owner real_repo real_branch real_key
    e2e_dir=$(mktemp -d 2>/dev/null || echo "")
    real_origin=$(git remote get-url origin 2>/dev/null || echo "")
    real_slug=$(hook_repo_slug_from_url "$real_origin")
    real_owner="${real_slug%%/*}"
    real_repo="${real_slug##*/}"
    real_branch=$(git branch --show-current 2>/dev/null || echo "")
    real_key=$(hook_branch_key "$real_branch")
    if [[ -n "$e2e_dir" && -n "$real_owner" && -n "$real_repo" && -n "$real_branch" ]]; then
      e2e_out=$(CLAUDE_HOOK_PR_MARKER_DIR="$e2e_dir" bash "${BASH_SOURCE[0]}" <<< \
        "{\"session_id\":\"e2e-test-sess\",\"tool_name\":\"mcp__github__create_pull_request\",\"tool_input\":{\"owner\":\"${real_owner}\",\"repo\":\"${real_repo}\",\"head\":\"${real_branch}\"},\"tool_response\":{\"number\":123456}}")
      if [[ -f "${e2e_dir}/claude-pr-confirmed-e2e-test-sess-${real_key}" ]]; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
        echo "  ✗ e2e: 本体をサブプロセス起動してもマーカーが作られなかった（出力: ${e2e_out}）"
      fi

      # skip 相当（head 不一致）ではマーカーが作られないことも確認
      e2e_out=$(CLAUDE_HOOK_PR_MARKER_DIR="$e2e_dir" bash "${BASH_SOURCE[0]}" <<< \
        "{\"session_id\":\"e2e-mismatch-sess\",\"tool_name\":\"mcp__github__create_pull_request\",\"tool_input\":{\"owner\":\"${real_owner}\",\"repo\":\"${real_repo}\",\"head\":\"definitely-not-current-branch\"},\"tool_response\":{\"number\":999}}")
      if [[ ! -f "${e2e_dir}/claude-pr-confirmed-e2e-mismatch-sess-definitely-not-current-branch" ]]; then
        pass=$((pass + 1))
      else
        fail=$((fail + 1))
        echo "  ✗ e2e: head 不一致なのにマーカーが作られた"
      fi
    else
      echo "  (e2e スキップ: origin リモート/現在ブランチを解決できない環境)"
    fi
    [[ -n "$e2e_dir" ]] && rm -rf "$e2e_dir"
  else
    echo "  (e2e スキップ: git リポジトリ外)"
  fi

  echo "[post-pr-confirm-mark --self-test] PASS=$pass FAIL=$fail"
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

current_branch=$(git branch --show-current 2>/dev/null || echo "")
[[ -n "$current_branch" ]] || exit 0

origin_url=$(git remote get-url origin 2>/dev/null || echo "")
origin_slug=$(hook_repo_slug_from_url "$origin_url")
expected_owner="${origin_slug%%/*}"
expected_repo="${origin_slug##*/}"
# origin が解決できないときは hook_owner_repo_match がフェイルオープン（期待値空 = 一致扱い）に
# なる。本フックは L-103 の安全網（stop-pr-check.sh）を抑止する側なので、ここだけは
# フェイルクローズにする（owner/repo を照合できないなら別リポジトリ宛の呼び出しを
# 「このブランチの PR 確認」と誤認しない・マーカーを作らない）。
if [[ -z "$origin_slug" || "$origin_slug" != */* || -z "$expected_owner" || -z "$expected_repo" ]]; then
  exit 0
fi

verdict=$(detect_confirm "$input" "$expected_owner" "$expected_repo" "$current_branch")
[[ "$verdict" == confirm:* ]] || exit 0

session_id=$(hook_extract_session_id "$input")
[[ -n "$session_id" ]] || exit 0

marker_dir="${CLAUDE_HOOK_PR_MARKER_DIR:-$GIT_DIR_PATH}"
mkdir -p "$marker_dir" 2>/dev/null || exit 0
: > "$(hook_pr_confirm_marker_path "$session_id" "$current_branch" "$marker_dir")" 2>/dev/null || true

exit 0
