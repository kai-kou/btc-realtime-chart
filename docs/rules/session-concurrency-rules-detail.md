# マルチセッション競合防止ルール — 詳細版（Warm 層）

> **本ファイルは `session-concurrency-rules.md`（Hot・原則+レイヤー要約）の詳細補完版**。
> Hot 層予算の棚卸し（Issue #146）で全文の背景・レイヤー別プロセス・コード例を本ファイルへ移設した
> （サマリー＝ポインタ規約）。判断ロジックの正本は引き続き Hot 版。

Claude Code クラウド環境（Scheduled Tasks）で複数セッションが並行実行する際の競合を防止するルール。

## 背景

Claude Code Scheduled Tasks は **同時実行制御機能を持たない**。毎時タスクが前セッション完了前に新セッションを起動する場合があり、同一対象に対して重複処理が発生する。

### 発生事例（対象 X の処理 — 2 つの PR の二重作成）

2 つの並行セッションが同時に discover スクリプト（プロジェクト定義）を実行し、どちらも `PENDING:対象 X` を返した結果、同一対象の処理が二重に実行され、2 つの PR が同時に作成された。

### 根本原因: TOCTOU レースコンディション

1. **セッション A**: discover スクリプト → 「対象 X は PENDING」→ 処理開始
2. **セッション B**: discover スクリプト → 「対象 X は PENDING」→ 処理開始
3. 両セッションとも PR 作成 → 一方の PR がマージ → もう一方の PR がコンフリクト

チェック時点（Time of Check）と使用時点（Time of Use）の間にタイムラグがあるため、オープン PR チェックだけでは防げない。

## 防止策（多層防御）

### レイヤー 1: discover スクリプトの排他チェック（検出時）

discover スクリプト（プロジェクト定義）は対象の検出時に以下の排他チェックを実装する。

| チェック | 内容 |
|---------|------|
| オープン PR | 同一対象のオープン PR が存在しないか |
| `status:in-progress` ラベル | 対象 Issue が別セッションにロックされていないか |
| 他ブランチコミット | 同一対象の作業ブランチに既にコミットがないか（必要な工程のみ） |

### レイヤー 2: Issue ラベルによる論理ロック（処理開始時）

各パイプラインスキルは処理開始時に **即座に** Issue の `status:waiting-claude` → `status:in-progress` にラベルを変更する。

```
パイプライン開始
  → Step 0: Issue ラベルを status:in-progress に変更（最優先で実行）
  → Step 1〜: 実際の処理
```

**重要**: ラベル変更は処理の **最初のアクション** として実行する。git fetch や辞書チェック等の前に行う。

### レイヤー 3: PR 作成前の再チェック（PR 作成時）

PR 作成コマンド実行の **直前** に、同一対象 ID のオープン PR が存在しないか再チェックする。

```bash
# PR 作成前チェック（各パイプラインスキルの PR 作成ステップに組み込み）
# {ID}: 処理対象の一意識別子（例: PR タイトル先頭の [V012] 等のエンティティ ID）
# {keyword}: 工程・パイプラインを識別する文字列（例: audio / script 等）
# 両者はプロジェクトの PR タイトル命名規約に合わせて置換する。
# クラウド（一次経路・L-114）: mcp__github__list_pull_requests(state="open") の応答タイトルを
#   client-side で {ID}/{keyword} フィルタする。以下の gh はローカル用。
existing_pr=$(gh pr list \
  -R kai-kou/tsukuru-studio \
  --state open \
  --json title,number \
  --limit 50 \
  --jq '.[] | select(.title | test("\\[{ID}\\].*{keyword}:")) | .number')

if [ -n "$existing_pr" ]; then
  echo "[SKIP] PR #${existing_pr} がすでに open のため、PR 作成をスキップ"
  exit 0
fi
```

### レイヤー 4: GitHub のマージコンフリクト（最終防衛線）

同一ファイルを変更する 2 つの PR は GitHub がコンフリクトを検出する。これは意図的な防衛線ではないが、最悪のケース（同一ファイルの二重マージ）を防ぐ。

### レイヤー 5: PR アクティビティロック（レビューフェーズの占有・Issue #3007）

Issue の `status:in-progress` 論理ロック（レイヤー 2）は **PR レビューフェーズには効かない**。全 hourly セッションが共通プリフライト（`check_pending_pr_reviews.py`）で全オープン PR を見るため、別セッションが作成・対応中の PR に催促・指摘対応・問題なし判定・マージ・subscribe で介入してしまう事故が起きていた（別セッションが連続実行中のブランチに重複参入し、処理まで二重実行されるケース）。

