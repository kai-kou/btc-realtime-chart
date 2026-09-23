# PRレビューフロー

> **🔴 外部 AI レビュアーへの依頼は廃止（飼い主決定・本タスク）**: **Copilot へのレビュー依頼（`mcp__github__request_copilot_review` / `gh pr edit --add-reviewer @copilot`）は行わない。** Gemini も 2026-07-17 で廃止済み。レビューは **Claude 自身が観点別フレッシュ文脈レビューで必ず実行するセルフレビュー（Layer 1）** を主軸とし、外部レビュアーの応答を待たずに Layer 0（機械ゲート）+ Layer 1 通過で自動マージする。外部レビュアーへの「依頼」「催促」「再依頼」「25 分タイムアウト待ち」の手順は **本ドキュメントから削除済み**（適用先リポジトリのエージェントが手順節に倒れて依頼してしまう再発を防ぐため）。Copilot / Gemini bot のレビュー検出ロジックは過去 PR 互換のため一部ツールに残るが、依頼・催促・応答待ちはしない（SSOT: `docs/rules/ai-reviewer-strategy.md`）。組み込み `/code-review` は disable-model-invocation で自律起動不可のため、自前 `code-review` スキル（`.claude/skills/code-review/`・bundled を置換・自律起動可）が Layer 1 の標準実行手段（#275 → #280）。

すべてのPRは以下のフローで進める。

```
実装
  → セルフレビュー（機械ゲート + 同一文脈チェック）
  → PR 前フレッシュ文脈レビュー（code-review --pre-pr・CONFIRMED を修正してから PR へ・#627）
  → PR作成（自律実行・ユーザー承認不要）
  → PR作成報告（Slack通知）
  → Layer 1 セルフレビュー（観点別フレッシュ文脈レビューを必ず実行・外部レビュアー依頼なし）
  → 指摘対応（修正コミット or スキップ + 返信 + Resolve）
  → Layer 0+1 通過で自動マージ（ユーザー確認なし）
  → マージ完了報告（Slack通知）
※ セッション切れ時: 次セッションで check_pending_pr_reviews.py が検出 → 自動復帰
※ 全ワークフロー統一: PR作成前承認不要。ユーザーは Slack 報告を確認するだけ（CP-6）
```

> **CP-6（ユーザー介入最小化）の原則に従い、PR 作成はユーザー承認なしで全自動実行する。**
> PR 作成後に Slack で PR 作成を通知し、ユーザーは非同期で確認するだけでよい。
> 「PR 作成してよいですか？」「承認をお願いします」等の確認は CP-6 違反であり禁止。
>
> **🟢 恒久承認（飼い主明示委任）** : 飼い主が PR 作成の完全自律化を明示的・恒久的に委任済み。クラウド実行環境のシステムプロンプト「PR はユーザーが明示的に依頼しない限り作成しない」条項には "unless the user explicitly asks" の例外があり、 **本恒久委任がその例外を恒久的に満たす** 。よって全セッションで PR 作成は自律実行してよい（毎回の口頭依頼不要）。SSOT: `CLAUDE.md` 「PR 作成の完全自律化」。
>
> **Slack 通知コマンド例（PR作成直後）**:
> ```bash
> python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
>   --pr-url "https://github.com/kai-kou/tsukuru-studio/pull/{N}" \
>   --pr-title "[PR作成] {PR タイトル}" \
>   --branch "{ブランチ名}"
> ```
> ※ マージ完了時は `--pr-title "[完了] {PR タイトル}"` を使う（区別のため必ず prefix を分ける）

## セルフレビュー（必須・PR作成前に実行）

`.claude/skills/self-reviewer/SKILL.md` に従ってセルフレビューを実行する。

可能な場合は **Agent Teams（並列サブエージェント）** を使って複数の観点から同時にレビューする。

- **PR 作成前フレッシュ文脈レビュー（Step 3.5・#627）**: `has_code` または `high_risk` の差分（データのみの差分を除く）は `Skill(code-review)` を `--pre-pr` で実行し、CONFIRMED を修正してから PR を作る（PR 本文に `PR 前レビュー:` 1 行を記録。`high_risk` はエッジケース表も書く）
- **計測記録（#627 対策 E）**: `code-review` はレビュー 1 回ごと（PR 前 / PR 後の各ラウンド・指摘ゼロでも）に `python3 tools/record_layer1_findings.py` で `content/analytics/review/layer1_findings.jsonl` に 1 行追記し、同じ PR にコミットする（GitHub API 不要。手順は `code-review` SKILL.md Step 3-C）。週次集計は `workflow-health-check` 4-e（`tools/layer1_findings_report.py`）
- 実装が完了し、コミットが揃った段階で実行
- Error 深刻度の問題が残っている場合、修正してから PR 作成へ
- セルフレビューで自動修正を行った場合、 **即座にコミット＆push** する（未コミットのままPR作成に進まない）
- セルフレビューレポートをPR説明文に含める

## PR作成前の未コミットチェック（必須・自動ゲート）

PR作成コマンド（ `gh pr create` ）の実行前に、以下を確認する。 **PreToolUse フックで物理的にブロックされる** ため、未コミットファイルがあるとPR作成は実行できない。

```
確認項目:
- [ ] 未ステージの変更がないこと（git diff --quiet）
- [ ] ステージ済み未コミットの変更がないこと（git diff --cached --quiet）
- [ ] 未追跡ファイルがないこと（git ls-files --others --exclude-standard が空）
- [ ] 未pushのコミットがないこと
- [ ] 自分のタスク範囲外のファイルが差分に含まれていないこと
      確認コマンド（全ケース共通）:
        git diff --name-only origin/main...HEAD  # 変更ファイル一覧を目視確認
      補助確認（meta.yaml のみ確認したい場合）:
        git diff --name-only origin/main...HEAD -- 'content/meta/*.yaml'
      ⚠️ 他動画の content/meta/*.yaml が含まれている場合は必ず確認すること（L-032 対策）
```

