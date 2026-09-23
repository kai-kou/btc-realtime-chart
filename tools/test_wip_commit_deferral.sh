#!/usr/bin/env bash
# WIP 自動コミットの「差し戻し 1 巡猶予 + 無条件フォールバック」の振る舞いテスト（#483）
#
# 検証する不変条件:
#   1. 差し戻し中（stop-git-check.sh が exit 2）かつ猶予未使用 → 1 巡だけコミットを見送る
#      （Claude が意味のあるコミットを作る機会を確保する）
#   2. 2 巡目（猶予マーカー有り）→ 差し戻し中でも **無条件でコミット**する（L-100 のフェイルセーフ）
#   3. 差し戻しなし → 従来どおり即コミットする
#   4. session_id が取れずマーカーを作れない → 見送らず即コミットする（安全側フォールバック）
#   5. working tree が clean になったら猶予マーカーをリセットする
#   6. 見送った 1 巡でも refs/claude-wip/<session_id> にスナップショットが残る
#      （HEAD・index・working tree は変更しない）。クラッシュしても作業を失わないための保険
#   7. pre-pr-create-check.sh の件名ガードが、単一コミットの自動保全コミットだけをブロックする
#
# 使い方: bash tools/test_wip_commit_deferral.sh
# 終了コード: 0 = 全 PASS / 1 = 1 件以上 FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.claude/hooks/stop-slack-notify.sh"
PR_HOOK="$REPO_ROOT/.claude/hooks/pre-pr-create-check.sh"
[ -f "$HOOK" ] || { echo "FATAL: フックが見つかりません: $HOOK"; exit 1; }
[ -f "$PR_HOOK" ] || { echo "FATAL: フックが見つかりません: $PR_HOOK"; exit 1; }

PASS=0
FAIL=0

report() { # report <結果 ok|ng> <説明>
  if [ "$1" = "ok" ]; then
    PASS=$((PASS + 1)); echo "  PASS: $2"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL: $2"
  fi
}

# テスト用リポジトリを作る（作業ブランチ + push 先の bare リモート）
setup_repo() {
  WORK=$(mktemp -d)
  git init --quiet --initial-branch=main "$WORK/remote.git" --bare 2>/dev/null \
    || git init --quiet --bare "$WORK/remote.git"
  # 既定ブランチ名は git のバージョン・設定で master になりうるため明示する
  # （main でないと origin/main が解決できず、件名ガードの通常経路を検証できない）
  git init --quiet --initial-branch=main "$WORK/repo" 2>/dev/null || {
    git init --quiet "$WORK/repo"
    git -C "$WORK/repo" checkout --quiet -B main
  }
  git -C "$WORK/repo" config user.email test@example.com
  git -C "$WORK/repo" config user.name test
  git -C "$WORK/repo" remote add origin "$WORK/remote.git"
  echo "base" > "$WORK/repo/base.txt"
  git -C "$WORK/repo" add -A
  git -C "$WORK/repo" commit --quiet -m "base"
  git -C "$WORK/repo" checkout --quiet -b feat/test
  git -C "$WORK/repo" push --quiet -u origin feat/test 2>/dev/null || true
}

teardown_repo() { rm -rf "$WORK"; }

# フックを 1 回実行する: run_hook <git_check_blocked> <session_id>
run_hook() {
  local blocked="$1" session="$2"
  ( cd "$WORK/repo" \
    && CLAUDE_CODE_REMOTE=true \
       CLAUDE_STOP_GIT_CHECK_BLOCKED="$blocked" \
       bash "$HOOK" <<< "{\"session_id\":\"${session}\",\"stop_hook_active\":\"false\"}" \
       >/dev/null 2>&1 )
}

commit_count() { git -C "$WORK/repo" rev-list HEAD --count 2>/dev/null || echo 0; }
marker_count() { find "$WORK/repo/.git" -maxdepth 1 -name 'claude-wip-deferred-*' 2>/dev/null | wc -l | tr -d ' '; }

