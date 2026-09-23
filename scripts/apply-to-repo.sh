#!/usr/bin/env bash
# apply-to-repo.sh — kai-kou/claude-code-repository-base のルール・スキル定義・ハーネスを
# 「任意の既存リポジトリ」へワンコマンドで適用（または最新へ同期）する。
#
# これまで他リポジトリで毎回手動指示していた
#   「gh で kai-kou/claude-code-repository-base を参照し、ルール・スキル・ハーネスを全部適用して」
# を 1 コマンドに置き換えるためのスクリプト。
#
# 使い方（対象リポジトリのルートで実行）:
#   # A. リモートから直接（最も手軽。git だけで動く）
#   curl -fsSL https://raw.githubusercontent.com/kai-kou/claude-code-repository-base/main/scripts/apply-to-repo.sh | bash
#
#   # B. ローカルに置いて実行（オプション付き）
#   bash scripts/apply-to-repo.sh [options]
#
# 主なオプション:
#   --base owner/repo       ベースリポジトリ（既定: kai-kou/claude-code-repository-base）
#   --ref  <branch|tag|sha> 取得する ref（既定: main）
#   --repo owner/repo       対象リポジトリ slug（既定: git remote origin から自動判定）
#   --name "Project Name"   プロジェクト名（プレースホルダ置換用・既定: リポジトリ名）
#   --desc "説明"           プロジェクト説明（既定: プロジェクト名）
#   --tz   Asia/Tokyo       タイムゾーン
#   --prune                 modules.yaml で enabled:false のモジュール資産を除去
#   --overwrite-project     CLAUDE.md / docs/project-mission.md も上書きする（既定: 既存があれば保護）
#   --keep-settings         .claude/settings.json を上書きしない（既定: バックアップしてから導入）
#   --check-updates         適用せず、前回適用時点からのアップデート内容だけ表示する
#   --no-merge              祖先を使った 3 方向マージを行わず、全ファイルを無条件上書きする
#                           （旧来の挙動。マージ機構に問題が出たときの退避経路）
#   --dry-run               実際にはコピーせず、適用対象を表示するだけ
#   -h | --help             ヘルプ表示
#
# 設計方針:
#   - 既存リポジトリのプロジェクト固有ファイル（CLAUDE.md / docs/project-mission.md）は
#     既定では上書きしない（look before overwrite）。--overwrite-project で明示的に上書き可能。
#   - .claude/settings.json はハーネス本体のため導入するが、既存があれば .bak に退避する。
#   - 何度でも再実行でき、最新のルール・スキル・ハーネスへ同期できる（idempotent）。
#   - SYNC_PATHS 配下も無条件上書きはしない。前回適用したベースの SHA
#     （.claude/base-sync-state.json）を祖先として、ファイルごとに
#       ① ベース側が前回から無変更 → 触らない（下流の変更をそのまま保つ）
#       ② 下流が祖先のまま        → ベース最新で上書き（fast-forward）
#       ③ 両側が変更              → 3 方向マージ（tools/merge_three_way.py）
#       ④ 衝突・検証失敗・祖先なし → 下流を温存し、ベース最新を <path>.base-latest に併置
#     と振り分ける。衝突マーカーはワークツリーに一切書かない（壊れた settings.json で
#     下流のセッションが起動不能になる／ルールが壊れた形で読まれる、という失敗を構造的に断つ）。
set -euo pipefail

# 自己上書き対策（Issue #33）: SYNC_PATHS が scripts/ を含むため、`bash scripts/apply-to-repo.sh`
# の形（オプション B）でローカル実行すると、同期処理が実行中のこのファイル自身を書き換え、
# bash の逐次読み込み位置とファイル上のバイトオフセットがずれて構文エラーで異常終了する（実機発生）。
# 未 re-exec かつ実ファイルから起動された場合（`curl | bash` 等の stdin 実行では BASH_SOURCE[0] が
# 実ファイルにならないため対象外）は、自分自身を一時コピーへ退避してそこから再実行する。
if [ -z "${_APPLY_TO_REPO_REEXEC:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
  SELF_TMP_DIR="$(mktemp -d)" || { echo "自己コピー用の一時ディレクトリ作成に失敗しました" >&2; exit 1; }
  SELF_TMP="$SELF_TMP_DIR/apply-to-repo.sh"
  cp -- "${BASH_SOURCE[0]:-}" "$SELF_TMP"
  chmod +x "$SELF_TMP"
  export _APPLY_TO_REPO_REEXEC=1
  export _APPLY_TO_REPO_REEXEC_TMPDIR="$SELF_TMP_DIR"
  exec bash "$SELF_TMP" "$@"
fi

BASE_REPO="kai-kou/claude-code-repository-base"
REF="main"
TARGET_SLUG=""
PROJECT_NAME=""
PROJECT_DESC=""
PROJECT_TZ=""
PRUNE=false
OVERWRITE_PROJECT=false
KEEP_SETTINGS=false
CHECK_UPDATES=false
DRY_RUN=false
NO_MERGE=false
TARGET="$(pwd)"

log() { echo "[apply] $*"; }
die() { echo "[apply][ERROR] $*" >&2; exit 1; }

# 値を取る引数で値が省略された場合（set -u 下で $2 未定義クラッシュ）を防ぐ
need_arg() { [ "$1" -ge 2 ] || die "$2 には引数が必要です"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) need_arg "$#" "--base"; BASE_REPO="$2"; shift 2;;
    --ref)  need_arg "$#" "--ref";  REF="$2"; shift 2;;
    --repo) need_arg "$#" "--repo"; TARGET_SLUG="$2"; shift 2;;
    --name) need_arg "$#" "--name"; PROJECT_NAME="$2"; shift 2;;
    --desc) need_arg "$#" "--desc"; PROJECT_DESC="$2"; shift 2;;
    --tz)   need_arg "$#" "--tz";   PROJECT_TZ="$2"; shift 2;;
    --prune) PRUNE=true; shift;;
    --overwrite-project) OVERWRITE_PROJECT=true; shift;;
    --keep-settings) KEEP_SETTINGS=true; shift;;
    --check-updates) CHECK_UPDATES=true; shift;;
    --dry-run) DRY_RUN=true; shift;;
    --no-merge) NO_MERGE=true; shift;;
    -h|--help)
      sed -n '2,40p' "$0" 2>/dev/null || echo "apply-to-repo.sh: see header for usage"
      exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 1;;
  esac
