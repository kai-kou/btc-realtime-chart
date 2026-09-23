---
name: research-runner
description: Deep Research を完全自動実行するスキル。ネイティブ `/deep-research`（クラウド環境でも直接実行可能・Opus orchestrator）を主エンジン、`claude -p` サブプロセス経由の `/deep-research` を第2、DIY（Sonnet 5 + WebSearch/WebFetch のウェブリサーチ）を最終フォールバックとして、`content/research/{ID}_prompt.md` から `content/research/{ID}_deep_research.{md,json}` を自動生成し、品質ゲート・PR 作成・AIレビュー・自動マージまでを担う。外部 LLM API（Gemini 等）によるディープリサーチは行わない。「リサーチ自動化して」「ディープリサーチして」「research-runner」と依頼された時に使用する。「ディープリサーチして」の既定エンジンは本スキルであり、ビルトインの deep-research や素の WebSearch へ直行しない。
model: sonnet
effort: high
disallowed-tools: AskUserQuestion
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# research-runner スキル

Deep Research の完全自動化スキル。ユーザーの手動ディープリサーチを不要にし、
プロンプト（`content/research/{ID}_prompt.md`）から引用付きの構造化リサーチ結果を自律生成する。

> このスキルは汎用ベース版。`{ID}` は任意のリサーチ識別子（slug）で、トピック名・案件 ID など
> 何でもよい。`kai-kou/btc-realtime-chart` はプロジェクト適用時に実リポジトリ slug に置換する
> （環境変数 `GITHUB_REPOSITORY` があればツールは自動でそれを使う）。
>
> 🔴 クラウド実行環境では repo スコープの `gh`（REST + GraphQL）が egress プロキシに 403 でブロックされる（L-114）。
> 本スキル内の `gh issue edit/view` 等の Issue 操作は GitHub MCP（`mcp__github__*`）を一次経路とし、
> `gh` コマンド例は **ローカル環境向けの代替** として読む（SSOT: `docs/rules/github-mcp-fallback-patterns.md`）。
>
> Step 3.6 / Step 4（フォールバック連鎖の詳細手順）は `reference.md` に分離済み。
> 主エンジン（ネイティブ `/deep-research`）が成功する通常パスでは Read 不要。

## §0: ルーティング（SSOT・最優先）

ユーザーが「ディープリサーチして」「deep research して」「深く調べて（リサーチ）」等と指示したら、
**このスキル（`research-runner`）が既定の起動先である**。明示しないと
ほぼ同名のビルトイン `deep-research` や素の `WebSearch` に倒れるため（Issue #73 の再発防止）、以下を厳守する。

| 候補 | 実体 | いつ使うか |
|------|------|-----------|
| **`research-runner`（本スキル）** | ネイティブ `/deep-research`（公式分類は **Workflow**・adversarial 多票検証）を、対話起動時は **`Workflow` ツールで直接呼び出し**（Step 3a・`Skill` ツール経由は v2.1.218 以降不可）、自律・バッチ起動時は `tools/run_deep_research_workflow.py`（`claude -p` サブプロセス経由・Step 3b）で実行 | **既定**。ユーザーが「ディープリサーチ」を求めたら必ずこれを最初に起動する |
| 素の `WebSearch` / `WebFetch` | メインセッションの単発検索 | ディープリサーチの **既定にしない**。軽い事実確認のみ |

> **注意**: ツール一覧の `deep-research` は本スキルの DIY フォールバックではなく、**ネイティブ `/deep-research`（Workflow）そのもの**（`code.claude.com/docs/en/commands` で `[Workflow]` 分類・§0 の実体列を参照）。DIY フォールバック（Step 4）は名称のみで専用ツールは存在しない。

- **禁止**: 「ディープリサーチして」に対し、本スキルを起動せず素の `WebSearch` で済ませる／ビルトイン `deep-research` を
  いきなり使う（安易な軽量経路直行禁止・Step 4 の DIY 直行禁止と同じ原則）。
- **例外**: ユーザーがコスト/速度優先を明示した、または対象が `/deep-research` の 30〜50 分が過剰なほど軽微な場合のみ、
  理由を 1 行述べて簡易リサーチに切り替えてよい（サイレント切替は禁止）。

