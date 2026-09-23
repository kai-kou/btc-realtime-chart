#!/usr/bin/env bash
# tools/test_pre_pr_create_check.sh — pre-pr-create-check.sh の
# 「PR 本文チェック（非ブロッキング・Issue #627 対策 C/D）」回帰テスト。
#
# 検証する不変条件:
#   1. 全要素が揃った本文（Session-Id:・「## テスト・確認内容」配下に python3 行・PR 前レビュー 行）
#      → 新規 Warning が1つも出ない（exit 0）
#   2. Session-Id: と「テスト・確認内容」が無い本文 → additionalContext に新規 Warning が2件含まれる
#      （exit 0・ブロックしない）
#   3. CLAUDE_BASE_DISABLE_PR_BODY_CHECK=1 → 2 と同じ本文でも新規 Warning が出ない
#   4. gh pr create --body "..."（Bash 経路）でも 2 と同様に検出される
#   5. 高リスク差分（.claude/settings.json 相当パス）なのに「エッジケース」記載が無い本文 →
#      エッジケース Warning のみが検出される（has_code=false のため他の3件は出ない）。
#      同じ差分に記録なし本文を渡すと、high_risk 単独でも「PR 前レビュー」記録欠落を含む 4 件が出る
#   6. gh pr create --body-file / -F（ファイル経由）でも 2 と同様に検出される
#   7. 箇条書き + インラインコード形式の証跡（テンプレート形式）は「検証証跡なし」にならない
#   8. gh pr create --body "$(cat <<'EOF' ... EOF)"（heredoc・本文に奇数個の "）でも本文が取り出され、
#      欠落があれば検出される（shlex が失敗しても無警告で素通りしない）
#   9. gh pr create --body-file の相対パスは hook 入力の .cwd（呼び出し元 cwd）基準で解決され、
#      リポジトリ直下の同名ファイルを誤って読まない
#  10. 未記入テンプレート（`Session-Id: {UUID}`・`PR 前レビュー: 検出 N 件`・見出し + プレースホルダ行だけの
#      エッジケース表）は「記載あり」と誤判定されず 3 件検出される。値・記録・データ行を埋めた本文と
#      `PR 前レビュー: スキップ（理由）` の書式は 0 件（#627 Layer 2 指摘）。`PR 前レビュー: 実施予定` のような
#      自由記述は記録ありと認めず、地の文の「エッジケース」+ 無関係な表はエッジケース表と見なさない
#      （#627 Layer 1 再レビュー指摘）
#  11. gh pr create --body-file が FIFO を指しても open でハングせず即座に通過する（本文なし扱い・Warning なし）
#  12. 本文抽出の外側 timeout（10 秒）が発火して exit=124 になっても、フックは `set -e` で異常終了せず
#      exit 0 のまま Layer 1 リマインダー等の additionalContext を出力する（本文なし扱い・Warning なし・
#      #627 Layer 1 再レビュー指摘）
#  13. PR 本文を self_review_check.py に渡す一時ファイル（claude-prbody.*）が、フック正常終了後に
#      残留しない（trap EXIT による削除・#628 Layer 1 テスト・検証指摘）
#
# has_code / high_risk の判定対象は一時リポジトリの git 差分そのもの（tools/detect_pr_diff_type.py
# を base コミットへ同梱して一時リポジトリ内でも実行できるようにし、フィクスチャファイルの追加で
# has_code=true / high_risk=true を作り分ける）。ネットワークは使わない（ローカル bare リモートのみ）。
#
# 使い方: bash tools/test_pre_pr_create_check.sh
# 終了コード: 0 = 全 PASS / 1 = 1 件以上 FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.claude/hooks/pre-pr-create-check.sh"
DETECT_TOOL="$REPO_ROOT/tools/detect_pr_diff_type.py"
SELF_REVIEW_TOOL="$REPO_ROOT/tools/self_review_check.py"
[ -f "$HOOK" ] || { echo "FATAL: フックが見つかりません: $HOOK"; exit 1; }
[ -f "$DETECT_TOOL" ] || { echo "FATAL: detect_pr_diff_type.py が見つかりません: $DETECT_TOOL"; exit 1; }
[ -f "$SELF_REVIEW_TOOL" ] || { echo "FATAL: self_review_check.py が見つかりません: $SELF_REVIEW_TOOL"; exit 1; }
# shellcheck source=tools/lib/test_harness.sh
source "$REPO_ROOT/tools/lib/test_harness.sh"

