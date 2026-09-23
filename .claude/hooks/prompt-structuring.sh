#!/usr/bin/env bash
# prompt-structuring.sh
# ユーザーがプロンプトを送信した直後・Claude が処理する前に実行されるフック（UserPromptSubmit イベント）。
#
# 役割（Issue #172）:
#   ユーザーの「生の指示」を Claude が着手する前に、構造化ディレクティブ（作業スペックへの展開指示）を
#   コンテキストに注入する。UserPromptSubmit フックは公式仕様上「生プロンプトの置換」はできない
#   （"can't replace the prompt"）ため、stdout 注入で「着手前に指示をスペック化せよ」と補助する
#   workaround で自動プロンプト整形を近似する（既存 user-prompt-submit-guard.sh と同じ注入経路）。
#
# 設計方針（議論型専門チームレビュー反映・content/discussions/prompt-structuring-design-20260709）:
#   - 非ブロッキング（常に exit 0）。プロンプトは一切ブロックしない。
#   - guard.sh（安全助言）と責務分離。高リスクパターン検出時は本フックの注入を完全抑制し、
#     guard.sh のバナーだけを残す（二重バナー防止・critical-2 是正）。
#   - スキル自然文ルーティング（例: ディープリサーチ→research-runner）を誤誘導しないため、
#     動詞トリガーから「リサーチ」を除外し、注入本文に「スキル起動を優先せよ」を明記（critical-1 是正）。
#   - 短文スキップ閾値は CJK 有無で動的化（言語差別の是正・critical-3 是正）。
#   - concise-neko output style / L-111 準拠: テンプレの見出し・ラベルと中身はチャット本文に貼らせない。
#     ただし着手時プラン提示（output-verbosity-rules.md §1.1・Issue #452）は別途必須なので、
#     禁止は「テンプレの逐語出力」に限定し、圧縮した平文3〜5行の報告は明示的に要求する
#     （否定文だけを残すとモデルが安全側＝無出力に倒れ、プラン提示ごと消える）。
#
# 入力: stdin に JSON（.prompt にユーザー入力テキスト）
# 公式仕様: https://code.claude.com/docs/en/hooks
#   - UserPromptSubmit の stdout はコンテキストに注入される（docs/rules/hook-events-reference.md で検証済み）
#   - exit 2 で prompt をブロック（本フックは使わない）
#
# 環境変数（トグル）:
#   CLAUDE_PROMPT_STRUCTURING = auto（既定・タスク指示にのみ発火）| off（無効）| always（スキップ条件以外の全プロンプト）
#   CLAUDE_PROMPT_MAX_LEN     = 長文スキップ閾値（既定 600 文字。auto のとき既に詳細な長文は整形不要）

set -euo pipefail

_mode="${CLAUDE_PROMPT_STRUCTURING:-auto}"
[ "$_mode" = "off" ] && exit 0

_max_len="${CLAUDE_PROMPT_MAX_LEN:-600}"
# 非数値の誤設定で後続の [ -gt ] がエラーになり長文スキップが無効化されるのを防ぐ（既定へ戻す）。
case "$_max_len" in ''|*[!0-9]*) _max_len=600 ;; esac

# 文字数計算・CJK 判定を正しく行うための UTF-8 ロケールを 1 度だけ解決する。
# 実行環境が POSIX/C ロケールだと wc -m がバイト数を返し、CJK テキストで閾値判定が壊れるため。
_utf8_locale=""
for _cand in C.UTF-8 C.utf8 en_US.UTF-8; do
  if locale -a 2>/dev/null | grep -qix "$_cand"; then _utf8_locale="$_cand"; break; fi
done
: "${_utf8_locale:=C}"  # UTF-8 ロケールが無ければ C（バイト計数・フォールバック）

_input="$(cat 2>/dev/null || true)"

