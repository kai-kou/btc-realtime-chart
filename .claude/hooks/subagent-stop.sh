#!/usr/bin/env bash
# subagent-stop.sh — SubagentStop フック（P-11 / #367）
#
# サブエージェントが異常終了したときだけ、その状態を additionalContext として
# オーケストレータに注入し、再試行・代替手段の選択を促す。
#
# 目的:
#   - エラー終了・非正常終了したサブエージェントの原因を可視化する
#   - 正常終了・判定不能のときは何も出力せず即終了する（低ノイズ・fail-safe 設計）
#
# 入力 (stdin JSON): 公式スキーマ（code.claude.com/docs/en/hooks・2026-07-29 逐語確認）
#   {
#     "hook_event_name":        "SubagentStop",
#     "session_id":             string,
#     "transcript_path":        string,
#     "cwd":                    string,
#     "permission_mode":        string,
#     "agent_id":               string,   // サブエージェントの一意 ID
#     "agent_type":             string,   // エージェント名（Explore / general-purpose 等）
#     "last_assistant_message": string,   // サブエージェントの最終テキスト出力
#     "stop_reason":            string    // 終了理由（"end_turn" 等）
#   }
#   ⚠️ `is_error` / `result` は **公式スキーマに存在しない**（#367）。旧実装はこの 2 つを読んでいたため、
#      `stop_reason != "end_turn"` のとき「最終出力（末尾）: (空)」という中身のない異常報告を
#      オーケストレータへ注入していた（診断不能なうえ、成果が失われたかのような誤った観測になる）。
#      最終出力は `last_assistant_message` から取る。互換のため旧フィールドも読む。
#
# 出力 (stdout JSON):
#   { "hookSpecificOutput": { "hookEventName": "SubagentStop", "additionalContext": "..." } }
#   ← 異常終了時のみ出力する。正常時は無出力で exit 0。
#
# 設計原則（fail-safe）:
#   「異常と断定できるシグナルがあるときだけ発話する」。未知・空の exit_reason は
#   正常扱いで無音にする（誤検知でオーケストレータの観測を汚染しないことを優先する）。
#
# 注記:
#   /usage コマンドは headless(-p)モードでは動作しないため、本フックでは使用しない（P-11・#2672）。

set -euo pipefail

INPUT=$(cat 2>/dev/null || true)
if [[ -z "$INPUT" ]]; then
  exit 0
fi

# python3 が無い環境ではフィードバックなしで終了する（誤判定より無音を優先）
if ! command -v python3 &>/dev/null; then
  exit 0
fi

printf '%s' "$INPUT" | python3 -c '
import json, sys

# 異常を示す stop_reason のトークン（部分一致・小文字比較）。
# 公式は stop_reason の値域を列挙していないため、既知の異常語を含むときだけ発話する。
ABNORMAL_TOKENS = ("error", "max_token", "refus", "timeout", "interrupt", "abort", "fail", "cancel")

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)

# 公式フィールドを優先し、旧フィールド（実在しない result / is_error）は後方互換で見る
stop_reason = str(d.get("stop_reason") or d.get("exit_reason") or "").strip()
last_message = str(d.get("last_assistant_message") or d.get("result") or "")
agent_type = str(d.get("agent_type") or "unknown")
agent_id = str(d.get("agent_id") or "-")
legacy_is_error = d.get("is_error") is True

lowered = stop_reason.lower()
abnormal = legacy_is_error or any(tok in lowered for tok in ABNORMAL_TOKENS)

# 正常終了・判定不能（空 / 未知の stop_reason）は無音で終了する
if not abnormal:
    sys.exit(0)

if "max_token" in lowered:
    advice = "- トークン上限超過: サブエージェントへのプロンプトを分割するか、対象範囲を狭めて再実行してください"
elif "timeout" in lowered or "interrupt" in lowered or "abort" in lowered or "cancel" in lowered:
    advice = "- 中断・タイムアウト: 処理を分割して再実行するか、別手段（直接実行）に切り替えてください"
else:
    advice = "- エラー終了: 上記の出力を確認し、根本原因を特定してください（L-077: problem-investigation-protocol.md を参照）"

tail = last_message[-500:] if last_message else "(最終出力なし)"
shown_reason = stop_reason or "unknown"
feedback = (
    f"サブエージェント（agent_type: {agent_type} / agent_id: {agent_id}）が正常に完了しませんでした"
    f"（stop_reason: {shown_reason}）。\n\n"
    f"最終出力（末尾）:\n{tail}\n\n"
    f"推奨アクション:\n{advice}"
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SubagentStop",
        "additionalContext": feedback,
    }
}, ensure_ascii=False))
'
