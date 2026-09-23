#!/bin/bash
# SessionStart hook（汎用ベース）
# Claude Code on the web（リモート実行環境）でのみ動作する起動フック。
# 役割: env 伝搬・gh CLI 準備・GitHub Variables ロード・作業ツリー整備・
#        依存インストール・ルール同期・プロジェクト状態注入・PR レビュー復帰チェック。
#
# プロジェクト固有のセットアップ（DB 起動・ツールチェーン導入・サービス起動等）は
# 末尾の「プロジェクト固有セットアップ」セクションに追記する。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# --- SessionStart 起動種別（source: startup/resume/clear/compact）---
# 公式仕様（https://code.claude.com/docs/en/hooks）で input JSON に source フィールドがある。
# 破壊的なワーキングツリークリーンアップを startup のみに限定するために読む（Issue #248）。
# stop-router.sh と同じ `cat` イディオムで stdin JSON を取得する（フック起動時は必ず piped）。
# 手動実行（端末から `CLAUDE_CODE_REMOTE=true bash session-start.sh`）で stdin が TTY のときは
# `cat` が入力待ちでハングするため、非 TTY（パイプ経由）のときだけ読む（Gemini 指摘・PR #251）。
HOOK_INPUT=""
if [ ! -t 0 ]; then
  HOOK_INPUT=$(cat 2>/dev/null || true)
fi
HOOK_SOURCE=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('source', '') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")
echo "[session-start] source=${HOOK_SOURCE:-unknown}" >&2
unset HOOK_INPUT

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
# 対象リポジトリ（bootstrap.sh が kai-kou/btc-realtime-chart を置換、または PROJECT_REPO で上書き）
REPO="${PROJECT_REPO:-kai-kou/btc-realtime-chart}"

# --- CLAUDE_ENV_FILE 肥大化防止（E2BIG 対策）---
# 本フックは毎セッション全 env を再構築して CLAUDE_ENV_FILE に追記する。truncate しないと
# resume の度に env が重複追記され、数千行に肥大化して全 bash が E2BIG で失敗する。
if [ -n "${CLAUDE_ENV_FILE:-}" ] && [ -f "${CLAUDE_ENV_FILE:-/dev/null}" ]; then
  : > "$CLAUDE_ENV_FILE" || echo "Warning: failed to truncate CLAUDE_ENV_FILE" >&2
fi

env_persist() {
  # $1 = "export NAME=value" 形式の行を CLAUDE_ENV_FILE へ書き出す
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "$1" >> "$CLAUDE_ENV_FILE" || echo "Warning: failed to write to CLAUDE_ENV_FILE" >&2
  fi
}

# --- gh シム有効化（クラウド 403 排除・Issue #254）---
# .claude/bin/gh（tools/gh_shim.py ラッパー）を PATH 先頭に注入し、GraphQL 依存の gh 高レベル
# コマンドを repo スコープ REST へ透過変換する。ローカルでは実 gh へ即 exec するため挙動不変。
# GH_SHIM=off で無効化できる（tools/gh_shim.py ヘッダ参照）。
_shim_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}/.claude/bin"
if [ -x "${_shim_dir}/gh" ]; then
  case ":${PATH}:" in
    *":${_shim_dir}:"*) ;;
    *) export PATH="${_shim_dir}:${PATH}" ;;
  esac
  env_persist "export PATH=\"${_shim_dir}:\${PATH}\""
  echo "[gh-shim] enabled: ${_shim_dir}/gh (GH_SHIM=off で無効化可)" >&2
fi

# --- タイムゾーン（日時は JST 統一が既定・datetime-rules.md。PROJECT_TZ で上書き可）---
# 表示・記録系の日時を JST に揃えるため、未指定なら Asia/Tokyo を既定にする。
# 機械処理用 UTC（GitHub API の after_timestamp 等）は各所で date -u を明示しており影響しない。
export TZ="${PROJECT_TZ:-Asia/Tokyo}"
env_persist "export TZ=${TZ}"
echo "[timezone] TZ=${TZ}: $(date '+%Y-%m-%d %H:%M %Z')" >&2

# --- git の credential プロンプト無効化（ハング防止）---
export GIT_TERMINAL_PROMPT=0
env_persist "export GIT_TERMINAL_PROMPT=0"