**防止策（アクティビティベースの占有判定）**: `check_pending_pr_reviews.py` が各 PR の人間側最終アクティビティ（PR 作成・head ブランチへのコミット・非ボットコメント）を `last_activity_min` として算出し、**直近 10 分以内に活動がある PR を `active_session: true` として `--actionable-only` から除外** する。

- 出力に現れない PR ＝別セッションが現役対応中 → **介入禁止**（`--include-active` での強制取得も禁止）
- 活動が 10 分以上途絶えた PR ＝孤児 → 従来どおり救済（CP-3 維持・救済遅延は最大 ~10 分）
- 作成セッション自身のハートビート監視（`--json` + PR 番号フィルタ）は `status` を直接参照するため影響なし
- （過去 PR 互換）外部レビュアー催促コメントが直近 10 分以内に存在する PR の `needs_prompt` 抑制ガードは残るが、現行フローでは外部レビュアー催促（`/gemini review` 等）自体を **行わない**（廃止済み・SSOT: `docs/rules/ai-reviewer-strategy.md`）

### レイヤー 6: アイデンティティベース所有判定（積極的所有・#47）

レイヤー 5 は **時間ベースの代理指標** であり、2 つの穴がある:

1. **自 PR でも 10 分超アイドルで奪われる**: 自セッションが他作業に集中して 10 分以上 PR を放置すると `active_session` 窓が切れ、他セッションが介入対象とみなす。
2. **セッション再起動・圧縮後に自 PR を見失う**: 会話メモリが消えると「どの PR が自分のものか」を識別できず、自 PR の責任継続（マージまで）が不能になる。

**防止策（決定論的な所有判定）**: PR 本文の `Session-Id: {UUID}` トレーラー（`session-sprint-rules.md` §2 で必須化・メトリクス集計と **所有判定の二重用途**）を所有権の権威ソースとして使う。`check_pending_pr_reviews.py` が各 PR の `owner_session_id` を解析し、`$CLAUDE_CODE_SESSION_ID` と一致するかを `is_mine` で返す。

```bash
# 自セッションが作成した PR のみを取得（他セッションの PR は出力されない）
python3 tools/check_pending_pr_reviews.py --mine --json
# 自 PR で「要対応」のものだけ（マージまで責任を持つスコープ）
python3 tools/check_pending_pr_reviews.py --mine --actionable-only --json
```

- `--mine` は `owner_session_id == $CLAUDE_CODE_SESSION_ID` の PR **のみ** を返す（積極的所有）。
- **自 PR は `active_session`（時間ベース）除外を適用しない**: 所有者本人なので 10 分超アイドルでもセッション再起動・圧縮後でも自 PR を見失わず責任継続できる（穴 1・2 を同時に塞ぐ）。
- `$CLAUDE_CODE_SESSION_ID` 未設定時は `--session-id <id>` を明示（未指定なら exit 2 で安全側に倒す＝全 PR を自 PR 扱いしない）。
- Session-Id トレーラー不在の PR（記載漏れ）は `owner_session_id` が空になり `--mine` に現れない → **その場合は従来のレイヤー 5（時間ベース）が保護を継続**（多層防御の縮退）。

**二面モデル（本ルールの要）**:

| スコープ | 仕組み | 目的 |
|---------|--------|------|
| **自スコープ（積極的所有）** | レイヤー 6・`--mine`（Session-Id 一致） | 自セッション作成 PR **のみ** を責任持ってマージまで進める |
| **他保護（消極的回避）** | レイヤー 5・`--actionable-only`（active_session 時間窓） | 他セッションの現役 PR・トレーラー欠落 PR に触れない安全網 |

> **PR 作成時の必須事項**: `--mine` が機能する前提として、PR 本文に `Session-Id: $CLAUDE_CODE_SESSION_ID` を必ず記載する（`session-sprint-rules.md` §2・`self_review_check.py` が記載漏れを Warning で検知）。記載漏れ時は時間ベース（レイヤー 5）にフォールバックするが、並行セッション時の所有特定精度が落ちる。

## パイプラインスキルへの適用

各パイプラインスキル（プロジェクト定義）は以下のパターンを組み込む。

```
Step 0 開始時:
  1. Issue ラベルを status:in-progress に変更
  2. discover スクリプト --fetch で対象確認

PR 作成ステップの直前:
  1. mcp__github__list_pull_requests（クラウド一次経路・L-114。ローカルは gh pr list）で同一対象のオープン PR を再チェック
  2. 存在する場合 → PR 作成スキップ＋ユーザー報告
```

全パイプラインスキルが共通して Step 0 でラベル変更、PR 作成直前で再チェックを行う。

禁止事項・既知の制限は Hot 版（`session-concurrency-rules.md`）が正本。本ファイルでは重複掲載しない。
