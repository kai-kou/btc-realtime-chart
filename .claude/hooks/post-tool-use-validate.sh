#!/bin/bash
# PostToolUse hook: プロジェクト成果物のバリデーション拡張ポイント（汎用ベース）
#
# このフックは Write/Edit などのツール実行後に呼ばれ、プロジェクト固有の成果物
# （生成 JSON・設定ファイル・スキーマ等）を物理的に検証する「Lv3 ハードコンストレイント」
# の置き場所。汎用ベースではデフォルト no-op（何も検証しない）。
#
# HOOK_PROFILE で警告強度を制御できる:
#   minimal  : Error レベルのみ
#   standard : Error + Warning（デフォルト）
#   strict   : 将来の強化用予約
#
# プロジェクト固有のバリデーションを追加する例:
#   1. stdin から tool_name / file_path を取得
#   2. 対象ファイル（例: *.schema.json）なら python3 tools/validate_xxx.py で検証
#   3. Error 検出時は hook_block "理由" でブロック（stderr 出力 + exit 2）
#
# 入力 (stdin JSON): { "tool_name": "...", "tool_input": { "file_path": "..." }, ... }
# 出力: ブロックする場合のみブロック理由を stderr に出して exit 2（hook_block 参照）

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_block.sh
source "$HOOK_DIR/lib/hook_block.sh"

input=$(cat 2>/dev/null || true)
HOOK_PROFILE="${HOOK_PROFILE:-standard}"

# --- プロジェクト固有のバリデーションをここに追加する ---
# 例:
#   tool_name=$(echo "$input" | jq -r '.tool_name // ""')
#   file_path=$(echo "$input" | jq -r '.tool_input.file_path // ""')
#   if [[ "$file_path" == *.schema.json ]]; then
#     if ! python3 "$(dirname "$0")/../../tools/validate_schema.py" "$file_path" 2>/dev/null; then
#       hook_block "[validate] スキーマ検証に失敗しました。修正してください。"
#     fi
#   fi

# --- lessons-core.md（Hot 層）サイズ上限 + 番号重複の機械強制（Lv3・lessons-management.md §4・#340）---
# lessons-core.md または Warm 層（docs/rules/lessons/*.md）を Write/Edit した直後に
# lessons_guard.py check を実行し、上限超過（350 行 / 15 エントリ）または L-NNN 番号の
# 重複（Hot/Warm 横断）があれば exit 2 でブロックして是正を促す。
file_path=$(printf '%s\n' "$input" | jq -r '.tool_input.file_path // ""' 2>/dev/null || true)
if [[ "$file_path" == *docs/rules/lessons-core.md || "$file_path" == *docs/rules/lessons/*.md ]]; then
  REPO_ROOT="$(cd "$HOOK_DIR/../.." && pwd)"
  # python3 不在環境（最小 Docker・一部 CI 等）で ! python3 が真になり誤ブロックするのを防ぐ。
  if [[ -f "$REPO_ROOT/tools/lessons_guard.py" ]] \
     && command -v python3 >/dev/null 2>&1 \
     && ! python3 "$REPO_ROOT/tools/lessons_guard.py" check >/dev/null 2>&1; then
    hook_block "[lessons_guard] lessons が Hot 層の上限（350 行 / 15 エントリ）を超過しているか、L-NNN 番号が Hot/Warm 横断で重複しています。上限超過は prune（python3 tools/lessons_guard.py prune --apply）または Warm 降格、番号重複は片方を未使用番号へ振り直し参照元を追随更新して解消してください（lessons-management.md §3/§4・#340）。"
  fi
fi

# デフォルト: 何も検証しない（許可）
exit 0
