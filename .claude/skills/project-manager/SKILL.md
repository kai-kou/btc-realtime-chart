---
name: project-manager
description: GitHub プロジェクト（Milestones・Projects V2・Issues・Labels）を管理する。タスク作成・ステータス更新・マイルストーン反映・新規指示の Issue 化を行う。「タスクを作って」「Issue にして」「進捗を更新して」「マイルストーンに反映して」と依頼された時に使用する。能動的なタスク運用（作る/進める）が目的。放置された Issue/PR の受動的な衛生掃除が目的なら project-sync を使う。
effort: medium
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# GitHub プロジェクト管理スキル

GitHub Issues + Labels（+ Projects V2）でタスクを一元管理する。
**初回セットアップ手順（ラベル・マイルストーン・Projects V2 の作成コマンド）は開発リポジトリの運用メモに
分離している**（progressive disclosure）。未セットアップのリポジトリでは §「ラベル体系（要約）」を参照して
`gh label create` 等で同等のラベルを用意すること。本スキルは日常運用フローを担う。

> **責務境界（`project-sync` との区別・#26）**: 本スキル（`project-manager`）は **能動的なタスク運用**
> （Issue 作成・ステータス更新・進捗確認・マイルストーン管理）を担う。一方 `project-sync` は
> **受動的なリポジトリ衛生**（Stale Issue リセット・Orphan PR 解消・Abandoned ブランチ検出）の
> メンテナンスを担う（CP-3）。「タスクを作る/進める」= project-manager、「放置された Issue/PR を掃除する」=
> project-sync と切り分ける。両者は下記のラベル体系を共有する。

## トリガー条件

- 「タスクを作って」「Issue にして」「Issue 作成して」
- 「進捗を更新して」「ステータスを変えて」「完了にして」
- 「マイルストーンに反映して」「マイルストーン確認して」
- 「プロジェクト状況を確認して」「進捗を見せて」
- 新規作業指示を受けた時のマイルストーン・タスクへの反映検討
- 工程完了時の自動ステータス更新

## 前提条件

- `gh` CLI が認証済み（`gh auth status`）・`gh auth refresh -s project` で project スコープ付与済み（**ローカル実行時のみ**）
- リポジトリ: `kai-kou/btc-realtime-chart`
- **未セットアップ（ラベル・マイルストーン未作成）の場合は先に「ラベル体系（要約）」に従って `gh label create` 等で初期セットアップを実施**

> 🔴 **クラウド実行環境では repo スコープの `gh`（REST + GraphQL）が egress プロキシに 403 でブロックされる（L-114）。**
> Issue / PR 操作は GitHub MCP（`mcp__github__*`）を一次経路とし、以下の `gh` コマンド例は **ローカル環境向けの代替** として読む
> （SSOT: `docs/rules/github-mcp-fallback-patterns.md`）。`gh project`（Projects V2 GraphQL）と `gh api repos/.../milestones` は
> ⚠️ クラウドでは実行不可（MCP に等価ツールなし・§2.5）。ローカル実行または代替手段（Issue ラベルでの代替運用）が必要。

## ラベル体系（要約）

| 区分 | プレフィックス | 値 |
|------|--------------|-----|
| 種別 | `type:` | feature / bug / docs / improvement / retro-try |
| ステータス | `status:` | waiting-user / waiting-claude / in-progress / blocked |
| 優先度 | `priority:` | critical / high / medium / low |
| 見積もり | `sp:` | 1 / 2 / 3 / 5 / 8（`session-sprint-rules.md` §3） |

色定義・作成コマンドの雛形は開発リポジトリの運用メモに保持している（`gh label create <name> --color <hex> --description <text>` の要領で同等のラベルを作成すればよい）。

---

## 1. 新規指示の Issue 化フロー

ユーザーから新しい作業指示を受けた時の判断フロー（基準は `user-instruction-issue-rules.md`）:

```
新規指示を受信
    │
    ├── マイルストーンに関連するか？
    │     ├── Yes → 既存タスクに該当 → 既存 Issue のステータス更新
    │     │          該当なし → 新規 Issue を作成しマイルストーンに紐付け
    │     └── No  → マイルストーンなしで新規 Issue 作成
    │
    └── 規模の判断
          ├── 小（1 Issue） → 直接作成
          ├── 中（2-5 Issues） → 親 Issue + サブタスクを tasklist 記法で管理
          └── 大（新マイルストーン級・A-5） → ユーザー確認してからマイルストーン追加
```