# --- gh CLI（クラウドでは導入しない・L-114 / Issue #342）---
# クラウド実行環境は公式仕様として gh をプリインストールしない。apt で導入すること自体は可能だが、
# repo スコープ REST が 403（リポジトリの API attach 不足）のため導入しても GitHub 操作はできない
# （2026-07-26 実測・#338）。毎セッション apt を叩く時間を捨てるだけなので試行しない。
# クラウドの GitHub 操作は mcp__github__* が一次経路（SSOT: docs/rules/github-mcp-fallback-patterns.md）。
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  if command -v gh &>/dev/null && [ "$(command -v gh)" != "${_shim_dir}/gh" ]; then
    echo "gh CLI: 実 gh を検出（クラウドでは repo スコープ操作が 403 になる想定。GitHub 操作は mcp__github__* を使う）" >&2
  else
    echo "gh CLI: クラウドでは未導入が既定（導入しない。GitHub 操作は mcp__github__* を使う・L-114）" >&2
  fi
elif ! command -v gh &>/dev/null; then
  # ローカル実行のみ: gh があると tools/*.py が高速に動くため導入を試みる
  echo "Installing gh CLI via apt (universe)..." >&2
  (apt-get update -qq && apt-get install -y -qq gh) >/dev/null 2>&1 || {
    GH_LATEST=$(curl -fsSL --max-time 15 "https://api.github.com/repos/cli/cli/releases/latest" 2>/dev/null | jq -r '.tag_name // empty' | sed 's/^v//' || true)
    if [ -n "${GH_LATEST:-}" ]; then
      curl -fsSL --max-time 60 "https://github.com/cli/cli/releases/download/v${GH_LATEST}/gh_${GH_LATEST}_linux_amd64.deb" -o /tmp/gh.deb \
        && apt-get install -y -qq /tmp/gh.deb >/dev/null 2>&1
      rm -f /tmp/gh.deb
    fi
  }
  command -v gh &>/dev/null && echo "gh CLI installed" >&2 || echo "Warning: gh CLI installation failed（ローカル実行。MCP 経路は影響なし）" >&2
else
  echo "gh CLI: already installed" >&2
fi

# --- gh CLI 認証（GH_TOKEN が未設定なら settings.local.json から読む）---
if [ -z "${GH_TOKEN:-}" ]; then
  SETTINGS_LOCAL="${PROJECT_DIR}/.claude/settings.local.json"
  if [ -f "$SETTINGS_LOCAL" ] && command -v jq &>/dev/null; then
    _token=$(jq -r '.env.GH_TOKEN // empty' "$SETTINGS_LOCAL" 2>/dev/null || true)
    if [ -n "$_token" ]; then
      export GH_TOKEN="$_token"
      if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
        # printf %q でシェル安全にエスケープ（トークンに ' 等が含まれても env ファイルが壊れない）
        ( set +x; printf 'export GH_TOKEN=%q\n' "$_token" ) >> "$CLAUDE_ENV_FILE"
      fi
    fi
    unset _token
  fi
fi
# クラウドでは GH_TOKEN 未設定が正常（コンテナが proxy-injected プレースホルダを入れ、
# プロキシが送信時に実認証情報へ差し替える・公式仕様）。未設定を警告しない。
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  if [ "${GH_TOKEN:-}" = "proxy-injected" ]; then
    echo "GitHub 認証: プロキシ注入モード（GH_TOKEN=proxy-injected・正常）" >&2
  elif [ -n "${GH_TOKEN:-}" ]; then
    echo "GitHub 認証: 環境設定の GH_TOKEN がパススルー（repo API の可否は attach 状態に依存・L-114）" >&2
  else
    echo "GitHub 認証: GH_TOKEN 未設定（クラウドでは正常。MCP 経路は影響を受けない）" >&2
  fi
elif [ -n "${GH_TOKEN:-}" ]; then
  echo "gh CLI: authenticated ($(timeout 10s gh api user --jq '.login' 2>/dev/null || echo 'token present'))" >&2
else
  echo "Warning: GH_TOKEN not set. gh CLI will be unauthenticated." >&2
fi

