# workflow-health-check 詳細リファレンス

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


> `SKILL.md` は日次の軽量版（Step 1〜2）を中心に構成している。本ファイルは
> **完全版限定の Step 3〜6・週次レポート雛形・実行コマンド例** を保持する
> （完全版として起動された時のみ Read する）。

## Step 3: パイプライン整合性監査（完全版のみ）

```
3-a: ブランチ孤立検出
  └─ 制作ブランチ（例: content/{ID}-*・プロジェクト定義）が存在するが、対応する Issue / PR がない場合 → Slack 警告
  └─ claude/* ブランチが 7 日以上前のコミットで止まっている → Slack 警告

3-b: 成果物整合性チェック（フェーズ構成はプロジェクト定義）
  └─ 先行フェーズ完了（PR マージ済み）なのに後続フェーズ Issue がない → 後続フェーズ Issue を自動作成
  └─ 並行フェーズが両方完了なのに次フェーズ Issue がない → 自動作成

3-c: 重複 PR 検出
  └─ 同一エンティティ ID + 同一フェーズ（例: [{ID}] {フェーズ名}）のオープン PR が複数存在 → Slack 通知
  └─ 古い PR（先に作成された方）に「重複の可能性」コメント + status:blocked 付与
```

## Step 4: 根本原因記録・再発防止（完全版のみ）

```
4-a: 問題パターン集計
  └─ 今週検出した問題を種別・頻度・影響範囲でサマリー化

4-b: retro-try Issue 自動生成
  └─ 同じ問題が 2 週連続で検出された場合 → type:retro-try Issue を作成（assignee:claude）
  └─ 例: 「ベースブランチ不整合が継続的に発生。PR 作成前チェックを強化する」

4-c: セルフレビュー学習ログ更新
  └─ P-XX / I-XX パターンに該当する新問題を docs/rules/self-review-checklist.md に追記提案
  └─ ルールファイルへの反映は commit + PR で行う（CLAUDE.md 直接変更は禁止）

4-d: 週次レポート生成
  └─ 下記「週次レポートフォーマット」の形式で Slack 通知（完全版のみ）

4-e: Layer 1 計測の週次集計（#627 対策 E・GitHub API 不要）
  └─ `python3 tools/layer1_findings_report.py --weeks 4` を実行し、指摘ゼロ PR 率・CONFIRMED / PR・観点別・
     PR 前レビューの修正数・「同種指摘 2 回以上」候補を週次レポートの「Layer 1 計測」表に転記する
  └─ 同種指摘候補（2 つ以上の PR で同カテゴリ）→ docs/rules/self-review-checklist.md に行を追加し、
     機械化可能なら tools/self_review_check.py にチェックを追加（同一 PR で・L-094）。
     その場で着手しない候補は type:improvement Issue に候補一覧と根拠（PR・path:line）を記録する
  └─ 指摘ゼロ PR 率が目標 60% 未満（Issue #627 §4）のまま 4 週連続 → 原因 1 行（増えている観点 / カテゴリ）を
     週次レポートに記載し、対策 A〜D のどこを直すかを type:improvement Issue にする
  └─ JSONL が 4 週間更新されていない → code-review Step 3-C の記録が抜けている（Warning・スキルの desync を疑う）
```

## Step 5: フィードバックループ健全性チェック（完全版のみ）

retro-try Issue の消化率・重複状況・パイプラインカバレッジを自動監査し、フィードバックループ自体を改善する。

