# Dynamic Workflows 運用ルール（共通ガードレール）

> Claude Code の **Dynamic Workflows**（2026-05 に research preview として登場・2026-07-03 時点の公式ドキュメントには「research preview」表記なし＝GA 相当に整理された可能性。§1 出典参照）を本プロジェクトで安全に運用するための共通ルール。
> 段階導入の追跡は Epic #2586、各候補は子 Issue #2587〜#2593 を参照。
>
> **本ルールは「タスク依存」**（全セッション常駐＝ESSENTIAL_RULES には入れない）。WF を実装・実行・レビューする時にだけ Read する。トークン最適化のため、各 WF 化スキルの SKILL.md に「作業前に本ルールを Read」ステップを記載する。

---

## 1. Dynamic Workflows とは

Claude が **JavaScript のオーケストレーションスクリプトを自分で書き**、サブエージェントを大規模並列実行するランタイム機能。

| 項目 | 内容 |
|------|------|
| 並列上限 | **同時 16 エージェント**（CPU コア数で減少）・**総数 1,000 / run** |
| 中間結果 | スクリプト変数に保持。**メインのコンテキストには最終結果だけ返る** |
| 保存・再利用 | `.claude/workflows/<name>.js` に保存 → `/<name>` コマンド化・`args` で入力を渡せる |
| 起動方法 | `ultracode` キーワード / `/effort ultracode` / バンドル済み `/deep-research` / 保存済み `/<name>` |
| 対応面 | CLI・Desktop・IDE 拡張・`claude -p`（headless）・Agent SDK・スケジュールタスク |
| 要件 | Claude Code **v2.1.154+**・全有料プラン + Anthropic API + Bedrock/Vertex AI/Microsoft Foundry（Pro は `/config` で要有効化） |
| 無効化 | `"disableWorkflows": true`（`settings.json`）／ `CLAUDE_CODE_DISABLE_WORKFLOWS=1`（起動時に読み込み・全 surface 共通） |
| 出典 | [公式Docs](https://code.claude.com/docs/en/workflows)（2026-07-03 Fetch 確認・以後「research preview」表記なし＝ GA 相当に整理された可能性。旧ブログ記事の「research preview」表記は古い） |

### 本プロジェクトの位置づけ

`/deep-research` は **バンドル済み Dynamic Workflow** であり、`research-runner` の主エンジンとして既に実稼働している（`tools/run_deep_research_workflow.py`・V167 で adversarial 検証付きリサーチを実証）。**ゼロからの新機能導入ではなく、実績の延長** として段階導入する。

### 実機検証で確定した実行モデル（2026-06-06 初版・2026-07-03 更新・2026-07-29 更新）

- **2026-07-29 更新（`/deep-research` の起動経路が変わった・実機検証 + 公式ドキュメント再確認・CLI v2.1.220 / クラウド実行環境・#370）**:
  - 🔴 **`Skill` ツール経由の起動は不可になった**。`Skill(skill="deep-research", args=...)` は
    `Skill deep-research cannot be used with Skill tool due to disable-model-invocation` で即失敗する
    （2026-07-03 時点の「`Skill` ツールから直接 invoke 可能」という記録は **現行仕様では成立しない**）。
  - 🔴 **`Workflow` ツール経由は現在も可能**。`Workflow({name: "deep-research", args: "<質問文>"})` で
    run が起動し、複数エージェントが phase 進行することを実機で確認した。**これが対話起動の正しい経路**。
  - 公式の根拠（`code.claude.com/docs/en/workflows` bundled workflows・min-version 注記）:
    *"`/deep-research` runs only when you invoke it. Before v2.1.218, Claude could also start it on its own."*
    → **v2.1.218 でモデル側の自律起動が廃止** された。`code.claude.com/docs/en/skills` の
    `disable-model-invocation` は「Claude がそのスキルを自動ロードするのを防ぐ」設定で、エラー文言と整合する。
  - 公式 CHANGELOG（`anthropics/claude-code` の `CHANGELOG.md` / リリースタグ `v2.1.218`・2026-07-22）の逐語:
    *"Changed `/deep-research` to start only when invoked manually; Claude no longer launches it on its own"*。
    区分は **Removed / Deprecated ではなく Changed** で、廃止されたのは **起動トリガー（自律起動）だけ**。
    コマンド自体は存続し手動呼び出しは有効（本セッションのネイティブ `/deep-research` 実走 97 エージェントで
    end-to-end 動作を確認済み）。2.1.219 / 2.1.220 に撤回・巻き戻しの記載はない。
  - ⚠️ 交絡要因（同バージョンの別変更・切り分け時に見る）: *"Changed skills with `context: fork` to run in the
    background by default; opt out per skill with `background: false`"*。また Desktop の Code タブで
    `/deep-research` が起動しない事象が別途 Issue 報告されており、**「手動呼び出しは常に動く」はサーフェス
    依存で普遍的に真ではない**（仕様変更ではなく個別バグ）。
  - 📌 バージョン帰属の注意: 調査中、WebFetch の要約器が同一 CHANGELOG エントリを 2.1.212 / 2.1.216 / 2.1.217 へ
    誤帰属する事例が複数発生した。**要約経由のバージョン帰属を信用せず、raw `CHANGELOG.md` の行順で照合する**。
  - `claude -p "/deep-research ..."` は内部で `Workflow({name: 'deep-research', args: ...})` に展開されるが、
    **`--allowedTools` 未指定だと `Error: Review dynamic workflow before running`（`user-rejected`）で拒否される**
    （非対話モードでは承認プロンプトを出せないため）。`tools/run_deep_research_workflow.py` が
    `SEARCH_ALLOWED_TOOLS` に `Workflow` を含めているのはこのため。
  - `ListSkills` / `SearchSkills`（claude.ai 側スキル）には `deep-research` は存在しない（0 件）。

- **2026-07-03 更新（公式ドキュメント `code.claude.com/docs/en/workflows` + `/en/commands` を Fetch して事実確認・ユーザー指示による再調査）**:
  - `/deep-research` は `/en/commands` の一覧表で **`[Workflow]`** に分類される（Skill ではない）。「Claude Code includes `/deep-research` as a built-in workflow」が公式の文言。
  - 公式に **「Workflows are available in the CLI, the Desktop app, the IDE extensions, non-interactive mode (`claude -p`), and the Agent SDK」** と明記。クラウド実行環境（本ハーネス・claude-code-remote）は Agent SDK 系統に相当し、**`/deep-research` はメインセッションから `claude -p` サブプロセスを介さず直接呼び出せる**（当時は `Skill` ツールから直接 invoke 可能なことを実機確認していた。**⚠️ この `Skill` 経路は v2.1.218 で廃止され、現在は `Workflow` ツール経由が正しい**（上記 2026-07-29 更新））。`claude -p` サブプロセス経由も引き続き公式サポート対象であり「誤り」ではないが、**唯一の経路ではなくなった**。
  - **モデルは既定でセッションのモデルを使用**（公式: 「Every agent in a workflow uses your session's model unless the script routes a stage to a different one」）。Anthropic 側が `/deep-research` を Opus に固定しているという確認は取れていない。`tools/run_deep_research_workflow.py` が Opus（エイリアス `opus`・世代交代時の追随修正が不要）を使うのは **本プロジェクトが `--model` で明示指定している選択** であり、ネイティブ `/deep-research` 自体の仕様ではない（過去の「Opus orchestrator」という言い回しは本プロジェクト実装の説明として使うこと。ネイティブ機能一般の性質と誤解しないこと）。
  - **手書き `.js` ワークフローも実際に呼び出し可能** と判明（本セッションの `Workflow` ツールが `agent()`/`parallel()`/`pipeline()`/`phase()` の DSL を受け付け、`.claude/workflows/<name>.js` への保存・`/<name>` 化にも対応）。旧記述「手書き `.js` は不採用」は撤回する。ただし `Workflow` ツール自体の説明に **明示的なユーザーオプトインがある場合のみ使う** 制約がある（`ultracode` キーワード／ユーザーが「専門チームを組成して」等ではなく「ワークフローを使って」と明示的に依頼／スキル・スラッシュコマンドがそう指示／ユーザーが特定の保存済みワークフロー実行を依頼、のいずれか）。要探索的タスクへの先回り適用は禁止（`agent-team-summary.md` の議論型 fan-out 判断基準と同様、明示指示が前提）。
  - 制約の再確認（公式）: 同時 **16 エージェント**（CPU コア数で減少）・総 **1,000/run**・**セッション内のみ resume 可**（セッションを終了すると次セッションでは新規実行になる＝再開不可）・`"disableWorkflows": true`（`settings.json`）または `CLAUDE_CODE_DISABLE_WORKFLOWS=1` で無効化可能・`/deep-research` は **WebSearch ツールが利用可能であること** が前提条件。
- **2026-06-23 記録（歴史的）**: 上記確認前の暫定メモ。「移行作業中」としていたが、2026-07-03 の公式ドキュメント確認により直接呼び出し可能な点は確定済みとなった。
- **2026-06-06 実機確認（歴史的記録）**: ハーネスのメインセッションには `Workflow`/`TeamCreate`/`SendMessage` が非露出だったため、**Bash から `claude -p` サブプロセスを起動** する経路が必要だった（ツール存在確認 + 2体チームの peer-to-peer 議論が end-to-end 成立）。`run_deep_research_workflow.py` と同経路。**2026-07-03 追記**: 現在のセッションでは `Workflow` ツールが露出しているため、この制約は解消済み（環境・バージョンにより差がありうる点は留意）。
- 議論の中間結果・履歴は **共有ホワイトボード（Blackboard パターン・git 管理 Markdown）** に集約する（ルール: `docs/rules/discussion-whiteboard-rules.md`。実装は `discussion-review` スキル（ネイティブ・既定）+ `tools/discussion_whiteboard.py` 基盤。`tools/run_discussion_review.py` = claude -p 駆動はフォールバック）。**2026-07-11 追記**: `SendMessage` も deferred ツールとしてメインセッションに露出（実測。詳細は開発リポジトリの提案記録 §1 に保持）。

---

## 2. 既存プリミティブとの使い分け（公式比較）

|  | Subagents | Skills | Agent Teams | **Workflows** |
|---|---|---|---|---|
| 実体 | Claude が spawn する worker | Claude が従う指示 | lead が peer を監督 | **ランタイムが実行するスクリプト** |
| 次の処理を決めるのは | Claude（毎ターン） | Claude（毎ターン） | lead（毎ターン） | **スクリプト** |
| 中間結果の置き場 | コンテキスト | コンテキスト | 共有タスクリスト | **スクリプト変数** |
| 再現単位 | worker 定義 | 指示文 | team 定義 | **オーケストレーション自体** |
| スケール | 数体/ターン | 同左 | 少数の長期 peer | **数十〜数百体/run** |
| 中断 | ターン再開始 | ターン再開始 | teammate 継続 | **同セッション内で再開可** |

**WF だけが持つ真価**: ①中間結果がメインに乗らない（コンテキスト節約） ②**敵対的相互レビューを再現可能なスクリプトとして codify** できる。本プロジェクトの既存 Agent Teams 並列レビューは全て「役割分担型」で、②はまだ `/deep-research` でしか活かせていない。

---

## 3. WF 化すべき / すべきでない判定基準

### 向いている（WF 化候補）

- **敵対的相互レビューが効く**（独立エージェントが互いの指摘を批判検証 → 誤検知削減）
- **read-only 中心**（`acceptEdits` 強制によるファイル競合リスクが低い）
- **中間結果が大きい**（各エージェントに台本全文・複数素材が渡り、メインコンテキストを圧迫している）
- **冪等に再現したい**（`args` 入力で繰り返す品質ゲート・監査）

### 向いていない（WF 化しない）

| 対象 | 理由 |
|------|------|
| audio-pipeline | VOICEVOX への逐次 HTTP が律速。ローカルサーバーが同時リクエストを捌けない |
| video-pipeline（Remotion） | レンダリングが単一プロセス・GPU 律速 |
| metadata-reviewer / video-reviewer | 既に3体で十分・入力が小さく（meta.yaml 1本分）節約効果が薄い |
| retrospective | 軽量 KPT 生成。WF オーバーヘッドが利益を上回る |
| ユーザー入力が途中で必要なフロー | WF は **run 中にユーザー入力を受け取れない**（stage 分割が必要） |

---

## 4. `.claude/workflows/` 運用規約

| 項目 | 規約 |
|------|------|
| 保存場所 | プロジェクト共有は `.claude/workflows/`（リポジトリにコミット）／個人用は `~/.claude/workflows/`（コミットしない） |
| 命名 | `<対象スキル名>.js`（例: `image-visual-review.js`・`script-review.js`）。`/<name>` がコマンド名になる |
| 入力契約 | スクリプトは `args` グローバルで入力を受け取る。本プロジェクトでは原則 **動画 ID を必須 arg** とし、省略時は明示エラーにする |
| 状態記述の同期 | WF スクリプトの挙動を SKILL.md / ルールに自然言語で書く場合、**同一 PR で必ず突き合わせる**（L-094 desync 防止） |
| 競合回避 | 既存の `status:in-progress` ラベル多層防御（CP-4）が **WF 内部の並列エージェントにも適用されるか実機検証** してから本番投入 |

---

## 5. 必須ガードレール（全 WF 共通・厳守）

> プロジェクト技術監修役の Lv3 レビュー WARN を反映。これらを満たさない WF を本番自動運用に載せない。

1. **フォールバック経路**: WF 実行が失敗（EXIT≠0）したら **既存スキル（Agent Teams / 逐次）へ自動退避** する。research-runner の `/deep-research（直接呼び出し）→ claude -p → DIY（ウェブリサーチ）` フォールバックが実証パターン（#2814: ただし `/deep-research` の **レート枠超過 EXIT=6 は DIY に即フォールバックせずスキップ→次スロットで claude -p 再試行** し、連続3回スキップで初めて DIY に降りる＝capacity 制約と真の失敗 EXIT=1/4/5 を区別する。外部 LLM API（Gemini 等）へのフォールバックは Issue #260 で廃止）。フォールバック発動は専用ログに永続記録し発動率を監視可能にする（サイレントフォールバック防止）。
2. **予算ゲート**: WF は同時 16 × 各エージェントのコンテキスト overhead で **トークンが線形増加** する。実行前に **小スコープ（1動画・1ディレクトリ）で token を実測** し、既存の月次 $50 サーキットブレーカーに WF 実行分を計上する（※サブスクリプション経路利用時は実課金が発生しないため計上対象外・virtual_cost_usd に分離記録）。`/workflows` ビューで各エージェントの token を監視し、暴走時は停止する（完了済み分は失われない）。
3. **acceptEdits 競合検証**: WF が spawn するサブエージェントは **常に `acceptEdits` モード** で走り、ファイル編集が自動承認される。並列エージェントが台本 JSON・meta.yaml・timed.json を同時編集する **L-066 型 TOCTOU** が新たな形で起きうるため、**read-only でない WF は実機検証必須**。
4. **進捗保存は小粒度 fan-out + artifact パターンで担保する**: WF の resume は **同一セッション内限定** で、停止時に実行中だったエージェントの成果は保存されず再実行になる。したがって **長い 1 体に任せず、多数の小粒度エージェントへ fan-out** し、各エージェントの成果を **1 成果物 = 1 ファイルの atomic write**（`tools/discussion_whiteboard.py` の `entries/` 方式）で外部化する。詳細は下記「進捗保存の設計則」。
5. **仕様変動リスクは「中」のまま扱う**: 2026-07-03 時点の公式ドキュメントには research preview 表記が無くなっているが、GA 昇格の明示アナウンスは未確認（未確認のまま断定しない・L-113）。**既存スキルを常にフォールバックとして残し**、WF を唯一の経路にしない方針は継続する。仕様変更は `claude-code-optimization.md` の定期リサーチで追跡する。

### 進捗保存の設計則（小粒度 fan-out + artifact パターン・#351）

> 公式の明文（[公式 workflows ドキュメント](https://code.claude.com/docs/en/workflows)）:「An agent that was still running when you stopped isn't saved and starts over on resume, so **a workflow that fans work out across many small agents preserves more progress than one long agent**.」「**Resume works within the same Claude Code session.** If you exit Claude Code while a workflow is running, the next session starts the workflow fresh.」

| 観点 | 内容 |
|------|------|
| **設計則** | 1 体の長時間エージェントに任せず、**小粒度に分割して fan-out** する。完了済みエージェントの結果はキャッシュされるため、粒度が細かいほど中断時に失われる作業が減る |
| **成果物の外部化** | 各エージェントの成果は **1 成果物 = 1 ファイルの atomic write** でリポジトリ側に書き出す（`tools/discussion_whiteboard.py` の `post`＝`entries/` にユニーク名で書き、集約は orchestrator の `render` だけが行う方式）。これにより **プロセス・セッションを跨いでも post 済み分が生存** する |
| **`checkpoint` スキルの射程と限界** | `.claude/skills/checkpoint/` はフェーズ境界での **手動・粗粒度** スナップショット（Issue コメント末尾 JSON）。セッション境界を跨げる利点はあるが、**分岐/合流を持たず、実行中だったサブエージェントの成果は救えない**（ネイティブ resume と同じ弱点）。「checkpoint があるから中断に強い」と誤認しないこと |
| **禁止** | 中間成果をメインのコンテキストにだけ持たせる設計（圧縮・中断で消える）。長時間 1 体に全工程を任せる設計 |

> **根拠と経緯**: Graph エンジニアリング適用検討（#348・詳細は開発リポジトリの提案記録として保持）で、公式設計則と本ベースの実装（whiteboard の atomic write）が結び付いていないため「checkpoint があるから安全」という誤認が残る、と判定されたため明文化した。

### 過大評価への注意（誤解しやすいポイント）

- WF は「セッション圧縮・タイムアウト問題の **本質的解決ではない**」。緩和できるのは **メインセッションのコンテキスト肥大のみ**。各サブエージェントは独自のコンテキスト・トークン予算を持ち、Stream idle timeout（L-055）・Request timed out は依然サブ側で起きうる。
- Sonnet/Opus は 1M コンテキストを持つため、Agent Teams 並列の中間結果がメインに乗っても圧縮は発生しにくい。**「コンテキストが実際に問題になっているか」を計測してから WF 化の優先度を判断** する。

---

## 6. 段階導入ロードマップ

進め方ループ（各候補共通）:

```
実装（WFスクリプト試作・.claude/workflows/ 保存）
  → 検証（小スコープで token 実測・既存スキルと品質/速度比較・ROI 算出）
  → 振り返り（/retrospective で KPT・lessons 反映）
  → フィードバック反映（横展開可否を Epic #2586 に記録・次候補へ）
```

| 優先度 | 候補 | Issue | 備考 |
|--------|------|-------|------|
| P0 | 本ルール策定（土台） | #2587 | パイロットの前提 |
| P1 | image-visual-reviewer | #2588 | パイロット最優先（read-only・依存なし・移行難度低） |
| P2 | script-team-reviewer | #2589 | コンテキスト節約最大・キャラ監修 JS 設計が前提 |
| P3 | self-improvement-loop | #2590 | 敵対ペアで誤 Issue 起票削減 |
| P4 | screenplay-reviewer | #2591 | 修正ループのスクリプト宣言化 |
| P5 | research-runner DIY | #2592 | 低難度・基盤既存 |
| P6 | sns-organic-pipeline | #2593 | A/Bテスト型・複数候補並列下書き |

---

## 7. 参照

| ドキュメント | 関係 |
|------------|------|
| `docs/rules/claude-code-optimization.md` | Claude Code 最新機能リファレンス（本ルールの上位ドキュメント） |
| `docs/rules/agent-team.md` | Agent Teams（WF と使い分ける既存並列プリミティブ） |
| `docs/rules/session-safety-rules.md` | タイムアウト3種別（WF が緩和できる範囲の正確な理解） |
| `docs/rules/lessons-core.md` | L-066（TOCTOU）・L-094（desync）・L-055（Stream idle） |
| `.claude/skills/research-runner/SKILL.md` | `/deep-research`（実稼働中の WF）のフォールバック実証パターン |
| `docs/rules/discussion-whiteboard-rules.md` | 議論ホワイトボード（Blackboard パターン）— WF主導の敵対的相互レビューの記録/履歴基盤 |