# --- GitHub Repository Variables から環境変数を自動ロード ---
# GH_TOKEN のみ Claude.ai 環境変数に設定すれば、他の env は
# GitHub Repository Variables で一元管理できる。既存 env は上書きしない（Claude.ai 側を優先）。
#
# 取得経路は2系統（Issue #40）。ゲートは GH_TOKEN のみ（gh CLI の有無に依存しない）:
#   ① gh CLI（あれば高速）  ② tools/gh_vars.py（urllib・トークンのみ・gh 不要）
# 🔴 クラウド（CLAUDE_CODE_REMOTE=true）では 2026-07-02 以降 egress プロキシが actions/variables
# パスを 403 ブロックし ①② とも必ず失敗する（Issue #133 / #342・github-mcp-fallback-patterns.md §2.4）。
# MCP にも variables の等価ツールが無いため代替経路も存在しない。したがってクラウドでは
# 試行自体をスキップし、env は Claude.ai 環境設定 / secrets-broker（後段ブロック）で供給する。
# 本ブロックはローカル実行専用として維持する。
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  echo "[env] GitHub Variables ロードはクラウドではスキップ（プロキシが 403・env は Claude.ai 環境設定 / secrets-broker から供給・L-114）" >&2
elif [ -n "${GH_TOKEN:-}" ]; then
  _env_file="/tmp/github_variables.env"
  _loaded_via=""
  _gh_ok=false
  # ① gh CLI 経路（インストール済みのときだけ試す）
  if command -v gh &>/dev/null; then
    _vars_json=$(timeout 10s gh variable list -R "$REPO" --json name,value 2>/dev/null) && _gh_ok=true
    if [ "$_gh_ok" = true ] && [ -n "$_vars_json" ] && [ "$_vars_json" != "[]" ]; then
      printf '%s\n' "$_vars_json" | python3 -c "