## 採用方針

| 項目 | 内容 |
|---|---|
| **主エンジン** | **ネイティブ `/deep-research`（公式分類=Workflow・adversarial 多票検証）**。対話起動（Step 3a）は本セッションから **`Workflow` ツール**（`Workflow({name: 'deep-research', args: ...})`）で直接呼び出す（`claude -p` 不要・実機検証 2026-07-29 / CLI v2.1.220・`docs/rules/dynamic-workflows-rules.md` 参照）。**`Skill` ツール経由は v2.1.218 以降 `disable-model-invocation` で不可**。正確性が最高で **必ず最初に実行する** |
| 第2エンジン | **`claude -p` サブプロセス経由の `/deep-research`** — `tools/run_deep_research_workflow.py`（`claude -p` サブプロセス + Opus 明示指定）。自律・バッチ起動（Step 3b）ではこちらが最初の経路。対話起動では Step 3a（直接呼び出し）が失敗したときのフォールバック |
| フォールバック | **DIY（ウェブリサーチ）**（Sonnet 5 + WebSearch + WebFetch）（上記2つが失敗時の最終手段） |
| 禁止 | **外部 LLM API（Gemini 等）によるディープリサーチは行わない**（飼い主決定・2026-07-16・Issue #260）。旧 Gemini Deep Research Max 経路は廃止済み |
| コスト | **既定=サブスク週次枠経路（追加 $ ゼロ）**: セッション認証（Claude Code Max サブスク）をそのまま使用し（`DEEP_RESEARCH_USE_SUBSCRIPTION=1` 既定）、`/deep-research` は週次クォータの枠内で実行され追加課金なし。`DEEP_RESEARCH_USE_SUBSCRIPTION=0` で従来の API 従量経路（1本上限 `--max-budget-usd`・当月累計 `$40` 超で DIY フォールバック・月 `$50` ブレーカー）に戻せる（Step 3a の直接呼び出しはセッションの既存認証をそのまま使うため、この課金分岐自体が発生しない） |
| モデル | 公式仕様は「ワークフロー内の各エージェントはセッションのモデルを使用（スクリプトが明示的に別モデルへ routing しない限り）」。**Opus 固定は本プロジェクトの選択**（Step 3b が `--model opus` を明示指定・エイリアス）であり、Anthropic 側が `/deep-research` を Opus に固定している仕様ではない。Step 3a（直接呼び出し）はそのときのセッションモデルに従う点に注意 |
| 想定時間 | 本番では 30〜50分/本（wall-clock）。Step 3a はネイティブの Workflow バックグラウンド実行 + 完了通知に従う。Step 3b は必ず `run_in_background` + heartbeat で監視する |

> **claude -p の位置づけ**: Step 3b の `claude -p` は Web ギャップ代替ではなく、コンテキスト隔離・90 分タイムアウト・レート枠検出（EXIT=6）が本質的に必要な **設計上の一次経路**（`isolation-by-design`）。分類とフォールバック標準形の SSOT は `docs/rules/native-fallback-rules.md`。

## 起動条件（自律起動・対話起動）

以下のいずれかを満たすとき起動する:

1. ラベル `phase:research`（または相当）+ `status:waiting-claude` のオープン Issue（自律起動）
2. `content/research/{ID}_prompt.md` が存在し、`content/research/{ID}_deep_research.md` が存在しない（自律起動）
3. **ユーザーが対話で「ディープリサーチして」等と依頼したとき（対話起動・アドホック）**。
   プロンプトファイルや Issue が **無くても STOP せず**、Step 2 の「アドホック起動」フローで
   ユーザー指示文からプロンプトを生成して主エンジンへ進む（§0 のルーティングに従う・Issue #73）。

> 自律起動（1・2）の最初のアクションは **対象 Issue を `status:in-progress` に切り替える** こと
> （CP-4 マルチセッション競合防止）。対話起動（3）は対象 Issue が無いことが多く、その場合はロック不要。

## 必読ドキュメント

