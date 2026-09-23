# レトロスペクティブルール

各ワークフロー（パイプライン）実行後に Agent Teams でレトロスペクティブを実施し、
Try アイテムを GitHub Issue 化するルール。

## 概要

| 項目 | 内容 |
|------|------|
| 実行タイミング | 各パイプラインの最終ステップ（自動）または手動 `/retrospective` |
| フレームワーク | KPT（Keep / Problem / Try） |
| Agent Teams 構成 | 3 役割を並列サブエージェントで担当 |
| Try の記録 | 候補台帳に記録し、資格判定を満たしたものだけ GitHub Issue 化（PULL 型・#662） |
| フィルタ用ラベル | `type:retro-try` で一覧取得可能 |

## 対象ワークフロー

| パイプライン | 呼び出しタイミング |
|-------------|-----------------|
| `script-pipeline` | 台本 PR マージ後（Step 9 の末尾） |
| `audio-pipeline` | 音声 PR マージ後（完了報告の末尾） |
| `image-pipeline` | 画像 PR マージ後（完了報告の末尾） |
| `video-pipeline` | 動画 PR マージ後（完了報告の末尾） |

手動実行: 「レトロスペクティブして」または `/retrospective` で任意タイミングに実行可能。

## Agent Teams 役割定義

3 つの専門レビュアーを **並列サブエージェント** として起動する。

### 役割 1: コンテンツ品質レビュアー

```
担当範囲: コンテンツ品質・キャラクター一貫性・ファクトチェック精度
評価観点:
  - fact_check_flags の発生件数・解消率
  - キャラクター設定（方言・感情・アクション）の遵守状況
  - 台本の尺目標達成状況（VOICEVOX 実測）
  - セルフレビュー・チームレビューで検出された問題パターン
  - AIレビュアーからの指摘の傾向と頻度
```

### 役割 2: プロセス・自動化レビュアー

```
担当範囲: ワークフロー効率・ボトルネック・自動化の有効性
評価観点:
  - 各ステップの所要時間・スキップ可能なステップの特定
  - 手動介入が必要だった箇所（ユーザー確認待ちの頻度）
  - パイプライン中断・再実行の発生有無とその原因
  - Issue / PR / Slack の連携が適切に機能したか
  - 次回の同ワークフローで省略・簡略化できる処理の提案
```

### 役割 3: 技術・ツールレビュアー

```
担当範囲: ツール・スクリプト・ドキュメントの整合性
評価観点:
  - エラーパターン・リトライ発生状況
  - SKILL.md / CLAUDE.md に記載されていない新ルールの発見
  - ドキュメント（docs/rules/*.md）と実装の乖離
  - 追加・修正すべきバリデーション・フック・品質ゲート
  - セルフレビュー学習ログ（self-review-learnings.md）への追記候補
```

## KPT 出力フォーマット

各サブエージェントは以下の JSON 形式で結果を返す。

```json
{
  "role": "quality | process | technical",
  "pipeline": "script | audio | image | video",
  "video_id": "V001",
  "keep": [
    { "title": "うまくいった点の要約", "detail": "具体的な説明" }
  ],
  "problem": [
    { "title": "問題点の要約", "detail": "具体的な問題の説明と影響" }
  ],
  "try": [
    {
      "title": "改善施策の要約（Issue タイトルに使用）",
      "detail": "具体的な改善案・実装方法・期待効果",
      "priority": "high | medium | low",
      "assignee": "claude | user",
      "estimated_effort": "small | medium | large"
    }
  ]
}
```

## Try アイテムの Issue 化ルール

### Issue タイトル命名規則

```
[Retro][{pipeline}] {Try内容の要約}
```

例:
- `[Retro][script] fact_check_flags の自動解消率をセルフレビューで検出する`
- `[Retro][audio] 発音辞書カバレッジチェックを音声生成前に必ず実行する`
- `[Retro][image] サムネイル評価スコアを PR 説明文に自動掲載する`
- `[Retro][video] レンダリング前の BGM トラック検証ステップを追加する`

### Issue ラベル

| ラベル | 付与条件 |
|--------|---------|
| `type:retro-try` | 全 Try Issue に必須（**フィルタ用の主キー**） |
| `type:improvement` | 全 Try Issue に付与 |
| `assignee:claude` | `assignee: "claude"` の場合 |
| `assignee:user` | `assignee: "user"` の場合 |
| `priority:high` | `priority: "high"` の場合 |
| `priority:medium` | `priority: "medium"` の場合 |
| `priority:low` | `priority: "low"` の場合 |
| `status:waiting-claude` | `assignee: "claude"` の場合 |
| `status:waiting-user` | `assignee: "user"` の場合 |

### Issue 本文テンプレート

