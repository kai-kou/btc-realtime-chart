# Warm 層 教訓 — PR レビュー・自動マージ

PR 作成・AI レビュー監視・自動マージに関するカテゴリ別教訓（タスク依存で Read）。

---

## L-050: PR 作成直後に存在確認しないとサイレントスキップを見逃す（2026-06-13）

**パターン**: `gh pr create` がプロキシ環境やエラーで失敗しても、戻り値を確認せず
「PR 作成済み」と思い込んで次の処理（レビュー依頼・マージ）に進む。実際には PR が無く、
作業がサイレントに失われる。

**根本原因**: PR 作成コマンドの成否を検証していない（Time-of-Check の欠落）。

**対策**: PR 作成の **直後に必ず存在確認** する。`pr-review-flow-summary.md` の必須フォーム。

クラウド（一次経路・L-114）:
```
mcp__github__list_pull_requests(owner="kai-kou", repo="tsukuru-studio",
                                head="kai-kou:{branch}", state="all")
```

ローカル実行用:
```bash
gh pr list --head {branch} -R kai-kou/tsukuru-studio --limit 1 --json number,url,state \
  --jq '.[0] | select(.url != null) | "PR #\(.number) \(.state): \(.url)"'
```

存在確認が取れない場合は PR 作成を再試行する（マージへ進まない）。

---

## L-102: AI レビュー指摘対応はユーザーに報告しない（サイレント原則）（2026-06-13）

**パターン**: AI レビュー（Gemini / Copilot 等）の指摘対応をチャットや Slack `@mention` で
逐次ユーザーに報告し、ユーザーをレビューの門番にしてしまう（Human-in-the-loop アンチパターン）。

**根本原因**: 指摘対応は境界内（自律実行）の作業なのに、進捗を逐次共有すべきと誤解している。

**対策**: 指摘対応の記録は **PR スレッド返信・Resolve・Issue コメントのみ**。チャット逐次報告・
Slack `@mention`・完了報告アウトカムへのレビュー対応混入は禁止。例外は A-1〜A-6
（サーキットブレーカー発動・ファクト致命的 NG 等）のみ。完了報告の `--outcome` は
「初回指示で何ができるようになったか」だけを書き、指摘件数・修正サイクルは書かない。

---

## L-109: 他セッションが対応中の PR に介入しない（アクティビティロック）（2026-06-13）

**パターン**: 共通プリフライト（`check_pending_pr_reviews.py`）で全オープン PR を見るため、
別セッションが作成・対応中の PR に催促・指摘対応・問題なし判定・マージ・subscribe で
重複参入してしまう（レンダリング等の二重実行事故）。

**根本原因**: `status:in-progress` の論理ロック（CP-4 レイヤー 2）は PR レビューフェーズには効かない。

**対策**: `check_pending_pr_reviews.py` が各 PR の人間側最終アクティビティを `last_activity_min`
として算出し、**直近 10 分以内に活動がある PR を `active_session: true` として
`--actionable-only` から除外** する。出力に現れない PR は別セッションが現役対応中
（`--include-active` での強制取得も禁止）。自分が作成した PR の監視は `--json` + PR 番号
フィルタで従来どおり行う。詳細は `session-concurrency-rules.md` レイヤー 5。

---

## L-120: 高頻度で自動更新される git 追跡テレメトリを feature の WIP 自動コミットに相乗りさせない（2026-06-27）

**パターン**: 月次コスト集計（`content/analytics/cost_monthly/`）を Stop hook の `--flush` が
毎セッション書き換え、直後の WIP 自動コミット `git add -A` がそれを作業中の feature ブランチへ
無差別にステージ。結果、全 feature PR に無関係な cost churn が混入し、レビューセッションが
正しく「無関係 churn」と判定して破棄しようとする不健全なループに陥った（実例: PR #101 が
本来 3 ファイルなのに cost_monthly 25 行が混入）。

**根本原因**: 「高頻度で自動更新される git 追跡ファイル（テレメトリ）」と「feature ブランチ上の
無差別 `git add -A` 自動コミット」が構造的に両立しない。永続化（追跡）と churn 隔離（feature
差分を汚さない）を分離する仕組みが無かった。

**対策**（#106 で実装 → #242 で永続化レーンを刷新）:
- Stop hook の WIP `git add -A` から `content/analytics/cost_monthly/` を **pathspec 除外**
  （`git add -A -- . ':(exclude)content/analytics/cost_monthly/'`）。