# .prompt を取り出す。jq → python3 → sed の順でフォールバック。
# 注: 生 JSON（_input）を最終手段でも _prompt にそのまま代入しない。JSON エンベロープ
#     （{"prompt": ...}）が混じると先頭一致（/・!・<）判定と文字数カウントが壊れるため、
#     抽出に失敗したら安全側に倒して無発火（exit 0）で終える。
if command -v jq >/dev/null 2>&1; then
  _prompt="$(printf '%s' "$_input" | jq -r '.prompt // empty' 2>/dev/null || true)"
elif command -v python3 >/dev/null 2>&1; then
  _prompt="$(printf '%s' "$_input" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("prompt",""))' 2>/dev/null || true)"
else
  # 最小フォールバック（jq/python3 なし）。.prompt の値だけを素朴に抽出する。
  _prompt="$(printf '%s' "$_input" | sed -n 's/.*"prompt"[[:space:]]*:[[:space:]]*"\(.*\)"[[:space:]]*}[[:space:]]*$/\1/p' 2>/dev/null || true)"
fi
[ -z "$_prompt" ] && exit 0

# 先頭の空白を除いた最初の文字で判定するためのトリム
_trimmed="$(printf '%s' "$_prompt" | sed -e 's/^[[:space:]]*//')"
[ -z "$_trimmed" ] && exit 0

# --- スキップ条件（発火しない = exit 0・無出力）---------------------------------

