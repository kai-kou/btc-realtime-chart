#!/usr/bin/env bash
# tools/lib/test_harness.sh — フック回帰テスト（tools/test_*.sh）の共通ヘルパー。
# report() / 一時 git リポジトリ生成 / teardown の重複実装を集約する（Issue #630・Refs #627 PR #629）。
#
# 使い方: 各テストスクリプトの先頭で source する
#   source "$REPO_ROOT/tools/lib/test_harness.sh"
#
# 提供する関数・変数:
#   PASS / FAIL                  - report() が加算するカウンタ（テストスクリプト末尾の集計で参照する）
#   report <ok|ng> <説明>        - 結果を1行出力し PASS/FAIL を加算する
#   init_tmp_git_repo <work_dir> - <work_dir>/remote.git（bare）+ <work_dir>/repo（origin 設定済み・
#                                  user.email/user.name 設定済み・main ブランチ）を作る。ネットワークには
#                                  触れない。呼び出し元は戻り値の後、fixture コミット・push・
#                                  feature ブランチ作成などテスト固有の手順を続ける
#   teardown_tmp_repo <dir> ...  - 渡された一時ディレクトリを rm -rf する

PASS=0
FAIL=0
report() { # report <結果 ok|ng> <説明>
  if [ "$1" = "ok" ]; then
    PASS=$((PASS + 1)); echo "  PASS: $2"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL: $2"
  fi
}

init_tmp_git_repo() { # init_tmp_git_repo <work_dir>
  local work_dir="$1"
  git init --quiet --initial-branch=main "$work_dir/remote.git" --bare 2>/dev/null \
    || git init --quiet --bare "$work_dir/remote.git"
  git init --quiet --initial-branch=main "$work_dir/repo" 2>/dev/null || {
    git init --quiet "$work_dir/repo"
    git -C "$work_dir/repo" checkout --quiet -B main
  }
  git -C "$work_dir/repo" config user.email test@example.com
  git -C "$work_dir/repo" config user.name test
  git -C "$work_dir/repo" remote add origin "$work_dir/remote.git"
}

teardown_tmp_repo() { # teardown_tmp_repo <dir> [<dir> ...]
  rm -rf "$@"
}
