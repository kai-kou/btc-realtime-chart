#!/bin/bash
set -euo pipefail
# PreToolUse hook: git push origin main のダイレクトpushブロック（ハードコンストレイント Lv3）
#
# Bash ツールで git push が実行される前に自動チェック。
# main ブランチへの直接 push を物理的にブロックする。
# 許可されるブランチ: content/*, claude/*, feat/*, fix/*, docs/*
#
# 【再設計の経緯（セルフレビュー critical 2 件・2026-08 実測）】
# critical 1: 旧実装は "(main|master) が行末にある" という文字列全体マッチだったため、
#   `git push origin main | tee ...` / `... && echo done` / `...; echo x` / `... # comment`
#   のように後続が連結されると素通りしていた（実測 EXIT=0）。
#   → 本実装はコマンド文字列を `git push` 呼び出し単位（セグメント）へ分解し、
#     各セグメントの push 先ブランチを個別判定する。
# critical 2: publish-sync レーンは別リポジトリ（公開リポジトリのチェックアウト）の main へ
#   push する正当な操作を行うが、旧実装は push 先リポジトリを一切見ていなかったため
#   誤ブロックしていた。
#   → 本実装は `cd <path> && git push ...` / `git -C <path> push ...` で cwd が
#     この開発リポジトリ以外に移動しているかを解決し、他リポジトリなら対象外にする。
#     判定できない場合（cd 先が解決不能・変数展開を含む等）は fail-closed でブロックする。
#
# 検証: `bash .claude/hooks/pre-git-push-check.sh --self-test`

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hook_block.sh
source "$HOOK_DIR/lib/hook_block.sh"

BLOCK_MESSAGE="[pre-git-push-check] ❌ main/master への直接 push をブロックしました。

ルール: main/master ブランチへの直接 push は禁止されています（PR 経由のみ）。

許可されているブランチへの push 例:
  git push -u origin content/V007-xxx
  git push -u origin claude/feature-abc
  git push -u origin feat/new-feature
  git push -u origin fix/bug-fix
  git push -u origin docs/update

別リポジトリ（cd や git -C で移動した先）への push は対象外です。

ブランチ名を変数展開・eval・bash -c 等で動的に組み立てるコマンドは静的解析できないため
fail-closed でブロックしています。ブランチ名や push 先を直接書いてください
（例: git push -u origin feat/x）。

PR 経由でマージしてください。"

# この開発リポジトリのルート（正規化済みパス）。以後の cd/-C 判定の基準にする。
REPO_ROOT="$(cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" 2>/dev/null && pwd -P)"
REPO_ROOT="${REPO_ROOT:-$(pwd -P)}"

# 現在の実 cwd（= このリポジトリ）のブランチ名を取得する。
# 単体テストからモックしやすいよう関数に切り出す。
current_branch_of_repo_root() {
  git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo ""
}

# cd / -C の引数からリポジトリ文脈を解決する。
#   same    : この開発リポジトリ（REPO_ROOT と一致）
#   other   : 別ディレクトリ（実在し REPO_ROOT と異なる）
#   unknown : 解決不能（存在しない・変数展開/チルダを含む等）→ fail-closed 側で扱う
resolve_repo_context() {
  local raw="$1"
  # 前後の引用符を簡易除去
  raw="${raw%\"}"; raw="${raw#\"}"
  raw="${raw%\'}"; raw="${raw#\'}"

  # 変数展開・チルダ展開はここでは解決しない（誤解決のリスクが高い）→ unknown
  if [[ "$raw" == *'$'* || "$raw" == '~'* ]]; then
    echo "unknown"
    return
  fi

  local resolved
  resolved=$(cd "$raw" 2>/dev/null && pwd -P 2>/dev/null) || true
  if [[ -z "$resolved" ]]; then
    echo "unknown"
    return
  fi

  if [[ "$resolved" == "$REPO_ROOT" ]]; then
    echo "same"
  else
    echo "other"
  fi
}