done

# --base / --ref の早期検証。値はマーカー JSON（ヘレドク）と clone URL に埋め込まれるため、
# 引用符・バックスラッシュ・空白が混入すると JSON が壊れ次回の json_field が誤パースする。
BASE_REPO="${BASE_REPO%.git}"   # `owner/repo.git` 表記を正規化（マーカーとの比較ゆれ防止）
case "$BASE_REPO" in
  */*) : ;;
  *) die "--base は owner/repo 形式で指定してください: $BASE_REPO";;
esac
case "${BASE_REPO}${REF}" in
  *'"'*|*'\'*|*' '*|*'	'*) die "--base / --ref に引用符・バックスラッシュ・空白は使えません";;
esac

# --- 0. 対象リポジトリの検証 ---
# git は必須（clone・slug 判定・symlink 同期で多用する）
if ! command -v git >/dev/null 2>&1; then
  die "git がインストールされていません。本スクリプトの実行には git が必須です"
fi
# worktree / submodule では .git がファイルのため、rev-parse で堅牢に判定する
if ! git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "カレントディレクトリは git リポジトリではありません: $TARGET"
fi
# リポジトリのルートで実行することを強制する（サブディレクトリ実行を防ぐ）
if [ -n "$(git -C "$TARGET" rev-parse --show-cdup 2>/dev/null)" ]; then
  die "対象リポジトリのルートディレクトリで実行してください: $TARGET"
fi

# --- 0.5 運用フォーク検出ゲート（実行時の最後の防衛線・wiki-hub #87/#89）---
# 2 信号 AND: sync-upstream.sh 有（wiki-hub 系で upstream 追従経路がある）かつ
# publish-template.sh 無（dev リポではない）= wiki-hub 運用フォーク。
# 運用フォークの正しい更新元は claude-wiki-hub（sync-upstream）であり、本ベースを
# 被せるのは誤り。旧ハーネスのフォークがスキル誤ルーティングで本スクリプトへ到達しても、
# 本体は毎回 fresh に取得されるためこのゲートが必ず評価される（最後の防衛線）。
if [ -f "$TARGET/scripts/sync-upstream.sh" ] && [ ! -f "$TARGET/scripts/publish-template.sh" ]; then
  echo "[apply][ERROR] このリポジトリは wiki-hub 運用フォーク（operational fork）と判定されました。" >&2
  echo "  claude-code-base の直接適用は対象外です（upstream は claude-wiki-hub）。" >&2
  echo "  アップデートの取り込み: bash scripts/sync-upstream.sh --yes" >&2
  echo "  （claude-wiki-hub の最新ハーネスを取り込みます。取り込み後は「アップデートを取り込んで」の発話で更新できます）" >&2
  echo "  この判定が誤りの場合は kai-kou/claude-code-repository-base に Issue を立ててください。" >&2
  exit 1
fi

# --- 1. 対象リポジトリ slug の自動判定 ---
if [ -z "$TARGET_SLUG" ]; then
  remote_url="$(git -C "$TARGET" remote get-url origin 2>/dev/null || true)"
  if [ -n "$remote_url" ]; then
    # https / ssh / プロキシ形式すべてから末尾の owner/repo を抽出（.git 除去）
    TARGET_SLUG="$(printf '%s' "$remote_url" \
      | sed -E 's#\.git$##' \
      | sed -E 's#^.*[/:]([^/]+/[^/]+)$#\1#')"
  fi
fi
[ -n "$TARGET_SLUG" ] || die "対象リポジトリの slug を判定できません。--repo owner/repo を指定してください"
TARGET_NAME="${TARGET_SLUG##*/}"
PROJECT_NAME="${PROJECT_NAME:-$TARGET_NAME}"

log "ベース   : $BASE_REPO@$REF"
log "対象     : $TARGET_SLUG ($TARGET)"
log "name     : $PROJECT_NAME"
$DRY_RUN && log "*** DRY-RUN モード（コピーは行いません）***"

