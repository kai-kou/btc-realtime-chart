# retro-try-handler スキル — 詳細テンプレート集（reference）

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


> SKILL.md の各 Step が参照する詳細コマンド・実装手順・テンプレートをまとめた補助ドキュメント。
> 該当 Step を実行する直前に該当セクションだけを Read する。
>
> 🔴 クラウド実行環境では repo スコープの `gh`（REST + GraphQL）が egress プロキシに 403 でブロックされる
> （L-114）。以下の取得系コマンドは GitHub MCP を一次経路とし、gh CLI 版は **ローカル環境向けの代替** として併記する。
> `list_issues` の複数ラベル指定は **OR**（gh の `--label A --label B` は AND）。以下の MCP 例は
> 単一ラベルで取得し client-side で残りの条件を絞り込む前提で書いている（詳細: SSOT
> `docs/rules/github-mcp-fallback-patterns.md` §2.1）。

---

## A. Step 1: 取得・ソートの詳細

### 1-A: レトロスペクティブ Try Issue

MCP（クラウド・一次経路）:
```
mcp__github__list_issues(owner, repo, state="OPEN", labels=["type:retro-try"])
```
（`status:waiting-claude` も課したい場合は、応答の `labels` 配列にそれも含まれる Issue だけを
client-side で絞り込む。上記の通り複数ラベル指定は OR のため。）

取得結果（`labels` 配列を含む）をもとに、以下の優先順で **Claude がクライアント側でソート** する
（jq 相当のロジックをそのまま踏襲）:

```
0: urgency:blocker
1: dep:blocking
2: urgency:quality かつ priority:high
3: urgency:process かつ priority:high
4: urgency:quality かつ priority:medium
5: urgency:process かつ priority:medium
50: urgency ラベルなし かつ priority:high
51: urgency ラベルなし かつ priority:medium
52: urgency ラベルなし かつ priority:low
53: urgency ラベルなし かつ priority なし
99: urgency:doc-only
（同順位内は createdAt 古い順）
```

> **urgency ラベルが付与されていない Issue（旧形式）**: フォールバックとして priority:high→50、medium→51、low→52、未設定→53 を適用する。urgency ラベル付き Issue が優先処理される。

ローカル環境（gh CLI 到達可能時）の代替:
```bash
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" --label "status:waiting-claude" --state open --limit 1000 \
  --json number,title,labels,body,createdAt \
  --jq 'sort_by([
    (if ([.labels[].name] | index("urgency:blocker"))   then 0
     elif ([.labels[].name] | index("dep:blocking"))    then 1
     elif ([.labels[].name] | index("urgency:quality")  and ([.labels[].name] | index("priority:high")))   then 2
     elif ([.labels[].name] | index("urgency:process")  and ([.labels[].name] | index("priority:high")))   then 3
     elif ([.labels[].name] | index("urgency:quality")  and ([.labels[].name] | index("priority:medium"))) then 4
     elif ([.labels[].name] | index("urgency:process")  and ([.labels[].name] | index("priority:medium"))) then 5
     elif ([.labels[].name] | index("urgency:doc-only")) then 99
     else (if ([.labels[].name] | index("priority:high")) then 50
           elif ([.labels[].name] | index("priority:medium")) then 51
           elif ([.labels[].name] | index("priority:low")) then 52
           else 53 end) end),
    .createdAt
  ])'
```

### doc-only Issue の扱い

`urgency:doc-only` は曜日で絞らず毎日候補に含める（旧「月曜のみ処理」は「6/7 日は候補外・月曜も最下位競争で敗退」の
二重スタベーションを生んでいたため廃止・#563）。段位 99 の最下位は維持し、選ばれた場合は同一カテゴリ `small` として
D のバンドル PR にまとめる。深い在庫で選ばれ続けない doc-only は SKILL.md Step 1.5 の TTL が退出させる。

### 1-B: 更新系 Issue（プロジェクト定義の更新ラベル）

> **プロジェクトで定義する**。上流スキル（例: ツール調査・ドメインリサーチ系スキル）が生成する「更新系」Issue を、プロジェクトが定義する `feat:*-update` ラベルで取得する。下記は汎用テンプレート。

MCP（複数ラベル指定は OR のため、`{更新ラベル}` で取得後 `status:waiting-claude` を client-side で絞り込む）:
```
mcp__github__list_issues(owner, repo, state="OPEN", labels=["{更新ラベル}"])
```

