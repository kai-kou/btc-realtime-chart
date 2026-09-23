# セッション安全ルール（Hot 層サマリー）

> **SSOT**: 詳細（タイムアウト種別の解説・コマンド例・確認ケース一覧・パイプライン停止テンプレート）は
> `docs/rules/session-safety-rules-detail.md` を参照。

---

## 🔴 L-077: ユーザー確認前の必須リサーチ義務（CP-6 中核・常駐）

「ユーザー確認」を選ぶ前に `docs/rules/problem-investigation-protocol.md` の **5 ステップを完全実施** する。
「環境変数がない」「API 認証失敗」等を理由にリサーチ不十分のまま確認に回すのは CP-6 重大違反。

```
[ ] Step 1: 状況の精密化（エラー全文・再現手順を 3 行で記述）
[ ] Step 2: 既存ナレッジ検索（lessons / Issue / SKILL.md を grep）
[ ] Step 3: Agent Teams 並列調査（コードベース・ドキュメント・公式情報の 3 役以上）
[ ] Step 4: 3 層因果分析（直接原因 / 中間原因 / 根本原因）
[ ] Step 5: 自己解決可否の判定
```

---

## タイムアウト防止ルール

**ルール 1**: 1 ターンのツール呼び出しは **最大 8 個**。超える場合は中間報告して次ターンへ。

**ルール 3**: 長いタスクは「8 ツール → 中間報告 → 続き」のリズムで進める。

> **別軸との混同に注意**: ルール 1・3 が制限するのは **1 ターンにメインセッションが発行するツール呼び出しの数** であって、**同時に走っているサブエージェントの数ではない**（後者の上限・並列化方針は `agent-team-summary.md` が持つ）。`Agent` の起動呼び出し **自体は 8 個にカウントする** が、起動済みサブエージェントが並行稼働している間はメインのターンを占有しないため **カウントに残らない**。したがって「1 ターンで 8 個まで起動 → 完了通知を待つ → 次ターンで続きを起動」が上限どおりの進め方であり、独立作業の並列委譲を「8 個制限」を理由に **逐次実行（1 個ずつ待つ）に落とさない**。

**ルール 4（Stream idle 防止）**: Read の limit をコンテンツ種別で調整する。

```
- シンプルなテキスト: max 200行
- コードブロック・テーブルが多い構造化 Markdown: max 80行
- Python スクリプト・大量コード: max 60行
```

大きな Read（構造化 80 行超 / コード 60 行超）の直後は **短い事実 1 行** を出力してから次のツールへ。

**ルール 5（空結果クロスチェック）**: 状態確認系コマンドがゼロ件を返したら即座にリトライ（最大 3 回）。
完了判定・存在確認は別経路（`gh` と `mcp__github__*`）でクロスチェックしてから判断する。

---

## git 操作安全則

**G-1**: squash マージ後・新ブランチ作成前に必ず origin/main を明示 refspec で同期する。

```bash
git fetch origin +main:refs/remotes/origin/main
git checkout -B <new-branch> origin/main
```

**G-2**: commit は複数 `-m` フラグを使い、直後に `git diff HEAD~1 HEAD --stat` で着地確認する。

```bash
git commit -m "件名" -m "本文"
git diff HEAD~1 HEAD --stat   # 空なら commit は着地していない
```

**G-3**: 完了報告の直前に実マージを機械検証する。

```bash
git fetch origin +main:refs/remotes/origin/main
git log origin/main --oneline | head -3
# state=MERGED の確認（クラウド一次経路 = MCP・L-114）:
#   mcp__github__pull_request_read(method="get", pullNumber=N) → state / merged を確認
# ローカル: gh pr view <N> -R <owner>/<repo> --json state,mergedAt
```

`state=MERGED` を確認できるまで「マージ済み」と報告しない。

---

**G-4（#483）**: `[wip]` 自動保全コミットは履歴に残す前提のものではない。Stop フックは未コミットの差し戻し中に **1 巡だけ猶予** するので、**その 1 巡で意味のある粒度・メッセージのコミットを切る**（2 巡目以降は無条件で自動保全・L-100。猶予中も `refs/claude-wip/<session_id>` にスナップショットが残るので作業は消えない）。PR 前の書き換えは `pr-review-flow-summary.md` 項目 0（`pre-pr-create-check.sh` が機械ブロック）。仕様全文は detail 版 G-4。

## ユーザー確認前の基本手順

```bash
git status
git add .
git commit -m "[wip] ユーザー確認待ち（{概要}）"
git push -u origin {ブランチ名}
# push 完了後にユーザーへの確認メッセージを送る
```
