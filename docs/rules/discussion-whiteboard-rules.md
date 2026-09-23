# 議論ホワイトボード運用ルール（Blackboard パターン）

> 専門チームに **役割分担型 fan-out ではなく敵対的相互レビュー（議論）** をさせ、各エージェントが
> 共有ドキュメントに自由記載して議論の整理 + 履歴を git 管理するための共通ルール。
>
> **本ルールは「タスク依存」**（全セッション常駐＝ESSENTIAL_RULES には入れない）。議論型レビューを
> 実装・実行・レビューする時にだけ Read する。各スキルの SKILL.md に「作業前に本ルールを Read」を記載する。
>
> 関連: `docs/rules/dynamic-workflows-rules.md`（WF主導の土台）・`docs/rules/agent-team.md`（並列プリミティブ）。

---

## 1. 背景：なぜ「ホワイトボード」か

多くのプロジェクトの専門チームレビューは **役割分担型 fan-out**（各エージェントが独立評価 →
メインが集計）で、エージェント同士が **互いの意見を読んで反論・検証する「議論」をしていない**。

Anthropic 自身のマルチエージェント研究システムは **artifact パターン**（サブエージェントが
共有ファイルシステムに findings を書き、相互に読み合う）+ **Blackboard（黒板）パターン** で
協調しており、これは「逐次調査のアンカリング（最初の仮説に引っ張られる）」を避けて
**相互に否定し合った末に残った結論の方が真の根本原因に近い** という利点がある（公式 deep-research の
adversarial 検証と同根）。

