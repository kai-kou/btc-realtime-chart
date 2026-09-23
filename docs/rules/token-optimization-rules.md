# トークン消費最適化ルール

Claude Code のトークン消費を最小化し、セッションあたりのコスト効率を最大化するためのルール。

## 背景（2026-03 調査）

2026年3月に報告された異常なトークン消費の原因は以下の4つが重なったものである。

| 原因 | 種別 | 影響 |
|------|------|------|
| セッション再開バグ（CC-BUG-08） | バグ | 大規模プロジェクトで出力トークン暴走 |
| プロンプトキャッシュミス | 構造的問題 | CLAUDE.md・ルールファイルの再送コスト増大 |
| ピーク時間帯の消費速度引き上げ | 意図的変更 | JST 22:00〜翌4:00 のコスト増 |
| 需要爆増によるインフラ圧迫 | 背景因 | 全ユーザーに影響 |

## ルールファイル階層化（最重要対策）

### 設計原則

`.claude/rules/` に配置するのは **全セッションで必要な基盤ルール** のみ（実際の常駐リストは `tools/check_rules_sync.sh` の `ESSENTIAL_RULES` が正本）。タスク依存のルールは `docs/rules/` に実体のみ配置し、スキルが必要時に Read で読み込む。

### 常時必要ファイル一覧

> **SSOT 注意**: Hot 層（常時必要）の **正本は `tools/check_rules_sync.sh` の `ESSENTIAL_RULES`** 。下表は概念説明のための例示であり、実際の常駐リストは ESSENTIAL_RULES を参照すること（ドリフト防止）。

| ファイル（例） | トークン概算 | 理由 |
|---------|------------|------|
| `agent-team-summary.md` | ~1,300 | 全タスクでサブエージェント使用 |
| `completion-report-rules.md` | ~1,250 | 全セッションの完了報告構造 SSOT |
| `core-principles.md` | ~1,100 | 全タスクの大原則（詳細は `core-principles-detail.md`） |
| `datetime-rules.md` | ~800 | 日時表記 JST 統一 SSOT |
| `lessons-core.md` | ~2,300 | クリティカル **行動規範** のみ（環境障害カタログは `lessons/cloud-environment.md` へ降格・#324） |
| `pr-review-flow-summary.md` | ~1,350 | ほぼ全タスクで PR 作成（実行手順は `pr-review-watcher` スキル） |
| `session-compression-rules.md` | ~800 | 圧縮時の安全（詳細は `session-compression-rules-detail.md`） |
| `session-concurrency-rules.md` | ~1,000 | マルチセッション競合防止（R-1 ルーティン稼働のため Hot・詳細は `session-concurrency-rules-detail.md`） |
| `session-safety-rules.md` | ~800 | セッション安全 |
| `session-sprint-rules.md` | ~500 | スプリント運用の最小フォーム |
| `user-confirmation-minimization.md` | ~2,700 | 確認要否の SSOT（プロジェクト例詳細は `user-confirmation-minimization-detail.md`） |
| `user-instruction-issue-rules.md` | ~900 | ユーザー直接指示の Issue 化判断 |
| `user-notification-triage.md` | ~1,500 | `@mention` 厳選 SSOT（分類ロジックの正本は `triage_notification.py`） |

> **Warm 降格済み**: `progress-reporting-rules.md`（制作系の長時間処理時にスキルが Read）は **既定では Hot 層に含めない**。`session-concurrency-rules.md` は本リポジトリでは R-1 ルーティン稼働（マルチセッション並行運用）のため Hot 化済み（E-B #20・PR #176）。単一セッション運用のプロジェクトでは Warm のままでよい。Hot 化/降格する場合は `ESSENTIAL_RULES` を編集して `./tools/check_rules_sync.sh --fix` を実行する。

### 削減効果・予算の推移（#146 → #324 → #369 → #469 → #504 で再校正）

| 指標 | 当初（8ファイル構成時） | #146 棚卸し前（2026-07-10） | #146 棚卸し後 | #324 棚卸し後（2026-07-26） | #369 棚卸し後（2026-08-04） | #469 棚卸し後（2026-08-23） | **#504 棚卸し後（2026-09-02）** |
|------|------|------|------|------|------|------|------|
| `.claude/rules/` ファイル数 | 8（7 symlink + 1 例外） | 13 | 13 | 13（変更なし） | 13（変更なし） | 13（変更なし） | 13（変更なし） |
| `.claude/rules/` 総サイズ（`wc -c` 実測・1KB=1000B換算） | ~76KB | ~123KB（123,038B） | ~95KB（94,825B） | ~65KB（65,335B） | ~68.7KB（68,713B） | ~77.8KB（77,828B） | **~78.7KB（78,724B）** |
| 推定トークン数 | ~19,000 | ~31,000 | ~24,000 | ~16,300 | ~17,200 | ~19,500 | **~19,400** |

