#!/bin/bash
# Stop hook: PR作成フロー未実行チェック
# push済みブランチにPRがなければClaude に通知する
#
# 【Issue #543】クラウド（CLAUDE_CODE_REMOTE=true）ではハーネスから PR の有無を判定できず
# （L-114）、feature ブランチ上では PR の有無に関わらず毎ターン「📋 PR 存在確認をお願いします」
# で差し戻していた。post-pr-confirm-mark.sh が「Claude が実際に PR 存在を確認済み」を
# PostToolUse で観測しセッションローカルのマーカーを立てるので、クラウド分岐に入る前に
# そのマーカーの有無を確認し、あれば無条件で通す（同じ確認を毎ターン繰り返させない）。
# マーカーはセッション + ブランチ単位でしか作られないため、他セッション・他ブランチの
# マーカーで誤って抑止することはない（L-103 防御は維持。ローカル経路の gh api 実確認は無変更）。
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_block.sh
source "$HOOK_DIR/lib/hook_block.sh"
# shellcheck source=lib/hook_layer1_common.sh
source "$HOOK_DIR/lib/hook_layer1_common.sh"

# PR 確認済みマーカーのパスを組み立てる（本体・自己テスト共用の純関数）。
# marker_dir は呼び出し側が解決した値をそのまま受け取る（本関数は git 操作をしない）。
pr_confirm_marker_path() {
  # 実体は lib（書き込み側 post-pr-confirm-mark.sh と同一関数を共有する）
  hook_pr_confirm_marker_path "$1" "$2" "$3"
}

