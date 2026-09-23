---
name: retro-try-handler
description: type:retro-try ラベル（およびプロジェクト定義の更新系ラベル）の未対応 GitHub Issue を自動検出・分類・実装・PR 化するスキル。レトロスペクティブスキルが生成した Try アイテムを実際の改善コードとして反映する。「retro-try 対応して」「Try Issue を処理して」「/retro-try-handler」と依頼された時、または日次の消化スロット（プロジェクト定義）から自動起動する時に使用する。
effort: medium
model: haiku
---

# retro-try-handler スキル

`type:retro-try` + `status:waiting-claude` の未対応 Issue を自動処理する。
詳細ルールは `docs/rules/retrospective-rules.md`、各 Step の詳細テンプレート・コマンド例は
`reference.md`（プログレッシブ・ディスクロージャ。該当 Step 実行直前に該当セクションだけ Read する）を参照。

> 🔴 クラウドでは repo スコープの `gh` が 403 になる（L-114）ため、本 SKILL.md の Issue/PR 操作は
> GitHub MCP（`mcp__github__*`）を一次経路とする。**複数ラベル指定は OR**・`issue_write` の `labels` は
> **全置換**・`perPage` 既定は 100 件までなどの gotcha は SSOT: `docs/rules/github-mcp-fallback-patterns.md`
> （§2.1〜2.3）を参照。ローカル環境（gh が直接到達可能）では相当の gh コマンドで代替してよい
> （詳細コマンド例は `reference.md`）。

## トリガー条件

- 「retro-try 対応して」「Try Issue を処理して」「レトロスペクティブ結果を反映して」
- `/retro-try-handler`
- 日次の消化スロット（プロジェクト定義）+ 週次の{親ワークフロー}（プロジェクト定義）内からの呼び出し

## 前提条件

- GitHub MCP（`mcp__github__issue_write` / `issue_read` / `list_issues` / `create_pull_request` 等）が利用可能なこと
- 作業ブランチ（`claude/retro-try-*`）を新規作成してから作業を開始すること

## 実行フロー概要

```
Step 0: ブランチ確認・作業ブランチ作成
  ↓
Step 1: 未対応 Try Issue の取得・ソート
  ↓
Step 1.5: 未着手 Try の TTL 自動クローズ（30 日更新なし → not_planned・最大 5 件）
  ↓
Step 2: Issue を分類（ドキュメント / スクリプト / バリデーション / スキル / ユーザー対応）
  ↓
Step 3: small / medium を優先実装（large はコメントのみ）
  ↓
Step 4: コミット・push → Step 4.5: PR バンドル判定
  ↓
Step 5: PR 作成・AIレビュー・自動マージ → Step 5.5: lessons 昇格チェック
  ↓
Step 6: 完了サマリー出力（マージ後のみ）
```

---

## Step 0: ブランチ確認・作業ブランチ作成

```bash
git branch --show-current
```

`main` または別タスクのブランチにいる場合は、新しいブランチを作成する（git 操作はクラウドでも生存する）。

```bash
git checkout main && git pull origin main
git checkout -b claude/retro-try-handler-{session_id}
```

> `{session_id}` は日付形式（例: `20260401`）。スケジューラー起動時は `$(date +%Y%m%d)` で取得できる。

### ルールファイル読み込み

`docs/rules/self-review-checklist.md`（過去のレビュー学習内容。旧ファイル名は廃止済みのため参照しない）を `docs/rules/` から Read する。

---

## Step 1: 未対応 Issue の取得・ソート

対象は 2 種類:
- **1-A: レトロスペクティブ Try Issue**（`type:retro-try`）
- **1-B: 更新系 Issue**（プロジェクト定義の `feat:*-update` ラベル）

```
mcp__github__list_issues(owner, repo, state="OPEN", labels=["type:retro-try"])
```

⚠️ `labels` の複数指定は OR（`github-mcp-fallback-patterns.md` §2.1）。`status:waiting-claude` も
かけたい場合は最も絞り込み効果の高い `type:retro-try` のみで取得し、応答の `labels` 配列に
`status:waiting-claude` も含まれる Issue だけを client-side で絞り込む。