ブロックされた場合の対応:
1. `git status` で未コミットファイルを確認
2. 必要なファイルを `git add` → `git commit` → `git push`
3. 不要なファイルは `.gitignore` に追加するか削除
4. 再度 `gh pr create` を実行

## PR 説明文テンプレート（AIレビュアー指摘削減）

PR 説明文に以下のテンプレートを使用することで、Gemini / Copilot が意図的な設計を「バグ」として繰り返し指摘することを防ぐ（L-001対策完了）。

```markdown
## 変更内容の概要

- {実装した機能や修正内容の要約（箇条書き）}

## セルフレビュー結果

- セルフレビュー: 実施済み（エラー: 0件 / 警告: N件）
- PR 前レビュー: 検出 N 件（🔴a 🟡b ⚪c・CONFIRMED d / PLAUSIBLE e）→ 修正 f 件・見送り g 件（対象外なら「スキップ（理由）」・#627）
- YAML/JSON 構文: エラーなし
- fact_check_flags: N件（ランクA: N / ランクB: N / ランクC: N）

## テスト・確認内容

- [ ] {実行したコマンドと結果（検証証跡。例: `python3 tools/x.py --self-test` → PASS。フックが欠落を Warning する・#627）}

## エッジケース表（high_risk 差分のみ・#627）

<!-- hooks / settings / permissions / パーサ等の変更で必須。入力 × 状態 → 期待挙動 を表にする。該当しなければ削除 -->
| 入力 | 状態 | 期待挙動 | 検証 |
|------|------|---------|------|

## 設計意図・既知の警告（AIレビュアー向け）

<!-- 意図的な設計変更がある場合のみ記載。不要なら削除してよい -->
<!-- 記載例:
- BGM ギャップ（0.3秒）: VOICEVOX との自然な繋ぎのための意図的なパディング
- LFS ポインター: クラウド環境では LFS バイナリ取得不可。フォールバックロジック対応済み
- speedScale = 1.1: 1分≒400文字の実測値に基づく調整済みパラメーター
- [wip] コミット: セッション保護の自動コミット。PR 全体の diff で評価してください
- completed_steps リセット: 別パイプライン開始時の意図的な動作（L-037対策）
-->

## AIレビュー結果（自動マージ時のみ記載）

<!-- AIレビュー完了後のみ記載。複数レビュアーの結果を透明性高く記録する -->
<!-- 記載例:
| レビュアー | 状態 | 指摘件数 | 対応 |
|-----------|------|---------|------|
| Gemini | ✅ 完了 | 2件 | 全対応済み |
| Copilot | ⏰ タイムアウト | — | 問題なし判定 |
-->

Closes #{Issue番号}
```

### 「設計意図・既知の警告」セクションを含めるべきケース

| 変更内容 | 理由 |
|---------|------|
| LFS ポインターが含まれる PR | フォールバックロジックが組み込み済みと分かる |
| `completed_steps: []` リセット | 意図的な動作（L-037）と分かる |
| `[wip]` コミットを含む PR | セッション保護用と分かる |
| `--limit 1000`（gh CLI） | Issue 数が 200 件超のため必要と分かる |
| プロジェクト固有の成果物 PR（データファイル・生成物） | 下記「プロジェクト固有定型文」を整備して貼り付け |

### プロジェクト固有定型文の整備（推奨）

AI レビュアーが繰り返す誤指摘（意図的な設計を「バグ」と指摘するパターン）は、対象ファイル種別ごとの
**定型文** を用意して PR の「設計意図・既知の警告」セクションに貼り付けると一括で抑止できる。

書き方の型（プロジェクトごとに実値を整備する）:

```markdown
## 設計意図・既知の警告（AIレビュアー向け）

### {成果物種別} 共通の意図的な設計（指摘不要）
- `{フィールド/値}` が {一見不正に見える状態} の場合: {実は正しい理由}。{機械検証がある場合は「◯◯ (Lv4 CI) が自動検出するため AI レビュー側での重複指摘は不要」}
- `{フィールド}` の `null` / 省略: {意図的な許容値である理由}。ただし {真にバグとなる条件} は指摘対象
```

> **効果**: このセクションがある PR では AI レビュアーが同じ指摘を繰り返す確率が大幅に低下する。
> 頻出誤指摘はリポジトリレベルの指示ファイル（例: `.github/copilot-instructions.md`）にも反映すると、
> 将来的にセクション自体が不要になる可能性がある。

## レビュー（Layer 1 セルフレビュー・外部 AI レビュアー依頼なし）

PR 作成時に **Claude 自身が観点別フレッシュ文脈レビューで必ずセルフレビューを実行する**（Layer 1・全 PR 必須）。**Copilot・Gemini への外部レビュー依頼は行わない。**

> **🔴 `/code-review` は自前スキルで置き換え済み（2026-07-21・#275 → #280）**: 組み込み `code-review` スキルは
> `disable-model-invocation` により Claude の Skill ツール経由の自律起動が禁止されたため、
> **project スコープの同名スキル `.claude/skills/code-review/`（自前実装・`disable-model-invocation` なし）で
> bundled を置換** した（公式仕様: 同名 project スキルは bundled スキルをオーバーライドする）。
> これにより対話（`/code-review` 手打ち）・自律セッション（`Skill(code-review)`）の両方から起動できる。
> Layer 1 の標準実行手段は自前 `code-review` スキルとする（SSOT: `ai-reviewer-strategy.md`）。

| レビュー | 方法 | 性質 |
|---------|------|------|
| **Layer 1 観点別フレッシュ文脈セルフレビュー（主軸）** | **自前 `code-review` スキルを `Skill(code-review)` で実行**（観点別ファインダー並列 → 敵対的検証 → 報告。修正適用 = `--fix` 相当も同スキル）。対話セッションの `/code-review` 手打ちも同じ自前スキルに解決される | 同一セッション内で完結。差分を「第三者の PR」として読み直し自己修正盲点を回避（CCR）。外部往復ゼロ |
| Layer 2 敵対的議論（条件付き） | `tools/discussion_review_trigger.py --pr {N}`（diff ≥300行 / `type:security` / `type:breaking-change` / `high_risk` 差分・#627） | 同セッション内・追加課金なし |
| Layer 3（任意・高リスクのみ） | `anthropics/claude-code-security-review` Action / `/ultrareview` | 非ブロッキング。Copilot/Gemini は使わない |