代表的な更新カテゴリの例（プロジェクト定義）:

| カテゴリ（例） | ラベル（例） | 対象 |
|--------------|------------|------|
| ツール/SDK 更新 | `feat:tool-update` | Claude Code / 利用 SDK の新機能・破壊的変更 |
| 制作ツール更新 | `feat:dev-tool-update` | プロジェクト定義の制作ツール・依存ライブラリの更新 |
| ドメイン/戦略更新 | `feat:domain-update` | 配信先・マーケ・ドメイン固有の戦略変更 |

**処理優先順位**: ① ツール/SDK 更新 + `priority:high`（Breaking Change） ② 制作ツール更新 + `priority:high` ③ ドメイン/戦略更新 + `priority:high` ④ `type:retro-try` + `priority:high` ⑤ 上記以外は通常の優先度順

---

## C. Step 3: カテゴリ別実装手順

### C-1: doc / skill カテゴリ

対象ファイルを **Read** で読み込み、Issue の「改善施策」に従って **Edit** で修正する。変更前後の差分を確認してからコミットする。

### C-2: script カテゴリ

`tools/*.py` / `tools/*.sh` を修正する。修正前に既存コードを **Read** で確認し、テスト実行可能な場合は実行して動作確認する。`docs/rules/self-review-checklist.md` の既知パターン（例: `encoding` 未指定・エラー握りつぶし）に注意する。

### C-3: validate カテゴリ

`.claude/hooks/post-tool-use-validate.sh` にチェックを追加する。既存のチェックパターンを **Read** で確認してから追記し、`WARNING`/`ERROR` レベルを区別する。追加後は `bash -n` で構文確認する。

### C-4: user / large の扱い（実装しない）

**user（`assignee:user`）**: 実装せず Slack 通知のみ。
```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" waiting \
  --issues "[Retro] ユーザー対応が必要な Try Issue があります: #{N1}, #{N2}" \
  --branch "{current_branch}"
```

**large 工数**: 実装計画をコメント投稿し `status:waiting-claude` のまま維持する。
```
mcp__github__add_issue_comment(owner, repo, issue_number={N}, body="""
## 実装計画
この Try は工数が large のため、本セッションでは実装計画のみ記載します。
### 実装ステップ
1. {具体的なステップ 1}
2. {具体的なステップ 2}
### 影響ファイル
- {ファイルパス 1}
次回セッションで対応します。
""")
# ⚠️ issue_write の labels は全置換（マージではない）。事前に issue_read(get_labels) 等で
# 現在のラベル一覧を取得し、status:waiting-claude を含む既存ラベルを落とさないフルリストに
# "done_type:D-plan" を加えて渡すこと（CP-4 ロックを誤って外さないため）。
mcp__github__issue_write(method="update", issue_number={N}, labels=[現在の全ラベル + "done_type:D-plan"])
```

**再選出スキップ（#563）**: Step 1 のソート結果に `done_type:D-plan` 付きの Issue が含まれる場合、その Issue の
最新コメントが本スキルの「実装計画」コメントであり、それ以降にコメント・ラベル変更がなければ **当該セッションでは
選出しない**（同じ計画を再投稿して `updated_at` を更新し続けると Step 1.5 の TTL が永久に発火しないため）。
新情報（人手コメント・再発検知コメント・priority 変更）が付いた large は通常どおり選出対象に戻す。

### C-5: tool-update カテゴリ（Claude Code / Anthropic SDK 新機能）

Issue の「参照」セクションの URL を WebFetch/WebSearch で取得してから対応する。

**対応優先順位**: `priority:high`（Breaking Change/Deprecated）は当日中、`priority:medium`（新機能）は週次、`priority:low`（マイナー）は月次。

**判断フロー**:
```
Claude Code の新機能・仕様変更        → docs/rules/claude-code-optimization.md を Edit
CLAUDE.md 記載のモデル名・機能の変更  → CLAUDE.md の当該箇所を Edit
MCP / Agent SDK の仕様変更           → docs/rules/agent-team.md を Edit
API Deprecated（破壊的変更）         → 全ルールファイルを grep して旧 API 参照を修正
```

変更は確認できた情報のみ反映する（推測で古い情報を削除しない）。不確かな情報は `<!-- TODO: 要確認 -->` を残す。更新後は対象ファイル先頭の「最終更新」日付を更新する。

### C-6: domain カテゴリ（ドメイン/戦略更新・プロジェクト定義）