BRANCH="feat/test-pr-body-check"

# setup_repo [extra_path] [extra_content]
# origin（bare）+ clean・push 済みの作業ブランチを持つ一時リポジトリを作る。
# tools/detect_pr_diff_type.py と tools/self_review_check.py は base コミット（main・origin と
# 共有する祖先）に同梱するため、一時リポジトリ内でも Layer 0 の has_code/high_risk 判定、および
# PR 本文チェック（Session-Id 等・Issue #628 で self_review_check.py に集約済み）が実行できる
# （base に含めることで判定対象の diff 自体には現れない）。extra_path/extra_content を渡すと
# base の後に追加コミットとして作業ブランチへ push し、has_code/high_risk 判定をフィクスチャ
# ごとに作り分ける（例: .py を追加すれば has_code=true、.claude/settings.json 相当パスなら
# high_risk=true）。
setup_repo() {
  local extra_path="${1:-}" extra_content="${2:-}"
  WORK=$(mktemp -d)
  init_tmp_git_repo "$WORK"

  mkdir -p "$WORK/repo/tools"
  cp "$DETECT_TOOL" "$WORK/repo/tools/detect_pr_diff_type.py"
  cp "$SELF_REVIEW_TOOL" "$WORK/repo/tools/self_review_check.py"
  echo "base" > "$WORK/repo/base.txt"
  git -C "$WORK/repo" add -A
  git -C "$WORK/repo" commit --quiet -m "base"
  git -C "$WORK/repo" push --quiet origin main 2>/dev/null || true
  git -C "$WORK/repo" fetch --quiet origin "+main:refs/remotes/origin/main" 2>/dev/null || true

  git -C "$WORK/repo" checkout --quiet -b "$BRANCH"
  if [ -n "$extra_path" ]; then
    mkdir -p "$(dirname "$WORK/repo/$extra_path")"
    printf '%s\n' "$extra_content" > "$WORK/repo/$extra_path"
    git -C "$WORK/repo" add -A
    git -C "$WORK/repo" commit --quiet -m "feat: add $(basename "$extra_path") (fixture)"
  fi
  git -C "$WORK/repo" push --quiet -u origin "$BRANCH" 2>/dev/null || true
  git -C "$WORK/repo" fetch --quiet origin "+${BRANCH}:refs/remotes/origin/${BRANCH}" 2>/dev/null || true
}

teardown_repo() { teardown_tmp_repo "$WORK"; }

# run_hook <stdin json>: pre-pr-create-check.sh を作業ブランチ上で起動し
# HOOK_EXIT / HOOK_STDOUT / HOOK_STDERR に結果を残す。
run_hook() {
  local json="$1"
  local out_file err_file
  out_file=$(mktemp); err_file=$(mktemp)
  ( cd "$WORK/repo" && printf '%s' "$json" | bash "$HOOK" >"$out_file" 2>"$err_file" )
  HOOK_EXIT=$?
  HOOK_STDOUT=$(cat "$out_file")
  HOOK_STDERR=$(cat "$err_file")
  rm -f "$out_file" "$err_file"
}

ctx_of() { # ctx_of <hook stdout>: hookSpecificOutput.additionalContext を取り出す
  printf '%s' "$1" | jq -r '.hookSpecificOutput.additionalContext // ""' 2>/dev/null
}

# count_new_warnings <additionalContext>: 本チェックが追加した4項目のうち検出された件数を数える
count_new_warnings() {
  local ctx="$1" n=0
  printf '%s' "$ctx" | grep -q 'PR 本文に Session-Id: が無い' && n=$((n + 1))
  printf '%s' "$ctx" | grep -q '検証証跡なし' && n=$((n + 1))
  printf '%s' "$ctx" | grep -q 'PR 前フレッシュ文脈レビューの記録が無い' && n=$((n + 1))
  printf '%s' "$ctx" | grep -q '高リスク差分（hooks / settings / permissions 等）なのにエッジケース表が無い' && n=$((n + 1))
  echo "$n"
}

