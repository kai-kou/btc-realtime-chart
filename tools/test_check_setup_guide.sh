#!/bin/bash
# test_check_setup_guide.sh — tools/check_setup_guide.py の統合テスト（Issue #13・D-36 の TDD 規律）
#
# `--self-test` は純粋関数しか通らない。本テストは CLI を実際に叩き、
#   ① 実在する手順書（routines-setup.md・setup-guide のテンプレート）が規律を満たしていること
#   ② 規律違反を混ぜた写しで実際に exit 1 になること（検出力の実測）
#   ③ 判定不能を合格に読み替えないこと（exit 2）
# を assert する。
#
#   使い方: bash tools/test_check_setup_guide.sh
#   終了コード: 0 = 全 assert PASS / 1 = いずれか FAIL
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/tools/check_setup_guide.py"
ROUTINES_SETUP="$REPO_ROOT/docs/autonomy-templates/routines-setup.md"
SKILL_DIR="$REPO_ROOT/.claude/skills/setup-guide"

pass=0
fail=0

ok() { pass=$((pass + 1)); echo "  ✓ $1"; }
ng() { fail=$((fail + 1)); echo "  ✗ $1"; }
assert() { if [[ "$2" == "$3" ]]; then ok "$1"; else ng "$1（want=$2 got=$3）"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[test_check_setup_guide] 実データ"

python3 "$SCRIPT" --self-test >/dev/null 2>&1
assert "--self-test（純粋関数）が PASS" "0" "$?"

python3 "$SCRIPT" "$ROUTINES_SETUP" >/dev/null 2>&1
assert "routines-setup.md（#110 の案内）が規律を満たす" "0" "$?"

python3 "$SCRIPT" "$SKILL_DIR/guide-template.md" >/dev/null 2>&1
assert "setup-guide の手順書テンプレートが規律を満たす" "0" "$?"

# SKILL.md は手順書ではない（工程見出しを持たない）ので、入力値とクレデンシャルの規律だけを見る:
# 工程 0 件の NG 1 件以外が出ないこと（スキル配下に実在値・クレデンシャルが無い＝ #13 完了条件 3）。
skill_ng="$(python3 "$SCRIPT" "$SKILL_DIR/SKILL.md" 2>&1 | grep -c ': L' || true)"
assert "SKILL.md に架空値違反・クレデンシャル形状が無い（工程 0 件の 1 件だけ）" "1" "$skill_ng"

# SKILL.md の手順が @mention の経路を増やしていない（#13 完了条件 2 の機械的な裏付け）:
# slack_notify.py を呼ぶコマンド例をスキル配下に持たない（行内のどこにあっても検出する）。
# 行頭に限らず、リスト項目内のインラインコード形式（`- \`python3 tools/slack_notify.py ...\``）も拾う。
if grep -rqE 'python3[[:space:]]+[^[:space:]]*slack_notify\.py' "$SKILL_DIR"; then
  ng "setup-guide スキルが slack_notify.py の呼び出しを含む（@mention 経路を増やさない）"
else
  ok "setup-guide スキルが slack_notify.py を呼ばない（@mention 経路を増やさない）"
fi

echo "[test_check_setup_guide] 検出力（違反を混ぜた写し）"

sed 's/^\*\*完了条件\*\*: 初回 run.*$//' "$ROUTINES_SETUP" > "$WORK/no-completion.md"
python3 "$SCRIPT" "$WORK/no-completion.md" >/dev/null 2>&1
assert "完了条件を 1 つ消すと NG（exit 1）" "1" "$?"

sed 's#https://app\.example\.com/callback#https://app.fictional-vendor.zz/callback#' \
  "$SKILL_DIR/guide-template.md" > "$WORK/real-host.md"
python3 "$SCRIPT" "$WORK/real-host.md" >/dev/null 2>&1
assert "入力値のホストを実在風に差し替えると NG（exit 1）" "1" "$?"

{ cat "$SKILL_DIR/guide-template.md"; printf '\nghp_%s\n' "$(printf 'a%.0s' {1..36})"; } > "$WORK/leak.md"
python3 "$SCRIPT" "$WORK/leak.md" >/dev/null 2>&1
assert "クレデンシャル形状を混ぜると NG（exit 1）" "1" "$?"

echo "[test_check_setup_guide] 配布先（publish-snapshot.sh が無い環境）"

# publish-snapshot.sh は配布物へ配られない（PUBLISH_DENYLIST）。写しだけで検査が成立することを確かめる。
mkdir -p "$WORK/dist/tools"
cp "$SCRIPT" "$WORK/dist/tools/"
python3 "$WORK/dist/tools/check_setup_guide.py" "$ROUTINES_SETUP" >/dev/null 2>&1
assert "原本が無い配布先でも写しで検査できる（exit 0）" "0" "$?"
python3 "$WORK/dist/tools/check_setup_guide.py" "$WORK/leak.md" >/dev/null 2>&1
assert "配布先でもクレデンシャル形状を検出する（exit 1）" "1" "$?"

echo "[test_check_setup_guide] 判定不能"

python3 "$SCRIPT" "$WORK/does-not-exist" >/dev/null 2>&1
assert "対象が無ければ判定不能（exit 2）" "2" "$?"

python3 "$SCRIPT" "$ROUTINES_SETUP" "$WORK/does-not-exist.md" >/dev/null 2>&1
assert "複数対象の 1 つが見つからなければ判定不能（exit 2・読み飛ばさない）" "2" "$?"

mkdir -p "$WORK/empty"
python3 "$SCRIPT" "$WORK/empty" >/dev/null 2>&1
assert ".md の無いディレクトリは判定不能（exit 2）" "2" "$?"

echo "[test_check_setup_guide] PASS=$pass FAIL=$fail"
[[ "$fail" -eq 0 ]]