⚠️ 取得結果からタイトルが `[Retro][ledger]` で始まる Issue（候補台帳 Issue）を除外する。台帳は
`retrospective` 側だけが読み書きし、本スキルは消化対象に含めない（`status:waiting-claude` の
client-side 絞り込みで通常は既に落ちるが、取りこぼし防止のため明示する）。

取得後、以下の優先順でソートする（`reference.md` A に urgency/priority マッピングの完全な決定表がある）:

1. `urgency:blocker` → `dep:blocking` → `urgency:quality`+`priority:high` → `urgency:process`+`priority:high` → …
2. urgency ラベル未設定（旧形式）は `priority:high/medium/low` → なしの順にフォールバック
3. 同順位内は `createdAt` 古い順

`urgency:doc-only` は曜日で絞らず毎日候補に含める（旧「月曜のみ処理」ルールは廃止・#563。tier 99 のまま最下位で競合するため、深い在庫では Step 1.5 の TTL が退出させ、選ばれた場合は同一カテゴリ small としてバンドル PR にまとめる）。

1-B（更新系 Issue）の取得・優先順位も `reference.md` A を参照（ツール/SDK 更新 > 制作ツール更新 > ドメイン/戦略更新 > 通常の retro-try）。

対象が 0 件の場合は「未対応の retro-try Issue はありませんでした」と出力して終了する（Step 1.5 の TTL クローズは対象 0 件でも実行済みであること）。

---

## Step 1.5: 未着手 Try の TTL 自動クローズ（昇格済み Issue のバックストップ出口）

> 数値（TTL 30 日・1 回最大 5 件・reopen 窓 90 日）の SSOT は `docs/rules/retrospective-rules.md`「WIP 制御」。
> `type:retro-try` はリファインメントの 4 出口から除外されている（`improvement-lane-map.md` §2 ルール 5・#160）ため、
> 「実装する」以外の出口を **振り返りレーン内のこの Step** が持つ。PUSH → PULL 転換（#662）後は Issue 化そのものが
> 資格判定を通過した昇格済み Try に限られるため、本 Step は昇格済み Issue のバックストップとして働き、
> 通常運用ではほぼ発火しない（見送られた大半の Try は Issue 化されず候補台帳に留まるため対象に入らない）。
> 候補台帳 Issue（`[Retro][ledger]` 接頭辞）自体は TTL 対象から除外する（判定式は `reference.md` H）。
> Step 2 の選定より前に在庫を減らしてから優先順位付けする。

Step 1 で取得した `type:retro-try` オープン Issue のうち、**`status:waiting-claude` で `urgency:blocker` でなく、30 日間更新がないもの**（`done_type:D-plan` の large も含む）を `not_planned` でクローズし、再発時に reopen される旨のコメントを残す。1 回の実行で **最大 5 件**（残りは次回へ持ち越す。1 件 = 2 呼び出しのため、ツール呼び出し上限〔8 個/ターン〕に合わせて中間報告を挟みターンを分けてよい）。

判定式・API 呼び出し・コメントテンプレート・エラー時の扱いは **`reference.md` H が唯一の定義**（本 Step では再掲しない）。

---

## Step 2: Issue を分類

各 Issue を以下のカテゴリに分類する（本文の「改善施策」「参考ルールファイル」セクションで判断）。

| カテゴリ | 対象ファイル | 実装 Step |
|---------|------------|----------|
| **doc** | `CLAUDE.md` / `docs/rules/*.md` / `SKILL.md` | `reference.md` C-1 |
| **script** | `tools/*.py` / `tools/*.sh` | `reference.md` C-2 |
| **validate** | `.claude/hooks/post-tool-use-validate.sh` | `reference.md` C-3 |
| **skill** | `.claude/skills/**/SKILL.md` | `reference.md` C-1 |
| **user** | `assignee:user` の Issue（実装しない・通知のみ） | `reference.md` C-4 |
| **tool-update** | Claude Code/SDK 新機能の反映 | `reference.md` C-5 |
| **domain** | プロジェクト定義の戦略・設定ファイル | `reference.md` C-6 |
| **dev-tool** | プロジェクト定義の制作ツール関連ルール | `reference.md` C-7 |

