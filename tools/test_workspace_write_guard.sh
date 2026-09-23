#!/bin/bash
# tools/test_workspace_write_guard.sh — 作業領域外書き込みガード（.claude/hooks/lib/workspace_write_guard.py）の回帰テスト
#
# 判定対象は「無人ルーティンが承認プロンプトで停止する原因になるコマンド」（Issue #578）。
# 実際に停止した 2 例（下流 blog-dispatch ルーティン）を再現ケースとして先頭に置く。
#
# 使い方: bash tools/test_workspace_write_guard.sh
# 終了コード: 0 = 全ケース期待どおり / 1 = 期待と異なるケースあり
set -uo pipefail

GUARD="$(cd "$(dirname "$0")/.." && pwd)/.claude/hooks/lib/workspace_write_guard.py"
TEST_CWD="/home/user/demo-repo"
TEST_HOME="/root"
TEST_SESSION="11111111-2222-3333-4444-555555555555"
PASS=0
FAIL=0

# run_case <期待: BLOCK|ALLOW> <説明> <コマンド>
run_case() {
  local expect="$1" desc="$2" cmd="$3"
  local payload output status actual
  payload=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"session_id":sys.argv[3],"tool_input":{"command":sys.argv[2]}}))' "$TEST_CWD" "$cmd" "$TEST_SESSION")
  output=$(printf '%s' "$payload" | HOME="$TEST_HOME" TMPDIR="" python3 "$GUARD" 2>&1)
  status=$?
  if [ "$status" -eq 1 ]; then actual="BLOCK"; else actual="ALLOW"; fi
  if [ "$actual" = "$expect" ]; then
    PASS=$((PASS + 1))
    printf '  ok   [%s] %s\n' "$expect" "$desc"
  else
    FAIL=$((FAIL + 1))
    printf '  NG   期待=%s 実際=%s : %s\n       cmd: %s\n' "$expect" "$actual" "$desc" "$cmd" >&2
    [ -n "$output" ] && printf '       out: %s\n' "$(printf '%s' "$output" | head -3 | tr '\n' ' ')" >&2
  fi
}

echo "[test] 停止した実例の再現"
run_case BLOCK "実例1: 変数経由で作業ツリー外に隔離ディレクトリを作る" \
  'WORK=/tmp/skill-doctor-demo; rm -rf $WORK; mkdir -p $WORK/.claude; cp -r /home/user/demo-repo/.claude/skills $WORK/.claude/skills'
run_case BLOCK "実例2: ホーム配下のツール結果ファイルを Bash で複製する" \
  "mkdir -p /tmp/claude-0/demo/$TEST_SESSION/scratchpad && cp /root/.claude/projects/demo/s/tool-results/r.txt /tmp/claude-0/demo/$TEST_SESSION/scratchpad/page1.json"

echo "[test] ブロックすべきケース"
run_case BLOCK "作業ツリー外への mkdir" 'mkdir -p /opt/demo-workspace'
run_case BLOCK "作業ツリー外へのリダイレクト書き込み" 'echo hi > /tmp/demo-out/a.txt'
run_case BLOCK "作業ツリー外への rm -rf" 'rm -rf /tmp/demo-out'
run_case BLOCK "作業ツリー外への cp（宛先が外）" 'cp ./report.md /tmp/demo-out/report.md'
run_case BLOCK "ホーム配下の Claude 領域の読み取り" 'cat /root/.claude/projects/demo/sess/tool-results/r.txt'
run_case BLOCK "チルダ表記のホーム配下 Claude 領域" 'ls ~/.claude/projects'

