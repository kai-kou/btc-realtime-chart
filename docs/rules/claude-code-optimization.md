# Claude Code 最新機能リファレンス & ワークフロー最適化

> 最終更新: 2026-07-03
> 対象: Claude Code v2.1.219+, Claude 4.6/4.7/4.8/**5** モデルファミリー

> **2026-07-24 追記②（モデル指定のエイリアス化・#314）**: モデル世代交代のたびにリポジトリを書き換える運用をやめ、**運用ファイルのモデル指定は `opus` / `sonnet` / `haiku` / `fable` のエイリアス既定** に統一した（公式: 「Aliases point to the recommended version for your provider and update over time」・Anthropic API では `opus`→Opus 5 / `sonnet`→Sonnet 5・v2.1.219+）。エイリアスは `/model`・`claude --model`・`ANTHROPIC_MODEL`・`settings.json` の `model`・サブエージェント/スキル frontmatter の `model`・Agent ツールの `model` パラメータで使える（Agent ツールは **エイリアスのみ** 受理）。厳密 ID を書く例外は ① API 直叩き ② 料金表など ID をキーにする実装 ③ 再現性のためのピン留め の 3 つ。SSOT は `agent-team.md`「モデル指定の方針」。

> **2026-07-24 追記（Opus 5 リリース）**: **Claude Opus 5（`claude-opus-5`）が現行 Opus** となり、本ファイル・`agent-team.md` の推奨モデルを Opus 4.8 → Opus 5 に更新した（Opus 4.8/4.7/4.6 は legacy 参考として残置）。Opus 4.8 と **同価格（$5 / $25）・1M コンテキスト・128K 出力のドロップイン更新**。差分は ① **thinking が既定 ON**（未指定でも adaptive。4.8/4.7 は未指定＝思考なし。`max_tokens` は thinking + 応答の合計上限のため余裕を持たせる） ② **thinking 無効化は effort `high` 以下限定**（`xhigh`/`max` と併用すると HTTP 400） ③ prompt cache 最小長 512 トークン（4.8 は 1024） ④ レート枠は Opus 4.x プールと別枠 ⑤ Fast mode（`/fast`）対象は Opus 5 / 4.8（4.7 は v2.1.219 で除外・#306）。デフォルト effort は `high` で 4.8 と同じ。**Opus 5 は `low`/`medium` でも高品質** なため、コスト削減はモデル変更より先に effort を下げて評価する。挙動の癖（出力が長い・自己検証が過剰になりやすい・スコープを広げがち・サブエージェント委譲に積極的）は `agent-team.md` のモデル一覧注記を参照。

> **2026-07-03 追記（ユーザー指示による事実確認・`/deep-research` のネイティブ実行）**: `code.claude.com/docs/en/workflows`・`/en/commands` を Fetch して確認した結果、`/deep-research` は公式に **Workflow** 分類（Skill ではない）で、CLI・Desktop・IDE拡張・`claude -p`・Agent SDK のいずれでも同一に動作し、**クラウド実行環境のメインセッションから `claude -p` サブプロセスを介さず直接 invoke できる** ことが確定した。モデルは既定でセッションのモデルを使用（スクリプトが明示的に別モデルへ routing しない限り）— 本プロジェクトの `run_deep_research_workflow.py` が Opus を使うのは明示指定によるプロジェクトの選択であり、ネイティブ機能自体が Opus 固定という意味ではない。手書き `.js` ワークフロー（`Workflow` ツール）も本セッションで実際に呼び出し可能なことを確認（ユーザー明示オプトイン時のみ使用する制約あり）。詳細は `docs/rules/dynamic-workflows-rules.md` と `.claude/skills/research-runner/SKILL.md`（Step 3a/3b）を参照。

> **2026-06-05 追記（v2.1.154〜v2.1.163・Opus 4.8）**: **Opus 4.8（`claude-opus-4-8`）が v2.1.154 でデフォルト化**（`/model opus` が 4.8 に解決）。**重要: Opus 4.8 のデフォルト effort は `high`**（4.7 は `xhigh`・切替時に自動リセット）。Fast mode（`/fast`）も Opus 4.8 へ・最大 2.5x 高速（料金 2x）。**Dynamic Workflows（`/workflows`・`ultracode`）** で多エージェント動的オーケストレーション。`/code-review --fix` で指摘自動修正。SessionStart フックで `sessionTitle`/`reloadSkills` 設定可。Stop/SubagentStop フックが `additionalContext` でフィードバック注入可。**`claude -p`（headless）はこのクラウド環境でも動作**（詳細は「`claude -p` ヘッドレス実行」セクション）。**2026-06-15 以降 `claude -p` / Agent SDK は別枠 Agent SDK Credit 課金**（要確認）。詳細は各セクション・末尾の新設セクション参照。

> **2026-05-19 追記（v2.1.143）**: バックグラウンドセッションが5分後に自動リタイア（モデル・effort を保持）・MCP tools/list がページネーション全件取得に対応・ `worktree.bgIsolation: "none"` オプション追加（作業ディレクトリの直接編集を許可・CP-4 注意）・ローカル設定がリモートマネージド設定をオーバーライドしてしまう問題を修正（クラウド環境の安定性向上）。詳細は「v2.1.143 変更点」セクション参照。

> **2026-05-08 追記** : `/effort` インタラクティブスライダー（v2.1.111）・Auto mode for Max subscribers（v2.1.111）・`alwaysLoad` MCP オプション（v2.1.121）が追加。長時間セッション読み込みが最大 67% 高速化。詳細は各セクション参照。

> **2026-04-18 追記** : Claude Opus 4.7（2026-04-16 リリース）と xhigh effort（5段階目）が追加。PreCompact フック（v2.1.105）新設で圧縮前制御が可能に。詳細は各セクション参照。

> **2026-04-11 追記** : `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` を `settings.json` の `env` セクションに追加済み。詳細は「Agent Teams（実験的機能）」セクションを参照。

本ドキュメントは Claude Code の最新機能・仕様・制限を一元管理し、本プロジェクトのワークフロー最適化に活用するためのリファレンス。定期的に最新情報をリサーチして更新する（CP-2 に基づく）。

## モデルファミリーと性能特性

### Claude 4.5/4.6 モデル一覧（2026-03 時点）

モデルの性能・料金・用途の使い分けは `docs/rules/agent-team.md` の「モデル一覧」を参照。本ファイルでは Claude Code 固有の最適化（コンテキスト活用・`/effort` コマンド）に絞って記載する。

> **最新情報（2026-03）**: Haiku 4.5 が Extended Thinking に初対応。また Opus 4.6 の料金は旧世代（Opus 4.1）比で 67% 削減されており、コストパフォーマンスが大幅に向上している。

### Adaptive Thinking（適応的思考）

現行の Opus / Sonnet（および Opus 4.x）に搭載。クエリの複雑さに基づき、内部で思考量を自動調整する（現行 Opus は既定 ON）。

- 単純なタスク → 思考を最小化（速く・安く）
- 複雑なタスク → 深く考える（品質優先）
- **Interleaved Thinking**: ツール結果を受け取った後に思考を挟める（Claude 4 全モデル対応）。multi-agent pipeline での推論精度が向上する

### Auto mode for Max subscribers（v2.1.111）

Claude Max サブスクライバーが Opus 系で利用できるモード。Adaptive Thinking が自動調整されるため、`/effort` を明示的に設定しなくてもタスク複雑度に応じて思考量が最適化される。

**`/effort` との関係**:
- Auto mode は Adaptive Thinking の **自動制御**。`/effort` は **手動上書き**
- `/effort xhigh` を指定すると Auto mode の自動調整より xhigh を優先する
- コスト重視の場合は Auto mode + `/effort low` で抑制可能
- **スケジュールタスク（headless）**: Auto mode も `/effort` も有効。コスト管理のため `medium` を推奨（`opus` + xhigh は高コストのため、日常的な自動パイプラインでは `medium` でバランスを取る）

### `/effort` コマンド（思考深度の明示制御）

Claude Code セッション内で思考の深度を設定できる。API レベルでは `output_config: {effort: "..."}` に対応する。

> **v2.1.111 追加**: 引数なしで `/effort` を実行すると矢印キーで操作できる **インタラクティブスライダー** が表示される。スライダーで選択後 Enter で確定。選択肢が視覚的に表示されるため、5段階の違いが把握しやすい。

| コマンド | API 値 | 思考深度 | 適用場面 |
|---------|--------|---------|---------|
| `/effort low` | `"low"` | 最小・大幅なトークン節約 | 定型チェック・軽量調査・ルーティング判断 |
| `/effort medium` | `"medium"` | バランス・適度なトークン節約 | 通常の実装・PR 対応（Sonnet 5 に特に推奨） |
| `/effort high` | `"high"` | 複雑なタスクに対応 | 複雑な実装・設計判断・ファクトチェック |
| `/effort xhigh` | `"xhigh"` | コーディング/エージェント向け最適 | **`opus` 推奨**・台本生成・複数ファイルリファクタリング・アーキテクチャ設計（現行 Opus / Sonnet は既定 `high` のため明示指定が必要） |
| `/effort max` | `"max"` | 絶対最大・トークン無制限 | 最深の推論が必要な設計・アーキテクチャ決定。実用上は xhigh で十分なことが多い |

> **2026-07-24 更新（Opus 5）**: effort は 5 段階（`low` / `medium` / `high` / `xhigh` / `max`）。**現行 Opus のデフォルトは `high`**（旧 Opus 4.7 のみ `xhigh`）。公式推奨は「コーディング/エージェントは `xhigh`、それ以外の知能重視は `high` を基準に eval で per-route 調整」。現行 Opus では **`low`/`medium` でも品質が高い** ため、コスト削減はまず effort を下げて評価する。`max` は Opus 系と Sonnet・Fable で利用可。
> **注意**: `/effort high` は現行 Opus のデフォルト。意識的にコストを下げたい場合は `medium` または `low` を指定する。
> **Opus 推奨**: 台本生成品質の観点から **`/model opus` + `/effort xhigh` を明示指定**。現行 Opus は既定が `high` のため、xhigh を使うには明示指定が必須。
> **`ultracode`（Opus 系セッション限定）**: `xhigh` + Dynamic Workflows（`/workflows`）。大規模・多エージェント作業の動的オーケストレーション。`settings.json` には書けない（セッション内のみ）。1ターンだけ深い推論が欲しい場合は `ultrathink` キーワードをプロンプトに含める（effort 設定は変わらない）。

**effort がサブエージェントに与える影響**:
- `/effort` コマンドはメインセッションの思考深度を設定する
- Agent tool で起動したサブエージェントは **独立したセッション** のため、メインセッションの effort 設定を **引き継がない**
- サブエージェントのコスト最適化は **`model` パラメータによるモデル選択** が最も効果的（`agent-team.md` 参照）
- サブエージェントプロンプトに「簡潔に・要点のみ」等の指示を入れることで間接的に出力量を削減できる

**推奨**:
- 台本生成（**`opus`** 使用時）: `/effort xhigh` を **明示指定**（現行 Opus の既定は `high`）。コーディング/エージェント/知能重視に最適
- Sonnet 5 で通常作業: `/effort medium` でコスト削減
- 定型・ルーティング判断: `/effort low` で高速化

**ブラウザ版（claude.ai/code）での effort 設定**:
- `/effort` コマンドは **CLI 専用**。ブラウザ版では `Unknown skill: effort` エラーになる
- ブラウザ版は画面下部のステータスバーに **Effort ドロップダウン**（Low/Med/High/Max）がある
- **最も効果的な方法**: スキルの frontmatter に `effort:` を設定すると、スキル実行中のみ自動的に effort が切り替わる（下記参照）

### スキル frontmatter の `effort` フィールド（ブラウザ版でも有効）

スキルの `SKILL.md` の frontmatter に `effort:` フィールドを追加すると、そのスキル実行中だけ effort が自動設定される。**ブラウザ版・CLI 版の両方で動作する** 唯一の effort 自動制御手段。

```yaml
---
name: script-writer
description: ...
effort: high   # このスキル実行中のみ effort: high が適用される
---
```

本プロジェクトのスキル別 effort 設定（`.claude/skills/*/SKILL.md` に設定済み）:

| effort | スキル |
|--------|--------|
| `xhigh` | `script-writer`（`opus` で明示指定・2026-07-24 更新） |
| `high` | `fact-checker`, `self-reviewer`, `script-team-reviewer` |
| `medium` | `script-pipeline`, `theme-discovery`, `metadata-reviewer`, `retrospective` |
| `low` | `refinement`, `retro-try-handler` |
| （未設定） | その他のスキル |

> **2026-07-24 更新**: `script-writer` は `effort: xhigh`。現行 Opus はデフォルト effort が `high` のため、frontmatter で `xhigh` を明示することで台本生成時のみ深い推論を確保する（旧: Opus 4.8 + xhigh）。

### `/advisor` コマンドと Advisor Tool（2026-04 確認済み）

> **最終確認**: 2026-04-18 (Claude Code v2.1.111)

Anthropic が 2026-03 に発表した **Advisor Strategy**。実行モデル（Executor: Sonnet/Haiku）が複雑な判断に差し掛かった際、より高性能なアドバイザーモデル（Advisor: Opus）に相談する機能。コスト削減と品質向上を両立する。

#### 機能概要

| モード | 概要 | 呼び出し方 |
|--------|------|-----------|
| **インタラクティブ `/advisor`** | セッション内でモデルペアを設定 | Claude Code セッションで `/advisor` と入力 |
| **advisor_tool（API Beta）** | Executor が自動的に Advisor を呼び出す（API キー環境のみ）| `anthropic-beta: advisor-tool-2026-03-01` ヘッダー + ツール定義 |

#### ベンチマーク（Anthropic 測定値）

| 構成 | 品質向上 | コスト削減 |
|------|---------|-----------|
| Haiku（Exec）+ Opus（Advisor）| BrowseComp +121%（19.7→41.2%）| **-85%**（Sonnet 単独比） |
| Sonnet（Exec）+ Opus（Advisor）| SWE-bench +2.7% | **-11.9%** |

#### 本環境（Claude.ai OAuth）での動作確認結果

```
確認日: 2026-04-11 / Claude Code v2.1.101

[✅] インタラクティブ /advisor コマンド
  → Claude.ai ブラウザ版（ユーザーが手動でセッションを開いた場合）で使用可能
  → スケジュールタスク（自動実行）では非インタラクティブのため使用不可

[❌] advisor_tool（API Beta）
  → --betas "advisor-tool-2026-03-01" を渡すと
    "Warning: Custom betas are only available for API key users. Ignoring provided betas."
  → 本環境（Claude.ai OAuth 認証）ではベータ機能が無効化される
  → 自動パイプラインへの組み込みは現時点では不可

