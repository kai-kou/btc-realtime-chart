# Warm 層 教訓 — セッション安全・タイムアウト

セッションタイムアウト・ストリーム安定性に関するカテゴリ別教訓（タスク依存で Read）。

---

## L-055: 大量の複雑コンテンツを 1 回で Read すると Stream idle timeout で停止する（2026-06-13）

**パターン**: Python コードブロックや YAML テーブルが多い構造化 Markdown を 1 回の Read で
大量取得（例: 400 行）すると、処理中に「API Error: Stream idle timeout - partial response
received」が発生してセッションが停止する。Read 直後にサイレントで次の処理を計画する（長い
Adaptive Thinking が続く）ことでも誘発される。

**根本原因**: 1 ツール応答あたりの処理量・思考時間が長すぎてストリームがアイドル切断される。

**対策**:
- Read の `limit` をコンテンツ種別で調整する: シンプルな Markdown は最大 200 行、構造化
  Markdown は 60〜80 行、Python/大量コードは 60 行
- Read の返値を受け取った直後、次のツール呼び出しより **前に 1〜2 文のテキスト応答** を出す
  （サイレント思考の連続を避ける）
- 200 行超のファイルを生成する予定なら、Read を省略して既知内容から直接 Write する
- 圧縮サマリーにファイル内容がある場合は再 Read せずサマリーを参照する

詳細は `session-safety-rules.md` のルール 4。

---

## L-115: 実マージを検証せず「マージ済み」と完了誤認する（2026-06-21）

**パターン**: フォローアップ修正を「PR #N マージ済み・Issue クローズ済み」と完了報告したが、実際には
PR は存在せず（GitHub API 404）、ファイルにも未反映だった。原因の連鎖: ① `git commit -m "$(cat <<'EOF'
... EOF)"` のヒアドキュメント形式がシェル残響でコミット空振り → HEAD が進まない、② ハーネスの
ツール出力が断続的に空結果を返す中で存在確認をクロスチェックしなかった、③ PR 番号の存在だけで
マージ済みと判断した。

**対策**（`session-safety-rules.md` の「git 操作の安全則」G-1〜G-3 + ルール5 に機械化）:
- commit はヒアドキュメントを避け複数 `-m` フラグを使い、直後に `git diff HEAD~1 HEAD --stat` で着地確認する。
- squash マージ後は `git fetch origin +main:refs/remotes/origin/main`（`+` で非 fast-forward にも追従）で remote-tracking ref を明示更新してから新ブランチを切る（二重 diff 防止）。
- 完了報告の直前に `state=MERGED` を実結果で検証する。**クラウドは `mcp__github__pull_request_read(method="get", pullNumber=N)` が一次経路**（gh は未導入・repo REST も 403・L-114）。ローカルは `gh pr view <N> --json state,mergedAt`。あわせて `git log origin/main --oneline` でスカッシュコミットの着地も確認する。検証できるまで「マージ済み」と報告しない。

**判定基準**: 「マージした / 完了した」と書こうとした瞬間が発動トリガー。状態確認の単一コマンド結果を
鵜呑みにしない。
