#!/bin/bash
# test_generate_autonomy_setup.sh — tools/generate_autonomy_setup.py の統合テスト（Issue #110・D-36 の TDD 規律）
#
# `--self-test`（純粋関数の単体テスト）は `main()` を 1 度も通らない。そのため CLI 経路の回帰
# （引数解決 → 雛形読み込み → 未解決検査 → 書き出し）は検出できず、Layer 1 セルフレビューで
# 実際に「main() の呼び出しを壊しても --self-test は 26/26 PASS のまま」と実証された。
# 本テストは CLI を実際に叩いて **出力と終了コードの性質** を assert する。
#
#   使い方: bash tools/test_generate_autonomy_setup.sh
#   終了コード: 0 = 全 assert PASS / 1 = いずれか FAIL
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/tools/generate_autonomy_setup.py"
TEMPLATES="$REPO_ROOT/docs/autonomy-templates"
SLUG="example-owner/sample-product"

pass=0
fail=0

ok() { pass=$((pass + 1)); echo "  ✓ $1"; }
ng() { fail=$((fail + 1)); echo "  ✗ $1"; }
assert() { if [[ "$2" == "$3" ]]; then ok "$1"; else ng "$1（want=$2 got=$3）"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[test_generate_autonomy_setup] 単体テストと引数バリデーション"

python3 "$SCRIPT" --self-test >/dev/null 2>&1
assert "--self-test（純粋関数）が PASS" "0" "$?"

python3 "$SCRIPT" --out "$WORK" --name "Sample" >/dev/null 2>&1
assert "--slug 欠落は判定不能（exit 2）" "2" "$?"

python3 "$SCRIPT" --out "$WORK" --name "Sample" --slug "not-a-slug" >/dev/null 2>&1
assert "不正な slug は判定不能（exit 2）" "2" "$?"

python3 "$SCRIPT" --out "$WORK/nonexistent" --name "Sample" --slug "$SLUG" >/dev/null 2>&1
assert "生成先が無ければ判定不能（exit 2）" "2" "$?"

mkdir -p "$WORK/empty-templates"
python3 "$SCRIPT" --out "$WORK" --name "Sample" --slug "$SLUG" \
  --template-dir "$WORK/empty-templates" >/dev/null 2>&1
assert "雛形欠落は判定不能（exit 2・fail-closed）" "2" "$?"

echo "[test_generate_autonomy_setup] 生成（CLI 統合経路）"

OUT="$WORK/product"
mkdir -p "$OUT"

python3 "$SCRIPT" --out "$OUT" --name "Sample Product" --slug "$SLUG" --dry-run >/dev/null 2>&1
assert "--dry-run が成功（exit 0）" "0" "$?"
assert "--dry-run では書き出さない" "0" "$(find "$OUT" -type f | wc -l | tr -d ' ')"

python3 "$SCRIPT" --out "$OUT" --name "Sample Product" --slug "$SLUG" >/dev/null 2>&1
assert "生成が成功（exit 0）" "0" "$?"
assert "3 ファイルを生成する" "3" "$(find "$OUT" -type f | wc -l | tr -d ' ')"
for rel in "docs/routines.md" "docs/setup/routines-setup.md" "config/labels.yaml"; do
  if [[ -f "$OUT/$rel" ]]; then ok "生成先に $rel がある"; else ng "生成先に $rel が無い"; fi
done

if grep -rq '{PROJECT_NAME}\|{REPO_SLUG}\|{CRON}\|{CRON_HUMAN}' "$OUT"; then
  ng "生成物にプレースホルダが残っている"
else
  ok "生成物にプレースホルダが残らない"
fi

if grep -q "$SLUG" "$OUT/docs/routines.md"; then ok "slug が埋まる"; else ng "slug が埋まらない"; fi
if grep -q "6 時間ごと・1 日 4 回" "$OUT/docs/routines.md"; then
  ok "既定 cron が可読文になる"
else
  ng "既定 cron の可読文が入らない"
fi

python3 "$SCRIPT" --out "$OUT" --name "Sample Product" --slug "$SLUG" 2>/dev/null | grep -q "skipped"
assert "既存ファイルは上書きしない" "0" "$?"

python3 "$SCRIPT" --out "$OUT" --name "Renamed Product" --slug "$SLUG" --force >/dev/null 2>&1
assert "--force で上書きできる（exit 0）" "0" "$?"
if grep -q "Renamed Product" "$OUT/docs/routines.md"; then ok "--force が中身を更新する"; else ng "--force で中身が変わらない"; fi

echo "[test_generate_autonomy_setup] 未解決検査の回帰（置換後の再スキャンに戻ると落ちる）"

# 🔴 このケースが本テストの主目的。main() が「置換後テキスト」を検査する実装へ戻ると、
# 値そのものに含まれる `{CRON}` を未解決と誤判定して exit 1 になる（--self-test では検出できない）。
TRICKY="$WORK/tricky"
mkdir -p "$TRICKY"
python3 "$SCRIPT" --out "$TRICKY" --name '{CRON} Bot' --slug "$SLUG" >/dev/null 2>&1
assert "値がプレースホルダ綴りを含んでも生成できる（exit 0）" "0" "$?"
if grep -q '{CRON} Bot' "$TRICKY/docs/routines.md"; then
  ok "値の綴りがそのまま出力される"
else
  ng "値の綴りが出力されない"
fi

echo "[test_generate_autonomy_setup] ラベルコマンド出力"

label_lines="$(python3 "$SCRIPT" --print-label-commands --slug "$SLUG" 2>/dev/null | wc -l | tr -d ' ')"
yaml_labels="$(grep -c '^  - name:' "$TEMPLATES/labels.yaml" | tr -d ' ')"
assert "ラベル定義の件数とコマンド数が一致する" "$yaml_labels" "$label_lines"

from_file="$(python3 "$SCRIPT" --print-label-commands --slug "$SLUG" --labels-file "$OUT/config/labels.yaml" 2>/dev/null | wc -l | tr -d ' ')"
assert "--labels-file 経路でも同じ件数" "$yaml_labels" "$from_file"

python3 "$SCRIPT" --print-label-commands --labels-file "$OUT/config/labels.yaml" >/dev/null 2>&1
assert "--slug 無しの --print-label-commands は判定不能（exit 2）" "2" "$?"

printf 'labels: [broken: yaml: here\n' > "$WORK/bad.yaml"
python3 "$SCRIPT" --print-label-commands --slug "$SLUG" --labels-file "$WORK/bad.yaml" >/dev/null 2>&1
assert "壊れた YAML はトレースバックを出さず判定不能（exit 2）" "2" "$?"

python3 "$SCRIPT" --print-label-commands --slug "$SLUG" --labels-file "$WORK/nonexistent.yaml" >/dev/null 2>&1
assert "ラベル定義が無ければ判定不能（exit 2）" "2" "$?"

echo
echo "[test_generate_autonomy_setup] $pass passed / $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