- セルフレビューで検出した指摘は、修正コミット or スキップ理由の記録で解消してから自動マージする。
- 外部レビュアーの応答待ちが存在しないため、Layer 0+1 通過後は即マージしてよい（25 分タイムアウト待機は廃止）。

### Layer 1 の記録先は PR の行単位インラインコメント（必須・#461）

**Layer 1 で検出した指摘のうち CONFIRMED を PR の行単位インラインコメントとして投稿する（CRITICAL / WARNING は全件、NIT は
`REVIEW.md` の上限 3 件まで）。PLAUSIBLE と上限超の NIT はレビュー本文（サマリー）に `file:line` 付きで列挙する**
（#461 → #627 で一部改訂・飼い主承認 2026-09-11）。後から PR を振り返ったときに「どの行にどんな指摘があり、どう決着したか」を
読み取れるようにするための必須要件であり、チャット報告で代替しない。

| 論点 | 規定 |
|------|------|
| 投稿対象 | CONFIRMED のみインライン（NIT は上限 3 件）。PLAUSIBLE / 上限超 NIT は本文列挙で記録し、返信・Resolve は不要（#627） |
| 投稿タイミング | PR 作成直後の Layer 1 実行時、および修正コミット後の再レビュー時（2 回目以降は新規 CRITICAL / WARNING と前回指摘の修正漏れのみ・`REVIEW.md` 収束ルール） |
| 指摘ゼロ件のとき | `event="COMMENT"` のレビューを 1 件だけ投稿し「CONFIRMED 0 件（観点 n 系統実施 → 敵対的検証）」を明記する（投稿しないと「レビュー未実施」と区別できない）。追加の issue コメントは打たない |
| レビューイベント | 常に `COMMENT`（`APPROVE` / `REQUEST_CHANGES` を使わない理由・API 制約は `code-review/SKILL.md` Step 3-A が正本） |
| 行がハンク外・ファイル削除 | `subjectType="FILE"` にフォールバックし、本文に元の `file:line` を明記して指摘を握りつぶさない |
| 投稿失敗 | 失敗分をまとめて `add_issue_comment` に 1 回で記録する（`update_pull_request` は全文置換のため使わない）。サイレント放棄は禁止 |
| 対応結論 | **同一スレッドへの返信 + Resolve**。新規コメントに分離しない（指摘と結論の紐付けが切れる） |

手順・本文テンプレート・フォールバックの実装 SSOT は `.claude/skills/code-review/SKILL.md` Step 3-A。
この記録はすべて PR 内で完結し、ユーザーには報告しない（L-102 サイレント原則と整合する）。

## レビュー自動監視（必須・ユーザー指示不要で自動実行）

**PR作成・レビュー依頼後、ユーザーへの確認を挟まず自動でレビュー監視を開始すること。「監視を開始してよいですか？」と聞いてはならない。**

PR作成フローの一部として、以下を連続実行する:

```
1. PR作成
2. 【必須・L-050】PR作成直後の存在確認（L-050 対策）
   クラウド（一次経路・L-114）: mcp__github__list_pull_requests(owner, repo, head="{owner}:{ブランチ}", state="open")
   ローカル: gh pr list --head {現在のブランチ名} -R kai-kou/tsukuru-studio \
     --limit 1 --json number,url,state \
     --jq '.[0] | select(.url != null) | "PR #\(.number) \(.state): \(.url)"'
   # → URL が返ってきた場合のみ「PR作成しました PR#N URL: https://...」と報告する
   # → エラー・空応答の場合は「PR作成に失敗した」と報告し、原因を特定して再試行する
3. Slack で PR 作成完了を通知（「承認」ではなく「報告」）
   python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
     --pr-url "https://github.com/kai-kou/tsukuru-studio/pull/{N}" \
     --pr-title "[PR作成] {PR タイトル}" \
     --branch "{ブランチ名}"
4. Layer 1 セルフレビューを実行（外部 AI レビュアー依頼なし）
   - **自前 `code-review` スキルを `Skill(code-review)` で必ず実行**
     （`.claude/skills/code-review/` が組み込みを置換・自律起動可・#280）
   - **CONFIRMED は PR の行単位インラインコメントで記録し、PLAUSIBLE と上限超 NIT は本文に集約する（#627）**（指摘ゼロでもレビュー 1 件を投稿・#461）
   - ❌ Copilot 依頼（`--add-reviewer @copilot` / `request_copilot_review`）はしない
   - ❌ Gemini 依頼（`/gemini review`）もしない（2026-07-17 廃止済み）
   - 条件付きで Layer 2（`discussion_review_trigger.py`）を起動
5. （任意）subscribe_pr_activity + ハートビートで CI / 人手コメントを監視（詳細は下記）
```

> **PR作成通知とマージ完了通知の区別**:
> - PR作成時: `--pr-title "[PR作成] {タイトル}"` → ユーザーへの情報通知（アクション不要）
> - マージ完了時: `--pr-title "[完了] {タイトル}"` → タスク完了の報告

### 監視方式: subscribe_pr_activity + ハートビート（推奨・L-052）

`mcp__github__subscribe_pr_activity` MCP ツールでPRのイベントを購読し、同時に `pr_review_heartbeat.sh` をバックグラウンドで起動して `Monitor` ツールでストリームする。クラウドセッションは **10 分無活動でタイムアウト** するため、ハートビートが 5 分ごとに stdout を出力してセッションを維持する。

