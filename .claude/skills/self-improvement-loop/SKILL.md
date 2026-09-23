---
name: self-improvement-loop
description: プロジェクト全体を定期的に横断レビューし、根本原因特定→改善Issue起票→棚卸し（集計・重複統合・Epic化・priority/sp 補完・低優先/滞留 Issue のリファインメント）→最優先即実装→マージまで自律実行するセルフ改善オーケストレーター。発見（定期の N 観点横断レビュー）・整理（棚卸し + リファインメント）・消化（改善Issueの高速処理）の3モードで動く。「セルフ改善して」「横断レビューして」「プロジェクト改善して」「改善Issueを棚卸しして」「改善バックログを整理して」「Epic化して」「リファインメントして」「後回しになっている Issue を精査して」「改善Issue消化して」「/self-improvement-loop」と依頼された時、またはプロジェクト定義の発見スロット / 日次消化スロット / リファインメントの週次ゲート（プロジェクト定義の定期ルーティン）で自動実行する。type:retro-try（振り返り由来の Try）は retro-try-handler、Issue/PR の状態・衛生は workflow-health-check / project-sync が担当する。
effort: medium
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# self-improvement-loop スキル

## 目的

プロジェクト全体（プロジェクト定義の N 観点・例: パイプライン健全性・リポジトリ衛生・成果物品質・コードベース・戦略 等）を定期的に横断レビューし、**個別症状ではなくメタ根本原因** を特定して、改善Issueの起票・棚卸し・実装・マージまでを自律実行する。

ユーザーが状況を確認しに来なくても、**課題発見 → 改善実装 が定期的に回る** 状態を維持する（CP-6 Human-on-the-loop）。

## 設計方針：改善 Issue のライフサイクルを 1 スキルで持つ

本スキルは「改善 Issue の世話」（起票 → 整理 → 実装）を **1 スキル 3 モード** で担う（Issue #147 の統合。旧 `improvement-groomer` スキルは整理モードとして本スキルに統合済み）。

| モード | 役割 | 頻度 | コスト |
|--------|------|------|--------|
| **発見モード** | N 観点の並列横断レビューで新規課題を発掘し改善Issueを起票 | 定期（プロジェクト定義の発見スロット） | 高（並列サブエージェント） |
| **整理モード** | 溜まった `type:improvement` の棚卸し（集計・重複統合・Epic 化・priority/sp 補完）+ **リファインメント**（後回しにされた低優先・滞留 Issue を 4 出口へ必ず遷移させる・Step G-1.5 / G-6） | 滞留時（30 件超）/ 月次 / **リファインメントは週次ゲート**（プロジェクト定義の定期ルーティンから起動） | 中 |
| **消化モード** | 起票済み改善Issueを優先度順に高速消化（実装 → PR → マージ） | 日次（プロジェクト定義の消化スロット） | 低〜中 |

**長期放置防止**: 発見（在庫生成）と消化（処理）を分離し、消化を日次・複数件で回すことで改善スループットを最大化する。在庫が積み上がって「多すぎて選べない」状態になったら整理モードを挟む。改善Issueは作成後 **7日で必ず再評価**（project-sync が検出）。

## 他レーンとの境界

責務境界の SSOT は **`docs/rules/improvement-lane-map.md`**（本ファイルでは再定義しない）。要点のみ:

- `type:retro-try` は **振り返りレーン**（`retrospective` → `retro-try-handler`）の担当。本スキルの消化モードは扱わない（奪い合い防止・#160）
- Issue / PR の **状態**（ラベル不整合・Stale・Orphan）は **監査・衛生レーン**（`workflow-health-check` → `project-sync`）の担当
- 監査結果は再監査せず参照する。発見モード Step 0.5 で `workflow-health-check`（軽量版）を実際に呼び出して入力にする（宣言だけの連携をしない）

---

## 発見モード実行フロー（定期・プロジェクト定義の発見スロット）

### Step 0: 前提チェック
```
- gh CLI 動作確認（which gh）。不在なら L-086 に従い session-start.sh の gh インストール成否を確認
- git log / project_state.md で現状把握
```