mcp_json() { # mcp_json <body>: mcp__github__create_pull_request 呼び出しを模した stdin JSON
  jq -n --arg body "$1" \
    '{tool_name: "mcp__github__create_pull_request", tool_input: {body: $body, head: "irrelevant", owner: "acme", repo: "widgets"}}'
}

GOOD_BODY='Session-Id: sess-good-0001

## テスト・確認内容

```
python3 tools/x.py
```

PR 前レビュー: 検出 0 件（🔴0 🟡0 ⚪0・CONFIRMED 0 / PLAUSIBLE 0）→ 修正 0 件・見送り 0 件'

BAD_BODY='Fixes a small bug in the parser.'

echo "[ケース 1] 全要素が揃った本文（has_code=true）→ 新規 Warning 0 件"
setup_repo "feature.py" '"""Test fixture for has_code=true."""
VALUE = 1'
run_hook "$(mcp_json "$GOOD_BODY")"
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "全要素が揃った本文は exit 0" \
  || report ng "全要素が揃った本文なのに exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
_ctx1=$(ctx_of "$HOOK_STDOUT")
_n1=$(count_new_warnings "$_ctx1")
[ "$_n1" -eq 0 ] \
  && report ok "新規 Warning が0件" \
  || report ng "新規 Warning が ${_n1} 件（期待 0・additionalContext: ${_ctx1}）"
teardown_repo

echo "[ケース 2] Session-Id・テスト・確認内容が無い本文 → 新規 Warning 2 件"
setup_repo
run_hook "$(mcp_json "$BAD_BODY")"
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "本文不足でもブロックしない（exit 0）" \
  || report ng "本文不足なのに exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
_ctx2=$(ctx_of "$HOOK_STDOUT")
_n2=$(count_new_warnings "$_ctx2")
[ "$_n2" -eq 2 ] \
  && report ok "新規 Warning が2件" \
  || report ng "新規 Warning が ${_n2} 件（期待 2・additionalContext: ${_ctx2}）"
printf '%s' "$_ctx2" | grep -q 'PR 本文に Session-Id: が無い' \
  && report ok "Session-Id 不足を検出" || report ng "Session-Id 不足の Warning が無い"
printf '%s' "$_ctx2" | grep -q '検証証跡なし' \
  && report ok "検証証跡なしを検出" || report ng "検証証跡なしの Warning が無い"

echo "[ケース 3] CLAUDE_BASE_DISABLE_PR_BODY_CHECK=1 → 同じ本文でも新規 Warning 0 件"
CLAUDE_BASE_DISABLE_PR_BODY_CHECK=1 run_hook "$(mcp_json "$BAD_BODY")"
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "トグル有効時も exit 0" \
  || report ng "トグル有効時に exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
_ctx3=$(ctx_of "$HOOK_STDOUT")
_n3=$(count_new_warnings "$_ctx3")
[ "$_n3" -eq 0 ] \
  && report ok "トグル有効時は新規 Warning0件" \
  || report ng "トグル有効時なのに新規 Warning ${_n3} 件（additionalContext: ${_ctx3}）"

echo "[ケース 4] gh pr create --body \"...\"（Bash 経路）でも同様に検出される"
GH_COMMAND="gh pr create --title \"Test PR\" --body \"${BAD_BODY}\" --base main --head ${BRANCH}"
gh_json=$(jq -n --arg cmd "$GH_COMMAND" '{tool_name: "Bash", tool_input: {command: $cmd}}')
run_hook "$gh_json"
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "Bash 経路でも exit 0" \
  || report ng "Bash 経路なのに exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
_ctx4=$(ctx_of "$HOOK_STDOUT")
_n4=$(count_new_warnings "$_ctx4")
[ "$_n4" -eq 2 ] \
  && report ok "Bash 経路でも新規 Warning が2件" \
  || report ng "Bash 経路の新規 Warning が ${_n4} 件（期待 2・additionalContext: ${_ctx4}）"
teardown_repo

