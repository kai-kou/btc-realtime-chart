#!/usr/bin/env bash
# tools/test_self_review_check.sh — self_review_check.py の Layer 0 強化チェック
# （bash 構文検査・Python 構文検査・対応テスト自動実行）の回帰テスト（Issue #627 対策 C）
#
# 検証する不変条件:
#   1. 構文エラーのある .sh・構文エラーのある .py・失敗する対応テスト（tools/test_<name>.sh）が
#      同一差分にあると exit=1 になり、3 種の Error 行がすべて出力される
#   2. 正常なファイルだけの差分（テストの裏付けを備えた新規 tools/*.py を含む）では exit=0 になる
#   3. 対応テスト失敗は既定で Error（exit=1・ブロック）になる
#   4. SELF_REVIEW_SELFTEST=warn を付けると同じ対応テスト失敗が Warning に降格し exit=0 になる
#   5. ハイフン区切りのフック名（.claude/hooks/pre-x-y.sh）からアンダースコア名の対応テスト
#      （tools/test_pre_x_y.sh）が照合・実行される（対応テストは base 側に既存として置き、
#      「テストスクリプト自身の変更」経路で偶然通らない否定テストにする）
#   6. shellcheck（PATH 上のスタブ）/ ruff（導入済み環境のみ）の出力が Warning 化され exit=0 のまま
#   7. --self-test / 対応テストの実行時間予算の起点は GATE_STARTED（プロセス起動時）で、起点が予算超過分
#      だけ過去なら対応テストを 1 本も起動せず「未実行（予算超過）」を即座に返す（lint 段の経過を含めて数える・
#      起点を self_test_errors() 内に戻す退行を検出する否定テスト・#627 Layer 1 再レビュー指摘）
#   8. 実装規律検査（check_implementation_discipline.py・Issue #84）の配線が生きており、テストの裏付けが
#      無い tools/*.py について「新規追加は Error（exit=1）／既存ファイルの変更は Warning（exit=0）」に
#      重大度が割れる（D-36 の段階適用。既存を Error にすると遡及リファクタの強制になるため否定テストで固定する）。
#      あわせて SELF_REVIEW_DISCIPLINE=warn で Error が exit=0 へ降格し、かつ違反自体は報告され続ける
#      （沈黙しない）ことを固定する
#   11. PR 本文チェック（Issue #628）: 本文未確定時は無条件スプリントメタ・リマインドへフォールバックし、
#      本文が渡されれば Session-Id / 検証証跡の欠落検出へ切り替わる（二重に出ない）
#
# 使い方: bash tools/test_self_review_check.sh
# 終了コード: 0 = 全 PASS / 1 = 1 件以上 FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF_REVIEW="$REPO_ROOT/tools/self_review_check.py"
[ -f "$SELF_REVIEW" ] || { echo "FATAL: チェッカーが見つかりません: $SELF_REVIEW"; exit 1; }
# shellcheck source=tools/lib/test_harness.sh
source "$REPO_ROOT/tools/lib/test_harness.sh"

# テスト用リポジトリ（作業ブランチ + push 先の bare リモート）を作る。origin はローカル
# bare のパスのまま使う（ネットワークに触れない）。changed_files() の主経路
# `git diff --name-only origin/<default>...HEAD` を実際に解決させるため、main を一度
# origin へ push して origin/main を作ってから各シナリオ用の feature branch を main から
# 切る（default_branch() は refs/remotes/origin/HEAD 未設定時 "main" 固定にフォールバック
# するため、origin/HEAD の設定は不要）。
setup_repo() {
  WORK=$(mktemp -d)
  init_tmp_git_repo "$WORK"
  echo "base" > "$WORK/repo/base.txt"
  git -C "$WORK/repo" add -A
  git -C "$WORK/repo" commit --quiet -m base
  # ローカル bare へのローカル push でも環境によっては push negotiation の警告が
  # stderr に出ることがある（結果には影響しないが出力が汚れるため抑制する）。
  # origin/main が実際に解決できることは直後に明示検証し、フェイルオープンにしない。
  git -C "$WORK/repo" push --quiet -u origin main 2>/dev/null || true
  git -C "$WORK/repo" rev-parse --verify origin/main >/dev/null 2>&1 \
    || { echo "FATAL: setup_repo で origin/main を解決できませんでした"; exit 1; }
}