```
# ① PRイベント購読を開始
mcp__github__subscribe_pr_activity(owner="kai-kou", repo="tsukuru-studio", pull_number={pr_number})

# ② ハートビートをバックグラウンドで起動（セッション維持・5分間隔で出力）
Bash(run_in_background=true): bash tools/pr_review_heartbeat.sh {pr_number} 30
→ 返ってきた PID を控えておく（例: PID=12345）

# ③ Monitor でハートビート出力をストリーム（各行が通知としてセッションを維持）
Monitor(pid={HEARTBEAT_PID}, description="PR #{pr_number} ハートビート（セッション維持）")
```

**ハートビートの動作**:
- 5 分ごとに `check_pending_pr_reviews.py` で PR 状態を確認し stdout に出力
- Monitor ツールが各行を通知としてトリガー → アイドルタイムがリセットされてタイムアウト防止
- `ready_to_merge` を検出したら 🚀 メッセージを出力（Claude が監視して即マージ）
- PR がマージ済みになったら ✅ メッセージを出力して自動終了（`max_minutes` 経過で ⏰）

**sleep ポーリングからの移行理由**: `tools/poll_pr_reviews.sh` は最大 25 分間 sleep ループを回す設計だが、クラウド環境のセッションタイムアウト（10 分）と競合し、レビュー対応が宙に浮く事例が頻発していた。subscribe_pr_activity はイベント駆動のため sleep が不要だが、10 分以上イベントがないとタイムアウトする。ハートビートを組み合わせることで両方の問題を解決する。

### フォールバック: poll_pr_reviews.sh

`subscribe_pr_activity` が利用できない環境では `tools/poll_pr_reviews.sh` を **1つのバックグラウンドタスク** として **CI / 人手コメントの監視** に使う（外部 AI レビュアーの応答待ちには使わない）。

現行のマージ判定（外部レビュアー非依存・SSOT: `docs/rules/ai-reviewer-strategy.md`）:

| 状況 | 状態 | 対応 |
|------|------|------|
| PR 作成直後（作成セッションが実行中） | `awaiting_review` | 自 PR は自分でセルフレビュー実行 → マージ |
| アイドル化した自/孤児 PR・未解決スレッドなし | `needs_prompt` | **観点別フレッシュ文脈セルフレビューを実行** → 指摘解消 → 即マージ |
| 未解決スレッドあり（CI 失敗・人手コメント等） | `needs_response` | 指摘対応（修正 or スキップ + 返信 + Resolve）→ マージ |

> 外部 AI レビュアー（Copilot/Gemini）への依頼・催促・再依頼は **廃止済み**（飼い主決定）。`check_pending_pr_reviews.py` は外部応答の 25 分タイムアウトを待たず、Layer 0（機械ゲート）+ Layer 1（観点別フレッシュ文脈セルフレビュー）通過で即マージする。催促・再依頼の手順は **存在しない**（過去の Gemini クォータ / Copilot エラー / 両者未応答フォールバック手順は廃止に伴い削除済み）。

### マージ品質チェックリスト（セルフレビュー完了後・Issue #1434 由来）

Layer 0（機械ゲート）+ Layer 1（観点別フレッシュ文脈セルフレビュー）通過後、自動マージ前に以下を全て満たすことを確認する（外部レビュアーの応答は待たない）:

- [ ] CI 全 check が PASS している（CI を運用する派生プロジェクトのみ。本ベースは Lv4 CI 現時点不採用・#298・`harness-escalation.md`）
- [ ] セルフレビューで Error ゼロ
- [ ] `fact_check_flags` が 5 件未満かつ rank C ゼロ（該当パイプラインのみ）
- [ ] 字幕改行エラーゼロ（該当パイプラインのみ）
- [ ] 方言ルール違反なし（post-tool-use-validate.sh 通過済み・該当パイプラインのみ）

**1つでも未達の場合はマージを停止し、原因を特定して対応する（自己解決不可かつ A-1〜A-6 該当時のみユーザー報告）。**

詳細は `.claude/skills/pr-review-watcher/SKILL.md` を参照。

## セッション復帰フロー（クラウド環境のタイムアウト対策）

クラウド環境（Claude.ai Scheduled Tasks）ではセッションが頻繁にタイムアウトする。
PR作成後のレビュー監視中にセッションが切れた場合、次セッションで自動復帰する。

### 根本原因

- `poll_pr_reviews.sh` が最大25分の sleep ポーリングを行う設計だった
- クラウドセッションのタイムアウト（10〜30分）と競合
- セッション切れ時に `/tmp/` の状態ファイルが消失し、復帰手段がなかった

### 対策: check_pending_pr_reviews.py

セッション開始時やスケジューラーの project-sync 実行時に、レビュー待ちPRを自動検出する。

```bash
# ① 自セッション作成 PR を最優先で回収（積極的所有・#47。再起動・圧縮後も Session-Id で識別）
python3 tools/check_pending_pr_reviews.py --mine --actionable-only --json
# ② 共有スコープで孤児 PR を救済（他保護＝時間窓フィルタ）
python3 tools/check_pending_pr_reviews.py --actionable-only --json
```

**出力ステータス**:

| status | 意味 | 対応 |
|--------|------|------|
| `needs_prompt` | Layer 1 セルフレビュー要実施（アイドル化した自/孤児 PR・未解決なし） | **観点別フレッシュ文脈セルフレビューを実行** → 指摘解消 → 即マージ（外部催促はしない） |
| `needs_response` | 未解決スレッドあり（CI 失敗・人手コメント等） | 指摘対応から再開（修正 or スキップ + 返信 + Resolve） |
| `awaiting_review` | PR 作成直後（作成セッションがセルフレビュー実行中） | 自 PR は自分で実行 → マージ。他 PR は待機 |
| `no_action` | Claude 以外 / 手動 PR | スキップ |

### アクティブセッション除外（CP-4・Issue #3007・L-109）

