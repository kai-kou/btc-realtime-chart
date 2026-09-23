#!/usr/bin/env bash
# orchestrator-directive.sh
# ユーザーがプロンプトを送信した直後に実行されるフック（UserPromptSubmit イベント・3 番目）。
#
# 役割（Issue #184）:
#   セッションが高コストモデル（Opus / Fable 系）で動作しているとき、
#   「オーケストレーターとして専門チームを組成してタスクを遂行せよ」というディレクティブを
#   コンテキストへ自動注入する。ユーザーが毎回同じ指示を書き足す手間をハーネスで恒久解消する。
#
# モデル検出方式（実機検証済み・2026-07-11）:
#   UserPromptSubmit の stdin JSON に model フィールドは含まれない（公式仕様）。
#   代わりに .transcript_path の JSONL を末尾から読み、最後の assistant エントリの
#   message.model を現在モデルとして採用する。この値はセッション途中の /model 変更に追随する
#   （sonnet-5 → fable-5 の切り替えが transcript 上で確認済み）。
#   セッション最初のプロンプト（assistant エントリ未生成）では .claude/settings.json の
#   model 既定値へフォールバックする。
#
# 既知の制限（レビュー #185 記録）:
#   - settings.json の model は「プロジェクト既定」であり実セッションの起動時選択モデルではない。
#     Opus/Fable を起動時に選んだセッションの最初の 1 プロンプトは検出不能のため注入されない
#     （2 ターン目以降は transcript から確実に追随する）。
#   - 末尾 512KB 窓に assistant エントリが 1 つも入らない稀なケース（巨大 tool_result 連続直後）
#     では検出をスキップする（安全側 = 無注入）。
#
# 設計方針:
#   - 非ブロッキング（常に exit 0）。検出失敗時は安全側（無注入）に倒す。
#   - guard.sh / prompt-structuring.sh と同じ stdout 注入経路・同じフォールバック順
#     （jq → python3 → 最小手段）を踏襲する。
#   - guard.sh が高リスクパターン（main 直 push・.env 等）を検出するケースでは本フックの注入も
#     抑制し、guard の助言バナーだけを残す（prompt-structuring.sh と同じ二重バナー防止方針）。
#   - 注入本文は agent-team-summary.md「2 協調モードと振り分け」（SSOT）と整合させる。
#
# 環境変数（トグル）:
#   CLAUDE_ORCHESTRATOR_DIRECTIVE = auto（既定・高コストモデル検出時のみ注入）
#                                 | off（無効）
#                                 | always（モデルに関係なく注入・検証用）
#   CLAUDE_HIGH_COST_MODEL_RE     = 高コスト判定の正規表現（既定 'opus|fable'・小文字比較）

set -euo pipefail

_mode="${CLAUDE_ORCHESTRATOR_DIRECTIVE:-auto}"
[ "$_mode" = "off" ] && exit 0

_hc_re="${CLAUDE_HIGH_COST_MODEL_RE:-opus|fable}"
_hc_default='opus|fable'
# 上書き値の妥当性検証（prompt-structuring.sh の _max_len 方式と同思想・レビュー #185 指摘）:
# ① 壊れた regex（grep exit 2）→ 既定へ戻す（サイレント無効化・stderr 汚染の防止）
# ② 過剰マッチ regex（'opus|' の打ち間違いや '.*' 等、全文字列にマッチ）→ 既定へ戻す
#    （低コストモデルにまで毎プロンプト注入される事故の防止。番兵文字列で検出する）
_rc=0
printf 'claude-probe' | grep -qE "$_hc_re" 2>/dev/null || _rc=$?
if [ "$_rc" -eq 2 ]; then
  _hc_re="$_hc_default"
elif printf '__lowcost_sentinel__' | grep -qE "$_hc_re" 2>/dev/null; then
  _hc_re="$_hc_default"
fi

_input="$(cat 2>/dev/null || true)"

