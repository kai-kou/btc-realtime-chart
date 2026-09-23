#!/usr/bin/env bash
# fetch_broker_secrets.sh — secrets-broker Worker からキー束を取得し env ファイルに書き出す
#
# 提案 案A（Cloudflare ブローカー）のクライアント側。session-start.sh から「SECRETS_BROKER_URL が
# 設定されているときだけ」source される想定。未設定なら何もしない（＝既存 GitHub Variables 経路に無影響）。
#
# 使い方（session-start.sh への Phase 1 追加・1 行）:
#   [ -n "$SECRETS_BROKER_URL" ] && . "$(dirname "$0")/../tools/fetch_broker_secrets.sh"
#
# 必要な env（GitHub Variables にリポジトリごとに 1 個ずつだけ置く）:
#   SECRETS_BROKER_URL   例: https://<your-repo>-secrets-broker.<acct>.workers.dev/secrets
#   SECRETS_BROKER_TOKEN ブローカーの bearer トークン（= Worker 側 BROKER_AUTH_TOKEN と同値）
#
# 設計メモ:
#  - 既存 GitHub Variables 経路と「併存」する。ブローカー値を優先したい場合は先に source する。
#  - 既に環境にある変数は上書きしない（session-start.sh の既存ポリシーと一致）。
#  - CF WAF 1010 回避のため User-Agent を curl/8.5.0 に固定（L-084 / L-087 と同系統の既知対策）。

set -u

_broker_out="${1:-/tmp/broker_secrets.env}"

if [ -z "${SECRETS_BROKER_URL:-}" ] || [ -z "${SECRETS_BROKER_TOKEN:-}" ]; then
  return 0 2>/dev/null || exit 0
fi

# Worker からキー束 JSON を取得（10s タイムアウト・失敗時は既存 env を維持してフォールバック）
_broker_json="$(curl -fsS --max-time 10 \
  -H "Authorization: Bearer ${SECRETS_BROKER_TOKEN}" \
  -H "User-Agent: curl/8.5.0" \
  "${SECRETS_BROKER_URL}" 2>/dev/null)" || {
  echo "secrets-broker: fetch failed, keeping existing env" >&2
  return 0 2>/dev/null || exit 0
}

# JSON を「未設定の変数のみ」export 文に変換（値は対話的に stdout 表示しない・P-12 準拠）
: > "$_broker_out"
printf '%s' "$_broker_json" | python3 -c "
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for name, value in (data or {}).items():
    if os.environ.get(name):
        continue
    escaped = str(value).replace(\"'\", \"'\\\\''\")
    print(f\"export {name}='{escaped}'\")
" >> "$_broker_out" 2>/dev/null || true

if [ -s "$_broker_out" ]; then
  # shellcheck disable=SC1090
  . "$_broker_out"
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    cat "$_broker_out" >> "$CLAUDE_ENV_FILE"
  fi
  # キー数は export 文の数で数える（複数行の値=NOTE_COOKIES_JSON 等で wc -l は膨張するため不可）
  _n=$(grep -c '^export ' "$_broker_out" 2>/dev/null || echo 0)
  echo "secrets-broker: loaded ${_n} secret(s) from broker (values not printed)" >&2
fi
unset _broker_json _broker_out _n
return 0 2>/dev/null || exit 0