#### 「大」で、まだ形が見えていないとき（親 Issue の書き方）

「大」には 2 種類ある。**やることは決まっていて量が多いだけ** のものと、**そもそも何を決めれば
着手できるのかがまだ見えていない**もの。後者を前者と同じ粒度で子 Issue に割ると、
前提が変わるたびに割り直すことになる。

後者では、親 Issue（Epic）の本文に次の 3 節を置く。子 Issue は **今すでに問いとして書けるものだけ** 作る。

```markdown
## Destination

<この Epic を終えたとき何が得られているか。1〜2 行>
<以降のセッションは、着手するチケットを選ぶ前に必ずここを読む>

## Not yet specified

<来ることは分かるが、まだ問いとして鋭く書けない領域。前提が片付くと子 Issue に昇格する>

## Out of scope

<この Destination の外側だと意識的に判断したもの。昇格しない>
```

**子 Issue にするか Not yet specified に置くかの判定基準は「今その問いを正確に述べられるか」であって、
「今答えられるか」ではない。** 述べられるなら、他の Issue にブロックされていても子 Issue にする
（ブロック関係は tasklist / sub-issue で表現できる）。述べられないなら Not yet specified に
粗いまま置く — ここを無理にチケット化すると、1 つの領域が実際には複数の問いに割れたり、
前提が変わって 1 つも残らなかったりする。

**Out of scope は Not yet specified と違って昇格しない。** 判断の軸が「鋭さ」ではなく「範囲」だから。
既存の子 Issue が Destination の外だと分かったら、その場で解決せずクローズし、
Out of scope に 1 行（要旨と外した理由・クローズした Issue へのリンク）を残す。
Destination 自体を引き直す場合は、この Epic の再開ではなく **新しい Epic として起票する**。

### Issue 作成テンプレート

MCP（クラウド・一次経路）:
```
mcp__github__issue_write(method="create", owner, repo,
  title="{title_prefix}: {タスク名}",
  body="{下記テンプレートと同構成}",
  labels=["{type_label}", "{priority_label}", "{sp_label}"])
```

ローカル環境（gh CLI 到達可能時）の代替:
```bash
gh issue create \
  --title "{title_prefix}: {タスク名}" \
  --body "$(cat <<'EOF'
## 概要
{概要}

## 背景・動機
{なぜ必要か}

## 作業内容
- [ ] {作業項目1}
- [ ] {作業項目2}

## 完了条件
{完了条件}
EOF
)" \
  --label "{type_label},{priority_label},{sp_label}" \
  --repo kai-kou/btc-realtime-chart
```

> プレースホルダの区別: `{title_prefix}` はタイトル接頭辞（`feat`/`fix`/`docs`/`improvement`）、
> `{type_label}`/`{priority_label}`/`{sp_label}` は **ラベル値**（`type:feature`/`priority:high`/`sp:3` の形）。
> タイトル接頭辞とラベルは別物なので混同しないこと（接頭辞をラベルに入れると無効ラベルになる）。

作業開始時は **最初のアクション** で `status:in-progress` を付与する（CP-4 論理ロック）。

---

## 2. ステータス更新フロー

```
作業開始時:  status:in-progress を付与（+ Projects V2 を "In Progress"）
PR 作成時:   Projects V2 を "In Review" / Issue にPRリンクを追記
PR マージ時: Issue を close（PR 本文に "Closes #N"）→ Projects V2 は自動で "Done"
```

### Projects V2 のステータス更新（使う場合・**ローカル実行のみ**）

> ⚠️ クラウドでは実行不可（MCP に等価ツールなし・§2.5）。`gh project`（Projects V2 GraphQL）はクラウドで 403 のため、
> ローカル実行または代替手段が必要。クラウドセッションではこのステップをスキップし、`status:*` Issue ラベルで代替する
> （built-in automation の close → Done は維持される）。

```bash
# 1. フィールドID・オプションID を取得
gh project field-list PROJECT_NUMBER --owner kai-kou --format json
# 2. アイテムID を取得
gh project item-list PROJECT_NUMBER --owner kai-kou --format json
# 3. ステータスを更新
gh project item-edit --project-id PROJECT_ID --id ITEM_ID \
  --field-id STATUS_FIELD_ID --single-select-option-id OPTION_ID
```

### PR 作成コマンド