| 優先度 | 対応内容 |
|--------|---------|
| `priority:high` | 「推奨アクション」を当日中に実施。取り消し困難な変更（A-2/A-6 相当）はユーザーに通知も行う |
| `priority:medium` | 週次の{親ワークフロー}内で対応。戦略ドキュメントの更新が中心 |
| `priority:low` | 「確認済み・参考情報として記録」コメントでクローズ |

### C-7: dev-tool カテゴリ（制作ツール/新ライブラリ・プロジェクト定義）

```
制作ツールの新バージョン・API変更 → プロジェクト定義のルールファイルを Edit + 関連スキルも確認
新ライブラリ（依存追加候補）      → 採用可否を Issue の「対応提案」で判断。採用時は tools/ 追加や
                                    requirements.txt/package.json 更新を提案。見送り時はコメントでクローズ
```

---

## D. Step 4.5: PR バンドル判定

### バンドル可能条件（全て満たす場合のみ）

| 条件 | 詳細 |
|------|------|
| 同一カテゴリ | `doc` + `doc`、`skill` + `skill` など（カテゴリをまたぐ場合は別 PR） |
| 推定工数 | 全て `small`（`medium` 以上が1件でもあれば個別 PR） |
| ファイル競合なし | 同一ファイルを複数 Issue が変更する場合は個別 PR |
| Issue 数 | 2〜5件（1件は個別 PR、6件以上はカテゴリを分割して 2PR。PR レビュー往復は 1 本あたり固定費のため、上限拡大は処理上限の引き上げより費用対効果が高い・#563） |

### バンドル PR のコミットメッセージ形式

```
[Retro] {カテゴリ}(bundle): {Issue 1 の要約}・{Issue 2 の要約}（Closes #{N1}, #{N2}）
```

### バンドル PR の説明文テンプレート

```markdown
## 変更内容の概要

{カテゴリ} 小改善 {N}件をバンドル処理。

- Issue #{N1}: {タイトル} — {変更概要}
- Issue #{N2}: {タイトル} — {変更概要}

## セルフレビュー結果

- セルフレビュー: 実施済み（エラー: 0件 / 警告: N件）
- YAML/JSON 構文: エラーなし

Closes #{N1}, #{N2}
```

**バンドルの効果**: 個別 PR（1件ずつ）では AI レビュー往復コストが件数分かかるが、バンドル PR にすることで 1 回に圧縮できる。

---

## E. Step 5.5: lessons 昇格フロー詳細

`retrospective/reference.md` の I 節と同じテンプレートを使う。

### A: 対応した Issue に対応する lessons エントリが存在する場合

```bash
grep -rn "{キーワード}" docs/rules/lessons-core.md docs/rules/lessons/
```

1. 昇格先への実装が **完了** した場合: Hot 層（`lessons-core.md`）なら常駐必須か判定し、不要なら物理削除（`tools/lessons_guard.py prune --apply`）。常駐必須なら `**保持理由**:` を付けて残す。Warm 層（`lessons/{カテゴリ}.md`）なら `**昇格先**: {実装ファイル}（昇格日: YYYY-MM-DD）` を記載
2. 未完了の場合 → `**昇格先**:` フィールドのみ更新してエントリは残す
3. 変更後にコミット・push

### B: 対応した内容が lessons に未記録の場合

Warm 層（`docs/rules/lessons/{カテゴリ}.md`）に新規エントリを追記する。

```markdown
### L-{N}: {パターン名}（{YYYY-MM-DD}）

**パターン**: {Issue で発見した問題パターン}
**根本原因**: {Issue の「背景」セクションから抜粋}
**試して失敗したアプローチ**: 該当なし（初回発見のため記録なし）
**対策**: {今回実施した修正内容}
**参照**: {Issue #{N}、修正コミット}
**昇格先**: `{修正したファイルパス}`（昇格日: YYYY-MM-DD）
```

### C: lessons との対応関係が不明な場合

このステップをスキップし、完了サマリーに「lessons 更新なし」と明記する。

---

## F. Step 5: PR 本文テンプレート・Slack 通知・完了サマリー後の通知

### PR 本文テンプレート

```markdown
## 対応した Try Issue

{対応済み Issue の一覧（Closes #N1, #N2, ...）}

## 変更内容

{変更ファイルと変更概要}

## セルフレビュー結果

{セルフレビューの結果（Error 0件 / Warning N件）}
```

### Slack 通知（PR 作成時）

