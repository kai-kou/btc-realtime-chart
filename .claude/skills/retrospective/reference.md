# retrospective スキル — 詳細テンプレート集（reference）

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


> SKILL.md の各 Step が参照する詳細プロンプト・コマンド・出力テンプレートをまとめた補助ドキュメント。
> SKILL.md を汎用 KPT 手順中心に保つため、長文テンプレートは本ファイルに分離している（プログレッシブ・ディスクロージャ）。
> 必要な Step を実行する直前に該当セクションだけを Read する。
>
> 🔴 クラウド実行環境では repo スコープの `gh`（REST + GraphQL）が egress プロキシに 403 でブロックされる
> （L-114）。以下の GitHub 操作は GitHub MCP を一次経路とし、gh CLI 版は **ローカル環境向けの代替** として併記する。
> `list_issues` の複数ラベル指定は **OR**（gh の `--label A --label B` は AND）のため、MCP では単一ラベルで
> 取得し client-side で残りの条件を絞り込む（詳細: SSOT `docs/rules/github-mcp-fallback-patterns.md` §2.1）。

---

## A. Step 1: サブエージェント共通プロンプト構造

3 役割のサブエージェントに共通で渡すプロンプトの骨格。

```
あなたは {プロジェクト名} のワークフローの{role}です。
直近の {pipeline} パイプライン実行（対象: {entity_id}）を振り返り、
KPT（Keep/Problem/Try）を以下の形式で出力してください。

【実行コンテキスト】
{Step 0 で収集した情報}

【あなたの評価観点】
{役割別の評価観点リスト（下記 B〜D）}

【出力フォーマット（JSON）】
{KPT 出力フォーマット（下記 E）}
```

### B. 役割 1: 成果物品質レビュアー（model: haiku）

```
担当範囲: 成果物品質・キャラ/トーン一貫性・ドメイン固有の検証精度

評価観点:
- ドメイン固有の検証フラグの発生件数・解消率は適切だったか
- キャラ/トーン属性（プロジェクト定義・例: 口調・感情・表現）は適切だったか
- 成果物の目標（プロジェクト定義・例: 尺・分量）は達成されたか
- セルフレビュー・チームレビューで検出されたパターンはあったか
- AIレビュアー（Copilot 等）からの指摘傾向は何か
- プロジェクト定義のルール・設定シート（例: 制作ルール / キャラ設定）の遵守状況はどうか
```

### C. 役割 2: プロセス・自動化レビュアー（model: haiku）

```
担当範囲: ワークフロー効率・ボトルネック・自動化の有効性

評価観点:
- 各ステップで手動介入は必要だったか（ユーザー確認待ちの頻度）
- パイプライン中断・再実行は発生したか（原因は何か）
- スキップ可能・簡略化できるステップはあったか
- Issue / PR / Slack の連携は適切に機能したか
- 次回の同ワークフローでより効率化できる処理はあるか
- セッションタイムアウトリスクへの対応は十分だったか
```

### D. 役割 3: 技術・ツールレビュアー（model: haiku）

```
担当範囲: ツール・スクリプト・ドキュメントの整合性

評価観点:
- エラーパターン・リトライ発生状況と根本原因
- SKILL.md / CLAUDE.md に記載されていない新ルールが発見されたか
- ドキュメント（docs/rules/*.md）と実装の乖離はあるか
- 追加・修正すべきバリデーション・フック・品質ゲートはあるか
- self-review-checklist.md への追記候補はあるか
- 新たなエラーパターン（P-XX）として登録すべき指摘があるか
```

### Step 1.5: プロジェクト定義のレビュー役スポット監査（Lv1・ノンブロッキング）

> **実行条件**: 成果物の「本人らしさ」チェックの価値が高い特定パイプライン（プロジェクト定義）の完了後のみ実行する。その他パイプラインはスキップ。

3 役割エージェントと **同時に並列起動** し、本人視点の一言評価を収集する。レビュー役・禁止事項・参照成果物はプロジェクトで定義する。

**レビュー役A（Lv1・Sonnet・例: 技術正確性レビュー役）**:

```
参加レベル: Lv1

# 絶対禁止（プロジェクト定義のキャラ属性ガード）
- プロジェクトで禁止された方言・口調の混入禁止
- 「AIとして」等の自己開示禁止

# タスク（50文字以内で一言）
プロジェクト定義の成果物（例: content/.../{entity_id}_*.json）を Read して、
「今回の成果物に専門観点での深みが出ていたか」を一言で。
```

