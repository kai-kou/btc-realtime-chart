#!/usr/bin/env bash
# check_rules_sync.sh
# docs/rules/ と .claude/rules/ の同期状態を検証するスクリプト
#
# 使い方:
#   ./tools/check_rules_sync.sh          # 不足・リンク切れを報告して終了コードで示す
#   ./tools/check_rules_sync.sh --fix    # 不足シンボリックリンクを自動作成・リンク切れを削除する
#                                          # （ESSENTIAL_RULES 側の表記ミス・存在しないエントリは --fix でも直せない）
#
# 終了コード:
#   0: 全ファイルが同期済み
#   1: 不足ファイルあり、またはリンク切れあり（--fix なしの場合）
#      または ESSENTIAL_RULES に docs/rules/ 側の実体が無いエントリがある場合（--fix でも解決しない）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCS_RULES="$REPO_ROOT/docs/rules"
CLAUDE_RULES="$REPO_ROOT/.claude/rules"
FIX_MODE=false

if [[ "${1:-}" == "--fix" ]]; then
  FIX_MODE=true
fi

missing=()
broken=()
unresolved=()

# トークン最適化: .claude/rules/ に配置するのは「常時必要」なルールのみ。
# タスク依存のルールは docs/rules/ に実体のみ配置し、スキルが必要時に Read する。
# この ESSENTIAL_RULES リストに含まれないファイルは .claude/rules/ に symlink を作成しない。
ESSENTIAL_RULES=(
  # tsukuru 固有: 要件定義フェーズと実装フェーズの確認境界（本プロジェクトの中核規律）。
  # 圧縮後に失われると CP-6 と上流工程が両方守られない状態に戻るため Hot に置く。
  "phase-boundary-rules.md"
  "agent-team-summary.md"
  "completion-report-rules.md"
  "core-principles.md"
  "datetime-rules.md"
  "lessons-core.md"
  "pr-review-flow-summary.md"
  "session-compression-rules.md"
  "session-concurrency-rules.md"
  "session-safety-rules.md"
  "session-sprint-rules.md"
  "user-confirmation-minimization.md"
  "user-instruction-issue-rules.md"
  "user-notification-triage.md"
)

# Warm 降格済み（既定では Hot 層に常駐させない）:
#   - progress-reporting-rules.md: 制作系の長時間処理を行うときに該当パイプラインスキルが冒頭で Read する
#   - session-concurrency-rules.md: Scheduled Tasks（マルチセッション並行運用）を使うプロジェクトのみ symlink する
#     → 本リポジトリは R-1 ルーティン（docs/routines.md）の稼働開始に伴い Hot 化済み（E-B #20・PR #176）
#   - ai-reviewer-strategy.md: Warm 降格済み（#88）。現行 FAIR 構成の要点は圧縮版 + pr-review-flow-summary.md に記載
#   - autonomous-operation-policy.md: Warm 降格済み（#89）。user-confirmation-minimization.md / core-principles.md と大幅重複
#   - session-sprint-rules-detail.md: session-sprint-rules.md の詳細版（Warm 専用・#90）
#   - session-safety-rules-detail.md: session-safety-rules.md の詳細版（Warm 専用・#91）
# これらを Hot 層に戻したいプロジェクトは上の ESSENTIAL_RULES に追記して --fix を実行する。

# ESSENTIAL_RULES に含まれるファイルのみを同期対象にする（トークン最適化）
is_essential() {
  local filename_to_check="$1"
  for ess in "${ESSENTIAL_RULES[@]}"; do
    if [[ "$filename_to_check" == "$ess" ]]; then
      return 0
    fi
  done
  return 1
}

# 正方向チェック: ESSENTIAL_RULES の各エントリが docs/rules/ に実在するか検証する（fail-closed）。
# 逆方向（docs/rules/ の実ファイル走査）だけでは、ESSENTIAL_RULES 側の表記ミス・
# 存在しないエントリを検出できない（黙殺・fail-open のバグの直接原因）。
for ess in "${ESSENTIAL_RULES[@]}"; do
  if [[ ! -e "$DOCS_RULES/$ess" ]]; then
    unresolved+=("$ess")
    echo "[UNRESOLVED] ESSENTIAL_RULES の \"$ess\" が docs/rules/ に実在しません（拡張子の有無・表記を確認してください）" >&2
  fi
done

for docs_file in "$DOCS_RULES"/*.md; do
  filename="$(basename "$docs_file")"
  is_essential "$filename" || continue  # 常時必要なファイルのみチェック

  claude_target="$CLAUDE_RULES/$filename"

  if [[ ! -e "$claude_target" ]]; then
    missing+=("$filename")
    if $FIX_MODE; then
      ln -s "../../docs/rules/$filename" "$claude_target"
      echo "[FIXED] シンボリックリンクを作成: .claude/rules/$filename"
    else
      echo "[MISSING] .claude/rules/$filename が存在しません"
    fi
  fi
done

# 逆方向チェック: .claude/rules/ にシンボリックリンクがあるが docs/rules/ に実体がないケース
for claude_file in "$CLAUDE_RULES"/*.md; do
  filename="$(basename "$claude_file")"

  # シンボリックリンクのリンク先が存在するか確認
  if [[ -L "$claude_file" ]] && [[ ! -e "$claude_file" ]]; then
    echo "[BROKEN] .claude/rules/$filename はリンク切れのシンボリックリンクです"
    broken+=("$filename")
    if $FIX_MODE; then
      rm "$claude_file"
      echo "[FIXED] リンク切れを削除: .claude/rules/$filename"
    fi
  fi
done

if [[ ${#unresolved[@]} -gt 0 ]]; then
  echo "[NG] ESSENTIAL_RULES に docs/rules/ 側の実体が無いエントリが ${#unresolved[@]} 件あります: ${unresolved[*]}" >&2
  echo "--fix では解決できません（ESSENTIAL_RULES の表記か docs/rules/ 側の実ファイルを直してください）" >&2
  if [[ ${#missing[@]} -gt 0 || ${#broken[@]} -gt 0 ]]; then
    echo "（あわせてシンボリックリンク不足 ${#missing[@]} 件・リンク切れ ${#broken[@]} 件も検出しています。詳細は上の [MISSING]/[BROKEN]/[FIXED] 行を参照してください）" >&2
  fi
  exit 1
fi

if [[ ${#missing[@]} -eq 0 && ${#broken[@]} -eq 0 ]]; then
  echo "[OK] docs/rules/ と .claude/rules/ は同期されています"
  exit 0
else
  if $FIX_MODE; then
    echo "[OK] ${#missing[@]} 件のシンボリックリンクを作成、${#broken[@]} 件のリンク切れを削除しました"
    exit 0
  else
    total=$(( ${#missing[@]} + ${#broken[@]} ))
    echo "[NG] ${#missing[@]} 件のシンボリックリンクが不足、${#broken[@]} 件のリンク切れがあります（合計 ${total} 件）"
    echo "自動修正するには: ./tools/check_rules_sync.sh --fix"
    exit 1
  fi
fi
