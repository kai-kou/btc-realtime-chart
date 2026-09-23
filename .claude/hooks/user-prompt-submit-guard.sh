#!/usr/bin/env bash
# user-prompt-submit-guard.sh
# ユーザーがプロンプトを送信した直後・Claude が処理する前に実行されるフック（UserPromptSubmit イベント）
#
# 役割（E-E #23）:
#   ユーザー入力に「不可逆・高リスクな操作」を示唆するパターンが含まれる場合、
#   関連ガードレール（A-1 main 直 push 禁止 / .env 保護 / 取り消し困難な操作の確認 等）を
#   Claude のコンテキストに「助言」として注入し、暴走を未然に防ぐ。
#
# 設計方針（保守的・非ブロッキング）:
#   - 既定では prompt をブロックしない（exit 0）。誤検知で正当な作業を止めるリスクを避ける。
#   - 該当パターン検出時は stdout に助言を出力する。UserPromptSubmit の stdout は
#     コンテキストに注入される（公式仕様・docs/rules/hook-events-reference.md で検証済み）ため、
#     Claude が該当ルールを再確認した上で応答できる。
#   - 真に破壊的なパターン（例: rm -rf / 等のルート削除）のみ、コメントアウトされた
#     ブロック例（exit 2）を残す。プロジェクトの判断で有効化できる。
#
# 入力: stdin に JSON（.prompt にユーザー入力テキスト）
# 公式仕様: https://code.claude.com/docs/en/hooks
#   - stdout はコンテキストに注入される（UserPromptSubmit / UserPromptExpansion / SessionStart のみ）
#   - exit code 2 で prompt をブロック＆消去、stderr が Claude にエラーとして渡る

set -euo pipefail

_input="$(cat 2>/dev/null || true)"

# .prompt を取り出す（jq があれば使う。無ければ raw 全体を対象にフォールバック）
if command -v jq >/dev/null 2>&1; then
  _prompt="$(printf '%s' "$_input" | jq -r '.prompt // empty' 2>/dev/null || true)"
else
  _prompt="$_input"
fi
[ -z "$_prompt" ] && exit 0

# 小文字化して検査（大文字小文字を無視）
_lc="$(printf '%s' "$_prompt" | tr '[:upper:]' '[:lower:]')"

_advice=""
_add() { _advice="${_advice}$1"$'\n'; }

# --- 高リスクパターン検出（助言注入・非ブロッキング）---

# 1. main/master への直接 push・force push（A-1 既約境界外）
# 注: grep -E（ERE）では \b（単語境界）が POSIX 未定義で移植性がないため使わず、
#     空白・行頭行末・非英字（[^a-z]）で境界を表現する。
if printf '%s' "$_lc" | grep -qE 'push[^a-z]+(origin[^a-z]+)?(main|master)([^a-z]|$)|--force([^a-z].*)?(main|master)([^a-z]|$)|force[-_. ]?push'; then
  _add "⚠️ [guard] main/master への直接 push・force push は A-1（既約境界外・不可逆）。直接 push せず、作業ブランチ → PR → 自動マージの経路を取ること（CLAUDE.md / user-confirmation-minimization.md §1）。"
fi

# 2. 破壊的なファイル削除（ルート・ホーム・git 履歴）
if printf '%s' "$_lc" | grep -qE 'rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]+(/|~|\$home|\*)|git[[:space:]]+clean[[:space:]]+-[a-z]*f'; then
  _add "⚠️ [guard] 破壊的な削除（rm -rf / git clean -f 等）は不可逆。対象パスを限定し、未コミット作業がないか git status を確認してから実行すること。"
fi

# 3. .env / 秘密情報の読み取り・コミット
# 注: ERE では \b が使えないため、.env の右境界は「行末 or 非英字（. _ / 空白等）」で表現する。
if printf '%s' "$_lc" | grep -qE '\.env($|[^a-z])|secret|credential|api[_-]?key|private[_-]?key|token.*(commit|push|print|echo|cat)'; then
  _add "⚠️ [guard] .env・秘密情報・トークンの取り扱い。.env は読み取りブロック対象（settings.json deny）。秘密情報をコミット・ログ出力・コンテキストに展開しないこと。"
fi

# 4. フック・ガードレールの無効化（--no-verify 等）
if printf '%s' "$_lc" | grep -qE 'no-verify|disable.*(hook|guard|check)|bypass.*(hook|permission)|skip.*(hook|ci)'; then
  _add "⚠️ [guard] フック・ガードレールの無効化を要求している可能性。Lv3 フック（main 直 push ブロック等）は安全装置。無効化の前にユーザー意図を確認すること（harness-escalation.md）。"
fi

# 5. settings.local.json への env 書き込み（クラウドで揮発・禁止）
if printf '%s' "$_lc" | grep -qE 'settings\.local\.json'; then
  _add "⚠️ [guard] .claude/settings.local.json に環境変数を書かないこと（クラウドでセッション間に消える・CLAUDE.md）。env は Claude.ai 環境設定 / secrets-broker で供給する（クラウドの gh variable set は 403・docs/rules/env-vars.md）。"
fi

# 6. 「専門チームを組成して」等の明示指示 → ネイティブ議論型（discussion-review スキル）を既定に（fan-out 誤選択の防止）
# 注: 小文字化済み・日本語はそのまま比較する。「専門チーム/エージェントチーム」+「組成/編成/組ん/組む/議論/レビュー」の同時出現で発火。
if printf '%s' "$_prompt" | grep -qE '(専門チーム|エージェントチーム|チームで議論)' \
   && printf '%s' "$_prompt" | grep -qE '(組成|編成|組ん|組む|議論|レビュー|起動|立ち上げ|作成|作っ|作る|構築)'; then
  _add "🧭 [guard] 「専門チーム」の明示指示。既定は議論型（ネイティブ Agent Teams = discussion-review スキル。claude -p の run_discussion_review.py はフォールバック）を選ぶこと。役割分担型 fan-out（Agent 並列）にするのはコスト/速度優先の明示か軽微タスクのときだけ（理由を1行述べる）。SSOT: agent-team-summary.md「2 協調モードと振り分け」/ discussion-whiteboard-rules.md。"
fi

if [ -n "$_advice" ]; then
  echo "━━━ [UserPromptSubmit guard] 高リスクパターンを検出。以下を再確認した上で応答すること ━━━"
  printf '%s' "$_advice"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi

# 既定は非ブロッキング（助言のみ）。真に破壊的なパターンをブロックしたいプロジェクトは
# 下記のような分岐を有効化する（exit 2 で prompt をブロック＆消去）:
#   if printf '%s' "$_lc" | grep -qE 'rm[[:space:]]+-rf[[:space:]]+/($|[[:space:]])'; then
#     echo "[guard] ルートディレクトリの削除はブロックしました。" >&2
#     exit 2
#   fi

exit 0