echo "[ケース 5] 高リスク差分（.claude/settings.json 相当パス）+ エッジケース記載なし"
setup_repo ".claude/settings.json" '{}'
run_hook "$(mcp_json "$GOOD_BODY")"
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "高リスク差分でも exit 0（ブロックしない）" \
  || report ng "高リスク差分なのに exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
_ctx5=$(ctx_of "$HOOK_STDOUT")
printf '%s' "$_ctx5" | grep -q '高リスク差分（hooks / settings / permissions 等）なのにエッジケース表が無い' \
  && report ok "高リスク差分のエッジケース不足を検出" \
  || report ng "高リスク差分なのにエッジケース Warning が無い（additionalContext: ${_ctx5}）"
_n5=$(count_new_warnings "$_ctx5")
[ "$_n5" -eq 1 ] \
  && report ok "新規 Warning はエッジケース欠落の1件のみ（has_code=false で他は出ない）" \
  || report ng "新規 Warning が ${_n5} 件（期待 1・additionalContext: ${_ctx5}）"
# high_risk 単独（has_code=false）でも PR 前レビュー記録の欠落は警告される（対策 D の安全網）
run_hook "$(mcp_json "$BAD_BODY")"
_ctx5b=$(ctx_of "$HOOK_STDOUT")
_n5b=$(count_new_warnings "$_ctx5b")
[ "$_n5b" -eq 4 ] \
  && report ok "high_risk 単独 + 記録なし本文で 4 件（Session-Id / 証跡 / PR 前レビュー / エッジケース）" \
  || report ng "high_risk 単独の新規 Warning が ${_n5b} 件（期待 4・additionalContext: ${_ctx5b}）"
teardown_repo

echo "[ケース 6] gh pr create --body-file / -F（ファイル経由）でも同様に検出される"
setup_repo
printf '%s' "$BAD_BODY" > "$WORK/body.md"
for _opt in "--body-file" "-F"; do
  GH_COMMAND="gh pr create --title \"Test PR\" ${_opt} ${WORK}/body.md --base main --head ${BRANCH}"
  gh_json=$(jq -n --arg cmd "$GH_COMMAND" '{tool_name: "Bash", tool_input: {command: $cmd}}')
  run_hook "$gh_json"
  _ctx6=$(ctx_of "$HOOK_STDOUT")
  _n6=$(count_new_warnings "$_ctx6")
  [ "$_n6" -eq 2 ] \
    && report ok "${_opt} 経由でも新規 Warning が2件" \
    || report ng "${_opt} 経由の新規 Warning が ${_n6} 件（期待 2・additionalContext: ${_ctx6}）"
done

echo "[ケース 7] テンプレート形式（箇条書き + インラインコード）の証跡も認識される（偽陽性なし）"
BULLET_BODY='Session-Id: sess-good-0002

## テスト・確認内容

- [ ] `python3 tools/x.py --self-test` → PASS
- bash tools/test_x.sh → PASS=3 FAIL=0

PR 前レビュー: スキップ（データのみ差分）'
run_hook "$(mcp_json "$BULLET_BODY")"
_ctx7=$(ctx_of "$HOOK_STDOUT")
_n7=$(count_new_warnings "$_ctx7")
[ "$_n7" -eq 0 ] \
  && report ok "箇条書き形式の証跡で新規 Warning 0 件" \
  || report ng "箇条書き形式の証跡なのに新規 Warning ${_n7} 件（additionalContext: ${_ctx7}）"
# 予約語で始まるだけの説明文（実行結果を伴わない）は証跡と認めない
PROSE_BODY='Session-Id: sess-good-0003

## テスト・確認内容

- npm run build was not executed due to time
- sh testing needed but not done yet

PR 前レビュー: スキップ（データのみ差分）'
run_hook "$(mcp_json "$PROSE_BODY")"
_ctx7b=$(ctx_of "$HOOK_STDOUT")
printf '%s' "$_ctx7b" | grep -q '検証証跡なし' \
  && report ok "予約語で始まる説明文だけでは「検証証跡なし」になる" \
  || report ng "予約語で始まる説明文を証跡と誤認した（additionalContext: ${_ctx7b}）"
teardown_repo

