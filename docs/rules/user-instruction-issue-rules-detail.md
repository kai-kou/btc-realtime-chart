# ユーザー指示の Issue 化ルール — 作成コマンド実例（Warm 層）

> **本ファイルは `user-instruction-issue-rules.md`（Hot・本体）の実例補完版**。
> Hot 層予算の棚卸し（Issue #146）で作成コマンドの具体例（本ファイル自身の起票時の実例）を
> 本ファイルへ移設した（サマリー＝ポインタ規約）。本文テンプレートの型は Hot 版を参照。

## 作成コマンド例

```bash
mcp__github__issue_write（create アクション）またはローカル:
gh issue create \
  --title "docs: ユーザー指示のIssue化ルール追加" \
  --body "$(cat <<'EOF'
## ユーザー指示
ユーザー指示を受けた際にIssue化するか判断してほしい。...

## 対応方針
- docs/rules/user-instruction-issue-rules.md を新規作成
- .claude/rules/ にシンボリックリンクを追加
- CLAUDE.md に参照を追記

## 完了条件
- ユーザー指示を受けたとき Claude が自律的にIssueを作成して追跡できる
EOF
  )" \
  --label "type:docs,status:in-progress" \
  -R kai-kou/tsukuru-studio
```