`check_pending_pr_reviews.py` は各 PR の **人間側最終アクティビティ**（PR 作成・head ブランチへのコミット・非ボットコメント）からの経過分を `last_activity_min` として算出し、**直近 10 分以内に活動がある PR は `active_session: true` として `--actionable-only` から除外** する。

- **意味**: その PR は別の現役セッションが作成・対応中。**自分が作成した PR 以外で `active_session: true` のものに介入（催促・指摘対応・問題なし判定・マージ・subscribe）してはならない**
- **CP-3 との両立**: 活動が 10 分途絶えた PR は従来どおり救済対象になる（救済遅延は最大 ~10 分）
- **作成セッション自身**: 自分の PR は観点別フレッシュ文脈セルフレビュー実行 → 指摘解消後に自分でマージする（外部レビュアー応答待ちなし）。ハートビート（`--json` + PR 番号フィルタ）は CI / 人手コメントの任意監視に使う
- **アイデンティティベース所有判定（#47）**: `--mine` を付けると PR 本文の `Session-Id` トレーラーが `$CLAUDE_CODE_SESSION_ID` と一致する PR **のみ** を返す。自 PR は `active_session` 除外を受けないため、**10 分超アイドル・セッション再起動・圧縮後でも確実に自 PR を回収して責任継続できる**（時間ベースの穴を埋める積極的所有。二面モデルの詳細は `session-concurrency-rules.md` レイヤー 6）。復帰時は `--mine` を先、共有スコープを後に実行する
- `--include-active` はデバッグ用途（`active_session: true` の PR も出力に含める）のみ。出力に含まれた場合でも介入（指摘対応・マージ）は引き続き禁止（L-109）

### 復帰フロー

```
全 hourly スロット開始時（プリフライト）/ project-sync 実行時:
  ↓
check_pending_pr_reviews.py --actionable-only --json
  ↓
（active_session=true の PR は出力されない＝別セッション対応中・介入しない）
  ↓
needs_prompt（Layer 1 セルフレビュー要実施・未解決なし）
  └─ 観点別フレッシュ文脈セルフレビューを実行 → 指摘解消 → 即マージ（外部催促なし）

awaiting_review（PR 作成直後・作成セッションが実行中）
  └─ 自 PR は自分でセルフレビュー実行 → マージ。他 PR は待機

needs_response（未解決スレッドあり・CI 失敗 / 人手コメント等）
  └─ 指摘対応（Step 3 から）→ マージ

no_action
  └─ スキップ
```

### スケジューラーへの組み込み

**全 hourly スロット**（06:00〜19:00）の開始時プリフライトとして `check_pending_pr_reviews.py` を呼び出す（RC-2 対策）。
07:00・11:00 の project-sync だけでなく全スロットで実行することで、セッション切れによる PR 放置を **最大 1 時間以内** に検知・解消する。

> **背景（PR #748 の再発防止）**: 14:55 に画像パイプラインが PR を作成 → Copilot 25 分待機中にセッション切れ → 次の project-sync（11:00）まで最大 20 時間放置されるリスクがあった。`{プロジェクト定義: hourly-routing 相当}` の「共通プリフライト」セクションに実装詳細を記載。

## レビュー対応サイレント原則（最重要・L-102）

**AIレビュー（Gemini / Copilot / Claude セルフレビュー）の指摘対応は「サイレント」で行う。ユーザーには報告しない。**

ユーザーはレビューコメント対応の経過に関心がなく、報告するとマージ後のアウトカムにノイズが混入する。レビュー対応の記録は **PR スレッド返信・Resolve・Issue コメント** で完結させ、以下をしない:

- ❌ チャットへの逐次報告（「Gemini の指摘 3 件に対応したにゃ」「Copilot レビュー解消したにゃ」等の各ラウンド narration）
- ❌ Slack `@mention` （レビュー対応は要対応イベントではない・ `user-notification-triage.md` の FYI 扱い）
- ❌ マージ後の完了報告アウトカム（ `--outcome` ・チャット）にレビュー対応プロセスを含めること

**完了報告のアウトカムは「初回指示に対して何ができるようになったか」だけを書く** （レビュー往復・指摘件数・修正サイクル数は書かない）。レビュー対応の詳細記録が必要なものは PR スレッドと Issue コメント（実行履歴サマリー）に残るため、ユーザー向け出力には不要。

**例外（ユーザーへ報告してよい / すべき）**:

| 事象 | 理由 |
|------|------|
| サーキットブレーカー発動（修正サイクル 2 回超） | A-4 既約境界外。無限ループ防止のため判断を仰ぐ |
| ファクトチェック致命的 NG | A-3 既約境界外。誤情報の公開リスク |
| 上記以外で A-1〜A-6 に該当する事象 | `user-confirmation-minimization.md` §1 参照 |

> `<github-webhook-activity>` イベント駆動でレビュー対応する場合も同様。グローバル指示の「Reply only if this resolves the task or raises a question — do not narrate each round of fixes」と整合する。PR diff が対応の記録であり、毎ラウンドの報告は不要。

### `<github-webhook-activity>` 入力とチャット出力の区別（運用上の混同防止・#61）

運用中にチャット履歴へ現れる「レビュー関連の表示」は **2 種類** あり、性質がまったく異なる。混同して「ノイズが必ず出る」と誤解しないこと。

| 種類 | 実体 | 抑制可否 |
|------|------|---------|
| `<github-webhook-activity>` ブロック | **ハーネスが配信する入力メッセージ**（`subscribe_pr_activity` が PR イベント＝レビュー / CI / コメントを会話に届ける「作業キュー」）。assistant の出力ではない | **購読中は必ず履歴に現れる**（イベント配信路そのもの。抑制しようとしない。不要になったら `unsubscribe_pr_activity` する） |
| レビュー対応の逐次ナレーション（「Layer N が X を検出 → 修整する」等） | **assistant のチャット出力** | **出さない**（L-102。記録は PR スレッド / Issue のみ） |