# --- 2. ベースの取得（git が一次経路。gh はローカル互換のフォールバック）---
TMP="$(mktemp -d)"
# re-exec 時の自己コピー用一時ディレクトリ（$_APPLY_TO_REPO_REEXEC_TMPDIR）も
# 同じ trap でまとめて削除する（同一シグナルの trap は後勝ちで上書きされ累積しないため）。
trap 'rm -rf "$TMP" "${_APPLY_TO_REPO_REEXEC_TMPDIR:-}"' EXIT
CLONE_DIR="$TMP/base"

fetch_base() {
  # git を一次経路にする（git は GitHub API プロキシとは別系統で常時生存し、
  # クラウドでは gh がプリインストールされないのが既定・L-114）
  log "git でベースを取得します"
  local url="https://github.com/${BASE_REPO}.git"
  if git clone --depth 1 --branch "$REF" "$url" "$CLONE_DIR" >/dev/null 2>&1; then
    return 0
  fi
  # ref がタグ/SHA でブランチ clone が失敗した場合。
  # fetch/checkout が失敗したら die する（意図しない default ブランチ適用を防ぐ）
  rm -rf "$CLONE_DIR"
  if git clone --depth 1 "$url" "$CLONE_DIR" >/dev/null 2>&1; then
    git -C "$CLONE_DIR" fetch --depth 1 origin "$REF" >/dev/null 2>&1 \
      || die "指定された ref ($REF) のフェッチに失敗しました"
    git -C "$CLONE_DIR" checkout -q FETCH_HEAD 2>/dev/null \
      || die "指定された ref ($REF) のチェックアウトに失敗しました"
    return 0
  fi
  # git が通らない環境（ローカルで gh 認証のみ通っている等）の互換フォールバック。
  # クラウドでは実 gh が無いためシムが即エラーを返し、次の die に落ちる
  rm -rf "$CLONE_DIR"
  if command -v gh >/dev/null 2>&1; then
    log "git での取得に失敗したため gh を試します"
    if gh repo clone "$BASE_REPO" "$CLONE_DIR" -- --depth 1 --branch "$REF" >/dev/null 2>&1; then
      return 0
    fi
    rm -rf "$CLONE_DIR"
    if gh repo clone "$BASE_REPO" "$CLONE_DIR" -- --depth 1 >/dev/null 2>&1; then
      git -C "$CLONE_DIR" fetch --depth 1 origin "$REF" >/dev/null 2>&1 \
        || die "指定された ref ($REF) のフェッチに失敗しました"
      git -C "$CLONE_DIR" checkout -q FETCH_HEAD 2>/dev/null \
        || die "指定された ref ($REF) のチェックアウトに失敗しました"
      return 0
    fi
  fi
  die "ベースの取得に失敗しました（$BASE_REPO@$REF）。ref と、クラウドなら対象リポジトリがセッションに attach されているかを確認してください（git 経路が一次・gh はクラウドでは未インストールが既定）"
}
fetch_base
[ -d "$CLONE_DIR/.claude" ] || die "取得したベースに .claude/ がありません。--base / --ref を確認してください"

# --- 適用対象の定義（show_updates のノイズ判定でも参照するためここで定義）---
# 常時同期（最新で上書き・更新）: ルール本体 / ハーネス / スキル / ツール / 設定雛形
SYNC_PATHS=(
  "docs/rules"
  ".claude/rules"
  ".claude/hooks"
  ".claude/skills"
  ".claude/agents"
  ".claude/output-styles"
  ".claude/commands"
  # .claude-plugin/ はディレクトリ丸ごとにしない。marketplace.json（本ベースを配布するための
  # マーケットプレイス定義）を下流へ配ると、下流リポジトリが「claude-code-base を配布する
  # マーケットプレイス」を名乗ってしまう。下流に要るのは plugin.json の雛形だけ。
  ".claude-plugin/plugin.json"
  "tools"
  "scripts"
  "modules.yaml"
  ".mcp.json"
  "requirements.txt"
  # config/ は性質ごとに個別指定する（ディレクトリ丸ごとにしない・Issue #448）。
  # 丸ごと同期すると state ファイルまで上書きされ、下流の実行状態がベースの値へ巻き戻る。
  "config/claude_code_spec_sync.yaml"    # tools/check_claude_code_updates.py が起動時に読む。
                                         # 既定値フォールバックが無く、不在だとロード自体が失敗する
  "config/broker_workflows.json.example" # プレースホルダのみの雛形。実ファイル（.json）は下流が作る
)
# 既存があれば保護（プロジェクト固有・--overwrite-project で上書き）。
# 既存がなければ配置し、既存があれば維持してベース版を `<path>.base` として並置する。
PROTECT_PATHS=(
  "CLAUDE.md"
  "docs/project-mission.md"
  # code-review スキルが Step 1 / 2 で注入するレビュー較正（Issue #627）。severity 定義・Nit 上限・
  # repo 固有チェックは下流が成果物種別に合わせて書き換えるため、既存があれば保護する。
  "REVIEW.md"
  # config/ のうち「下流が追記して拡張する」契約をファイル自身が明記しているもの（Issue #448）。
  # SYNC 側に置くと cp -a の無条件上書きで下流の追記が必ず失われる。
  "config/publish_events.yaml"
  "config/data_only_path_prefixes.txt"
  "config/pr_review_comment_categories.json"
  # 子固有資産の台帳（modules.yaml は SEMANTIC_MERGE_PATHS で毎回ベース最新版に置き換わり、
  # 子だけが持つ新規モジュールエントリを復元する処理が無いため代替を要る・Issue #2 / FR-8）。
  "local-modules.yaml"
)
# かつて配布していたが、いまは配布しないパス（1 回限りの移行削除）。
# SYNC_PATHS から外しただけでは、過去の適用で下流へ渡ったファイルが永久に残る。
# 例: .claude-plugin/marketplace.json は「本ベースを配布するマーケットプレイス定義」であり、
# 下流に残ると下流リポジトリが claude-code-base を配布するマーケットプレイスを名乗ってしまう。
REMOVE_PATHS=(
  ".claude-plugin/marketplace.json"
)