**#146 の経緯（メタ肥大化）**: 当初 76KB は 8 ファイル構成時の校正値。その後 7 ファイルが個別 Issue で正当化されて追加され 13 ファイル構成になった。個々の追加判断は妥当だったが累積の再校正がなく 76KB→123KB まで肥大化。#146 で「プロジェクト例」テーブル・詳細プロセス記述を各 `-detail.md`（Warm 層）へ抽出し 95KB まで圧縮した。

**#324 の再校正（到達値 ~65KB / ~16,300 トークン）**: #146 の直後から再増加が始まり（95KB→98KB）、同 Issue が「追記マージンはほぼ無い」と明記した状態を超過していた。Anthropic「[The new rules of context engineering for Claude 5 generation models](https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models)」の progressive disclosure 原則に沿って再棚卸しし、**Hot に残すのは「判断基準・不変の境界・実観測ベースの行動規範」だけ** とした。降格の判断軸は「代替の強制レイヤ（ハーネス / スキル / ツール / ツール description）が既にあるか」。

**#369 の再校正（当時の到達値 ~68.7KB / ~17,200 トークン。現行予算は下の増減ログを参照）**: #367→#375 の追加で増減ログが 4 行に到達し再棚卸しの合図が立ったため、#324 と同じ判断軸（「代替の強制レイヤが既にあるか」）で 13 ファイル全件を再点検した。降格したのは ① `session-compression-rules.md` の「新規ルールファイル追加時の必須手順」（`session-start.sh`/`post-compact.sh` の `check_rules_sync.sh --fix` が既に自動検出・修正するため、Hot には要旨 1 行のみ残し手順全文は `session-compression-rules-detail.md` へ）② `agent-team-summary.md` の Verbalized Sampling 記述（`agent-team.md`「サブエージェントの高度な機能」へ移設し SSOT を一本化）③ `completion-report-rules.md` の良い例/悪い例の具体テキスト（`stop-completion-report-check.sh` が Stop 時に既に是正リマインドを出すため、Hot には判断基準の「鉄則」5 項目のみ残し、例文は新設 `completion-report-rules-detail.md` へ）。#325/#328/#367/#375 で追加された行動規範自体は「実観測ベースの行動規範」（削減対象外②）に該当し、代替の強制レイヤが無いため Hot に残置した（再点検の結果、追加分の削除は不可と判断）。

**削減対象外（意図的に残す）**: ① A-1〜A-6 の既約境界外リスト ② 実観測ベースの行動規範 lessons（記事の削除基準 "specific, demonstrable failure mode" に照らすと残す側）③ Haiku サブエージェント向けの明示的な出力ルール（Claude 5 世代ではないため「判断に委ねる」の適用外）。**#469・#504 の再点検でもこの 3 区分は維持**（該当箇所は全件この基準で残置と再確認）。#504 で削減できたのは削減対象外に該当しない箇所のうち、フック（`pre-pr-create-check.sh`）または他 Hot 層ファイル（`pr-review-flow-summary.md`）と逐語重複していた 2 箇所のみで、以後の追加は下記の機械チェックに従う。

#### もう一方の常駐コスト: スキル / コマンドの `description`（#493・参考値）

上の予算表が数えているのは `.claude/rules/` **だけ** である。しかし `.claude/skills/*/SKILL.md` と
`.claude/commands/*.md` の frontmatter `description` は、セッション冒頭の一覧に全件展開されるため
**毎ターン常駐する**。ここが長らく集計外だったせいで「スキルを 1 本増やしても Hot 予算に影響しない」
という誤った前提が生まれ、実際に採否判断を歪めた（mattpocock/skills の採用検討で `wizard` の
コスト評価が「ゼロ」と見積もられた。正しくは「未計測」）。

`tools/check_hot_budget.py` はこれを **参考値として併記** する:

```
[hot-budget] 実測: 78,724B / 基準: 78,724B / ログ最新行: 78,724B / ログ行数: 1
[hot-budget] 参考: description 常駐 15,990B（22 件） / rules + description 合計 94,714B
```

**実測 15,990B は rules 側 78,724B の約 20%** にあたる。スキルを 1 本増やすことは、平均して
Hot 層に 700B 強を足すのと同じ重みを持つ。

現時点では **計測と表示のみ** で、閾値は設けない（rules 側の予算判定の挙動は変えない）。
数値が積み上がってから、どの水準で再棚卸しの合図とするかを決める — 先に閾値だけ置いても、
根拠のない数字を守ることになる。

#### 予算の増減ログ（1 行 1 追加・#146 型のメタ肥大化を防ぐため累積を可視化する）

| 日付 | 実測 | 差分 | 追加の正当化 / 相殺 |
|---|---:|---:|---|
| 2026-09-15 | 90,245 B | 基準 | **#116 の再棚卸し後の到達値**（増減ログ 4 行到達の合図 ① を受けての再点検）。前回基準（2026-09-11・88,193 B）からの増分 +2,052 B・+2.3% は超過閾値（+10%）に遠い。前回棚卸し以降に Hot 実体が変わったのは **4 本のみ** で（`completion-report-rules.md` +613 B / `pr-review-flow-summary.md` +428 B / `agent-team-summary.md` +256 B / `lessons-core.md` +51 B。いずれも 2026-09-15 のベース同期 `87acf5e` → `aa2d318` で親ベースから取り込んだ改訂）、残る 10 本は 2026-09-11 時点から無変更のため当時の点検結果（下流での削減 1 件・削減対象外 3 区分は全件残置）がそのまま有効。変わった 4 本の増分はいずれも **削減対象外 ②（実観測ベースの行動規範）** で、機械強制レイヤが代替しない（#633 は再掲抑止マーカーが実質機能していなかった実績、#645 は gh 不在時のフォールバック経路が無いと PR 回収そのものが止まる実測、#648 は `sleep` 限定の禁止表現がポーリングの別手段を素通りさせた実観測）。加えて 4 本とも **親ベース管理下** で、下流で削ると apply-base 同期が 3 方向マージ（両側変更）へ移行し、更新頻度の高い Hot 層では衝突コストを毎回払うことになる（2026-09-11 行で確立した判断軸。削減の実行主体は親ベース側とし、下流は還流候補の列挙までを担う）。**下流での削減対象は 0 件** のため、増減ログをここで打ち切り本行を新基準とする（`tools/check_hot_budget.py` で機械検証） |

**記載予算（基準）は ~90.2KB / ~22,000 トークン**（#116 再校正・下記ツールが機械検証する）。

**再棚卸しの合図**（どちらか一方を満たせば発火）: ① 増減ログが 4 行に到達する（累積の合図・#146 で見落とした点） ② **実測が基準行を 10% 以上超過する**。`python3 tools/check_hot_budget.py` が両条件と「実測とログ最新行の乖離」（ログ更新漏れ・#469 の根本原因）を毎回機械判定する（`tools/self_review_check.py` から Hot 層変更時に自動実行）。

> 🔴 **予算は「記録した値」ではなく「実測」で管理する**。Hot 層のファイルを **追加または追記** する PR は、同一 PR で次を実行して増減ログに 1 行足す:
>
> ```bash
> python3 tools/check_hot_budget.py   # 実測・基準・ログ最新行・再棚卸しの合図を機械判定してから増減ログを更新する
> ```

### 削減の品質バーを先に固定する（Anthropic cookbook 由来）