run_case BLOCK "GNU の -t で宛先を先頭指定する cp" 'cp -t /tmp/demo-out file1.txt file2.txt'
run_case BLOCK "--target-directory= 形式の宛先" 'cp --target-directory=/tmp/demo-out file1.txt'
run_case BLOCK "curl のダウンロード先が作業ツリー外" 'curl -o /tmp/demo-out/report.json https://example.com/r.json'
run_case BLOCK "wget の出力先が作業ツリー外" 'wget -O /tmp/demo-out/a.bin https://example.com/a.bin'
run_case BLOCK "sed -i による作業ツリー外の書き換え" 'sed -i "s/a/b/" /etc/demo.conf'
run_case BLOCK "dd の of= が作業ツリー外" 'dd if=/dev/zero of=/tmp/demo-out/blob bs=1M count=1'
run_case BLOCK "fd 付きリダイレクト（2>）" 'somecmd 2> /tmp/demo-out/err.log'
run_case BLOCK "noclobber 上書きリダイレクト（>|）" 'echo hi >| /tmp/demo-out/a.txt'
run_case BLOCK "cd で作業ツリー外へ移動してからの相対パス削除" 'cd /tmp/other-place && rm -rf temp_output'
run_case BLOCK "ラッパー（sudo）越しの削除" 'sudo rm -rf /tmp/demo-out/x'
run_case BLOCK "他セッションの scratchpad の削除" 'rm -rf /tmp/claude-0/demo/99999999-aaaa-bbbb-cccc-dddddddddddd/scratchpad'
run_case BLOCK "クォート内に & を含む URL があっても宛先を見失わない" \
  'curl "https://x.example/a?b=1&c=2" -o /tmp/demo-out/report.json'
run_case BLOCK "未終端 heredoc の後続行は解析対象に戻す（fail-closed）" \
  'cat <<EOF
intro
rm -rf /tmp/demo-out'

echo "[test] リポジトリ内の保護パス（#618・C1 / C3 の実例）"
run_case BLOCK "実例 C1: sed -i でリポジトリ自身のフックを書き換える（下流 A）" \
  'cd /home/user/demo-repo && grep -n "tail -n 1" .claude/hooks/pre-tool-use-router.sh && sed -i "/x/d" .claude/hooks/pre-tool-use-router.sh && diff -q /tmp/r.sh.bak3 .claude/hooks/pre-tool-use-router.sh && echo "NO_CHANGE" || echo "CHANGED"'
run_case BLOCK "実例 C3: .git/info/exclude へのリダイレクト追記（下流 C）" \
  'cd /home/user/demo-repo && echo "tmp-content-issues.json" >> .git/info/exclude && git status --porcelain; echo "--- ok"'
run_case BLOCK "hooks 以外の .claude/** への sed -i" 'sed -i "s/a/b/" .claude/skills/foo/SKILL.md'
run_case BLOCK ".claude/settings.json への sed -i" 'sed -i "s/a/b/" .claude/settings.json'
run_case BLOCK "tee -a で .claude 配下へ追記" 'echo x | tee -a .claude/hooks/x.sh'
run_case BLOCK ".git/hooks への cp（許容している副作用・脱出ハッチで通す）" 'cp local-hook.sh .git/hooks/pre-commit'
run_case BLOCK "cd で .claude/hooks に入ってからの相対パス書き換え" 'cd .claude/hooks && sed -i "s/a/b/" dummy.sh'
run_case BLOCK "rules 以外の .claude/** への symlink 作成" 'ln -sf ../../docs/x.md .claude/skills/foo/SKILL.md'
run_case ALLOW "正規手順: docs/rules → .claude/rules の symlink" 'ln -sf ../../docs/rules/x.md .claude/rules/x.md'
run_case ALLOW "公式の明示除外: .claude/worktrees" 'echo x > .claude/worktrees/wt-1/marker'
run_case ALLOW "git サブコマンド自体は対象外（config）" 'git config core.excludesfile .git/info/exclude'
run_case ALLOW "git サブコマンド自体は対象外（update-index）" 'git update-index --assume-unchanged path/to/file'
run_case ALLOW "git サブコマンド自体は対象外（worktree）" 'git worktree add ../wt-x'
run_case ALLOW "ラッパースクリプト経由（内部の cp -a はこの層から不可視）" 'bash scripts/apply-to-repo.sh'
run_case ALLOW "リポジトリ内 .claude の読み取り" 'cat .claude/hooks/pre-tool-use-router.sh'
run_case ALLOW ".claude をコピー元にして scratchpad へ複製" \
  "cp -r .claude/skills /tmp/claude-0/demo/$TEST_SESSION/scratchpad/skills"