# .prompt / .transcript_path を 1 プロセスでまとめて取り出す（jq → python3 フォールバック・
# 毎プロンプト実行のためプロセス起動回数を最小化）。値はタブ区切り 2 行にせず NUL 安全を
# 優先して改行区切り 2 行で受ける（transcript_path に改行は入らない前提・公式仕様のパス値）。
_prompt=""
_transcript=""
if command -v jq >/dev/null 2>&1; then
  _fields="$(printf '%s' "$_input" | jq -r '(.transcript_path // ""), (.prompt // "")' 2>/dev/null || true)"
else
  _fields="$(printf '%s' "$_input" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("transcript_path","")); print(d.get("prompt",""))' 2>/dev/null || true)"
fi
_transcript="$(printf '%s\n' "$_fields" | head -1)"
_prompt="$(printf '%s\n' "$_fields" | tail -n +2)"
[ -z "$_prompt" ] && exit 0

_trimmed="$(printf '%s' "$_prompt" | sed -e 's/^[[:space:]]*//')"
[ -z "$_trimmed" ] && exit 0

# --- スキップ条件（prompt-structuring.sh と同方針）--------------------------------
# スラッシュコマンド・明示オプトアウト・ハーネス注入の非ユーザー入力には発火しない。
# 🔁 同期義務: 下記の既知タグ・埋め込みマーカーのリストは prompt-structuring.sh の
#    スキップ条件と逐語同期を保つこと（片方だけ更新する L-094 型 desync 禁止）。
case "$_trimmed" in
  /*|'!'*) exit 0 ;;
  '<system-reminder'*|'<task-notification'*|'<local-command'*|'<github-webhook-activity'*|'[SYSTEM NOTIFICATION'*) exit 0 ;;
esac
if printf '%s' "$_prompt" | grep -qF -e '[SYSTEM NOTIFICATION' \
     -e '<task-notification>' -e 'Stop hook feedback' \
     -e '<system-reminder>' -e '<local-command' -e '<github-webhook-activity'; then
  exit 0
fi

# --- 高リスクパターン検出時は注入を抑制（guard.sh に一元化・二重バナー防止）---------
# user-prompt-submit-guard.sh が助言バナーを出すケースでは、本フックの注入も出さない
# （prompt-structuring.sh と同方針）。
_lc="$(printf '%s' "$_prompt" | tr '[:upper:]' '[:lower:]')"
if printf '%s' "$_lc" | grep -qE \
   'push[^a-z]+(origin[^a-z]+)?(main|master)([^a-z]|$)|--force([^a-z].*)?(main|master)([^a-z]|$)|force[-_. ]?push|rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]+(/|~|\$home|\*)|git[[:space:]]+clean[[:space:]]+-[a-z]*f|\.env($|[^a-z])|secret|credential|api[_-]?key|private[_-]?key|no-verify|settings\.local\.json'; then
  exit 0
fi

# --- 現在モデルの検出 --------------------------------------------------------------
_model=""

# 1) transcript JSONL の最後の assistant エントリの message.model（/model 変更に追随）。
#    巨大 transcript に備え末尾 512KB のみ読む。文中に "model":"..." が現れる
#    tool_result テキストを誤検出しないよう、type=assistant の構造化フィールドだけを見る。
if [ -n "$_transcript" ] && [ -f "$_transcript" ] && command -v python3 >/dev/null 2>&1; then
  _model="$(python3 - "$_transcript" <<'PYEOF' 2>/dev/null || true
import json, os, sys
path = sys.argv[1]
TAIL = 512 * 1024
model = ""
with open(path, "rb") as f:
    size = os.fstat(f.fileno()).st_size
    f.seek(max(0, size - TAIL))
    data = f.read().decode("utf-8", errors="replace")
for line in data.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except Exception:
        continue  # tail 先頭の途中行は捨てる
    if e.get("type") == "assistant":
        m = (e.get("message") or {}).get("model")
        if m:
            model = m
print(model)
PYEOF
)"
fi
# 注: jq による transcript 解析フォールバックは置かない（レビュー #185 指摘）。
#     tail -c は高確率で JSONL 行の途中から始まり、-R + fromjson? なしの jq は最初の
#     パースエラーでストリーム全体を中断するため実質機能しない死にコードだった（YAGNI）。
#     python3 は本リポジトリの必須前提（tools/*.py を全ワークフローで実行）のため単独で足りる。

# 2) フォールバック: settings.json の既定モデル（セッション最初のプロンプト等）。
if [ -z "$_model" ]; then
  _settings="${CLAUDE_PROJECT_DIR:-.}/.claude/settings.json"
  if [ -f "$_settings" ]; then
    if command -v jq >/dev/null 2>&1; then
      _model="$(jq -r '.model // empty' "$_settings" 2>/dev/null || true)"
    elif command -v python3 >/dev/null 2>&1; then
      _model="$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1])).get("model",""))' "$_settings" 2>/dev/null || true)"
    fi
  fi
fi

# --- 高コスト判定 ------------------------------------------------------------------
if [ "$_mode" != "always" ]; then
  [ -z "$_model" ] && exit 0
  _model_lc="$(printf '%s' "$_model" | tr '[:upper:]' '[:lower:]')"
  printf '%s' "$_model_lc" | grep -qE "$_hc_re" || exit 0
fi

# --- オーケストレーター・ディレクティブの注入 ----------------------------------------
# 注入本文のオーバーライド（Issue #211）:
#   派生リポジトリは .claude/orchestrator-directive.txt を置くと注入本文を全文差し替えできる
#   （既定文言は discussion-review スキルと agent-team-summary.md のセクション名に依存しており、
#     それらを持たない・作り替えた派生リポがロジックに触れず文言だけ差し替えるための逃げ道）。
#   このファイルは base リポジトリには置かない（SYNC_PATHS 同期で下流のカスタム文言を
#   潰す事故経路を作らないため）。空ファイル・不在時は既定文言へフォールバックする。
#   本フックは毎プロンプト発火するため 4KB 上限（超過分は切り詰め・stderr に警告）。
_tpl="${CLAUDE_PROJECT_DIR:-.}/.claude/orchestrator-directive.txt"
if [ -f "$_tpl" ]; then
  _body="$(head -c 4096 "$_tpl" 2>/dev/null || true)"
  if [ "$(wc -c < "$_tpl" 2>/dev/null || echo 0)" -gt 4096 ]; then
    echo "[orchestrator-directive] warning: ${_tpl} が 4KB を超過。先頭 4096 バイトのみ注入します（マルチバイト境界で欠ける可能性あり）" >&2
  fi
  if [ -n "$_body" ]; then
    echo "━━━ [orchestrator-directive] 高コストモデル検出（${_model:-unknown}）━━━"
    printf '%s\n' "$_body"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
  fi
fi

echo "━━━ [orchestrator-directive] 高コストモデル検出（${_model:-unknown}）━━━"
echo "あなたは高コストなモデルで動作中。オーケストレーターとして専門チームを組成してタスクを遂行せよ。"
echo "▶ 単調・並列化可能な作業（探索・調査・定型チェック・大量ファイル処理）は自分で直接実行せず、"
echo "  サブエージェント（Explore/general-purpose・model: haiku/sonnet）へ委譲する。"
echo "▶ 協調モードの振り分けは agent-team-summary.md「2 協調モードと振り分け」（SSOT）に従う:"
echo "  ユーザーが「専門チームを組成して」と明示 → 議論型（discussion-review スキル・ネイティブ）を既定。"
echo "  内部の自動調査・fan-out は Agent 並列（速度/コスト優先）。"
echo "▶ 自分（高コストモデル）が直接担うのは、統合判断・設計・最終レビュー・ユーザー報告のみ。"
echo "▶ 例外: 会話的な応答・数ステップで終わる軽微タスクは委譲せず直接処理してよい（過剰組成の禁止）。"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exit 0
