#!/bin/bash
# Stop ルーター: セッション終了時の3つのフックを1つに統合
# トークン最適化: 3つの Stop フック → 1つに統合（CC-BUG-16 対策）
#
# 各チェックスクリプトを順に実行する。
# 1つが exit 2（ブロック）を返した場合でも、残りは実行する（終了処理は全て完了させる）。
#
# 【L-050 修正】複数サブスクリプトが個別に stdout/stderr 出力すると
# Claude Code が最初の1つしか解析しないリスクがある。
# → 各サブスクリプトの stderr（hook_block 経由のブロック理由）を一時ファイルで収集し、
#   最後に単一の stderr メッセージとして出力する（Issue #142: stdout JSON と exit 2 は排他のため
#   stdout JSON ではなく stderr に統一する）。

# stdin を保存して各サブスクリプトに渡す
INPUT=$(cat 2>/dev/null || true)

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
FINAL_EXIT=0
# 直近に実行したサブスクリプトの終了コード。FINAL_EXIT は全サブスクリプトの合算値であり、
# 「どのフックがブロックしたか」を区別できない（PR 未作成でブロックしただけのケースと
# 未コミット変更でブロックしたケースが混ざる）。個別フックの結果を後段へ渡すために別途保持する。
LAST_HOOK_EXIT=0

# 一時ファイルでメッセージを収集（改行を保持するため変数ではなくファイルを使用）
MSG_FILE=$(mktemp /tmp/stop-router-msgs-XXXXXX)
trap 'rm -f "$MSG_FILE"' EXIT

# サブスクリプトを実行し、stderr のブロック理由を MSG_FILE に追記する関数
# run_hook <script> [notify_on_success]
#   notify_on_success を渡すと、exit 0 で終わったときも非空の stderr を MSG_FILE に転記する。
#   既定（省略時）は exit 2 / 異常終了のときだけ転記する従来動作。
#   stop-slack-notify.sh は常に exit 0 で終わる設計のため、これを渡さないと
#   「WIP 自動コミットを 1 巡見送った」「push に失敗した」といった通知が Claude に一切届かない。
run_hook() {
  local script="$1"
  local notify_on_success="${2:-}"
  local err exit_code

  # ファイル不在は正常スキップ（クラッシュ扱いにしない）。
  # 公開物では著者専用フック（例: stop-publish-check.sh）が DENYLIST で除外されるため、
  # 下流の配布先ではファイルが存在しない状態が正規の構成になる。
  # 個別の呼び出し箇所に条件を書くと将来別のフックが外れたときに同じ事故が再発するため、
  # run_hook 側で一律吸収する（存在するのに失敗した場合は従来どおりクラッシュ扱い）。
  if [ ! -f "$HOOK_DIR/$script" ]; then
    LAST_HOOK_EXIT=0
    return 0
  fi

  # stderr をキャプチャしつつ exit code を取得（set -e 未使用のため $? は確実にサブスクリプトの終了コード）
  err=$(printf '%s\n' "$INPUT" | "$HOOK_DIR/$script" 2>&1 >/dev/null)
  exit_code=$?
  LAST_HOOK_EXIT=$exit_code

  if [ "$exit_code" -eq 2 ]; then
    FINAL_EXIT=2
    # 既存メッセージがあれば区切り線を挿入
    if [[ -s "$MSG_FILE" ]]; then
      printf '\n\n---\n\n' >> "$MSG_FILE"
    fi
    if [[ -n "$err" ]]; then
      printf -- '%s' "$err" >> "$MSG_FILE"
    else
      # stderr も空の場合: フォールバック文言を追記
      printf -- '%s がブロック理由を出力しませんでした（exit 2）' "$script" >> "$MSG_FILE"
    fi
  elif [ "$exit_code" -ne 0 ]; then
    # クラッシュ系（exit 1/127 等）も可視化してサイレントスキップを防ぐ（L-050 対策）
    FINAL_EXIT=2
    if [[ -s "$MSG_FILE" ]]; then
      printf '\n\n---\n\n' >> "$MSG_FILE"
    fi
    printf -- '%s が exit %s で失敗しました' "$script" "$exit_code" >> "$MSG_FILE"
  elif [ -n "$notify_on_success" ] && [[ -n "$err" ]]; then
    # 正常終了（exit 0）だが伝えるべき情報がある場合。FINAL_EXIT は変えない（ブロックしない）。
    if [[ -s "$MSG_FILE" ]]; then
      printf '\n\n---\n\n' >> "$MSG_FILE"
    fi
    printf -- '%s' "$err" >> "$MSG_FILE"
  fi
}