teardown_repo() { teardown_tmp_repo "$WORK"; }

# write_failing_companion_fixture: tools/foo.py と常に失敗する tools/test_foo.sh を置く（ケース 1 / 3 共用）
write_failing_companion_fixture() {
  mkdir -p "$WORK/repo/tools"
  # ヒアドキュメントの本文と終端は関数内でもインデントしない（終端 EOS の一致に必要）
cat > "$WORK/repo/tools/foo.py" <<'EOS'
"""Dummy fixture module for self_review_check.py companion-test coverage."""


def add(a, b):
    return a + b
EOS
cat > "$WORK/repo/tools/test_foo.sh" <<'EOS'
#!/usr/bin/env bash
# tools/test_self_review_check.sh のフィクスチャ専用ダミーテスト（常に失敗する）。
echo "simulated failure for self_review_check companion-test coverage"
exit 1
EOS
}

# run_review <branch> [ENV_NAME=VALUE ...] → REVIEW_OUT / REVIEW_EXIT に結果を残す。
# SELF_REVIEW は現在リポジトリ（本テストの対象）の絶対パスなので、テスト対象は常に
# 「いま編集中の self_review_check.py」であり、フィクスチャリポジトリ側にはコピーしない。
run_review() {
  local branch="$1"; shift
  git -C "$WORK/repo" checkout --quiet "$branch"
  REVIEW_OUT=$(cd "$WORK/repo" && env "$@" python3 "$SELF_REVIEW" 2>&1)
  REVIEW_EXIT=$?
}

setup_repo

echo "[ケース 1] 構文エラー .sh + 構文エラー .py + 失敗する対応テストが同一差分 → exit=1・Error 3 種"
git -C "$WORK/repo" checkout --quiet -b feat/bad main
mkdir -p "$WORK/repo/tools"
cat > "$WORK/repo/bad.sh" <<'EOS'
#!/usr/bin/env bash
if [ true ]; then
  echo "oops"