# push 先ブランチ判定 + リポジトリ文脈から block/allow を決定する。
#   branch_target: "main" | "other-explicit" | "implicit"
#   repo_ctx:      "same" | "other" | "unknown"
decide() {
  local branch_target="$1" repo_ctx="$2"

  # 別リポジトリへの push は A-1 の対象外（この開発リポジトリの保護が目的のため）
  if [[ "$repo_ctx" == "other" ]]; then
    echo "allow"
    return
  fi

  case "$branch_target" in
    main)
      echo "block"
      ;;
    other-explicit)
      # main/master 以外への明示 push はブランチ名に関わらず許可
      echo "allow"
      ;;
    implicit)
      if [[ "$repo_ctx" == "unknown" ]]; then
        # push 先リポジトリすら判定できない implicit push は fail-closed でブロック
        echo "block"
        return
      fi
      local cur
      cur=$(current_branch_of_repo_root)
      if [[ "$cur" == "main" || "$cur" == "master" ]]; then
        echo "block"
      else
        echo "allow"
      fi
      ;;
    *)
      # 未知の分類は fail-closed
      echo "block"
      ;;
  esac
}

# コマンド文字列 1 本を解析し、含まれる全ての git push 呼び出しを走査して
# 1 つでもブロック対象なら "block"、なければ "allow" を返す。
scan_and_decide() {
  local full_command="$1"
  local decision_found="allow"
  local cwd_context="same"

  # 1. 物理行ごとに末尾コメントを除去（"; "以降を巻き込まないよう、改行分割の前に行う）
  #    シェルのコメント規則（# は行頭または空白直後でのみコメント開始）を簡易再現。
  local stripped
  stripped=$(printf '%s\n' "$full_command" | sed -E 's/(^|[[:space:]])#.*$//')

  # 2. 連結演算子（|| / && / ; / |）・構文境界（( ) { } do done then else fi）・改行で
  #    セグメントに分割する（|| を先に処理し | と混同しない。do/done 等は単語境界 \<...\> で
  #    誤爆（docs/update の "do" 等）を避ける）。
  local segments
  segments=$(printf '%s\n' "$stripped" | sed -E \
    -e 's/\|\|/\n/g' -e 's/&&/\n/g' -e 's/;/\n/g' -e 's/\|/\n/g' \
    -e 's/\(/\n/g' -e 's/\)/\n/g' -e 's/\{/\n/g' -e 's/\}/\n/g' \
    -e 's/\<do\>/\n/g' -e 's/\<done\>/\n/g' -e 's/\<then\>/\n/g' \
    -e 's/\<else\>/\n/g' -e 's/\<fi\>/\n/g')

  local seg
  while IFS= read -r seg; do
    seg="$(printf '%s' "$seg" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [[ -z "$seg" ]] && continue

    # cd（先頭の -L/-P/-e/-@ フラグは軽く読み飛ばす）
    if [[ "$seg" =~ ^cd[[:space:]]+(.+)$ ]]; then
      local cdarg="${BASH_REMATCH[1]}"
      while [[ "$cdarg" =~ ^-[LPe@][[:space:]]+(.+)$ ]]; do
        cdarg="${BASH_REMATCH[1]}"
      done
      cwd_context=$(resolve_repo_context "$cdarg")
      continue
    fi

    # eval / bash -c / sh -c / zsh -c: 引数文字列は静的解析できない。
    # セグメントに git と push が両方現れるなら fail-closed で block。
    if [[ "$seg" =~ ^eval([[:space:]]|$) ]] || [[ "$seg" =~ ^(bash|sh|zsh)[[:space:]]+-c([[:space:]]|$) ]]; then
      if printf '%s' "$seg" | grep -q 'git' && printf '%s' "$seg" | grep -q 'push'; then
        decision_found="block"
      fi
      continue
    fi

    local rest="" repo_ctx=""
    if [[ "$seg" =~ ^git[[:space:]]+-C[[:space:]]+([^[:space:]]+)[[:space:]]+push([[:space:]]+(.*))?$ ]]; then
      repo_ctx=$(resolve_repo_context "${BASH_REMATCH[1]}")
      rest="${BASH_REMATCH[3]:-}"
    elif [[ "$seg" =~ ^git[[:space:]]+push([[:space:]]+(.*))?$ ]]; then
      repo_ctx="$cwd_context"
      rest="${BASH_REMATCH[2]:-}"
    else
      # git push 呼び出しでないセグメント（cd 以外の任意コマンド）は無視
      continue
    fi

    # クォート除去（'main' / "main" 等の引用符付きブランチ名を正規化）
    local rest_norm
    rest_norm=$(printf '%s' "$rest" | tr -d "\"'")

    # push 先ブランチの分類
    #   implicit: 引数がフラグ列 + 高々1トークン（remote名のみ、または引数なし）
    #   main    : 末尾が main/master（ブランチ引数・+prefix・refs/heads/ 完全形・refspec dst いずれも）
    #   other-explicit: 上記以外（明示的に main/master 以外を指定）
    local branch_target="other-explicit"
    if [[ "$rest_norm" =~ ^(-[^[:space:]]+[[:space:]]+)*[A-Za-z0-9._/+-]*[[:space:]]*$ ]]; then
      branch_target="implicit"
    elif [[ "$rest_norm" =~ (^|[[:space:]])\+?(main|master)[[:space:]]*$ ]] || \
         [[ "$rest_norm" =~ (^|[[:space:]])\+?refs/heads/(main|master)[[:space:]]*$ ]] || \
         [[ "$rest_norm" =~ :[[:space:]]*\+?(main|master)[[:space:]]*$ ]] || \
         [[ "$rest_norm" =~ :[[:space:]]*\+?refs/heads/(main|master)[[:space:]]*$ ]]; then
      branch_target="main"
    fi

    # 変数展開・コマンド置換（$VAR / ${VAR} / $(...) / `...`）を含むなら静的に解決不能。
    # A-1 は不可逆なので見逃しコストを重く見て fail-closed（main 扱い）でブロックする。
    if [[ "$rest_norm" == *'$'* || "$rest_norm" == *'`'* ]]; then
      branch_target="main"
    fi

    local d
    d=$(decide "$branch_target" "$repo_ctx")
    if [[ "$d" == "block" ]]; then
      decision_found="block"
    fi
  done <<< "$segments"

  echo "$decision_found"
}