- **#242 以降**: cost_monthly は gitignore 対象（main では追跡しない）。永続化は
  `tools/commit_cost_telemetry.py` が **テレメトリ専用データブランチ `telemetry/cost-data` へ
  1 日 1 回 plain git push** で行う（gh 非依存・PR レーンは廃止。旧「1 日 1 回の専用 PR」は
  クラウドの gh 403 で機能しなかった）。`chore/cost-telemetry-*` PR はもう作られない。
- `tools/self_review_check.py` が「feature 差分に cost_monthly が追加/変更として現れたら
  回帰」と Warning する。

**判定基準**: hook やスケジュールが「自動で書き換える追跡ファイル」を作るとき、その commit 経路が
feature ブランチに乗らないか（専用ブランチ／専用 PR に隔離されているか）を必ず確認する。

---

## L-118: 局面限定の「マージするな」口頭指示を恒久ポリシーに昇格させてマージ前で止まる（2026-06-26）

**パターン**: セッション中盤（デバッグ・調査など不安定な局面）で受けた一時的な口頭指示
「ストップしたら自動マージしないで」「いったん止めて」を、**その局面が終わった後の通常の PR フローにまで持ち越し**、
Layer 0 + Layer 1 を通過してマージ可能になった PR を「マージ判断待ち」で止めてしまう。
これは L-103（PR を出さず止まる）の姉妹形態で、**PR は出してレビューも済んだのにマージ前で止まる** CP-6 中核違反。

**根本原因**: 一時的・局面限定の口頭指示（session-scoped）と、恒久ポリシー（`CLAUDE.md`「PR 作成の完全自律化」・
auto-merge 既定）の **相互作用ルールが無く**、曖昧なまま最も保守的な「止まり続ける」に倒した。
チャット履歴は圧縮で揮発するが恒久ルール（CLAUDE.md・`.claude/rules/`）はディスクから再読込されるため、
CLAUDE.md/ルールに書かれていない口頭指示は **その局面が終われば失効** するのが正しい解釈。

**対策（判定基準）**:
- 「マージするな」「止めて」等の口頭指示は **発話された局面（デバッグ・中途状態・特定 PR の保留）に限定** して解釈する。恒久ポリシーの上書きとは見なさない。
- ユーザーがその後「進めて」「完遂して」等で **フロー継続を再承認** したら、auto-merge を含む完全自律フローを最後まで走らせる（マージ前で再び止まらない）。
- PR のマージ保留を恒久化したいなら、ユーザーが **現在の・明示の** 保留指示を出しているか、PR に保留ラベルがある場合のみ。それ以外は **Layer 0 + Layer 1 通過 = 自律マージが既定の終端状態**。
- 迷ったら「マージ判断待ち」で止めるのではなく、`pr-review-flow-summary.md` の自律マージへ進む（PR 作成・マージは A-1〜A-6 の既約境界外に **含まれない**＝確認不要）。

---

## L-119: 組み込み `code-review` スキルは `disable-model-invocation` により Claude の自律起動不可（2026-07-21）

**パターン**: 自律セッションで Layer 1 セルフレビューを実行しようと `Skill(code-review)` を呼ぶと
`Skill code-review cannot be used with Skill tool due to disable-model-invocation` で失敗する（v2.1.216 実機確認）。
同じ検証で `Skill(security-review)` は問題なく起動できたため、`code-review` **個別** にモデル自律起動が
禁止されていると判明（一時的な不具合ではなく Anthropic 側の意図的な仕様変更）。

**根本原因**: `disable-model-invocation` は Claude Code 公式の skill frontmatter フィールドで、
`true` にすると Claude（Skill ツール経由の自律起動）を禁止し、**ユーザーが対話セッションで
スラッシュコマンドを手打ちしたときだけ** 起動できる（deploy・publish 等の副作用/不可逆操作を
「テストが通ったから Claude が勝手に決める」のを防ぐ設計）。Anthropic が組み込み `code-review` に
このフィールドを付与したため、Layer 1「Claude 自身が `/code-review` を必ず実行する」前提が崩れた。

