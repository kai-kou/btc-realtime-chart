#!/bin/bash
# Stop hook: WIP自動コミット + Slackセッション終了通知
# セッション終了時に未コミット変更を自動保存し、Slackに通知する
set -euo pipefail

input=$(cat)

# 再帰防止フラグ（jq 失敗時は "false" にフォールバック）
#
# 【#483】かつてはここでスクリプト全体を早期 return していたが 2 つの問題があった:
#   1. 廃止済み Slack 通知（Issue #2597）の多重発火防止のために書かれたガードが、その後も
#      残り続け、日次コスト集計まで巻き添えでスキップしていた。
#   2. WIP 自動コミットまで止まるため「差し戻し後の 2 巡目で必ず保全する」という
#      フェイルセーフが原理的に実装できなかった（L-100 の後退につながる）。
# → 全体 return をやめ、再帰防止が本来必要なコスト集計ブロックだけを条件で囲む。
#   WIP 自動コミットの可否は後段の専用ロジック（差し戻し 1 巡猶予 + 無条件フォールバック）が決める。
stop_hook_active=$(echo "$input" | jq -r '.stop_hook_active // "false"' 2>/dev/null || echo "false")

# git リポジトリでなければスキップ
if ! git rev-parse --git-dir >/dev/null 2>&1; then exit 0; fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# ── 日次コスト集計（#1213・#95・#106・#242）────────────────────────────
# 月次レポート（content/analytics/cost_monthly/YYYY-MM.json）は gitignore 対象で、
# main では追跡しない（#242）。高頻度更新テレメトリを feature ブランチに相乗りさせると
# churn 混入・未コミット誤検知でトークンを浪費するため、永続化経路を完全分離する:
#   - 本フックでは cost_log.jsonl への追記と月次 JSON のローカル更新（flush）のみ行う。
#   - 永続化は commit_cost_telemetry.py がテレメトリ専用データブランチ
#     telemetry/cost-data へ「1 日 1 回の plain git push」で行う（gh 非依存・後述ブロック）。
#
# 2 ステップ呼び出し:
#   1. --summary-only: 当セッションのコストを cost_log.jsonl に O_APPEND 追記
#   2. --flush --rotate: 追記済み cost_log.jsonl から月次 JSON を生成・古い行を削除
# ※ --summary-only を --flush に置換すると early return でセッションデータが欠落するため禁止。
#
# 再帰防止: 差し戻し後の再発火（stop_hook_active=true）では二重計上を避けるためスキップする（#483）。
# 同梱ツールは REPO_ROOT（git 操作対象＝消費先プロジェクト）ではなく CLAUDE_PLUGIN_ROOT
# （プラグイン配布時にハーネスが設定）優先で探す。分離しないと第三者プロジェクトで
# REPO_ROOT/tools/ が存在せず常に不発扱いになる（#539。無害不発自体は意図した設計）。
# 値は絶対パス形式のときのみ採用する（空文字・相対パス等の想定外値は REPO_ROOT へフォールバック）。
SCRIPTS_ROOT="$REPO_ROOT"
case "${CLAUDE_PLUGIN_ROOT:-}" in
  /*) SCRIPTS_ROOT="$CLAUDE_PLUGIN_ROOT" ;;
esac
_calc_script="${SCRIPTS_ROOT}/tools/calc_daily_cost.py"
if [[ "$stop_hook_active" != "true" ]] && [[ -f "$_calc_script" ]] && command -v python3 &>/dev/null; then
  timeout 15s python3 "$_calc_script" --summary-only <<< "$input" >/dev/null 2>&1 || true
  timeout 15s python3 "$_calc_script" --flush --rotate >/dev/null 2>&1 || true
fi
unset _calc_script

# ── 月次コスト集計の永続化（telemetry/cost-data ブランチへ直 push・#242）──
# 作業中のチェックアウトに触れず、git plumbing でコミットを構築してデータブランチへ
# push する（PR・gh 不要）。--gate-daily で JST 当日 1 回に収束する
# （外部スケジューラ非依存。実データ差分が無ければ no-op で push しない）。
# 上の flush 直後に置き、最新の月次 JSON をローカルから読ませる。
_tele_script="${SCRIPTS_ROOT}/tools/commit_cost_telemetry.py"
if [[ "$stop_hook_active" != "true" ]] && [[ "${CLAUDE_CODE_REMOTE:-}" = "true" ]] && [[ -f "$_tele_script" ]] && command -v python3 &>/dev/null; then
  # 120s: fetch/push リトライ込みの内部予算に余裕を持たせる。途中で SIGTERM されても
  # マーカーは成功後 stamp のため、同日中の次セッション Stop hook が再試行する（#243）
  timeout 120s python3 "$_tele_script" --gate-daily >/dev/null 2>&1 || true
fi
unset _tele_script

# ──────────────────────────────────────────
# 未コミット変更の自動保存（セッション終了時のファイルリセット防止）
# 問題: セッション終了後に新セッションが起動すると SessionStart フックが
#       git reset/checkout/clean を実行し未コミット変更が全て消える。
#       停止直前に自動コミット&プッシュすることで作業内容を保護する。
# 対象: クラウド環境（CLAUDE_CODE_REMOTE=true）かつ main/master 以外のブランチのみ
#
# 【差し戻し 1 巡猶予・#483】
#   同一の Stop 呼び出しの中で stop-git-check.sh が「コミットして push してください」と
#   差し戻しているにもかかわらず、ここで無条件に `git add -A` + `[wip]` コミットを確定させると、
#   Claude が意味のあるコミット（意味あるメッセージ・意味ある粒度）を作る機会が構造的に消える
#   （差し戻しを受けた次ターンでは working tree が既に clean で `nothing to commit` になる・実測）。
#   → 差し戻し中は **1 巡だけ** 見送り、Claude に機会を与える。
#
#   ただし L-100（未コミット作業の消失防御）は絶対制約であり、猶予は必ず有限にする:
#   セッション ID 付きマーカーで巡数を数え、**2 巡目以降は差し戻しの有無にかかわらず無条件でコミット**する
#   （Claude が対応しない・対応しきれない場合でも必ず保全される）。
#
#   残余リスク（設計上の限界・docs/rules/session-safety-rules.md に明記）:
#     セッションのクラッシュ・タイムアウト等で「次の Stop 自体が発火しない」場合、
#     猶予した 1 巡分は保全されない。これは Stop ベースのどの設計でも原理的に回避できない。
# ──────────────────────────────────────────
if [[ "${CLAUDE_CODE_REMOTE:-}" = "true" ]]; then
  _branch=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "")
  if [ -n "$_branch" ] && [ "$_branch" != "main" ] && [ "$_branch" != "master" ]; then
    # 猶予マーカー: git 管理外（.git/ 配下）に置きリポジトリを汚さない。
    # セッション ID でファイル名を分けるためマルチセッション並行実行でも競合しない（CP-4）。
    _defer_marker=""
    _git_dir=$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir 2>/dev/null || echo "")
    # jq 失敗（不正 JSON 等）でスクリプト全体が落ちないよう必ずフォールバックを付ける。
    # ここで落ちると WIP 自動保全そのものが走らず L-100 の防御が消える（set -euo pipefail 下）。
    _session_id=$(echo "$input" | jq -r '.session_id // ""' 2>/dev/null | tr -cd 'A-Za-z0-9_-' | cut -c1-64 || echo "")
    if [ -n "$_git_dir" ] && [ -n "$_session_id" ]; then
      _defer_marker="${_git_dir}/claude-wip-deferred-${_session_id}"
      # 古い（7 日超）マーカーの残骸を掃除する（ローカル実行で .git/ に溜まり続けるのを防ぐ）
      find "$_git_dir" -maxdepth 1 -name 'claude-wip-deferred-*' -mtime +7 -delete 2>/dev/null || true
    fi

    if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)" ]; then
      # 差し戻し中 かつ まだ猶予を使っていない場合のみ 1 巡見送る。
      # マーカーを作れない環境（session_id 取得失敗・.git が書き込み不可等）では見送らない
      # ＝安全側（従来どおり即コミット）。マーカー作成の成否そのものを判定条件にする。
      _deferred=false
      if [ "${CLAUDE_STOP_GIT_CHECK_BLOCKED:-0}" = "1" ] && [ -n "$_defer_marker" ] && [ ! -f "$_defer_marker" ]; then
        if : > "$_defer_marker" 2>/dev/null; then _deferred=true; fi
      fi

      if [ "$_deferred" = true ]; then
        # 【見送る 1 巡でも作業は必ず保全する（L-100 を落とさない）】
        # ブランチ HEAD を進めずにスナップショット ref を作る。一時 index（GIT_INDEX_FILE）を
        # 使うため、Claude のステージ状態・working tree・HEAD を一切変更しない。未追跡ファイルも
        # 含まれる（`git stash create` は未追跡を拾えないため使わない）。
        # これにより「この 1 巡でクラッシュして次の Stop が発火しない」場合でも、
        # 次セッションの SessionStart クリーンアップ（git reset / checkout / clean）から復元できる。
        #   復元: git checkout refs/claude-wip/<session_id> -- .
        if [ -n "$_session_id" ]; then
          _tmp_index=$(mktemp 2>/dev/null || echo "")
          if [ -n "$_tmp_index" ]; then
            if GIT_INDEX_FILE="$_tmp_index" git -C "$REPO_ROOT" read-tree HEAD 2>/dev/null \
               && GIT_INDEX_FILE="$_tmp_index" git -C "$REPO_ROOT" add -A -- . ':(exclude)content/analytics/cost_monthly/' 2>/dev/null; then
              _snap_tree=$(GIT_INDEX_FILE="$_tmp_index" git -C "$REPO_ROOT" write-tree 2>/dev/null || echo "")
              if [ -n "$_snap_tree" ]; then
                _snap_commit=$(git -C "$REPO_ROOT" commit-tree "$_snap_tree" -p HEAD \
                  -m "wip snapshot (deferred auto-commit, session ${_session_id})" 2>/dev/null || echo "")
                if [ -n "$_snap_commit" ]; then
                  git -C "$REPO_ROOT" update-ref "refs/claude-wip/${_session_id}" "$_snap_commit" 2>/dev/null || true
                fi
              fi
            fi
            rm -f "$_tmp_index"
          fi
          unset _tmp_index _snap_tree _snap_commit 2>/dev/null || true
        fi
        echo "[Stop] 未コミット変更の差し戻し中のため WIP 自動コミットを 1 巡だけ見送ります（#483）。" >&2
        echo "[Stop] → 意味のある単位・意味のあるメッセージでコミットしてください。対応がなければ次の Stop で無条件に自動保全します。" >&2
        echo "[Stop] 保険として refs/claude-wip/${_session_id} にスナップショットを保存しました（復元: git checkout refs/claude-wip/${_session_id} -- .）。" >&2
      else
        # 2 巡目以降 / 差し戻しなし / マーカー不可 → 無条件で保全する（L-100 のフェイルセーフ）
        # 猶予マーカーと、猶予中に作ったスナップショット ref は役目を終えるので片付ける
        # （ここから先は通常のコミットが履歴に載るため、ref を残すと残骸になる）。
        [ -n "$_defer_marker" ] && rm -f "$_defer_marker"
        [ -n "$_session_id" ] && git -C "$REPO_ROOT" update-ref -d "refs/claude-wip/${_session_id}" 2>/dev/null || true
        _timestamp=$(TZ='Asia/Tokyo' date '+%Y-%m-%d %H:%M' 2>/dev/null || date '+%Y-%m-%d %H:%M')
        # 月次コストテレメトリ（cost_monthly）は feature ブランチに混入させない（#106・#242）。
        # gitignore 化済みだが、gitignore 反映前の旧ブランチで追跡されている場合に備え
        # pathspec でも明示除外する（永続化は telemetry/cost-data ブランチが担う）。
        git -C "$REPO_ROOT" add -A -- . ':(exclude)content/analytics/cost_monthly/' 2>/dev/null || true
        # cost_monthly 以外に変更が無ければ何もコミットしない（空コミットを避ける）
        #
        # メッセージは意図的に `[wip]` プレフィックスを維持する（#483）。
        # 差分から機械生成した件名（例 `chore(config): 1 file changed`）は、実際に main を汚した
        # 5 件に当てはめると全件が同一文言になり情報量ゼロで、かつ「意図的なコミット」に偽装する分
        # 可読性がむしろ後退する（議論記録: content/discussions/auto_commit_message_design/）。
        # ここでの正しい振る舞いは「これは自動保全であって意味あるコミットではない」と正直に名乗り、
        # pre-pr-create-check.sh のガードに確実に引っかかって書き換えを強制させることである。
        if ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null && \
           git -C "$REPO_ROOT" commit -m "[wip] 自動保全: 意味のあるコミット未作成のまま終了（${_timestamp}）"; then
          # push 失敗時はリトライ（指数バックオフ: 2s, 4s, 8s）
          _pushed=false
          for _wait in 0 2 4 8; do
            [ "$_wait" -gt 0 ] && sleep "$_wait"
            if git -C "$REPO_ROOT" push -u origin "$_branch" 2>/dev/null; then
              _pushed=true
              break
            fi
          done
          if [ "$_pushed" = false ]; then
            echo "Warning: Stop-hook push failed after retries. Commit is local-only." >&2
          fi
          unset _pushed _wait
        fi
      fi
    else
      # working tree が clean なら猶予をリセットする（次に差し戻しが起きたら再び 1 巡与える）。
      # 保険のスナップショット ref も不要になるので削除する。
      [ -n "$_defer_marker" ] && rm -f "$_defer_marker"
      [ -n "$_session_id" ] && git -C "$REPO_ROOT" update-ref -d "refs/claude-wip/${_session_id}" 2>/dev/null || true
    fi
  fi
  unset _branch _timestamp _defer_marker _git_dir _session_id _deferred
fi

# ──────────────────────────────────────────
# Slack 通知（session-stop）は廃止した（Issue #2597）
# ──────────────────────────────────────────
# セッション単位の開始/終了通知が通知氾濫の主因（約64通/日・全体の75〜85%）だったため、
# 「半日アウトカムサマリー」（tools/half_day_summary.py・07:00/19:00 発火）に集約した。
# 本フックに残る役割は「WIP 自動コミット」と「cost_log.jsonl へのコスト追記」のみ。
# （cost_log.jsonl は half_day_summary.py の稼働集計データソースになる）
exit 0