# ── ケース 1・2・5: 差し戻し中は 1 巡見送り、2 巡目は無条件でコミット ──
echo "[ケース 1/2/5] 差し戻し中の 1 巡猶予 → 2 巡目で無条件コミット"
setup_repo
echo "work" > "$WORK/repo/work.txt"
_before=$(commit_count)

_head_before=$(git -C "$WORK/repo" rev-parse HEAD)
run_hook 1 sess-A
[ "$(commit_count)" -eq "$_before" ] \
  && report ok "1 巡目: 差し戻し中はコミットしない（作業は消えずに残る）" \
  || report ng "1 巡目: 差し戻し中なのにコミットされた"
[ -f "$WORK/repo/work.txt" ] \
  && report ok "1 巡目: 未コミット変更はワークツリーに保持されている" \
  || report ng "1 巡目: 未コミット変更が失われた"
[ "$(marker_count)" -eq 1 ] \
  && report ok "1 巡目: 猶予マーカーが 1 件作られた" \
  || report ng "1 巡目: 猶予マーカーが作られていない（数=$(marker_count)）"

# --- 見送った巡のスナップショット（クラッシュしても作業を失わない保険・L-100）---
_snap=$(git -C "$WORK/repo" rev-parse --verify --quiet refs/claude-wip/sess-A || echo "")
[ -n "$_snap" ] \
  && report ok "1 巡目: スナップショット ref が作られた" \
  || report ng "1 巡目: スナップショット ref が無い（クラッシュ時に作業が消える）"
[ "$(git -C "$WORK/repo" rev-parse HEAD)" = "$_head_before" ] \
  && report ok "1 巡目: スナップショット作成が HEAD を進めていない" \
  || report ng "1 巡目: HEAD が動いた（副作用あり）"
git -C "$WORK/repo" diff --cached --quiet 2>/dev/null \
  && report ok "1 巡目: index を汚していない（Claude のステージ状態を壊さない）" \
  || report ng "1 巡目: index が変更された"
if [ -n "$_snap" ] && git -C "$WORK/repo" cat-file -e "${_snap}:work.txt" 2>/dev/null; then
  report ok "1 巡目: 未追跡ファイルもスナップショットに含まれている"
else
  report ng "1 巡目: 未追跡ファイルがスナップショットに含まれていない"
fi

run_hook 1 sess-A
[ "$(commit_count)" -eq $((_before + 1)) ] \
  && report ok "2 巡目: 差し戻しが続いていても無条件でコミットした（L-100 フェイルセーフ）" \
  || report ng "2 巡目: コミットされなかった（L-100 後退）"
[ "$(marker_count)" -eq 0 ] \
  && report ok "2 巡目: 猶予マーカーが消費（削除）された" \
  || report ng "2 巡目: 猶予マーカーが残っている"
git -C "$WORK/repo" rev-parse --verify --quiet refs/claude-wip/sess-A >/dev/null 2>&1 \
  && report ng "2 巡目: 不要になったスナップショット ref が残骸として残っている" \
  || report ok "2 巡目: スナップショット ref が片付けられた"

git -C "$WORK/repo" log -1 --pretty=%s | grep -q '^\[wip\]' \
  && report ok "自動保全コミットの件名は [wip] プレフィックスを維持している（PR ガードの検知対象）" \
  || report ng "件名が [wip] で始まっていない（pre-pr-create-check.sh のガードをすり抜ける）"

# clean になった後は猶予がリセットされる（ケース 5）
run_hook 1 sess-A
[ "$(marker_count)" -eq 0 ] \
  && report ok "clean 時: 猶予マーカーを作らない（不要なマーカーを残さない）" \
  || report ng "clean 時: 不要な猶予マーカーが作られた"
teardown_repo