run_case BLOCK "深い位置の .claude も保護（Layer 1 指摘）" 'sed -i "s/a/b/" sub/.claude/hooks/x.sh'
run_case BLOCK "worktree 内のフックは再び保護（Layer 1 指摘）" 'sed -i "s/a/b/" .claude/worktrees/wt-1/.claude/hooks/x.sh'
run_case BLOCK "mv でフックを外へ移す（移動元の除去・Layer 1 指摘）" 'mv .claude/hooks/pre-tool-use-router.sh /tmp/demo-out/x'
run_case BLOCK "リンク元が docs/rules 外の .claude/rules への symlink（Layer 1 指摘）" 'ln -sf /etc/passwd .claude/rules/evil.md'
run_case BLOCK "symlink 以外の .claude/rules 書き込み" 'echo x > .claude/rules/new.md'
# ベースは ALLOW（素通り）だが、本リポジトリは Issue #50 の fail-closed（基点不明の相対書き込みを
# 判定不能としてブロック）を併存させているため BLOCK になる
run_case BLOCK "cd - 後の相対書き込みは基点不明として差し戻す（#50 fail-closed）" 'cd .claude/hooks && cd - && sed -i "s/a/b/" .claude/hooks/x.sh'
run_case ALLOW "scratchpad 内のラボの .claude は対象外" \
  "sed -i 's/a/b/' /tmp/claude-0/demo/$TEST_SESSION/scratchpad/lab/.claude/hooks/dummy.sh"
run_case ALLOW "前置きトグルで .git 配下の復旧操作を通す" \
  'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 rm -f .git/index.lock'

echo "[test] 保護パス × 既存の下流パッチ（再帰評価・find）の合成（PR #55 Layer 1 指摘）"
run_case BLOCK "bash -c 越しの .claude 書き換え" 'bash -c "sed -i '"'"'s/a/b/'"'"' .claude/hooks/x.sh"'
run_case BLOCK "sh -c 越しの .git/info/exclude 追記" 'sh -c "echo x >> .git/info/exclude"'
run_case BLOCK "コマンド置換の内側の .claude/rules 書き込み" 'echo $(sed -i "s/a/b/" .claude/rules/new.md)'
run_case BLOCK "cd で保護配下に入ってから bash -c" 'cd .claude/hooks && bash -c "sed -i '"'"'s/a/b/'"'"' dummy.sh"'
run_case BLOCK "cd で保護配下に入ってからコマンド置換" 'cd .claude/hooks && echo $(sed -i "s/a/b/" pre-tool-use-router.sh)'
run_case BLOCK "find -exec sed -i によるフック書き換え" 'find .claude/hooks -type f -exec sed -i "s/a/b/" {} \;'
run_case BLOCK "find -execdir sed -i によるフック書き換え" 'find .claude/hooks -type f -execdir sed -i "s/a/b/" {} \;'
run_case BLOCK "find -exec の直書き書き込み先（作業領域外）" 'find . -type f -exec sed -i "s/a/b/" /tmp/demo-out/x \;'
run_case BLOCK ".claude/worktrees コンテナ自体の削除は除外しない" 'rm -rf .claude/worktrees'
run_case ALLOW "find -exec が非破壊コマンドなら通す" 'find .claude/hooks -type f -exec grep -l ERROR {} \;'

echo "[test] 通すべきケース"
run_case ALLOW "自セッションの scratchpad への書き込み" \
  "mkdir -p /tmp/claude-0/demo/$TEST_SESSION/scratchpad && echo hi > /tmp/claude-0/demo/$TEST_SESSION/scratchpad/a.txt"