> 出典: [How we built our multi-agent research system（Anthropic）](https://www.anthropic.com/engineering/multi-agent-research-system) 、
> [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams)（competing hypotheses 例の "Update the findings doc"）。

本ルールはこの findings doc を **git 管理の Markdown ホワイトボード** として実装し、議論の整理 + 履歴を
プロジェクトで永続管理する。

---

## 2. 実装（同時書き込み破損の構造的排除）

`tools/discussion_whiteboard.py` が基盤。**各エージェントは個別のユニークファイルに投稿（post）し、
orchestrator だけが集約（render）する** ことで、同一ファイルへの同時書き込み破損
（[claude-code Issue #29217](https://github.com/anthropics/claude-code/issues/29217)）を構造的に防ぐ。

```
content/discussions/<id>/
  meta.json          … 議題・参加者・論点
  entries/           … 1 post = 1 ユニークファイル（並列 post でも衝突しない・atomic write）
    r01_<ns>_<pid>_<rand>_<author>_<kind>.md
  whiteboard.md      … render が集約する人間可読ビュー（git 履歴 = 議論履歴）
```

### コマンド

| コマンド | 誰が | 用途 |
|---------|------|------|
| `init <id> --topic --participants --brief` | orchestrator | 議題作成（冪等） |
| `post <id> --author <name> --round <n> --kind <種別> --body/--body-file` | 各エージェント | 投稿（**並列安全**） |
| `render <id>` | orchestrator のみ | entries を whiteboard.md に集約（**単一書き手**） |
| `list <id> [--round N] [--json]` | 任意 | 投稿一覧 |
| `show <id>` | 任意 | whiteboard.md 表示（他者の意見を読む） |

### 投稿種別（kind）

`claim`（主張）/ `evidence`（根拠）/ `rebuttal`（反論）/ `question`（問い）/ `concession`（譲歩）/
`consensus`（合意）/ `verdict`（判定）/ `note`（メモ）。

### 厳守事項

- **whiteboard.md を直接編集しない**（必ず `post` 経由）。render が上書きするため直接編集は失われる。
- **render は orchestrator（lead）だけが呼ぶ**（複数書き手にしない）。
- 各エージェントは投稿前に `show` で他者の意見を読み、**相手の具体的な指摘に対して反論/譲歩する**
  （独立感想の垂れ流しは「議論」ではない）。

---

## 3. 議論の進行（標準フロー）

**`discussion-review` スキル（ネイティブ Agent Teams・既定）** がメインセッション主導でこれを駆動する
（旧経路 `tools/run_discussion_review.py` = claude -p 駆動はフォールバック・§4）。

```
init（議題作成）
  → ラウンド1: 各専門家が自分のレンズで claim/evidence を post（並列・独立）
  → render
  → ラウンド2: 各専門家が他者の投稿を読み rebuttal を post（敵対的相互検証）
  → render
  → 合意: lead が対立点/合意点を整理し consensus + verdict を post
  → render（締め）
  → whiteboard.md を git コミット（= 議論履歴の永続化）
```

- ラウンド数は議題の難度に応じて増やしてよい（既定 2）。
- **critical は「議論を経ても残った真の問題」のみ** に絞る（相互検証で否定された指摘は除外）。これが
  fan-out 版に対する品質的優位（過剰指摘・誤検知の削減）。

---

## 4. 実行基盤（ネイティブ Agent Teams が既定・実機検証済み 2026-07-11）

### 4.1 既定: ネイティブ経路（`discussion-review` スキル）

- メインセッションが lead 進行役を兼ね、参加者を **name 付き background Agent** で起動する
  （チーム作成手続き不要・1 セッションに単一の暗黙チーム）。`SendMessage` は deferred ツールとして
  露出しており、ToolSearch でロードして **完了済み参加者の名前宛て再開**（ラウンド進行の合図）に使う。
  手順詳細は `.claude/skills/discussion-review/SKILL.md`。
- **配達制約（V-5・実測）**: SendMessage の配達は受信側の「次のツールラウンド」のみ。受信側が
  ターン終了済みだと **サブエージェント発のメッセージは消失する**（送信結果は success を返すため
  送達保証と混同しない）。よって議論の永続化は **ホワイトボード（artifact）に寄せ**、SendMessage は
  進行合図に限定する。参加者同士の直接往復は同時稼働中のみ。
- 共有タスクボード（TaskCreate 等）はサブエージェント側から利用不可（V-6）。設計に使わない。
- **参加者は lead（メインセッション）だけが起動する**（公式制約・#367）。teammate は teammate を
  spawn できず（nested team 不可）、in-process teammate からの background サブエージェント起動は
  **エラー** になる。多段委譲を設計に入れない。
- **参加者には CLAUDE.md は届くが output style は届かない**（#367 F-2/F-3）。日本語で議論させたいなら
  投稿プロンプトに明記する。`Explore` / `Plan` を参加者に使う場合は CLAUDE.md すら読まないため、
  議論のルール（投稿種別・反論義務）を毎回プロンプトに再掲する。

### 4.2 フォールバック: claude -p 経路（`run_discussion_review.py`）

ネイティブ経路が成立しない場合（Agent/SendMessage 不可等）のみ使う（サイレント退避禁止・理由をログ）:

- Bash から `claude -p` サブプロセスを起動し、子セッションの lead に Workflow/Agent ツールで
  ラウンド進行させる（`run_deep_research_workflow.py` と同経路）。
- ⚠️ **`claude -p` は cwd=リポジトリで起動しない**。子セッションの SessionStart フック
  （`git clean -fd` / `git checkout`）が未コミットの作業を破壊するため（L-100 と同根）。
  `run_discussion_review.py` は **cwd=一時ディレクトリ** で起動し、リポジトリへは **絶対パス** で読み書きさせる。

---

## 4.5 コスト最適化（model mix）

議論型レビューは lead + 参加者分のトークンを消費し、**大型入力（長尺ドキュメント等）× 複数ラウンドで
コストが嵩む**。以下の最適化で **検知精度を落とさずコストを削減** できる。

| レバー | 方針 | 効果・根拠 |
|--------|------|-----------|
| **model mix** | **機械的判定** のレンズ（文字数・表記ゆれ・数字変換・URL有無等）は `haiku`、**判断重視** のレンズ（構成・演出・専門家監修・lead 合成）は `sonnet` | haiku 化しても検知精度は低下しにくい。fan-out 設計の model 割当と一致 |
| **round2 再読抑制** | 各レンズに「round2 は対象を再読せず round1 の自分の分析とホワイトボード（`show`）のみで反論する」と明記 | 大型入力の再読トークンを削減 |
| **ラウンド数** | 既定 2（独立→反論）。軽微な対象は `--rounds 1` も可だが、反論ラウンドが過剰指摘削減の核なので原則 2 を維持 | — |

**設計原則**: スペックの各 participant に `model` を指定し、**そのレンズが「機械的検証」か「判断・創造」か**
で haiku/sonnet を割り当てる。lead（synthesizer）は統合判断のため sonnet を維持する。

---

## 5. ガードレール

1. **フォールバック連鎖**: ネイティブ議論型が失敗 → claude -p 経路（§4.2）→ それも失敗
   （EXIT≠0・verdict 抽出不可）したら **既存の fan-out レビュー経路へ退避** する
   （`dynamic-workflows-rules.md` §5-1 と同方針）。各段ともサイレントフォールバック禁止（ログに残す）。
2. **予算**: ネイティブ経路はメインセッションの課金に一本化される（参加者の最終出力を 1 行サマリーに
   限定してコンテキスト消費を抑える）。claude -p フォールバック時はサブスク経路（既定・追加 $ ゼロ）を
   使い、API 従量経路時は `--max-budget-usd` で上限を付ける（`DISCUSSION_USE_SUBSCRIPTION=0` で従量モード）。
3. **監修ゲート**: プロジェクト定義のキャラクター/専門家オブザーバーを参加者に含める場合、設定逸脱・技術誤りの
   指摘は **自動ゲート扱い** にする（CLAUDE.md サブエージェントルール準拠）。
4. **議題 ID 規約**: プロジェクトの命名規約に沿った一意な ID（例: `<対象ID>-<用途>` や `<スキル>-<日付>`）。
   git 管理されるため使い捨て議論は作らない（残す価値のある議論のみ）。
5. **状態記述の同期（L-094 desync 防止）**: スペック（participants/lens）を変えたら、対応する
   SKILL.md の記述も同一 PR で更新する。

---

## 6. スキルからの使い方

各スキルは「議論スペック JSON」を用意し、**`discussion-review` スキル（ネイティブ・既定）** に
spec・targets・rounds を渡して実行する（手順は `.claude/skills/discussion-review/SKILL.md`）。

フォールバック（ネイティブ不成立時のみ・理由をログ）:

```bash
python3 tools/run_discussion_review.py \
  --id <議題ID> \
  --spec <スキル>/discussion_review_spec.json \
  --targets "<対象パス,...>" --rounds 2
```

スペック形式は両経路で共通。`tools/run_discussion_review.py` の docstring を参照（最小例のスペック JSON は
開発リポジトリ側に置いている。下流では各スキルが持つ議論スペック JSON を `--spec` に指定する）。

---

## 7. 参照

| ドキュメント | 関係 |
|------------|------|
| `.claude/skills/discussion-review/SKILL.md` | ネイティブ議論型の実行手順（既定経路） |
| `tools/discussion_whiteboard.py` | ホワイトボード基盤（init/post/render/list/show・--self-test） |
| `tools/run_discussion_review.py` | claude -p 駆動オーケストレーター（フォールバック経路） |
| `docs/rules/dynamic-workflows-rules.md` | WF主導の土台・ガードレール |
| `docs/rules/agent-team.md` | 役割分担型 fan-out（議論型と使い分ける既存並列プリミティブ） |