[将来対応] API キーへの切り替えを行った場合は advisor_tool が利用可能になる
```

#### 本プロジェクトでの推奨利用方法

**今すぐ使える（手動セッション）**:
- ユーザーが台本生成・複雑な設計判断をする際、セッション冒頭で `/advisor` を入力して Sonnet+Opus advisor に切り替える
- 特に `script-writer` / `fact-checker` の実行前に有効

```
手動セッションでの推奨フロー:
1. https://claude.ai/code を開く
2. `/advisor` を入力 → Sonnet（Exec）+ Opus（Advisor）を選択
3. /script-writer や /fact-checker を実行
→ 品質が向上し、Opus 単独より安くなる
```

**自動パイプラインへの将来対応（API キーへ切り替え後）**:
```python
# Anthropic API key 使用時の advisor_tool 組み込み例
response = client.beta.messages.create(
    model="claude-sonnet-5",   # Executor: Sonnet（低コスト実行）
    betas=["advisor-tool-2026-03-01"],
    tools=[{
        "type": "advisor_20260301",
        "name": "advisor",
        "model": "claude-opus-5",  # Advisor: Opus（高精度判断・API 直叩きのため full ID が必須＝エイリアス例外①）
        "max_uses": 3
    }],
    messages=[...]
)
```

#### 推奨利用シーン（優先度順）

| シーン | Executor | Advisor | 期待効果 |
|--------|---------|---------|---------|
| 台本生成（Phase 3） | Sonnet | Opus | 品質向上、コスト削減（Opus 単独より安価） |
| ファクトチェック | Haiku | Opus | コスト -85%（Sonnet 比）、品質は Opus 水準 |
| セルフレビュー | Haiku | Opus | 軽量チェック + 深いレビュー |
| 画像生成判断 | Haiku | Opus | edge case の高精度判定 |

#### 公式リソース

- [Advisor Tool API ドキュメント](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)
- [The advisor strategy ブログ](https://claude.com/blog/the-advisor-strategy)
- [Claude Code コマンド一覧](https://code.claude.com/docs/en/commands)

### コンテキストウィンドウ活用戦略

| 状況 | 推奨戦略 |
|------|---------|
| 台本生成（Phase 3）| Opus の 1M コンテキストを活用し、リサーチ全文 + キャラ設定 + 既存台本を同時参照 |
| 画像パイプライン | Sonnet 5 でデザイン定義書 + 全 visual_cue を一括処理（圧縮リスク低減） |
| レビュー・検証 | Haiku でコンテキスト効率最大化（不要な情報を渡さない） |
| 長時間パイプライン | 圧縮は Claude 標準の Auto Compaction（コンテキスト上限付近で自動発動）に委ねる。PostCompact フックが自動コミットで作業を保護する |

#### tool call パースエラーの緩和（2026-06-06 追加）

**問題**: `The model's tool call could not be parsed (retry also failed).` でセッションが頻繁に停止する。Opus 4.7 / 4.8 + 1M コンテキスト + 強い thinking の組み合わせで発生する Claude Code 側の既知バグ（公式 Issue #61133 / #62344 / #64658・2026-06-06 時点で未解決）。モデルが構造化 tool_use ブロックの代わりにレガシー `<invoke>` XML（ラッパー欠落・JSON 途中切れ）を吐き、それが履歴に残ると自己回帰生成が模倣して同一セッション内の retry が確定的に再失敗する（in-context few-shot poisoning）。本プロジェクトは `claude-opus-4-8[1m]`・巨大 CLAUDE.md/常駐ルール・日本語＋コード混在とトリガー条件に直撃する。詳細は L-101 を参照。

**対策（多層防御）**:

| 対策 | 設定箇所 | 効果 |
|------|---------|------|
| 発生時は **retry せず `/clear` か新規セッション** | 運用ルール | 壊れた tool_use が混入したセッションは破損状態。retry は毒入りお手本を再生するだけで逆効果 |
| 高負荷でない工程は Sonnet 5 | `/model` | Sonnet 5 ではほぼ再現しない（台本生成など Opus 必須工程のみスポット使用） |
| 1 ターンのツール呼び出しを 8 個以下 | `session-safety-rules.md` | 連続 Bash 呼び出しで誘発されやすいため緩和 |

> **圧縮タイミングは Claude 標準に委ねる**: 圧縮タイミングを env（`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 等）で固定すると、パイプライン中の圧縮過多や任意の閾値での強制圧縮が起きうる。本ベースは Claude 標準の Auto Compaction（コンテキスト上限付近で自動発動）に委ね、env での明示指定はしない。圧縮後の作業は PostCompact フックの自動コミットで保護するため、標準タイミングでも作業ロスのリスクは低い。

**参照**: [Claude Code env vars 公式ドキュメント](https://code.claude.com/docs/en/env-vars)、公式 Issue [#61133](https://github.com/anthropics/claude-code/issues/61133) / [#62344](https://github.com/anthropics/claude-code/issues/62344)（few-shot poisoning）/ [#64658](https://github.com/anthropics/claude-code/issues/64658)

### コスト削減戦略

コスト削減戦略の詳細は `docs/rules/agent-team.md` の「コスト最適化の優先順位」を参照。

#### レスポンス verbosity 最適化（2026-05-20 追加）

**問題**: ユーザーのフィードバック・指摘に対し、「いい指摘にゃ。確認してみるにゃ。」のような確認応答（acknowledgment）をツール呼び出し前に出力すると、無駄なトークンを消費する。

**対策（多層防御）**:

| 対策 | 設定箇所 | 効果 |
|------|---------|------|
| `DISABLE_NON_ESSENTIAL_MODEL_CALLS=1` | `.claude/settings.json` の `env` | フレーバーテキスト・非必須モデル呼び出しを無効化（公式環境変数） |
| acknowledgment 禁止ルール | `CLAUDE.md` 応答スタイルセクション | ツール呼び出し前の確認応答テキストを明示的に禁止 |

**設定済み（2026-05-20）**:
```json
// .claude/settings.json
{
  "env": {
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1"
  }
}
```

**CLAUDE.md 追加ルール**:
> ユーザーの指摘・フィードバックに対して「いい指摘！」「確認してみる」等の確認応答をツール呼び出し前に出力しない。無言でツール実行に移り、結果が出てから初めて報告する。

**参照**: [Claude Code env vars 公式ドキュメント](https://code.claude.com/docs/en/env-vars)

## フック（Hooks）最新仕様

> **フックイベント名の実在性・採否決定・件数（公式 31 イベント）・stdout 注入対象・`exit 2`/JSON 排他性
> （Issue #142 再発防止）の唯一の正本（SSOT）は `docs/rules/hook-events-reference.md`。**
> 本ファイルには重複記載しない（旧テーブルは二重管理で件数表記が食い違う desync を招いたため撤去・Issue #149）。
> 本プロジェクトで実装済みのフックスクリプト一覧は `CLAUDE.md` 「ハーネス（`.claude/hooks/`）」表を参照。

**SessionStart フックの構造化出力フィールド**（実用メモ・hook-events-reference.md に未収録の詳細）:

| フィールド | 効果 | 本プロジェクトでの活用案 |
|-----------|------|----------------------|
| `hookSpecificOutput.additionalContext` | 返した文字列を Claude のコンテキストに注入 | Issue 一覧（status:in-progress/waiting-claude）の結果を注入しセッション再開プロトコル Step1〜4 を自動化（クラウドのフックからは gh が 403 のため取得失敗を明示する・L-114） |
| `sessionTitle` | `/rename` 相当でセッション名を自動設定 | hourly スロット名（例 `"09:00 sns-organic-pipeline"`）を付与し識別性向上 |
| `reloadSkills: true` | フック完了後にスキルを再スキャン | `check_rules_sync.sh` の symlink 修正後に即反映 |

**Stop / SubagentStop フックの `additionalContext`**: 品質ゲート結果・CI 状態・テスト結果を次ターンに注入できる（`stop-pr-check.sh` 等の補完）。

### PreCompact フックの設計パターン（v2.1.105+）

```bash
#!/bin/bash
# .claude/hooks/pre-compact.sh — 圧縮前処理の例

# ⚠️ 重要: trigger='auto' の場合は無条件で通過させる（絶対ルール）
# auto は「コンテキスト上限回復」のための強制圧縮。ブロックすると API エラーで
# リクエスト自体が失敗してパイプライン全体が止まる。
INPUT=$(cat)
TRIGGER=$(printf '%s' "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trigger',''))" 2>/dev/null)
if [ "$TRIGGER" = "auto" ]; then
  exit 0  # 強制通過 — ブロック禁止
fi

# 以下は trigger='manual'（/compact コマンド）のみ実行される
# パターン1: 未コミット変更を先行コミット（PostCompact より1ステップ早い保護）
if [ "${CLAUDE_CODE_REMOTE:-false}" = "true" ]; then
  BRANCH=$(git branch --show-current 2>/dev/null)
  if [[ "$BRANCH" != "main" && "$BRANCH" != "master" ]]; then
    CHANGES=$(git status --porcelain 2>/dev/null)
    if [ -n "$CHANGES" ]; then
      git add -A && git commit -m "[pre-compact] 手動圧縮前自動コミット" && git push
    fi
  fi
fi

# パターン2: パイプライン中間状態の保護（条件付きブロック）
# 特定の中間状態ファイルが存在する場合のみブロック
# if [ -f "/tmp/pipeline_critical_state" ]; then
#   echo "パイプライン重要中間状態のため圧縮をブロック（状態を保護中）" >&2
#   exit 2
# fi

exit 0
```

> **現状**: `PreCompact`（pre-compact.sh）は圧縮前の未コミット変更 WIP コミット & push（L-100 一次防御）として実装済み。`PostCompact` + `Stop` フックが二次防御を担う（`docs/rules/session-compression-rules.md` 参照）。上記コード例は当時の設計パターンの記録であり、実装は `.claude/hooks/pre-compact.sh` を正とする。
> **実装時の絶対制約**: trigger='auto' は無条件で通過（exit 0）。これを守らないとコンテキスト上限到達時に API エラーが表面化してリクエスト失敗が起きる（プロジェクト技術監修役レビュー 2026-04-18）。

### フック活用候補（旧提案・実装状況の記録）

以下 3 件は提案当時は未実装だったが、現在はいずれも実装済み（CLAUDE.md ハーネス表参照）。

1. **SubagentStart/SubagentStop ログ** → `subagent-stop.sh` として実装済み（サブエージェント異常終了の自己修正フィードバック）
2. **UserPromptSubmit バリデーション** → `user-prompt-submit-guard.sh` / `prompt-structuring.sh` / `orchestrator-directive.sh` として実装済み
3. **PreCompact フック** → `pre-compact.sh` として実装済み（上記参照）

## Agent Teams（実験的機能） — `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`

### 概要

Claude Code v2.1.32（2026-02-05 リリース）で追加された **実験的マルチエージェント協調機能** 。
本プロジェクトでは `settings.json` の `env` セクションに `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"` を設定済み（2026-04-11）。

**本プロジェクトの「Agent Teams パターン」（docs/rules/agent-team.md）とは別物**:
- プロジェクトが「Agent Teams」と呼ぶのは: Agent tool（ `subagent_type` ）による並列サブエージェント起動パターン
- この機能が指す「Agent Teams」: Claude Code プロセスを複数起動し、チームメイトとして協調させる公式機能

### 有効化で追加されるツール

| ツール | 機能 |
|--------|------|
| `TeamCreate` | チームメイトプロセスを生成（ `spawnTeam` ） |
| `TeamDelete` | チーム全体をクリーンアップ（ `cleanup` ） |
| `SendMessage` | チームメイト間でメッセージ送受信（ `message` / `broadcast` / `shutdown_request` / `shutdown_response` ） |

有効化確認: 上記3ツールが `settings.json` の `env` 設定後にセッションで使用可能になった（2026-04-11 動作確認済み）。

### 既存 Agent tool（サブエージェント）との関係

- **既存 Agent tool** （ `subagent_type` 指定）の動作は変更されない（公式ドキュメント確認済み）
- Agent Teams と subagents は **独立した機能として共存する**
- 既存の script/audio/image/video パイプラインへの悪影響: **なし**

### 環境別の動作状況（2026-04-11 調査）

| 環境 | 動作状況 | 詳細 |
|------|---------|------|
| **インタラクティブセッション（claude.ai/code 手動）** | ✅ **有効** | ツール追加確認済み。TeamCreate/SendMessage が使用可能 |
| **Scheduled Tasks（headless/SDK）** | ⚠️ **制限あり** | チームメイトプロセスはスポーンされるが、リードセッションがターン 10〜11 前後で終了するため、チームメイトの完了結果を受け取れない（Issue #1124）|
| **VS Code 拡張** | ❌ **機能しない** | `isTTY` チェックでブロック。Enterprise プランでも「not available on this plan」と表示される場合あり（Issue #28048）|

> **Scheduled Tasks での重要制約** : `TeamCreate` を明示的に呼ばない限り既存ワークフローへの影響はない。 `TeamCreate` を Scheduled Tasks から呼ぶと、チームメイト完了前にリードセッションが終了する問題が発生する。 **Scheduled Tasks 内での `TeamCreate` 使用は禁止とする** 。

### 本プロジェクトでの推奨利用方法

**今すぐ使える（インタラクティブセッションのみ）**:
- 複数の独立したコードベース変更を並列で進めるとき（例: SKILL.md と tools/ の同時改修）
- セルフレビューの並列実行をより高効率化したいとき

**使い方**:
```
/agent-teams start --team-name "review-team"
# → チームメイトが別ウィンドウ/ペインで起動
# → 共有タスクリスト（~/.claude/tasks/review-team/）を通じて協調
```

**既存 Agent tool との選択基準**:
| 状況 | 推奨 |
|------|------|
| 独立したファイル調査・生成（単発） | **Agent tool**（subagent_type） — 低コスト |
| Scheduled Tasks からの並列実行 | **Agent tool**（subagent_type） — headless 互換 |
| 手動セッションで長時間・複雑な協調タスク | **Agent Teams** — チームメイト間直接通信が有効 |
| セルフレビューの3役割並列実行 | **Agent tool**（既存 script-team-reviewer パターン） — 十分 |

### 無効化方法

悪影響が確認された場合は `settings.json` の `env` セクションから削除するか値を `"0"` にすることで即時無効化できる。

### 参照リソース