> 出典: [Cost Optimization on the Claude API](https://github.com/anthropics/claude-cookbooks/blob/main/cost_optimization/cost_optimization.ipynb)（Anthropic Applied AI チーム）。
> 採否の議論記録は `content/discussions/cost-optimization-cookbook-adoption/`。

cookbook の中核は「**品質バーを制約として先に固定し、コストだけを最小化する変数として扱う。eval が無ければ削減と劣化を区別できない**」。本リポジトリの Hot 層棚卸し（#146 / #324 / #369）は **KB とトークン数しか見ておらず、削った規律が実際に守られ続けているかを確認する手続きが無い**。以下の最小形で埋める（新規スクリプト・新規スケジュールは作らない）。

**ルール文書を削除・降格・要約する PR は、セルフレビュー時に次を満たすこと**:

- [ ] 削減対象の記述が **実際に適用されたはずの直近の実ケース**（Issue コメント / PR diff / セッションの行動記録）を **1 件以上** 挙げ、PR 本文に書く
- [ ] そのケースが「削減後に Hot 層へ残る要約だけで再現できるか」を確認する（再現できないなら削らない、または降格先を SKILL.md Step 0 の Read 対象にして決定論的に読ませる）
- [ ] 新規のラベル付きテストケースは作らない（既に起きた実ケースの回顧のみ。$/task の計測もしない — 本リポジトリはサブスク課金でタスク単価を測る手段が無い）

#### no-op テスト（既定挙動を変えない行を狩る）

品質バーの逆側に、**そもそも最初から効いていなかった行** を見つける判定がある。1 文ずつ次を問う:

> **この文は、モデルの既定挙動を変えるか？**

変えないなら、その行は載っているだけで毎ターン課金され、何もしていない（no-op）。書いた人が読んで
「もっともらしい」と感じるかは判定に関係しない。**基準はモデルの既定であって読者の納得ではない** ので、
no-op かどうかで意見が割れたときは議論ではなく **その記述を外して走らせて確かめる**。

- 引っかかったら、語を削るのではなく **文ごと消す**（弱い文を短くしても弱いままで、載っている限り課金される）
- 語の強さも同じ物差しで測れる。既定に勝てない語（既にそこそこ丁寧なモデルへの「丁寧に」）は no-op で、
  直し方は別の技法ではなく **より強い語**（「執拗に」）に置き換えること
- **上の実ケース 1 件のバーは免除されない**。no-op 判定は「消しても情報が減らない」ことの根拠にはなるが、
  「その規律が今も守られる」ことの確認は別途要る（両方を満たしてから削る）

同じ pruning の軸として **single source of truth** も見る。同じ意味が Hot 層の複数ファイルにあるなら、
ガードレールそのものではなく **重複した記述の側** を参照 1 行へ畳む（意味の重複は保守コストと
トークンを二重に払ううえ、階層上の重要度を実際より高く見せる）。

**単発の観測で構成を決めない**: cookbook は同一構成でも試行ごとに pass rate が入れ替わることを繰り返し警告する。モデル選択・effort 設定・ルール圧縮の良し悪しを **1 回の結果で断定しない**（`session-sprint-rules-detail.md` の SP 較正が「生値を KPI 化しない」としているのと同じ理由）。

#### 要約が例外条項を落とす失敗パターン（rule card 化のリスク）

cookbook で最も安価だった構成（マニュアルを「ルールカード」に圧縮して安いモデルへ分解）は、**例外条項（carve-out）が要約から抜け落ちて誤判定** した。「安くて少し間違っている」は最適化ではない。

本リポジトリの Hot 層サマリー化 + `-detail.md` 分離は、原文を **破棄せず退避** する点と、`SKILL.md` Step 0 の対応表が **モデルの自己判断に依存しない決定論的な Read ディスパッチ** である点で、この失敗例とは形が違う（全面的に同型ではない）。ただし **メインセッションが Hot 層のサマリーだけを見て判断する箇所**（本文中に散発する「詳細は `X-detail.md` を参照」）は、モデルが「今が例外を確認すべき局面だ」と気づけるかに依存するため同じリスクを共有する。

- Hot 層に残す要約からは、**判断の分岐を変える例外・境界だけは落とさない**（例示・手順・背景は落としてよい）
- 例外を Warm へ移すなら、参照を散発的な注記に留めず **スキルの Step 0 Read 対象** に載せて決定論的に読ませる

### 入力トークン管理（progressive disclosure）の適用範囲

cookbook の入力側レバーのうち、本リポジトリで **実行経路があるのは Hot/Warm 階層化と CLAUDE.md 圧縮**（上記）だけである。以下は汎用ベースでは採用しない:

| cookbook のレバー | 本ベースでの扱い | 理由 |
|---|---|---|
| tool search（`defer_loading`） | 採用しない | Claude Code は MCP ツール定義を **既定で遅延ロード** する（[公式](https://code.claude.com/docs/en/costs)）。ルール文書に書いても repo 側に制御手段が無い |
| 画像の事前ダウンスケール / Files API + code execution / token counting によるゲート | 汎用ベースには書かない | 画像・PDF・大規模 CSV を agentic loop に貼り込むワークロードが汎用ベースに存在しない（YAGNI）。**該当ワークロードを持つ下流プロジェクトが自分の `docs/rules/` に追記する** |
| 大量出力のフック側での事前フィルタ | 有効（採用可） | 公式もフックでの前処理を推奨。テスト出力・ログを丸ごと読ませず、フックで抽出してから渡す |

### 棚卸し手段としての `/doctor`（#327）

Claude Code 公式の診断コマンドを定期棚卸しに使う。実行は `workflow-health-check` スキルの Step 6-0 に組み込み済み。

| 実行形態 | 何を返すか |
|---|---|
| CLI `claude doctor` | **インストール健全性のみ**（native/npm 併存・パス破損・更新チャネル）。スキル / CLAUDE.md のサイズ適正化は含まれない（v2.1.220 実測・2026-07-26） |
| セッション内 `/doctor` | 設定・スキル・CLAUDE.md を含むフルチェックアップと修正 |

**出力は判断材料の 1 つとして扱う**。汎用ツールの「削れる」判定と、運用規律が主体である本リポジトリ Hot 層の必要性判定は一致しないことがある。削除の可否は「代替の強制レイヤ（ハーネス / スキル / ツール / 本体システムプロンプト）が実在するか」で決める。

### スキルが Read すべきルールファイル対応表

> ⚠️ 以下の表のスキル名・ルールファイル名は **出自プロジェクト（動画制作）の実例** 。汎用ベースには存在しないファイルもあるため、自分のプロジェクトのスキル・ルール名に読み替えること。

各スキルは Step 0 で必要なルールファイルを `docs/rules/` から Read する。

| スキル | 必要なルールファイル（`docs/rules/` から Read） |
|--------|-----------------------------------------------|
| script-pipeline, script-writer | script-rules.md, research-rules.md |
| script-team-reviewer | script-rules.md |
| audio-pipeline, voicevox-audio | audio-pipeline-rules.md, intonation-rules.md, pronunciation-rules.md |
| image-pipeline, image-generator | image-pipeline-rules.md, youtube-thumbnail-rules.md |
| video-pipeline | video-storage-rules.md, youtube-upload-safety-rules.md, youtube-title-rules.md, video-international-rules.md |
| shorts-pipeline | shorts-rules.md, research-rules.md, video-storage-rules.md |
| self-reviewer | self-review-learnings.md, script-rules.md, research-rules.md |
| retrospective | retrospective-rules.md, self-review-learnings.md |
| refinement | refinement-rules.md, research-rules.md |
| pr-review-watcher | self-review-learnings.md |
| youtube-scheduler | youtube-scheduling-rules.md |
| sns-publisher | slack-notification-rules.md |
| comment-responder | comment-response-rules.md |
| workflow-health-check | youtube-content-variation-rules.md, self-review-learnings.md |
| retro-try-handler | self-review-learnings.md |
| metadata-reviewer | youtube-title-rules.md |
| theme-discovery | series-management-rules.md |
| zenn-book-writer | zenn-book-rules.md |

### コンテキスト圧縮ポリシー

コンテキスト圧縮は Claude 標準の Auto Compaction（コンテキスト上限付近で自動発動・圧縮してセッションを継続）に委ねる。本ベースは圧縮タイミングを env（`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 等）で固定しない。

## ピーク時間帯回避ルール

### Anthropic ピーク帯（2026-03-26 公式発表）

**PT 5:00〜11:00 / UTC 13:00〜19:00 / JST 22:00〜翌 4:00**

この時間帯はトークン消費レートが最大 2〜3 倍に膨らむ。

### ピーク帯に避けるべきタスク

- 長時間パイプライン（image-pipeline: ~60 分、video-pipeline: ~180 分）
- Opus（`opus`）を使用するタスク（台本生成、複雑な設計判断）
- 大量のサブエージェントを起動するタスク（Agent Teams レビュー等）

### ピーク帯でも許容されるタスク

- 5 分以内で完了する軽量チェック
- Haiku モデルのみを使用するタスク
- Slack 通知やコメント投稿のみの操作

### スケジュールタスクへの適用

メインアカウントのスケジュールはすべて JST 05:00〜19:00 に収まっており影響なし。

**サブアカウントの調整が必要**:

| タスク | 変更前（JST） | 変更後（JST） | 理由 |
|--------|-------------|-------------|------|
| image-pipeline（サブ） | **01:00**（ピーク帯） | **05:00** | ピーク帯回避 |
| video-pipeline（サブ） | 05:00 | **08:00** | image の後に実行 |
| script + audio（サブ） | 18:00 | 18:00（変更なし） | ピーク帯外 |

> **2026-05-05 更新（3アカウント体制移行）**: メインA が 24 時間フル稼働（深夜帯含む）に移行し、
> サブBも hourly 専用スロットを追加した。ピーク帯（JST 22:00〜翌4:00）での実行は Extra Usage を
> 消費するが、3アカウント合計で最大 84回/日（各28回/日 × 3）の実行容量を確保しているため、
> コスト効率より制作スループットを優先する設計判断。ピーク帯での長時間タスクがExtra Usage上限に
> 先に到達した場合はセッションが中断されるが、次スロットで自動復帰する（`session-safety-rules.md` 参照）。

## フック統合（CC-BUG-16 対策）

### 問題

フック 8 個以上でコンテキスト肥大化・ターン早期終了のリスクがある（CC-BUG-16）。

### 対策

| 変更 | 変更前 | 変更後 |
|------|--------|--------|
| PreToolUse (Bash) | 3 個（push, PR, comment） | **1 個**（`pre-tool-use-router.sh`） |
| PreToolUse (MCP) | 1 個（image gen） | 1 個（変更なし） |
| Stop | 3 個（git, PR, slack） | **1 個**（`stop-router.sh`） |
| **合計** | 11 個 | **7 個** |

ルータースクリプトがコマンド内容に応じて適切なチェックスクリプトに委譲するため、検証機能は完全に維持される。

## セッション再開バグ防御（CC-BUG-08 補強）

### 問題（2026-03-23 発生）

大規模プロジェクトのセッション再開時、ユーザー入力ゼロで出力トークン 652,069 が生成された事例。
本プロジェクトはルールファイル ~19K トークン（最適化後）を持つが、スキル SKILL.md を含めると依然として大規模。

### 既存の防御策（有効性確認済み）

- ✅ セッション再開に依存しない設計（Git + Issue コメントが権威ソース）
- ✅ PostCompact / Stop フックで自動コミット
- ✅ 「大きなセッション（50+ ターン）は再開せず新規セッションで開始」ルール

### 追加防御策

- Claude Code を常に最新バージョンに維持（session-start.sh で自動更新済み）
- `ccusage` でセッション再開後のトークン消費を定期監視（月次 workflow-health-check で実施）
- 異常なトークン消費（1 セッションで出力 100K+ トークン）を検知した場合、retro-try Issue を作成

## CLAUDE.md 圧縮

### 設計原則

CLAUDE.md には **全セッションで必要な判断基準と参照リンク** のみを記載する。Phase 固有の詳細仕様はルールファイルまたはスキル SKILL.md に委譲する。

### 移譲した主要セクション

> ⚠️ 以下の表の移譲先ルールファイル名は **出自プロジェクト（動画制作）の実例** 。汎用ベースには存在しないファイルもあるため、自分のプロジェクトのルール名に読み替えること。

| セクション | 移譲先 | 削減量 |
|-----------|--------|--------|
| Remotion 詳細仕様（z-index, VisualCue, 字幕, SourceCredit） | `docs/rules/remotion-rules.md` | ~106 行 <!-- refcheck:ignore --> |
| 画像生成ルール詳細 | `docs/rules/image-pipeline-rules.md` 参照 | ~12 行 <!-- refcheck:ignore --> |
| VOICEVOX 詳細 | `docs/rules/audio-pipeline-rules.md` 参照 | ~4 行 <!-- refcheck:ignore --> |
| YouTube API 詳細 | `docs/rules/youtube-scheduling-rules.md` 参照 | ~6 行 <!-- refcheck:ignore --> |
| Slack 通知詳細 | `docs/rules/slack-notification-rules.md` 参照 | ~7 行 |
| スキル配置リスト（28 行） | 各スキル SKILL.md | ~24 行 |
| **合計** | | **~159 行削減** |

## 禁止事項

- `.claude/rules/` にタスク依存のルールファイルを symlink で追加しない（`ESSENTIAL_RULES` リスト外）
- ピーク帯（JST 22:00〜翌 4:00）に長時間パイプラインをスケジュールしない
- フック数を 8 個以上に増やさない（統合ルータースクリプトを使用）
- CLAUDE.md に Phase 固有の詳細仕様を直接記載しない（ルールファイルまたは SKILL.md に委譲）
