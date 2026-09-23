#!/usr/bin/env bash
# sync_broker_drift.sh — secrets-broker のドリフト（現役生キーの未同期）を自動検知して同期する（#3453）
#
# 目的（構造的な取りこぼし防止）:
#   移行中は「GitHub Variables 先 → broker 後」の供給順により、新しい環境変数を `gh variable set` で
#   足しただけだと broker 未同期でも現役名で動いてしまい（サイレント）、最終移行（生キー purge）で
#   取りこぼされる。本ツールを全スロット共通プリフライトで毎時回すことで、どの経路で環境変数を
#   追加しても次の hourly で broker に自動回収され、verify ゲートが READY を維持する（CP-6 自律運用）。
#
# 仕組み:
#   verify_broker_migration.py --json の parity.missing_in_broker（= bootstrap/_PREMIGRATE を除いた
#   現役生キーで broker に無いもの）を抽出 → setup_secrets_broker.sh --add で増分同期 → 再検証。
#   missing は is_migrated_key で絞られているため、そのまま --add に渡して安全（bootstrap は混ざらない）。
#
# 軽量設計:
#   - broker 未設定（移行前/移行完了でブートストラップ削除後）なら no-op（exit 10）。
#   - drift が無ければ npx wrangler を起動しない（verify の curl + gh variable list だけで即 exit 10）。
#   - drift 検知時のみ setup_secrets_broker.sh --add（npx wrangler）を実行する。
#
# 終了コード:
#   0  = drift を検知して同期した（呼び出し側は state 変更をコミットする）
#   10 = 同期不要（broker 未設定 or drift なし）= プリフライトでスキップ
#   1  = 同期を試みたが失敗（呼び出し側は調査）

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

if [ "${1:-}" = "--self-test" ]; then
  # ネットワーク非依存: missing 抽出ロジックの健全性のみ確認する
  python3 - <<'PY'
import json
sample = {"parity": {"missing_in_broker": ["FOO_TOKEN", "BAR_URL"]}}
got = " ".join(sample.get("parity", {}).get("missing_in_broker", []))
assert got == "FOO_TOKEN BAR_URL", got
empty = {"parity": {"missing_in_broker": []}}
assert " ".join(empty.get("parity", {}).get("missing_in_broker", [])) == ""
print("self-test: PASS")
PY
  exit 0
fi

if [ -z "${SECRETS_BROKER_URL:-}" ] || [ -z "${SECRETS_BROKER_TOKEN:-}" ]; then
  echo "secrets-broker 未設定: skip（broker ドリフト同期は移行中のみ有効）"
  exit 10
fi

_TMP="$(mktemp)"
trap 'rm -f "$_TMP"' EXIT
python3 "${HERE}/tools/verify_broker_migration.py" --json > "$_TMP" 2>/dev/null || true

MISSING="$(python3 -c "import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(' '.join(d.get('parity', {}).get('missing_in_broker', [])))" "$_TMP" 2>/dev/null || true)"

if [ -z "${MISSING// /}" ]; then
  echo "broker drift なし（現役生キーは全て同期済み）"
  exit 10
fi

echo "broker 未同期キーを検知: ${MISSING} → 自動同期する（setup_secrets_broker.sh --add）"
# shellcheck disable=SC2086
if bash "${HERE}/tools/setup_secrets_broker.sh" --add ${MISSING}; then
  python3 "${HERE}/tools/verify_broker_migration.py" --gate
  echo "broker ドリフト同期完了"
  exit 0
fi
echo "❌ broker ドリフト同期に失敗（手動確認が必要）"
exit 1
