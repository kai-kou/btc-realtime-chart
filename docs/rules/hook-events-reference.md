# フックイベント公式リファレンス（実在検証 + 採否決定・SSOT）

> このファイルは Claude Code の **フックイベント名の実在性** と、本ベースでの **採否決定** の唯一の正本（SSOT）。
> タスク依存ルール（Warm 層）であり `.claude/rules/` に symlink しない（フック設計・拡張時のみ Read する）。
>
> **検証日**: 2026-06-14（初版）/ 2026-07-14（SessionStart 詳細を再フェッチ・§2.5 追加）/
> **一次情報**: https://code.claude.com/docs/en/hooks ・ https://code.claude.com/docs/en/claude-code-on-the-web
> 訓練データではなく公式ドキュメントの実フェッチで裏取りした（E-E #23・🔍 タスク）。

## 0. 検証の結論

公式ドキュメントのライフサイクル表には **31 個** の設定可能なフックイベントが存在すると明記されている。
本ファイルの §1 一覧には、一次フェッチで名称を確認できた **30 件** を掲載する（残り 1 件は未確認・後述）。
過去に「実在が疑わしい」とされたイベント名（`PostToolBatch` / `StopFailure` / `PostToolUseFailure` /
`UserPromptExpansion` / `PreCompact` / `PostCompact` / `SessionEnd` / `Notification`）は **すべて実在** する。

## 1. 公式イベント一覧（公式表記 31・本表は確認済み 30 を掲載・2026-06-14 時点）

| # | イベント | トリガー（要約） | 本ベースでの採否 |
|---|---------|----------------|----------------|
| 1 | `SessionStart` | セッション開始・再開時（matcher: startup/resume/clear/compact） | ✅ 採用（session-start.sh） |
| 2 | `Setup` | 環境セットアップ時 | ⬜ 未採用 |
| 3 | `UserPromptSubmit` | ユーザー送信直後・処理前。**stdout がコンテキスト注入**。exit 2 で prompt をブロック＆消去 | ✅ **採用（user-prompt-submit-guard.sh・#23）** |
| 4 | `UserPromptExpansion` | ユーザー入力コマンドが prompt に展開される前。**stdout 注入**・展開ブロック可 | ⬜ 未採用 |
| 5 | `PreToolUse` | ツール実行前（matcher: tool_name）。permissionDecision で allow/deny/ask/defer | ✅ 採用（pre-tool-use-router.sh・Bash） |
| 6 | `PermissionRequest` | 権限プロンプト発生時。decision.behavior で allow/deny | ✅ 採用（permission-request-auto-allow.sh） |
| 7 | `PermissionDenied` | 権限が拒否されたとき | ⬜ 未採用 |
| 8 | `PostToolUse` | ツール成功後（matcher: tool_name） | ✅ 採用（post-tool-use-validate.sh / post-merge-publish-check.sh・#449） |
| 9 | `PostToolUseFailure` | ツール失敗後 | ✅ 採用（post-tool-use-failure.sh・Bash） |
| 10 | `PostToolBatch` | 並列ツール呼び出しのバッチ解決後・次のモデル呼び出し前 | ⬜ 未採用 |
| 11 | `Notification` | Claude Code が通知を送るとき（matcher: permission_prompt/idle_prompt 等・`message` フィールド）。decision 制御なし | ⬜ **未採用（観測専用・後述）** |
| 12 | `MessageDisplay` | メッセージ表示時 | ⬜ 未採用 |
| 13 | `SubagentStart` | サブエージェント開始時 | ⬜ 未採用 |
| 14 | `SubagentStop` | サブエージェント終了時 | ✅ 採用（subagent-stop.sh） |
| 15 | `TaskCreated` | タスク作成時 | ⬜ 未採用 |
| 16 | `TaskCompleted` | タスク完了時 | ⬜ 未採用 |
| 17 | `Stop` | Claude が応答を終えるとき。decision: block で継続 | ✅ 採用（stop-router.sh） |
| 18 | `StopFailure` | API エラーでターンが終わるとき。**出力・exit code は無視される** | ⬜ 未採用（出力無視のため副作用フック不可） |
| 19 | `TeammateIdle` | Agent Team のチームメイトがアイドルになったとき | ⬜ 未採用 |
| 20 | `InstructionsLoaded` | CLAUDE.md / ルール再注入時（load_reason: compact 等） | ⬜ 未採用（ログ用途のみ） |
| 21 | `ConfigChange` | 設定変更時 | ⬜ 未採用 |
| 22 | `CwdChanged` | 作業ディレクトリ変更時 | ⬜ 未採用 |
| 23 | `FileChanged` | ファイル変更時 | ⬜ 未採用 |
| 24 | `WorktreeCreate` | git worktree 作成時 | ⬜ 未採用 |
| 25 | `WorktreeRemove` | git worktree 削除時 | ⬜ 未採用 |
| 26 | `PreCompact` | 圧縮開始前（matcher: manual/auto）。exit 2 で圧縮ブロック可 | ✅ **採用（pre-compact.sh・#23）** |
| 27 | `PostCompact` | 圧縮完了後（matcher: manual/auto）。decision 制御なし。**stdout 注入・additionalContext とも非対応（side-effect 専用・2026-07-12 再フェッチ確認）** | ✅ 採用（post-compact.sh・出力は stderr ログのみ） |
| 28 | `Elicitation` | 入力要求ダイアログ時 | ⬜ 未採用 |
| 29 | `ElicitationResult` | 入力要求の結果確定時 | ⬜ 未採用 |
| 30 | `SessionEnd` | セッション終了時（matcher: clear/resume/logout/prompt_input_exit/bypass_permissions_disabled/other）。decision 制御なし | ⬜ **未採用（後述）** |