**レビュー役B（Lv1・Haiku・例: 初心者目線チェック役）**:

```
参加レベル: Lv1

# 絶対禁止（プロジェクト定義のキャラ属性ガード）
- プロジェクトで禁止された方言・口調の混入禁止
- 「AIとして」等の自己開示禁止

# タスク（50文字以内で一言）
プロジェクト定義の成果物（例: content/.../{entity_id}_*.json）を Read して、
「利用者として最後まで価値を感じられそうだったか」を一言で。
```

2人の一言コメントは Step 5（完了報告）の末尾に「本人コメント」として追記するのみ。KPT 判定には影響しない。

---

## E. KPT 出力フォーマット（全役割共通）

各サブエージェントは以下の JSON 形式で出力する。`{KPT 出力フォーマット}` の実体として使用する。

```json
{
  "keep": [
    {"title": "Keep アイテムのタイトル", "detail": "詳細説明"}
  ],
  "problem": [
    {"title": "Problem アイテムのタイトル", "detail": "詳細説明", "severity": "high|medium|low"}
  ],
  "try": [
    {
      "title": "Try アイテムのタイトル",
      "detail": "具体的な改善施策の説明",
      "assignee": "claude",
      "priority": "high",
      "estimated_effort": "small",
      "urgency": "quality",
      "done_type": "A-doc"
    }
  ]
}
```

**urgency フィールドの定義**:

| 値 | 意味 | 例 |
|----|------|----|
| `blocker` | パイプラインが止まる・データが壊れる致命的問題 | SSL エラー、タイムアウト、ファイル上書き |
| `quality` | 品質に影響するが即座には止まらない問題 | 成果物品質の低下、整合ズレ（プロジェクト定義） |
| `process` | 効率・自動化改善（品質には直接影響しない） | ソート順改善、ドキュメント構造整理 |
| `doc-only` | 説明・コメント・ルール文書のみの更新 | SKILL.md のわかりにくい表現を修正 |

**done_type フィールドの定義**:

| 値 | 意味 | 対応カテゴリ |
|----|------|------------|
| `A-doc` | ドキュメント更新で完結（SKILL.md / docs/rules/*.md / CLAUDE.md） | doc / skill |
| `B-script` | スクリプト実装が必要（`tools/*.py` / `tools/*.sh`） | script |
| `C-validate` | フック/バリデーター追加が必要（`post-tool-use-validate.sh` 等） | validate |
| `D-plan` | 実装計画のみ（large / 依存関係あり・今すぐ実装不可） | large issue |

---

## F. Step 3-A: 既存 Issue との重複チェック（OPEN → CLOSED/not_planned の 2 段）

Issue 作成前に、`type:retro-try` ラベルの既存 Issue と突合し、類似する Issue がないか確認する。
検索は **Step 3-0 で取得済みの `open_list`**、および資格判定を満たした Try についてのみ取得する `closed_list` に対して行う。

### 0. キー算出と台帳突合

各 Try についてまず次のキーを算出し、台帳本文の集約表（reference.md K）と突合して観測回数を更新する:

```
key = {pipeline}|{target}
```

- `target` はタイトル + detail から抽出した対象トークン。次を優先して機械抽出する: ファイル名
  （`[A-Za-z_][A-Za-z0-9_./-]*\.(py|sh|md|json|yaml)`）/ ツール名 / 品質指標・フィールド名
- 抽出できない場合は、正規化した問題対象の名詞句（**5 語以下・小文字・助詞と動詞を除く**）を `target` とする
  （空欄キー同士の誤一致を防ぐ）
- 照合は **キーの完全一致**。表現ゆれの意味的同一性判定はキーに含めない（過小カウント側に倒し、
  `workflow-health-check` 5-b の名寄せがバックストップになる）
- **観測窓の判定は `last_seen` との差分で行う**（集約表は個々の観測日時を持たないため）: 今回の観測日 − 前回の
  `last_seen` が観測窓（数値は `retrospective-rules.md`「WIP 制御」）を **超えていれば `count` を 1 にリセット** して
  記録し、超えていなければ `count += 1` する。これにより `count ≥ 2` は「観測窓内で連続した再観測」を意味し、
  離れた 2 回の観測（例: 85 日ぶり）は資格判定を満たさない

`closed_list` の取得（下記）は、上記の突合で **資格判定を満たした Try が最初に出た時点で 1 回だけ** 行い、同一レトロ内の以後の Try で使い回す（毎回は取得しない。1 件も満たさなければ取得しない）。

MCP（クラウド・一次経路）:
```
open_list   = mcp__github__list_issues(owner, repo, state="OPEN",   labels=["type:retro-try"])   # Step 3-0 で取得済み
closed_list = mcp__github__list_issues(owner, repo, state="CLOSED", labels=["type:retro-try"], since={90 日前の ISO 8601 UTC})
              # 応答に state_reason は含まれない（fields は number/title/body/state/user/labels/assignees/comments/
              # created_at/updated_at/field_values のみ）。ここでは理由で絞らず、類似ヒット時に個別確認する（下記 2）
```

判定順（open_list との突合）:
1. `open_list` に類似あり → G（既存 Issue へコメント追記）
2. 1 で一致なし、かつ `closed_list` に類似あり → `mcp__github__issue_read(method="get", issue_number={N})` で `state_reason` を確認する
   （類似ヒット時のみの追加 1 呼び出し）:
   - `not_planned`（TTL クローズ済み・retro-try-handler Step 1.5 由来、または reference.md K の移行吸収）→ **reopen**:
     `mcp__github__issue_write(method="update", issue_number={N}, state="open")`（**`labels` は渡さない**。渡さなければ全置換の
     対象にならず `status:waiting-claude` を含む元のラベルが保持される）。続けて G の再発検知テンプレートを投稿し、
     末尾に「TTL クローズ（not_planned）から再発により reopen」の 1 行を添える
   - `completed`（実装済み）→ 退行として扱い、3 へ進む（新規作成。本文に「#N で実装済みの内容が再発」と記載）
3. どちらにも一致なし → H（新規作成）

90 日窓（`since`）は closed Issue の全件検索コストを避けるための境界（数値の SSOT は `docs/rules/retrospective-rules.md`「WIP 制御」）。

ローカル環境（gh CLI 到達可能時）の代替:
```bash
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" \
  --state open \
  --json number,title,body \
  --limit 1000
```

### 類似判定の基準

以下のいずれかに該当する場合、**類似 Issue あり** と判定する:

| 判定条件 | 例 |
|---------|-----|
| タイトルに **同じツール名・ファイル名** が含まれる | プロジェクト定義のツール・スクリプト名（例: `generate_*.py`） |
| タイトルに **同じ品質指標・フィールド名** が含まれる | プロジェクト定義の品質指標・フィールド名（例: ドメイン固有の検証フラグ） |
| タイトルに **同じワークフロー・ステップ名** が含まれる | 各パイプライン名・ステップ名（プロジェクト定義） |
| タイトルに **同じ問題パターン** を指している | 「〜を検証する」「〜をチェックする」といった表現が同じ対象を指している |

> **判定の迷い時の原則**: 同じファイル・ツール・フィールドを対象とした改善提案は、たとえ観点が少し異なっても「類似」として既存 Issue にまとめる。Issue の乱立を防ぎ、関連情報を一箇所に集約することを優先する。

---

## G. Step 3-B: 既存 Issue へのコメント追記（類似あり）

類似 Issue が見つかった場合、新規 Issue を作成せず既存 Issue にコメントで情報を追記する。

`mcp__github__add_issue_comment` を使用（`owner: "kai-kou"` / `repo: "tsukuru-studio"` / `issue_number: {既存Issueの番号}`）。

### コメントテンプレート

```markdown
## 再発検知（{YYYY-MM-DD} / {pipeline} パイプライン・{entity_id}）

同じ問題パターンが再び検出されました。

### 今回の発生状況

**ワークフロー**: {pipeline}（{entity_id}）
**担当レビュアー**: {role_name}

### 問題・課題

{対応する Problem の detail}

### 今回の改善提案

{try.detail}

### 再発回数

このコメントをもって {N} 回目の検知となります。優先度の引き上げを検討してください。

---
*レトロスペクティブスキルによる自動追記*
```

追記後、既存 Issue の番号を「コメント追記」として記録し、Step 5 の完了報告に含める。

> **優先度エスカレーション**: 同一 Issue に 3 回以上再発が確認された場合、既存 Issue の priority ラベルを `priority:high` に引き上げる（現在 high でなければ）。

---

## H. Step 3-C: 新規 Issue 作成（類似なし）

> 前提: 本テンプレートを使うのは **資格判定を満たした Try だけ**（`docs/rules/retrospective-rules.md`「WIP 制御」の資格判定式）。資格判定を満たさない Try は Issue を作らず台帳記録のみ（K）。

類似 Issue が見つからなかった場合のみ新規 Issue を作成する。`mcp__github__issue_write`（`method: "create"`）を使用する。

```
method: "create"
owner: "kai-kou"
repo: "tsukuru-studio"
title: "[Retro][{pipeline}] {try.title}"
labels: [
  "type:retro-try",                       # ← フィルタ用の主キー（必須）
  "type:improvement",
  "{try.estimated_effort を sp:N に写像}",  # small→sp:2 / medium→sp:3 / large→sp:5（session-sprint-rules.md §3.3・必須）
  "assignee:{try.assignee}",
  "priority:{try.priority}",
  "urgency:{try.urgency}",               # blocker / quality / process / doc-only
  "done_type:{try.done_type}",           # A-doc / B-script / C-validate / D-plan（done_type フィールドの値をそのまま使う）
  "status:waiting-{try.assignee}"        # assignee の値（claude/user）に応じて設定
]
body: （本文テンプレートに従って生成）
```

### Issue 本文テンプレート

```markdown
## 背景

**ワークフロー**: {pipeline}（{entity_id}）
**レトロスペクティブ日**: {YYYY-MM-DD}
**担当レビュアー**: {role_name}

## 問題・課題

{対応する Problem の detail（なければ「Try アイテムとして直接提案」）}

## 改善施策

{try.detail}

## 期待効果

- {具体的な改善内容}
- 再発防止・品質・自動化効率の向上

## 関連情報

- Pipeline PR: {pr_url}（あれば）
- 推定工数: {try.estimated_effort}（small / medium / large）
- 参考ルールファイル: {関連する docs/rules/ のファイル名}
- 起票理由: {blocker 即時 / 観測 n 回目（初回 {日付}）+ 空き枠あり}

---
*このIssueはレトロスペクティブスキルにより自動生成されました*
*フィルタ: `type:retro-try` ラベル（クラウド: `mcp__github__list_issues(labels=["type:retro-try"])` / ローカル: `gh issue list -R kai-kou/btc-realtime-chart --label "type:retro-try" --state open`）*
```

---

## I. Step 6: lessons 更新テンプレート

### 条件 A: 新規 Problem パターン — Warm 層への新規エントリ

**採番ルール**: `L-{N}` の N は全 lessons ファイル横断の最大番号 + 1 とする。

```bash
grep -rhoP 'L-\K[0-9]+' docs/rules/lessons-core.md docs/rules/lessons/ docs/rules/lessons-archive.md | sort -n | tail -1
```

で最大番号を取得し、重複・欠番を防ぐ。追記後にコミット:

```bash
git add docs/rules/lessons/{カテゴリ}.md
git commit -m "docs: lessons/{カテゴリ} L-{N} 追加（{パターン名}）（{pipeline} {entity_id}）"
git push
```

新規エントリのフォーマット:

```markdown
### L-{N}: {パターン名}（{YYYY-MM-DD}）

**パターン**: {繰り返し発生している問題の説明}

**根本原因**: {なぜ発生するのかの分析}

**試して失敗したアプローチ**:
- 初回発見のため記録なし

**対策**: {効果的だった解決策、または「要調査」}

**参照**: {関連Issue番号・PR番号}

**昇格先**: なし / {反映先}（昇格日: {YYYY-MM-DD}）
```

### 条件 B: 既存エントリと同パターンの再発

既存エントリの「**対策**:」セクション末尾に追記してコミット＆push:

```
同パターン再発: {YYYY-MM-DD}（{pipeline} {entity_id}）
→ Lv3（フック）への昇格を検討する
```

> **再発3回超の場合**: `type:retro-try` Issue を作成し（本文に関連する L-{N} を明記）、Lv3 フック（`.claude/hooks/post-tool-use-validate.sh`）への昇格を推奨する。

---

## J. Try Issue 対応フロー（別ワークフロー）

本スキルが資格判定を満たして **昇格させた**（Issue 化・reopen した）Try のみが本フローの対象になる（台帳記録のみで留まっている Try は Issue ではないため対象外）。以下のフィルタで次回実行時に取得・対応する（実際の対応は `retro-try-handler` スキルが担う。`retro-try-handler` は台帳を読まず、オープン Issue の消化のみを行う）。

MCP（クラウド・一次経路）:
```
# 未対応の Try Issue を一覧取得（複数ラベルは OR のため単一ラベルで取得し client-side で AND 判定）
mcp__github__list_issues(owner, repo, state="OPEN", labels=["type:retro-try"])
  → 応答の labels に "status:waiting-claude" を含む Issue のみ対象にする

# 対応中にする（labels は全置換のため、現在のラベルから waiting-claude を除き in-progress を加えたフルリストを渡す・§2.2）
mcp__github__issue_write(method="update", issue_number=N, labels=[現在のラベル − "status:waiting-claude" ＋ "status:in-progress"])

# 対応完了でクローズ
mcp__github__add_issue_comment(owner, repo, issue_number=N, body="対応完了。{コミットハッシュ} で修正済み")
mcp__github__issue_write(method="update", issue_number=N, state="closed")
```

ローカル環境（gh CLI 到達可能時）の代替:
```bash
# 未対応の Try Issue を一覧取得
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" \
  --label "status:waiting-claude" \
  --state open \
  --limit 1000

# 対応中にする（issue番号は実際の番号に置き換え）
gh issue edit {number} \
  --remove-label "status:waiting-claude" \
  --add-label "status:in-progress" \
  -R kai-kou/btc-realtime-chart

# 対応完了でクローズ
gh issue close {number} \
  --comment "対応完了。{コミットハッシュ} で修正済み" \
  -R kai-kou/btc-realtime-chart
```

---

## K. 候補台帳（Candidate Ledger・PULL 型・#662）

> 数値（観測窓 30 日・WIP 上限 6 件・台帳保持 90 日・ローテーション 300 件）の SSOT は `docs/rules/retrospective-rules.md`「WIP 制御」。本節は運用手順のみを定義する。

### 1. 台帳 Issue の仕様

- **タイトル**: `[Retro][ledger] Try 候補台帳 #{通番}`（通番はローテーション時に 1 ずつ増やす。初回は `#1`）
- **ラベル**: `type:retro-try` のみ（新ラベルは作らない。クラウドの MCP にラベル作成 API が無いため・`github-mcp-fallback-patterns.md` §2.5）。`status:*` は付けない（消化対象の作業 Issue と区別するため）
- **本文テンプレート**（集約表。read-modify-write で更新する。**新規作成時はヘッダー行までで、データ行は空**）:

```markdown
候補台帳（PULL 型・資格判定を満たさない Try のプール）。数値の SSOT は `docs/rules/retrospective-rules.md`「WIP 制御」。
**この Issue は消化対象の作業 Issue ではない**（実装・クローズしない。`retro-try-handler` / 自動消化ルーティンはタイトル接頭辞 `[Retro][ledger]` で除外する）。
直接編集しないこと（`retrospective` Step 3-2 が read-modify-write で更新する）。

| key | count | first_seen | last_seen | urgency | last_title | issue |
|-----|-------|-----------|-----------|---------|-----------|-------|
```

データ行の例（**テンプレートには含めない**。記入形式の参考のみ）:

```markdown
| script\|fact_check_flags | 1 | 2026-09-14 | 2026-09-14 | quality | fact_check_flags の自動解消率をセルフレビューで検出する | |
```

`count` は観測窓内の連続観測回数（F の 0.: 前回 `last_seen` から観測窓を超えて再観測されたら 1 にリセット）。`issue` 列は昇格（Issue 化・reopen）したキーだけ昇格先 Issue 番号を記入する。

### 2. コメントテンプレート（監査ログ・1 レトロ 1 本）

```json
[{"key": "script|fact_check_flags", "date": "2026-09-14", "pipeline": "script", "entity_id": "V001",
  "title": "fact_check_flags の自動解消率をセルフレビューで検出する", "urgency": "quality", "done_type": "A-doc",
  "priority": "medium", "estimated_effort": "small", "promoted_to": null}]
```

1 レコード = 1 Try（配列に今回の全 Try を並べる）。`promoted_to` は昇格した場合のみ Issue 番号を入れる（それ以外は `null`）。

### 3. 本文更新手順（read-modify-write）

Step 3-0 で読んだ本文は **資格判定用の参照** に留め、書き込みは次の手順で行う（Step 3-1 の処理時間の間に他セッションが本文を更新していても上書きしないため）:

1. **書き込み直前に** `mcp__github__issue_read(method="get", issue_number={台帳番号})` で本文を **読み直す**
2. 読み直した本文の集約表に、今回のレトロで決めた **キーごとの差分** を再適用する（Step 3-0 時点の本文に差分を当てた結果を丸ごと書かない）: 既存キーは `count` を今回の観測分だけ加算（観測窓超えのリセットは Step 3-1 の判定どおり）し `last_seen`/`last_title`/`urgency` を今回の値で更新、新規キーは行を追加、昇格したキーは `issue` 列を記入、90 日超キーは削除
3. `mcp__github__issue_write(method="update", issue_number={台帳番号}, body={更新後の本文})` で書き戻す

同時実行で失われるのは「1 と 3 の間（数秒）に別セッションが書いた 1 件の観測」だけであり、その観測はコメント（監査ログ）に残るため次回観測時に回復する（許容。台帳を排他ロックする仕組みは持たない・YAGNI）。

### 4. 台帳保持・ローテーション・再構築

- **保持**: 本文更新時、`last_seen` が現在から **90 日を超えた** キーは集約表から削除する（本文をトークン有界に保つ。削除された観測はコメントに残るため履歴は失われない）
- **ローテーション**: 台帳の `comments` 数（`open_list` 応答で追加 API なしに取得可能）が **300 件以上** になったら、新しい台帳 Issue（`[Retro][ledger] Try 候補台帳 #{通番+1}`）を作成して現行の集約表本文を引き継ぎ、旧台帳に「→ #{新台帳番号}」のポインタコメントを付けて `not_planned` でクローズする。以降の Step 3-0 は `open_list` から新台帳を特定する
- **再構築**: 本文の集約表がパース不能な場合、直近のコメント（監査ログ）を可能な範囲で走査してキー・観測回数を再構築してから書き戻す。全件走査が困難なら直近のコメントのみで再構築し、完了報告に「台帳を部分再構築した」旨を明記する

### 5. 移行手順（台帳不在時に 1 回だけ・下流の初回レトロ）

台帳が存在しないことは「未移行」の合図。Step 3-0 で次を **1 回だけ** 実行する:

1. 台帳 Issue を新規作成する（本文は空の集約表テンプレート）
2. 既存オープン `type:retro-try` Issue のうち `status:in-progress` と `urgency:blocker` を **除いた** ものを、更新日の新しい順に **WIP 上限の件数だけ残し**（数値は SSOT）、残りを台帳へ吸収する
3. 吸収する各 Issue について:
   - 台帳の集約表に 1 行追加（`count=1`・`first_seen`/`last_seen` は元 Issue の作成日・`issue` 列に `#N（吸収）` と記入して観測 1 回で記録。以後の資格判定では `issue` 列の吸収元を F の `closed_list` 突合と同様に reopen 先として使う）
   - 対象 Issue に吸収コメントを追加:
     ```markdown
     候補台帳 #{台帳番号} へ吸収（PULL 転換・#662）。同一キーが再発すれば資格判定で reopen される。
     ```
   - **クローズ直前にラベルを再取得し**（`mcp__github__issue_read(method="get_labels")`）、`status:in-progress` が付いていたら他セッションが着手済みなのでスキップして次へ（選定からクローズまで複数ターンに分かれるため・CP-4）
   - 対象 Issue を `not_planned` でクローズする

移行直後の同一レトロでは `open_list` を再取得しない（吸収でクローズした分が OPEN のまま数えられるが、移行は WIP 上限の件数だけ残す手順なので空き枠は再取得しても 0 で、資格判定の結果は変わらない。完了報告の空き枠表示だけが保守側にずれる）。

**下流のルーティン定義への追記（移行時に 1 回だけ・必須）**: 台帳は `type:retro-try` ラベルを持つオープン Issue なので、オープン Issue を type 不問で消化するルーティン（開発ベースの R-1 ルーティン定義の手順 2 相当・下流はプロジェクト定義）や独自スクリプトがあれば、「タイトルが `[Retro][ledger]` で始まる Issue は対象外」の除外を **取得直後の共有地点** に追記する（sp 補完・選出・内訳のすべてが同じ一覧を使うため）。除外を入れないと台帳が「実装」されてクローズされ、観測履歴を失う。

再発すれば F の判定順（`closed_list` 突合 → `not_planned` 確認 → reopen）で自動的に扱われる。1 Issue あたり 2 API 呼び出し（コメント + クローズ）のため、対象件数が多い場合は 8 呼び出し/ターンの上限（`session-safety-rules.md` ルール 1）に合わせて中間報告を挟み複数ターンに分けてよい。既存 Issue の吸収は A-1〜A-6 に該当せず自律実行する。