```markdown
## 背景

**ワークフロー**: {pipeline}（{video_id}）
**レトロスペクティブ日**: {date}
**担当レビュアー**: {role}

## 問題・課題

{problem の detail（対応する Problem があれば引用）}

## 改善施策

{try の detail}

## 期待効果

- {具体的な改善内容}
- 再発防止・品質向上への貢献

## 関連情報

- Pipeline PR: {pr_url}（あれば）
- 推定工数: {estimated_effort}
- 参考ルールファイル: {docs/rules/ の関連ファイル}

---
*このIssueはレトロスペクティブスキルにより自動生成されました*
```

### Issue フィルタリング方法

Try Issue を一覧で取得する。クラウドは MCP 一次経路（L-114）:
`mcp__github__list_issues(owner, repo, labels=["type:retro-try"], state="OPEN")`
（複数ラベル AND・タイトル検索は応答を client-side でフィルタする・`github-mcp-fallback-patterns.md` §2.1）。
以下の gh コマンドはローカル実行用:

```bash
# 全 Try Issue を取得
gh issue list -R kai-kou/tsukuru-studio --label "type:retro-try" --state open

# Claude 担当の Try のみ
gh issue list -R kai-kou/tsukuru-studio --label "type:retro-try" --label "assignee:claude" --state open

# 特定パイプラインの Try のみ（タイトル検索）
gh issue list -R kai-kou/tsukuru-studio --label "type:retro-try" --search "[Retro][script]"

# 高優先度の Try のみ
gh issue list -R kai-kou/tsukuru-studio --label "type:retro-try" --label "priority:high" --state open
```

## レトロスペクティブ実行フロー

### Step 0: コンテキスト収集

実行直前のパイプライン情報を収集する:

1. **git log** で直近のコミット一覧を取得（`git log --oneline -20`）
2. **PR 情報**: マージ済みの PR タイトル・URL・コミット数
3. **実行サマリー**: パイプライン種別、動画 ID、所要ステップ数、発生エラー件数
4. **品質メトリクス**: fact_check_flags 件数、セルフレビュー Error/Warning 件数、AIレビュー指摘件数

### Step 1: Agent Teams 起動（並列）

3 つのサブエージェントを **同時に** 起動する。各エージェントに以下を渡す:

```
コンテキスト情報（Step 0 で収集した情報）
+ 担当役割（quality / process / technical）
+ 評価観点リスト（本ドキュメントの「役割定義」参照）
+ KPT 出力フォーマット（本ドキュメント参照）
```

モデル選択: `model="haiku"`（チェック・評価系タスク）

### Step 2: KPT 結果のマージ

3 つのサブエージェントの結果を統合する:

1. Keep を役割別にまとめる
2. Problem を役割別にまとめる
3. **Try を全役割から収集** し、重複・類似アイテムを統合する

### Try アイテムの記録（台帳既定・Issue 化は資格判定のみ）

Try の既定は Issue ではなく **候補台帳への記録**（PULL 型・WIP 制御は本ファイル「WIP 制御」節・#563 → #662）。手順は次の PULL 型に従う:

1. **台帳読み**: 台帳 Issue（`[Retro][ledger]` 接頭辞・`type:retro-try` ラベル）の本文（集約表）を 1 回読む。台帳が無ければ初回移行を実行する
2. **キー算出と突合**: 各 Try のキー（定義は `retrospective` reference.md F の 0.）を算出し、既存オープン Issue に類似があれば再発コメントを追記（空き枠を消費しない）、なければ台帳の集約表と突合して観測回数を更新する
3. **資格判定**: 「WIP 制御」節の資格判定式（`urgency:blocker` 即時 ∨ 同一キー観測窓内 2 回以上 ∧ 空き枠あり）を満たすかどうかで分岐する
   - 満たす → TTL クローズ済み Issue の reopen / 新規作成で Issue 化し、空き枠を 1 減らす
   - 満たさない → Issue化せず **台帳に記録するのみ**（記録が残る限り「見送り」ではなく「記録あり」として扱う）
4. **台帳記録**: 今回処理した全 Try のキー・観測回数・昇格結果を台帳へ書き戻す（コメント追記 + 本文更新）

### Step 4: Slack 通知

```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pipeline \
  --pipeline "レトロスペクティブ（{pipeline}）" \
  --video-id "{video_id}" \
  --result "完了（Keep {K}件 / Problem {P}件 / Try {T}件→Issue化）" \
  --duration "{所要時間}"
```

### Step 5: 完了報告

以下の形式でレポートを出力する:

```
## レトロスペクティブ完了報告

### ワークフロー
- パイプライン: {pipeline}
- 動画 ID: {video_id}
- 実施日: {date}

### KPT サマリー

#### ✅ Keep（うまくいったこと）
{Keep の一覧}

#### ⚠️ Problem（問題・改善が必要なこと）
{Problem の一覧}

#### 🚀 Try（次回への改善施策）→ Issue 化済み
{Try の一覧（Issue #N へのリンク付き）}

#### 📒 台帳記録のみ（Issue 化せず・キーと観測回数）
{資格判定を満たさなかった Try のキー・観測回数（n/2）}

### 次のアクション
- Try Issue の対応: クラウドは `mcp__github__list_issues(labels=["type:retro-try"], state="OPEN")`（L-114）/ ローカルは `gh issue list -R kai-kou/tsukuru-studio --label "type:retro-try" --state open`
```