### 1 セッションの処理上限（動的・バックログ残件数に応じる）

| バックログ残件数 | 処理上限 | 理由 |
|--------------|---------|------|
| 0〜9件 | 2件 | 通常運用 |
| 10〜19件 | 3件 | 消化ペース加速 |
| 20〜29件 | 4件 | バックログ圧縮モード |
| 30件以上 | 5件 | 最大スループット |

> 1 ターンのツール呼び出しは 8 個以内（`session-safety-rules.md`）。処理上限を増やすときは中間報告を挟んで複数ターンに分散する。
> PULL 転換（#662）後はオープン在庫が `retrospective` 側の WIP 上限（`docs/rules/retrospective-rules.md`「WIP 制御」）
> 近傍に収まるため、通常運用は表の 0〜9 件区分で回る。表の上位区分（10 件以上）は移行期・blocker 連発時のバッファであり、
> 恒常的に上位区分が発生する場合は資格判定ゲート（`retrospective` Step 3）が機能していない疑いとして扱う。

推定工数ごとの対応方針: `small` は処理上限まで実装、`medium` は 1〜2 件、`large` は実装計画コメントのみ投稿（`done_type:D-plan` を付与し次回に回す）。

**large の空撃ち防止（#563）**: `large` の選出は **1 セッションにつき 1 件まで**（実装計画コメントのみで枠を消費し、クローズ 0 件のまま処理上限を圧迫するのを防ぐ）。さらに **既に `done_type:D-plan` が付いており、前回の計画コメント以降に新しいコメント・ラベル変更がない Issue は選出しない**（同じ計画コメントの再投稿を止め、更新が止まった large は Step 1.5 の TTL が退出させる。詳細は `reference.md` C-4）。

---

## Step 3: 対応実装

実装開始前に **論理ロック**（CP-4）として `status:waiting-claude` → `status:in-progress` に更新する。

```
mcp__github__issue_write(method="update", owner, repo, issue_number={N},
  labels=[現在のラベルから "status:waiting-claude" を除き "status:in-progress" を加えたフルリスト])
```

> ⚠️ `issue_write` の `labels` は全置換（gh の `--remove-label` のような差分指定ではない）。現在のラベル一覧を
> `list_issues` の応答または `issue_read(get_labels)` で取得してから組み立てる。

- **user カテゴリ**: 実装せず Slack 通知のみ（`reference.md` C-4）
- **large 工数**: 実装計画をコメント投稿し `done_type:D-plan` を付与、ステータスは `status:waiting-claude` のまま維持（`reference.md` C-4）
- **doc / skill / validate / script / tool-update / domain / dev-tool**: 各カテゴリの実装手順は `reference.md` C-1〜C-7 を Read してから実施する

実装の原則: 変更は最小限（Issue で指示された箇所のみ）。**1 Issue = 1 コミット** を原則とする。

---

## Step 4: コミット・push

```bash
git add {変更したファイル}
git commit -m "[Retro] {カテゴリ}: {Issue タイトルの要約}（Closes #{number}）"
```

全 Issue の実装が完了したら `git push -u origin {current_branch}`。

## Step 4.5: PR バンドル判定（AI レビューコスト削減）

同一セッションで複数 Issue を実装した場合、条件を満たせば PR をバンドルする（同一カテゴリ・全て `small`・
ファイル競合なし・2〜3 件）。バンドル可否の詳細条件・コミット/PR 説明文テンプレートは `reference.md` D を参照。

---

## Step 5: Issue クローズ・PR 作成・AIレビュー・自動マージ

> ⚠️ **完了報告は必ず PR マージ後に出力すること**（Step 6 はマージ後のみ実行・L-056 対策）。

**全ての変更は PR を作成して `main` にマージする**（`main` への直接 push は禁止）。