echo "[ケース 8] gh pr create --body \"\$(cat <<'EOF' ... EOF)\"（heredoc・本文に奇数個の \"）でも本文が取り出される"
setup_repo
HEREDOC_CMD="gh pr create --title \"Test PR\" --body \"\$(cat <<'EOF'
Renamed the \"helper function
EOF
)\" --base main --head ${BRANCH}"
gh_json=$(jq -n --arg cmd "$HEREDOC_CMD" '{tool_name: "Bash", tool_input: {command: $cmd}}')
run_hook "$gh_json"
_ctx9=$(ctx_of "$HOOK_STDOUT")
_n9=$(count_new_warnings "$_ctx9")
[ "$_n9" -eq 2 ] \
  && report ok "heredoc 本文（奇数個の \"）でも Session-Id / 証跡の欠落 2 件を検出" \
  || report ng "heredoc 本文の新規 Warning が ${_n9} 件（期待 2・additionalContext: ${_ctx9}）"
HEREDOC_GOOD_CMD="gh pr create --title \"Test PR\" --body \"\$(cat <<'EOF'
${GOOD_BODY}
Note: renamed the \"helper function
EOF
)\" --base main --head ${BRANCH}"
gh_json=$(jq -n --arg cmd "$HEREDOC_GOOD_CMD" '{tool_name: "Bash", tool_input: {command: $cmd}}')
run_hook "$gh_json"
_ctx9b=$(ctx_of "$HOOK_STDOUT")
_n9b=$(count_new_warnings "$_ctx9b")
[ "$_n9b" -eq 0 ] \
  && report ok "heredoc の要素完備本文（奇数個の \" 含む）は新規 Warning 0 件" \
  || report ng "heredoc の要素完備本文なのに新規 Warning ${_n9b} 件（additionalContext: ${_ctx9b}）"
teardown_repo

echo "[ケース 9] gh pr create --body-file の相対パスは呼び出し元 cwd（hook 入力 .cwd）基準で解決される"
setup_repo "body.md" "$BAD_BODY"
mkdir -p "$WORK/sub"
printf '%s' "$GOOD_BODY" > "$WORK/sub/body.md"
gh_json=$(jq -n --arg cmd "gh pr create --title \"Test PR\" --body-file body.md --base main --head ${BRANCH}" --arg cwd "$WORK/sub" \
  '{tool_name: "Bash", cwd: $cwd, tool_input: {command: $cmd}}')
run_hook "$gh_json"
_ctx8=$(ctx_of "$HOOK_STDOUT")
_n8=$(count_new_warnings "$_ctx8")
[ "$_n8" -eq 0 ] \
  && report ok "cwd 基準で sub/body.md（要素完備）が読まれ、リポジトリ直下の別 body.md は読まれない" \
  || report ng "相対 --body-file の解決が cwd 基準でない（新規 Warning ${_n8} 件・additionalContext: ${_ctx8}）"
teardown_repo


echo "[ケース 10] 未記入テンプレート（Session-Id: {UUID}・PR 前レビュー: 検出 N 件・空のエッジケース表）は記載ありと誤判定しない"
setup_repo ".claude/settings.json" '{}'
TEMPLATE_BODY='Session-Id: {UUID}

## テスト・確認内容

- `python3 tools/x.py --self-test` → PASS

## エッジケース表（high_risk 差分のみ・#627）

| 入力 | 状態 | 期待挙動 | 検証 |
|------|------|---------|------|
| {入力} | {状態} | {期待挙動} | {検証} |

PR 前レビュー: 検出 N 件（🔴a 🟡b ⚪c・CONFIRMED d / PLAUSIBLE e）→ 修正 f 件・見送り g 件'
run_hook "$(mcp_json "$TEMPLATE_BODY")"
_ctx10=$(ctx_of "$HOOK_STDOUT")
_n10=$(count_new_warnings "$_ctx10")
[ "$_n10" -eq 3 ] \
  && report ok "未記入テンプレートで 3 件（Session-Id / PR 前レビュー / エッジケース。証跡は記載あり）" \
  || report ng "未記入テンプレートの新規 Warning が ${_n10} 件（期待 3・additionalContext: ${_ctx10}）"