## WIP 制御（発生・在庫・出口の数値 SSOT・#563）

> **このセクションが振り返りレーンの WIP 制御に関する数値の唯一の定義**。各 SKILL.md はここを参照し、数値を再定義しない。
> 背景: 「振り返り = Try を全件 Issue 化する」設計は WIP 制限のない PUSH 型で、到着率 λ がパイプライン回数に比例して
> 無制限に伸びる一方、消化側は上限つきのため在庫 L = λW（リトルの法則・Little 1961）が単調増加する。
> 消化上限の引き上げだけでは λ が上回る限り解決しない。設計議論は `content/discussions/retro-issue-overflow-20260904/`。
>
> **#563 後も滞留は続いた**（追補・#662・議論 `content/discussions/retro-issue-stagnation-20260914/`）: 起票上限後も λ_in がパイプライン回数に比例する一方 μ_out は上限つきのため、在庫は WIP 上限に張り付き「常時オープン・TTL 日数で放置後に `not_planned`」が定常状態になった。#563 は発散を止めたが **水準（30 件・30 日）を滞留として固定** しており、TTL クローズは「実装されない見送り」を Little の法則上の退出として偽装していた。今回、Try の既定を Issue から **候補台帳への記録** に変え、Issue 化は下表の資格判定を満たすときだけに限定する PUSH → PULL 転換を行った。

| 制御点 | 値 | 実装先 | 根拠 |
|--------|-----|--------|------|
| **資格判定**（Try を Issue 化してよい条件） | `urgency:blocker` 即時 ∨（同一キーが観測窓内に 2 回以上 ∧ オープン `type:retro-try`〔`urgency:blocker` と台帳を除く〕< WIP 上限） | `retrospective` Step 3-1 | Kanban（Anderson『Kanban』2010）はプル地点で WIP を強制する・スクラムガイド 2020「最もインパクトの大きい改善だけをスプリントバックログへ」 |
| **観測窓**（同一キーの再発カウント） | **30 日**（前回 `last_seen` からこれを超えて再観測されたら `count` を 1 にリセット。`count ≥ 2` は窓内の連続観測を意味する） | `retrospective` reference.md F | 同上 |
| **WIP 上限**（オープン `type:retro-try`〔`urgency:blocker` を除く〕の累積） | `μ_base × W_target` = 2 件/日（`retro-try-handler` 処理上限表の最低区分）× 3 日 = **6 件**。式で定義し、`retro-try-handler` の処理上限が変わったら本値も追随する | `retrospective` Step 3-0 | リトルの法則 L = λW（Little 1961）。μ は据え置き |
| **TTL 出口**（昇格済み Issue の未着手自動クローズ） | **30 日** 更新なし。1 回の実行で最大 **5 件**（`not_planned`） | `retro-try-handler` Step 1.5 | 昇格済み在庫のバックストップ出口（現行維持） |
| **再発 reopen 窓** | **90 日**（昇格時にのみ closed_list を参照して reopen 判定） | `retrospective` reference.md F | 誤クローズの回復コストを下げる |
| **台帳保持**（候補台帳・本文の集約表） | 直近観測から **90 日** を超えたキーは本文から削除する | `retrospective` reference.md K | 本文をトークン有界に保つ |
| **台帳ローテーション** | コメント数が **300 件** に達したら新台帳 Issue を作成し本文を引き継ぐ | `retrospective` reference.md K | `list_issues` 応答の `comments` で追加 API なしに判定できる |

> 旧「起票上限 3 件/レトロ」は廃止した（理由: 空き枠判定〔WIP 上限との差分〕が上限として機能するため冗長）。

上限値を変えるときは **本表を先に更新** し、各 SKILL.md / reference.md が出典明記のうえ再掲している値を **同一 PR で更新する**
（再掲箇所は `grep -rn "WIP 制御" .claude/skills docs/rules` で列挙できる。参照なしの独立した数値定義は作らない）。
下流プロジェクトが値を変える場合も同様で、`project-mission.md` に上書き値を書く場合はここを参照させる。

## 禁止事項

- Try を Issue 化も候補台帳への記録もせずに「次回気をつける」で済ませない（資格判定を満たさない Try は台帳記録のみで足り、それは記録ありとみなす）
- 資格判定を満たす Try を Issue 化・reopen しないでいる（TTL クローズ済み Issue と同種の Try は新規作成ではなく reopen で扱う）
- 資格判定を満たさない Try を Issue 化する・WIP 上限以上の状態で非 `urgency:blocker` の Try を Issue 化する（在庫の発散を防ぐ WIP 制御の無効化）
- KPT を 3 役割の並列実行ではなく逐次実行する（並列化必須）
- `type:retro-try` ラベルなしで Try Issue を作成する（フィルタリングが機能しなくなる）
