# セッション圧縮ルール（Context Compaction・Hot 層サマリー）

> 全文（モデル別コンテキスト表・PostCompact 自動コミットの内部動作・symlink 自動修正の詳細）は
> `docs/rules/session-compression-rules-detail.md`。

## 圧縮時に何が残り、何が消えるか

| 項目 | 内容 |
|------|------|
| **発生タイミング** | コンテキストが約 95% に到達した時（自動）、または `/compact`（手動） |
| **プロジェクトルートの CLAUDE.md / `.claude/rules/`** | 圧縮後にディスクから **再読み込み** される（失われない） |
| **サブディレクトリのネストした CLAUDE.md** | **自動では再注入されない**（そのディレクトリのファイルを次に読んだときに再ロードされる・公式） |
| **会話内の口頭指示** | 圧縮により **失われる**（下記オートメモリの条件付き例外を除き、CLAUDE.md に書かれた指示のみ保持） |
| **未コミットのファイル変更** | `PostCompact` フック（`post-compact.sh`）が自動コミット & push する |

`PostCompact` は圧縮直後に未コミット変更を検出し、`main`/`master` 以外のブランチなら `git add -A && git commit && git push` する（圧縮後の SessionStart クリーンアップ `git checkout -- .` / `git clean -fd` が作業を消すことへの一次防御）。あわせて symlink 整合も自動修正する。

## シンボリックリンク方式の運用ルール

実体は `docs/rules/`、`.claude/rules/` は symlink（圧縮後もリンク先が再解決されるため正常に動作する）。タスク依存ルールは `docs/rules/` のみに置き、スキル起動時に Read する。

**新規ルールファイル追加時**: `docs/rules/{名前}.md` に実体を作成 → symlink → `check_rules_sync.sh` で検証 → Hot 予算確認 → コミット、の 5 手順（詳細・コマンド全文は `session-compression-rules-detail.md`）。symlink 作成を忘れても `session-start.sh` / `post-compact.sh` が `check_rules_sync.sh --fix` で自動検出・修正する（ハーネスが既に強制するレイヤーのため、Hot では手順を省略）。

## 圧縮に備えた情報の置き場所

| 情報の種類 | 保持方法 |
|-----------|---------|
| プロジェクトルール | `CLAUDE.md` または `.claude/rules/*.md` |
| セッションをまたぐ作業状態・重要な決定 | GitHub Issue / PR コメントに明示コミットする |
| 確認・判断依頼の内容 | ユーザーに確認する前にコミット & push（`session-safety-rules.md`） |
| 会話内の口頭指示 | **CLAUDE.md に反映しない限り圧縮後に失われる**（クラウド運用では下記のとおりオートメモリに頼れない） |

長大データ・サブエージェントの全文出力はコンテキストに置き続けない（1M トークンあっても不要な情報を詰め込まない）。

## オートメモリ（auto memory）との関係（実機検証 2026-07-26・#328）

Claude 自身が学びを書き溜める **オートメモリは既定で ON**（保存先 `~/.claude/projects/<project>/memory/`）。ただし公式が *"Auto memory is machine-local. […] Files are not shared across machines or cloud environments."* と明記しており、コンテナが破棄される **クラウド実行環境ではセッションを跨いで残らない**。

→ **オートメモリを永続化手段として当てにしない**。「重要な決定は Issue / PR コメントへ、恒久ルールは CLAUDE.md / `.claude/rules/` へ」という本ファイルの原則はクラウドでは変わらない（ローカル実行主体のプロジェクトでは、Claude が自分で気づいた事実の蓄積先として機能する）。仕様の詳細（読み込み範囲 200 行 / 25KB・設定・サブエージェントの扱い・役割分担）は `session-compression-rules-detail.md`。

## 禁止事項

- 「次のセッションで気をつける」で済ませない（CLAUDE.md またはルールファイルに反映する）
- **常時必要なルール** を symlink なしで `docs/rules/` のみに置かない（タスク依存ルールは `docs/rules/` のみで可）
- 新規ルール追加時に `check_rules_sync.sh` と Hot 層予算チェックを省略しない
