#!/bin/bash
# progress_heartbeat.sh — 制作系長時間処理の汎用進捗ハートビート
#
# 目的（docs/rules/progress-reporting-rules.md 軸 B）:
#   音声生成・レンダリング等、5 分を超える単一の長時間コマンドを run_in_background で
#   実行する際に、本スクリプトを併走させて一定間隔で stdout に進捗を出力する。
#   Monitor ツールでこの出力をストリームすることで、
#     (1) セッションのアイドルタイムアウト（クラウド ~10分）を防止し、
#     (2) ユーザー/Claude が進捗を確認できる。
#
# 特定パイプライン専用のハートビートスクリプトが対象・完了パターンを固定するのに対し、
# 本スクリプトは任意の処理向けに完了/エラー検出パターンを引数で指定できる汎用版。
#
# Usage:
#   bash tools/progress_heartbeat.sh <id> [max_minutes] [options]
#
# Options:
#   --log PATH         監視するログファイル（デフォルト: /tmp/progress_<id>.log）
#   --done "PATTERN"   完了検出の grep -E パターン
#                      （デフォルト: "完了|done|DONE|Finished|Rendered and encoded|All done|✅"）
#   --error "PATTERN"  エラー検出の grep -E パターン
#                      （デフォルト: "Error:|error:|Exception|Traceback|FAILED|failed|❌"）
#   --interval SEC     ハートビート間隔秒（デフォルト: 180＝3分）
#   --label "TEXT"     表示ラベル（例: "V123 audio"）。省略時は <id>
#   --emoji EMOJI      進捗行の絵文字（デフォルト: ⏱️）
#   --slack            開始・完了・エラー時に slack_notify.py message で FYI 通知（@mention なし・失敗しても継続）
#   --pipeline NAME    --slack 用の pipeline 名（audio/image/video 等）
#   --self-test        完了/エラー検出ロジックを自己検証して終了
#
# 例（音声生成・shorts/audio パイプライン）:
#   Bash(run_in_background=true): python3 tools/generate_audio.py V123 > /tmp/progress_V123audio.log 2>&1
#   Bash(run_in_background=true): bash tools/progress_heartbeat.sh V123audio 45 \
#       --log /tmp/progress_V123audio.log --label "V123 audio" --emoji 🎙️ --slack --pipeline audio
#   → 返った heartbeat の PID を Monitor ツールに渡す
#
# 出力形式（1行1イベント・Monitor がトリガーする）:
#   🎙️ [progress-hb 3分/45分] V123 audio: running — ログ最終行 (HH:MM JST)
#   ✅ [progress-hb] V123 audio: 完了検出 — ハートビート終了 (HH:MM JST)
#   ❌ [progress-hb 6分/45分] V123 audio: エラー検出 — ... (HH:MM JST)
#   ⏰ [progress-hb] タイムアウト — 処理状態を手動確認してください

set -euo pipefail

# ── デフォルト値 ──
DONE_PATTERN_DEFAULT="完了|done|DONE|Finished|Rendered and encoded|All done|✅"
ERROR_PATTERN_DEFAULT="Error:|error:|Exception|Traceback|FAILED|failed|❌"

DONE_PATTERN="$DONE_PATTERN_DEFAULT"
ERROR_PATTERN="$ERROR_PATTERN_DEFAULT"
INTERVAL=180
LABEL=""
EMOJI="⏱️"
USE_SLACK=false
PIPELINE=""
LOG_FILE=""
SELF_TEST=false

# ── 自己テスト（パターン検出ロジックの検証） ──
run_self_test() {
  local tmp rc=0
  tmp="$(mktemp)"

  printf 'rendering frame 100\nRendered and encoded 1200 frames\n' > "$tmp"
  if grep -qE "$DONE_PATTERN_DEFAULT" "$tmp"; then
    echo "[OK] 完了パターン検出（Rendered and encoded）"
  else
    echo "[NG] 完了パターン未検出"; rc=1
  fi

  printf 'generating...\n完了しました\n' > "$tmp"
  if grep -qE "$DONE_PATTERN_DEFAULT" "$tmp"; then
    echo "[OK] 完了パターン検出（日本語 完了）"
  else
    echo "[NG] 完了パターン未検出（日本語）"; rc=1
  fi

  printf 'step 1 ok\nTraceback (most recent call last):\n' > "$tmp"
  if grep -qE "$ERROR_PATTERN_DEFAULT" "$tmp"; then
    echo "[OK] エラーパターン検出（Traceback）"
  else
    echo "[NG] エラーパターン未検出"; rc=1
  fi

  printf 'all good\nprocessing item 5/10\n' > "$tmp"
  if grep -qE "$DONE_PATTERN_DEFAULT" "$tmp" || grep -qE "$ERROR_PATTERN_DEFAULT" "$tmp"; then
    echo "[NG] 進行中ログを誤検出"; rc=1
  else
    echo "[OK] 進行中ログは未検出（誤検出なし）"
  fi

  rm -f "$tmp"
  if [ "$rc" -eq 0 ]; then
    echo "[SELF-TEST PASS] progress_heartbeat.sh"
  else
    echo "[SELF-TEST FAIL] progress_heartbeat.sh"
  fi
  return "$rc"
}