# ── ケース 3: 差し戻しなしなら即コミット ──
echo "[ケース 3] 差し戻しなし → 従来どおり即コミット"
setup_repo
echo "work" > "$WORK/repo/work.txt"
_before=$(commit_count)
run_hook 0 sess-B
[ "$(commit_count)" -eq $((_before + 1)) ] \
  && report ok "差し戻しがなければ 1 巡目で即コミットする" \
  || report ng "差し戻しなしなのにコミットされなかった"
teardown_repo

# ── ケース 4: session_id が無い場合は見送らない（安全側） ──
echo "[ケース 4] session_id 取得不可 → 見送らず即コミット（安全側フォールバック）"
setup_repo
echo "work" > "$WORK/repo/work.txt"
_before=$(commit_count)
run_hook 1 ""
[ "$(commit_count)" -eq $((_before + 1)) ] \
  && report ok "マーカーを作れない環境では猶予せず即コミットする" \
  || report ng "マーカー不可なのに見送られた（保全されないリスク）"
teardown_repo

# ── ケース 7: pre-pr-create-check.sh の件名ガード ──
# squash マージが HEAD 件名を継承するのは **単一コミットのブランチ** だけなので、
# 過剰ブロック（複数コミットで巻き戻しを強要する）と検知漏れの両方を検証する。
echo "[ケース 7] 件名ガード: 単一コミットの自動保全コミットだけをブロックする"

# PR 作成をブロックしたか（exit 2）を返す: pr_guard_blocked → 0 = ブロックした / 1 = 通した
pr_guard_blocked() {
  ( cd "$WORK/repo" \
    && bash "$PR_HOOK" <<< '{"tool_name":"mcp__github__create_pull_request"}' >/dev/null 2>&1 )
  [ "$?" -eq 2 ]
}

setup_guard_repo() { # setup_guard_repo <件名...>: 引数の件数ぶんコミットを積む
  setup_repo
  # ガードが基準にする origin/main をローカルで解決できるようにする
  git -C "$WORK/repo" push --quiet origin main:main 2>/dev/null || true
  git -C "$WORK/repo" fetch --quiet origin "+main:refs/remotes/origin/main" 2>/dev/null || true
  local i=0
  for _subject in "$@"; do
    i=$((i + 1))
    echo "change $i" > "$WORK/repo/file${i}.txt"
    git -C "$WORK/repo" add -A
    git -C "$WORK/repo" commit --quiet -m "$_subject"
  done
  git -C "$WORK/repo" push --quiet -u origin feat/test 2>/dev/null || true
}

setup_guard_repo "[wip] 自動保全: 意味のあるコミット未作成のまま終了（2026-08-28 12:00）"
pr_guard_blocked \
  && report ok "単一コミットの [wip] 自動保全コミット → ブロックする" \
  || report ng "単一コミットの [wip] を通してしまった（main の履歴が汚れる）"
teardown_repo

setup_guard_repo "feat: 機能を追加する" "fix: 不具合を直す" "[wip] 自動保全: 意味のあるコミット未作成のまま終了（2026-08-28 12:00）"
pr_guard_blocked \
  && report ng "複数コミットで過剰ブロックした（squash は PR タイトルを使うので実害がない）" \
  || report ok "複数コミットで HEAD だけ [wip] → ブロックしない（過剰ブロックしない）"
teardown_repo

setup_guard_repo "fix: revert accidental auto-commit before compaction hack"
pr_guard_blocked \
  && report ng "正当な件名を部分一致で誤検知した（正規表現のアンカー漏れ）" \
  || report ok "定型文言を途中に含むだけの正当な件名 → ブロックしない"
teardown_repo

setup_guard_repo "improvement: 意味のあるコミットへ書き換え済み"
pr_guard_blocked \
  && report ng "意味のある件名をブロックした" \
  || report ok "意味のある件名 → ブロックしない"
teardown_repo

echo
echo "結果: PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