- [公式ドキュメント: Agent Teams](https://code.claude.com/docs/en/agent-teams)
- [Release v2.1.32（初出）](https://github.com/anthropics/claude-code/releases/tag/v2.1.32)
- [Issue #1124: SDK/headless モード非互換](https://github.com/anthropics/claude-code-action/issues/1124)
- [Issue #28048: VS Code 拡張での動作不可](https://github.com/anthropics/claude-code/issues/28048)

---

## サブエージェント最新機能

### 新しいパラメータ

| パラメータ | 型 | 効果 | 推奨設定 |
|-----------|-----|------|---------|
| `model` | `"haiku"` / `"sonnet"` / `"opus"` | サブエージェントのモデル選択 | タスク複雑度に応じて選択（`agent-team.md` 参照） |
| `isolation` | `"worktree"` | Git ワークツリーで隔離実行 | 並列ブランチ作業時に使用 |
| `run_in_background` | `boolean` | バックグラウンド実行 | 独立した長時間タスクに使用 |

### サブエージェントの MCP ツール継承（v2.1.101 修正済み）

> **修正日**: 2026-04-11（v2.1.101）/ 確認日: 2026-04-18

**修正前（v2.1.100 以前）**: サブエージェントが動的注入 MCP サーバーのツールを継承しなかった（Issue #13898）。gemini-image MCP や github MCP のツールをサブエージェントから呼び出すと、ツール不在でハルシネーションが発生するケースがあった。

**修正後（v2.1.101+）**: サブエージェントは Agent tool の `tools` フィールドを省略した場合、メインセッションの全 MCP ツールを継承するようになった。

**本プロジェクトへの影響**:
- `script-team-reviewer`・`image-pipeline` 等でサブエージェントから `mcp__gemini-image__generate_image` を呼び出すパターンが安定化
- `tools` フィールドを省略した Agent tool 呼び出しで全 MCP ツールが利用可能（意図的な設計が確実に動作する）
- worktree 隔離サブエージェントが自ワークツリー内ファイルへの Read/Edit を拒否される問題も同時に修正

### ワークツリー隔離（`isolation: "worktree"`）

サブエージェントを一時的な Git ワークツリーで実行し、メインの作業ディレクトリと完全に隔離する。

**活用シナリオ**:
- 画像パイプラインと音声パイプラインを **異なるブランチで並行実行**
- セルフレビューを隔離環境で実行（メインの作業を中断しない）
- 実験的な変更を安全にテスト

**制約**:
- ワークツリー内でファイルを変更しなかった場合、自動でクリーンアップされる
- 変更があった場合、ワークツリーのパスとブランチ名が返される

#### EnterWorktree への path パラメータ追加（v2.1.105+）

既存のワークツリーに `path` 指定で直接切り替えが可能になった（従来は新規作成との混同ケースあり）。

```python
# 既存のワークツリーを再利用する場合（v2.1.105+）
# isolation: "worktree" は新規作成。既存ワークツリーには path 指定を使う
Agent(
  prompt="...",
  isolation="worktree",  # 新規作成時
  # または
  # path="/path/to/existing/worktree"  # 既存ワークツリー切り替え時
)
```

**本プロジェクトでの推奨**:
- image-pipeline と audio-pipeline の並列実行では、2回目以降のサブエージェントに `path` 指定で既存ワークツリーを再利用すると安定性が向上する

### バックグラウンドエージェント（`run_in_background: true`）

サブエージェントをバックグラウンドで実行し、完了時に通知を受け取る。

**活用シナリオ**:
- ファクトチェックをバックグラウンドで実行しながら台本生成を続行
- AI レビュー依頼後、レビュー到着をバックグラウンドで監視
- theme-discovery のニューススキャンをバックグラウンドで実行

**注意**: バックグラウンドエージェントの進捗をポーリングしない。待ち方の正本は `agent-team.md` §F-8「『待つ』の作法」（禁止手段の一覧・例外はそちらが SSOT）。

## v2.1.101〜v2.1.111 の主要新機能（2026-04-11→2026-04-18）

### /recap コマンド（v2.1.108+）

セッション復帰時のコンテキストサマリーを生成する。CC-BUG-17 の緩和策として活用可能。

```
# 手動サマリー生成
/recap

# 設定からの有効化
/config → セッション復帰時サマリーを有効化

# 環境変数での強制有効化（Scheduled Tasks 向け）
CLAUDE_CODE_ENABLE_AWAY_SUMMARY=1
```

**本プロジェクトでの推奨**: 既存の `session-start.sh` 再開プロトコル（git log → check_pending_pr_reviews.py → Issue 一覧確認）と併用する補完的ツールとして扱う。Git/Issue が権威ソースであることは変わらない。

### /ultrareview コマンド（v2.1.111）

並列マルチエージェントで包括的コードレビューを実行する Pro/Max 向け機能。

> **本プロジェクトでの判断（プロジェクト技術監修役レビュー 2026-04-18）**: 現時点では **使用しない**。Gemini Code Assist + GitHub Copilot の2つの外部 AI レビュアーと self-reviewer スキルで品質担保は十分。収益化（YPP Stage 1）達成後に費用対効果を再評価する。

### /team-onboarding コマンド（v2.1.101）

ローカルの Claude Code 使用状況からチームメイト向けオンボードガイドを自動生成する。本プロジェクトでは `CLAUDE.md` が同等の役割を担っているため不要。

### built-in slash command の Skill tool 経由自動発見（v2.1.111）

`/init`, `/review`, `/security-review` 等の組み込みコマンドが Skill tool 経由で自動発見・実行できるようになった。

**注意事項（プロジェクト技術監修役レビュー 2026-04-18）**:
- スキルの自動発見には **description コンテキストバジェット（フォールバック 8,000 文字）** がある
- 29 スキルの description 合計がバジェットを超えると、一部スキルの description が切り詰められて自動発見が失敗する可能性
- **対策**: 重要なスキル（script-pipeline・audio-pipeline 等）の description 冒頭 50 文字に重要キーワードを配置する

```bash
# description バジェット超過チェック（定期実行推奨）
total_chars=$(cat .claude/skills/*/SKILL.md | python3 -c "
import sys, re
content = sys.stdin.read()
descs = re.findall(r'^description:\s*(.+?)$', content, re.MULTILINE)
total = sum(len(d) for d in descs)
print(f'合計: {total}文字 / 制限: 8000文字 → {\"OK\" if total < 8000 else \"超過\"}')" 2>/dev/null)
echo "$total_chars"
```

## v2.1.111〜v2.1.123 の主要新機能（2026-04-18→2026-05-08）

### `/effort` インタラクティブスライダー（v2.1.111）

引数なしで `/effort` を実行すると矢印キー操作のスライダーが表示される。詳細は上記「`/effort` コマンド」セクションを参照。

### Auto mode for Max subscribers（v2.1.111）

Opus 系での Adaptive Thinking 自動制御モード。詳細は上記「Auto mode for Max subscribers」セクションを参照。

### `alwaysLoad` MCP オプション（v2.1.121）

MCP サーバーの常時ロード設定。詳細は上記「`alwaysLoad` MCP オプション」セクションを参照。

### パフォーマンス改善（v2.1.111〜v2.1.123）

| 改善項目 | 効果 |
|---------|------|
| 長時間セッション読み込み | **最大 67% 高速化**（コンテキスト圧縮後の復帰が特に改善） |
| メモリ漏洩 | 大幅削減（長時間パイプラインの安定性向上） |
| OAuth 認証 | 401 ループバグ修正（スケジュールタスクの認証切れ問題が改善） |

> **本プロジェクトへの影響**: 長時間パイプライン（audio/image/video）で PostCompact 後の再起動コストが削減される。OAuth 401 ループ修正により、スケジュールタスクの認証エラーが減少する見込み。

## スケジュールタスクの仕様と制限

### Claude.ai クラウド環境の制限

| 項目 | 制限値 | 備考 |
|------|--------|------|
| スロット数 | hourly 1 + daily 2 = **3 スロット**（Max プラン） | `youtube-scheduling-rules.md` で最適配分済み |
| セッションタイムアウト | **10〜30 分**（タスク内容による） | 長時間パイプラインは daily 枠に分離 |
| 同時実行制御 | **なし**（前セッション完了前に新セッション起動あり） | Issue ラベルによる論理ロックで対処（CP-4） |
| 環境変数 | Claude.ai 設定 + GitHub Variables | `env-vars.md` 参照 |

### セッションタイムアウト対策

| 対策 | 実装状況 | 効果 |
|------|---------|------|
| PostCompact 自動コミット | ✅ 実装済み | 圧縮時の作業保護 |
| Stop フック自動コミット | ✅ 実装済み | セッション終了時の作業保護 |
| Issue コメントへの再開ステップ記録 | ✅ ルール化済み | セッション再開時の継続性 |
| 長時間タスクの daily 枠分離 | ✅ 実装済み | image/video パイプラインの保護 |
| セルフレビュー同期呼び出し禁止 | ✅ ルール化済み | タイムアウト防止 |

### `/schedule` コマンド（プログラマティックタスク管理）

`/schedule` コマンドで Scheduled Tasks をプログラムから管理できる。

```
# タスク作成
/schedule create --name "テーマ探索" --cron "0 8 * * *" --prompt "/theme-discovery"

# タスク一覧
/schedule list

# タスク削除
/schedule delete <task-id>
```

**現在の運用**: Claude.ai Web UI から手動登録。将来的に `/schedule` コマンドでのプログラマティック管理への移行を検討。

## サンドボックスと権限管理

### 現在の設定（最適化済み）

| 設定 | 値 | 目的 |
|------|-----|------|
| `bypassPermissions` | `true` | フック制御下での自動実行 |
| `autoAllowBashIfSandboxed` | `true` | Bash ツールの自動許可 |
| `excludedCommands` | 接続先が動的な一部スクリプト限定（narrow exclusion・2026-08-01 見直し・#379） | secrets-broker 移行ツール等、`allowedDomains` に事前登録できないスクリプトだけのネットワーク制限バイパス |
| `allowedDomains` | 登録ドメイン一覧は `docs/rules/sandbox-rules.md` 参照 | MCP サーバー・外部 API 通信用（tools/ スクリプトの既定の到達経路） |

### 権限の 3 層アーキテクチャ

```
Layer 1: settings.json allow-list（ツールレベルの許可）
  ↓
Layer 2: PreToolUse フック（コンテキスト依存のブロック）
  ↓
Layer 3: PostToolUse フック（出力の品質検証）
```

## MCP サーバー統合

> **2026-04 更新**: Anthropic・OpenAI・Block が共同で **Agentic AI Foundation（AAIF）** を設立し、MCP を Linux Foundation 傘下の指定基金として寄贈（April 2026）。MCP はもはや Anthropic の独自規格ではなく、AWS・Google・Microsoft・Cloudflare 等 8 社が Platinum メンバーとして参加する **オープン標準** となった。本プロジェクトで使用する MCP サーバーは AAIF 準拠の標準実装として引き続き動作するが、今後の仕様変更は Linux Foundation リリースノートで追跡する。

### OS CA 証明書のデフォルト信頼（v2.1.101+）

Enterprise TLS プロキシが追加設定なしで動作するようになった。本プロジェクトの Cloudflare Workers 経由の MCP 接続（gemini-image）やプロキシ経由 HTTPS 接続の安定性が向上している可能性がある。

**L-057・L-058 のワークアラウンドへの影響**（確認推奨）:
- L-058: `YOUTUBE_UPLOAD_PROXY_INSECURE=1` ワークアラウンド → v2.1.101+ では不要になった可能性あり
- L-057: Remotion の Google Fonts SSL エラー → Root.tsx の `delayRender` 削除が本質的解決のため影響なし
- **注意**: 確認なしでのワークアラウンド削除は禁止。実動テスト後に削除判断する

### 現在の MCP サーバー構成

| サーバー | 接続先 | 用途 | ヘルスチェック |
|---------|--------|------|-------------|
| `gemini-image` | Cloudflare Workers | 画像生成 | ✅ `session-start.sh` で疎通確認 |
| `youtube` | ローカル Python | YouTube API | ⬜ 未実装 |
| `github` | GitHub MCP | GitHub API | ✅ 接続時に自動確認 |

### `alwaysLoad` MCP オプション（v2.1.121）

MCP サーバー設定に `alwaysLoad: true` を追加すると、そのサーバーの全ツールが ToolSearch の deferral をスキップして **常時利用可能** になる。

```json
// .claude/settings.json の mcp 設定例
{
  "mcpServers": {
    "github": {
      "command": "...",
      "alwaysLoad": true
    }
  }
}
```

**本プロジェクトへの適用方針**:
- **GitHub MCP** (`mcp__github__*`): `alwaysLoad: true` 推奨。全パイプライン・スキルが GitHub API を頻繁に使用するため、deferral を毎回スキップできると効率的
- **gemini-image MCP** (`mcp__gemini-image__*`): image-pipeline 実行時のみ必要なため `alwaysLoad` は不要
- **youtube MCP** (`mcp__youtube__*`): video-pipeline・upload-pipeline でのみ使用するため `alwaysLoad` は不要

> **注意**: `alwaysLoad: true` を多くのサーバーに設定すると、セッション開始時の初期化コストが増加する。使用頻度の高いサーバーのみに限定する。

### MCP ヘルスチェックの追加（推奨）

`session-start.sh` に MCP サーバーの疎通確認を追加し、起動失敗時に早期検出する。

## パフォーマンス最適化のベストプラクティス

### 並列ツール呼び出しの最大化

- 独立した Read/Grep/Glob は **1 メッセージ内で並列呼び出し** する
- 依存関係のあるツール呼び出しは逐次実行（プレースホルダーを使わない）
- サブエージェントは独立タスクなら **同一メッセージ内で複数起動**

### コンテキスト効率化

| パターン | 効果 |
|---------|------|
| サブエージェント出力の凝縮 | メインコンテキストの 20〜40% 節約 |
| Explore エージェントへの調査委譲 | 調査結果のみを受け取り、ファイル全文を避ける |
| CLAUDE.md の最適化 | 必読ドキュメントのみ記載し、詳細は `.claude/rules/` に分離 |

### Bash コマンドの最適化

- 独立した複数コマンドは **複数の Bash ツール呼び出し** で並列実行
- 依存するコマンドは `&&` で連鎖
- `tools/*.py` は直接 `python3` で呼び出し（シェルラッパーを介さない）
- エージェントの Bash 実行ではシェル演算子（`&&`, `||`, `|`, `;`）でコマンドを結合しない

## 既知の不具合・制限と防御策（GitHub Issues / コミュニティ情報）

> 最終リサーチ: 2026-03-28
> ソース: [anthropics/claude-code Issues](https://github.com/anthropics/claude-code/issues), [status.anthropic.com](https://status.anthropic.com/), コミュニティ報告

### 深刻度: CRITICAL

#### CC-BUG-01: Auto-Compact 無限ループ（macOS）

**Issue**: [#35192](https://github.com/anthropics/claude-code/issues/35192)
**症状**: コンテキストバッファが 200K を超えると無限圧縮ループに陥り、一切の作業ができなくなる。
**影響**: 長時間パイプライン（script-pipeline, image-pipeline）の途中で発生するとセッション全損。
**本プロジェクトの防御策**:
- ✅ PostCompact フックで自動コミット（`post-compact.sh`）
- ✅ Stop フックで自動コミット（`stop-slack-notify.sh`）
- ✅ Issue コメントへの再開ステップ記録（`session-safety-rules.md`）

#### CC-BUG-02: Auto-Compact の早期発動

**Issue**: [#6123](https://github.com/anthropics/claude-code/issues/6123)
**症状**: コンテキスト使用率 8〜12% で Auto-Compact が発動し、数分おきに圧縮が繰り返される。
**影響**: 作業が頻繁に中断される。Haiku サブエージェント（小さいコンテキスト）で特に発生しやすい。
**本プロジェクトの防御策**:
- ✅ Haiku サブエージェントに渡す情報を 1,000〜2,000 トークンに制限（`agent-team.md`）
- **追加推奨**: Haiku サブエージェントが早期圧縮に遭遇した場合、Sonnet にモデルを上げて再実行

#### CC-BUG-03: Stop 後もバックグラウンドでトークン消費が継続

**Issue**: [#14229](https://github.com/anthropics/claude-code/issues/14229)
**症状**: セッション停止を確認した後も、バックグラウンドで「Stream closed」エラーを繰り返しながらトークンを消費し続ける。
**影響**: 予期しないトークン消費。レート制限の早期到達。
**本プロジェクトの防御策**:
- ✅ `bypassPermissions: true` で権限要求ループを回避
- **追加推奨**: セッション終了後にトークン使用量の急増がないか監視。異常を検知したらプロセスを強制終了

### 深刻度: HIGH

#### CC-BUG-04: フック（PreToolUse/PostToolUse）が実行されない

**Issue**: [#6305](https://github.com/anthropics/claude-code/issues/6305), [#3148](https://github.com/anthropics/claude-code/issues/3148), [#10367](https://github.com/anthropics/claude-code/issues/10367)
**症状**:
1. `matcher: "*"` （ワイルドカード）を使うと PreToolUse/PostToolUse フックが発火しない
2. サブディレクトリから Claude Code を実行するとすべてのフックが失敗する
**影響**: 品質ゲート（台本バリデーション、画像生成チェック）がサイレントにスキップされる。
**本プロジェクトの防御策**:
- ✅ `matcher: "Bash"` 等の具体的なツール名を使用（`settings.json` で確認済み）
- ✅ `matcher: ""` （空文字）は使用していない
- **確認事項**: サブディレクトリ実行は本プロジェクトでは発生しない（`session-start.sh` がプロジェクトルートに `cd` する）
- **追加推奨**: フックが発火しなかった場合のフォールバック検出を PostToolUse で実装検討

#### CC-BUG-05: MCP サーバー SSE タイムアウト無視

**Issue**: [#3033](https://github.com/anthropics/claude-code/issues/3033), [#20335](https://github.com/anthropics/claude-code/issues/20335)
**症状**: `settings.json` で設定したタイムアウト値が SSE (Server-Sent Events) 接続の MCP サーバーで無視される。
**影響**: Gemini Image MCP サーバー（Cloudflare Workers）が「オフライン」と誤判定される。
**本プロジェクトの防御策**:
- ✅ `excludedCommands` で `tools/*.py` のネットワーク制限をバイパス済み
- **追加推奨**: `session-start.sh` に MCP サーバー疎通確認を追加（後述）

#### CC-BUG-06: MCP サーバー 5 分アイドル切断

**Issue**: [trigger.dev #2134](https://github.com/triggerdotdev/trigger.dev/issues/2134)
**症状**: MCP サーバーの SSE ストリームが 5 分間のアイドル後に切断される。以降すべての MCP 呼び出しが `Body Timeout Error` で失敗する。
**影響**: 画像パイプライン等で MCP 呼び出しの間隔が 5 分を超えると失敗。
**本プロジェクトの防御策**:
- **画像パイプライン**: 画像生成は連続実行のため通常は 5 分以内に次の呼び出しが発生
- **YouTube MCP**: ローカル Python サーバーのため SSE アイドルの影響を受けにくい
- **追加推奨**: MCP 呼び出し失敗時のリトライロジックをパイプラインスキルに追加

#### CC-BUG-07: Git LFS がクラウド環境で動作しない

**Issue**: [#25043](https://github.com/anthropics/claude-code/issues/25043)
**症状**: クラウド環境のローカルプロキシが LFS Batch API を拒否（「invalid git path」エラー）。
**影響**: LFS 追跡ファイルがポインタのまま残る。
**本プロジェクトの防御策**:
- ✅ `.gitattributes` で `!filter` オーバーライド済み（`audio-pipeline-rules.md`）
- ✅ `fix_lfs_pointers.py` で LFS ポインタを自動復元（`session-start.sh`）
- ✅ 動画ファイルは Cloudflare R2 にバックアップ（`video-storage-rules.md`）
- ✅ `GIT_LFS_SKIP_SMUDGE=1` で checkout 時のブロックを回避

#### CC-BUG-08: セッション再開時のトークン大量消費

**Issue**: [#38029](https://github.com/anthropics/claude-code/issues/38029)
**症状**: 大きなセッションを再開すると、ユーザー入力ゼロでトークン使用率が 80% に急増。45 分以内に 100% 到達。
**影響**: セッション再開後のトークン予算が極端に少なくなる。
**追加情報（2026-03-23 発生事例）**: Max $100 プランユーザーが大規模プロジェクトを再開した際、ユーザー入力ゼロで出力トークン 652,069（コスト $342.74）が生成された。モデルがコンテキスト再構築のために大量の出力トークンを無用に生成する内部ループ的バグ。同日リリースのアップデートで修正パッチが含まれている。
**本プロジェクトの防御策**:
- ✅ 頻繁なコミットでセッション切り替えの影響を最小化
- ✅ セッション再開プロトコル（`session-safety-rules.md`）で軽量な状態確認から開始
- ✅ 大きなセッション（50+ ターン）は再開せず新規セッションで開始する
- ✅ `.claude/rules/` を常時必要な 7 ファイル（~66KB）に厳選（トークン最適化。詳細は `token-optimization-rules.md`）
- ✅ CLAUDE.md を ~470 行に圧縮（Phase 固有の詳細を外部ファイルに移譲）
- **追加推奨**: `ccusage` でセッション再開後のトークン消費を定期監視。異常検知時は retro-try Issue を作成

#### CC-BUG-09: `excludedCommands` がサンドボックスを完全にバイパスしない

**Issue**: [#29274](https://github.com/anthropics/claude-code/issues/29274), [#14162](https://github.com/anthropics/claude-code/issues/14162)
**症状**: `excludedCommands` に登録したコマンドでも DNS ルックアップ等のネットワーク制限が適用される場合がある。
**影響**: `tools/*.py` スクリプトの外部 API 通信が散発的に失敗する可能性。
**本プロジェクトの防御策**:
- ✅ `allowedDomains` で全接続先ドメインを明示的にホワイトリスト登録（多層防御）
- ✅ `sandbox-rules.md` でパターン設計の意図を文書化済み
- **設計原則**: `excludedCommands` だけに頼らず、`allowedDomains` との **両方** で確実にカバーする
- ⚠️ **前提条件（#383）**: 上記はいずれも `sandbox.enabled: true`（2026-08-02 追加）かつ `bwrap` /
  Seatbelt が使えるローカル環境でのみ効く。クラウド実行環境ではサンドボックス自体が動作しないため、
  本項の「防御策あり」をクラウドのリスク評価に数えない（SSOT: `sandbox-rules.md`）

#### CC-BUG-13: サンドボックスの書き込み/読み込み非対称性

**Issue**: [#40321](https://github.com/anthropics/claude-code/issues/40321)
**症状**: ファイル書き込みは実ファイルシステムに反映されるが、読み込みはサンドボックス内で処理される非対称動作。
**影響**: ファイル書き込み後に同じファイルを読み込めない場合がある。
**本プロジェクトの防御策**:
- ✅ `excludedCommands` で `tools/*.py` をサンドボックス外で実行
- **追加推奨**: 重要なファイル書き込み後は `git add` + `git status` で実在を確認する

#### CC-BUG-14: コンテキスト圧縮後の Tool Use API エラー 400

**Issue**: [#40305](https://github.com/anthropics/claude-code/issues/40305)
**症状**: Auto-Compact 後に孤立した `tool_result` ブロックが残り、API エラー 400（Tool Use Concurrency Issues）が発生する。
**影響**: 圧縮後にセッションが使用不能になる。パイプラインの途中で発生すると作業が中断。
**本プロジェクトの防御策**:
- ✅ PostCompact フックで自動コミット（作業データは保護済み）
- ✅ 圧縮は Claude 標準の Auto Compaction に委ね、圧縮後は PostCompact フックの自動コミットとセッション再開プロトコル（`session-safety-rules.md`）で復旧する

#### CC-BUG-15: 圧縮時にサブエージェントチームのコンテキストが失われる

**Issue**: [#23620](https://github.com/anthropics/claude-code/issues/23620)
**症状**: 長時間セッションで Auto-Compact が発動すると、サブエージェントのコンテキストがリードエージェントと非同期になる。
**影響**: Agent Teams（script-team-reviewer, video-reviewer 等）の並列レビューが圧縮後に不整合を起こす可能性。
**本プロジェクトの防御策**:
- ✅ サブエージェントの出力を構造化サマリー（1,000〜2,000 トークン）に凝縮（`agent-team.md`）
- **追加推奨**: サブエージェントバッチは 100K トークン以下に保つ。圧縮後にサブエージェントを再初期化する

#### CC-BUG-16: フック 8+ 個でコンテキスト肥大化

**Issue**: [#36121](https://github.com/anthropics/claude-code/issues/36121)
**症状**: フックが成功しても「Hook Error」がトランスクリプトに出力される。8 個以上のフックがあるとモデルコンテキストが汚染され、ターンが早期終了する。
**根本原因**: フックの stdout を `hookSpecificOutput` JSON ラッパーで囲まずに出力すると、エラーとして扱われる。stdin の未消費により broken pipe エラーも発生。
**影響**: 本プロジェクトは **13 個のフック** を使用（SessionStart ×2, PermissionRequest ×1, PreToolUse ×4, PostToolUse ×1, PostToolUseFailure ×1, PostCompact ×1, Stop ×3）。コンテキスト肥大化のリスクあり。
**本プロジェクトの防御策**:
- ✅ フックスクリプトは stderr にログ出力（stdout を汚染しない）
- **追加推奨**: 全フックスクリプトで stdin を明示的に消費する（`cat > /dev/null 2>&1 || true` をスクリプト冒頭に追加）
- **追加推奨**: PreToolUse の 4 フック（git push, PR 作成, 画像生成, コメント投稿）を **1 つのルーティングスクリプト** に統合してフック数を削減する検討

#### CC-BUG-17: セッション再開で会話履歴がゼロにリセット

**Issue**: [#40319](https://github.com/anthropics/claude-code/issues/40319)
**症状**: セッション再開時に会話履歴がサイレントにドロップされ、コンテキストがゼロの状態から開始される。
**影響**: 前セッションの作業内容が全て失われる。
**本プロジェクトの防御策**:
- ✅ セッション再開プロトコル（`session-safety-rules.md`）で Git ログ + Issue ラベルから状態を復元
- ✅ Issue コメントへの「次回再開ステップ」記録
- ✅ PostCompact / Stop フックでの自動コミット
- ✅（新・v2.1.108+）**`/recap` コマンド + `CLAUDE_CODE_ENABLE_AWAY_SUMMARY`** — セッション復帰時にコンテキストサマリーを自動生成する緩和策として活用可能
  - `/config` で有効化。手動では `/recap` コマンドで即座にサマリー生成
  - `CLAUDE_CODE_ENABLE_AWAY_SUMMARY=1` 環境変数で強制有効化
  - **住み分け**: `/recap` は会話サマリー。**Git コミット + Issue コメント** が権威ソースであることは変わらず、補完的に利用する
- **設計原則**: 会話履歴に依存せず、**Git コミット + Issue コメント** を権威ソースとする

#### CC-BUG-18: Plan Mode でも書き込みツールがブロックされない

**Issue**: [#40324](https://github.com/anthropics/claude-code/issues/40324)
**症状**: Plan Mode で実行しても、モデルがファイル編集を実行できてしまう。
**本プロジェクトの防御策**:
- ✅ `bypassPermissions: true` + PreToolUse フックで危険操作のみをブロックする設計のため、Plan Mode は使用していない
- **注意**: Plan Mode を信頼して読み取り専用を前提にしない

### 深刻度: MEDIUM

#### CC-BUG-10: パーミッション要求の無限ループ

**Issue**: [#11380](https://github.com/anthropics/claude-code/issues/11380)
**症状**: 「常に許可」を選択しても、同じパーミッション要求が繰り返される。
**本プロジェクトの防御策**:
- ✅ `bypassPermissions: true` で完全にバイパス済み
- ✅ PreToolUse フックで危険な操作のみをブロック

#### CC-BUG-11: Read 操作でパーミッション要求（リグレッション）

**Issue**: [#11285](https://github.com/anthropics/claude-code/issues/11285)
**症状**: 読み取り専用操作にパーミッション確認が要求される。
**本プロジェクトの防御策**:
- ✅ `bypassPermissions: true` で影響なし
- ✅ `PermissionRequest` フック（`permission-request-auto-allow.sh`）で `.claude/` 配下を自動許可

#### CC-BUG-12: `/schedule` が一部環境で機能しない

**Issue**: [#29022](https://github.com/anthropics/claude-code/issues/29022), [#36131](https://github.com/anthropics/claude-code/issues/36131)
**症状**: Windows 環境で `/schedule` スキルが失敗。デスクトップアプリがフォーカスされていないとタスクが実行されない。
**本プロジェクトの防御策**:
- ✅ Claude.ai Web クラウド環境を使用しており、デスクトップアプリの問題は回避
- ✅ スケジュールタスクは Web UI から登録（Windows `/schedule` の問題を回避）

### セキュリティ脆弱性（パッチ済み）

| CVE | 深刻度 | 内容 | ステータス |
|-----|--------|------|-----------|
| CVE-2025-59536 | HIGH (CVSS 8.7) | 信頼できないディレクトリでの起動時に任意のシェルコマンド実行 | **パッチ済み** |
| CVE-2026-21852 | MEDIUM (CVSS 5.3) | 悪意のあるリポジトリ読み込みで API キーが流出 | **パッチ済み** |

**対策**: Claude Code を常に最新バージョンに維持する。信頼できないリポジトリで実行しない。

### インフラ障害履歴（参考）

| 日付 | 内容 | 影響 | 復旧状況 |
|------|------|------|---------|
| 2026-03-26〜27 | ネットワークパフォーマンス低下 | Opus/Sonnet のエラー率上昇、MCP 呼び出し失敗 | **復旧済み** |
| 2026-03-26 | レート制限の急速消費バグ | 5 時間枠が 1〜2 時間で枯渇 | **修正済み** |
| 2026-03-26 | ピーク時間帯の消費速度引き上げ（**意図的変更**） | PT 5:00〜11:00（JST 22:00〜翌4:00）のトークン消費レートが増加。約 7% のユーザーに影響 | **仕様変更（恒久）** |
| 2026-03-23 | セッション再開時の出力トークン暴走バグ | 大規模プロジェクトで出力 652K トークン（$342）が消費 | **修正済み** |
| 2026-01-26〜28 | ハーネスバグによる品質低下 | 応答品質の全般的な低下 | **ロールバック済み** |

## 防御設計の方針

### 多層防御（Defense in Depth）

本プロジェクトでは、Claude Code の既知の不具合に対して以下の多層防御を適用する。

```
Layer 0: Claude Code バージョン管理
  → 常に最新バージョンを使用（session-start.sh で gh CLI を自動更新）
  → セキュリティパッチの自動適用

Layer 1: 作業データの保護（最重要）
  → PostCompact フックで自動コミット
  → Stop フックで自動コミット
  → ユーザー確認前のコミット義務（session-safety-rules.md）
  → Issue コメントへの再開ステップ記録

Layer 2: ネットワーク通信の確実性
  → excludedCommands + allowedDomains の両方でカバー（片方に頼らない）
  → MCP サーバーのフォールバック CLI コマンド
  → API 呼び出し失敗時のリトライ（指数バックオフ）

Layer 3: フックの信頼性確保
  → ワイルドカード `*` を matcher に使わない（具体的なツール名を指定）（CC-BUG-04）
  → フックスクリプトは stderr にログ出力（stdout を汚染しない）（CC-BUG-16）
  → 全フックスクリプトで stdin を明示的に消費する（CC-BUG-16）
  → フック総数を監視（13個以上でコンテキスト肥大化リスク）（CC-BUG-16）
  → フック失敗時のフォールバック検出

Layer 4: コンテキスト管理
  → Haiku サブエージェントに渡す情報を厳選（CC-BUG-02 早期圧縮対策）
  → 圧縮は Claude 標準の Auto Compaction に委ね、PostCompact フックの自動コミットで作業を保護（CC-BUG-14）
  → サブエージェントバッチは 100K トークン以下に保つ（CC-BUG-15）
  → 大きなセッション（50+ ターン）は再開せず新規セッションで開始（CC-BUG-17）
  → 会話履歴に依存せず、Git コミット + Issue コメントを権威ソースとする（CC-BUG-17）

Layer 5: セッション競合防止
  → Issue ラベルによる論理ロック（CP-4）
  → PR 作成前の同一動画 ID チェック
  → discover スクリプトの排他チェック（session-concurrency-rules.md）
```

### 不具合発見時の対応フロー

```
Claude Code で不具合に遭遇
  ↓
1. 未コミット変更を即座にコミット & push（最優先）
  ↓
2. Issue コメントに症状と再現手順を記録
  ↓
3. 本ドキュメントの「既知の不具合」セクションと照合
  ↓
  ├─ 既知の不具合 → 記載された防御策を適用
  └─ 未知の不具合 → 以下を実施:
       a. anthropics/claude-code Issues で類似報告を検索
       b. 本ドキュメントに新規エントリを追加
       c. 防御策を設計し、必要に応じてフック・ルールを追加
       d. retro-try Issue を作成して再発防止策を追跡
```

## 定期リサーチ・更新ルール

### 更新トリガー

| トリガー | アクション |
|---------|-----------|
| Claude Code の新バージョンリリース | 本ドキュメントを更新 |
| 新しいモデルのリリース | モデルテーブルを更新、`agent-team.md` に反映 |
| ワークフローで繰り返し問題が発生 | 対策を本ドキュメントに追記 |
| 月次の workflow-health-check | 本ドキュメントの鮮度を確認 |
| **Claude Code の GitHub Issues で重要な報告** | **既知の不具合セクションを更新** |
| **Anthropic ステータスページで障害報告** | **インフラ障害履歴を更新** |

### リサーチ方法

1. Claude Code 公式ドキュメント（`docs.anthropic.com/en/docs/claude-code/`）を Web 検索で確認
2. Claude Code changelog で新機能・破壊的変更を確認
3. Anthropic 公式ブログで新モデル・機能発表を確認
4. **[anthropics/claude-code Issues](https://github.com/anthropics/claude-code/issues) で既知の不具合を確認**
5. **[status.anthropic.com](https://status.anthropic.com/) でインフラ障害を確認**
6. **コミュニティ（Reddit, HackerNews）で再現性の高い問題を収集**
7. 本プロジェクトの retro-try Issue で繰り返しパターンを分析

### 不具合トラッキングの命名規則

本ドキュメント内の不具合エントリには `CC-BUG-{NN}` の連番を付与する。
新規エントリ追加時は最大番号 + 1 とする。

## 最新ツール・機能（2026-04 追加）

> **2026-04-12 追記（Issue #1130 対応）**: Anthropic 2026-04-09 発表の三大機能を反映。

### ant CLI（Anthropic API 向けコマンドラインツール）

| 項目 | 内容 |
|------|------|
| **用途** | Anthropic API のテスト・デバッグ・スクリプティング・自動化 |
| **インストール** | `npm install -g @anthropic-ai/cli` |
| **基本コマンド** | `ant messages create --model claude-sonnet-5 --max-tokens 1024 -p "Hello"` |
| **ストリーミング** | `ant messages stream --model claude-sonnet-5 -p "長い応答"` |
| **ファイル添付** | `ant messages create --file image.png -p "この画像を説明して"` |
| **環境変数** | `ANTHROPIC_API_KEY` を設定して使用 |

**本プロジェクトでの活用場面**:
- `generate_audio.py` や `pipeline_state.py` の API テスト・デバッグに使用
- Claude Code を起動せずに軽量な API 呼び出しをスクリプトから実行する場合
- CI/CD パイプラインでの API 疎通確認

### Claude Cowork GA（マルチユーザーコラボレーション）

> **GA リリース（2026-04）**: 複数ユーザーが同一 Claude Code セッションをリアルタイム共有できる機能。

| 項目 | 内容 |
|------|------|
| **用途** | チームでの同時コーディング・ペアプログラミング・コードレビュー |
| **対応プラン** | Claude Pro / Team / Enterprise |
| **参加方法** | セッション URL を共有するだけ（招待リンク方式） |
| **RBAC 対応** | 閲覧者（read-only）/ 編集者（edit）の権限分離 |
| **OpenTelemetry** | セッションイベントを外部のオブザーバビリティツールに転送可能 |

**本プロジェクトでの活用場面**:
- ユーザーが直接 Claude Code セッションを参照して、台本品質のリアルタイム確認
- `status:waiting-user` Issue の対応をユーザーと共同で実施する場合

### Focus View（Ctrl+O でファイルツリー非表示）

| 項目 | 内容 |
|------|------|
| **ショートカット** | `Ctrl+O`（macOS は `Cmd+O`）でファイルツリーをトグル |
| **用途** | コード編集時の画面領域を最大化・集中モード |
| **対応環境** | Claude Code デスクトップアプリ（Mac/Windows）・VS Code 拡張 |

**本プロジェクトでの活用場面**:
- 台本 JSON / meta.yaml など長いファイルを編集する際に作業領域を拡大

---

## v2.1.143 変更点（2026-05-17 リリース）

### バックグラウンドセッションの改善

| 変更点 | 内容 |
|--------|------|
| **自動リタイア** | 5 分間アイドル後にバックグラウンドセッションが自動リタイア |
| **状態保持** | 復帰時にモデルと effort レベルを保持（別セッションの `/model` 選択を拾わない） |

**本プロジェクトへの影響**: スケジュールタスクが長い処理を実行する際、処理完了後の待機時間が 5 分を超えるとセッションがリタイアする可能性がある。タスク完了後は速やかに次のステップへ進むか、パイプラインの自動化レベルを上げて待機時間を最小化すること。

### MCP サーバー改善

| 変更点 | 内容 |
|--------|------|
| **ページネーション対応** | `tools/list` が全ページを返すよう改善（旧: 最初のページのみ） |
| **MIME タイプ処理** | 非対応 MIME タイプの画像（SVG 等）が会話を破壊しない → ディスク保存 & 参照化 |

**本プロジェクトへの影響**: MCP サーバーにツールが多数ある場合（例: GitHub MCP の 50+ ツール）、全ツールが確実に取得されるようになった。

### ワークツリー設定追加

```json
// .claude/settings.json に追加可能
{
  "worktree": {
    "bgIsolation": "none"  // EnterWorktree なしでバックグラウンドセッションが直接編集
  }
}
```

**用途**: ワークツリーの作成が制限されている環境や、バックグラウンドセッションによる変更を即座に現在の作業ディレクトリに反映させたい場合に有効。ただし、本プロジェクトでは CP-4（マルチセッション共存）の観点から原則として `isolation: "worktree"` によるワークツリー隔離を推奨しているため、このオプションの使用は慎重に判断すること。

### バグ修正（クラウド環境に影響するもの）

| 問題 | 修正内容 |
|------|---------|
| **ローカル設定のオーバーライド問題** | ローカル `settings.local.json` がリモートマネージド設定を上書きしていた問題を修正 → クラウド環境の設定が正しく適用されるように |
| **拡張子と内容不一致ファイル** | `.png` なのに HTML コンテンツのファイルなどをテキストフォールバックで処理 |

---

## v2.1.154〜v2.1.163 変更点（2026-05-28〜06-04・Opus 4.8 同梱）

> 出典: [Claude Code changelog](https://code.claude.com/docs/en/changelog) / [model-config](https://code.claude.com/docs/en/model-config.md) / [fast-mode](https://code.claude.com/docs/en/fast-mode.md) / [headless](https://code.claude.com/docs/en/headless)。リサーチ日: 2026-06-05。

### v2.1.154（2026-05-28）— Opus 4.8 デフォルト化（最重要）

| 変更点 | 内容 | 本プロジェクト適用示唆 |
|--------|------|----------------------|
| **Opus 4.8 デフォルト化** | `/model opus` が 4.7 → 4.8 に解決。**デフォルト effort が `xhigh` → `high` に変化** | 台本生成は `/effort xhigh` を **明示指定**。`agent-team.md` / `agent-team-summary.md` 更新済み |
| **Fast mode デフォルト → Opus 4.8** | `/fast` の対象モデルが 4.7 → 4.8 に。最大 2.5x 高速・料金 2x（$10/$50 per 1M） | スケジュールタスクは cost-sensitive のため **Fast mode 不要**。手動デバッグ・即興修正時のみ |
| **Dynamic Workflows（`/workflows`）** | Claude が数百エージェントにまたがる作業を動的にオーケストレーション。`ultracode` で自動使用 | パイプライン系（script/audio/image/video）の大タスクで活用検討。セッション限定（settings 不可） |
| **`!<command>` バックグラウンドシェル** | シェルコマンドをバックグラウンドセッションとして実行・アタッチ | 長時間 bash 処理（レンダリング等）の非同期化に活用余地 |
| セキュリティ修正 | `rm -rf $HOME` ブロック・サンドボックス write allowlist バグ修正 | — |
| `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE` 廃止 | 2026-06-01 削除済み（Opus 4.6 Fast は廃止） | 該当環境変数を使わない |

### v2.1.152（2026-05-27）

| 変更点 | 内容 | 適用示唆 |
|--------|------|---------|
| **`/code-review --fix`** | 指摘を自動適用（既存 `--comment` に追加） | ⚠️ 本プロジェクトの `/code-review` は自前スキルに置換済みで **これらのフラグは存在しない**（#280）。インライン投稿は常時仕様、修正適用は「`--fix` 相当」としてスキル側に内包（`.claude/skills/code-review/SKILL.md`・#461） |
| スキル `disallowed-tools` frontmatter | スキル実行中のツールを制限可能 | 副作用のあるスキルの安全性向上 |
| `/reload-skills` | スキルディレクトリ再スキャン | — |
| **SessionStart フックで `reloadSkills`/`sessionTitle`** | stdout JSON で設定可能 | **スケジュールタスクが `claude agents` ビューで識別しやすくなる**（例: `sessionTitle: "hourly-10:00-audio-pipeline"`）。`session-start.sh` 拡張候補 |
| **`MessageDisplay` フック**（新規） | アシスタントメッセージテキストを変換可能 | 将来: 出力整形・マスキング |

### v2.1.157（2026-05-29）

| 変更点 | 内容 | 適用示唆 |
|--------|------|---------|
| `.claude/skills/` 自動プラグイン化 | マーケットプレイス不要でプラグイン読み込み | 既存 `.claude/skills/` 構成がそのままプラグインとして機能 |
| `claude plugin init <name>` | 新規プラグインをスキャフォールド | skill-creator フローの補完 |
| `EnterWorktree` セッション中切替 | Claude 管理ワークツリー間を切り替え | 並列パイプラインの安定性向上 |

### v2.1.163（2026-06-04）

| 変更点 | 内容 | 適用示唆 |
|--------|------|---------|
| **Stop/SubagentStop の `additionalContext`** | `hookSpecificOutput.additionalContext` でエラー扱いなしのフィードバック注入 | `stop-router.sh` を拡張してセッション終了時のステータス詳細を注入できる |
| `requiredMinimumVersion`/`requiredMaximumVersion` | managed settings のバージョン範囲強制 | — |
| バックグラウンドセッション再アタッチ修正 | タスクを失わずに更新・再アタッチ | 重要バグ修正 |

---

## `claude -p`（headless / print モード）— クラウド環境での CLI 機能活用

> **本セクションは「CLI 専用機能を `claude -p` 経由でクラウド/スクリプトから活用する」ためのガイド（2026-06-05 実機検証済み）。**
> 出典: [headless docs](https://code.claude.com/docs/en/headless) + 本環境での実コマンド検証（Claude Code v2.1.163）。

### 前提: スケジュールタスクは「インタラクティブセッション」

本プロジェクトのスケジュールタスクは Claude Code の Scheduled Tasks（**インタラクティブセッション**）として動作する。そのため `/effort`・`/fast`・`/workflows` などの **CLI 専用スラッシュコマンドはスケジュールタスク内ではそのまま使える**。`claude -p`（headless）の出番は、**`tools/` の Python スクリプトや CI から Claude を呼ぶ** 場合や、**構造化 JSON 出力でコスト追跡したい** 場合。

> **例外（2026-07-21・#275 → #280 で解消）**: 組み込み `code-review` スキルは `disable-model-invocation: true` が付与されており、
> スケジュールタスク（Claude が Skill ツール経由で自律起動）からは起動できない（`Skill code-review
> cannot be used with Skill tool due to disable-model-invocation` で失敗・v2.1.216 実機確認。公式 changelog では
> v2.1.215 に「Claude no longer runs the `/verify` and `/code-review` skills on its own」と明記・以降撤回なし）。同じ frontmatter を
> 持たない `security-review` 等の組み込みスキルは Skill ツール経由でも起動可（`code-review` 個別の制限）。
> **対策（#280）**: 同名 project スキル `.claude/skills/code-review/`（自前実装・`disable-model-invocation` なし）が
> bundled を置換する公式仕様により、`/code-review` は対話・自律の両方から再び起動可能
> （SSOT: `docs/rules/ai-reviewer-strategy.md`）。

### このクラウド環境で動作確認済みのフラグ（2026-06-05 検証）

| フラグ | 動作 | 備考 |
|--------|------|------|
| `-p` / `--print` | ✅ | ヘッドレス非対話実行（基本） |
| `--model <id>` | ✅ | `claude-haiku-4-5` 等を指定 |
| `--output-format text/json/stream-json` | ✅ | `json` は `total_cost_usd`・所要時間・result を構造化取得。`stream-json` は `--verbose` 必須 |
| `--json-schema '<schema>'` | ✅ | JSON スキーマ準拠の型安全出力（v2.1.163） |
| `--allowedTools '<pattern>'` | ✅ | `Bash(git log *)` 等のパターン制限が有効。副作用の封じ込めに有用 |
| `--permission-mode default` | ✅ | `bypassPermissions` は **root 環境では禁止**（本環境はエラー） |
| `--append-system-prompt` / `--system-prompt` | ✅ | CLAUDE.md への追記 / 完全置換 |
| `--max-turns <n>` | ✅ | ターン数制限（重いスキルのタイムアウト対策） |
| `--effort <level>` | ✅ | `/effort` の headless 代替（`low`/`medium`/`high`/`xhigh`/`max`）。`--effort low` で受理を実機確認（2026-06-05） |
| `--no-session-persistence` | ✅ | セッション保存なし（CI 向け）。ただし CLAUDE.md は読み込まれる |
| `--continue` / `--resume` | ✅ | 会話継続 |
| **`--bare`** | ❌ | **このクラウド環境（OAuth 認証）では動作不可**。`--bare` は `ANTHROPIC_API_KEY` 専用。`claude-code-guide` の「将来 `-p` のデフォルト」情報はあるが、現状クラウドでは使えない（要再検証） |

### `claude -p` から slash command / skill を呼べるか（検証結果）

| コマンド種別 | `claude -p` | 詳細 |
|------------|-------------|------|
| `/status`（軽量スキル） | ✅（~30秒） | GitHub API・Issue 集計・コミット確認を含む状態報告を出力 |
| `/help` | ❌ | `-p` モードで明示的に無効化（`"/help isn't available in this environment."`） |
| `/next`・`/workflow-health-check`（重量スキル） | ⚠️ タイムアウト | 60〜90秒超でタイムアウト。`--max-turns` や処理分割が必要 |
| プロンプトベースのタスク | ✅ | `--allowedTools` でツール制限しつつ読み取りタスクを実行 |
| `/security-review`・`/fast`・`/workflows` | ❌ | インタラクティブ専用。`-p` では不可（スケジュールタスク内では使える） |
| `/code-review` | ❌ | `-p` では不可（組み込み・自前とも headless のスラッシュコマンド実行は不可）。**スケジュールタスク・自律セッションでは自前 `code-review` スキル（`.claude/skills/code-review/`・bundled を置換）を `Skill(code-review)` で起動できる**（#275 → #280） |

### 推奨パターン（クラウド/CI から CLI 機能を呼ぶ）

```bash
# パターン1: 構造化出力での状態チェック（JSON で parse・コスト追跡）
timeout 120 claude -p "Check pending PR reviews and output JSON summary" \
  --model claude-haiku-4-5 \
  --allowedTools "Bash(gh *)" \
  --no-session-persistence \
  --output-format json

# パターン2: 軽量スキル実行（/status 等）
timeout 120 claude -p "/status" --model claude-haiku-4-5 --output-format text

# パターン3: 副作用なしの安全な読み取り（特定ツールのみ許可）
timeout 60 claude -p "Summarize last 5 commits" \
  --model claude-haiku-4-5 \
  --allowedTools "Bash(git log *)" --no-session-persistence --output-format json

# パターン4: JSON スキーマで型安全な出力（v2.1.163）
# ※ gh issue list はローカル実行用の例。クラウドでは 403 のため MCP（list_issues）を許可する（L-114）
timeout 120 claude -p "List open issues as JSON" \
  --model claude-haiku-4-5 \
  --allowedTools "Bash(gh issue list *)" \
  --json-schema '{"type":"object","properties":{"issues":{"type":"array"}}}'
```

### コスト・制約（重要）

- **コスト所感（Haiku 4.5）**: CLAUDE.md ロード込みで **$0.13〜0.20/回**（CLAUDE.md ~100K トークンのキャッシュ生成が初回コストに影響）。2 回目以降はキャッシュリードで大幅減。実行時間は軽量タスクで 6〜20 秒（SessionStart フック実行時間込み）
- **`claude -p` 起動ごとに `session-start.sh` の `git reset/checkout/clean` が走る** → **その cwd の未コミット変更を消す**。編集中のメインセッションと並行して `claude -p` を多用すると CP-4 競合で作業が revert される（本タスク中に実体験）。
  - **回避策（2026-06-05 実機検証済み）**: `claude -p` は **`isolation: "worktree"` のサブエージェント経由で実行する** と、別 worktree（`.claude/worktrees/agent-*`）が cwd になり、`session-start.sh` の git 操作がその worktree に閉じる → **メインセッションの未コミット状態（追跡ファイルの改変・未追跡ファイル）が完全に保持される**。
  - ⚠️ **隔離なしの通常サブエージェント経由では保持されない**（メインと cwd を共有するため revert される）。必ず `isolation: "worktree"` を指定すること。
  - 注意: worktree は **コミット済み状態** からチェックアウトされるため、claude -p サブエージェントはメインの未コミット編集を参照できない（状態確認・JSON クエリ等の独立タスク向け。メインの未コミット変更に作用させたい場合は先にコミットする）。
  - 補強策として、重要な編集は **即コミット** して保護する（worktree 隔離と併用すると二重に安全）。
  - **本質的解決策（将来・#2605）**: `session-start.sh` が headless（`claude -p`）起動を検知して破壊的クリーンアップ（`git reset/checkout/clean`）を自動スキップする改修。実装後は外部スクリプト/CI から **同一 cwd で直接 `claude -p` を呼んでもメイン作業ツリーが破壊されない**（worktree 隔離なしでも安全になる）。
- **2026-06-15 以降の課金変更（要確認）**: `claude -p` / Agent SDK の使用が別枠 **Agent SDK Credit**（Pro $20/月・Max 5x $100/月・Max 20x $200/月、超過は API 従量）から消費される。**スケジュールタスク本体がこの範囲に含まれるかは公式ドキュメントで要確認**（含まれる場合はクレジット内運用 or 従量課金超過への対応が必要）

### CLI 専用機能の利用可否マトリクス（まとめ）

| 機能 | CLI インタラクティブ（=スケジュールタスク） | `claude -p` headless | ブラウザ/web |
|------|:--:|:--:|:--:|
| `/effort` | ✅ | ❌（`--effort` フラグで代替） | ❌（ステータスバー or スキル frontmatter） |
| `/fast` | ✅ | ❌ | ❌ |
| `/workflows`（ultracode） | ✅ | ❌ | ❌ |
| `/security-review` | ✅ | ❌ | ❌ |
| `/code-review`（自前スキル置換済み・`Skill(code-review)` で自律起動可・#280。修正適用 = `--fix` 相当はスキル内 Step 3） | ✅ | ✅ | ❌ |
| カスタムスキル（軽量） | ✅ | ✅（`/status` 等） | ✅ |
| カスタムスキル（重量） | ✅ | ⚠️ タイムアウト | ✅ |
| 構造化 JSON 出力 | — | ✅（`--output-format json`） | — |
| フック全種 | ✅ | ✅ | ✅（Scheduled Tasks） |

> **結論**: 本プロジェクトのスケジュールタスクはインタラクティブ CLI セッションのため CLI 専用機能を直接使える。`claude -p` の主用途は **`tools/` スクリプト・CI からの呼び出し**（構造化出力・コスト追跡・ツール制限つき安全実行）。重いスキルはタイムアウトに注意し `--max-turns` で制御する。

---

## 2026-06-05 リサーチ知識反映（専門チーム検証済み）

> Issue #2609。Claude Code 公式ベストプラクティス・スキル/hooks/監査仕様を専門チーム4役 + claude-code-guide 検証で再調査した結果の記録。**本セクションは知識反映（記録）であり、挙動変更を伴う項目は次節「未適用の改善提案」で未適用として管理する。**

### 検証済み公式機能（記録のみ・既存挙動に影響しない）

| 機能 | 公式記載 | 概要 |
|------|---------|------|
| SKILL.md frontmatter 全フィールド | [skills](https://code.claude.com/docs/en/skills) | `when_to_use` / `disallowed-tools` / `disable-model-invocation` / `user-invocable` / `paths` / `hooks` / `context:fork` / `agent` / `shell` 等が公式サポート。**全フィールドの一覧は左列の公式ドキュメントが正本**（本リポジトリ内に正本ファイルは置かない）。`license`/`metadata` は非対応 |
| description 文字数 | 同上 | `description` + `when_to_use` 合算 **1,536 文字** で切り詰め（旧 1024 は廃止） |
| サブエージェント `memory` フィールド | [sub-agents](https://code.claude.com/docs/en/sub-agents) | `memory: project` 等で `~/.claude/agent-memory/<name>/MEMORY.md`（先頭200行/25KB）を次セッション自動ロード |
| `/fork` コマンド | 同上 | 現会話コンテキストを継承したサブエージェントをバックグラウンド実行（通常サブエージェントは空コンテキスト開始） |
| `/btw <質問>` | [best-practices](https://code.claude.com/docs/en/best-practices) | 会話履歴を汚さないオーバーレイ回答（コンテキスト非消費） |
| `/compact <指示>` | 同上 | 指示付き部分圧縮。CLAUDE.md に「圧縮時に保持する内容」を書くと制御可能 |
| CLAUDE.md `@path` import | 同上 | `@docs/foo.md` で別ファイルをインクルード。肥大化した CLAUDE.md の分割に有効 |
| `security-guidance` プラグイン | [security-guidance](https://code.claude.com/docs/en/security-guidance) | 公式 Anthropic プラグイン。①編集時正規表現 ②ターン終了時 git diff レビュー ③commit/push 時 深層レビューの3層。`enabledPlugins` で有効化・`.claude/security-patterns.yaml` でカスタム可 | <!-- refcheck:ignore -->
| `/security-review` スキル | 同上 | ブランチ全体のオンデマンドセキュリティレビュー（self-reviewer 後段の Lv2 強化候補） |
| OTEL テレメトリ | [observability](https://code.claude.com/docs/en/agent-sdk/observability) | `CLAUDE_CODE_ENABLE_TELEMETRY=1` でコスト・ツール実行を収集（コレクター別途必要） |
| community マーケットプレイス（`claude-community`） | [discover-plugins](https://code.claude.com/docs/en/discover-plugins) | `anthropics/claude-plugins-community` は Anthropic 管理の read-only ミラー。手動追加が必要で、永続化は `extraKnownMarketplaces` + `enabledPlugins`（v2.1.195 以降、project settings の enable だけでは外部ソースは自動インストールされない）。**本ベースへの導入は見送り**（2026-08-25・議論記録は `content/discussions/community-marketplace-eli5-20260825/`） |

### 未適用の改善提案（ユーザー判断待ち・挙動変更を伴うため未適用）

> ⚠️ **以下は「既存ワークフローの挙動に支障が出うる」ためユーザー指示（2026-06-05）により未適用。提案として記録する。各セッションは本節を理由に勝手に適用しないこと。**

> **2026-06-05 実装ステータス（Issue #2615・ユーザー「すべて対応」指示／最終）**: ✅ **適用**= P-2 / P-3 / P-5（技術監修役のみ）/ P-7 プルーニング監査 / P-8。⏸️ **見送り**= P-1（致命的・自律実行停止）/ P-4（既存フック+CI で充足・プラグインはスループット害）。🟡 **部分**= P-6（additionalContext は既存等価・JSON 全面書換は高リスクで見送り）/ P-7 @import 分割（report-only 提案に留め自動適用せず）。判断基準は「正確性＝既存の自律ワークフローを壊さない」を最優先（ユーザーの併存指示）。

| # | 提案 | 期待効果 | ステータス / 挙動変更点 |
|---|------|---------|-------------------------|
| P-1 | 副作用大スキル（sns-organic-pipeline / note-post / youtube-upload-pipeline / express-news-pipeline / shorts-pipeline / tiktok-pipeline）に `disable-model-invocation: true` | description 誤マッチによる意図しない自動投稿/公開を防止 | ❌ **見送り（致命的）**。公式仕様で `disable-model-invocation: true` は「**モデルによる起動を禁止** し人間の手動 `/コマンド` のみ可」。本プロジェクトのスケジュールタスクは **モデルが Skill tool で起動** して自律実行するため、付与すると当該スキルの **自動実行が全停止** する（CP-6 崩壊）。誤自動起動防止は各スキル内部ガード（予算/クールダウン/ledger）で既にカバー済み |
| P-2 | 自律パイプライン（audio/image/video/script/sns-organic/express-news/shorts/tiktok/research-runner）に `disallowed-tools: AskUserQuestion` | CP-6 準拠で「確認待ち停止」を物理的に防止（確認削減） | ✅ **適用済み**。公式仕様で `disallowed-tools` は **スキル起動を阻害せず**、実行中のみ AskUserQuestion をプールから除外し **次メッセージで自動解除**。真の境界（A-1〜A-6・fact-check 致命的 NG）は AskUserQuestion ではなく **STOP+Slack 通知** で扱う設計のため副作用なし。`waiting-user-handler`（確認が本務）は対象外 |
| P-3 | `effort` 未設定スキルへの付与（express-news=high / video-pipeline=medium / shorts=medium / pr-review-watcher=medium） | 思考深度の最適化・コスト制御 | ✅ **適用済み**。思考深度=実行時挙動は変わるがロジックフローは不変。express-news は高ステークス・低頻度のため high |
| P-4 | `security-guidance` プラグイン導入（`main` 直 push・`gh variable` 等をパターン化） | コミット/push 前のセキュリティ自動レビュー（ハーネス Lv2/3 強化） | ⏸️ **見送り（既存で充足・プラグインは非導入）**。P-4 の意図は既に充足: ① `main` 直 push は Lv3 フック `pre-git-push-check.sh` が物理ブロック ② 秘密情報は GitGuardian CI が全 PR でスキャン ③ `.env`/`settings.local.json` は permission deny。フルプラグインは **自律コミット毎にモデルレビューが挟まりスループット/コストを害する** だけで限界利益が小さいため非導入（CP-6 自律運用保護）。新たな脅威パターンが出たら既存フック idiom（Lv3）に追加する方針 |
| P-5 | プロジェクト固有オブザーバー（技術監修役/初心者役）に `memory: project` 付与 | 指摘履歴の蓄積で監修精度向上 | ✅ **技術監修役のみ適用**。`memory: project`（`.claude/agent-memory/<agent>/MEMORY.md` を seed）。`memory` は公式仕様で Read/Write/Edit を自動有効化するため、**初心者役は非技術設定（高度ツール不使用）維持のため除外**。保護対象（frontmatter `tools`・方言禁止）は不変。CLAUDE.md の編集ツール禁止ルールにメモリ自己記録の carve-out を明記 |
| P-6 | SessionStart フックに `additionalContext`/`sessionTitle`/`reloadSkills` 導入 | 再開プロトコル自動化・セッション識別・スキル即反映 | 🟡 **部分対応**。`additionalContext`（待機Issue・状態スナップショット注入）は **現行 session-start.sh のプレーンテキスト stdout で既に等価に機能** している。`sessionTitle`/`reloadSkills` は 497 行の critical フックの **出力契約を JSON へ全面書き換え** る必要があり、誤れば全セッションの文脈注入が壊れる（高リスク・効果は限定的）。「壊さない」優先で **書き換えは見送り**。将来 JSON 化する場合は capture-buffer 方式で慎重に回帰確認すること |
| P-7 | CLAUDE.md の `@import` 分割 + 定期プルーニング（公式「長すぎると半分無視される」基準） | ルール遵守率向上・トークン削減 | 🟡 **部分対応**。**定期プルーニング監査を実装済み**（`workflow-health-check` Step 6・report-only で肥大化/重複/SSOT 化/@import 候補を週次提示）。`@import` 分割そのものは rule-loading 構造・圧縮時保持挙動・`.claude/rules/` symlink 運用に影響するため **自動適用せず**、Step 6 の提案をユーザー/別 Issue 判断に委ねる（CLAUDE.md は現 469 行で肥大化閾値 600 行未満） |
| P-8 | YAML block-scalar の description を `description` + `when_to_use` に分離（content-marketing-review / create-setting-snapshot / marketing-research / marketing-weekly-pdca / retro-try-handler / skill-creator 日本語トリガー追加 等） | 自動発見精度向上・先頭キーワード最適化 | ✅ **適用済み**。6スキルの block-scalar を `description`（機能）+`when_to_use`（トリガー）に分離。同一キーワードを保持するためトリガー挙動はほぼ等価。skill-creator は英語 description に日本語トリガーの `when_to_use` を追加 |

> 適用する場合は **1 提案 = 1 PR** で段階導入し、各 PR で対象スキル/フックの回帰確認（既存パイプライン1サイクル実行）を行うこと。

### Claude Code 最新BP差分 P-9〜P-14（2026-06-06・専門チーム議論済み・#2672）

> 2026-06-06 ユーザー指示で再リサーチ（最新機能・運用BP の2エージェント並列）。P-1〜P-8 と Dynamic Workflows Epic（#2586）に **含まれない新差分**。技術監修役（技術裏取り）+ @owner（破壊リスク予測）で議論し、ユーザーが「全部Issue化して段階導入」を選択。**1提案=1PR** で進行中（追跡: #2672）。

| # | 提案 | 統合判定 | ステータス / 根拠 |
|---|------|---------|------------------|
| P-9 | `ENABLE_PROMPT_CACHING_1H=1`（プロンプトキャッシュ TTL 5分→1h） | ✅ 即採用（最優先） | ✅ **適用**（GitHub Variable 設定済 + env-vars.md 追記）。技術監修役/@owner 両GO・低リスク。回帰: 次パイプライン1サイクルでコスト計測 |
| P-10 | headless `--fallback-model`（`claude -p` のモデル降格） | ✅ 採用 | ✅ **適用**（2026-06-06・#2672）。`run_deep_research_workflow.py`（`--fallback-model claude-sonnet-5`）/ `run_discussion_review.py` / `monitor_x_mentions.py` / `monitor_qiita_comments.py`（各 `--fallback-model claude-haiku-4-5`）に追加。⚠️ settings.json 配列指定は未実装（#8413）・`-p` フラグ限定・overload(529)のみ発火 |
| P-11 | `SubagentStop` additionalContext 自己修正フィードバック | 🔸 縮小採用 | ✅ **適用**（2026-06-06・#2672）。`.claude/hooks/subagent-stop.sh` 新設・`settings.json` に `SubagentStop` エントリ追加。⚠️ `/usage` はインタラクティブ専用・headless不可のため除外（コメントに明記）。正常終了（end_turn・is_error=false）はスキップ・エラー/非正常時のみ additionalContext 注入 |
| P-12 | 秘匿情報の印字側マスク監査 | 🔄 方向転換 | ✅ **適用**（2026-06-06・PR #2700 マージ済み）。`tools/mask_secrets.py` 新設・`setup_github_variables.py` 統合・`env-vars.md` 整備。`mask_value(None)→"****"` の設計確定 |
| P-13 | `requiredMinimumVersion`（2ライセンス版固定） | ⏸️ 見送り | ✅ **検証完了・見送り**（2026-06-06・#2672）。v2.1.163 リリースノート確認: **managed-settings（組織レベル）専用**。プロジェクト `.claude/settings.json` に記載しても無効。Claude.ai スケジュール環境は managed 設定を使えないため本プロジェクトには適用不可。代替: CLAUDE.md + lessons-core で最低バージョン要件を文書化（今後必要になれば） |
| P-14 | `/goal` 品質ゲート自律継続 | ⏸️ 見送り | headless（`claude -p`）と設計思想が噛み合わない（公式が `-p` 推奨）。既存の Stop hook + パイプラインロジックで品質ゲートは完結済み |

## バージョン差分ログ（claude-code-spec-sync 記録先）

> `claude-code-spec-sync` レーン（SSOT: `docs/rules/claude-code-spec-sync.md`）が Claude Code の
> バージョンアップへ対応した際の記録先。破壊的変更の即対応・新機能の採用/見送り判定を
> 1 行ずつ追記する（上の `## vX.Y.Z 変更点` 形式の手動リサーチセクションを本表が引き継ぐ）。

| 日付 | バージョン | 分類 | 対応内容 / 判定 | PR / Issue |
|------|-----------|------|----------------|-----------|
| 2026-07-21 | v2.1.216 | 破壊的変更 | 組み込み `code-review` スキルに `disable-model-invocation: true` が付与され Skill ツール経由の自律起動が不可に。自律セッションの Layer 1 実行手段を `self-reviewer` Step 2（サブエージェント観点別フレッシュ文脈レビュー）に変更（`ai-reviewer-strategy.md`/`pr-review-flow*.md`/`self-reviewer`/`pr-review-watcher` を整合修正） | #275 |
| 2026-07-21 | v2.1.216 | 恒久対策 | 同名 project スキル `.claude/skills/code-review/`（自前実装）で bundled `code-review` を置換（公式オーバーライド仕様）。対話・自律の両セッションから `/code-review` が再び起動可能になり、Layer 1 標準実行手段を自前スキルへ再統一。`modules.yaml` pr-review モジュールに登録し派生リポジトリへも apply-base で伝播 | #280 |
| 2026-07-22 | v2.1.217 | 破壊的変更（影響なし） | サブエージェントが既定でネストしたサブエージェントを起動できなくなった（`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` で深化可能）。横断調査（`code-review`/`self-reviewer`/`discussion-review`/`retrospective`/`self-improvement-loop`/`problem-investigation-protocol.md` 等の並列サブエージェント起動箇所）の結果、本プロジェクトの全ファンアウトはメインセッション（lead）が直接サブエージェントを起動する設計で、サブエージェントがさらにサブエージェントを起動する箇所は皆無と確認。対応不要 | #284 |
| 2026-07-23 | v2.1.218 | 破壊的変更（影響なし・5件一括） | ① 組み込み `/code-review` がバックグラウンドサブエージェント実行に変更（レビュー対象はスタックされたスラッシュコマンド）→ 本プロジェクトは同名 project スキル（`.claude/skills/code-review/`）で bundled を完全に置換済み・レビュー対象は自スキル Step 0 で明示決定するため無関係。② auto mode で危険 rm・バックグラウンド `&`・疑わしい Windows パスの許可ダイアログが廃止されクラシファイア判定に変更 → `.claude/settings.json` は `permissions.defaultMode`（auto 等）を設定しておらず auto mode のダイアログ機構自体を利用していない。`user-prompt-submit-guard.sh` は UserPromptSubmit 時のプロンプト文字列に対する助言専用フック（非ブロッキング・rm は `rm -rf /` 等の一部パターンのみ検出・`&`/Windows パス検出なし）であり、auto-mode 許可ダイアログとは独立した別レイヤーで代替関係にない（本フックが `&`/Windows パスの安全網として機能しているわけではない点に注意）。よって設定変更は不要。③ `/deep-research` が手動起動時のみ開始に変更（自発始動の廃止） → `research-runner` は元々 `Skill` ツール経由の明示呼び出しのみで自発始動に依存していないため無関係。④ auto の plan mode で read-only 証明不能な Bash コマンドの確認プロンプトが廃止されクラシファイア判定に変更 → 本プロジェクトは Plan mode の対話許可ダイアログに依存する設定・ルールを持たないため無関係。⑤ サーバー管理設定の軽微なトグルが settings-approval プロンプトを発火しなくなった → `.claude/settings.json` の env/permissions は影響対象の「軽微なトグル」カテゴリに該当する記述なし。5 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #289 |
| 2026-07-24 | v2.1.219 | 破壊的変更（影響なし） | Fast mode（`/fast`）の対象モデルから Opus 4.7 が完全除外され、Opus 5 / Opus 4.8 のみが対象に変更。`.claude/`（hooks/skills/agents/settings）を横断 Grep した結果、`/fast` を実際に呼び出す・Opus 4.7 との組み合わせを前提とする箇所は皆無（本プロジェクトのスケジュールタスクは cost-sensitive のため Fast mode 自体を使用しない方針・既存記載どおり）。`agent-team.md`/`agent-team-summary.md`/本ファイル内の Opus 4.7 併記は legacy 参考情報であり `/fast` 運用手順ではないため修正不要 | #306 |
| 2026-08-04 | v2.1.221 | 破壊的変更（影響なし・4件一括） | ① `/ultrareview` の共有履歴なしリポジトリに対するエラーメッセージ改善（`git fetch --unshallow` 誤案内の削除等） → 本プロジェクトは `/ultrareview` を Layer 3 任意レビューとして言及するのみで、共有履歴なしリポジトリを前提にした運用・スクリプトは持たない。② Windows 起動時のプロセス作成時刻取得を PowerShell 起動からネイティブ kernel32 呼び出しに変更 → 本プロジェクトはクラウド/Linux 実行前提で Windows 起動経路に依存する記述なし。③ Claude in Chrome が使用済みタブを自動で閉じるよう変更 → 本プロジェクトは Claude in Chrome 拡張の設定・運用手順を持たない。④ 「Permission mode changed while the auto-mode classifier call was queued」通知の重複表示を承認プロンプトから削除（影響領域ヒント: settings） → `auto-mode classifier`/`Permission mode changed` で横断 Grep した結果 `.claude/settings.json` 含め該当記述なし（`permissions.defaultMode` の auto 系設定も未使用・#289 で確認済みの状態を維持）。4 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #408 |
| 2026-08-04 | v2.1.221 | 新機能・検証（5件） | ① VSCode Focus view トグル → 見送り（適用対象外。VSCode 拡張の UI 機能で、本プロジェクトは CLI/クラウド運用のみのため無関係）。② サンドボックス資格情報ファイルの `mode: "mask"`（Linux/WSL 限定・macOS は `deny` フォールバック） → 見送り（時期尚早。具体的な必要性が無く、クラウドではサンドボックス自体が動作しない＝効果はローカル/下流のみ。credential への部分アクセスが必要になったら再検討）。③ `claude plugin validate` のマーケットプレイス/プラグイン名却下警告 → 見送り。本リポジトリは `marketplace.json` を持たず、`.claude-plugin/plugin.json` の名前は `claude-code-base`（英数字+ハイフン）で単純。実機で `claude plugin validate .` を実行し該当警告なしを確認済み（無関係の CLAUDE.md 警告のみ検出）。④ `claude-api` スキル（bundled）への `prompt-audit` サブコマンド追加 → **採用**。`skill-audit` SKILL.md の「陳腐化」観点に、旧世代モデル向け表現の検出手段として参照を追記（低コスト・L-114 型の陳腐化検出を補強）。⑤ `/fork` のセッションが独自 worktree で動作するよう変更 → 見送り（本プロジェクトは `/fork` を運用フローで使用していない） | #409 |
| 2026-08-05 | v2.1.222 | 破壊的変更（影響なし・2件一括） | ① Remote Control の自動起動を repo ローカル設定（`.claude/settings.json`/settings.local.json）ではオンにできなくなり（オフのみ可）、有効化はユーザースコープの `/config` 限定に変更 → 「Remote Control」「remoteControl」「autostart」「auto-start」等で横断 Grep した結果、本プロジェクトの `.claude/settings.json`・settings.local.json（gitignore 済みローカル専用ファイル）・`docs/rules/*.md`・`tools/*.py` に Remote Control 自動起動を設定する記述は皆無（そもそも repo ローカル設定でオンにする運用を持たない）。② ultraplan 機能の削除 → 「ultraplan」で横断 Grep した結果、CLAUDE.md・`docs/rules/*.md`・`.claude/skills/*/SKILL.md`・`tools/*.py` のいずれにも参照なし（本プロジェクトは `/ultrareview`（Layer 3 任意レビュー）のみ言及し ultraplan とは無関係）。2 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #422 |
| 2026-08-06 | v2.1.223 | 新機能・検証（3件） | ① `strictKnownMarketplaces`/`blockedMarketplaces` の owner wildcard（`"owner/*"`） → 見送り（managed settings 専用機能で project settings には元々配置不可。本リポジトリはマーケットプレイス制限自体を未使用）。② サブエージェント要求モデルが制限され親モデルで実行される場合の警告 → 見送り（組織の `availableModels` allowlist が無いと発火しない。本プロジェクトはモデル指定をエイリアス既定とし組織側モデル制限は未使用）。③ クラウドセッションでの `/teleport` ヒント → 見送り（対話的ローカル CLI 向け UX 機能で、R-1 等の無人自動化ルーティンには無関係）。3 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #432 |
| 2026-08-07 | v2.1.224 | 破壊的変更（1件影響あり・2件影響なし） | ① **200-subagent-per-session spawn cap の撤廃**（"long-running sessions no longer refuse new agents"）→ **影響あり・修正**。`agent-team.md` §F-9 の上限表と `agent-team-summary.md` の要約が「セッション合計 200」を既定値として記載していたため、撤廃を反映（同時実行数 20・ネスト深度 3 は継続。`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` の存続有無は公式 Docs に明記なく未確認と明記）。② managed settings の承認プロンプトが再ログイン/組織切替後に組織設定不変なら再表示されなくなる → 見送り（managed settings は本リポジトリのクラウド運用では使用不可・#2672 で確認済みの前提を維持。関連記述なし）。③ feedback-survey transcript share が同意ベースでモデル設定（システムプロンプト・ツール定義・パラメータ）も送信するよう変更 → 見送り（ユーザー同意が前提のオプトイン機能で、本プロジェクトの rules/skills/hooks に feedback-survey 関連の記述・運用なし） | #439 |
| 2026-08-07 | v2.1.224 | 新機能・検証（7件） | ① `claude self-hosted-runner`（自前マシン/コンテナを Team/Enterprise 向けセッション実行先にする） → 見送り（本プロジェクトは Anthropic 管理クラウド運用のみで、自前インフラへ切り替える必要性なし）。② `archive` プラグインソース（zip over HTTPS からの git/npm 不要インストール） → 見送り（本プロジェクトはプラグイン機構を未使用。`.claude/skills/` 中心の資産構成のまま）。③ ペースト変更取消時の cancel-and-confirm ステップ追加 → 見送り（対話 UI 向けの軽微な UX 改善で、無人自動化ルーティンとは無関係）。④ `ANTHROPIC_BEDROCK_REGION_PREFIX` 環境変数 → 見送り（Bedrock 未使用。Anthropic API を直接利用）。⑤ `crossSessionInbound`/`dialogExpiry` 設定（権限バイパスセッション向けクロスセッションメッセージの承認保留・有効期限） → 見送り（時期尚早。本プロジェクトはクロスセッションメッセージング運用を持たず、CP-4 の並行実行制御は Issue ラベルによる論理ロックで完結している。導入するなら `session-concurrency-rules.md` への影響評価が要るため、実際の運用ニーズが生じたら再検討）。⑥ sandbox 資格情報マスキング拡張（構造化値の `extract`/`onExtractNoMatch`、JWT 対応 `decode:"jwt"`/`maskClaims`、AWS `awsPairs`/`sigv4`。`network.tlsTerminate` 必須） → 見送り（#409 の `mode:"mask"` 見送りと同一理由。クラウド実行はサンドボックス自体が動作せず効果はローカル/下流限定。必要になったら再検討）。⑦ クロスセッション `SendMessage` + `ListAgents`（同一マシン上の他セッションを発見しメッセージ送信・macOS/Linux） → 見送り（時期尚早。ツール自体はセッションに到達済みだが、R-1 は毎回新規セッション・エフェメラル VM で動作するため他ルーティンセッションを実際に発見できるかは未実証。CP-4 の競合防止を Issue ラベルからクロスセッションメッセージングへ置き換えるのはアーキテクチャ影響が大きく、実証と議論型レビューを要するため見送り）。7 件とも見送り・本プロジェクトの内部資産（rules/skills/hooks/settings）に修正なし | #440 |
| 2026-08-08 | v2.1.225 | 破壊的変更（影響なし・2件） | ① `CLAUDE_CODE_OAUTH_TOKEN` を短命トークンへ置換する一時的 401 で headless セッションが壊れる不具合の修正 → 「CLAUDE_CODE_OAUTH_TOKEN」で横断 Grep した結果、本プロジェクトがこのトークンを設定・参照するコード/フック/スキルは皆無（headless (`claude -p`) 呼び出しはセッション内で直接 OAuth トークンを扱わない運用）。② Remote Control セッション再開時、大きな会話が圧縮された後に履歴が壊れる不具合の修正 → 「Remote Control」で横断 Grep した結果、本プロジェクトは Remote Control の自動起動設定・運用手順を持たない（#422 で既に repo ローカル設定でオンにする運用を持たないことを確認済み）。2 件とも Anthropic 側のバグ修正（不具合解消）であり「これまでのやり方がエラーになる」変更ではないため対応不要 | #454 |
| 2026-09-01 | v2.1.257 | 破壊的変更（影響なし・3件） | ① auto mode に Containment Escape rule 追加（クラウドメタデータ資格情報取得・egress 回避・クロステナントアクセスが「想定内」と明示しない限り自動承認されなくなる） → 「Ctrl+E」「metadata-credential」「cross-tenant」「Containment Escape」「egress」（`regress` 等の部分一致を除く厳密一致）で横断 Grep した結果、本プロジェクトの `.claude/settings.json`（`permissions.allow`・`sandbox.network.allowedDomains`）にクラウドメタデータ取得・egress 回避・クロステナントアクセスを許可する記述は元々皆無（`allowedDomains` は github.com/slack.com/api.anthropic.com 等の限定リストのみ）。`permissions.defaultMode` も未設定で auto mode の許可ダイアログ機構自体を利用していない（#289 で確認済みの前提を維持）ため、新ルールが従来の自動承認を覆すケース自体が存在しない。② Bash/PowerShell 権限プロンプトの Ctrl+E コマンド説明削除 → UI 表示テキストのみの変更で、本プロジェクトに Ctrl+E への言及・依存記述なし。③ VSCode スクリーンリーダー読み上げ修正 → VSCode 拡張限定の修正で、本プロジェクトは CLI/クラウド運用のみのため無関係。3 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要。⚠️ **本行は不完全**（`breaking_keywords` 一致行のみの確認だった）→ 次行（2026-09-03）で見落とし 4 件を訂正 | #523 |
| 2026-09-03 | v2.1.257 | 訂正（横断確認漏れ 4件・#548） | PR #547（L-127）のセルフレビューで、上記 2026-09-01 行の「3件で横断確認済み」が `breaking_keywords` 一致行のみの確認であり CHANGELOG 全変更の確認ではなかったと判明。見落とし: ④ `permissions.ask` が複合コマンド・サブシェル内でスキップされる不具合の修正 → 影響あり（防御強化）。deny/ask の静的評価の抜け穴が塞がれた。`docs/rules/lessons/permissions.md` L-127 に既述。⑤ Bash `Read()`/`Edit()` deny ルールが `< file` リダイレクト・`tac`/`egrep` に適用されていなかった不具合の修正 → 影響あり（防御強化）。本プロジェクトの `permissions.deny`（`Read(.env)` 等）の抜け穴が塞がれた。L-127 に既述。⑥ `defaultMode: "bypassPermissions"` がプロジェクト/ローカル設定（`.claude/settings.json`/`settings.local.json`）で無視されるよう変更 → 影響なし。「defaultMode」で確認した結果、本プロジェクトの `.claude/settings.json` は defaultMode キー自体を持たない（`settings.local.json` は `.gitignore` 対象のローカル専用ファイルで追随対象外）。⑦ auto mode に作業ディレクトリ外初回ファイル読み取り前の一度きりプロンプト追加（`permissions.blockReadsOutsideWorkingDirectories`） → 見送り（L-127 に既述の通り本ベース未採用）。**原因の切り分け**: ④⑤ は「Fixed」始まりの行で `claude-code-spec-sync.md` §2 のデモートポリシー（明示的 breaking 記述がない限り other へ）によりツール起票対象外というポリシー通りの挙動（PR #547 の作業で個別に発見され L-127 へ既に記録済みだったため実害はなかった）。⑥ は「Changed `defaultMode: …` to be ignored」という言い回しが `breaking_keywords`（`config/claude_code_spec_sync.yaml`）のどの語にも一致しない **真の分類漏れ**（唯一の実バグ）。⑦ は「Added」行で feature 側に正しく分類され得るが、L-127 で既に「本ベースは未採用」と判断済みだったため独立 Issue 化はされていない。**再発防止**: `breaking_keywords` に `"to be ignored"` を追加（CHANGELOG 全文照合で既存語との新規衝突なしを確認済み）。あわせて `claude-code-spec-sync.md` §2 に「『横断確認済み』はツールが breaking 判定した行の確認を指し、CHANGELOG 全行の精読を意味しない」旨を明記し、今後の記録が同じ誤読を招かないようにした | #548 |
| 2026-09-04 | v2.1.260 | 破壊的変更（影響なし・5件） | ① 1M コンテキストモデル（Opus/Fable）の auto-compact 改善（1M 上限直前で圧縮・巨大コンテキストの復旧圧縮が 10 分でタイムアウトしなくなった） → 見送り（挙動改善のみで既存ルールが前提とする制約なし。「10分」「auto-compact」で `session-compression-rules*.md` を確認したが本変更に依存する記述なし）。② Fable 5.1 で `/effort` をセッション途中で変更してもプロンプトキャッシュが無効化されなくなった → 見送り（純粋な改善で、本プロジェクトは effort 変更がキャッシュを無効化する前提の警告・回避策を持たない。`agent-team.md` の effort 表に影響なし）。③ managed CLAUDE.md（`claudeMd`）がセキュリティ承認ダイアログを発火しなくなった（hooks/shell-command/sandbox/unsafe env は引き続き承認要） → 見送り（managed settings は本リポジトリのクラウド運用では使用不可の前提を維持・#2672/#439 で確認済み）。④ サブエージェントが起動するバックグラウンドコマンドの 1 時間制限が撤廃されメインセッションと同様になった → 見送り（「1 時間」「background command」で横断確認した結果、本プロジェクトにこの制限を前提とした記述・回避策なし）。⑤ VSCode の応答再試行時の描画不具合修正 → 見送り（VSCode 拡張限定・CLI/クラウド運用のみのため無関係）。5 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #555 |
| 2026-09-04 | v2.1.260 | 訂正（revert 行の取りこぼし・L-127 訂正） | 上記 2026-09-04 行の「5 件」に **"Reverted the 2.1.259 change applying `Read()` deny rules to Bash arguments"** が含まれていなかった（`breaking_keywords` / `feature_keywords` のいずれにも一致せず「その他」へ落ちて起票前に消えていた）。この revert は v2.1.259 で下流に多発した「auto モードでも `grep -r` / `cd`+`grep` が承認プロンプトになる」事象の解消そのもので、PR #547 の対策（`--exclude` 自動付与）は v2.1.259 に効かず v2.1.260 では不要と実測で確定 → フック撤去・L-127 訂正・`tools/probe_permission_prompts.sh` 新設・`breaking_keywords` に `revert` 追加・本 Step 0 に「新バージョン検知時は分類に関わらずプローブ実行」を追加。議論記録 `content/discussions/auto-mode-prompt-root-cause-20260904/` | #558 |
| 2026-09-04 | v2.1.260 | 新機能・検証（9件・全件見送り） | ① フルスクリーンモードの diff パネル（`/diff`） → 見送り（対象外。対話 UI 限定機能で、本プロジェクトはクラウド/無人セッション運用が主体）。② `/cost`・ステータスラインへの prompt-cache miss 原因表示 → 見送り（実利用機会なし。R-1 等の無人ルーティンは `/cost` を呼び出さない運用で、`token-optimization-rules.md` のキャッシュ TTL 記述にも影響なし）。③ `/reload-plugins` の headless 対応 → 見送り（対象外。本プロジェクトはプラグイン機構〈`.claude-plugin/`〉を未使用・#440 で確認済みの前提を維持）。④ `/advisor` テキスト形式の headless 対応 → 見送り（時期尚早。`/advisor` 自体が experimental 機能で、本プロジェクトは既に `problem-investigation-protocol.md`〈5 ステップ調査〉・`discussion-review`〈議論型レビュー〉・`code-review`/`self-reviewer`〈Layer 1 セルフレビュー〉で複眼検証を構造化済み。stable 化・具体的な適用場面が出たら再検討）。⑤ `oidc.scope_on_refresh`〈Claude apps gateway〉 → 見送り（対象外。Enterprise/Team 管理者向け gateway 設定で、本リポジトリは organization gateway を未運用）。⑥ Claude apps gateway の Desktop policy 新キー（`userPluginMarketplacesEnabled` 等） → 見送り（対象外。同上の理由で organization gateway 未運用）。⑦〜⑨ VSCode 拡張の footer pill effort 表示・session list フィルタ・welcome screen 不具合修正 → 見送り（対象外。VSCode 拡張限定の UI で、本プロジェクトは CLI/クラウド運用のみ）。9 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #556 |
| 2026-09-05 | v2.1.261 | 破壊的変更（影響なし・6件） | ① Vertex AI 起動時、`GOOGLE_APPLICATION_CREDENTIALS` 設定時の Google Cloud プロジェクト検出・`gcloud` 再起動を省略する高速化 → 見送り（「Vertex」「GOOGLE_APPLICATION_CREDENTIALS」で横断確認した結果、`sandbox-rules.md`/`dynamic-workflows-rules.md` の言及は起動時認証情報再検出とは無関係）。② ストリーミング描画時、既描画済みブロックの再チェック省略による高速化 → 見送り（クライアント内部最適化のみ）。③ auto mode でパブリック図表レンダラー URL へのコンテンツ埋め込みアップロードが要求時のみ自動承認に変更 → 見送り（excalidraw/mermaid.live/draw.io/diagrams.net/plantuml で横断確認した結果、該当ツール・スキルなし）。④ プロンプトの単語編集キーが Bash 準拠に変更（Ctrl+W 等）・`keybindingFlavor` 無効化 → 見送り（該当記述なし）。⑤⑥ VSCode の MCP サーバーダイアログ改善・セッション一覧ホロー表示 → 見送り（VSCode 拡張限定・CLI/クラウド運用のみのため無関係）。6 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要。権限プローブ（`probe_permission_prompts.sh`）も OK（挙動退行なし） | #572 |
| 2026-09-05 | v2.1.261 | 新機能・検証（12件・全件見送り） | ① `/status`・`claude doctor` への Organization policy 未読込理由の表示追加 → 見送り（managed settings/organization gateway 未運用の前提を維持・#439/#2672 で確認済み）。② `bashOutputMaxChars`/`taskOutputMaxChars` 設定（インライン出力上限を最大 128K 文字まで拡張） → 見送り（`token-optimization-rules.md` に既存の出力上限で困っている記述はなく、具体的な必要性が生じていない。将来大出力のファイル保存切替が問題化したら再検討）。③ `--append-subagent-system-prompt-file`（サブエージェントシステムプロンプトをファイルから読み込み） → 見送り（`claude -p` 呼び出し箇所〈`tools/run_deep_research*.py`/`run_discussion_review.py`/`native_fallback.py`/`discussion_review_trigger.py`/`skill-creator` 配下〉を横断確認したが `--append-system-prompt` 系フラグの使用・長大サブエージェントプロンプトのコマンドライン受け渡し運用はなし）。④ `/skill-doctor`（未使用スキルとそのコンテキストコスト表示） → 見送り（本プロジェクトは同等の陳腐化・肥大化検出を `skill-audit` スキルで既に運用中。ビルトインコマンドとしての追加であり rules/skills 側の変更は不要）。⑤ managed settings gateway 403 エラーメッセージの改善 → 見送り（同上・managed settings 未運用）。⑥〜⑫ VSCode 拡張の walkthrough・fold button・archive session・group/badge/usage meter 不具合修正（7件） → 見送り（対象外。VSCode 拡張限定の UI 機能で、本プロジェクトは CLI/クラウド運用のみ）。12 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #573 |
| 2026-09-12 | v2.1.269 | 破壊的変更（影響なし・7件） | ①〜③ 日本語/中国語/韓国語プロンプト候補フィルタ改善・長時間セッションでの transcript 更新高速化・Artifact データベース読み取り〈scratchpad 保存時〉の working-folder 承認省略 → 見送り（クライアント内部最適化、または本プロジェクトが未使用の Artifact db 読み取り機能）。④〜⑥ VSCode 拡張のセッションリネーム保持・右クリックメニュー整理 → 見送り（VSCode 拡張限定・CLI/クラウド運用のみのため無関係）。⑦ Claude Tag の失敗通知が Slack レート制限で欠落する不具合の修正（リトライ化） → 見送り（本プロジェクトは Claude Tag／Slack スレッド運用を持たず `slack_notify.py` は Webhook 経由の一方向通知のみ）。権限プローブ（`probe_permission_prompts.sh`）は OK（挙動退行なし）。⚠️ 要精読 38 件（`others_hinted`）も横断確認: `!` 否定権限ルール・`Bash(tee:*)` allow ルール・`CLAUDE_CODE_BG_TASKS_REPORT_RUNNING`・attribution reminder 関連の記述は `.claude/settings.json`/`docs/rules/*.md`/hooks のいずれにも該当なし（`tee` はテスト用パイプ検知パターンとしてのみ出現）。7 件+38 件とも本プロジェクトの内部資産（rules/skills/hooks/settings）に修正不要 | #639 |
| （spec-sync レーンが対応時に追記する） | — | — | — | — |

## 禁止事項

- Claude Code のバージョンアップ情報を無視してワークフローを旧仕様のまま運用しない
- 新モデルリリース時にモデル選択ルール（`agent-team.md`）を更新せずに放置しない
- 本ドキュメントを 3 ヶ月以上更新しない（陳腐化リスク）
- **既知の不具合（CC-BUG）に対する防御策なしでパイプラインを運用しない**
- **フックの matcher にワイルドカード `*` を使用しない**（CC-BUG-04）
- **`excludedCommands` のみでネットワーク通信を確保しない**（必ず `allowedDomains` も併用）（CC-BUG-09）