run_case ALLOW "作業ディレクトリ内へのリダイレクト書き込み" 'echo hi > ./notes.md'
run_case ALLOW "作業ディレクトリ内の絶対パス書き込み" 'mkdir -p /home/user/demo-repo/build'
run_case ALLOW "相対パスの削除" 'rm -rf node_modules'
run_case ALLOW "外部パスからの読み取り（cp のソース）" 'cp /usr/share/doc/readme ./readme'
run_case ALLOW "外部パスの読み取り専用コマンド" 'grep -rn foo /usr/share/doc'
run_case ALLOW "プロジェクト内 .claude の読み取り" 'cat .claude/settings.json'
run_case ALLOW "スクリプト実行（内部の一時ディレクトリは射程外）" 'python3 tools/generate_project_context.py'
run_case ALLOW "解決できない変数を含むパス（判定不能は素通り）" 'mkdir -p "$EXTERNAL_BASE/x"'
run_case ALLOW "heredoc 本文に例示パスを含む文書生成（誤ブロック防止・導入直後に実発生）" \
  'cat > docs/note.md <<EOF
例: cat /root/.claude/projects/demo/tool-results/r.txt を Bash で読まない
例: rm -rf /tmp/demo-out は承認プロンプトになる
EOF'
run_case BLOCK "heredoc の外側の実コマンドは heredoc があっても判定される" \
  'rm -rf /tmp/demo-out && cat > ./note.md <<EOF
本文
EOF'

run_case ALLOW "クォート内の & を含む URL・宛先は作業ツリー内" \
  'curl "https://x.example/a?b=1&c=2" -o ./report.json'
run_case ALLOW "cd で作業ツリー内へ移動してからの相対パス削除" 'cd docs && rm -rf build'
run_case ALLOW "curl -O（出力先は cwd）" 'curl -O https://example.com/a.bin'
run_case ALLOW "/dev/null へのリダイレクト（誤ブロック防止・導入直後に実発生）" 'some-check 2>/dev/null | head -3'
run_case ALLOW "/dev/null を宛先にする dd" 'dd if=/dev/zero of=/dev/null bs=1M count=1'

run_case ALLOW "コマンド先頭の前置きトグル（#582・案内文どおりの外し方）" \
  'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 rm -f /tmp/demo-out/marker'
run_case BLOCK "トグル名が後続セグメントに現れてもガードは外れない" \
  'rm -rf /tmp/demo-out; CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 echo done'
run_case BLOCK "トグル名が引数の文字列に現れてもガードは外れない" \
  'echo "CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1" && rm -rf /tmp/demo-out'
run_case BLOCK "トグルの適用範囲はそのセグメントに閉じる（後段は検査する）" \
  'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 rm -f /tmp/demo-out/marker && rm -rf /tmp/demo-out/other'
run_case BLOCK "heredoc 本文にトグル名があってもガードは外れない" \
  'cat > docs/n.md <<EOF
CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 と書く
EOF
rm -rf /tmp/demo-out'

run_case BLOCK "シェルコメント内のトグル名では外れない" \
  'rm -rf /tmp/demo-out # CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1'
run_case BLOCK "複数行コマンドの後方行のトグルは前の行に及ばない" \
  'rm -rf /tmp/demo-out
echo x
CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 true'

echo "[test] Issue #50: PR #49 レビューで見つかった検知漏れ 4 件"
run_case BLOCK "bash -c 経由の削除" 'bash -c "rm -rf /tmp/demo-out"'
run_case BLOCK "sh -c 経由の削除" 'sh -c "rm -rf /tmp/demo-out"'
run_case BLOCK "find -delete による削除" 'find /tmp/demo-out -type f -delete'
run_case BLOCK "find -exec rm による削除" 'find /tmp/demo-out -exec rm -rf {} \;'
run_case BLOCK "コマンド置換の内側にある削除" 'echo $(rm -rf /tmp/demo-out)'
run_case BLOCK "cd 先が未解決のまま相対パスを削除（fail-closed）" 'cd "$(mktemp -d)"; rm -rf newfile'
run_case BLOCK "find | xargs rm による削除" 'find /tmp/demo-out -type f | xargs rm -f'
run_case ALLOW "bash -c 経由でも非破壊コマンドは通す" 'bash -c "echo hello"'
run_case ALLOW "find に -delete/-exec が無ければ通す" 'find . -name "*.pyc"'
run_case ALLOW "xargs の対象が非破壊コマンドなら通す" 'find . -name "*.log" | xargs grep -l ERROR'
run_case ALLOW "コマンド置換の内側が非破壊コマンドなら通す" 'echo $(pwd)'

