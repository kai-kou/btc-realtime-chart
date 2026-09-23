#!/usr/bin/env bash
# setup_secrets_broker.sh — 案A（Cloudflare ブローカー）の Phase 0 自動化
#
# 現在の環境変数（GitHub Variables 由来）から「移行対象キー束」を作り、Cloudflare Secrets Store に
# 投入 → ブローカー Worker をデプロイ → リポジトリ用 bearer トークンを GitHub Variables に登録する。
# これ1本で Phase 0〜1 のセットアップが完了する。
#
# 前提（本環境に存在することを確認済み）:
#   CLOUDFLARE_API_TOKEN   Workers Scripts:Edit + Secrets Store:Edit 権限が必要
#   CLOUDFLARE_ACCOUNT_ID
#   npx（wrangler を npx 経由で実行）
#
# 使い方:
#   bash tools/setup_secrets_broker.sh --dry-run     # 投入対象キー名のみ表示（値は出さない・既定の安全確認）
#   bash tools/setup_secrets_broker.sh --apply       # 実際に store 作成・bundle 投入・deploy・Variables 登録
#
# 安全設計:
#   - --dry-run が既定。--apply 明示時のみ実アカウント操作を行う。
#   - bundle.json はメモリ/一時ファイルのみで扱い、終了時に必ず削除（平文をリポジトリに残さない）。
#   - 投入する値は一切 stdout に出さない（P-12 準拠・キー名のみ表示）。
#   - bootstrap（GH_TOKEN 等）と broker 自身の変数は移行対象から除外する。

set -euo pipefail

MODE="--dry-run"
[ "${1:-}" = "--apply" ] && MODE="--apply"

# --- リポジトリ自動検出（汎用化・#3482）---
# 優先順: env SECRETS_BROKER_REPO → env GITHUB_REPOSITORY → git remote 解析。
# クラウドプロキシ環境では `gh repo view` が使えないため git remote を直接解析する。
# どれも解決できない場合は **特定プロジェクトを既定にせず即エラー**（誤ったプロジェクトの
# Worker/bundle を上書きする事故を防ぐ）。
REPO="${SECRETS_BROKER_REPO:-${GITHUB_REPOSITORY:-}}"
if [ -z "$REPO" ]; then
  _url="$(git remote get-url origin 2>/dev/null || echo '')"
  REPO="$(printf '%s' "$_url" | sed -E 's#\.git$##' | sed -E 's#^.*[/:]([^/]+/[^/]+)$#\1#')"
fi
if [ -z "$REPO" ]; then
  echo "リポジトリを自動検出できませんでした。env SECRETS_BROKER_REPO か GITHUB_REPOSITORY を設定してください（誤ったプロジェクトの Worker 上書き防止）" >&2
  exit 1
fi
# SLUG（repo 名）から各リソース名を導出（元プロジェクトでは従来名と完全一致＝挙動不変）
SLUG="${REPO##*/}"
STORE_NAME="$SLUG"
SECRET_BUNDLE_NAME="${SLUG//-/_}_bundle"
SECRET_TOKEN_NAME="${SLUG//-/_}_broker_token"
WORKER_NAME="${SLUG}-secrets-broker"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
BROKER_DIR="${HERE}/infra/secrets-broker"

# 移行対象から除外する変数（bootstrap・broker 自身・非機密の運用フラグ）
EXCLUDE_REGEX='^(GH_TOKEN|SECRETS_BROKER_URL|SECRETS_BROKER_TOKEN|CLOUDFLARE_API_TOKEN|TZ|GIT_TERMINAL_PROMPT|HOOK_PROFILE|PATH|HOME|PWD|SHLVL|_)$'