- **記録の SSOT は PR スレッド返信・Resolve・Issue コメントであって、チャットではない**。チャットは揮発し圧縮で失われる（`session-compression-rules.md`）。レビュー指摘・対応・検証結果は必ず PR 側に残す。
- 自律 / バックグラウンド運用ではユーザーはチャットを常時見ていない前提（Human-on-the-loop）。webhook ブロックが履歴に積まれても、ユーザーが見るのは PR と完了報告のみ。
- **例外**: 「動作確認・デモをユーザーが明示依頼」した場合のみ、説明のためレビュー対応を可視化してよい（ルールの逸脱ではなく、デモという別タスク）。通常運用には持ち込まない。

## レビュー対応ルール

1. AIレビュアーの指摘は内容を精査し、妥当なものは追加コミットで対応する
2. 対応不要と判断した指摘はPRコメントで理由を記載してスキップする
3. **指摘コメントへの返信（必須）**: 対応完了・スキップいずれの場合も、該当コメントスレッドに返信を投稿し、会話をResolve済みにする
   - 対応した場合: `「対応しました。{修正概要}（{commit_sha}）」`
   - スキップした場合: `「スキップします。理由: {理由}」`
   - 返信: クラウドは `mcp__github__add_reply_to_pull_request_comment(owner, repo, pullNumber, commentId, body)`（L-114）/ ローカルは `gh api repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies --method POST -f body="..."`
   - インラインコメントのthread resolve: クラウドは `mcp__github__pull_request_review_write(method="resolve_thread", threadId=...)`（threadId は `pull_request_read(method="get_review_comments")` で取得）/ ローカルは GraphQL `resolveReviewThread` mutation（後述）。Step 1 で取得したインラインコメントの `id` と `reviewThreads` の `comments.nodes[0].id` を照合して対応する `thread_id` を特定します。
4. 指摘対応後、内容に応じて再レビューを依頼する（軽微な修正は省略可）
5. すべてのAIレビュー指摘を解消（対応 or スキップ判断）した後、 **ユーザー確認なしで自動マージする**

### AIレビュー指摘をスキップする場合の必須記録

**製品名・人名・API仕様等の修正提案をスキップする場合は、必ず公式ドキュメント等で根拠を確認し、PR コメントに記録する。**

スキップ理由の記録テンプレート:

```
スキップします。理由: 正式名称は「{既存表記}」です。
参考: {公式ドキュメント URL}（確認日: YYYY-MM-DD）
```

**例**: 
```
スキップします。理由: 正式名称は「Nemotron 3 Super」です（model ID: nemotron-3-super-128b）。
参考: https://huggingface.co/nvidia/Nemotron-3-Super-128B-Instruct（確認日: 2026-04-28）
```

**期待効果**: 
- スキップ理由が PR コメントに永続記録され、後続セッションから追跡可能になる
- AIレビュアーの誤指摘（製品名の誤った修正）を Gemini に正しくフィードバックできる

## 自動マージ（ユーザー承認不要）

AIレビュー指摘を全て解消したら、ユーザー確認なしで自動マージする。

```bash
# GitHub MCP ツールでマージ
mcp__github__merge_pull_request(owner="kai-kou", repo="tsukuru-studio", pull_number=N, merge_method="squash")
```

マージ後の完了報告（アウトカムを必ず含める）:
```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
  --pr-url "https://github.com/kai-kou/tsukuru-studio/pull/{N}" \
  --pr-title "[完了] {PR タイトル}" \
  --outcome "{ユーザー視点のアウトカム1文}" \
  --branch "{ブランチ名}"
```

> **完了報告テンプレート・アウトカムの書き方（良い例/悪い例）・URL 記法は `completion-report-rules.md`（唯一の SSOT）に従う**。本ファイルでは再掲しない。要点のみ: `--outcome` には「ユーザーが最初に依頼した指示に対して何ができるようになったか」を書き、マージ手順・レビュー往復・指摘件数は書かない（複数 PR を含むタスクは全マージ後に「タスク全体のアウトカム」を1回報告する）。

> **例外**: サーキットブレーカー（後述）が発動した場合はユーザーに報告して判断を仰ぐ。

### コメントスレッドへの返信・Resolve のAPIコマンド

> クラウド一次経路（L-114）: 返信は `mcp__github__add_reply_to_pull_request_comment`、
> Resolve は `mcp__github__pull_request_review_write(method="resolve_thread", threadId=...)`、
> threadId 取得は `mcp__github__pull_request_read(method="get_review_comments")`。
> 以下の gh コマンドはローカル実行用。

```bash
# インラインレビューコメントへの返信
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies \
  --method POST \
  -f body="対応しました。{修正概要}（{commit_sha}）"

# インラインコメントのスレッドをResolve済みにする（thread_id はGraphQLで取得）
gh api graphql -f query='
  mutation {
    resolveReviewThread(input: {threadId: "{thread_id}"}) {
      thread { isResolved }
    }
  }
'

# thread_id の取得（pr_number のスレッド一覧）
# ※ comments.nodes[0].id は node_id（Base64）。REST APIのレスポンスの node_id と照合する
gh api graphql -f query='
  query {
    repository(owner: "{owner}", name: "{repo}") {
      pullRequest(number: {pr_number}) {
        reviewThreads(first: 100) {
          nodes { id isResolved comments(first: 1) { nodes { id } } }
        }
      }
    }
  }
'
```

## マージコンフリクト解決フロー

PR 作成後やブランチ更新時にコンフリクトが発生した場合、以下のフローで対応する。

```
コンフリクト発生
  ↓
1. git diff HEAD...origin/main {ファイル} で差分を確認
   → ローカル変更の「意図」を把握する
  ↓
2. 判断基準:
   a) ローカル変更が「今回のタスク専用」の変更 → 保持してリベース
   b) ローカル変更が「過去の修正」で main に既に取り込まれている → origin/main を採用
   c) 両方に意味がある変更 → 手動マージ（推測でどちらかを破棄しない）
  ↓
3. 対応コマンド
   # ケース a/c: インタラクティブリベース（推奨）
   git fetch origin main
   git rebase origin/main
   # コンフリクト解消 → git add → git rebase --continue

   # ケース b: origin/main を採用
   git checkout origin/main -- {ファイル}
   git add {ファイル}
   git rebase --continue  # または git commit
  ↓
4. push
   git push --force-with-lease origin {ブランチ名}
```