```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
  --pr-url "https://github.com/kai-kou/btc-realtime-chart/pull/{pr_number}" \
  --pr-title "[PR作成] [Retro] Try Issue 対応（{N}件）" \
  --branch "{current_branch}"
```

### Slack 通知（マージ完了後）

```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
  --pr-url "https://github.com/kai-kou/btc-realtime-chart/pull/{pr_number}" \
  --pr-title "[完了] [Retro] Try Issue 対応（{N}件）" \
  --outcome "{アウトカム1文}" \
  --branch "{current_branch}"
```

---

## G. フィルタコマンド（参考・ローカル環境向け gh CLI 例）

クラウドでは同等のクエリを `mcp__github__list_issues`（labels フィルタ）または `mcp__github__search_issues` で代替する。
🔴 複数ラベルの絞り込みは `list_issues(labels=[A,B])` が **OR** になる点に注意（gh の `--label A --label B` は AND）。
「Claude 担当のみ」「高優先度のみ」は、絞り込み効果が高い方の単一ラベルで取得し、応答の `labels` を見て
残りの条件を client-side で AND 判定する（SSOT: `docs/rules/github-mcp-fallback-patterns.md` §2.1）。

**クラウド（一次経路）**:
```
# 全 Try Issue
mcp__github__list_issues(owner, repo, state="OPEN", labels=["type:retro-try"], perPage=100)

# Claude 担当のみ（"assignee:claude" で取得 → 応答の labels に "type:retro-try" を含むものだけ client-side で残す）
mcp__github__list_issues(owner, repo, state="OPEN", labels=["assignee:claude"], perPage=100)

# 高優先度のみ（"priority:high" で取得 → 応答の labels に "type:retro-try" を含むものだけ client-side で残す）
mcp__github__list_issues(owner, repo, state="OPEN", labels=["priority:high"], perPage=100)

# 特定パイプラインのみ（タイトル検索。{pipeline} を実際のパイプライン名に置き換える）
mcp__github__search_issues(query="repo:{owner}/{repo} is:issue is:open label:type:retro-try \"[Retro][{pipeline}]\" in:title")
```

（以下はローカル実行用・gh CLI 到達可能時）
```bash
# 全 Try Issue を取得
gh issue list -R kai-kou/btc-realtime-chart --label "type:retro-try" --state open --limit 1000

# Claude 担当のみ
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" --label "assignee:claude" --state open --limit 1000

# 高優先度のみ
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" --label "priority:high" --state open --limit 1000

# 特定パイプラインのみ（タイトル検索。{pipeline} を実際のパイプライン名に置き換える）
gh issue list -R kai-kou/btc-realtime-chart \
  --label "type:retro-try" --search "[Retro][{pipeline}]" --limit 1000
```

---

## H. Step 1.5: TTL 自動クローズの判定式・テンプレート

判定（全て AND。数値の SSOT は `docs/rules/retrospective-rules.md`「WIP 制御」）:

```
labels ∋ "status:waiting-claude"
labels ∌ "urgency:blocker"
title が "[Retro][ledger]" で始まらない   # 候補台帳 Issue は TTL 対象外（retrospective 側が管理）
now_utc − updated_at > 30 日        # 内部計算のため UTC 基準（datetime-rules.md の機械処理用 UTC 例外）
```

候補を `updated_at` 昇順（古い順）に並べ、先頭 **5 件** だけ処理する（残りは次回）。1 件ごとに:

```
mcp__github__issue_write(method="update", owner, repo, issue_number={N}, state="closed", state_reason="not_planned")
mcp__github__add_issue_comment(owner, repo, issue_number={N}, body="""
## TTL クローズ（not_planned）
30 日間更新がないため、振り返りレーンの TTL 出口（retro-try-handler Step 1.5）でクローズしました。
- 最終更新: {updated_at を JST で表記}
- 同じ問題が再発した場合は、次回のレトロスペクティブ（retrospective reference.md F）が本 Issue を検出して reopen します
""")
```

- `issue_write` が失敗したら当該 Issue はスキップして次へ進む（リトライは 1 回まで。失敗は Step 6 の完了サマリーに列挙する）
- クローズ結果は Step 6 の完了サマリーに「TTL クローズ: #N, #M（最終更新 N 日前）」として記録する（サイレントに閉じない）
- `state_reason` を `completed` にしない（`completed` は実装済みの意味で、消化率〔workflow-health-check 5-a〕を汚す）