# --- 増分追加モード（--add KEY [KEY...]）: 既存 broker bundle に指定キーだけ安全に上乗せする ---
# 移行完了後に新しい環境変数が増えたとき、bundle 全体を作り直すと現在の GitHub Variables の状態
# （生キーが *_PREMIGRATE へ rename 済み）を取り込んでしまい、broker のキー名が壊れる。
# そこで「現 broker bundle（正規名）を土台にし、指定キーだけ GitHub Variables 値で merge → 再投入」する。
# 値は一切 stdout に出さない（P-12 準拠・キー名のみ表示）。deploy はせず secret put のみ（worker.js 不変のため）。
if [ "${1:-}" = "--add" ]; then
  shift
  ADD_KEYS=("$@")
  [ "${#ADD_KEYS[@]}" -gt 0 ] || { echo "使い方: bash tools/setup_secrets_broker.sh --add KEY [KEY...]"; exit 1; }
  echo "== secrets-broker 増分追加: ${ADD_KEYS[*]} =="
  [ -n "${SECRETS_BROKER_URL:-}" ] && [ -n "${SECRETS_BROKER_TOKEN:-}" ] \
    || { echo "SECRETS_BROKER_URL / SECRETS_BROKER_TOKEN が必要（env か GitHub Variables）"; exit 1; }

  VARS_JSON="$(mktemp)"; CUR_JSON="$(mktemp)"; NEW_JSON="$(mktemp)"
  trap 'rm -f "$VARS_JSON" "$CUR_JSON" "$NEW_JSON"' EXIT
  gh variable list -R "${REPO}" --json name,value > "$VARS_JSON" 2>/dev/null || echo '[]' > "$VARS_JSON"
  gv() { python3 -c "import json,sys;print(next((v['value'] for v in json.load(open(sys.argv[1])) if v['name']==sys.argv[2]),''))" "$VARS_JSON" "$1"; }
  [ -n "${CLOUDFLARE_API_TOKEN:-}" ]  || CLOUDFLARE_API_TOKEN="$(gv CLOUDFLARE_API_TOKEN)"
  [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] || CLOUDFLARE_ACCOUNT_ID="$(gv CLOUDFLARE_ACCOUNT_ID)"
  [ -n "${CLOUDFLARE_API_TOKEN}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID}" ] \
    || { echo "CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID が必要（env か GitHub Variables）"; exit 1; }

  # 現 broker bundle（正規名）を土台に取得（失敗時は中止＝既存 secret を壊さない）
  curl -fsS --max-time 12 -H "Authorization: Bearer ${SECRETS_BROKER_TOKEN}" -H "User-Agent: curl/8.5.0" \
    "${SECRETS_BROKER_URL}" > "$CUR_JSON" 2>/dev/null \
    || { echo "broker から現 bundle を取得できない（中止）"; exit 1; }

  # 指定キーを GitHub Variables 値で merge（値は stdout 非表示・値が無いキーがあれば exit 2 で中止）
  python3 - "$CUR_JSON" "$VARS_JSON" "$NEW_JSON" "${ADD_KEYS[@]}" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))
varj = {v["name"]: v.get("value", "") for v in json.load(open(sys.argv[2]))}
out_path, keys = sys.argv[3], sys.argv[4:]
added, missing = [], []
for k in keys:
    if varj.get(k):
        cur[k] = varj[k]; added.append(k)
    else:
        missing.append(k)
json.dump(cur, open(out_path, "w"))
print(f"merge 後 bundle: {len(cur)} キー（追加/更新: {','.join(added) or 'なし'}）", file=sys.stderr)
if missing:
    print(f"GitHub Variables に値が無いため追加不可: {','.join(missing)}", file=sys.stderr)
    sys.exit(2)