### git stash pop コンフリクト時の注意（⚠️ --theirs/--ours が直感と逆・L-078）

`git stash pop` でコンフリクトが発生した場合、 `--theirs` と `--ours` の意味が **開発者の直感（自分の退避した変更＝ours と思いがちだが実際は theirs）や `git merge` と逆**（`git rebase` とは同じ）になる。技術的には `git merge` と同じ仕組み（HEAD = ours、適用する側 = theirs）で動くが、stash pop では「適用する側 = 自分のスタッシュ」になるため、merge の主観（自分のブランチ＝ours）と食い違う。一方 `git rebase` も「適用する自分のコミット = theirs」なので、ours/theirs の対応関係としては rebase と同じ。誤操作するとスタッシュ内の古い状態が main に紛れ込み、修正 PR が別途必要になる（V118 で発生）。

**stash pop コンテキストでの意味（直感と逆！）**:

| オプション | 採用される側 |
|---------|-----------|
| `git checkout --theirs path/to/file` | **スタッシュ側**（保存した変更） |
| `git checkout --ours path/to/file` | **現在のブランチ**（main 等）の内容 |

**判断フロー**:

```
git stash pop でコンフリクト発生
  ↓
1. git diff HEAD stash@{0} -- path/to/file で差分を確認
   → スタッシュ側と現在のブランチで何が違うかを把握する
  ↓
2. 採用したい側を決定:
   現在のブランチ（main 等）の内容を保持したい → git checkout --ours path/to/file
   スタッシュ側の変更を取り込みたい           → git checkout --theirs path/to/file
   不明 / 両方意味あり                         → 手動でファイルを正しい内容に編集する
  ↓
3. git add path/to/file → 通常通り作業を続行
```

**参照**: `docs/rules/lessons-core.md` L-078（V118 PR #1779 / 修正 PR #1783）

### 禁止事項

- 「とりあえず origin/main を採用」というデフォルト戦略を取らない（ローカル変更の確認を必ず行う）
- `git push --force`（`--force-with-lease` を使う）
- コンフリクト解消を推測で行わない（不明な場合はユーザーに報告）
- `git stash pop` コンフリクトで `--theirs`/`--ours` の意味を `git merge` の主観（自分のブランチ＝ours）や直感（退避した変更＝ours）と同じだと思い込まない。`git rebase` と同じく「適用する自分の変更 = theirs」（上記サブセクション参照）

---

## Force Push 後の AI レビュー再実施フロー

PR に `--force-with-lease` で push した場合、既存のレビューコメントが outdated 化する。
この場合、**必ず** 以下の手順で AI レビューを再実施すること。

```
Force push 実施（git push --force-with-lease origin {ブランチ名}）
  ↓
1. outdated レビューコメントを全て Resolve する
   # クラウド（L-114）: mcp__github__pull_request_read(method="get_review_comments") で thread_id を取得し
   #   mcp__github__pull_request_review_write(method="resolve_thread", threadId=...) を実行
   # ローカル: GraphQL で全スレッドの thread_id を取得してから resolveReviewThread を実行
   gh api graphql -f query='
     query {
       repository(owner: "kai-kou", name: "tsukuru-studio") {
         pullRequest(number: {pr_number}) {
           reviewThreads(first: 100) {
             nodes { id isResolved }
           }
         }
       }
     }
   '
   # 未 Resolve のスレッドを一括 Resolve
   # （mcp__github__resolve_review_thread ツールでも可）
  ↓
2. Layer 1 セルフレビューを再実行
   # 自前 code-review スキル（Skill(code-review)）で、書き換わった差分に対して必ず再度セルフレビューする
  ↓
3. 条件付きで Layer 2 を再起動
   python3 tools/discussion_review_trigger.py --pr {pr_number}
  ↓
4. （任意）subscribe_pr_activity で CI / 人手コメントを監視
   mcp__github__subscribe_pr_activity(owner="kai-kou", repo="tsukuru-studio", pull_number={pr_number})
  ↓
5. Layer 0+1 通過後に自動マージ（外部レビュアー依頼・25 分待ちは廃止）
```

### Force Push が必要になる典型ケース

| ケース | 原因 | 対処 |
|--------|------|------|
| rebase コンフリクト解消後 | ブランチ履歴が書き換えられる | 上記フローで再レビュー |
| wip コミットの squash | コミット SHA が変わる | 上記フローで再レビュー |
| ブランチ汚染（別テーマ混入）の修正 | 不要なコミットを除去 | 上記フローで再レビュー |

> **重要**: Force push 後に outdated コメントをそのままにして自動マージしてはいけない。
> 新コンテンツに対するレビューが一切実施されないまま main に取り込まれるリスクがある。

---

## サーキットブレーカー（無限ループ防止）

AIレビュー指摘への対応が収束しない場合に備え、以下の上限を設ける。

- **修正サイクル上限: 2回**（レビュー依頼 → 指摘対応 → 再レビュー依頼 の繰り返し）
- 2サイクル経過後も解消できない指摘がある場合は **即座に STOP** し、以下をユーザーに報告する:
  1. 未解消の指摘内容と件数
  2. 試みた対応の履歴
  3. **`docs/rules/problem-investigation-protocol.md` の Step 3 専門チーム並列調査の結果**（コードベース・ドキュメント・公式情報の3役以上）
  4. 直接原因 / 中間原因 / 根本原因の3層分析
  5. 推奨アクション（スキップ / 手動修正 / アーキテクチャ変更）
- ユーザーの判断を仰いでから再開する（推測で続行しない）