# ── 自己テスト（hook_branch_key のサニタイズ + マーカーパス組み立ての単体テスト）────────
run_self_test() {
  local pass=0 fail=0
  _bk() { # $2 は期待する正規表現（ハッシュ成分は固定値で比較しない）
    local desc="$1" want="$2" input="$3" got
    got=$(hook_branch_key "$input")
    if [[ "$got" =~ $want ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }
  _bk_distinct() {
    local desc="$1" a="$2" b="$3" ka kb
    ka=$(hook_branch_key "$a"); kb=$(hook_branch_key "$b")
    if [[ "$ka" != "$kb" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: 同一キー $ka"
    fi
  }
  _bk "通常のブランチ名（スラッシュ含む）はサニタイズ接頭辞 + 12 桁ハッシュになる" "^featx-[0-9a-f]{12}$" "feat/x"
  _bk "ドット・アンダースコア・ハイフンは許可文字として残る" "^feat_x-1\.2-[0-9a-f]{12}$" "feat_x-1.2"
  _bk "危険文字（; とスペース）は除去される" "^featxrm-rf-[0-9a-f]{12}$" 'feat/x; rm -rf'
  _bk "許可文字ゼロのブランチ名でも空にならない" "^branch-[0-9a-f]{12}$" "認証"
  _bk_distinct "除去対象文字だけが異なるブランチは別キーになる（衝突防止）" "feat/認証機能" "feat/決済機能"
  _bk_distinct "スラッシュ位置だけが異なるブランチは別キーになる" "fe/atx" "feat/x"

  _mp() {
    local desc="$1" want="$2" sid="$3" branch="$4" dir="$5" got
    got=$(pr_confirm_marker_path "$sid" "$branch" "$dir")
    if [[ "$got" == "$want" ]]; then
      pass=$((pass + 1))
    else
      fail=$((fail + 1))
      echo "  ✗ $desc: want=$want got=$got"
    fi
  }
  _mp "通常ケース" "/tmp/mdir/claude-pr-confirmed-sess123-$(hook_branch_key feat/x)" "sess123" "feat/x" "/tmp/mdir"
  _mp "session_id と branch が異なればパスも異なる" "/tmp/mdir/claude-pr-confirmed-sessB-$(hook_branch_key other/branch)" "sessB" "other/branch" "/tmp/mdir"

  # マーカーの往復テスト（実 git dir を汚さない一時ディレクトリで実施）
  local tmp_dir marker
  tmp_dir=$(mktemp -d 2>/dev/null || echo "")
  if [[ -n "$tmp_dir" ]]; then
    marker=$(pr_confirm_marker_path "sessX" "feat/y" "$tmp_dir")
    [[ ! -f "$marker" ]] \
      && pass=$((pass + 1)) \
      || { fail=$((fail + 1)); echo "  ✗ マーカー未作成時点で存在してしまっている"; }
    : > "$marker"
    [[ -f "$marker" ]] \
      && pass=$((pass + 1)) \
      || { fail=$((fail + 1)); echo "  ✗ マーカー touch 後にファイルが見つからない"; }
    rm -rf "$tmp_dir"
  fi

  echo "[stop-pr-check --self-test] PASS=$pass FAIL=$fail"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit $?
fi

input=$(cat)

# 再帰防止
stop_hook_active=$(echo "$input" | jq -r '.stop_hook_active // "false"')
if [[ "$stop_hook_active" == "true" ]]; then exit 0; fi

# git リポジトリでなければスキップ
if ! git rev-parse --git-dir >/dev/null 2>&1; then exit 0; fi
# マーカーの読み書きは post-pr-confirm-mark.sh と同じ基準ディレクトリで解決する（他フックと同じ防御）
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0

current_branch=$(git branch --show-current)

# main / 空 はスキップ（slug 導出より前に判定し、main では slug 警告を出さない）
if [[ -z "$current_branch" ]] || [[ "$current_branch" == "main" ]]; then exit 0; fi

# リポジトリ slug（owner/repo）を動的に導出する。
# 雛形プレースホルダ kai-kou/btc-realtime-chart をハードコードすると、bootstrap で置換し忘れた
# プロジェクトで PR チェックが機能しない（実際に発生・L-103 再発の温床）。
# 優先順: GITHUB_REPOSITORY → gh repo view → origin URL パース。
REPO_SLUG="${GITHUB_REPOSITORY:-}"
# クラウドでは gh repo view が 403（GraphQL・L-114）のため試行せず origin URL パースへ進む
if [[ -z "$REPO_SLUG" ]] && [[ "${CLAUDE_CODE_REMOTE:-}" != "true" ]] && command -v gh >/dev/null 2>&1; then
  REPO_SLUG=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null || echo "")
fi
if [[ -z "$REPO_SLUG" ]]; then
  origin_url=$(git remote get-url origin 2>/dev/null || echo "")
  if [[ -n "$origin_url" ]]; then
    # http(s)://.../<owner>/<repo>(.git) / git@host:<owner>/<repo>(.git) の両形式に対応
    REPO_SLUG=$(printf '%s' "$origin_url" | sed -E 's#(\.git)?/?$##; s#.*[:/]([^/]+/[^/]+)$#\1#')
  fi
fi
# owner/repo 形式に解決できなければ、断定せず「判定不能」警告で明示停止する（不正 API パス
# repos//pulls を組み立てない・サイレント素通りも防ぐ）。
# owner にドットを含むものも弾く（GitHub の owner 名にドットは不可。`host/repo` の単一セグメント
# URL を `github.com/single` 等と誤パースした場合を検知する）。
if [[ -z "$REPO_SLUG" || "$REPO_SLUG" != */* || "${REPO_SLUG%%/*}" == *.* ]]; then
  hook_block "⚠️ PR確認できません: リポジトリ名（owner/repo）を自動検出できませんでした（GITHUB_REPOSITORY 未設定・origin 不正のいずれか）。\`git remote -v\` で origin を確認したうえで、mcp__github__list_pull_requests（クラウド一次経路）または \`gh pr list --head ${current_branch} --state all\`（ローカル）で PR が作成されているか確認してください。"
fi
REPO_OWNER="${REPO_SLUG%%/*}"

# 検証手段の案内文を環境で切り替える。クラウド（CLAUDE_CODE_REMOTE=true）では gh の repo スコープ
# 操作が egress プロキシに 403 でブロックされるため、`gh pr list` を案内しても機能しない（L-114）。
# 公式 MCP（mcp__github__list_pull_requests）を案内する。
if [[ "${CLAUDE_CODE_REMOTE:-}" == "true" ]]; then
  VERIFY_HINT="mcp__github__list_pull_requests(owner=\"${REPO_OWNER}\", repo=\"${REPO_SLUG#*/}\", head=\"${REPO_OWNER}:${current_branch}\", state=\"all\") で PR を確認してください（クラウドでは gh の repo 操作が 403 でブロックされます・L-114）"
else
  VERIFY_HINT="\`gh pr list --head ${current_branch} --state all -R ${REPO_SLUG}\` を手動実行して PR が作成されているか確認してください"
fi

# リモートブランチの存在確認
# branch_check_status: "exists" | "not_found" | "unknown"
# "unknown" = timeout/認証/ネットワーク等で判定不能 → PR チェックに進む（サイレントスキップしない）
branch_check_status="unknown"

git_ls_exit=0
timeout 10s git ls-remote --exit-code --heads origin -- "$current_branch" >/dev/null 2>&1 \
  || git_ls_exit=$?

if [[ $git_ls_exit -eq 0 ]]; then
  branch_check_status="exists"
elif [[ $git_ls_exit -eq 2 ]]; then
  # --exit-code: exit 2 = マッチする ref なし = ブランチが存在しない（ネットワークは正常）
  branch_check_status="not_found"
else
  # 判定不能（timeout/認証/ネットワーク等） → gh api フォールバック（ローカル実行専用）
  # ブランチ名に / を含む場合のためURL エンコードを適用。
  # クラウドでは gh 自体が未導入で repo スコープ REST も 403 のため試行しない（L-114 / #342）。
  if [[ "${CLAUDE_CODE_REMOTE:-}" != "true" ]] && command -v gh >/dev/null 2>&1; then
    branch_api_result=$(timeout 10s gh api \
      "repos/${REPO_SLUG}/branches/$(printf -- '%s' "$current_branch" | jq -Rr @uri)" \
      --jq '.name' 2>/dev/null || echo "")
    if [[ "$branch_api_result" == "$current_branch" ]]; then
      branch_check_status="exists"
    fi
  fi
  # gh 未導入・gh api が空を返した場合（404/timeout/認証エラー）→ unknown のまま
  # PR チェック側に判断を委ねる
fi

# ブランチが存在しないことが確定した場合のみスキップ
# unknown（両方失敗）はサイレントスキップせず PR チェックに進む（L-050 対策）
if [[ "$branch_check_status" == "not_found" ]]; then exit 0; fi

# --- クラウド: PR 存在確認は Claude が MCP で行う（ハーネスからは判定できない・L-114 / #342）---
# クラウドではフックから MCP を呼べず、gh も未導入・repo スコープ REST も 403 のため、
# ハーネス側で PR の有無を判定する手段が存在しない。これは障害ではなく既定の運用なので、
# 「確認できません」という異常表現ではなく Claude への実行指示として渡す。
#
# 【Issue #543】その差し戻しに入る前に「このセッション + このブランチで PR 存在確認済み」
# マーカー（post-pr-confirm-mark.sh が立てる）を確認する。あれば毎ターン同じ確認を
# 求めず無条件で通す。マーカーが無い（=未確認 or 別セッション/別ブランチ）場合のみ
# 従来どおり差し戻す。
if [[ "${CLAUDE_CODE_REMOTE:-}" == "true" ]]; then
  pr_confirm_session_id=$(hook_extract_session_id "$input" || echo "")
  if [[ -n "$pr_confirm_session_id" ]]; then
    pr_confirm_marker_dir="${CLAUDE_HOOK_PR_MARKER_DIR:-$(git rev-parse --git-dir 2>/dev/null || echo "")}"
    if [[ -n "$pr_confirm_marker_dir" ]]; then
      pr_confirm_marker=$(pr_confirm_marker_path "$pr_confirm_session_id" "$current_branch" "$pr_confirm_marker_dir")
      [[ -f "$pr_confirm_marker" ]] && exit 0
    fi
  fi
  hook_block "📋 PR 存在確認をお願いします（クラウドではハーネスから判定できない仕様。gh の導入では解決しません）: ${VERIFY_HINT}
- PR が既にある場合: 確認結果（PR 番号・state）を踏まえてそのまま終了してよい
- PR が無い場合: pr-review-flow.md に従いセルフレビュー → PR 作成まで進める"
fi

# --- 以下はローカル実行専用（gh が GitHub に直接到達できる環境）---
# PR存在チェック: gh api で確認（timeout付き・リトライ付き）
# --method GET を明示指定（-f フラグ使用時のデフォルト POST を回避）
# state=all + jq フィルタ: open PR と merged PR のみカウント（closed/abandoned PR は除外）

# ローカルで gh が未導入の場合は実行可能な代替手段を案内して終了。
# 固定文言「gh をインストールしてください」だけでは実行不能なため GitHub UI も併記する（#313 / #318）。
if ! command -v gh >/dev/null 2>&1; then
  hook_block "⚠️ PR確認できません: gh が未導入のため PR 存在確認ができません。gh をインストールするか GitHub UI（https://github.com/${REPO_SLUG}/pulls）でブランチ ${current_branch} の PR を確認してください。作成されていない場合はpr-review-flow.mdに従いPRを作成してください。"
fi

total="unknown"
# ローカル実行では gh が GitHub に直接到達できるため repo スコープ REST で実確認する。
# 失敗時は結果が空になり unknown 分岐へ落ちる（サイレント素通りしない・安全側維持）。
for attempt in 1 2; do
  gh_err=$(mktemp)
  result=$(timeout 15s gh api --method GET "repos/${REPO_SLUG}/pulls" \
    -f head="${REPO_OWNER}:${current_branch}" -f state=all -f per_page=100 \
    --jq '[.[] | select(.state == "open" or .merged_at != null)] | length' 2>"$gh_err" || echo "")
  if [[ "$result" =~ ^[0-9]+$ ]]; then
    rm -f "$gh_err"
    total="$result"
    break
  fi
  # 4xx（プロキシ 403 回帰・権限不足等）は決定的失敗なのでリトライしない（即 unknown 分岐へ）
  if grep -qE 'HTTP 4[0-9][0-9]' "$gh_err" 2>/dev/null; then
    rm -f "$gh_err"
    break
  fi
  rm -f "$gh_err"
  [[ $attempt -lt 2 ]] && sleep 2
done

if [[ "$total" == "0" ]]; then
  if [[ "$branch_check_status" == "exists" ]]; then
    # ブランチの存在が確定している場合のみ "push済み" と断定する
    hook_block "⚠️ PR未作成警告: ブランチ ${current_branch} はリモートにpush済みですが、PRがまだ作成されていません。pr-review-flow.md に従い、セルフレビュー → PR作成 → AIレビュー依頼 → レビュー監視を実行してください。

【根本原因対策 L-050】PR作成を報告する前に必ずPR URLを確認してください。"
  else
    # branch_check_status == "unknown": ブランチpush状態が確認できないため断定を避ける
    hook_block "⚠️ PR確認できません: ブランチ ${current_branch} のブランチ存在確認でエラー（timeout/認証/ネットワーク等）が発生したため、PR未作成かどうか断定できません。${VERIFY_HINT}。作成されていない場合はpr-review-flow.mdに従いPRを作成してください。"
  fi
elif [[ "$total" == "unknown" ]]; then
  # 判定不能時（timeout/認証/レート制限/ネットワーク等）はサイレントスキップせず警告を出す（L-050 対策）
  hook_block "⚠️ PR確認できません: ブランチ ${current_branch} のPR存在確認でエラー（timeout/認証/レート制限/ネットワーク等）が発生しました。${VERIFY_HINT}。作成されていない場合はpr-review-flow.mdに従いPRを作成してください。"
fi

exit 0