# 同じ差分で値・記録・データ行を埋めた本文 → 0 件（締め付けが正しい記載を誤検出しない）
FILLED_BODY='Session-Id: f7f07f85-ba7f-5236-81b1-c3c47b1ed177

## テスト・確認内容

- `python3 tools/x.py --self-test` → PASS

## エッジケース表（high_risk 差分のみ・#627）

| 入力 | 状態 | 期待挙動 | 検証 |
|------|------|---------|------|
| 空 JSON `{}` | 初回 | 既定値で続行 | 実行済み |

PR 前レビュー: 検出 2 件（🔴0 🟡1 ⚪1・CONFIRMED 1 / PLAUSIBLE 1）→ 修正 1 件・見送り 1 件'
run_hook "$(mcp_json "$FILLED_BODY")"
_ctx10b=$(ctx_of "$HOOK_STDOUT")
_n10b=$(count_new_warnings "$_ctx10b")
[ "$_n10b" -eq 0 ] \
  && report ok "値・記録・データ行を埋めた本文は 0 件（データ行に {} を含む実値も数える）" \
  || report ng "記載済み本文で新規 Warning ${_n10b} 件（期待 0・additionalContext: ${_ctx10b}）"
# スキップ記録（データのみ差分の書式）も記録ありと認める
SKIP_BODY=$(printf '%s' "$FILLED_BODY" | sed 's/^PR 前レビュー: .*/PR 前レビュー: スキップ（データのみ差分）/')
run_hook "$(mcp_json "$SKIP_BODY")"
_n10c=$(count_new_warnings "$(ctx_of "$HOOK_STDOUT")")
[ "$_n10c" -eq 0 ] \
  && report ok "PR 前レビュー: スキップ（理由）の書式も記録ありと認める" \
  || report ng "スキップ書式で新規 Warning ${_n10c} 件（期待 0）"
# 「実施」の裸一致では未完了・否定の言い回し（実施予定 / 実施しない方針）を弾けない（Layer 1 再レビュー指摘）
PENDING_BODY=$(printf '%s' "$FILLED_BODY" | sed 's/^PR 前レビュー: .*/PR 前レビュー: 実施予定/')
run_hook "$(mcp_json "$PENDING_BODY")"
_ctx10d=$(ctx_of "$HOOK_STDOUT")
printf '%s' "$_ctx10d" | grep -q 'PR 前フレッシュ文脈レビューの記録が無い' \
  && report ok "PR 前レビュー: 実施予定（未実施の言い回し）は記録ありと認めない" \
  || report ng "実施予定が記録ありと誤判定された（additionalContext: ${_ctx10d}）"
# 地の文の「エッジケース」+ 無関係な表は、見出し配下のエッジケース表と見なさない（Layer 1 再レビュー指摘）
PROSE_TABLE_BODY='Session-Id: f7f07f85-ba7f-5236-81b1-c3c47b1ed177

## テスト・確認内容

- `python3 tools/x.py --self-test` → PASS
- エッジケースを考慮してテストした

| ファイル | 変更 |
|------|------|
| tools/x.py | 追加 |

PR 前レビュー: 検出 0 件（🔴0 🟡0 ⚪0・CONFIRMED 0 / PLAUSIBLE 0）→ 修正 0 件・見送り 0 件'
run_hook "$(mcp_json "$PROSE_TABLE_BODY")"
_ctx10e=$(ctx_of "$HOOK_STDOUT")
printf '%s' "$_ctx10e" | grep -q '高リスク差分（hooks / settings / permissions 等）なのにエッジケース表が無い' \
  && report ok "地の文の「エッジケース」+ 無関係な表はエッジケース表と見なさない" \
  || report ng "無関係な表がエッジケース表として数えられた（additionalContext: ${_ctx10e}）"
teardown_repo

echo "[ケース 11] gh pr create --body-file が FIFO を指しても open でハングせず即座に通過する"
setup_repo
mkdir -p "$WORK/sub"
mkfifo "$WORK/sub/body.md"
gh_json=$(jq -n --arg cmd "gh pr create --title \"Test PR\" --body-file body.md --base main --head ${BRANCH}" --arg cwd "$WORK/sub" \
  '{tool_name: "Bash", cwd: $cwd, tool_input: {command: $cmd}}')