> **根拠**: エラーの複合蓄積を防ぎ、無限ループによるトークン浪費を回避するため。
> 「3回目の修正よりユーザーへの報告」を優先する。

> **L-077 対策**: サーキットブレーカー発動時の報告は、必ず `problem-investigation-protocol.md` のエスカレーション報告テンプレートを使用する。「実施済み調査」セクションが空の状態で報告するのは禁止。

## パイプライン別チェックリスト（再発防止）

**全パイプライン（script / audio / image / video）** で以下が揃っていることを毎回確認する。

| ステップ | script-pipeline | audio-pipeline | image-pipeline | video-pipeline |
|---------|:-:|:-:|:-:|:-:|
| セルフレビュー（self-reviewer スキル） | ✅ Step 5 | ✅ Step 7 | ✅ Step 6（セルフレビュー内） | ✅ Phase 4 |
| PR 作成前競合チェック | ✅ Step 6 | ✅ Step 8 | ✅ Step 7 | ✅ Phase 4 |
| PR 作成（`gh pr create`） | ✅ Step 6 | ✅ Step 8 | ✅ Step 7 | ✅ Phase 4 |
| **PR 作成後 Slack 通知（`[PR作成]`）** | ✅ Step 6 | ✅ Step 8 | ✅ Step 7 | ✅ Phase 4 |
| **Layer 1 観点別フレッシュ文脈セルフレビュー（必須・外部依頼なし）** | ✅ Step 6 | ✅ Step 8 | ✅ Step 7 | ✅ Phase 4 |
| 条件付き Layer 2（`discussion_review_trigger.py`） | ✅ Step 6 | ✅ Step 8 | ✅ Step 7 | ✅ Phase 4 |
| （任意）subscribe_pr_activity で CI / 人手コメント監視 | ✅ Step 7 | ✅ Step 9 | ✅ Step 7 | ✅ Phase 4 |
| 指摘コメントへの返信 + Resolve | ✅ Step 7 | ✅ Step 9 | ✅ Step 7 | ✅ Phase 4 |
| 自動マージ（squash） | ✅ Step 8 | ✅ Step 10 | ✅ Step 8 | ✅ Phase 4 |
| **マージ後 Slack 完了報告** | ✅ Step 9 | ✅ Step 10 | ✅ Step 8 | ✅ Phase 4 |
| Issue クローズ + 次 Phase Issue 作成 | ✅ Step 8 | ✅ Step 11 | ✅ Step 8 | ✅ Phase 4 |
| レトロスペクティブ実行 | ✅ Step 10 | ✅ Step 11 | ✅ Step 8 | ✅ Phase 6.5 |

### 不遵守パターンと根本原因（2026-03-30 調査）

過去の不遵守パターンと根本原因を記録する。

| パターン | 根本原因 | 対策 |
|---------|---------|------|
| audio/image/video パイプラインで `subscribe_pr_activity` が未使用 | SKILL.md に「pr-review-watcher スキルに従う」とだけ記載され、具体的な MCP 呼び出しコードが欠落していた | 各 SKILL.md に `mcp__github__subscribe_pr_activity` の呼び出しコードを明記（2026-03-30 修正済み） |
| マージ後の Slack 通知が audio/image パイプラインで欠落 | script-pipeline にはあったが、後発の audio/image に転記されなかった | 各 SKILL.md の自動マージ後ステップに `slack_notify.py pr` コマンドを追加（2026-03-30 修正済み） |
| image/video パイプラインでレビュー催促・タイムアウト判定のコードが未記載 | `pr-review-watcher スキル` への参照のみで実装詳細がなかった | タイムラインと催促コマンドを SKILL.md に直接記載（2026-03-30 修正済み） |

### 新パイプライン追加時のチェック

新たなパイプラインスキルを追加する際、または既存パイプラインに PR フローを追加する際は、以下を必ず含めること。

```markdown
# 必須チェック項目（テンプレート）

## PR 作成ステップ
1. セルフレビュー（self-reviewer スキル）実行
2. PR 作成前競合チェック（同一動画 ID のオープン PR を確認）
3. PR作成: クラウドは `mcp__github__create_pull_request`（L-114）/ ローカルは `gh pr create`（`--head` / `--base` / `Closes #N` を必ず含める）
4. 【必須・L-050】PR作成直後に存在確認: クラウドは `mcp__github__list_pull_requests(head="{owner}:{ブランチ}", state="open")` / ローカルは `gh pr list --head {ブランチ名} -R kai-kou/tsukuru-studio --limit 1 --json number,url,state` → URL を確認してから報告する
5. `slack_notify.py pr --pr-title "[PR作成] ..."` で PR 作成完了を通知
6. **Layer 1 セルフレビュー: 自前 `code-review` スキルを `Skill(code-review)` で必ず実行**（`.claude/skills/code-review/` が組み込みを置換・自律起動可・❌ Copilot/Gemini 依頼はしない）
7. 条件付きで Layer 2: `python3 tools/discussion_review_trigger.py --pr {pr_number}`
8. （任意）`mcp__github__subscribe_pr_activity` で CI / 人手コメントを監視

## レビュー監視ステップ
- 0分: 観点別フレッシュ文脈セルフレビュー実行 → 指摘対応（修正コミット or スキップ + 返信 + Resolve）
- Layer 0+1 通過後: 即自動マージ（外部レビュアー応答待ちなし）
- 任意: CI 失敗・人手コメントがあれば対応してからマージ
- サーキットブレーカー: 修正サイクル 2回で STOP

## マージ後ステップ
1. `mcp__github__merge_pull_request(owner="OWNER", repo="REPO", pull_number=PR_NUMBER, merge_method="squash")` で自動マージ
2. `slack_notify.py pr --pr-title "[完了] {タイトル}" --outcome "{アウトカム1文}" ...` で完了報告（アウトカム必須・L-052）
3. Issue クローズ + 次 Phase Issue 作成
4. `retrospective` スキル実行
```