# 意図的に配布しない config/: config/backlog_refinement_state.json（実行状態そのもの。
# 配ると新規下流に著者環境の last_refinement_at が初期値として入り、週次ゲートが
# 初回から誤って「実行済み」と判定する。読み手は不在時に「未実行」として初回に自動生成する）

# --- 2.5 アップデート確認（前回適用マーカーとの差分表示）---
# 前回適用時に記録した .claude/base-sync-state.json（適用済みベース SHA・日時）を基準点に、
# 「前回適用〜今回」のコミット一覧と、手動手順が必要な更新（docs/base-update-notes.md）を表示する。
STATE_FILE="$TARGET/.claude/base-sync-state.json"
UPDATE_NOTES_REL="docs/base-update-notes.md"
BASE_HEAD="$(git -C "$CLONE_DIR" rev-parse HEAD 2>/dev/null || true)"

json_field() {  # $1=file $2=key（フラット JSON 前提の簡易抽出・jq 非依存）
  sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
}

have_commit() {  # $1=SHA。clone 内にコミットオブジェクトが存在するか
  git -C "$CLONE_DIR" cat-file -e "$1^{commit}" 2>/dev/null
}

downstream_paths_re() {
  # 「下流に届くパス」の先頭一致 RE を SYNC_PATHS + PROTECT_PATHS から導出する（Issue #211）。
  # 独立した除外リストを持たない＝配布対象の定義が変われば判定も自動追従し、
  # 誤除外（汎用改善をノイズ扱いする事故）が構造的に起きない。
  local re="" p
  for p in "${SYNC_PATHS[@]}" "${PROTECT_PATHS[@]}"; do
    p="${p//./\\.}"
    re="${re:+$re|}$p"
  done
  # 右境界（/ か行末）を付ける: "tools" が "tools-foo/" や "modules.yaml.bak" 等の
  # 前方一致で誤ヒットしないようにする
  printf '^(%s)(/|$)' "$re"
}

print_commit_log() {
  # コミット一覧を表示し、下流に届くパス（SYNC_PATHS/PROTECT_PATHS）を 1 つも触らない
  # コミット（telemetry・content/analytics/・content/discussions/ 等の base 内部生成物のみ）
  # に注記を付ける。非表示にはしない（誤判定時も情報が失われない・表示のみのタグ付け）。
  local commit_log="$1" dre noise=0 line sha
  dre="$(downstream_paths_re)"
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    sha="${line%% *}"
    if git -C "$CLONE_DIR" diff-tree --no-commit-id --name-only -r "$sha" 2>/dev/null | grep -qE "$dre"; then
      printf '[apply]   %s\n' "$line"
    else
      printf '[apply]   %s ※base内部生成物のみ・下流影響なし\n' "$line"
      noise=$((noise + 1))
    fi
  done <<EOF
$commit_log
EOF
  if [ "$noise" -gt 0 ]; then
    log "（※付き ${noise} 件は同期対象パス外のみの変更＝逆輸入・精査は不要）"
  fi
}