import json, sys, os
for v in json.load(sys.stdin):
    name, value = v['name'], v['value']
    if not os.environ.get(name):
        escaped = value.replace(\"'\", \"'\\\"'\\\"'\")
        print(f\"export {name}='{escaped}'\")
" 2>/dev/null > "$_env_file" || true
      [ -s "$_env_file" ] && _loaded_via="gh"
    fi
    unset _vars_json
  fi
  # ② gh_vars.py フォールバック（gh 不在・失敗時のみ。urllib・トークンのみで gh 不要）。
  # gh が成功していれば（0 件・全件既設定でも）フォールバックしない（冗長 API 呼び出し回避）。
  if [ "$_gh_ok" = false ] && [ -z "$_loaded_via" ] && [ -f "${PROJECT_DIR}/tools/gh_vars.py" ]; then
    GHV_PROJECT_DIR="$PROJECT_DIR" GHV_REPO="$REPO" timeout 15s python3 -c "
import os, sys
sys.path.insert(0, os.environ['GHV_PROJECT_DIR'])
from tools.gh_vars import get_all_variables
for name, value in get_all_variables(repo=os.environ['GHV_REPO']).items():
    if not os.environ.get(name):
        escaped = value.replace(\"'\", \"'\\\"'\\\"'\")
        print(f\"export {name}='{escaped}'\")
" 2>/dev/null > "$_env_file" || true
    [ -s "$_env_file" ] && _loaded_via="gh_vars.py"
  fi

  if [ -n "$_loaded_via" ] && [ -s "$_env_file" ]; then
    . "$_env_file"
    [ -n "${CLAUDE_ENV_FILE:-}" ] && cat "$_env_file" >> "$CLAUDE_ENV_FILE"
    # ~/.bashrc 先頭への source 行追記（CLAUDE_ENV_FILE 未提供の resume でも伝搬）
    _bashrc="${HOME}/.bashrc"
    _marker="# github-variables-autoload"
    if [ -f "$_bashrc" ] && ! grep -qF "$_marker" "$_bashrc" 2>/dev/null; then
      _tmprc=$(mktemp)
      { echo "${_marker}"; echo "[ -f ${_env_file} ] && . ${_env_file}"; echo ""; cat "$_bashrc"; } > "$_tmprc"
      mv "$_tmprc" "$_bashrc"
    fi
    echo "GitHub Variables: loaded $(wc -l < "$_env_file") var(s) via ${_loaded_via}" >&2
  else
    : > "$_env_file"
    echo "GitHub Variables: none loaded (gh/gh_vars.py 双方とも取得不可 or 0 件 in ${REPO})" >&2
  fi
  unset _env_file _bashrc _marker _tmprc _loaded_via _gh_ok
fi

# --- secrets-broker（案A）: 設定時のみブローカーからキー束を取得 ---
# GitHub Variables 平文保存を脱却するための Cloudflare ブローカー経路。
# SECRETS_BROKER_URL / SECRETS_BROKER_TOKEN が両方設定されているときだけ有効化し、
# 未設定なら何もしない（＝既存 GitHub Variables 経路に無影響）。
# 詳細: infra/secrets-broker/README.md
if [ -n "${SECRETS_BROKER_URL:-}" ] && [ -n "${SECRETS_BROKER_TOKEN:-}" ]; then
  _self="${BASH_SOURCE[0]:-$0}"
  _root="$(cd "$(dirname "$_self")/../.." 2>/dev/null && pwd)"
  _broker_client="${_root}/tools/fetch_broker_secrets.sh"
  if [ -f "$_broker_client" ]; then
    # shellcheck disable=SC1090
    . "$_broker_client" "/tmp/broker_secrets.env"
  fi
  unset _self _root _broker_client
fi

# --- HOOK_PROFILE: フック強度のランタイム制御 ---
export HOOK_PROFILE="${HOOK_PROFILE:-standard}"
env_persist "export HOOK_PROFILE='${HOOK_PROFILE}'"

# --- ワーキングディレクトリのクリーンアップ ---
# 前セッションの残留（別ブランチでマージ済みの変更）が未コミットファイルとして残り、
# 次セッションのブランチが古い起点で作られるのを防ぐ。
# headless（claude -p）起動時はスキップ（cwd の未コミット変更を壊さないため）。
# 保持したいパスは CLEANUP_KEEP_GLOBS（スペース区切り）で指定する。
# 破壊的クリーンアップ（reset/checkout/clean）は source=startup のときのみ実行する。
# startup は新規クローン直後で失う未コミット変更が存在しないため安全。resume/compact/clear
# では未コミット作業（Stop フックが走らず残った変更等）を消さない（Issue #248・L-100 ①）。
# 「新規セッション=失うものが無い」は本ベース自身のクラウド実行前提（毎回使い捨てクローン）
# にのみ成り立つ。プラグインとして配布した場合、CLAUDE_PROJECT_DIR/cwd は消費先の第三者
# プロジェクトを指すため（実測確認済み・#539）、そこに実 WIP があっても無条件に発火し破壊
# しうる。CLAUDE_PLUGIN_ROOT はこのスクリプトがプロジェクトローカルなコピー（apply-base
# 経由で自リポジトリの .claude/hooks/ に置かれた場合）ではなく、プラグイン配布物として
# 実行されているときにハーネスが設定することを実測確認済みのため、これをゲートに使う（#539）。
# 値そのものは検証しない（絶対パス形式のみ確認）。ハーネスが設定する他の制御変数
# （CLAUDE_CODE_ENTRYPOINT・HOOK_SOURCE 等）も同様に無検証で信頼しており、プロセス環境を
# 汚染できる主体を想定した完全性検証（既知プラグインディレクトリの allowlist 等）は、
# その allowlist 自体の正本となる配布規約がまだ存在しないため時期尚早と判断した（#539 レビュー）。
_is_plugin_dist=false
case "${CLAUDE_PLUGIN_ROOT:-}" in
  /*) _is_plugin_dist=true ;;
esac
_skip_cleanup=false
if [ "${CLAUDE_HOOK_SKIP_CLEANUP:-}" = "true" ] \
   || [ "${CLAUDE_CODE_ENTRYPOINT:-}" = "cli" ] || [ "${CLAUDE_CODE_ENTRYPOINT:-}" = "headless" ] \
   || [ "$_is_plugin_dist" = true ]; then
  _skip_cleanup=true
elif [ -n "${HOOK_SOURCE:-}" ] && [ "${HOOK_SOURCE}" != "startup" ]; then
  # source が取得できて startup 以外（resume/compact/clear）なら破壊的クリーンアップをスキップ。
  # source 不明（空）のときは従来どおり実行（後方互換・安全側は startup 相当）。
  _skip_cleanup=true
fi
if git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  if [ "$_skip_cleanup" = true ]; then
    echo "[cleanup] SKIP（headless / 明示スキップ / plugin配布 / source=${HOOK_SOURCE:-?}≠startup）" >&2
  else
    _untracked_count=$(git -C "$PROJECT_DIR" ls-files --others --exclude-standard | wc -l)
    _has_changes=false
    if ! git -C "$PROJECT_DIR" diff --quiet 2>/dev/null || ! git -C "$PROJECT_DIR" diff --cached --quiet 2>/dev/null; then
      _has_changes=true
    fi
    if [ "$_has_changes" = true ] || [ "$_untracked_count" -gt 0 ]; then
      git -C "$PROJECT_DIR" reset HEAD -- . >/dev/null 2>&1 || true
      GIT_LFS_SKIP_SMUDGE=1 git -C "$PROJECT_DIR" checkout -- . 2>/dev/null || true
      # CLEANUP_KEEP_GLOBS を -e 引数列に展開
      _keep_args=()
      for _g in ${CLEANUP_KEEP_GLOBS:-}; do _keep_args+=( -e "$_g" ); done
      git -C "$PROJECT_DIR" clean -fd "${_keep_args[@]}" 2>/dev/null || true
      echo "[cleanup] completed (${_untracked_count} untracked)" >&2
    fi
    unset _untracked_count _has_changes _keep_args _g
  fi

  # main（デフォルトブランチ）の最新化は全 source で実行（破壊的でない・checkout はしない）。
  # 明示 refspec で remote-tracking ref（origin/<default>）を確実に更新する（Issue #78）。
  # 素の `git fetch origin <branch>` は構成次第で origin/<branch> が古いまま残り、
  # 新ブランチを切り直す際に merge-base がズレて二重 diff の原因になる。
  _default_branch=$(git -C "$PROJECT_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
  git -C "$PROJECT_DIR" fetch origin "+${_default_branch}:refs/remotes/origin/${_default_branch}" --quiet 2>/dev/null \
    && echo "origin/${_default_branch}: fetched (refspec)" >&2 \
    || echo "⚠ Failed to fetch origin/${_default_branch}." >&2
  unset _default_branch
fi
unset _skip_cleanup

# --- Python 依存（requirements.txt があれば）---
if [ -f "${PROJECT_DIR}/requirements.txt" ]; then
  echo "Installing Python dependencies..." >&2
  python3 -m pip install -r "${PROJECT_DIR}/requirements.txt" --quiet --disable-pip-version-check \
    && echo "Python dependencies: OK" >&2 \
    || echo "Warning: pip install failed." >&2
fi

# --- Node 依存（package.json があれば）---
if [ -f "${PROJECT_DIR}/package.json" ]; then
  echo "Installing Node dependencies..." >&2
  npm install --prefix "$PROJECT_DIR" --silent 2>/dev/null \
    && echo "Node dependencies: OK" >&2 \
    || echo "Warning: npm install failed." >&2
fi

# --- ルール同期チェック（docs/rules/ と .claude/rules/ の symlink 同期）---
if [[ -x "${PROJECT_DIR}/tools/check_rules_sync.sh" ]]; then
  sync_output=$("${PROJECT_DIR}/tools/check_rules_sync.sh" --fix 2>&1 || true)
  echo "$sync_output" | grep -qE "FIXED|NG" && echo "[rules-sync] $sync_output" >&2 || echo "[rules-sync] OK" >&2
fi

# --- 言語ルールリマインダー（stdout → Claude コンテキストに注入）---
# コンテキスト肥大化・圧縮後の英語切り替えを防止するため毎セッション注入する。
# 口調を変えたいプロジェクトは本行を書き換える（または削除する）。
#
# 注: SessionStart は plain stdout がそのまま Claude のコンテキストに注入される公式イベント
# （UserPromptSubmit / UserPromptExpansion と並ぶ例外・E-D #19 で裏取り・
# https://code.claude.com/docs/en/hooks）。コンテキスト注入のみの用途なら JSON
# （hookSpecificOutput.additionalContext）を組み立てず printf 直書きでよい（公式推奨）。
printf '\n🔴 【応答言語ルール（最重要・常に有効）】日本語・ねこキャラ（語尾「にゃ」適度に）で応答すること。コンテキストが大きくなっても・英語コードが多くても英語に切り替えてはならない。\n\n'

# --- プロジェクト状態スナップショット注入 ---
# tools/generate_project_context.py が content/context/project_state.md を生成し、
# stdout 出力でセッションコンテキストに注入する（現状把握コストを削減）。
_ctx_file="${PROJECT_DIR}/content/context/project_state.md"
_ctx_gen="${PROJECT_DIR}/tools/generate_project_context.py"
# クラウドの render_remote はトークン不要（gh 依存セクションを生成しない・Issue #249）。
# GH_TOKEN 必須ゲートに縛ると、トークン未供給のクラウドセッションで軽量スナップショットすら
# 再生成されなくなるため、remote では token 無しでも再生成する（ローカルは従来どおり要 token）。
if [ -f "$_ctx_gen" ] && { [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || [ -n "${GH_TOKEN:-}" ]; }; then
  # 24h より古ければ再生成
  _stale=true
  if [ -f "$_ctx_file" ]; then
    # GNU stat（Linux）優先・BSD/macOS stat（-f %m）へフォールバック
    _mtime=$(stat -c %Y "$_ctx_file" 2>/dev/null || stat -f %m "$_ctx_file" 2>/dev/null || echo 0)
    _age=$(( $(date +%s) - _mtime ))
    [ "$_age" -lt 86400 ] && _stale=false
  fi
  [ "$_stale" = true ] && timeout 30s python3 "$_ctx_gen" >/dev/null 2>&1 || true
  unset _stale _mtime _age
fi
if [ -f "$_ctx_file" ]; then
  printf '\n'; cat "$_ctx_file"; printf '\n'
fi
unset _ctx_file _ctx_gen

# --- 公開リポジトリ反映レーンの回収指示（#449）---
# 反映（push）は add_repo が使えるインタラクティブセッションでしか完遂できない（L-117）。
# 一方 main へのマージはその多くが scheduled trigger セッション（R-1）で起きるため、
# 「マージしたセッションが反映する」だけでは滞留が残る。そこで **インタラクティブセッションの
# 冒頭** を回収点として明示する。ここでドリフト判定そのものは走らせない（公開側の clone を伴い
# 毎セッション数秒〜数十秒かかるため）。判定はセッション側が最初のツール呼び出しで行う。
if [ -f "${PROJECT_DIR}/tools/check_publish_drift.py" ]; then
  printf '\n📦 【公開リポジトリ反映レーン】作業の早い段階で `python3 tools/check_publish_drift.py --quiet` を実行し、終了コードが 1（ドリフトあり）または 2（判定不能）のときは `publish-sync` スキルで反映まで完遂すること（ユーザー確認は不要）。🔴 判定は `add_repo` の可否ではなく **配布物がこのセッションに供給されているかの実測**（`ls`）で行う（供給は発火ごとに変わる・`docs/routines.md` §5.1.5）。供給されていない発火では push を試さず `[publish-sync]` Issue への記録までで止める。\n\n'
fi

# --- PR レビュー待機状態チェック（セッションタイムアウト復帰・CP-4 対策）---
# ⚠ 本スクリプトは冒頭で CLAUDE_CODE_REMOTE=true のときだけ本体を実行する。クラウドでは
# check_pending_pr_reviews.py が依存する gh 呼び出しが構造的に必ず失敗する（Issue #249）。
# 失敗の具体的な機序はセッション種別で異なる: インタラクティブ/GitHub Issue-PR トリガーでは
# repo スコープ REST が 403（L-114）、**scheduled trigger（R-1）では CLAUDE_ENV_FILE が未設定で
# 上記の gh シム PATH 注入が persist せず `gh` 自体が command not found になる**
# （2026-09-14 実測・L-133・Issue #656）。いずれの機序でも毎回確実に失敗するため、
# 毎セッション「取得失敗」警告 1 行を注入するだけの無情報ノイズになり、ランタイム試行は行わない。
# ready_to_merge PR の復帰回収は MCP 経由がセッションの責務（pr-review-flow-summary.md）:
#   mcp__github__list_pull_requests で確認 → check_pending_pr_reviews.py は MCP 未対応の
#   ローカル実行時のプリフライト用途に限定する。現状把握の 1 行ポインタは
#   generate_project_context.py（クラウドモード）のスナップショットが担う。

# ======================================================================
# プロジェクト固有セットアップ（必要に応じて追記する）
#   例: DB 起動 / 言語ランタイム導入 / ローカルサービス起動 / MCP 疎通確認 など
# ======================================================================

echo "Session start hook completed." >&2