# 1. Git 未コミットチェック
run_hook "stop-git-check.sh"
# git-check「専用」の終了コードを退避する（#483）。
# run_hook はサブシェルではなく同一プロセスの bash 関数のため、ここで得た結果を
# 同一 Stop 呼び出し内で後段（3. の WIP 自動コミット）へ確定的に渡せる
# （プラットフォームの再発火セマンティクス = stop_hook_active に依存しない）。
GIT_CHECK_EXIT=$LAST_HOOK_EXIT

# 2. PR 存在チェック
run_hook "stop-pr-check.sh"

# 3. Slack 通知 + WIP 自動コミット
# git-check が差し戻し中かどうかを環境変数で **stop-slack-notify.sh にだけ** 渡す（#483）。
# 渡さないと、差し戻しと同一の Stop 呼び出しの中で WIP 自動コミットが先に確定してしまい、
# Claude が意味のあるコミットを作る機会が構造的に奪われる（実測済み）。
# 見送るか撃つかの最終判断（上限つきフェイルセーフ）は stop-slack-notify.sh 側が持つ。
#
# notify_on_success: stop-slack-notify.sh は常に exit 0 で終わるため、これを付けないと
# 「1 巡見送った」「push に失敗した」という stderr が Claude に届かない。なお見送りが起きるのは
# git-check が exit 2 を返したときだけなので、その場合は FINAL_EXIT=2 となりメッセージは確実に届く。
#
# 変数は直後に unset し、後続フック（4. / 5.）へ巻き添えで漏らさない
#（stop_hook_active の全体 early return がコスト集計まで止めていた同型の事故を繰り返さない）。
if [ "$GIT_CHECK_EXIT" -eq 2 ]; then
  export CLAUDE_STOP_GIT_CHECK_BLOCKED=1
else
  export CLAUDE_STOP_GIT_CHECK_BLOCKED=0
fi
run_hook "stop-slack-notify.sh" notify_on_success
unset CLAUDE_STOP_GIT_CHECK_BLOCKED

# 4. 完了報告フォーマットチェック（ご依頼再掲→アウトカム中心・completion-report-rules.md）
run_hook "stop-completion-report-check.sh"

# 5. 公開リポジトリ（kai-kou/claude-code-repository-base）のドリフト検知（Issue #381）
run_hook "stop-publish-check.sh"

# 【Issue #543】Stop フックが差し戻した（FINAL_EXIT=2）続行ターンでは、直前の完了報告
# フォーマット（テンプレート全体）をそのまま再掲せず、求められた確認・追加対応の結果だけを
# 返すべきという続行ターン規律（completion-report-rules.md §1.2）へのポインタを 1 回だけ付記する。
# 判定基準（[report-format] の有無で書き直すか等）はここに書かない: SSOT は §1.2 であり、
# ここに分岐説明を埋め込むとルール改訂時に文言がズレる（議論 round 3 の裁定・#543）。
# サブフックが複数ブロックしても MSG_FILE への追記は 1 度（この if ブロック自体が 1 回しか
# 通らないため自然に満たされる）。
if [[ "$FINAL_EXIT" -eq 2 ]] && [[ -s "$MSG_FILE" ]]; then
  printf '\n\n---\n\n[continuation] これは Stop フックの差し戻しです。続行ターンでは直前の完了報告を再掲しない（SSOT: docs/rules/completion-report-rules.md §1.2）\n' >> "$MSG_FILE"
fi

# 収集したメッセージを単一の stderr 出力として出す（L-050: 複数メッセージ問題を修正）。
# 公式仕様: exit 2 時は stdout の JSON が無視されるため stderr に統一する（Issue #142）。
if [[ -s "$MSG_FILE" ]]; then
  cat "$MSG_FILE" >&2
fi

exit $FINAL_EXIT
