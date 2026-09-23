#!/bin/bash
# PR AIレビュー ポーリングスクリプト
# 単一のバックグラウンドプロセスでレビュー到着を監視する。
# 使い方: tools/poll_pr_reviews.sh <owner/repo> <pr_number> <output_file> [after_timestamp]
#
# after_timestamp: ISO 8601形式（例: "2026-03-24T14:30:00Z"）。
#   指定した時刻以降のレビュー・コメントのみを検出対象にする。
#   省略時は全レビューを対象にする（初回PR作成時のポーリング向け）。
#   再レビュー依頼後のポーリングでは `$(date -u +"%Y-%m-%dT%H:%M:%SZ")` を渡す。
#
# output_file にレビュー状況をJSON形式で書き出す。
# メインエージェントは output_file を読んでレビュー対応を判断する。
set -euo pipefail

REPO="${1:?Usage: $0 <owner/repo> <pr_number> <output_file> [after_timestamp]}"
PR_NUMBER="${2:?Usage: $0 <owner/repo> <pr_number> <output_file> [after_timestamp]}"
OUTPUT_FILE="${3:?Usage: $0 <owner/repo> <pr_number> <output_file> [after_timestamp]}"
# 第4引数: タイムスタンプフィルタ（省略時はエポック = フィルタなし）
AFTER_TIMESTAMP="${4:-1970-01-01T00:00:00Z}"

# --- 引数フォーマットバリデーション（Lv3 ハードコンストレイント） ---
# owner/repo 形式チェック（スラッシュを含み、前後が非空）
if ! echo "$REPO" | grep -qE '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'; then
  echo "ERROR: 第1引数 '$REPO' が owner/repo 形式ではありません。" >&2
  echo "正しい形式: tools/poll_pr_reviews.sh <owner/repo> <pr_number> <output_file>" >&2
  echo "例: tools/poll_pr_reviews.sh kai-kou/btc-realtime-chart 187 /tmp/pr_review_187.json" >&2
  exit 1
fi

# pr_number が正の整数かチェック
if ! echo "$PR_NUMBER" | grep -qE '^[0-9]+$'; then
  echo "ERROR: 第2引数 '$PR_NUMBER' が正の整数ではありません。" >&2
  echo "正しい形式: tools/poll_pr_reviews.sh <owner/repo> <pr_number> <output_file>" >&2
  exit 1
fi

# output_file がリポジトリルートのファイル名（パス区切りなし）でないことを確認
# リポジトリ内に状態ファイルを作ってしまう事故を防止
if ! echo "$OUTPUT_FILE" | grep -qE '/'; then
  echo "ERROR: 第3引数 '$OUTPUT_FILE' にパス区切り（/）が含まれていません。" >&2
  echo "リポジトリルートに状態ファイルが作成されるのを防止します。" >&2
  echo "正しい形式: /tmp/pr_review_<pr_number>.json" >&2
  exit 1
fi

# after_timestamp が ISO 8601 形式かチェック（省略デフォルト値は除く）
if [ "$AFTER_TIMESTAMP" != "1970-01-01T00:00:00Z" ]; then
  if ! echo "$AFTER_TIMESTAMP" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'; then
    echo "ERROR: 第4引数 '$AFTER_TIMESTAMP' が ISO 8601 形式（YYYY-MM-DDTHH:MM:SSZ）ではありません。" >&2
    echo "例: $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >&2
    exit 1
  fi
fi