### Step 0.5: 監査レーンの結果取得（重複監査の回避）

`Skill(workflow-health-check)` を **軽量版**（`--light`・Step 1 PR 健全性 + Step 2 Issue 状態）で実行し、その検出結果（放置 PR・スタック Issue・ラベル不整合）を Step 1 の観点 2（パイプライン健全性）・観点 3（リポジトリ衛生）の **入力として渡す**。本スキルは同種の監査を再実行しない。

> 軽量版が失敗した場合（API エラー等）は監査結果なしで Step 1 に進み、その旨を Step 5 の報告に明記する（発見モード全体は止めない）。

### Step 1: N 観点の並列サブエージェントレビュー
N 体を **1メッセージで並列起動**（model=sonnet 推奨・客観性重視）。各エージェントに自己完結プロンプト + 「強み2-3 / 重大問題3-5（根拠データ必須）/ 改善提案（優先度付き）/ 100点満点スコア」を 800字以内で要求する。

> **観点はプロジェクトで定義する**。下表は汎用テンプレート（プレースホルダ）。各プロジェクトは `docs/project-mission.md` の KPI・ドメインに合わせて観点・調査対象を差し替える（ミッション KPI・パイプライン・成果物品質・キャラ/トーン属性 等）。

| # | 観点（テンプレート） | subagent_type | 主な調査対象（プロジェクト定義） |
|---|------|--------------|------------|
| 1 | ミッション KPI・成長 | general-purpose | プロジェクト定義の分析データ（例: content/analytics/）, メタ情報 |
| 2 | パイプライン健全性 | general-purpose | .claude/skills/*pipeline*, lessons-core.md, discover系ツール, git log, **Step 0.5 の監査結果** |
| 3 | リポジトリ衛生・PM | general-purpose | GitHub MCP（list_issues/PR・status別内訳・滞留日数）, **Step 0.5 の監査結果** |
| 4 | 成果物品質・キャラ/トーン一貫性 | general-purpose | プロジェクト定義の成果物（例: ドメイン固有の検証項目・キャラ属性）, 検証スクリプト |
| 5 | 技術アーキ・コードベース | general-purpose | tools/（規模・重複・死蔵）, tests/, .github/workflows/, requirements |
| 6 | 戦略・利用者価値 | プロジェクト定義のレビュー役（Lv3） | 分析データ, リサーチ資料, 成果物一覧 |

### Step 2: メタ根本原因の特定
- N 観点の所見を統合し、**複数の症状にまたがる共通の根** を3層因果分析（直接/中間/根本）で特定する
- 「個別Issueの寄せ集め」ではなく「1つの根が複数症状を生んでいないか」を最優先で探す
- 必要なら裏取り（実機コマンド・grep）で根本原因を確定する

### Step 3: 改善Issue起票（重複チェック必須）
```
1. 既存オープンIssueを検索（クラウド一次: `mcp__github__search_issues(query, owner, repo)` / ローカル: `gh issue list` 等）し、同一根本原因のIssueが無いか確認
2. 重複が無ければ起票:
   - title: feat:/fix:/improvement: {根本原因に対する対策}
   - labels: type:{feature/bug/improvement} + priority:{high/medium/low} + sp:{2/3/5}（small/medium/large 相当を写像・session-sprint-rules.md §3.3・必須）
   - body: 背景 / 根本原因(3層) / 対応方針 / 完了条件 / 再発防止
3. Issue乱発を避ける（1根本原因=1Issue・refinement の命名規約に準拠）
```

### Step 4: 最優先1件を即実装 → マージ
- priority:high かつ実装コスト小・効果大の1件を選ぶ
- status:in-progress 付与（CP-4 論理ロック）→ 実装 → 効果を実機検証 → コミット → PR → AIレビュー → 自動マージ
- L-086 等のレッスン記録が必要なら lessons に追記

### Step 5: 報告
- 特定した根本原因・起票Issue一覧・実装した最優先課題を Slack/完了報告で出力（L-076 アウトカム形式）

---

## 整理モード実行フロー（棚卸し・grooming）

> **目的**: 溜まった `type:improvement` を棚卸しし、「データなき量産」状態を解消して、消化モードに渡せる整然としたバックログを維持する。
> **2 層構成**: `tools/triage_improvements.py`（重い集計・**読み取り専用**）+ 本フロー（Claude の判断）。

### Step G-0: ロック取得（CP-4）

棚卸しは Issue 群を変更するため、作業 Issue を 1 件（`feat: 改善Issue棚卸し YYYY-MM-DD` 等）作って `status:in-progress` を付与するか、ユーザー指示由来の既存 Issue にロックを取る。スケジュール起動時は直近の棚卸し Issue がオープンでないか確認する（重複起動防止）。

### Step G-1: レポート生成（コード）

```bash
python3 tools/triage_improvements.py --out /tmp/groom_report.md   # Markdown
python3 tools/triage_improvements.py --json > /tmp/groom.json     # 機械処理用
```

レポートの内容: **集計**（priority / sp / 監査フェーズの分布）・**カテゴリ別件数**（監査タグ優先、なければキーワードクラスタ）・**Epic 統合候補**（同一カテゴリに `--epic-threshold`〔既定 6〕件以上集中）・**重複/酷似**（監査ドメインコード重複 / タイトルトークンの Jaccard 類似度 ≥ 0.6）・**ラベル欠損**（priority / sp 未設定。Epic は除外）。

### Step G-1.5: リファインメント対象の抽出（type 非依存・週次ゲートの入口）

> **目的**: R-1 の選択順（priority 降順 → sp 昇順）で **構造的に永久に選ばれない** Issue と、
> ブロック要因の再評価だけでは「そもそも要るのか」を問われない Issue を拾い上げる。
> **入口**: プロジェクト定義の定期ルーティンに配線した週次ゲート（state は `config/backlog_refinement_state.json` の `last_refinement_at`。ファイルが無ければ「未実行」とみなして実行し、初回に作成する）。 <!-- refcheck:ignore -->
> 手動起動（「リファインメントして」）でも同じ手順を実行する。

Step G-1 のレポート（`triage_improvements.py`）は `type:improvement` に限定されるため **本 Step では使わない**。
`mcp__github__list_issues`（`state=OPEN` / `fields` に `number,title,labels,created_at,updated_at,comments`）で
全オープン Issue を取得し、次の述語で対象集合 `T` を計算する（新規ツールは作らない・ラベルと日付だけで再計算可能）。

```
T = R3-1 ∪ R3-2

R3-1（構造的スタベーション予備軍）:
  state = OPEN
  AND status:in-progress / status:waiting-user / status:blocked のいずれも付いていない
  AND (priority:low OR priority ラベル欠落)
  AND (sp:5 OR sp:8)
  AND created_at から 14 日以上経過

R3-2（ブロック理由を超えた精査対象）:
  status:blocked
  AND 定期ルーティンの blocked 再評価が付ける「未解消」コメントが 2 回以上（≒ 14 日以上）
  ただし打ち切り回（≒21 日・A/B/C/D 分類へ強制分岐）に達したものは再評価側の担当

共通の除外:
  status:waiting-user（A 区分＝ユーザー判断待ち。waiting-user-handler の担当）
  type:retro-try（振り返りレーンの専管・#160 の奪い合い防止）
  同じ回のルーティンが blocked 再評価コメントを付けたばかりの Issue
    （二重処理を避け、再評価の打ち切りカウントを優先する。次回の精査で拾う）
```

> **用語の注意**: 本 Step の「リファインメント」は **起票済み Issue の要否・束ね方を問い直す工程** で、
> `session-sprint-rules.md` 系の「refinement 昇格」（起票時に `sp:` を付けてバックログに載せる工程）とは
> 別物。前者はライフサイクル後半、後者は入口の作業を指す。

**type ラベルで絞らない**（`type:feature` / `type:docs` / type 欠落も対象。#335 のカバレッジ不変条件を継承）。

`T` が空なら **no-op**（「対象ゼロ（オープン N 件 / 除外内訳）」を 1 行報告して終了。Step G-6 は実行しない）。

### Step G-2: @owner（PO）連携で優先度・SP を補完

ラベル欠損 Issue（`missing_priority` / `missing_sp`）について `@owner` を **PO として** 呼び出す。@owner は `mcp__github__issue_write` で `sp:` / `priority:` を直接付与できる（`session-sprint-rules.md` §4・ホワイトリストは `sp:` / `priority:` のみ）。基準は SP = `session-sprint-rules.md` §3.1、priority = CP-5 ミッション貢献度（`docs/project-mission.md` の KPI 直結度）。

### Step G-3: 重複・陳腐化の処理（自律実行）

| 判定 | アクション |
|------|-----------|
| **明白な重複**（同一監査コード・酷似タイトルで内容も同一） | 新しい/情報量の少ない方を `mcp__github__issue_write`（state=closed・`duplicate_of` 指定）でクローズし、残す側に集約コメント |
| **陳腐化**（既に実装済み・前提が消滅・他 Issue で解決済み） | 確認のうえクローズ（理由をコメント）。**仕様・実装状況は必ず grep / コード確認してから** 判断する |
| **判断が割れる**（重複に見えて切り口が違う等） | クローズせず、両 Issue に相互リンクコメントのみ |

> 自律クローズの境界: 内容の同一性に確信が持てる場合のみ。少しでも切り口が異なれば残す（消し過ぎより取りこぼしを許容）。

### Step G-4: Epic 統合（自律実行）

`epic_candidates`（同一カテゴリ集中）について追跡 Epic を自動生成する。

- タイトル `[Epic] {カテゴリ}: 改善バックログ統合追跡（{N}件・YYYY-MM-DD）` で `mcp__github__issue_write`（create）
- 本文に子 Issue 一覧（`- [ ] #NNNN タイトル`）と統合の意図を記載。`mcp__github__sub_issue_write` で紐付けられる場合は紐付ける
- **子 Issue はクローズせず残す**（Epic は実装単位でなく追跡単位。実装は消化モードが子 Issue 単位で進める）
- Epic 自体に `type:improvement` ＋（@owner 判断で）`priority:` / `sp:` を付与
- 新規 **マイルストーン** の追加は A-5（要ユーザー確認）。Epic 化では代用しない

### Step G-5: 棚卸しサマリーの記録 + 最優先の受け渡し

1. 結果（処理件数・クローズ数・新規 Epic・優先度補完数）を **作業 Issue にコメント記録**（チャットでの逐次報告は不要・サイレント運用）
2. **最優先 1 件**（priority:high かつ即実装可能）を消化モードへ渡す（そのまま実装するか、`status:waiting-claude` のまま次スロットに委譲）
3. レポートをリポジトリに残す場合は `content/analytics/grooming/YYYY-MM-DD.md`（プロジェクトの保管規約に合わせる）に置き、ファイル変更があれば PR 化

### Step G-6: リファインメント 4 出口判定（Step G-1.5 で `T` が非空のときのみ）

> **目的**: 対象 Issue を「取り組む / やめる / 束ねる / 保留」の **いずれかに必ず遷移させる**。
> 既存 Step G-3（明白な重複・陳腐化のクローズ）・G-4（Epic 統合）の判断基準は **変更しない**。
> 本 Step は確信度の異なる新しい判断軸（CP-5 貢献度）を並走させ、実行機構（Epic 化・クローズ）だけを再利用する。

#### 判定順（前段が該当したら後段は評価しない。すべて NO なら「保留」）

```
Q1 束ねる : 同カテゴリの他候補、または既存 [Epic] が存在するか
            （カテゴリは対象 Issue のタイトル・本文のキーワードクラスタで簡易判定する。
             triage_improvements.py のカテゴリ分類器は type:improvement 前提のため使わない）
Q2 やめる : Step G-3 の「陳腐化」基準に該当する（判定手順もそのまま適用・grep / コード確認必須）
            OR CP-5 貢献度が「低」
Q3 取り組む: CP-5 貢献度が「中」以上 AND 着手コストが明確（sp:1〜3、または sp:5 でも設計が明確）
Q4 保留   : 上記いずれにも至らない（貢献度は中だが着手コストが不明瞭・前提調査が必要 等）
```

#### CP-5 貢献度の判定（縮退プロキシ付き）

```
IF docs/project-mission.md の KPI 表が実値で埋まっている:
    どの KPI に直結するか明示できる → 高 ／ 間接的（品質・自動化に資する）→ 中 ／ 接続できない → 低
ELSE（{{...}} 雛形のまま＝bootstrap 直後のプロジェクト）:
    core-principles.md CP-5 の汎用優先順位を代理指標にする
    （① KPI 直結 ②品質向上 ③自動化・効率化 ④ドキュメント・運用改善）
    ①② に該当する根拠を示せる → 中〜高 ／ ③ のみ → 中 ／ ④ のみで実利を示せない → 低
```

#### 精査コストのゲート（件数ではなく「個々の Issue の曖昧さ」で決める）

| 判定の性質 | 実行経路 |
|-----------|---------|
| `T` が空 | no-op（Step G-1.5 で終了） |
| Q1（束ねる）・Q2 の陳腐化枝など **明白なケース** | 単独判断（既存 G-3 / G-4 と同じ確信度） |
| Q2 の CP-5「低」枝・Q3 の境界など **グレーゾーン** | `Skill(discussion-review)` で専門チーム精査（対象 1 件でも起動してよい） |

対象件数を発火下限にしない（件数は精査コストの代理指標にならない。1 件でもグレーなら精査価値があり、
複数件でも全て明白なら単独判断で足りる）。

#### 各出口の実行アクションと権限

| 出口 | ラベル操作 | 実行アクション | 権限 |
|------|-----------|---------------|------|
| **取り組む** | `status:blocked` を外し `status:waiting-claude` を付与（`status:in-progress` にはしない＝実装は次回の消化スロットへ委譲） | **R3-1 由来の対象は `priority:` の再査定を @owner（PO）に必ず依頼する**（`priority:low` のままでは消化スロットの選択順が変わらず、ラベル付け替えだけでは着手されないため）。sp:8 は分割余地も併せて諮る | メイン（`status:`）/ @owner（`priority:` `sp:` のみ・`session-sprint-rules.md` §4） |
| **やめる** | なし | `mcp__github__issue_write(state="closed", state_reason="not_planned")`。根拠は「陳腐化」または「CP-5 貢献度が低い」 | メイン（Issue クローズは A-1〜A-6 に該当しない＝ユーザー確認不要） |
| **束ねる** | 変更しない（子はそのまま） | Step G-4 の Epic 機構を再利用（既存 Epic があれば `mcp__github__sub_issue_write` で紐付け、なければ新規作成）。**子はクローズしない** | メイン |
| **保留** | 変更しない | コメントのみ（次回精査日を明記） | メイン |

#### 記録フォーマット（機械検証の前提・省略禁止）

対象 Issue すべてに次のプレフィックス付きコメントを 1 件残す（`mcp__github__add_issue_comment`）:

```
[refinement] 決定: 取り組む（理由 1 文）
[refinement] 決定: やめる（理由 1 文。再オープンで復帰できる旨を添える）
[refinement] 決定: 束ねる(#Epic番号)（理由 1 文）
[refinement] 決定: 保留(次回精査日: YYYY-MM-DD JST)（理由 1 文）
```

「やめる」のコメントには **再オープン導線** を必ず書く（誤クローズの最後の防衛線）:
「必要と判明したら Issue を再オープンしてください（`state_reason=not_planned` は優先度上の見送りを意味し、否定ではありません）」。

#### 同じ判定を繰り返して滞留させないためのサーキットブレーカー

- **保留**: 同一 Issue に `[refinement] 決定: 保留` が **3 回連続** で付いていたら、4 回目は保留を選べない
  （Q2 か Q3 のどちらかを強制する）。定期ルーティンの blocked 再評価「未解消 3 回で打ち切り」と同型。
- **取り組む**: 同一 Issue が `[refinement] 決定: 取り組む` を **2 回連続** 受けてもなお着手されていない
  （＝再び対象集合 `T` に現れた）場合、ラベル付け替えでは解決していない。この回は
  **@owner に priority 引き上げか Issue 分割（sp 縮小）を諮り、その結果をコメントに記録する**。
  それでも 3 回目に再登場したら Q2（やめる）を評価する（着手されない Issue を「取り組む」認定のまま
  無限に持ち越さない）。

#### 完了検証（実行の最後に必ず行う）

`T` の各 Issue について、`[refinement] 決定:` プレフィックスの最新コメントが次のいずれかに一致することを確認する。

```
^\[refinement\] 決定: 取り組む
^\[refinement\] 決定: やめる
^\[refinement\] 決定: 束ねる\(#\d+\)
^\[refinement\] 決定: 保留\(次回精査日: \d{4}-\d{2}-\d{2}
```

一致しない・存在しない Issue が残ったら「未処理 d 件（Issue 番号）」として報告する（R-1 手順 8 の
「不一致 d 件」と同じ言い回し）。この検証は state ファイルを持たずコメント文字列だけで再計算できる。

### 整理モードの禁止パターン

```
❌ レポートを見ずに勘でクローズ・Epic 化する（必ず triage_improvements.py のデータに基づく）
❌ 切り口が少しでも違う Issue を「重複」として消す（取りこぼし優先・残す）
❌ priority/sp を @owner を通さずメインが恣意的に決める（PO は @owner）
❌ 子 Issue を Epic 化と同時にクローズする
❌ status:* ラベルを @owner に操作させる（PO 権限境界 §4.1 違反）
❌ Step G-6 の 4 出口判定を、対象件数が少ないことを理由にスキップする（1 件でも実行する）
❌ Step G-6 で「やめる」を選びながら `[refinement] 決定:` コメント・再オープン導線を省く
❌ Step G-1.5 の対象抽出に `triage_improvements.py`（type:improvement 固定）をそのまま使う（#335 と同型の取りこぼしになる）
```

---

## 消化モード実行フロー（日次・retro-try-handler と連携）

> **トリガー起動時の上書き**: スケジュールトリガー（ルーティン）から起動された場合、起動プロンプト
> 側（下流プロジェクトの運用メモが定義する実行手順等）が指定する **対象スコープ・件数上限・同 priority 内のタイブレーク順**
> は本フローの既定値（status:waiting-claude フィルタ・5 件/回・下記タイブレーク）に **優先** する。
> それ以外（priority ラベルの大小順・サーキットブレーカー等）は本フローに従う。
> タイブレークを上書き対象に含めるのは、1 回 1 件のルーティンでは「小さく確実に減らす」順序が
> 在庫削減に直結し、日次 5 件消化を前提とする本フローの既定と最適解が異なるため（#335）。

```
1. 対象Issue取得:
   クラウド一次経路（repo スコープの gh は 403・L-114）:
     mcp__github__list_issues(owner, repo, state="OPEN", labels=["status:waiting-claude"], perPage=100)
   ローカル環境（gh CLI 到達可能時）の代替:
     gh issue list -R kai-kou/btc-realtime-chart --state open --limit 1000 \
       --label "status:waiting-claude" --json number,title,labels,milestone,updatedAt
   → type:improvement / type:bug でフィルタし、priority ラベル順（high → medium → なし → low）でソート
     （同 priority 内の順序は下記タイブレークのみで決める。「古い順」は単独では適用しない）
   → **監査バックログ（プロジェクト定義のマイルストーン例: 「監査 P0〜P3」）**: 監査 Issue は
     priority ラベル（P0/P1=high・P2=medium・P3=low）とマイルストーンを起票時に付与済みのため、
     本ソートにそのまま乗る。**同 priority 内のタイブレークは以下の順序で一意に適用する**:
     1. 監査マイルストーン付き Issue を優先し、マイルストーン昇順（P0→P1→P2→P3）でソート
     2. プロジェクト定義の重み付け（例: lessons 高頻度指摘から算出した領域別ウェイト）があれば参照し、
        ウェイトが高い Issue 種別を優先
        （ユーザーが繰り返し重視してきた領域を自律的に先取りするための重み付け）
     3. 上記が同等の場合は作成日時の古い順を適用する

2. 上位 5件（priority:high → priority:medium → priority なし → priority:low → 各群内で上記タイブレーク）を選び、各々:
   - status:in-progress 付与
   - 実装コスト評価 → 小〜中なら実装 → PR → マージ
   - 大規模・曖昧なら設計をIssueコメントに記録し priority 据え置き（着手は次サイクル）
   - ※ in/out 均衡のため上限は 5件/回（refinement の生成ペース 3〜5件/日に対し消化 5件/日で均衡）。
     コスト上限内（時間/トークン）で完走できる範囲に留め、未完分は次サイクルへ持ち越す

3. 放置検出:
   - type:improvement / retro-try で7日以上 updatedAt が古いものを検出
   - サーキットブレーカー: 3サイクル試行しても進まないものは status:blocked + 理由コメント
   - **在庫が 30 件を超えたら整理モードを先に実行する**（「多すぎて選べない」状態の解消）

4. 報告: 消化件数・残数・放置検出結果（**在庫増加率 vs 消化率**を明記）:
   - 当日 created（status:waiting-claude 付与）件数 = in flow
   - 当日 closed もしくは status:waiting-claude 解除 件数 = out flow
   - out < in が3日続く場合は CP-3 衛生アラートとして発見モードにエスカレーション
```

---

## サーキットブレーカー / 安全装置

- 1課題あたり修正サイクル2回超で STOP → Issueコメントに状況記録（session-safety-rules.md）
- 発見モードのコスト上限意識: N 観点並列は重いため発見スロットを限定（プロジェクト定義の頻度）
- 整理モードの自律クローズは「明白な重複・陳腐化」に限定する（判断が割れるものは残す）
- main直接push禁止・PR経由のみ（CP-6 既約境界外リスト遵守）

## トリガー

| モード | 実行タイミング |
|--------|-------------|
| 発見（完全版） | プロジェクト定義の発見スロット / `/self-improvement-loop` / 「横断レビューして」 |
| 整理（棚卸し） | `type:improvement` が 30 件超滞留した時 / 月次の棚卸しスロット / `/self-improvement-loop --groom` / 「改善Issueを棚卸しして」「改善バックログを整理して」「Epic化して」 |
| 整理（**リファインメント**・Step G-1.5 → G-6） | **週次ゲート**（プロジェクト定義の定期ルーティン・state は `config/backlog_refinement_state.json`）/ 「リファインメントして」「後回しになっている Issue を精査して」。件数閾値では発火しない（低優先スタベーションは在庫が少なくても起こるため） <!-- refcheck:ignore --> |
| 消化（軽量版） | 日次の消化スロット / `/self-improvement-loop --consume` / 「改善Issue消化して」 |

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `docs/rules/improvement-lane-map.md` | レーン境界の SSOT（振り返り・監査/衛生レーンとの分担） |
| `tools/triage_improvements.py` | 整理モードの集計・分類・重複検出・Epic 候補抽出（副作用なし・`type:improvement` 限定のため Step G-1.5 では使わない） |
| `config/backlog_refinement_state.json` | リファインメント週次ゲートの state（`last_refinement_at`）。定期ルーティン側が着手前に更新して先勝ち排他する。**配布対象外** で、無ければ初回実行時に自動生成される（#448） <!-- refcheck:ignore --> |
| `.claude/skills/discussion-review/SKILL.md` | Step G-6 のグレーゾーン精査（専門チーム議論）の実行手段 |
| `docs/rules/session-sprint-rules.md` | SP 基準（§3）・PO=@owner（§4） |
| `.claude/agents/owner.md` | PO ロール定義（`priority:` / `sp:` ホワイトリスト） |
