#!/bin/bash
# Lv3 ブロッキングフック共通ヘルパー（Issue #142）
#
# 公式仕様: exit 2 でツール/処理をブロックするとき、stdout に出した JSON
#（systemMessage 等）は Claude Code に無視される（stdout JSON と exit 2 は排他）。
# ブロック理由を Claude に確実に届けるには stderr に出力する
# （docs/rules/claude-code-optimization.md「フックの出力と制御」参照）。
#
# 使い方: source "$(dirname "$0")/lib/hook_block.sh" してから
#   hook_block "ブロック理由（複数行可）"
# を呼ぶ（内部で stderr 出力 + exit 2 する。呼び出し元に戻らない）。
hook_block() {
  printf '%s\n' "$1" >&2
  exit 2
}