```
5-a: retro-try Issue 消化率チェック
  └─ `type:retro-try` ラベルの Issue を全件取得（open + closed）。タイトルが `[Retro][ledger]` で始まる
     候補台帳 Issue は集計対象から除外する（台帳は「実装対象」ではなく振り返りレーンの中間状態のため）
  └─ 消化率（closed / total）を算出
  └─ 消化率 50% 未満 → Warning（Slack 通知 + 改善提案）
  └─ 非 blocker のオープン件数（`urgency:blocker` を除く）が WIP 上限（`docs/rules/retrospective-rules.md`
     「WIP 制御」・#662 の PULL 転換で μ_base × W_target に再定義）を **超えた**（`>`）→ Warning（バックログ肥大化。
     PULL 型では上限ちょうどまで埋まるのが正常な定常状態なので、`≥` にすると健全運用でも常時鳴る）

5-b: 重複 Issue 自動検出・統合
  └─ `type:retro-try` のオープン Issue を全件取得。台帳 Issue（`[Retro][ledger]` 接頭辞）は除外する
  └─ タイトルからキーワードを抽出し、同一テーマの Issue グループを特定
     判定基準: 同じツール名・フィールド名（プロジェクト定義）、同じファイルパス、または同じ問題パターン
  └─ 3 件以上の同テーマ Issue が存在 → メイン Issue にコメント追記 + 残りを duplicate クローズ
  └─ 1 回の実行で統合するグループは最大 3 グループまで（サーキットブレーカー）

5-c: パイプライン別カバレッジチェック
  └─ 各パイプライン（プロジェクト定義）の retro-try Issue 件数を集計
  └─ 過去 7 日間にパイプライン PR がマージされたのにレトロスペクティブ Issue が 0 件 → Warning
     例: あるパイプラインの PR がマージされたが [Retro][{pipeline}] Issue が存在しない → レトロスペクティブ未実行の可能性
  └─ Warning の場合は Issue コメントまたは Slack 通知で報告

5-b2: waiting-user 重複 Issue 検出（完全版のみ）
  └─ status:waiting-user のオープン Issue を全件取得（バックログ分類〔例: ネタ候補・phase:1-*〕は除外）
  └─ タイトルの [{ID}] + フェーズキーワードで正規表現マッチし、同一エンティティ ID + 同一フェーズの Issue グループを検出
     例: 「[{ID}] {フェーズ名}: ...」が 2 件以上存在 → 重複候補
  └─ 検出した場合は Slack 通知のみ（自動クローズは禁止。ユーザー判断に委ねる）
     通知例: 「⚠️ waiting-user 重複 Issue を検出しました: {ID} {フェーズ名} が 2 件 → #{N1}, #{N2}」
  └─ 1 回の実行で通知するグループは最大 5 グループまで（サーキットブレーカー）

5-d: WIP ゲート適合性チェック（report-only・アクチュエータなし・#563 → #662 で PULL 転換に追随）
  └─ `type:retro-try` のオープン Issue を取得し（台帳 Issue を除外）、`urgency:blocker` を除いた
     非 blocker オープン件数 N と「直近 7 日以内に created_at がある件数」M を数える
  └─ N > WIP 上限（retrospective の資格判定ゲート・SSOT は docs/rules/retrospective-rules.md「WIP 制御」）
     かつ M ≥ 1
     → Warning: 「資格判定ゲート（retrospective Step 3）が機能していない疑い。在庫 N 件が WIP 上限を
       超えたまま新規 Issue が週 M 件生成されている」
     （非 blocker の昇格は「< WIP 上限」でゲートされるため、正常系では N は上限ちょうどまでしか達しない。
      上限を超えた状態で非 blocker の新規が出るのはゲート素通りの兆候。`≥` にすると正常な満杯状態で誤警報になる）
  └─ #662 の PULL 転換により Try の既定は台帳記録で、Issue 化は資格判定（blocker 即時 ∨ 同一キー 30 日
     以内 2 回以上 ∧ 空きあり）を通過した昇格分に限られる。新規 Issue が発生すること自体は正常系だが、
     それが WIP 上限に張り付いた状態と同時に起きるのはゲートが素通りしている兆候
  └─ 生成側（retrospective）の内部状態は参照しない（レーンをまたぐ暗黙状態共有を避け、GitHub 上の Issue 集合だけから独立に検査する）
  └─ 旧「生成/消化ペース比較」は廃止（ゲート本体は retrospective 側にあり、頻度調整は下流のプロジェクト定義で決まるため
     レポートしても実行者がいなかった）。TTL 出口（retro-try-handler Step 1.5）の作動状況は 5-a の消化率に反映される

5-e: 候補台帳の存在・重複・沈黙検出（完全版のみ・#397 → #662 で PULL 転換に追随）
  └─ `type:retro-try` かつタイトルが `[Retro][ledger]` で始まる OPEN Issue（候補台帳）を取得する
  └─ 0 件 → Warning: 「候補台帳が存在しない（未移行 or 誤クローズ）。retrospective Step 3-0 の台帳作成を確認」
  └─ 2 件以上 → Warning: 「候補台帳の重複: #N1, #N2（自動統合しない・report-only）」
  └─ 1 件（正常系）→ 沈黙検出に進む: `type:retro-try`（台帳を除く）の open + closed 全件を取得し最新の
     created_at を求め、**かつ** 台帳 Issue 自体の updated_at を確認する。両方が 30 日超のときだけ
     Warning: 「振り返りレーンが N 日間 1 件も Try を観測していない。retrospective の起動経路を確認」
     （PULL 転換後は Issue 新規ゼロが正常系なので `created_at` 単独では判定しない。台帳の
     `updated_at` も 30 日超のときだけ「レトロ自体が動いていない」と確定できる）
  └─ 5-a（消化率）・5-c（パイプライン別カバレッジ）では検出できない状態を拾うための独立条件:
     5-a は closed/total の比率を見るため「全件 closed で新規ゼロ」は 100% と評価されて発火しない。
     5-c は「過去 7 日にパイプライン PR がマージされたのに retro Issue 0 件」というパイプライン単位・
     7 日窓の条件のため、総数のグローバルな沈黙は対象外（#394 の議論で実測確認）
  └─ report-only（Issue の自動生成・統合・クローズはしない。retrospective を代行実行もしない）

5-f: スケジュールルーティン生存確認（heartbeat・完全版のみ・#397）
  └─ 前提: プロジェクトがスケジュールルーティンを使っている場合のみ実行する。ルーティンの
     構成記録ファイル（トリガー名・cron・用途を記録したもの。プロジェクト側に置く）が無ければスキップする
  └─ mcp__Claude_Code_Remote__list_triggers を実行し、構成記録の各ルーティンをトリガー名で突き合わせる
  └─ Warning 条件（いずれか 1 つでも該当したら報告）:
     ・next_run_at が現在時刻を過ぎている
     ・last_fired_at からの経過が「cron 間隔 × 2 + 起動ジッター上限（既定 30 分）」を超える
       （1 回分の遅延では発火させず false positive を避ける。ジッター分を上乗せするのは、
        早発→遅発が連続すると純粋な 2 倍閾値を正常系でも超えうるため）
     ・enabled が false、または enabled フィールドが欠落している
     ・suspension_reason が空でない
  └─ 構成記録にあるのに list_triggers に該当エントリが無い → Warning（トリガー未作成）
     🔴 ただし UI 作成（created_via: "http_api"）のルーティンは recurring / include_completed / limit を
     どう指定しても list_triggers に返らないため、この条件から除外する（2026-09-22 実測・tsukuru-studio の
     R-1 が該当・#99 / docs/routines.md §1）。除外すると当該ルーティンは生存確認そのものが成立しないので、
     「本チェックの対象外」として報告し、Run list の目視（下記の既知の限界と同じ動線）へ回す
  └─ 🔴 自動再有効化・自動再作成は禁止（report-only）。ended_reason と suspension_reason が
     ともに空の停止は list_triggers の仕様上「user-paused」＝ユーザーが意図的に止めた状態を含み、
     機械的に障害と区別できないため（#394 の実測で R-1 がこの状態だった）
  └─ 報告は Slack 通知 + 対象 Issue へのコメント。ユーザー操作が必要と判断した場合のみ
     user-notification-triage.md に従い A-6 として @mention する
  └─ 既知の限界: 本チェック自体もルーティン経由で実行されるため、全ルーティンが同時に停止した
     状態は自己検知できない（監視の監視は積まない・#397 で合意）。その場合は
     [Run list](https://claude.ai/code/routines) の目視が唯一の経路
```