作業前に以下を Read する:
- `docs/rules/research-rules.md` — リサーチ品質基準（情報ランク A/B/C・正式名称・必須セクション・fact_check 閾値）
- `tools/research_schema.json` — 出力スキーマ

## 実行フロー

### Step 0: 共通プリフライト

1. （プロジェクトに hourly プリフライトがあれば）`python3 tools/check_pending_pr_reviews.py --actionable-only --json` を実行
2. （API 従量経路時のみ）当月コスト累計を確認: `python3 tools/run_deep_research.py {ID} --dry-run`
3. サーキットブレーカー発動時は **STOP + 通知**（境界外。ユーザー判断を仰ぐ）

### Step 1: 対象 Issue のロック取得（CP-4）

MCP（クラウド・一次経路。`labels` は全置換のため現在のラベルを取得してフルリストを渡す・§2.2）:
```
mcp__github__issue_read(method="get_labels", issue_number={ISSUE_NUM})
mcp__github__issue_write(method="update", issue_number={ISSUE_NUM},
  labels=[現在のラベル − "status:waiting-claude" ＋ "status:in-progress"])
```

ローカル環境（gh CLI 到達可能時）の代替:
```bash
gh issue edit {ISSUE_NUM} -R kai-kou/btc-realtime-chart \
  --remove-label "status:waiting-claude" \
  --add-label "status:in-progress"
```

### Step 2: プロンプト確認

```bash
cat content/research/{ID}_prompt.md
```

**自律起動（起動条件 1・2）の場合**: 未存在なら、ツールは research_id を含むオープン Issue 本文の
`## Deep Research プロンプト` セクションからの復元を試みる（`load_prompt` フォールバック）。
それでも取得できない場合は上流のプロンプト生成が未実行 → ユーザーへ報告して STOP。

**対話起動（起動条件 3・アドホック）の場合**: プロンプトファイルが無いのは正常。STOP せず、
ユーザーの指示文（「〜についてディープリサーチして」の本文）から `{ID}_prompt.md` を生成して Step 3 へ進む。

```bash
# ID は指示文から短い slug を作る（例: "競合の料金体系" → pricing-competitor-20260621）
mkdir -p content/research
cat > content/research/{ID}_prompt.md <<'EOF'
# Deep Research プロンプト

## 調査テーマ
{ユーザー指示文の本文をそのまま}

## 調査項目（5〜7 項目に分解）
- {指示文から分解した観点1}
- {観点2}
- …

## 出力要件
docs/rules/research-rules.md の品質基準（ランク A/B/C・official_names・5〜7 セクション・出典 8 件以上）に従う
EOF
```

> 指示が曖昧で調査項目に分解できない場合のみ、`AskUserQuestion` は使えない（`disallowed-tools`）ため、
> メインセッション側で 2〜3 点の前提（対象範囲・期間・地域など）を補ってプロンプト化する。
> プロンプト生成後は **必ず Step 3（主エンジン）を最初に実行** する（素の WebSearch 直行禁止・§0）。

### Step 3: 主エンジン実行（ネイティブ `/deep-research`）【必ず最初に実行】

> 🔴 **最重要**: 主エンジンは **ネイティブ `/deep-research`**（公式分類=Workflow）。Step 4 の DIY を
> 最初から実行してはならない（安易な DIY 直行禁止）。起動方法は起動条件で分岐する（下記 3a / 3b）。

**どちらを使うか（起動条件で自動分岐・§0 参照）**:

| 起動条件 | 使う経路 |
|---|---|
| 対話起動（起動条件 3・ユーザーが今この対話でディープリサーチを依頼） | **Step 3a（直接呼び出し）** |
| 自律起動（起動条件 1・2・Issue ラベル駆動 / 他セッションからの再開） | **Step 3b（`tools/run_deep_research_workflow.py`）** |

#### Step 3a: 対話起動時 — `Workflow` ツールで直接呼び出し（`claude -p` 不要）