show_updates() {
  local prev_sha="" prev_date="" prev_base="" prev_ref=""
  if [ -f "$STATE_FILE" ]; then
    prev_sha="$(json_field "$STATE_FILE" commit)"
    prev_date="$(json_field "$STATE_FILE" applied_at | cut -c1-10)"
    prev_base="$(json_field "$STATE_FILE" base_repo)"
    prev_ref="$(json_field "$STATE_FILE" ref)"
  fi
  # applied_at の欠落・値破損を日付比較に流すと「偽の『更新なし』表示」になるため、
  # 使用前に形式検証して不正なら空に正規化する（後段で明示警告に倒す・サイレントスキップ禁止）
  case "$prev_date" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) : ;;
    *) prev_date="" ;;
  esac
  echo ""
  log "── アップデート確認（$BASE_REPO@$REF = ${BASE_HEAD:0:7}）──"
  if [ -z "$prev_sha" ]; then
    log "前回適用マーカー（.claude/base-sync-state.json）なし: 初回適用として扱います"
    log "（適用完了時にマーカーを作成し、次回からアップデート一覧を表示します）"
    return 0
  fi
  # ベース切替（--base 変更）は履歴の連続性がないため初回適用相当に落とす。
  # prev_base 空（旧形式マーカー）は切替と誤判定しない。ref の差は SHA 比較が引き続き
  # 有効（タグ⇄ブランチの正当な切替あり）なので情報行のみで続行する。
  if [ -n "$prev_base" ] && [ "$prev_base" != "$BASE_REPO" ]; then
    log "ベース切替を検出（前回: $prev_base → 今回: $BASE_REPO）: 前回の履歴とは比較できないため初回適用として扱います"
    return 0
  fi
  if [ -n "$prev_ref" ] && [ "$prev_ref" != "$REF" ]; then
    log "参考: ref が前回と異なります（$prev_ref → $REF）。コミット比較は SHA ベースのため続行します"
  fi
  if [ "$prev_sha" = "$BASE_HEAD" ]; then
    log "前回適用（${prev_sha:0:7}・$prev_date）から変更なし"
  else
    # 浅い clone を深掘りして前回 SHA まで辿る（見つからなければ一覧は省略）
    if ! have_commit "$prev_sha"; then
      git -C "$CLONE_DIR" fetch --deepen 500 origin >/dev/null 2>&1 \
        || log "（履歴の深掘りフェッチに失敗しました。ネットワーク要因の可能性があります）"
    fi
    if have_commit "$prev_sha"; then
      log "前回適用（${prev_sha:0:7}・${prev_date:-日時不明}）以降の更新コミット:"
      local commit_log
      commit_log="$(git -C "$CLONE_DIR" log --oneline --no-decorate --no-merges "${prev_sha}..HEAD")"
      if [ -n "$commit_log" ]; then
        print_commit_log "$commit_log"
      else
        log "（一覧なし: マージコミットのみ、または ref が前回適用より古い（巻き戻し）可能性。$UPDATE_NOTES_REL で更新内容を確認してください）"
      fi
    else
      log "前回適用コミット（${prev_sha:0:7}）が取得範囲（--deepen 500）に見つからず、コミット一覧は省略します"
      log "（force-push 等で失われた可能性もあります。$UPDATE_NOTES_REL の日付（前回適用: $prev_date 以降）で更新内容を確認してください）"
    fi
  fi
  # 手動手順が必要な更新（UPDATE NOTES）: 前回適用日以降のエントリを抜粋
  if [ -f "$CLONE_DIR/$UPDATE_NOTES_REL" ]; then
    if [ -z "$prev_date" ]; then
      log "マーカーの applied_at が欠落または不正なため、手動手順が必要な更新の抜粋を省略します"
      log "（ベースの $UPDATE_NOTES_REL を全文確認してください）"
    else
      local notes malformed
      # 記載ルール上、エントリは最初の --- 区切り以降に置かれる。日付形式でない
      # `## ` 見出しは抽出から漏れる（サイレント脱落）ため、件数を検出して警告する
      malformed="$(LC_ALL=C awk '
        BEGIN { entries = 0; bad = 0 }
        /^---[[:space:]]*$/ { entries = 1 }
        entries && /^## / && $0 !~ /^## [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/ { bad++ }
        END { print bad }' "$CLONE_DIR/$UPDATE_NOTES_REL")"
      if [ "${malformed:-0}" -gt 0 ]; then
        log "⚠ $UPDATE_NOTES_REL に日付形式（## YYYY-MM-DD）でないエントリ見出しが ${malformed} 件あります（抽出から漏れます。全文を確認してください）"
      fi
      notes="$(LC_ALL=C awk -v d="$prev_date" '
        BEGIN { show = 0 }
        /^## [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/ { show = (substr($2, 1, 10) >= d) }
        show' "$CLONE_DIR/$UPDATE_NOTES_REL")"
      if [ -n "$notes" ]; then
        echo ""
        log "── 手動手順が必要な更新（$UPDATE_NOTES_REL・前回適用日 $prev_date 以降）──"
        printf '%s\n' "$notes"
        log "（前回適用と同日のエントリは対応済みの場合があります。全文はベースの $UPDATE_NOTES_REL を参照）"
      else
        log "手動手順が必要な更新: なし（$UPDATE_NOTES_REL に $prev_date 以降のエントリなし）"
      fi
    fi
  fi
  echo ""
}
show_updates
if $CHECK_UPDATES; then
  log "確認のみ（--check-updates）。適用するには --check-updates を外して再実行してください。"
  exit 0
fi


# --- 3. 適用（対象パスの定義は 2.5 の直前を参照）---
# --- 3.1 祖先（前回適用したベース）の解決 ---
# show_updates() が既に必要な深掘りフェッチを済ませているので、ここでは到達性の確認だけ行う。
# 到達できない理由（マーカー無し・ベース切替・force-push・ネットワーク不通）は
# いずれも「祖先を使えない」という 1 つの結論に落ちるため、区別せず 2 方向コピーへ degrade する。
PREV_SHA=""
resolve_prev_sha() {
  $NO_MERGE && { log "3 方向マージを無効化（--no-merge）: 全ファイルを上書きします"; return; }
  [ -f "$STATE_FILE" ] || { log "前回適用マーカーが無いため、今回は上書き同期します（次回から下流の変更を保護します）"; return; }
  local sha prev_base
  sha="$(json_field "$STATE_FILE" commit)"
  prev_base="$(json_field "$STATE_FILE" base_repo)"
  if [ -n "$prev_base" ] && [ "$prev_base" != "$BASE_REPO" ]; then
    log "ベースが前回と異なるため祖先を使いません（$prev_base → $BASE_REPO）: 上書き同期します"
    return
  fi
  [ -n "$sha" ] || { log "マーカーに commit が無いため上書き同期します"; return; }
  if ! have_commit "$sha"; then
    git -C "$CLONE_DIR" fetch --deepen 500 origin >/dev/null 2>&1 || true
  fi
  if have_commit "$sha"; then
    PREV_SHA="$sha"
  else
    log "前回適用コミット（${sha:0:7}）に到達できないため上書き同期します（下流の変更は .base-latest で保護されません）"
  fi
}
if ! command -v python3 >/dev/null 2>&1; then
  log "⚠ python3 が見つからないため 3 方向マージを無効化します（全ファイル上書き）"
  NO_MERGE=true
