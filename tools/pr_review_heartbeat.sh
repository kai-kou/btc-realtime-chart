#!/bin/bash
# pr_review_heartbeat.sh — PRレビュー待機中のセッションタイムアウト防止ハートビート
#
# Usage: bash tools/pr_review_heartbeat.sh <pr_number> [max_minutes]
#
# subscribe_pr_activity で待機中、クラウドセッションが10分でタイムアウトする問題を防ぐ。
# 5分ごとに PR レビュー状態を stdout に出力し、Monitor ツールでストリームすることで
# Claude セッションを生かし続ける。
#
# 使い方（SKILL.md / pr-review-flow.md 参照）:
#   Bash(run_in_background=true): bash tools/pr_review_heartbeat.sh {PR_NUMBER} 30
#   → 返ってきた PID を Monitor ツールに渡して stdout をストリームする
#
# 出力形式（1行1イベント、Monitor がトリガーする）:
#   🔄 [heartbeat 5分/30分] PR #N: status — summary (HH:MM JST)
#   ✅ [heartbeat] PR #N は対応完了 — ハートビート終了
#   ⏰ [heartbeat] タイムアウト — 最終状態確認を実行してください

set -euo pipefail

# --test フラグ: 環境チェックのみ実行して終了（クラウド環境での起動確認用）
if [ "${1:-}" = "--test" ]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
  CHECK_SCRIPT="${PROJECT_DIR}/tools/check_pending_pr_reviews.py"
  ok=true
  command -v python3 &>/dev/null || { echo "⚠️ python3 not found"; ok=false; }
  [ -f "$CHECK_SCRIPT" ]         || { echo "⚠️ check_pending_pr_reviews.py not found: ${CHECK_SCRIPT}"; ok=false; }
  command -v gh &>/dev/null      || echo "ℹ️ gh CLI not found (will use check_skipped mode)"
  [ -n "${GH_TOKEN:-}" ]         || echo "ℹ️ GH_TOKEN not set (will use check_skipped mode)"
  if $ok; then
    echo "✅ heartbeat OK"
    exit 0
  else
    echo "⚠️ heartbeat 利用不可 → check_pending_pr_reviews.py で代替"
    exit 1
  fi
fi

PR_NUMBER="${1:?Usage: bash tools/pr_review_heartbeat.sh <pr_number> [max_minutes]}"
MAX_MINUTES="${2:-30}"
INTERVAL=300  # 5分 = 300秒

# PR_NUMBER が数値か検証（セキュリティ: シェルインジェクション防止）
if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "ERROR: pr_number は数値を指定してください: ${PR_NUMBER}" >&2
  exit 1
fi

# MAX_MINUTES が正の整数か検証
if ! [[ "$MAX_MINUTES" =~ ^[0-9]+$ ]] || [ "$MAX_MINUTES" -lt 1 ]; then
  echo "ERROR: max_minutes は正の整数を指定してください: ${MAX_MINUTES}" >&2
  exit 1
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
CHECK_SCRIPT="${PROJECT_DIR}/tools/check_pending_pr_reviews.py"
# 切り上げ除算: max_minutes が INTERVAL の倍数でない場合も正しく動作する（Copilot 指摘修正 #1）
TOTAL_SECONDS=$(( MAX_MINUTES * 60 ))
ITERATIONS=$(( (TOTAL_SECONDS + INTERVAL - 1) / INTERVAL ))

echo "🔄 [heartbeat] PR #${PR_NUMBER} 監視開始（最大${MAX_MINUTES}分・${INTERVAL}秒間隔・${ITERATIONS}回）"

for (( i=1; i<=ITERATIONS; i++ )); do
  sleep "$INTERVAL"

  elapsed_min=$(( i * INTERVAL / 60 ))
  timestamp=$(TZ=Asia/Tokyo date '+%H:%M' 2>/dev/null || date '+%H:%M')

  # PR レビュー状態を確認（タイムアウト付き）
  # Copilot 指摘修正 #2: gh コマンドも条件に追加し、exit code で失敗を検知する（[] フォールバックで not_found 誤判定を防止）
  # Copilot 指摘修正 #3: SKIP 理由を個別に判定して具体的なメッセージを出力する
  if [ -f "$CHECK_SCRIPT" ] && command -v python3 &>/dev/null && command -v gh &>/dev/null && [ -n "${GH_TOKEN:-}" ]; then
    _json=$(timeout 25s python3 "$CHECK_SCRIPT" --json 2>/dev/null)
    _json_exit=$?
    if [ "$_json_exit" -ne 0 ] || [ -z "$_json" ]; then
      # スクリプト実行失敗 → check_failed 扱い（not_found と区別するため [] を使わない）
      pr_status="check_failed"
      pr_summary="check_pending_pr_reviews.py 実行エラー（exitcode: ${_json_exit}）"
    else
      _result=$(echo "$_json" | python3 -c "
import json, sys
try:
    prs = json.load(sys.stdin)
    target = [p for p in prs if p.get('pr_number') == ${PR_NUMBER}]
    if target:
        p = target[0]
        print(json.dumps({'status': p.get('status', 'unknown'), 'summary': p.get('summary', '')[:80]}))
    else:
        # リストに存在しない = マージ済みまたは no_action
        print(json.dumps({'status': 'not_found', 'summary': '対応完了（マージ済みまたは no_action）'}))
except Exception as e:
    print(json.dumps({'status': 'check_failed', 'summary': str(e)[:60]}))
" 2>/dev/null || echo '{"status":"check_failed","summary":"JSON解析エラー"}')
      pr_status=$(echo "$_result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
      pr_summary=$(echo "$_result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('summary',''))" 2>/dev/null || echo "")
    fi
  else
    # 個別条件を判定してより具体的な SKIP 理由を出力（Copilot 指摘修正 #3）
    pr_status="check_skipped"
    if [ ! -f "$CHECK_SCRIPT" ]; then
      pr_summary="check_pending_pr_reviews.py が見つかりません（パス: ${CHECK_SCRIPT}）"
    elif ! command -v python3 &>/dev/null; then
      pr_summary="python3 が見つかりません"
    elif ! command -v gh &>/dev/null; then
      pr_summary="gh CLI が見つかりません"
    elif [ -z "${GH_TOKEN:-}" ]; then
      pr_summary="GH_TOKEN 未設定"
    else
      pr_summary="チェック条件未満（詳細不明）"
    fi
  fi

  echo "⏱️  [heartbeat ${elapsed_min}分/${MAX_MINUTES}分] PR #${PR_NUMBER}: ${pr_status} — ${pr_summary} (${timestamp} JST)"

  # 終了条件: 対応完了 or マージ済み
  case "$pr_status" in
    not_found|no_action)
      echo "✅ [heartbeat] PR #${PR_NUMBER} は対応完了（${pr_status}）— ハートビート終了"
      exit 0
      ;;
    ready_to_merge)
      echo "🚀 [heartbeat] PR #${PR_NUMBER} は ready_to_merge — 即時マージを実行してください"
      # 終了はせず、Claude が Monitor 通知を受けて判断する
      ;;
  esac
done

echo "⏰ [heartbeat] ${MAX_MINUTES}分経過 — タイムアウト。check_pending_pr_reviews.py で最終状態を確認してください"
exit 0