# スラッシュコマンド（スキル自動ルーティングを阻害しない）
case "$_trimmed" in
  /*) exit 0 ;;
esac

# エスケープ接頭辞（そのターンだけ整形を拒否する明示オプトアウト）
case "$_trimmed" in
  '!'*) exit 0 ;;
esac

# システム/バックグラウンド注入（既知タグ・既知マーカーのみ・厳格化）
# 🔁 同期義務: このタグ・マーカーリストは orchestrator-directive.sh のスキップ条件と
#    逐語同期を保つこと（片方だけ更新する L-094 型 desync 禁止）。
# ハーネスが差し込む非ユーザー入力（system-reminder・task-notification・Stop hook feedback・
# local-command・github-webhook-activity 由来）は整形対象にしない。
# 注: 任意の '<' 始まりを一律スキップすると「<T> の型を実装して」等の正当な指示まで誤除外するため、
#     既知タグの先頭一致に限定する（過剰スキップの是正）。
case "$_trimmed" in
  '<system-reminder'*|'<task-notification'*|'<local-command'*|'<github-webhook-activity'*|'[SYSTEM NOTIFICATION'*) exit 0 ;;
esac
if printf '%s' "$_prompt" | grep -qF -e '[SYSTEM NOTIFICATION' \
     -e '<task-notification>' -e 'Stop hook feedback' \
     -e '<system-reminder>' -e '<local-command' -e '<github-webhook-activity'; then
  exit 0
fi

# --- 高リスクパターン検出時は注入を完全抑制（guard.sh に一元化・二重バナー防止）-------
# user-prompt-submit-guard.sh が助言バナーを出すケースでは、本フックのテンプレ注入を出さない。
_lc="$(printf '%s' "$_prompt" | tr '[:upper:]' '[:lower:]')"
if printf '%s' "$_lc" | grep -qE \
   'push[^a-z]+(origin[^a-z]+)?(main|master)([^a-z]|$)|--force([^a-z].*)?(main|master)([^a-z]|$)|force[-_. ]?push|rm[[:space:]]+-[a-z]*r[a-z]*f?[[:space:]]+(/|~|\$home|\*)|git[[:space:]]+clean[[:space:]]+-[a-z]*f|\.env($|[^a-z])|secret|credential|api[_-]?key|private[_-]?key|no-verify|settings\.local\.json'; then
  exit 0
fi
# main/master への push は語順非依存の共起でも抑制する（guard の正規表現が語順に依存するため取りこぼしを補う）。
if printf '%s' "$_lc" | grep -qE 'push' && printf '%s' "$_lc" | grep -qE '(^|[^a-z])(main|master)([^a-z]|$)'; then
  exit 0
fi

# --- 長さによるスキップ（CJK 有無で動的閾値・コードポイント数ベース）----------------
# 注: auto では後段の動詞ゲートがタスク性を担保するため、min_len は「ごく短いトリビアル入力」の
#     除去に留める。CJK は情報密度が高い（例:「直して」=3 字が実タスク）ため閾値を下げる。
# 文字数（マルチバイト対応・コードポイント数）を数える。UTF-8 ロケールを明示指定しないと
# POSIX/C 環境で wc -m がバイト数を返し、CJK テキストの閾値判定が壊れる（finder 指摘の是正）。
_len="$(printf '%s' "$_trimmed" | LC_ALL="$_utf8_locale" wc -m | tr -d '[:space:]')"
case "$_len" in ''|*[!0-9]*) _len=0 ;; esac

# CJK（漢字・ひらがな・カタカナ）を含むか。含む場合は 1 文字あたりの情報量が大きいため短文閾値を下げる。
# grep -P が使えるかを 1 度だけ判定し、使える場合のみコードポイント範囲で厳密判定する。
# 使えない環境に限りバイト帯フォールバック（過剰検出を避けるため primary 成否では分岐しない）。
_has_cjk=0
if printf '' | LC_ALL="$_utf8_locale" grep -qP '' 2>/dev/null; then
  if printf '%s' "$_trimmed" | LC_ALL="$_utf8_locale" grep -qP '[\x{3040}-\x{30ff}\x{3400}-\x{4dbf}\x{4e00}-\x{9fff}\x{f900}-\x{faff}]' 2>/dev/null; then
    _has_cjk=1
  fi
elif printf '%s' "$_trimmed" | LC_ALL=C grep -q $'[\xe3-\xe9]'; then
  # grep -P 非対応環境向けフォールバック（UTF-8 の CJK は先頭バイトが 0xE3-0xE9 帯に多い）
  _has_cjk=1
fi

if [ "$_has_cjk" = "1" ]; then _min_len=3; else _min_len=8; fi
if [ "$_len" -lt "$_min_len" ]; then exit 0; fi

# 既に詳細な長文は整形の価値が薄い（auto のみ・always は長文でも注入）
if [ "$_mode" = "auto" ] && [ "$_len" -gt "$_max_len" ]; then exit 0; fi

# --- タスク指示ヒューリスティック（auto のみ）------------------------------------
# アクション動詞を含むか。純粋な質問（動詞なし）は auto では注入しない（fail-safe 方向）。
# 注: スキル誤誘導防止のため「リサーチ/research」は動詞トリガーから除外する（critical-1 是正）。
# 注: 日本語動詞はそのまま代替（境界不要）。英語動詞は語境界で囲む。POSIX ERE の \b は移植性が
#     ないため（guard.sh と同方針）、(^|[^a-z])...([^a-z]|$) で「文末・記号隣接」も拾う。
#     _lc は _prompt の ASCII 小文字化（日本語は不変）のため、この 1 回の grep で日英を両方カバーする。
# 注: 非実装系（調査・レビュー・議論・監査）もタスク性を持つため動詞に含める（#452）。ファイル変更が
#     ゼロでも複数工程になる指示は着手時プラン提示の対象であり（output-verbosity-rules.md §1.1）、
#     ここで拾わないとリマインダーが届かない。ただし「リサーチ/research」は上記 critical-1 の除外を
#     維持する（research-runner スキルの自然文ルーティングを誤誘導しないため）。
_verb_re='実装|修正|追加|作成|作っ|作る|作れ|直し|直す|直せ|変更|対応|整理|設計|導入|改善|更新|削除|リファクタ|置き換え|置換|移行|統合|分割|抽出|生成|構築|セットアップ|書い|書く|書き換え|反映|適用|実行|走らせ|検証|調査|検討|議論|レビュー|監査|精査|棚卸し|してほしい|してくれ|して[くだ]|を作|を直|を実装|を追加|を修正|(^|[^a-z])(implement|refactor|fix|add|create|update|change|remove|delete|build|migrate|integrate|setup|rewrite|make|investigate|review|audit|analyze)([^a-z]|$)'
# 「プレビュー」は「レビュー」に部分一致して誤発火する（日本語動詞は境界を付けられないため）。
# 動詞判定の対象からだけ除去する（_lc 本体は後段の複雑性シグナル判定で使うので壊さない）。
_verb_target="$(printf '%s' "$_lc" | sed 's/プレビュー//g')"
if [ "$_mode" = "auto" ]; then
  if ! printf '%s' "$_verb_target" | grep -qE "$_verb_re"; then
    exit 0
  fi
fi

# --- 複雑性シグナル（テンプレ縮退の判定）----------------------------------------
# 単純明快な 1 手順タスク（短文・単一節・改行なし）は 2 項目の簡略テンプレへ縮退する。
_nl_count="$(printf '%s' "$_prompt" | grep -c '' 2>/dev/null || echo 1)"
_simple=0
if [ "$_len" -le 40 ] && [ "${_nl_count:-1}" -le 1 ] \
   && ! printf '%s' "$_prompt" | grep -qE 'かつ|および|そして|さらに|加えて|、.*、|,.+,|then'; then
  _simple=1
fi

# --- 構造化ディレクティブの注入 --------------------------------------------------
echo "━━━ [prompt-structuring] 着手前に元指示を作業スペックへ展開せよ ━━━"
echo "以下は「ユーザーの生指示を、実行前に構造化した作業スペックとして扱え」という補助指示。"
echo "この展開は思考内で一度だけ行い、テンプレの見出し・ラベルと中身そのものはチャット本文に貼らないこと"
echo "（過剰な実況を避ける concise-neko / 内部作業サイレントの原則に準拠）。"
if [ "$_simple" = "1" ]; then
  echo "▶ ただし、複数の作業工程を要すると見込まれるなら、展開結果を圧縮した平文（目的1文＋主要ステップ）を"
  echo "  最初のツール呼び出しより前に1回だけチャット本文へ出すこと（着手時プラン提示・output-verbosity-rules.md"
  echo "  §1.1）。単一ツールで即答できる質問・自明な1行修正（誤字等）なら提示は不要（同 §1.1 の除外）。"
else
  echo "▶ ただし、展開結果を圧縮した平文 3〜5 行（目的1文＋主要ステップ2〜4項目）を、最初のツール呼び出しより"
  echo "  前に1回だけチャット本文へ出すこと（着手時プラン提示・必須。output-verbosity-rules.md §1.1）。"
fi
echo "  承認要求ではなく報告なので確認文で終わらせず、報告後は無言で着手する（CP-6）。同一指示の実行中は"
echo "  フェーズが変わっても再提示しない。この1回の報告と、L-111 が禁じる各ツール呼び出し毎の逐次宣言・"
echo "  事後実況は別物であり、後者は引き続き禁止のまま。委譲する場合も到達点だけ述べ手段は実況しない。"
echo "▶ まず、この指示が既知スキル（例: ディープリサーチ→research-runner、ベース反映→apply-base 等）の"
echo "  自然文トリガーに該当するなら、テンプレ展開より該当 Skill の起動判断を優先せよ。"
if [ "$_simple" = "1" ]; then
  echo "▶ 展開テンプレ（単純タスク・2 項目）:"
  echo "  【目的】達成したい結果（対象ファイル/リポジトリを含め 1 文）"
  echo "  【成功条件】検証可能な完了条件（テスト通過・出力確認等）"
else
  echo "▶ 展開テンプレ（4 項目）:"
  echo "  【目的】達成したい結果（前提・対象リポジトリ/ファイルを統合して簡潔に）"
  echo "  【成功条件】検証可能な完了条件"
  echo "  【範囲/非対象】やること / やらないこと（タスク外変更の抑止・YAGNI）"
  echo "  【手順】主要ステップ（該当時のみ・可変長）"
fi
echo "▶ 生指示と矛盾する解釈はしない。曖昧点は最も単純な合理的解釈で仮定を1行記録して進める"
echo "  （Think Before Coding）。不可逆リスク（A-1〜A-6）の確認要否は guard の助言に従う。"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exit 0