fi
# ヘルパーはクローン側を優先する（tools/ 自体が同期対象で、適用中に差し替わるため）。
# クローン側に無い場合（ヘルパー導入前のベースを --ref で指定した等）は対象側の既存版を使う。
MERGE_HELPER="$CLONE_DIR/tools/merge_three_way.py"
[ -f "$MERGE_HELPER" ] || MERGE_HELPER="$TARGET/tools/merge_three_way.py"

# 適用サマリー用のカウンタと一覧（最後に必ず報告する）
N_SKIPPED=0; N_COPIED=0; N_MERGED=0; N_CONFLICT=0; N_DELETED=0
MERGED_LIST=""; CONFLICT_LIST=""; DELETED_LIST=""

place_file() {  # $1=src $2=dst（属性を保って配置する）
  mkdir -p "$(dirname "$2")"
  cp -a "$1" "$2"
}

# かつて modules.yaml は専用の意味マージャ（下流固有の modules.yaml 専用マージスクリプト）で
# 行ベースの 3 方向マージから除外していたが、祖先比較ガード込みの3方向マージ（Issue #509 で実測検証）は
# enabled:false / project: 値に限らずあらゆる下流カスタマイズ（独自モジュール追加・コメント等）を
# 保護でき、衝突時も他の SYNC_PATHS と同じく下流温存 + .base-latest 併置に倒れて安全なため、
# 個別救済スクリプトを畳んで通常フローへ統合した（当該スクリプトは削除済み）。

# ファイル 1 件を同期する。$1=リポジトリ相対パス
sync_file() {
  local rel="$1"
  local src="$CLONE_DIR/$rel" dst="$TARGET/$rel"
  local anc merged

  # 祖先が使えない / symlink（行ベースのマージ対象外）→ そのまま配置する
  if [ -z "$PREV_SHA" ] || [ -L "$src" ] || [ -L "$dst" ]; then
    if $DRY_RUN; then log "  ~ would place: $rel"; else place_file "$src" "$dst"; fi
    N_COPIED=$((N_COPIED + 1))
    return
  fi

  # 下流に無い場合、それが「ベースの新規ファイル」なのか「下流が意図的に消したもの」なのかは
  # 祖先を見れば分かる。CLAUDE.md は不要なルール・スキルの無効化手段として削除を案内している
  # ため、祖先に在ったものを黙って復活させない（復活させたい下流は --no-merge を使う）。
  if [ ! -e "$dst" ]; then
    if git -C "$CLONE_DIR" cat-file -e "$PREV_SHA:$rel" 2>/dev/null; then
      if $DRY_RUN; then log "  ~ would keep deleted: $rel"; fi
      N_DELETED=$((N_DELETED + 1)); DELETED_LIST="${DELETED_LIST}${rel}"$'\n'
    else
      if $DRY_RUN; then log "  ~ would place: $rel"; else place_file "$src" "$dst"; fi
      N_COPIED=$((N_COPIED + 1))
    fi
    return
  fi

  # 前回の適用で衝突したまま取り込まれていない（.base-latest が残っている）ファイルは、
  # 祖先（マーカーの SHA）が下流の実際の派生元とずれている。そのままマージすると
  # 「ベース側の変更を静かに巻き戻した内容」がクリーンマージとして採用されうるので、
  # ベース最新版で .base-latest を更新し直し、未解決として毎回報告する。
  if [ -e "$dst.base-latest" ]; then
    if cmp -s "$dst" "$dst.base-latest"; then
      # 下流が取り込み済み（内容が一致）でマーカーを消し忘れただけ。外して通常フローへ戻す
      # （消し忘れで以後ずっとベース更新が届かなくなるのを防ぐ）
      $DRY_RUN || rm -f "$dst.base-latest"
    else
      if $DRY_RUN; then
        log "  ~ would refresh unresolved: $rel.base-latest"
      else
        place_file "$src" "$dst.base-latest"
      fi
      N_CONFLICT=$((N_CONFLICT + 1)); CONFLICT_LIST="${CONFLICT_LIST}${rel}（前回からの未解決）"$'\n'
      return
    fi
  fi

  # ① ベース側が前回適用から変更していない → 触らない（下流の変更をそのまま保つ）
  if git -C "$CLONE_DIR" diff --quiet "$PREV_SHA" HEAD -- "$rel" 2>/dev/null; then
    N_SKIPPED=$((N_SKIPPED + 1))
    return
  fi

  anc="$TMP/ancestor.blob"
  if git -C "$CLONE_DIR" show "$PREV_SHA:$rel" > "$anc" 2>/dev/null; then
    # ② 下流が祖先のまま → ベース最新で上書き（fast-forward）
    if cmp -s "$anc" "$dst"; then
      if $DRY_RUN; then log "  ~ would update: $rel"; else place_file "$src" "$dst"; fi
      N_COPIED=$((N_COPIED + 1))
      return
    fi
    # ③ 両側が変更 → 3 方向マージ（クリーン + 検証通過のときだけ採用）
    merged="$TMP/merged.out"
    if [ -f "$MERGE_HELPER" ] && python3 "$MERGE_HELPER" \
        --ours "$dst" --base "$anc" --theirs "$src" --output "$merged" --path-hint "$rel" 2>/dev/null; then
      if $DRY_RUN; then log "  ~ would merge: $rel"; else place_file "$merged" "$dst"; fi
      N_MERGED=$((N_MERGED + 1)); MERGED_LIST="${MERGED_LIST}${rel}"$'\n'
      return
    fi
  fi

  # ④ 衝突・検証失敗・祖先に無い → 下流を温存し、ベース最新を併置する
  if $DRY_RUN; then
    log "  ~ would keep local, save base as $rel.base-latest"
  else
    place_file "$src" "$dst.base-latest"
  fi
  N_CONFLICT=$((N_CONFLICT + 1)); CONFLICT_LIST="${CONFLICT_LIST}${rel}"$'\n'
}