_t0=$SECONDS
run_hook "$gh_json"
_elapsed=$((SECONDS - _t0))
[ "$HOOK_EXIT" -eq 0 ] \
  && report ok "FIFO の --body-file でも exit 0" \
  || report ng "FIFO の --body-file で exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
[ "$_elapsed" -lt 8 ] \
  && report ok "FIFO を読みに行かず ${_elapsed} 秒で完了（timeout 10 秒の発動待ちにもならない）" \
  || report ng "FIFO の --body-file で ${_elapsed} 秒かかった（open でブロック、または timeout 待ち）"
_n11=$(count_new_warnings "$(ctx_of "$HOOK_STDOUT")")
[ "$_n11" -eq 0 ] \
  && report ok "本文を取得できない場合は Warning を出さない（抽出失敗と未記載を区別しない方針のまま）" \
  || report ng "FIFO なのに新規 Warning ${_n11} 件"
teardown_repo

echo "[ケース 12] 本文抽出の外側 timeout が発火（exit=124）してもフックは異常終了せず、リマインダーを出力する"
REAL_TIMEOUT=$(command -v timeout || true)
if [ -n "$REAL_TIMEOUT" ]; then
  setup_repo "body.md" "$GOOD_BODY"
  mkdir -p "$WORK/bin"
  # フェイク timeout: 本文抽出の呼び出し（引数 "10 python3 -"）だけ exit=124 を返し、他の呼び出しは本物へ委譲する
  cat > "$WORK/bin/timeout" <<EOS
#!/usr/bin/env bash
if [ "\$1" = "10" ] && [ "\$2" = "python3" ] && [ "\$3" = "-" ]; then cat >/dev/null; exit 124; fi
exec "$REAL_TIMEOUT" "\$@"
EOS
  chmod +x "$WORK/bin/timeout"
  gh_json=$(jq -n --arg cmd "gh pr create --title \"Test PR\" --body-file body.md --base main --head ${BRANCH}" --arg cwd "$WORK/repo" \
    '{tool_name: "Bash", cwd: $cwd, tool_input: {command: $cmd}}')
  PATH="$WORK/bin:$PATH" run_hook "$gh_json"
  [ "$HOOK_EXIT" -eq 0 ] \
    && report ok "抽出が exit=124 でもフックは exit 0（set -e で即死しない）" \
    || report ng "抽出の timeout 発火でフックが exit ${HOOK_EXIT}（stderr: ${HOOK_STDERR}）"
  _ctx12=$(ctx_of "$HOOK_STDOUT")
  printf '%s' "$_ctx12" | grep -q 'PR 作成後に Layer 1 セルフレビュー（FAIR・全PR必須）を必ず実行してください' \
    && report ok "Layer 1 リマインダーの additionalContext が失われない" \
    || report ng "timeout 発火時に Layer 1 リマインダーが出力されない（stdout: ${HOOK_STDOUT}）"
  _n12=$(count_new_warnings "$_ctx12")
  [ "$_n12" -eq 0 ] \
    && report ok "抽出失敗は本文なし扱い（本文チェックの Warning は出さない）" \
    || report ng "抽出失敗なのに新規 Warning ${_n12} 件（additionalContext: ${_ctx12}）"
  teardown_repo
else
  report ok "timeout 未導入のためスキップ（導入済み環境で検証される）"
fi

echo "[ケース 13] PR 本文を渡す一時ファイル（claude-prbody.*）がフック終了後に残留しない（Issue #628 Layer 1 テスト・検証指摘）"
setup_repo
_tmp_before=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'claude-prbody.*' 2>/dev/null | sort)
run_hook "$(mcp_json "$GOOD_BODY")"
_tmp_after=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'claude-prbody.*' 2>/dev/null | sort)
[ "$_tmp_before" = "$_tmp_after" ] \
  && report ok "trap による一時ファイル削除が機能し、残留ファイルが増えない" \
  || report ng "一時ファイルが残留した（前: [${_tmp_before}] 後: [${_tmp_after}]）"
teardown_repo

echo
echo "結果: PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