> 🔴 **起動手段は `Workflow` ツール。`Skill` ツール経由は使えない**（実機検証 2026-07-29・CLI v2.1.220）。
> `/deep-research` は **bundled workflow** であり、**v2.1.218 で `disable-model-invocation` 相当が適用され
> モデル側からの Skill 起動が塞がれた**（公式: *"`/deep-research` runs only when you invoke it.
> Before v2.1.218, Claude could also start it on its own."*）。`Skill(skill="deep-research")` は
> `cannot be used with Skill tool due to disable-model-invocation` で即失敗する。
> 一方 **`Workflow` ツールの `name` 指定は現在も通る**（実測で run 起動を確認）。
> 検証記録は `docs/rules/dynamic-workflows-rules.md`。

```
Workflow({name: "deep-research", args: "{Step 2 で生成したプロンプトの調査テーマ本文}"})
```

> **起動経路は固定せず ladder で降格する**（`native-fallback-rules.md` §2.5）。上の実行形は
> 現時点の `preferred` 経路であり、CLI のバージョンで移動しうる。**実行前に現行の試行順を台帳から確認する**:
>
> ```bash
> python3 tools/native_fallback.py routes deep-research-interactive
> ```
>
> ladder: `skill`（**v2.1.218 以降 unavailable**）→ **`workflow`（現行 preferred）** → `claude -p`（Step 3b）
> → DIY（Step 4）。降格シグネチャ（`disable-model-invocation` / `Review dynamic workflow before running` 等）に
> 一致するエラーが返ったら次段へ進み、**経路が動いていたら台帳の `routes` を更新する**（§2.5 の後始末）。

- 呼び出しは Workflow ランタイム経由でバックグラウンド実行される。ターン終了を焦らず、完了通知
  （タスク完了 / レポート到着）を待つ。**モデルはそのときのセッションモデルに従う**（Opus 品質が
  必要なら呼び出し前に `/model opus` するか、Step 3b を使う。§「モデル」の注意参照）。
- 結果として返るレポート本文を `content/research/{ID}_research_raw.md` に保存 → 即コミット&push（L-100）
  → Step 3b と同じ **正規化ロジック**（`tools/run_deep_research_workflow.py --normalize-only --report ...`）
  で `research_schema.json` 準拠 JSON に変換して Step 5 へ進む（再検索なし・$0）。
- **失敗時（EXIT相当のエラー・本文欠落・WebSearch 未許可等）→ Step 3b（`claude -p` サブプロセス）へ**
  （同じネイティブ `/deep-research` を隔離サブプロセスで再試行する）。
- 前提条件: WebSearch ツールが利用可能であること（公式要件）。`disableWorkflows` 設定は公式に
  「同じ無効化設定が全 surface に適用される」ため、Step 3b（`claude -p` サブプロセス）も同じ
  設定ファイルを読む限り同様に無効化される（Step 3b なら回避できるわけではない点に注意）。
  無効化されている場合は `/deep-research` 自体（3a/3b とも）が使えないため、DIY（Step 4）へ進む。

#### Step 3b: 自律・バッチ起動時 — `tools/run_deep_research_workflow.py`（`claude -p` サブプロセス）

> 自律起動（Issue ラベル駆動・cross-session 実行）は、既存のレート枠スキップカウンタ（Issue ラベル
> `research-skip:N`）・月次コストログ・Opus 明示指定を使い続けるため、引き続き本ツールを使う。
> **必ず `run_in_background` + heartbeat で監視** し、完走（既定 timeout 5400 秒＝90分）を待つ。

```bash
python3 tools/run_deep_research_workflow.py {ID}
# 終了コードの意味（echo で握り潰さず、そのまま判定すること）:
#   0=成功(schema OK) / 1=schema NG・normalize失敗 / 2=入力不足 / 3=予算ゲート超過
#   4=本文回収失敗(出力欠落・長さガード発火) / 5=権限不足・実行エラー
#   6=レート枠超過(capacity) → Step 3.6 のスキップ判定へ（DIY に即落とさない）
#   → 1/3/4/5 は Step 4（DIY）へ / 6 のみ Step 3.6（スキップ＋カウンタ）へ分岐 / 2 はユーザー報告
```