> 注: 公式ライフサイクル表は 31 個と明記しているが、一次フェッチで名称を確認できたのは上記 30 件。
> 残り 1 件は **未確認**（本フェッチでは名称を特定できなかった）。断定を避け、フック拡張時に
> 公式ドキュメントを再フェッチして本表を補完する。

## 2. stdout がコンテキスト注入されるイベント（重要）

ほとんどのイベントの stdout はデバッグログ止まりで Claude には見えない。**例外** は以下の 3 つのみ:

- `UserPromptSubmit`
- `UserPromptExpansion`
- `SessionStart`

これら以外のフックで Claude に情報を渡したい場合は、`hookSpecificOutput.additionalContext`
（Claude 向け）を JSON で返す。`systemMessage` は **ユーザー向け表示専用で Claude には届かない**
（Claude に行動させたい内容を `systemMessage` に書くのは #202 同型の「意図とチャネルの不一致」バグ）。

`hookSpecificOutput.additionalContext` を公式サポートするのは次の **11 イベントのみ**
（2026-07-12 公式ドキュメント再フェッチで確認・Issue #211）:
`SessionStart` / `Setup` / `SubagentStart` / `UserPromptSubmit` / `UserPromptExpansion` /
`PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `PostToolBatch`（ツール結果の隣に挿入）/
`Stop` / `SubagentStop`（ターン終了時に注入）。
**`PreCompact` / `PostCompact` はどちらも非対応**（side-effect 専用。Claude に読ませる出力は書けない）。

> **⚠️ `exit 2` と stdout JSON は排他（Issue #142）**: ブロック用に `exit 2` するフックが stdout に
> `systemMessage` 等の JSON を出しても無視される（"Don't mix them: Claude Code ignores JSON when you
> exit 2"）。ブロック理由を Claude に届けるには **stderr にプレーンテキストで出力してから `exit 2`**
> する（`.claude/hooks/lib/hook_block.sh` の `hook_block "理由"` を使う）。JSON 構造化判定
> （`permissionDecision` 等）を使う場合は `exit 0` + stdout JSON の組み合わせにする。

## 2.5 SessionStart の詳細仕様（source / timeout / JSON 出力 / setup script との使い分け）

2026-07-14 に公式ドキュメント（hooks / claude-code-on-the-web）を一次フェッチして裏取りした要点。
本ベースの `session-start.sh` の設計判断（Issue #248 / #249 / #250）はこれに基づく。

### タイムアウト

- **既定タイムアウト = 600 秒**（`command` / `http` / `mcp_tool` タイプ）。`prompt` = 30 秒・`agent` = 60 秒。
- **per-hook `timeout` フィールドで変更可**（`.claude/settings.json` の各 hook エントリ）。
- `UserPromptSubmit` は `command`/`http`/`mcp_tool` の既定を 30 秒に、`MessageDisplay` は 10 秒に下げる。
- SessionStart は 600 秒あるが、毎セッション走るため軽量に保つ（重い依存導入は下記 setup script へ）。

### 入力 JSON の `source` フィールド（matcher と同値）

- SessionStart の input JSON には `source`（`startup` / `resume` / `clear` / `compact`）が入る。
- matcher でも同じ 4 値で分岐できる（settings.json の `matcher`）。
- **本ベースの活用**: `session-start.sh` は stdin JSON から `source` を読み、**破壊的なワーキングツリー
  クリーンアップ（reset/checkout/clean）を `startup` のときのみ実行**する。`startup` は新規クローン直後で
  失う未コミット変更がないため安全。`resume`/`compact`/`clear` では未コミット作業を消さない（Issue #248・
  L-100 ①）。env 永続化・依存インストール・main fetch・stdout 注入は全 source で維持する。

### SessionStart の JSON 出力フィールド（`hookSpecificOutput`）

plain stdout 注入（§2）に加え、以下の event-specific フィールドを JSON で返せる:

| フィールド | 用途 |
|-----------|------|
| `additionalContext` | 会話開始前に Claude のコンテキストへ追加する文字列（plain stdout と等価の注入経路） |
| `initialUserMessage` | セッション最初のユーザーメッセージ（`-p` の非対話モードのみ有効） |
| `sessionTitle` | セッションタイトルを自動設定（`/rename` 相当）。`startup`/`resume` のみ有効・`clear`/`compact` は無視 |
| `watchPaths` | `FileChanged` イベントで監視する絶対パスの配列 |
| `reloadSkills` | `true` でフック完了後に skill/command ディレクトリを再スキャン（フックが動的導入した skill を即利用可能に） |

> **本ベースの選択**: 現状は plain stdout 注入（言語リマインダー + スナップショット）を使い、`sessionTitle`
> 等の JSON 出力は未使用。plain stdout と JSON 出力の混在は避ける（stdout 全体が JSON パース対象になり
> 得るため）方針で、load-bearing な言語リマインダー注入を壊さないことを優先している。将来 `sessionTitle`
> でセッション自動命名（ブランチ名/Issue 番号）を採るなら、全注入を `additionalContext` に移す形で慎重に行う。

### setup script（クラウド環境）と SessionStart フックの使い分け

| 観点 | setup script | SessionStart フック |
|------|-------------|---------------------|
| 紐付け先 | クラウド環境（Claude.ai の環境設定 UI） | リポジトリ（`.claude/settings.json`） |
| 実行タイミング | Claude Code 起動 **前**（初回実行後 約 7 日ファイルシステムキャッシュ） | 起動 **後**・毎セッション（キャッシュなし） |
| スコープ | クラウド環境のみ | ローカル + クラウド両方（`CLAUDE_CODE_REMOTE` でガード可） |
| 向き | 重い導入（言語ランタイム・Docker イメージ・大型依存） | 軽量なプロジェクト準備（`npm install` 等の冪等・高速処理） |

> **指針**: SessionStart は毎セッション走りキャッシュされないため、5 分超かかる導入は setup script 側へ移す。
> 本ベースの `session-start.sh` は依存が軽量（PyYAML のみ・実測 1 秒未満）のため SessionStart 内で完結させている。
> 派生プロジェクトで重いツールチェーン導入が必要になったら setup script へ委ねる。

## 3. #23 で新規採用したフックと役割整理

### UserPromptSubmit（user-prompt-submit-guard.sh + prompt-structuring.sh + orchestrator-directive.sh）
UserPromptSubmit には 3 フックを配線する（settings.json で guard → structuring → orchestrator-directive
の順）。stdout は全てコンテキストに注入されるが、役割を分離している:

- **user-prompt-submit-guard.sh**（安全助言）: ユーザー入力の高リスクパターン（main 直 push / rm -rf /
  .env・秘密情報 / フック無効化 / settings.local.json への env 書き込み）を検出し、関連ガードレールを
  **助言としてコンテキスト注入** する。既定は **非ブロッキング**（exit 0）。誤検知で正当作業を止めない
  ため。真に破壊的なパターンをブロックしたいプロジェクト向けに exit 2 のブロック例をコメントで残してある
- **prompt-structuring.sh**（プロンプト自動構造化・Issue #172）: ユーザーの生指示（タスク依頼）を着手前に
  作業スペックへ展開させる構造化ディレクティブを注入する。公式仕様で生プロンプトの置換は不可
  （"can't replace the prompt"）のため、stdout 注入で近似する workaround。トグル
  `CLAUDE_PROMPT_STRUCTURING=auto|off|always`（既定 auto）。**高リスク入力を検出したら本フックの注入を
  完全抑制** し guard の助言だけを残す（二重バナー防止）。詳細 SSOT は `docs/rules/prompt-structuring-rules.md`
- **orchestrator-directive.sh**（高コストモデル検出時のオーケストレーター指示・Issue #184）: セッションが
  高コストモデル（Opus/Fable 系）で動作しているとき、「専門チームを組成してタスクを遂行せよ」という
  ディレクティブを注入する。トグル `CLAUDE_ORCHESTRATOR_DIRECTIVE=auto|off|always`（既定 auto）。
  **高リスク入力を検出したら本フックの注入も抑制** し guard の助言だけを残す（二重バナー防止）

### PreCompact（pre-compact.sh）と PostCompact（post-compact.sh）の役割分担
- **PreCompact**: 圧縮が始まる *前* に未コミット変更を WIP コミット＆push（L-100 の一次防御）。
  圧縮処理中の不具合や圧縮後の SessionStart クリーンアップで作業が消える前に、最も早く確定させる
- **PostCompact**: 圧縮 *後* の symlink 同期 + 二次的な WIP セーフティネット。
  PreCompact が確定済みなら working tree は clean になり二重コミットは発生しない。
  ルール再確認リマインダーは SessionStart が担当（PostCompact は stdout 注入非対応・Issue #211）

## 4. 未採用イベントの採否理由（明示）

| イベント | 未採用の理由 |
|---------|------------|
| `SessionEnd` | WIP 保全は `Stop`（stop-router.sh）+ `PreCompact`/`PostCompact` で既にカバー済み。logout 時のクリーンアップが必要なプロジェクトのみ追加する（settings.json に `SessionEnd` を追記）。汎用ベースでは冗長コミットを避けるため未採用 |
| `Notification` | decision 制御なしの観測専用。Slack 通知は `tools/slack_notify.py` で能動制御しており、idle/permission プロンプトを横取りする用途がないため未採用。observability が欲しいプロジェクトのみ採用 |
| `StopFailure` | 出力・exit code が無視されるため副作用フックを置けない（API エラー時のログは別経路で取得） |
| `PostToolBatch` / `InstructionsLoaded` 他 | 現行ハーネスの守備範囲（main 直 push 防止・PR フロー・圧縮保全）に必要十分。将来要件が出たら本表の採否を更新する |

## 4.5 フック入力 JSON のフィールド名は公式スキーマで検証する（#367）

フックが **公式スキーマに存在しないフィールド** を読むと、`jq` も `.get()` も既定値を返すだけでエラーにならず、
**フックが静かに壊れる**。しかもフックの出力（`additionalContext`）は Claude の観測になるため、壊れたフックは
「偽の観測」をオーケストレータに注入する。実害例（#367）: `subagent-stop.sh` が非実在の `is_error` / `result`
を読んでいたため、サブエージェントの異常終了時に **中身が空の異常報告**（`最終出力（末尾）: ` が常に空）を
注入し、成果が失われたかのような誤解を招いていた。

**必須手順**: フックを新規作成・改修するときは、読むフィールド名を
[公式 hooks ドキュメント](https://code.claude.com/docs/en/hooks) の該当イベントの **JSON 例（逐語）** と
突き合わせる。要約や記憶で書かない（要約経路はフィールド名を取り違えることがある・L-113 の姉妹則）。

### Stop / SubagentStop の入力 JSON（2026-07-29 逐語確認・v2.1.220）

```json
{
  "session_id": "abc123",
  "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/home/user/.claude/projects/.../transcript.jsonl",
  "cwd": "/home/user/my-project",
  "permission_mode": "default",
  "hook_event_name": "SubagentStop",
  "agent_id": "subagent-123",
  "agent_type": "Explore",
  "last_assistant_message": "I've completed the exploration...",
  "stop_reason": "end_turn"
}
```

- `Stop` は `agent_id` / `agent_type` を持たない（それ以外は同形）。**加えて `stop_hook_active` / `background_tasks` / `session_crons` を持つ**（公式逐語・2026-09-03 再確認・#543）: 「Stop hooks receive `stop_hook_active`, `last_assistant_message`, `background_tasks`, and `session_crons`. The `stop_hook_active` field is `true` when Claude Code is already continuing as a result of a stop hook. […] Claude Code overrides the hook and ends the turn after 8 consecutive blocks.」上の JSON 例はこれらを省いた抜粋であり、フィールドの非存在を意味しない。
- **差し戻し（exit 2 / `decision: "block"` / `additionalContext`）は `type=user` / `isMeta=true` のメッセージとして届き、本文は `Stop hook feedback:` で始まる**（本セッション transcript で実測・#543）。複数フックがブロックするとこのブロックがフックの数だけ連続する。`stop_hook_active` が防ぐのは同一 Stop 試行の直後 1 回だけで、**メインが新しい final message を出すたびに新しい Stop 試行になり再び発火する**（「1 セッション 1 回」ではない）。続行ターンの規律は `completion-report-rules.md` §1.2。
- **`is_error` / `result` / `exit_reason` は存在しない**。最終出力は必ず `last_assistant_message` から取る。
- `transcript_path` は非同期書き込みで **現在ターンの最新メッセージを含まないことがある**。
  ターンの最終テキストが要るときは transcript を読まず `last_assistant_message` を使う（公式明記）。
- `stop_reason` の値域は公式に列挙がない。**判定は fail-safe に倒す**
  （既知の異常語を含むときだけ発話し、空・未知は無音にする。誤検知で Claude の観測を汚さないことを優先する）。

## 5. サブエージェント persistent memory の frontmatter キー（#23 検証）

公式（[sub-agents docs](https://code.claude.com/docs/en/sub-agents)）で確認した正しいキーは **`memory`**（`persistent_memory` ではない）。

```yaml
---
name: code-reviewer
description: ...
memory: user   # user | project | local
---
```

| スコープ | 保存先 | 用途 |
|---------|--------|------|
| `user` | `~/.claude/agent-memory/<name>/` | 全プロジェクト横断で学習を蓄積 |
| `project` | `.claude/agent-memory/<name>/` | プロジェクト固有・バージョン管理で共有 |
| `local` | `.claude/agent-memory-local/<name>/` | プロジェクト固有・コミットしない |

- サブエージェントの全 frontmatter キー: `name`(必須) / `description`(必須) / `prompt` / `tools` /
  `disallowedTools` / `model` / `permissionMode` / `mcpServers` / `hooks` / `maxTurns` / `skills` /
  `initialPrompt` / `memory` / `effort` / `background` / `isolation` / `color`
- **Plugin 経由のサブエージェントは `hooks` / `mcpServers` / `permissionMode` が無視される**
  （`.claude-plugin/plugin.json` はサブエージェントを配布しない（`skills` のみ宣言）ため影響なし・#538）

## 6. 参照

| ドキュメント | 関係 |
|------------|------|
| `.claude/settings.json` | フック登録の実体 |
| `docs/rules/session-compression-rules.md` | PreCompact/PostCompact の運用文脈 |
| `docs/rules/harness-escalation.md` | フック Lv3 昇格の判断 |
| `docs/rules/lessons-core.md` | L-100（未コミット作業消失）の対策 |