## Step 6: CLAUDE.md / 常駐ルール肥大化監査（完全版のみ・P-7）

公式ベストプラクティス「CLAUDE.md が長すぎると Claude が半分無視する」（[best-practices](https://code.claude.com/docs/en/best-practices)）に基づき、CLAUDE.md と常駐ルール（`.claude/rules/` symlink 群）のトークン肥大化を監査する。**report-only（自動編集しない）** — 削除候補を提示するのみで、CLAUDE.md の編集はユーザー判断または別 Issue で実施する（rule-loading 構造変更のリスク回避）。

```
6-0: /doctor による棚卸し（Claude Code 公式の診断コマンド・#327）
  └─ 対話セッションなら /doctor を実行する（スキル・CLAUDE.md のサイズ適正化を含むフルチェックアップ）
  └─ 出力を 6-a〜6-d の判断材料の1つとして扱う（後述の注意を必ず適用する）

6-a: CLAUDE.md（プロジェクトの主要指示ファイル）サイズチェック
  └─ wc -c -l CLAUDE.md を取得
  └─ 600 行超 → Warning（「肥大化。プルーニング or @import 分割を検討」）

6-b: プルーニング候補の提示（公式判定基準）
  └─ 各セクションについて「この行を消したら Claude がミスするか？」を自問
  └─ NO（= Claude が既に正しくやっている / 自明 / フックで強制済み）の行 → 削除 or hook 昇格の候補としてレポート
  └─ 重複記述（同一ルールが CLAUDE.md と docs/rules/*.md の両方に冗長定義）→ SSOT 化候補としてレポート
  └─ 本体が既に持っている情報（本体システムプロンプトの汎用規範・本体が description 付きで
     自動列挙するスキル一覧など）→ 削除候補としてレポート（#326 の判定軸）

6-c: 常駐ルール合算サイズチェック
  └─ cat .claude/rules/*.md | wc -c で symlink 先の実体合算を集計
  └─ `python3 tools/check_hot_budget.py` で基準・実測・再棚卸しの合図を機械判定する
     （基準値の正本は token-optimization-rules.md の増減ログ。**ここに数値を書き写さない** — 再校正のたびに desync する・#648）
  └─ 予算超過 → Warning（Warm 降格 or 既存ファイルの追加圧縮を提案）
  └─ 降格提案には必ず「代替の強制レイヤ（ハーネス / スキル / ツール）が実在するか」の確認を添える

6-d: @import 分割の提案（report-only・自動実行しない）
  └─ CLAUDE.md 内で「大きく・低頻度更新・独立性が高い」セクション（例: プロジェクト定義のドメイン詳細節）を検出
  └─ `@docs/...md` インクルードでの分割候補としてレポート（実装はユーザー承認後に別 PR。圧縮時保持挙動・symlink 運用への影響評価が必要なため自動適用しない）
```

> **`/doctor` の実測仕様（2026-07-26・Claude Code v2.1.220 で確認）**:
> - **CLI 版 `claude doctor`** が返すのは **インストール健全性のみ**（native/npm の併存・パス破損・自動更新チャネル等）。
>   スキルや CLAUDE.md のサイズ適正化は **含まれない**。`claude doctor --help` の実出力（逐語）:
>   `Check the health of your Claude Code installation. Reads settings files in the current directory without a trust prompt. For a full checkup that can also fix issues, run /doctor in a session.`
> - **セッション内 `/doctor`** が設定・スキル・CLAUDE.md を含むフルチェックアップと修正を担当する。
>   公式ドキュメント（[memory](https://code.claude.com/docs/en/memory)）によれば、**checked-in の CLAUDE.md に対して
>   trim を提案する**（v2.1.206 以降）: *コードベースから導出できる内容（ディレクトリ構成・依存リスト・
>   アーキテクチャ概要）を削り、落とし穴・根拠・ツール既定と異なる規約を残す*。これは 6-b の判定軸と同方向。
>   自律セッション（非対話）では起動できないことがあるため、その場合は 6-a〜6-d の機械チェックで代替し、
>   「`/doctor` 未実行」をレポートに明記する（実行していないものを実行したと書かない・L-113）
>
> **出力の扱い**: `/doctor` は汎用ツールであり、**本リポジトリの Hot 層は運用規律（Issue ロック・通知トリアージ・
> 完了報告構造）が主** で、汎用的な「削れる」判定と本プロジェクトの必要性判定は一致しないことがある。
> 判断材料の 1 つとして扱い、削除の可否は 6-b / 6-c の判定軸（代替の強制レイヤが実在するか）で決める。

> **安全方針（P-7）**: 本ステップは **監査・提案のみ**。CLAUDE.md の `@import` 分割や行削除は rule-loading 構造・セッション圧縮時保持挙動に影響するため、health-check では自動実行せず、検出結果を週次レポートに記載してユーザー/別 Issue の判断に委ねる。

> **検討メモ（#164）**: Step 6 は「ワークフロー健全性」というスキル名から見ると責務越境気味（CLAUDE.md 肥大化監査は別領域）。将来 workflow-health-check のさらなる分割・統合を検討する際は、本ステップを独立スキルへ切り出す案も候補に入れること。

## 週次レポートフォーマット

```
## 🔍 ワークフロー健全性チェック週次レポート

### 今週の検出サマリー
| カテゴリ | 検出件数 | 自動修正 | 要確認 |
|---------|---------|---------|--------|
| PR 健全性 | N件 | N件 | N件 |
| Issue 状態 | N件 | N件 | N件 |
| パイプライン | N件 | N件 | N件 |

### 自動修正した問題
- ✅ スタック Issue {N}件 → ラベルリセット
- ✅ ラベル不整合 {N}件 → 修正済み

### 要確認事項（ユーザーアクション必要）
- ⚠️ {問題の概要}: #{Issue番号 or PR番号}

### 検出された繰り返しパターン
- {パターン}: {今週N回目} → {対応中 or retro-try Issue #N に記録}

### Layer 1 計測（4-e・`python3 tools/layer1_findings_report.py --weeks 4` の出力を転記）
| 指標 | 値 | 判定 |
|------|-----|------|
| 指摘ゼロ PR 率（直近週 / 4 週平均） | {N}% / {M}% | OK / Warning（60% 未満が 4 週連続で Warning） |
| CONFIRMED / PR 中央値（PR 後ラウンド 1） | {N} | OK / Warning（3 超で Warning） |
| PR 前レビュー（件 / PR 前に修正） | {N} / {M} | — |
| 観点別 CONFIRMED（上位 3・直近週 / 前週） | {観点} {N} / {M}、{観点} {N} / {M}、{観点} {N} / {M} | OK / Warning（同じ観点が 2 週連続で増加） |
| 同種指摘候補（カテゴリ一致 2 PR 以上・粗い束ねは 3 PR 以上） | {N} 件 | 反映済み / Issue #N |

### フィードバックループ健全性（Step 5）
| 指標 | 値 | 判定 |
|------|-----|------|
| retro-try 消化率 | {closed}/{total} ({N}%) | OK / Warning |
| オープン件数（非blocker・台帳除く） | {N}件 | OK / Warning（WIP上限〔SSOT〕超過で Warning） |
| 重複統合 | {N}グループ統合 | — |
| パイプラインカバレッジ | 各パイプライン:{N}（プロジェクト定義） | OK / Warning（0件で Warning） |
| WIP ゲート適合性 | 非blockerオープン{N}件 / 直近7日新規{M}件 | OK / Warning（N>WIP上限〔SSOT〕かつM≥1 で Warning・5-d） |
| 候補台帳の状態 / 最新生成からの経過 | 台帳{N}件 / 最新生成{M}日・台帳更新{K}日 | OK / Warning（台帳0件or2件以上、または両方30日超で Warning・5-e） |
| ルーティン生存（heartbeat） | {ルーティン名}: 最終発火 {N}時間前 | OK / Warning（cron 間隔の2倍超・停止・未作成で Warning・5-f） |

### 次週への改善アクション
- {type:retro-try Issue があれば一覧}
- {フィードバックループの改善提案があれば記載}
```

> 自動修正の安全範囲（全ステップ共通）は `SKILL.md` に定義する（軽量版の Step 1〜2 にも適用される
> 共通ルールのため、軽量版でも読み込まれる本文側に一本化・重複させない）。

## Claude Code による実行手順・実行コマンド例

### 手動実行（完全版）

```bash
# workflow-health-check を手動で実行（全6ステップ）
# 「/workflow-health-check」または「ワークフロー健全性チェックして」で起動
```

### 軽量版（project-sync から呼び出し）

```bash
# project-sync の Step 0 として以下を実行（Step 1〜2 のみ）
# PR 健全性 + Issue 状態監査
```

### 実行コマンド例

MCP（クラウド・一次経路。repo スコープの `gh` はクラウドで 403・L-114。SSOT: `docs/rules/github-mcp-fallback-patterns.md`）:
```
# PR 健全性確認（ベースブランチ・マージ可能性）
mcp__github__list_pull_requests(owner, repo, state="open")

# スタック Issue 確認（in-progress かつ 4h 超）
mcp__github__list_issues(owner, repo, state="OPEN", labels=["status:in-progress"])

# ラベル不整合確認（status: が 2 つ以上）
mcp__github__list_issues(owner, repo, state="OPEN")
  → 応答の labels 配列を client-side で判定（status: 開始のラベルが 2 つ以上の Issue を抽出）

# ワークフロー実行状況の確認（gh run / workflow list 相当）
mcp__github__actions_list(method="list_workflow_runs", owner, repo)
mcp__github__actions_list(method="list_workflows", owner, repo)
```

ローカル環境（gh CLI 到達可能時）の代替:
```bash
# PR 健全性確認（ベースブランチ・マージ可能性）
gh pr list -R kai-kou/btc-realtime-chart \
  --state open \
  --json number,title,baseRefName,mergeable,isDraft,updatedAt \
  --limit 100 \
  --jq '.[] | {number, title, base: .baseRefName, mergeable, isDraft, updatedAt}'

# スタック Issue 確認（in-progress かつ 4h 超）
gh issue list -R kai-kou/btc-realtime-chart \
  --label "status:in-progress" \
  --state open \
  --json number,title,updatedAt \
  --limit 100

# ラベル不整合確認（status: が 2 つ以上）
gh issue list -R kai-kou/btc-realtime-chart \
  --state open \
  --json number,title,labels \
  --limit 200 \
  --jq '[.[] | select([.labels[].name | select(startswith("status:"))] | length > 1)] | .[] | {number, title, status_labels: [.labels[].name | select(startswith("status:"))]}'
```