- 出力: `content/research/{ID}_deep_research.{md,json}`（`research_schema.json` 準拠・`engine=claude-deep-research-workflow`）。**EXIT=0 なら Step 4 をスキップして Step 5（品質ゲート）へ**。
- **本文回収の仕組み**: `/deep-research` はレポート全文を作業ファイル `deep_research_report.md` へ Write し、ツールは永続 work_dir からそれを harvest して本文の正本にする（`--output-format json` の `result` は最終メッセージしか拾わないため保険扱い）。本文が **3000 字未満** なら出力欠落とみなし **EXIT=4** で即フォールバックへ。
- **🔴 喪失防止チェックポイント（必須運用）**: 検索成功直後、normalize の前に生レポートが `content/research/{ID}_research_raw.md` に保存される（stdout に `[1.5/3] 喪失防止:` と出る）。**この `_research_raw.md` が存在したら、後続処理の前に必ず即コミット＆push する**（L-100: 未コミットは SessionStart で消える）。normalize が落ちて **EXIT=1** でも、生レポートは git に残り `--normalize-only --report content/research/{ID}_research_raw.md` で再検索なし（$0）に再正規化できる。EXIT=0（成功）時はツールが `_research_raw.md` を自動削除する。
- セキュリティ/動作: `--allowedTools` で事前許可（`tools/run_deep_research_workflow.py` 経由時）。**Bash/Write は Workflow のオーケストレーション実行に必須**。インジェクション対策は work_dir をリポジトリ外へ隔離 + 実行後に必ず破棄 + `--max-budget-usd` 上限で緩和（git commit はサブプロセスにさせず親＝本スキルが行う。root では `--dangerously-skip-permissions` 不可）。
- **EXIT=1/4/5（真の失敗）→ Step 4（DIY）へ**。ただし EXIT=1 で `_research_raw.md` が存在する場合は、DIY へ行く前に **まず `--normalize-only` 再試行**（再検索コストを払わず復旧できるため）。
- **🔴 EXIT=4/5 の反復検知（#4699 再発防止）**: `content/pipeline-state/research_fallback_log.jsonl`（ツールが EXIT=1/4/5/6 で自動記録）の直近エントリを確認し、**EXIT=4/5 が異なるリサーチ ID で 2 連続以上記録されていたら、DIY 消化と並行して `type:bug` Issue を起票** する（Claude Code CLI 更新による `/deep-research` 起動インターフェース変化を疑う。旧形式 `/deep-research {質問}` がワークフローとして解釈されなくなり EXIT=4 が恒常化した下流プロジェクトの実障害（#4699）と同型）。エラー出力に `DEEP_RESEARCH_UNAVAILABLE` が含まれる場合は確定（`dr_prompt` / `SEARCH_ALLOWED_TOOLS` の現行 CLI 追従が必要）。
- **EXIT=6（レート枠超過）→ Step 3.6（スキップ判定）へ**。**DIY に即落とさない**。
- **EXIT=2（入力不足）→ ユーザー報告**（プロンプト未生成）。

> モデルは既定 **Opus**（`DEFAULT_ENGINE_MODEL=opus`・エイリアスなので世代交代に自動追随）。コスト/時間削減が必要なら `--max-budget-usd` を下げるか DIY を使う。

### Step 3.6: レート枠超過時のスキップ判定【Step 3 が EXIT=6（レート枠超過・capacity）のときのみ】

> **ポリシー**: ディープリサーチは **必ずネイティブ `/deep-research` で行う**。レート枠超過は DIY に
> 即フォールバックせず「スキップ」して次スロットで再試行し、**連続3回スキップしたときだけ DIY（Step 4）へ** 降りる。
> skip カウンタは対象 Issue のラベル `research-skip:N`（N=1..3）で永続化する。

手順（MCP/gh ラベル操作・NEXT 別アクション表）は `reference.md`「Step 3.6: レート枠超過時のスキップ判定」を参照。

> 🔴 Step 3 が **EXIT=0（成功）したら、Step 6 のコミット前に必ず `research-skip:*` ラベルを全削除** する。

### Step 4: 最終フォールバック（DIY・ウェブリサーチ＝Sonnet 5 + WebSearch）【Step 3（3a/3b）が失敗時のみ】