# ── 引数パース ──
POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --log)      LOG_FILE="${2:?--log にパスを指定してください}"; shift 2 ;;
    --done)     DONE_PATTERN="${2:?--done にパターンを指定してください}"; shift 2 ;;
    --error)    ERROR_PATTERN="${2:?--error にパターンを指定してください}"; shift 2 ;;
    --interval) INTERVAL="${2:?--interval に秒数を指定してください}"; shift 2 ;;
    --label)    LABEL="${2:?--label にラベルを指定してください}"; shift 2 ;;
    --emoji)    EMOJI="${2:?--emoji に絵文字を指定してください}"; shift 2 ;;
    --pipeline) PIPELINE="${2:?--pipeline に名前を指定してください}"; shift 2 ;;
    --slack)    USE_SLACK=true; shift ;;
    --self-test) SELF_TEST=true; shift ;;
    --*)        echo "ERROR: 不明なオプション: $1" >&2; exit 2 ;;
    *)          POSITIONAL+=("$1"); shift ;;
  esac
done

if $SELF_TEST; then
  run_self_test
  exit $?
fi

ID="${POSITIONAL[0]:?Usage: bash tools/progress_heartbeat.sh <id> [max_minutes] [options]}"
MAX_MINUTES="${POSITIONAL[1]:-30}"

# ── バリデーション（シェルインジェクション防止） ──
if ! [[ "$ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: id に使用できない文字が含まれています: ${ID}" >&2
  exit 1
fi
if ! [[ "$MAX_MINUTES" =~ ^[0-9]+$ ]] || [ "$MAX_MINUTES" -lt 1 ]; then
  echo "ERROR: max_minutes は正の整数を指定してください: ${MAX_MINUTES}" >&2
  exit 1
fi
if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -lt 10 ]; then
  echo "ERROR: interval は 10 以上の整数を指定してください: ${INTERVAL}" >&2
  exit 1
fi

[ -z "$LOG_FILE" ] && LOG_FILE="/tmp/progress_${ID}.log"
[ -z "$LABEL" ] && LABEL="$ID"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

now_jst() { TZ=Asia/Tokyo date '+%H:%M' 2>/dev/null || date '+%H:%M'; }

# Slack マイルストーン通知（失敗しても heartbeat は継続）
# FYI 扱い（@mention しない）のため message タイプを使う（user-notification-triage.md）
slack_progress() {
  local event="$1" detail="$2" icon
  $USE_SLACK || return 0
  case "$event" in
    start) icon="🚀" ;;
    done)  icon="✅" ;;
    error) icon="❌" ;;
    *)     icon="📊" ;;
  esac
  python3 "$REPO_ROOT/tools/slack_notify.py" message \
    --text "${icon} [進捗 ${PIPELINE:-制作}] ${detail}" >/dev/null 2>&1 || true
}

TOTAL_SECONDS=$(( MAX_MINUTES * 60 ))
ITERATIONS=$(( (TOTAL_SECONDS + INTERVAL - 1) / INTERVAL ))

echo "${EMOJI} [progress-hb] ${LABEL} 監視開始（最大${MAX_MINUTES}分・${INTERVAL}秒間隔・${ITERATIONS}回）"
echo "   ログファイル: ${LOG_FILE}"
slack_progress start "${LABEL} 処理開始（heartbeat 監視・最大${MAX_MINUTES}分）"

for (( i=1; i<=ITERATIONS; i++ )); do
  sleep "$INTERVAL"

  elapsed_min=$(( i * INTERVAL / 60 ))
  timestamp="$(now_jst)"

  if [ -f "$LOG_FILE" ]; then
    last_line=$(tail -1 "$LOG_FILE" 2>/dev/null | tr -d '\n' | cut -c1-80 || echo "(ログ読み取り失敗)")
    if grep -qE "$DONE_PATTERN" "$LOG_FILE" 2>/dev/null; then
      echo "✅ [progress-hb] ${LABEL}: 完了検出 — ハートビート終了 (${timestamp} JST)"
      slack_progress done "${LABEL} 完了（実測 約${elapsed_min}分）"
      exit 0
    fi
    if grep -qE "$ERROR_PATTERN" "$LOG_FILE" 2>/dev/null; then
      echo "❌ [progress-hb ${elapsed_min}分/${MAX_MINUTES}分] ${LABEL}: エラー検出 — ${last_line} (${timestamp} JST)"
      slack_progress error "${LABEL} でエラー検出（経過 約${elapsed_min}分）: ${last_line}"
      exit 1
    fi
    echo "${EMOJI} [progress-hb ${elapsed_min}分/${MAX_MINUTES}分] ${LABEL}: running — ${last_line} (${timestamp} JST)"
  else
    echo "${EMOJI} [progress-hb ${elapsed_min}分/${MAX_MINUTES}分] ${LABEL}: ログ未生成（処理開始待ち） (${timestamp} JST)"
  fi
done

echo "⏰ [progress-hb] ${MAX_MINUTES}分経過 — タイムアウト。処理状態を手動確認してください"
exit 1