MCP（クラウド・一次経路）:
```
mcp__github__create_pull_request(owner, repo, title="{PRタイトル}",
  head="{現在のブランチ名}", base="main", body="{PR本文（Closes #N を含める）}")
```

ローカル環境（gh CLI 到達可能時）の代替。プロキシ環境では remote からブランチを自動検出できないため `--head` / `--base` を **必ず** 付与する:

```bash
gh pr create --head {現在のブランチ名} --base main \
  --title "{PRタイトル}" --body "$(cat <<'EOF'
{PR本文（Closes #N を含める）}
EOF
)" --repo kai-kou/btc-realtime-chart
```

> `--head` 省略は "could not resolve remote" エラーの原因。ブランチ名は `git branch --show-current` で確認。
> 🔴 クラウドでは gh の repo スコープ操作（REST + GraphQL）が 403 でブロックされるため、Issue/PR 操作は `mcp__github__*` ツールを一次経路にする（L-114・`docs/rules/github-mcp-fallback-patterns.md`）。

---

## 3. 進捗確認フロー

MCP（クラウド・一次経路）:
```
# ステータス別の Issue 一覧
mcp__github__list_issues(owner, repo, state="OPEN", labels=["status:in-progress"])
mcp__github__list_issues(owner, repo, state="ALL", perPage=100)

# マイルストーン別進捗: milestones REST の等価 MCP なし → list_issues 応答の milestone フィールドから集計する
```

ローカル環境（gh CLI 到達可能時）の代替:
```bash
# マイルストーン別進捗
gh api repos/kai-kou/btc-realtime-chart/milestones --jq '.[] | "\(.title): \(.closed_issues)/\(.open_issues + .closed_issues) done"'

# ステータス別の Issue 一覧
gh issue list --label "status:in-progress" --state open --repo kai-kou/btc-realtime-chart
gh issue list --state all --limit 100 --repo kai-kou/btc-realtime-chart

# Projects V2 ボード（使う場合・ローカルのみ。クラウドでは実行不能のためスキップし Issue ラベルで代替）
gh project item-list PROJECT_NUMBER --owner kai-kou --format json
```

---

## 4. マイルストーン完了判定

マイルストーン内の全 Issue が close されたら、完了基準を確認してマイルストーンを close する
（⚠️ クラウドでは実行不可（MCP に等価ツールなし・§2.5）: milestones REST はクラウドで 403 のため、
ローカル実行または代替手段が必要。クラウドではスキップし、束ねた epic Issue のクローズで代替する）:

```bash
gh api repos/kai-kou/btc-realtime-chart/milestones --jq '.[] | "\(.number): \(.title)"'
gh api --method PATCH repos/kai-kou/btc-realtime-chart/milestones/MILESTONE_NUMBER -f state="closed"
```

`milestone:M*` ラベル運用の場合は、束ねた epic Issue を完了コメント付きで close する。

---

## Issue 命名規約

| パターン | 例 | 用途 |
|---------|-----|------|
| `feat: {機能名}` | `feat: project-manager スキル追加` | 機能開発 |
| `fix: {修正名}` | `fix: フック出力スキーマ修正` | バグ修正 |
| `docs: {内容}` | `docs: プロジェクト管理ルール追加` | ドキュメント |
| `improvement: {概要}` | `improvement: Hot 層スリム化` | 改善・リファクタ |

> ドメイン固有の命名（工程タスク等）が必要なプロジェクトは本表に追記する。

---

## 注意事項

- **1 コマンド = 1 回の Bash 呼び出し**: CLAUDE.md の Bash 実行ルールに従い、シェル演算子で結合しない
- **Issue は基本的に Claude が作成**: ユーザー指示を受けて Claude が Issue 化する（`user-instruction-issue-rules.md`）
- **完了済みタスクの移行**: 完了済みも Issue 化して即 close（トレーサビリティ確保）
- **Projects V2 は open/close と連動**: built-in automation（close → Done）を活用
- **`priority:*` / `sp:*` は PO ロール権限**（`.claude/agents/owner.md`）。`status:*` は CP-4 論理ロックのため PO でも操作しない
- **private リポジトリの制限**: Free プランは Rulesets 不可。レビューは Claude 自身の Layer 1 セルフレビュー（必須・自前 `code-review` スキル。組み込みを同名 project スキルで置換済み・自律起動可・#280）で完結し、外部 AI レビュアー（Copilot / Gemini）への依頼はしない（SSOT: `docs/rules/ai-reviewer-strategy.md`）