Step 3 のネイティブ `/deep-research`（対話起動なら 3a 直接呼び出し → 3b `claude -p` の両方、
自律起動なら 3b）が **実際に失敗した場合のみ** 実行する（安易な DIY 直行禁止）。
本セッション自身が WebSearch/WebFetch で調査項目ごとに並列 sub-agent
（Haiku 4.5 推奨）を起動し、`research_schema.json` 準拠 JSON を組み立てて `tools/run_deep_research.py` に引き渡す。
**外部 LLM API（Gemini 等）へのフォールバックは行わない**（Issue #260）。

sub-agent 起動フォーマット・統合手順は `reference.md`「Step 4: 最終フォールバック（DIY）」を参照。

### Step 5: 品質ゲート

`docs/rules/research-rules.md` と `tools/research_schema.json` に基づき判定する。
プロジェクトに lint/URL 検証ツールがあれば併用する（無ければスキーマ準拠の手動チェックで代替）:

```bash
# 任意（存在する場合のみ）
[ -f tools/lint_research_files.py ] && python3 tools/lint_research_files.py {ID}
[ -f tools/validate_fact_check_urls.py ] && python3 tools/validate_fact_check_urls.py content/research/{ID}_deep_research.md
```

判定基準:

| 条件 | アクション |
|---|---|
| `fact_check_flags >= 5 件` | 主エンジン（Step 3）で 1回だけ再リサーチ |
| `rank C >= 1 件` | 同上 |
| `sections < 5` | Step 4 で不足項目だけ追加実行 |
| `sources < 8` | 同上 |
| 上記すべてクリア | Step 6 へ |
| 再リサーチ後も基準未達 | `status:waiting-user` に戻す + 通知（境界外） |

### Step 6: コミット & PR

```bash
git checkout -b claude/{ID}-deep-research-{timestamp}
git add content/research/{ID}_deep_research.{json,md}
git commit -m "[{ID}] research: Deep Research 自動実行完了 (engine={engine})"
git push -u origin claude/{ID}-deep-research-{timestamp}
```

PR 作成（`docs/rules/pr-review-flow-summary.md` の標準フローに従う）:
- タイトル: `[{ID}] research: Deep Research 自動実行`
- 本文: `engine` / `cost_usd` / `search_count` / `rank_distribution` / `fact_check_flags` 件数を明記
- `Closes #{ISSUE_NUM}`

### Step 7: AIレビュー監視 & 自動マージ

`docs/rules/pr-review-flow-summary.md` 標準の subscribe_pr_activity + ハートビートで監視。
レビュアー OK で自動マージ → Issue クローズ → （あれば）次工程の Issue 起票。

### Step 8: コスト記録 & レトロ

- `tools/run_deep_research.py` / `run_deep_research_workflow.py` が `content/pipeline-state/research_cost_log.jsonl` に追記する
- 失敗（フォールバック発動含む）時は `/retrospective` を起動し Try Issue 化する

## 失敗モードと対応

主要な失敗モード（`claude -p` 実行失敗・レート枠超過・品質劣化・月予算超過・同テーマ並行実行・
プロンプト未生成）の検出方法と対応の一覧は `reference.md`「失敗モードと対応」を参照。

## 関連ファイル

- `tools/run_deep_research_workflow.py` — 第2エンジン Step 3b（自律・バッチ起動の一次経路。ネイティブ `/deep-research` を `claude -p` サブプロセス経由で実行 + 正規化。対話起動は Step 3a で `Workflow` ツールから直接呼ぶため本ファイルを経由しない）
- `tools/run_deep_research.py` — オーケストレーション・I/O・コスト監視（DIY ランナー）
- `tools/research_schema.json` — 出力 JSON Schema
- `content/pipeline-state/research_fallback_log.jsonl` — フォールバック発動の記録（可視化）
- `content/pipeline-state/research_cost_log.jsonl` — コスト記録
- `docs/rules/research-rules.md` — リサーチ品質基準
- `reference.md` — フォールバック分岐（Step 3.6/4）・失敗モード一覧の詳細