PY

  WRANGLER="npx --yes wrangler@latest"
  export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID
  ( cd "${BROKER_DIR}"
    grep -v 'secrets_store_secrets\|store_id\|secret_name' wrangler.jsonc.example > wrangler.jsonc 2>/dev/null || cp wrangler.jsonc.example wrangler.jsonc
    sed -i -E "s/\"name\"[[:space:]]*:[[:space:]]*\"[^\"]*\"/\"name\": \"${WORKER_NAME}\"/" wrangler.jsonc  # 自動検出した Worker 名を注入（汎用化・別プロジェクトが他の Worker を上書きするのを防ぐ・#3482）
    B64="$(base64 -w0 "${NEW_JSON}")"
    CHUNK=4000; off=0; idx=0; len=${#B64}
    while [ "$off" -lt "$len" ]; do
      printf '%s' "${B64:$off:$CHUNK}" | ${WRANGLER} secret put "SECRET_BUNDLE_${idx}"
      off=$((off + CHUNK)); idx=$((idx + 1))
    done
    echo "  bundle を ${idx} チャンク（base64・各<5kB）で再投入（増分追加・値は非表示）" )
  echo "== 増分追加完了。確認: python3 tools/verify_broker_migration.py --gate =="
  exit 0
fi

echo "== secrets-broker setup (${MODE}) =="

# 1) 移行対象キー名を列挙（gh variable list を真実源にする・gh 認証のみで動作）
#    PRoot/Termux ではプロセス置換 <(...) が /dev/fd/63 で失敗するため一時ファイル経由にする。
#    また gh_vars.py は GH_TOKEN env を要求するが、gh auth login だけのクリーン端末では未設定の
#    ため空になる。gh variable list は gh のログイン資格情報で動くのでクラウド/端末の両方で機能する。
_NAMES_TMP="$(mktemp)"
gh variable list -R "${REPO}" --json name --jq '.[].name' 2>/dev/null \
  | grep -vE "${EXCLUDE_REGEX}" | grep -E '^[A-Z]' > "${_NAMES_TMP}" || true
mapfile -t NAMES < "${_NAMES_TMP}"
rm -f "${_NAMES_TMP}"
echo "移行対象キー: ${#NAMES[@]} 個"
printf '  - %s\n' "${NAMES[@]}"

if [ "$MODE" = "--dry-run" ]; then
  echo "（--dry-run のため実操作なし。--apply で実行）"
  exit 0
fi

# --- 以下 --apply 時のみ ---
# 値の真実源は GitHub Variables（name+value を一括取得）。これにより gh 認証だけで完結し、
# env に生キーが読み込まれていないクリーンな端末からでも飼い主が1コマンドで実行できる。
VARS_JSON="$(mktemp)"
BUNDLE_JSON="$(mktemp)"
trap 'rm -f "$VARS_JSON" "$BUNDLE_JSON"' EXIT
gh variable list -R "${REPO}" --json name,value > "$VARS_JSON" 2>/dev/null || echo '[]' > "$VARS_JSON"

# CF bootstrap は env になければ GitHub Variables から補完
gv() { python3 -c "import json,sys;print(next((v['value'] for v in json.load(open(sys.argv[1])) if v['name']==sys.argv[2]),''))" "$VARS_JSON" "$1"; }
[ -n "${CLOUDFLARE_API_TOKEN:-}" ]  || CLOUDFLARE_API_TOKEN="$(gv CLOUDFLARE_API_TOKEN)"
[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] || CLOUDFLARE_ACCOUNT_ID="$(gv CLOUDFLARE_ACCOUNT_ID)"
[ -n "${CLOUDFLARE_API_TOKEN}" ]  || { echo "CLOUDFLARE_API_TOKEN が必要（env か GitHub Variables）"; exit 1; }
[ -n "${CLOUDFLARE_ACCOUNT_ID}" ] || { echo "CLOUDFLARE_ACCOUNT_ID が必要（env か GitHub Variables）"; exit 1; }

# 2) bundle.json を構築（値は GitHub Variables 優先・env フォールバック・stdout 非表示）
python3 - "$VARS_JSON" "$BUNDLE_JSON" <<PY
import json, os, sys, re
exclude = re.compile(r"${EXCLUDE_REGEX}")
bundle = {}
for v in json.load(open(sys.argv[1])):
    n = v["name"]
    if exclude.match(n) or not re.match(r"^[A-Z]", n):
        continue
    bundle[n] = v.get("value") or os.environ.get(n, "")
with open(sys.argv[2], "w") as f:
    json.dump(bundle, f)
print(f"bundle: {len(bundle)} 個のキーを格納（値は非表示）", file=sys.stderr)
PY

WRANGLER="npx --yes wrangler@latest"
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID

# 本環境の CLOUDFLARE_API_TOKEN は Workers Scripts:Edit を保有（Secrets Store スコープは無し）。
# よって既定は「Worker Secrets 版」でデプロイする（即動く・Beta 非依存）。
BROKER_TOKEN="$(openssl rand -hex 32)"

# 3) Worker をデプロイ（Worker Secrets 版: wrangler.jsonc にバインディング不要）
echo "Worker をデプロイ中（Worker Secrets 版）..."
( cd "${BROKER_DIR}"
  grep -v 'secrets_store_secrets\|store_id\|secret_name' wrangler.jsonc.example > wrangler.jsonc 2>/dev/null || cp wrangler.jsonc.example wrangler.jsonc
  sed -i -E "s/\"name\"[[:space:]]*:[[:space:]]*\"[^\"]*\"/\"name\": \"${WORKER_NAME}\"/" wrangler.jsonc  # 自動検出した Worker 名を注入（汎用化・#3482）
  ${WRANGLER} deploy )

# 4) Worker Secrets を投入（値は stdin・stdout 非表示）
#    Worker Secret は1個あたり約5.1kB上限（code 10054）のため、束(JSON)を base64 化して
#    4000文字ごとのチャンク SECRET_BUNDLE_0, _1, ... に分割投入する（Worker 側で結合・復元）。
( cd "${BROKER_DIR}"
  B64="$(base64 -w0 "${BUNDLE_JSON}")"
  CHUNK=4000; off=0; idx=0; len=${#B64}
  while [ "$off" -lt "$len" ]; do
    printf '%s' "${B64:$off:$CHUNK}" | ${WRANGLER} secret put "SECRET_BUNDLE_${idx}"
    off=$((off + CHUNK)); idx=$((idx + 1))
  done
  echo "  bundle を ${idx} チャンク（base64・各<5kB）に分割投入"
  printf '%s' "${BROKER_TOKEN}" | ${WRANGLER} secret put BROKER_AUTH_TOKEN )

# 5) workers.dev サブドメインから URL を確定
SUBDOMAIN="$(curl -fsS --max-time 12 -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "User-Agent: curl/8.5.0" \
  "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/workers/subdomain" 2>/dev/null \
  | python3 -c "import json,sys;print((json.load(sys.stdin).get('result') or {}).get('subdomain',''))" || true)"
[ -n "$SUBDOMAIN" ] || SUBDOMAIN="${CLOUDFLARE_ACCOUNT_ID}"
WORKER_URL="https://${WORKER_NAME}.${SUBDOMAIN}.workers.dev/secrets"
echo "Worker デプロイ完了: ${WORKER_URL}"

# 6) GitHub Variables に bootstrap 2 個を登録
gh variable set SECRETS_BROKER_URL   -R "${REPO}" --body "${WORKER_URL}" >/dev/null
printf '%s' "${BROKER_TOKEN}" | gh variable set SECRETS_BROKER_TOKEN -R "${REPO}" >/dev/null

echo "== 完了: 次セッションからブローカー経由で取得（GitHub Variables と併存）=="
echo "動作確認: curl -fsS -H 'Authorization: Bearer <token>' -H 'User-Agent: curl/8.5.0' '${WORKER_URL}' | python3 -m json.tool"
echo "Phase 2〜3: 併存確認後、GitHub Variables から生キーを削除し bootstrap 2 個のみ残す"