# --- gh 疎通プローブ（クラウド 403 の即時検出・Issue #133） ---
# クラウドでは gh の repo 操作が egress プロキシに 403 でブロックされる（L-114）。
# 従来は `|| echo "[]"` の縮退で 25 分間「レビュー 0 件」を装って timeout していたため、
# ループ開始前に 1 回だけ疎通確認し、失敗時は gh_unavailable を明示して exit 3 する
#（check_pending_pr_reviews.py の GH_UNAVAILABLE / exit 3 と同じ規約）。
if ! gh api "repos/${REPO}/pulls/${PR_NUMBER}/reviews" --jq 'length' >/dev/null 2>&1; then
  cat > "$OUTPUT_FILE" <<EOJSON
{"status":"gh_unavailable","elapsed":0,"reviews":[],"inline_comments":[],"nudged":false,"after_timestamp":"${AFTER_TIMESTAMP}","error":"gh api 失敗（クラウドでは 403・L-114）。mcp__github__pull_request_read(method=get_reviews / get_review_comments) で直接確認すること"}
EOJSON
  echo "ERROR: gh_unavailable — gh api repos/${REPO}/pulls/${PR_NUMBER}/reviews が失敗しました（クラウドでは 403・L-114）。mcp__github__pull_request_read で直接確認してください。" >&2
  exit 3
fi

INTERVAL=120        # ポーリング間隔（秒）
NUDGE_AFTER=900     # 催促までの待機時間（秒）= 15分
TIMEOUT=1500        # 全体タイムアウト（秒）= 25分

START_TIME=$(date +%s)
NUDGED=false

# 初期状態を書き出す
cat > "$OUTPUT_FILE" <<EOJSON
{"status":"polling","elapsed":0,"reviews":[],"inline_comments":[],"nudged":false,"after_timestamp":"${AFTER_TIMESTAMP}"}
EOJSON

while true; do
  sleep "$INTERVAL"

  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TIME ))

  # レビュー取得（after_timestamp 以降のみ）
  # submitted_at でフィルタ: "1970-..." の場合は全件取得（フィルタなし）
  REVIEWS=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}/reviews" \
    --jq "[.[] | select(.submitted_at > \"${AFTER_TIMESTAMP}\") | {user: .user.login, state, submitted_at, body}]" 2>/dev/null || echo "[]")

  # インラインコメント取得（after_timestamp 以降のみ）
  INLINE=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}/comments" \
    --jq "[.[] | select(.created_at > \"${AFTER_TIMESTAMP}\") | {user: .user.login, created_at, body, path, line}]" 2>/dev/null || echo "[]")

  # Issueコメント（ボットのみ）取得（after_timestamp 以降のみ）
  ISSUE_COMMENTS=$(gh api "repos/${REPO}/issues/${PR_NUMBER}/comments" \
    --jq "[.[] | select((.created_at > \"${AFTER_TIMESTAMP}\") and (.user.type == \"Bot\" or (.user.login | test(\"copilot|gemini\"; \"i\")))) | {user: .user.login, created_at, body_len: (.body | length), body_start: (.body[:200])}]" 2>/dev/null || echo "[]")

  # レビューが届いたか判定（jq で統一、python3 依存を排除）
  REVIEW_COUNT=$(echo "$REVIEWS" | jq 'length' 2>/dev/null || echo "0")
  INLINE_COUNT=$(echo "$INLINE" | jq 'length' 2>/dev/null || echo "0")

  # 催促（15分経過 & 未催促）
  if [ "$ELAPSED" -ge "$NUDGE_AFTER" ] && [ "$NUDGED" = false ]; then
    NUDGED=true
    # 催促はメインエージェントに任せる（スクリプトからはステータスのみ通知）
  fi

  # ステータス判定（インラインコメントも終了条件に含める）
  STATUS="polling"
  if [ "$REVIEW_COUNT" -gt 0 ] || [ "$INLINE_COUNT" -gt 0 ]; then
    STATUS="reviews_received"
  elif [ "$ELAPSED" -ge "$TIMEOUT" ]; then
    STATUS="timeout"
  fi

  # 状況を書き出し
  cat > "$OUTPUT_FILE" <<EOJSON
{
  "status": "${STATUS}",
  "elapsed": ${ELAPSED},
  "review_count": ${REVIEW_COUNT},
  "inline_count": ${INLINE_COUNT},
  "nudged": ${NUDGED},
  "after_timestamp": "${AFTER_TIMESTAMP}",
  "reviews": ${REVIEWS},
  "inline_comments": ${INLINE},
  "issue_comments": ${ISSUE_COMMENTS}
}
EOJSON

  # 終了条件
  if [ "$STATUS" != "polling" ]; then
    exit 0
  fi
done