echo "[test] Layer 1 セルフレビュー指摘（CONFIRMED・PR #51）"
run_case BLOCK "xargs -I {} 越しの bash -c（値取りフラグの値をコマンド名と誤認しない）" \
  'find /tmp/demo-out -type f | xargs -I {} bash -c "rm -rf {}"'
run_case BLOCK "bash の複合短縮フラグ（-euc）でも -c と同じ扱いにする" 'bash -euc "rm -rf /tmp/demo-out"'
run_case BLOCK "bash の複合短縮フラグ（-lc）でも -c と同じ扱いにする" 'bash -lc "rm -rf /tmp/demo-out"'
run_case ALLOW "xargs -I {} の対象が非破壊コマンドなら通す" \
  'find . -name "*.txt" | xargs -I {} grep -l ERROR {}'
run_case BLOCK "bash -c のネスト（テストカバレッジ指摘・PR #51）" \
  'bash -c "bash -c '"'"'rm -rf /tmp/demo-out'"'"'"'
run_case BLOCK "コマンド置換のネスト（テストカバレッジ指摘・PR #51）" 'echo $(echo $(rm -rf /tmp/demo-out))'

echo "[test] TMPDIR 配下の .claude は対象外（実値で分岐を通す）"
tmp_out=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"tool_input":{"command":"cp -r .claude/skills /tmp/wwg-tmpdir/.claude/skills"}}))' "$TEST_CWD" \
  | HOME="$TEST_HOME" TMPDIR="/tmp/wwg-tmpdir" python3 "$GUARD" 2>&1)
if [ $? -eq 0 ] && [ -z "$tmp_out" ]; then
  PASS=$((PASS + 1)); echo "  ok   [ALLOW] TMPDIR 配下の .claude への書き込みは保護対象にしない"
else
  FAIL=$((FAIL + 1)); echo "  NG   TMPDIR 配下の .claude を誤ブロック: $tmp_out" >&2
fi

echo "[test] トグルによる無効化"
toggle_out=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"tool_input":{"command":"rm -rf /tmp/demo-out"}}))' "$TEST_CWD" \
  | HOME="$TEST_HOME" CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 python3 "$GUARD" 2>&1)
if [ $? -eq 0 ] && [ -z "$toggle_out" ]; then
  PASS=$((PASS + 1)); echo "  ok   [ALLOW] CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 で素通りする"
else
  FAIL=$((FAIL + 1)); echo "  NG   トグルが効いていない" >&2
fi

echo "[test] ルーター統合（フックが exit 2 でブロックすること）"
router="$(cd "$(dirname "$0")/.." && pwd)/.claude/hooks/pre-tool-use-router.sh"
router_out=$(python3 -c 'import json; print(json.dumps({"tool_name":"Bash","cwd":"/home/user/demo-repo","tool_input":{"command":"mkdir -p /opt/demo-workspace"}}))' \
  | bash "$router" 2>&1)
router_status=$?
if [ "$router_status" -eq 2 ] && printf '%s' "$router_out" | grep -q "BLOCK:"; then
  PASS=$((PASS + 1)); echo "  ok   [BLOCK] ルーター経由で exit 2 になる"
else
  FAIL=$((FAIL + 1)); echo "  NG   ルーター統合: exit=$router_status out=$router_out" >&2
fi

echo
echo "[result] PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