run_self_test() {
  local failures=0 total=0
  local other_dir missing_dir
  other_dir=$(mktemp -d)
  missing_dir="/nonexistent-precheck-$$-${RANDOM}/probably-not-here"

  check() {
    local desc="$1" cmd="$2" expected="$3"
    local got
    got=$(scan_and_decide "$cmd")
    total=$((total + 1))
    if [[ "$got" != "$expected" ]]; then
      failures=$((failures + 1))
      printf '  FAIL: %-40s cmd=[%s] expected=%s got=%s\n' "$desc" "$cmd" "$expected" "$got" >&2
    fi
  }

  echo "[pre-git-push-check] self-test 開始（REPO_ROOT=${REPO_ROOT}）" >&2

  # --- critical 1: 実測で素通りしていた 6 パターン（全て block 期待）---
  check "bare main"            "git push origin main"                       "block"
  check "-u main"               "git push -u origin main"                    "block"
  check "pipe"                  "git push origin main | tee /tmp/l"          "block"
  check "and-and"               "git push origin main && echo done"          "block"
  check "semicolon"             "git push origin main; echo x"               "block"
  check "trailing comment"      "git push origin main # comment"             "block"

  # --- その他 main/master 判定 ---
  check "master (bare)"         "git push origin master"                     "block"
  check "refspec dst main"      "git push origin HEAD:main"                  "block"
  check "refs/heads/main"       "git push origin HEAD:refs/heads/main"       "block"
  check "multiple pushes 2nd main" "git push origin feat/x && git push origin main" "block"
  check "-C same repo main"     "git -C ${REPO_ROOT} push origin main"       "block"
  check "unresolvable cd + main" "cd ${missing_dir} && git push origin main" "block"
  check "unresolvable cd + implicit" "cd ${missing_dir} && git push"         "block"

  # --- lead 追加検証ですり抜けが判明した 10 件（すべて block 期待）---
  check "quoted branch (single)"   "git push origin 'main'"                          "block"
  check "quoted branch (double)"   "git push origin \"main\""                        "block"
  check "full refspec form"        "git push origin refs/heads/main"                 "block"
  check "force refspec + prefix"   "git push origin +main"                           "block"
  check "for-do-done loop"         "for i in 1; do git push origin main; done"       "block"
  check "subshell (cd + push)"     "(cd ${REPO_ROOT} && git push origin main)"       "block"
  check "variable expansion (bare)"      "B=main; git push origin \$B"               "block"
  check "variable expansion (quoted)"    "BR=main && git push origin \"\$BR\""       "block"
  check "eval"                     "eval \"git push origin main\""                   "block"
  check "bash -c"                  "bash -c \"git push origin main\""                "block"

  # --- 許可すべきパターン（誤検知防止）---
  check "feature branch"        "git push origin feat/main-test"             "allow"
  check "refspec to feature"    "git push origin HEAD:refs/heads/feature"    "allow"
  check "not a push command"    "git log --oneline main"                     "allow"
  check "other repo via cd"     "cd ${other_dir} && git push -u origin main" "allow"
  check "other repo via -C"     "git -C ${other_dir} push origin main"       "allow"
  check "other repo, no branch" "cd ${other_dir} && git push"                "allow"

  # --- ユーザー指定の誤ブロック禁止 18 件（すべて allow 期待）---
  check "claude branch -u"         "git push -u origin claude/xxx"                   "allow"
  check "feat branch -u"           "git push -u origin feat/new-feature"             "allow"
  check "docs branch"              "git push origin docs/update"                     "allow"
  check "force-with-lease"         "git push --force-with-lease origin claude/xxx"   "allow"
  check "delete branch refspec"    "git push origin :old-branch"                     "allow"
  check "tags"                     "git push --tags"                                 "allow"
  check "push HEAD"                "git push -u origin HEAD"                         "allow"
  check "git fetch (not push)"     "git fetch origin main"                           "allow"
  check "log --grep push main"     "git log --oneline --grep push main"              "allow"
  check "branch -d main"           "git branch -d main"                              "allow"
  check "echo literal"             "echo \"git push origin main is blocked\""        "allow"
  check "npm run push"             "npm run push"                                    "allow"
  check "main-feature branch"      "git push origin main-feature"                    "allow"
  check "mainline branch"          "git push origin mainline"                        "allow"
  check "feature/mainfix branch"   "git push origin feature/mainfix"                 "allow"
  check "other repo cd -u"         "cd ${other_dir} && git push -u origin main"      "allow"
  check "other repo -C (dup)"      "git -C ${other_dir} push origin main"            "allow"

  # --- A-1 の中核シナリオ: 同一リポジトリで main/master にいるときの引数なし push ---
  # 引数なし push は「現在のブランチを追跡先へ送る」ため、main にいれば main 直 push になる。
  # 明示的に main と書かないので、引数だけを見る実装では捕捉できない。REPO_ROOT を
  # main ブランチの一時リポジトリへ差し替えて、現在ブランチ判定が効いていることを固定する。
  local saved_root main_repo feat_repo
  saved_root="$REPO_ROOT"

  main_repo=$(mktemp -d)
  git -C "$main_repo" init -q -b main 2>/dev/null || git -C "$main_repo" init -q 2>/dev/null
  REPO_ROOT="$main_repo"
  check "implicit push while on main"        "git push"                     "block"
  check "implicit push while on main (flag)" "git push --force"             "block"
  check "explicit feature while on main"     "git push origin feat/x"       "allow"

  feat_repo=$(mktemp -d)
  git -C "$feat_repo" init -q -b feat/work 2>/dev/null || git -C "$feat_repo" init -q 2>/dev/null
  REPO_ROOT="$feat_repo"
  check "implicit push while on feature"     "git push"                     "allow"

  REPO_ROOT="$saved_root"
  rm -rf "$main_repo" "$feat_repo" 2>/dev/null || true

  rmdir "$other_dir" 2>/dev/null || true

  echo "" >&2
  echo "[pre-git-push-check] self-test: $((total - failures)) passed / ${failures} failed (total ${total})" >&2
  [[ "$failures" -eq 0 ]]
}

main() {
  local input tool_name command decision
  input=$(cat)

  tool_name=$(echo "$input" | jq -r '.tool_name // ""')
  if [ "$tool_name" != "Bash" ]; then exit 0; fi

  command=$(echo "$input" | jq -r '.tool_input.command // ""')

  # "push" という語すら含まなければ対象外（安価な早期リターン）
  if ! printf '%s' "$command" | grep -q 'push'; then exit 0; fi

  decision=$(scan_and_decide "$command")
  if [[ "$decision" == "block" ]]; then
    hook_block "$BLOCK_MESSAGE"
  fi

  exit 0
}

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit $?
fi

main