1. **セルフレビュー**: `self-reviewer` スキルに従う（シェル構文チェック・JSON 構文チェック）
2. **PR 作成**: `mcp__github__create_pull_request`（本文テンプレートは `reference.md` F）
3. **PR 存在確認（L-050）**: `mcp__github__list_pull_requests(owner, repo, head="{owner}:{current_branch}", state="open")` で 1 件ヒットすることを確認（`head` は `{owner}:{branch}` 形式が必須）
4. **Slack 通知**: `python3 tools/slack_notify.py pr --pr-url ... --pr-title "[PR作成] ..." --branch ...`
5. **Layer 1 セルフレビュー（必須）**: 自前 `code-review` スキルを `Skill(code-review)` で必ず実行（`.claude/skills/code-review/` が組み込みを置換・自律起動可・#280）。外部 AI レビュアー（Copilot/Gemini）への依頼はしない。diff ≥300行 / `type:security` / `type:breaking-change` / `high_risk` 差分（#627）は Layer 2（`discussion_review_trigger.py`）も起動
6. **レビュー対応・自動マージ**: 指摘対応（修正コミット or スキップ+返信+Resolve）→ Layer 0+1 通過で `mcp__github__merge_pull_request(owner, repo, pullNumber={pr_number}, merge_method="squash")`

Issue クローズは PR 本文の `Closes #N` が `main` マージ時に自動処理する（マージ前は `status:in-progress` のまま維持）。自動クローズが働かない場合のみ `mcp__github__issue_write(method="update", owner, repo, issue_number={N}, state="closed", state_reason="completed")` で手動クローズする。

## Step 5.5: lessons 昇格フロー（昇格=物理削除）

Issue クローズ後、対応する lessons エントリを確認する。手順・prune コマンドは `reference.md` E を参照
（`retrospective` スキルの Step 6 と同じテンプレートを使う）。

---

## Step 6: 完了サマリー出力（マージ後のみ実行）

```
## retro-try-handler 完了サマリー

### アウトカム（ユーザー視点）
- {このタスクにより何ができるようになったか・何が改善されたか}

### 対応済み Issue
| # | タイトル | カテゴリ | コミット |
|---|---------|---------|---------|

### スキップ（large / ユーザー対応）
| # | タイトル | 理由 |
|---|---------|------|
```

マージ完了後の Slack 通知テンプレートは `reference.md` F。

---

## エラーハンドリング

| エラー | 対応 |
|--------|------|
| Issue 取得失敗 | `list_issues` を再実行（最大2回）。それでも失敗したら STOP してユーザーに報告 |
| 対象ファイルが存在しない | Issue にコメントを残し、スキップ |
| 編集後にコンパイル/構文エラー | 変更を元に戻して Issue に「保留」コメントを投稿 |
| ブランチ push 失敗 | 指数バックオフでリトライ（最大4回: 2s, 4s, 8s, 16s） |

## 既存スキルとの関係

> レーン境界の SSOT は `docs/rules/improvement-lane-map.md`。本スキルは **振り返りレーン** の下流
> （`type:retro-try` の実装）に限定され、`type:improvement` の消化は改善 Issue レーン
> （`self-improvement-loop` 消化モード）が担当する（奪い合い防止・#160）。

| 関連スキル | 関係 |
|-----------|------|
| `retrospective` | Try Issue を生成する上流スキル。候補台帳（`[Retro][ledger]`）の読み書きは `retrospective` 側のみが担い、本スキルは台帳を参照しない |
| `self-reviewer` | 実装後のセルフレビューに使用（PR 作成時） |
| `pr-review-watcher` | PR 作成後の AIレビュー監視・自動マージに使用 |
| `project-manager` | Projects V2 のステータス更新が必要な場合に参照 |

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `reference.md` | Step 別の詳細コマンド・テンプレート（プログレッシブ・ディスクロージャ） |
| `docs/rules/retrospective-rules.md` | 詳細ルール |
| `docs/rules/self-review-checklist.md` | セルフレビュー学習ログ |