EOS
cat > "$WORK/repo/bad.py" <<'EOS'
def broken(:
    pass
EOS
write_failing_companion_fixture
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add bad fixtures"

run_review feat/bad
[ "$REVIEW_EXIT" -eq 1 ] \
  && report ok "構文エラー + 対応テスト失敗が揃うと exit=1" \
  || report ng "exit=${REVIEW_EXIT}（期待 1）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "構文エラー (bash -n): bad.sh" \
  && report ok "bash 構文エラーが Error 行として出力される" \
  || report ng "bash 構文エラーの Error 行が無い（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "構文エラー (python): bad.py:1" \
  && report ok "Python 構文エラーが Error 行として出力される（行番号込み）" \
  || report ng "Python 構文エラーの Error 行が無い（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "対応テスト失敗: tools/test_foo.sh（exit=1）" \
  && report ok "対応テスト失敗が Error 行として出力される" \
  || report ng "対応テスト失敗の Error 行が無い（出力: ${REVIEW_OUT}）"

echo "[ケース 2] 正常なファイルだけの差分 → exit=0"
git -C "$WORK/repo" checkout --quiet -b feat/good main
mkdir -p "$WORK/repo/tools"
cat > "$WORK/repo/tools/good.py" <<'EOS'
"""Dummy fixture module (valid syntax, companion test present)."""


def multiply(a, b):
    return a * b
EOS
# 実装規律検査（Issue #84）は新規追加の tools/*.py にテストの裏付けを要求する。
# 本ケースの主張は「正常なファイルだけの差分は exit=0」なので、裏付けを備えた
# 状態を「正常」として組む（規律を回避するのではなく満たす）。
cat > "$WORK/repo/tools/test_good.sh" <<'EOS'
#!/usr/bin/env bash
# tools/good.py のフィクスチャ用ダミー対応テスト（常に成功する）。
exit 0
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add good fixture"

run_review feat/good
[ "$REVIEW_EXIT" -eq 0 ] \
  && report ok "正常なファイルだけの差分は exit=0" \
  || report ng "exit=${REVIEW_EXIT}（期待 0）（出力: ${REVIEW_OUT}）"

echo "[ケース 3] 対応テスト失敗は既定で Error（exit=1・ブロック）"
git -C "$WORK/repo" checkout --quiet -b feat/companion-only main
mkdir -p "$WORK/repo/tools"
write_failing_companion_fixture
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add companion-only fixture"

run_review feat/companion-only
[ "$REVIEW_EXIT" -eq 1 ] \
  && report ok "既定（SELF_REVIEW_SELFTEST 未設定）では対応テスト失敗が exit=1" \
  || report ng "exit=${REVIEW_EXIT}（期待 1）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "\[self-review\] Error" \
  && report ok "既定では Error セクションに出力される" \
  || report ng "Error セクションが無い（出力: ${REVIEW_OUT}）"

echo "[ケース 4] SELF_REVIEW_SELFTEST=warn で Warning に降格 → exit=0"
run_review feat/companion-only SELF_REVIEW_SELFTEST=warn
[ "$REVIEW_EXIT" -eq 0 ] \
  && report ok "SELF_REVIEW_SELFTEST=warn では対応テスト失敗があっても exit=0" \
  || report ng "exit=${REVIEW_EXIT}（期待 0）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "対応テスト失敗: tools/test_foo.sh（exit=1）" \
  && report ok "対応テスト失敗の内容自体は Warning として出力される" \
  || report ng "対応テスト失敗の Warning 行が無い（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "\[self-review\] Error" \
  && report ng "SELF_REVIEW_SELFTEST=warn なのに Error セクションが残っている（出力: ${REVIEW_OUT}）" \
  || report ok "SELF_REVIEW_SELFTEST=warn では Error セクションが出ない"

echo "[ケース 5] ハイフン区切りのフック名（.claude/hooks/pre-x-y.sh）→ アンダースコア名の対応テスト（tools/test_pre_x_y.sh）が実行される"
# 対応テストは base（main・origin）側に既存として置き、差分にはフックだけが現れるようにする
# （「テストスクリプト自身の変更」経路で偶然通らないための否定テスト）
git -C "$WORK/repo" checkout --quiet main
mkdir -p "$WORK/repo/tools"
cat > "$WORK/repo/tools/test_pre_x_y.sh" <<'EOS'
#!/usr/bin/env bash
# ハイフン → アンダースコア照合のフィクスチャ専用ダミーテスト（常に失敗する）。
echo "simulated failure for hyphen-to-underscore companion lookup"
exit 1
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add companion test on base"
git -C "$WORK/repo" push --quiet origin main 2>/dev/null || true
git -C "$WORK/repo" checkout --quiet -b feat/hyphen-hook main
mkdir -p "$WORK/repo/.claude/hooks"
cat > "$WORK/repo/.claude/hooks/pre-x-y.sh" <<'EOS'
#!/usr/bin/env bash
echo "dummy hook fixture"
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add hyphenated hook fixture"
git -C "$WORK/repo" diff --name-only origin/main...HEAD | grep -q '^tools/test_pre_x_y.sh$' \
  && report ng "フィクスチャ不備: 対応テストが差分に含まれている（否定テストとして無効）" \
  || report ok "差分にはフックだけが現れる（対応テストは base 側に既存）"

run_review feat/hyphen-hook
printf '%s' "$REVIEW_OUT" | grep -q "対応テスト失敗: tools/test_pre_x_y.sh（exit=1）" \
  && report ok "ハイフン区切りのフック名からアンダースコア名の対応テストが実行される" \
  || report ng "対応テスト（tools/test_pre_x_y.sh）が実行されていない（出力: ${REVIEW_OUT}）"

echo "[ケース 6] shellcheck / ruff の Warning 経路 → スタブ（shellcheck）と実ツール（ruff・導入済み環境のみ）の出力が Warning 化される"
git -C "$WORK/repo" checkout --quiet -b feat/lint main
cat > "$WORK/repo/lint_target.sh" <<'EOS'
#!/usr/bin/env bash
unused_var="x"
echo "ok"
EOS
cat > "$WORK/repo/lint_target.py" <<'EOS'
"""Fixture with an undefined name (ruff F821)."""


def run():
    return undefined_name
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add lint fixtures"
mkdir -p "$WORK/bin"
cat > "$WORK/bin/shellcheck" <<'EOS'
#!/usr/bin/env bash
# shellcheck のスタブ（gcc 形式の出力を 1 行返す）。self_review_check.py のパース経路の検証用
echo "lint_target.sh:2:1: warning: unused_var appears unused. [SC2034]"
EOS
chmod +x "$WORK/bin/shellcheck"
run_review feat/lint PATH="$WORK/bin:$PATH"
[ "$REVIEW_EXIT" -eq 0 ] \
  && report ok "lint 系は Warning のみで exit=0" \
  || report ng "exit=${REVIEW_EXIT}（期待 0）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "shellcheck: lint_target.sh:2:1: warning" \
  && report ok "shellcheck の出力が Warning 化される（スタブ経由）" \
  || report ng "shellcheck Warning が無い（出力: ${REVIEW_OUT}）"
if command -v ruff >/dev/null 2>&1; then
  printf '%s' "$REVIEW_OUT" | grep -q "ruff: .*lint_target.py.*F821" \
    && report ok "ruff の未定義名（F821）が Warning 化される" \
    || report ng "ruff Warning が無い（出力: ${REVIEW_OUT}）"
else
  report ok "ruff 未導入のためスキップ（導入済み環境で検証される）"
fi

teardown_repo

echo "[ケース 7] 予算の起点は GATE_STARTED（プロセス起動時）を共有する（起点が過去なら対応テストを起動せず未実行 Error）"
_gate_out=$(cd "$REPO_ROOT" && python3 - <<'PY'
import sys, time
sys.path.insert(0, "tools")
import self_review_check as m
# 起点を予算超過分だけ過去へずらす → 対応テスト（tools/test_self_review_check.sh）は起動されず
# 「未実行（予算超過）」になる。起点を self_test_errors() 内で取り直す実装に退行すると、ここで
# 対応テストが実際に走ってしまい（本スクリプトの再帰実行・数秒）、ERRS の件数が 0 になる
m.GATE_STARTED = time.monotonic() - (m.SELF_TEST_BUDGET_SECONDS + 5)
t0 = time.monotonic()
errs = m.self_test_errors(["tools/self_review_check.py"])
elapsed = time.monotonic() - t0
print("ERRS", len(errs), all(("未実行" in e and "予算" in e) for e in errs), f"{elapsed:.1f}")
# 起点を現在に戻すと予算内なので、軽いコマンドは実際に実行されて成功する（判定が起点を読んでいる裏付け）
m.GATE_STARTED = time.monotonic()
errs2 = []
m._run_within_budget(["true"], "対応テスト", "true", m.GATE_STARTED, errs2, "true")
print("FRESH", len(errs2))
PY
)
printf '%s\n' "$_gate_out" | grep -qE '^ERRS [1-9][0-9]* True ' \
  && report ok "起点が予算超過分だけ過去なら対応テストを起動せず「未実行（予算超過）」を返す" \
  || report ng "予算超過の起点で未実行 Error にならない（出力: ${_gate_out}）"
printf '%s\n' "$_gate_out" | grep -qE '^ERRS [0-9]+ True [01]\.[0-9]$' \
  && report ok "予算超過時は対応テストを実際には走らせない（2 秒未満で判定）" \
  || report ng "予算超過時に対応テストが実行された疑い（出力: ${_gate_out}）"
printf '%s\n' "$_gate_out" | grep -q '^FRESH 0$' \
  && report ok "起点が現在なら予算内としてコマンドを実行する（起点を読んでいる裏付け）" \
  || report ng "起点を現在に戻しても実行されない（出力: ${_gate_out}）"

echo "[ケース 8] 実装規律検査の配線（Issue #84）→ 新規追加は Error（ブロック）・既存ファイルの変更は Warning"
# ケース 6 の末尾で teardown_repo 済みのため、作業リポジトリを作り直す
setup_repo
# 既存扱いの検証に使う裏付け無しファイルを base（main・origin）側へ置く。
# これが差分に現れると「新規追加」判定になってしまうため、必ず main へ push してから
# feature branch を切る（ケース 5 と同じ否定テストの組み方）。
git -C "$WORK/repo" checkout --quiet main
mkdir -p "$WORK/repo/tools"
cat > "$WORK/repo/tools/legacy_tool.py" <<'EOS'
"""Dummy fixture module on base (valid syntax, intentionally no test backing)."""


def add(a, b):
    return a + b
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add legacy fixture on base"
git -C "$WORK/repo" push --quiet origin main 2>/dev/null || true

# 8-a: 新規追加でテストの裏付けが無い → Error（exit=1）
git -C "$WORK/repo" checkout --quiet -b feat/discipline-new main
cat > "$WORK/repo/tools/brand_new_tool.py" <<'EOS'
"""Dummy fixture module (valid syntax, intentionally no test backing)."""


def divide(a, b):
    return a / b
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add new tool without test backing"

run_review feat/discipline-new
[ "$REVIEW_EXIT" -eq 1 ] \
  && report ok "新規追加でテストの裏付けが無いと exit=1（ブロック）" \
  || report ng "exit=${REVIEW_EXIT}（期待 1）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "実装規律違反: tools/brand_new_tool.py: \[TDD-1\]" \
  && report ok "新規追加の違反が Error セクションへ TDD-1 として出力される" \
  || report ng "実装規律違反の Error 行が無い（出力: ${REVIEW_OUT}）"

# 8-a': SELF_REVIEW_DISCIPLINE=warn で Error を非ブロック化できる（M-1 の KPI 直結タスク用の逃げ道）
run_review feat/discipline-new SELF_REVIEW_DISCIPLINE=warn
[ "$REVIEW_EXIT" -eq 0 ] \
  && report ok "SELF_REVIEW_DISCIPLINE=warn で同じ違反が exit=0 へ降格する" \
  || report ng "exit=${REVIEW_EXIT}（期待 0）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "実装規律違反: tools/brand_new_tool.py" \
  && report ok "降格しても違反自体は Warning として報告される（沈黙しない）" \
  || report ng "降格時に違反が報告されていない（出力: ${REVIEW_OUT}）"

# 8-b: 既存ファイルの変更で裏付けが無い → Warning のみ（exit=0・遡及リファクタを強制しない）
git -C "$WORK/repo" checkout --quiet -b feat/discipline-existing main
printf '\n\ndef sub(a, b):\n    return a - b\n' >> "$WORK/repo/tools/legacy_tool.py"
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "modify legacy tool without adding tests"

run_review feat/discipline-existing
[ "$REVIEW_EXIT" -eq 0 ] \
  && report ok "既存ファイルの変更は裏付けが無くても exit=0（Warning 止まり）" \
  || report ng "exit=${REVIEW_EXIT}（期待 0）（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q "実装規律（推奨）: tools/legacy_tool.py: \[TDD-1\]" \
  && report ok "既存ファイルの変更は Warning セクションへ出力される" \
  || report ng "実装規律の Warning 行が無い（出力: ${REVIEW_OUT}）"

# 9: scripts/*.sh の対応テストも自動起動される（Issue #59）
# check_implementation_discipline.py の TDD-3 は scripts/*.sh に tools/test_<name>.sh を要求するが、
# 対応テストの起動側（_companion_test_script_targets）が scripts/ を見ていないと、
# 要求されて書いたテストが 1 度も走らないまま腐る。この否定テストは、対象スコープから
# scripts/ を外すと「対応テスト失敗」の Error が消えて FAIL する。
echo "[ケース 9] scripts/*.sh の変更で tools/test_<name>.sh が自動起動される"
git -C "$WORK/repo" checkout --quiet -b feat/scripts-companion main
mkdir -p "$WORK/repo/scripts" "$WORK/repo/tools"
cat > "$WORK/repo/scripts/sample-script.sh" <<'EOS'
#!/usr/bin/env bash
# tools/test_self_review_check.sh のフィクスチャ専用ダミースクリプト（構文は正しい）。
echo "sample"
EOS
cat > "$WORK/repo/tools/test_sample_script.sh" <<'EOS'
#!/usr/bin/env bash
# scripts/sample-script.sh のフィクスチャ用ダミー対応テスト（常に失敗する）。
echo "simulated failure for scripts/*.sh companion-test coverage"
exit 1
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add scripts companion fixture"

run_review feat/scripts-companion
printf '%s' "$REVIEW_OUT" | grep -q "対応テスト失敗: tools/test_sample_script.sh（exit=1）" \
  && report ok "scripts/*.sh の変更でハイフン→アンダースコア綴りの対応テストが起動される" \
  || report ng "scripts/*.sh の対応テストが起動されていない（出力: ${REVIEW_OUT}）"
[ "$REVIEW_EXIT" -eq 1 ] \
  && report ok "起動した対応テストの失敗が exit=1 でブロックされる" \
  || report ng "exit=${REVIEW_EXIT}（期待 1）（出力: ${REVIEW_OUT}）"

# 10: AC-4（既定 1 本）の静的検査が PR 前ゲートに配線されている（Issue #112）
# チェッカ自体は #19 で存在したのに self_review_check.py から呼ばれておらず、PR #111 の
# S-1 違反（framework-templates への先行配置）が main まで素通りした。配線を外すと
# 「違反あり」の Error が消えて FAIL する否定テストとして固定する。チェッカは base 側に
# 置き（差分に出さない）、DEFAULT_DOC_COUNT_TRIGGERS の **全要素** について 1 本ずつ起動を
# 確かめる（1 要素だけの確認では、他の要素を落とす退行が PASS のまま通る・Layer 1 指摘）。
# 最後にトリガー外の差分で起動しないことを見て、起動判定が接頭辞で決まることを固定する。
echo "[ケース 10] 雛形ディレクトリの変更で AC-4 静的検査が起動し、違反が Error になる"
git -C "$WORK/repo" checkout --quiet main
mkdir -p "$WORK/repo/tools"
cat > "$WORK/repo/tools/check_default_doc_count.py" <<'EOS'
#!/usr/bin/env python3
"""Fixture stub: 常に AC-4 の S-1 違反を報告して exit 1 する（配線の有無だけを見る）。"""
import sys

print("FAIL: AC-4（既定 1 本）の静的検査に違反があります")
print("  - S-1: Tier 0 以外の雛形本体が先行配置されている（D-19 違反）: autonomy/routines.md")
sys.exit(1)
EOS
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add AC-4 checker stub to base"
git -C "$WORK/repo" push --quiet origin main 2>/dev/null || true

# DEFAULT_DOC_COUNT_TRIGGERS の各要素に対応する代表ファイル（順序は定数側と揃える）。
# 定数から 1 要素でも落とすと、対応するケースが「起動していない」で FAIL する。
TRIGGER_TARGETS=(
  "docs/framework-templates/product-brief.md"
  "docs/autonomy-templates/routines.md"
  "docs/02_requirements/document-catalog.md"
  ".claude/skills/upstream-flow/SKILL.md"
  "tools/check_default_doc_count.py"
)
trigger_index=0
for target in "${TRIGGER_TARGETS[@]}"; do
  trigger_index=$((trigger_index + 1))
  git -C "$WORK/repo" checkout --quiet -b "docs/trigger-${trigger_index}" main
  mkdir -p "$WORK/repo/$(dirname "$target")"
  # 追記にすることで、既存のチェッカスタブ（tools/check_default_doc_count.py）を壊さずに
  # 「そのファイルが差分に現れた」状態だけを作る。
  printf '# placeholder for AC-4 trigger coverage\n' >> "$WORK/repo/$target"
  git -C "$WORK/repo" add -A
  git -C "$WORK/repo" commit --quiet -m "touch $target"

  run_review "docs/trigger-${trigger_index}"
  printf '%s' "$REVIEW_OUT" | grep -q "AC-4（既定 1 本）の静的検査に違反があります" \
    && report ok "$target の変更で AC-4 静的検査が起動する" \
    || report ng "$target の変更で AC-4 静的検査が起動していない（出力: ${REVIEW_OUT}）"
  [ "$REVIEW_EXIT" -eq 1 ] \
    && report ok "$target: AC-4 違反が exit=1 でブロックされる" \
    || report ng "$target: exit=${REVIEW_EXIT}（期待 1）（出力: ${REVIEW_OUT}）"
done

git -C "$WORK/repo" checkout --quiet -b docs/unrelated-change main
printf 'unrelated\n' > "$WORK/repo/unrelated.txt"
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "touch unrelated file"

run_review docs/unrelated-change
printf '%s' "$REVIEW_OUT" | grep -q "AC-4（既定 1 本）の静的検査に違反があります" \
  && report ng "トリガー外の差分でも AC-4 静的検査が起動している（出力: ${REVIEW_OUT}）" \
  || report ok "トリガー外の差分では AC-4 静的検査を起動しない"

echo "[ケース 11] PR 本文チェック（Issue #628）: 本文未定時の無条件フォールバックと Session-Id/検証証跡チェックの集約"
setup_repo
git -C "$WORK/repo" checkout --quiet -b feat/pr-body-check main
echo "fixture" > "$WORK/repo/note.txt"
git -C "$WORK/repo" add -A
git -C "$WORK/repo" commit --quiet -m "add fixture for PR body check"

run_review feat/pr-body-check
printf '%s' "$REVIEW_OUT" | grep -q 'スプリントメタを PR 本文に記載してください' \
  && report ok "SELF_REVIEW_PR_BODY 未設定時は無条件スプリントメタ・リマインドへフォールバックする" \
  || report ng "フォールバック・リマインドが出ない（出力: ${REVIEW_OUT}）"

run_review feat/pr-body-check "SELF_REVIEW_PR_BODY=Fixes a bug."
printf '%s' "$REVIEW_OUT" | grep -q 'PR 本文に Session-Id: が無い' \
  && report ok "本文あり（Session-Id 欠落）を検出する" \
  || report ng "Session-Id 欠落の Warning が無い（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q '検証証跡なし' \
  && report ok "本文あり（検証証跡なし）を検出する" \
  || report ng "検証証跡なしの Warning が無い（出力: ${REVIEW_OUT}）"
printf '%s' "$REVIEW_OUT" | grep -q 'スプリントメタを PR 本文に記載してください' \
  && report ng "本文ありなのに無条件フォールバックまで二重に出てしまう（出力: ${REVIEW_OUT}）" \
  || report ok "本文ありでは無条件フォールバックに置き換わり二重に出ない"

PR_BODY_GOOD='Session-Id: sess-good-0001

## テスト・確認内容

```
python3 tools/x.py
```

PR 前レビュー: 検出 0 件'
run_review feat/pr-body-check "SELF_REVIEW_PR_BODY=${PR_BODY_GOOD}"
printf '%s' "$REVIEW_OUT" | grep -q 'PR 本文に Session-Id: が無い' \
  && report ng "値を記載した Session-Id が欠落と誤判定された（出力: ${REVIEW_OUT}）" \
  || report ok "値を記載した Session-Id は欠落と判定されない"
printf '%s' "$REVIEW_OUT" | grep -q '検証証跡なし' \
  && report ng "コマンド＋結果を含む証跡が検出されなかった（出力: ${REVIEW_OUT}）" \
  || report ok "コマンド＋結果を含む証跡は検出なしと判定されない"

teardown_repo

echo
echo "結果: PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