**対策（恒久・#280）**: **同名 project スキルで bundled をオーバーライドする**。公式仕様
（skills ドキュメント「A skill at any of these levels also overrides a bundled skill with the same name.
For example, a `code-review` skill in your project's `.claude/skills/` replaces the bundled `/code-review`」）
により、`.claude/skills/code-review/SKILL.md`（自前実装・`disable-model-invocation` なし）を置くと
対話（`/code-review` 手打ち）・自律（`Skill(code-review)`）の両方から起動できる。Layer 1 の標準実行手段は
この自前スキル（SSOT: `docs/rules/ai-reviewer-strategy.md`）。暫定対応期（#275）に使った
`self-reviewer` Step 2 のサブエージェント直接レビューは、自前スキルの解決が bundled 側に倒れて
自律起動エラーが再発した場合のフォールバックとして残す。
`disable-model-invocation` が付与された他の組み込みスキルを自律起動しようとして同種のエラーに
遭遇したら、まずこのフィールドの有無を疑い、恒久利用したい場合は同名 project スキルでの置換を検討する
（対応: #275 → #280）。

---

## L-125: PR インラインレビュー投稿で必ず踏む GitHub API の地雷 4 件（2026-08-11・#461）

**パターン**: Layer 1 セルフレビューの指摘を PR の行単位インラインコメントとして残す実装で、
以下 4 つは仕様上ほぼ確実に踏む。SKILL.md の手順どおりに回避する
（実装 SSOT: `.claude/skills/code-review/SKILL.md` Step 3-A）。

| # | 地雷 | 症状 | 回避策 |
|---|------|------|--------|
| 1 | 指摘行が diff ハンク外 | `add_comment_to_pending_review` が 422（`line must be part of the diff` 相当） | `get_diff` のハンク表で事前判定 → 外れていれば `subjectType="FILE"` に切替（本文に元の `file:line` を明記）。事後の 422 も 1 回だけ FILE で再投稿 |
| 2 | pending review の二重作成 | 前セッションが `submit_pending` 前に中断していると `create` が失敗（pending は 1 ユーザー 1 PR に 1 件） | 投稿前に `get_reviews` で自分の `state="PENDING"` を検出し `delete_pending` で破棄してから作り直す |
| 3 | 自己 PR への `APPROVE` | PR 著者は自分の PR を承認できず必ず失敗する | `event` は常に `COMMENT`。`REQUEST_CHANGES` も dismiss/更新の method が無く自分で解除できないため使わない |
| 4 | submit 済み review の body 編集不可 | 「あとでサマリーに追記する」設計が成立しない | サマリーは投稿時に確定させ、再レビューは新しいレビューとして投稿する |

**あわせて禁止**: 既存コメントの `file:line` 照合による重複投稿スキップ。修正コミットで行番号がシフトするため、
同じ行に生まれた **別の新規欠陥を「既出」と誤判定して握りつぶす**（L-077 の沈黙禁止と自己矛盾する）。
重複対策は「pending 破棄」と「同一ラウンド内のバッチ dedup」に限定する。
また、失敗した指摘の集約記録に `update_pull_request(body=...)` を使わない（全文置換で PR 説明文を破壊する）。
`add_issue_comment` へ 1 回だけまとめて投稿する。

---

## L-131: 同一文脈のセルフレビューは自己修正盲点を越えられない。フレッシュ文脈レビューは PR の前に置く（2026-09-11・#627）

**パターン**: 実装 → `self-reviewer` Step 2（同じセッションがチェックリストで見返す）→ PR 作成 → `code-review`
（初めての第三者視点）という順序のため、PR 後の Layer 1 に指摘が集中する。直近 22 PR の指摘 121 件は修正率 79%・
誤検知 4%（大半が実欠陥）で、1 コンポーネント（シェルコマンド解析ガード）だけで 21% を占めた。
「テストのテスト」（ロジックを外しても緑の vacuous test）と「自分の要約を疑わない」（Spec 忠実性）が典型的な死角。

**根本原因**: 自分起因の誤りは同一文脈では 64.5% で修正に失敗する（Self-Correction Bench・arXiv:2507.02778）。
レビューの厳しさではなく **順序** の問題。

**対策（#627）**: ① `self-reviewer` Step 3.5 で `Skill(code-review)` を `--pre-pr` で実行し CONFIRMED を修正してから
PR を作る（`has_code` または `high_risk`。データのみの差分はスキップ）② `REVIEW.md` で較正（検証バー・Nit 上限 3・
PLAUSIBLE は本文列挙・再レビュー収束）③ Layer 0 に構文検査と対応テストの自動実行、PR 本文の検証証跡チェック。
対照例 #586（PR 前に観点別レビュー実施 → PR 後 0 件）。効果は Layer 1 の JSONL 計測（指摘ゼロ PR 率・
PR あたり CONFIRMED 件数）で確認する。
