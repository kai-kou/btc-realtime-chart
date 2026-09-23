#!/usr/bin/env bash
# bootstrap.sh の marketplace.json 自己実行ガードの振る舞いテスト（Issue #44）
#
# 検証する不変条件:
#   1. git remote origin と marketplace.json の plugins[0].homepage が同じ owner/repo を
#      指す（＝配布元リポジトリ自身での再実行）→ 削除をスキップする
#   2. owner/repo が異なる（＝下流リポジトリ）→ 従来どおり削除する
#   3. トークン付き https origin（クラウド実行環境のプロキシ URL 形式）でも 1・2 の判定が崩れない
#   4. origin が無い（remote 未設定）→ 判定不能として安全側（削除）にフォールバックする
#
# 使い方: bash tools/test_bootstrap_marketplace_guard.sh
# 終了コード: 0 = 全 PASS / 1 = 1 件以上 FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="$REPO_ROOT/scripts/bootstrap.sh"
[ -f "$BOOTSTRAP" ] || { echo "FATAL: bootstrap.sh が見つかりません: $BOOTSTRAP"; exit 1; }

PASS=0
FAIL=0
report() { # report <結果 ok|ng> <説明>
  if [ "$1" = "ok" ]; then
    PASS=$((PASS + 1)); echo "  PASS: $2"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL: $2"
  fi
}

MARKETPLACE_JSON='{
  "name": "kai-kou-claude-base",
  "owner": {"name": "kai-kou", "url": "https://github.com/kai-kou"},
  "plugins": [{"name": "claude-code-base", "source": "./", "homepage": "https://github.com/kai-kou/claude-code-repository-base"}]
}'

# setup_case <origin_url>: bootstrap.sh 実行に必要な最小構成の一時リポジトリを作る
setup_case() {
  WORK="$(mktemp -d)"
  mkdir -p "$WORK/.claude-plugin" "$WORK/docs" "$WORK/.claude" "$WORK/tools" "$WORK/scripts"
  printf '%s' "$MARKETPLACE_JSON" > "$WORK/.claude-plugin/marketplace.json"
  cp "$BOOTSTRAP" "$WORK/scripts/bootstrap.sh"
  ( cd "$WORK" && git init --quiet )
  if [ -n "$1" ]; then
    ( cd "$WORK" && git remote add origin "$1" ) \
      || { echo "FATAL: git remote add に失敗しました（$1）"; exit 1; }
  fi
}

teardown_case() { rm -rf "$WORK"; }

run_bootstrap() { # run_bootstrap <--repo 値>：非ゼロ終了時は出力を stderr に残す（サイレント失敗の防止）
  local _out
  if ! _out="$( cd "$WORK" && bash scripts/bootstrap.sh --repo "$1" --name test 2>&1 )"; then
    echo "  WARN: bootstrap.sh が非ゼロ終了しました。以降の marketplace_exists 判定は無効: $_out" >&2
  fi
}

marketplace_exists() { [ -f "$WORK/.claude-plugin/marketplace.json" ]; }

echo "[ケース1] 配布元リポジトリ自身（origin と homepage が同じ owner/repo）→ 保持"
setup_case "https://github.com/kai-kou/claude-code-repository-base.git"
run_bootstrap "kai-kou/claude-code-repository-base"
marketplace_exists \
  && report ok "marketplace.json が保持された" \
  || report ng "配布元自身なのに marketplace.json が削除された"
teardown_case

echo "[ケース2] 下流リポジトリ（origin が別 owner/repo）→ 削除"
setup_case "git@github.com:someone/my-new-project.git"
run_bootstrap "someone/my-new-project"
marketplace_exists \
  && report ng "下流リポジトリなのに marketplace.json が残った" \
  || report ok "marketplace.json が削除された"
teardown_case

echo "[ケース3] トークン付き https origin（クラウド実行環境のプロキシ URL 形式）でも判定が崩れない"
setup_case "https://x-access-token:ABC123XYZ@github.com/kai-kou/claude-code-repository-base.git"
run_bootstrap "kai-kou/claude-code-repository-base"
marketplace_exists \
  && report ok "トークン付き origin でも配布元自身と正しく判定し保持した" \
  || report ng "トークン付き origin で誤って削除した"
teardown_case

setup_case "https://x-access-token:ABC123XYZ@github.com/someone/my-new-project.git"
run_bootstrap "someone/my-new-project"
marketplace_exists \
  && report ng "トークン付き origin の下流リポジトリなのに残った" \
  || report ok "トークン付き origin でも下流リポジトリと正しく判定し削除した"
teardown_case

echo "[ケース4] origin 未設定（判定不能）→ 安全側フォールバックで削除"
setup_case ""
run_bootstrap "someone/x"
marketplace_exists \
  && report ng "判定不能なのに marketplace.json が残った（安全側フォールバック違反）" \
  || report ok "判定不能時は安全側（削除）にフォールバックした"
teardown_case

echo
echo "結果: PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