copy_path() {
  local rel="$1"
  local src="$CLONE_DIR/$rel"
  local f
  local b_skip=$N_SKIPPED b_copy=$N_COPIED b_merge=$N_MERGED b_conf=$N_CONFLICT
  if [ ! -e "$src" ]; then
    log "  - skip（ベースに無い）: $rel"
    return
  fi
  if [ -d "$src" ]; then
    # ディレクトリはファイル単位に展開して判定する（丸ごとコピーでは祖先比較ができない）
    # symlink も対象にする（.claude/rules は実体がすべて symlink のため
    # -type f だけだと丸ごと同期されなくなる）
    while IFS= read -r -d '' f; do
      sync_file "$rel/${f#"$src"/}"
    done < <(find "$src" \( -type f -o -type l \) -print0)
  else
    sync_file "$rel"
  fi
  # 内訳を出す（一律の "+" は、全件スキップや衝突でも更新されたように読めてしまう）
  log "  + $rel（更新 $((N_COPIED - b_copy)) / マージ $((N_MERGED - b_merge)) / 要確認 $((N_CONFLICT - b_conf)) / 触れず $((N_SKIPPED - b_skip))）"
}

log "── ルール・スキル・ハーネスを同期 ──"
resolve_prev_sha
# modules.yaml も通常の sync_file（guard → fast-forward → 3方向マージ → 衝突退避）に乗る。
# 下流の enabled:false / project: 値は、ベースが無変更なら guard で一切触れられず、
# ベースが変更した場合も 3 方向マージが下流の変更行をそのまま保持する（Issue #509）。
for p in "${SYNC_PATHS[@]}"; do
  copy_path "$p"
done

# 配布をやめたパスの回収（過去の適用で下流へ渡ったものを消す）
for p in "${REMOVE_PATHS[@]}"; do
  if [ -e "$TARGET/$p" ]; then
    rm -rf "$TARGET/$p"
    log "  - $p を削除（ベースが配布対象から外したため）"
  fi
done

# --- 4. .claude/settings.json（ハーネス本体）の導入 ---
SETTINGS_SRC="$CLONE_DIR/.claude/settings.json"
SETTINGS_DST="$TARGET/.claude/settings.json"
if [ -f "$SETTINGS_SRC" ]; then
  if $KEEP_SETTINGS && [ -f "$SETTINGS_DST" ]; then
    log "  - settings.json は既存を維持（--keep-settings）"
  else
    # 既存 .bak は上書きしない（再実行でオリジナル設定のバックアップを失わないため）
    if [ -f "$SETTINGS_DST" ] && [ ! -f "$SETTINGS_DST.pre-base.bak" ] && ! $DRY_RUN; then
      cp -a "$SETTINGS_DST" "$SETTINGS_DST.pre-base.bak"
      log "  ! 既存 settings.json を退避: .claude/settings.json.pre-base.bak"
    fi
    # SYNC_PATHS と同じ 4 分岐に通す（下流が足した hooks matcher・permissions・
    # sandbox 許可ホストを、ベースの更新を取り込みつつ保持するため）
    s_conf=$N_CONFLICT
    sync_file ".claude/settings.json"
    if [ "$N_CONFLICT" -gt "$s_conf" ]; then
      log "  ! 要確認: .claude/settings.json（下流を温存・.base-latest を確認してください）"
    else
      log "  + .claude/settings.json"
    fi
  fi
fi

# --- 5. プロジェクト固有ファイル（既存は保護）---
log "── プロジェクト固有ファイル ──"
for p in "${PROTECT_PATHS[@]}"; do
  src="$CLONE_DIR/$p"; dst="$TARGET/$p"
  [ -e "$src" ] || continue
  if [ -e "$dst" ] && ! $OVERWRITE_PROJECT; then
    if $DRY_RUN; then
      log "  ~ would keep existing, save template as $p.base: $p"
    else
      place_file "$src" "$dst.base"
      log "  = 既存を維持・雛形を $p.base として配置: $p"
    fi
  else
    if $DRY_RUN; then
      log "  ~ would install: $p"
    else
      place_file "$src" "$dst"
      log "  + $p"
    fi
  fi
done

print_sync_summary() {
  echo ""
  log "── 同期サマリー ──"
  if [ -n "$PREV_SHA" ]; then
    log "  祖先: ${PREV_SHA:0:7}（前回適用したベース）"
  else
    log "  祖先: 未使用（上書き同期）"
  fi
  log "  触れず（ベース側に更新なし）: $N_SKIPPED / 配置・更新: $N_COPIED / マージ: $N_MERGED / 要確認: $N_CONFLICT / 削除を尊重: $N_DELETED"
  if [ -n "$DELETED_LIST" ]; then
    log "  下流で削除済みのため復活させなかったファイル（意図した無効化ならこのままで正しい）:"
    printf '%s' "$DELETED_LIST" | while IFS= read -r x; do [ -n "$x" ] && printf '[apply]     %s\n' "$x"; done
  fi
  if [ -n "$MERGED_LIST" ]; then
    log "  下流の変更を保ったままベース更新を取り込んだファイル（コミット前に diff を確認してください）:"
    printf '%s' "$MERGED_LIST" | while IFS= read -r m; do [ -n "$m" ] && printf '[apply]     %s\n' "$m"; done
  fi
  if [ -n "$CONFLICT_LIST" ]; then
    log "  ⚠ 自動反映できず、下流のファイルを温存しました（ベース最新は <path>.base-latest に併置）:"
    printf '%s' "$CONFLICT_LIST" | while IFS= read -r c; do [ -n "$c" ] && printf '[apply]     %s\n' "$c"; done
    log "  → diff <path> <path>.base-latest で確認し、取り込んだら .base-latest を削除してください"
  fi
}

if $DRY_RUN; then
  print_sync_summary
  log "DRY-RUN 完了。--dry-run を外すと実際に適用します。"
  exit 0
fi

# --- 6. bootstrap で仕上げ（プレースホルダ置換 + symlink 同期 + 任意 prune）---
log "── 仕上げ（プレースホルダ置換 + symlink 同期）──"
BOOTSTRAP="$TARGET/scripts/bootstrap.sh"
if [ -f "$BOOTSTRAP" ]; then
  args=(--repo "$TARGET_SLUG" --name "$PROJECT_NAME")
  [ -n "$PROJECT_DESC" ] && args+=(--desc "$PROJECT_DESC")
  [ -n "$PROJECT_TZ" ] && args+=(--tz "$PROJECT_TZ")
  $PRUNE && args+=(--prune)
  bash "$BOOTSTRAP" "${args[@]}" || log "bootstrap でエラー（プレースホルダ置換は部分的かもしれません）"
else
  # bootstrap が無い場合でも最低限 symlink 同期はする
  [ -x "$TARGET/tools/check_rules_sync.sh" ] && bash "$TARGET/tools/check_rules_sync.sh" --fix || true
fi

# --- 6.5 同期マーカーの記録（次回のアップデート確認の基準点）---
json_escape() {  # $1=value → \ と " をエスケープ（--base/--ref の任意文字列が JSON を壊すのを防ぐ）
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}
if [ -n "$BASE_HEAD" ]; then
  mkdir -p "$TARGET/.claude"
  cat > "$STATE_FILE" <<EOF
{
  "base_repo": "$(json_escape "$BASE_REPO")",
  "ref": "$(json_escape "$REF")",
  "commit": "$BASE_HEAD",
  "applied_at": "$(TZ="${PROJECT_TZ:-Asia/Tokyo}" date +%Y-%m-%dT%H:%M:%S%z)"
}
EOF
  log "  + .claude/base-sync-state.json（適用済みベース: ${BASE_HEAD:0:7}）"
  # マーカーが下流の .gitignore に食われるとコミットできず、毎回「初回適用」扱いに退行する
  if git -C "$TARGET" check-ignore -q .claude/base-sync-state.json 2>/dev/null; then
    log "  ⚠ .claude/base-sync-state.json が .gitignore で無視されています。次回のアップデート確認が働くよう ignore 設定を見直してコミットしてください"
  fi
fi

# --- 7. 完了サマリー ---
print_sync_summary
echo ""
log "✅ 適用完了: $TARGET_SLUG"
echo "  - ルール     : docs/rules/ + .claude/rules/（symlink）"
echo "  - スキル     : .claude/skills/"
echo "  - ハーネス   : .claude/hooks/ + .claude/settings.json"
echo "  - エージェント: .claude/agents/ / コマンド: .claude/commands/"
echo ""
echo "注意: 配布されたルール・スキル本文中の Issue/PR 番号（例: Issue #123）は"
echo "      ベース（$BASE_REPO）内部の参照です。このリポジトリの Issue とは無関係です。"
echo ""
echo "注意: フック（.claude/hooks/）・settings.json の変更は新セッションから有効になります"
echo "      （セッション途中の再適用では反映されません・起動時スナップショット・公式仕様）。"
echo ""
echo "次のステップ:"
echo "  1. docs/project-mission.md にミッション・KPI を記入（.base 雛形があれば参照）"
echo "  2. CLAUDE.md の応答スタイル / PR 自律化方針を確認（.base 雛形があれば差分を取り込む）"
echo "  3. 不要モジュールは modules.yaml を編集して再実行（--prune）"
echo "  4. クラウド実行する場合は GH_TOKEN を環境変数に設定"
echo "  5. .claude/base-sync-state.json をコミットに含める（次回アップデート確認の基準点）"
echo ""
echo "最新へ同期したいときは、同じコマンドを再実行してください（idempotent）。"
