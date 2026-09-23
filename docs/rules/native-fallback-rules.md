# ネイティブ優先フォールバックルール（native-first / claude -p fallback・SSOT）

> **このファイルは「CLI と Web のリリースギャップで未提供の機能を claude -p で代替する際の判定・実行・撤去」の唯一の正本（SSOT）である。**（Issue #198）
> Warm 層（タスク依存 Read）: claude -p フォールバックを実装・実行するスキル/ツールを書くとき、
> および新機能のギャップに遭遇したときに Read する。
> 機械実装は `tools/native_fallback.py` + `tools/native_capabilities.json`（レジストリ）。

---

## 0. 目的・背景

Claude Code の CLI と Claude Code on the web では新機能のリリースタイミングがずれることがある
（例: ビルトイン `/deep-research`・ネイティブ Agent Teams は CLI 先行だった）。
過去は「Web で未提供の機能を `claude -p`（headless CLI サブプロセス）で代替」する個別実装を都度作り、
**Web が追いついた際に撤去作業（コード・ドキュメントの書き換え）が発生** していた。

本ルールはこれを次の設計で解消する:

1. **判定はランタイム（毎セッション）に行う** — 「この機能はネイティブで使えるか」を実行時に検出するため、
   Web が追いついたら **コード変更なしで自動的にネイティブ経路へ切り替わる**。
2. **claude -p 実行は共通ラッパーに一本化** — cwd 隔離（L-100）・env 除去・タイムアウト・退避ログを
   個別実装に再発明させない。
3. **capability はレジストリで台帳管理** — どの機能がどの分類（§1）にあるかを 1 箇所で追跡し、
   撤去判断・派生リポ展開の単位にする。

## 1. capability の 4 分類（レジストリ `category`）

| 分類 | 意味 | claude -p の位置づけ |
|------|------|--------------------|
| `native-default` | ネイティブが既定経路（Web 追いつき済み or 最初からネイティブ） | 休眠フォールバック（ネイティブ失敗時のみ） |
| `gap-fallback` | ネイティブが本環境で **未提供**（Web ギャップ現役） | 暫定の一次経路。probe/実試行が通り次第、自動的にネイティブへ切替 |
| `isolation-by-design` | 隔離セッション・タイムアウト制御等が **本質的に必要** | 設計上の恒久一次経路（ギャップ代替ではない。自動切替・撤去の対象外） |
| `substrate` | フォールバック基盤そのもの（claude CLI / `-p` フラグ）の可用性 | — |

**現状の台帳（2026-07-11 全数棚卸し・Issue #198 事実確認）**: `gap-fallback` は **0 件**。
`claude -p` を一次経路とするものは `deep-research-batch` / `skill-eval`（いずれも isolation-by-design）のみで、
「Web ギャップ代替として claude -p が残っている箇所」は存在しない。
詳細は `tools/native_capabilities.json` と `python3 tools/native_fallback.py probe --all` を参照。

## 2. capability 判定手順（ランタイム検出）

判定は **実行するセッションの中で毎回** 行う（結果をコード・ドキュメントに固定しない。これが自動切替の要）。

| 対象 | 判定方法 |
|------|---------|
| セッションツール（`SendMessage`・`Workflow` 等の deferred ツール） | `ToolSearch "select:<ツール名>"` でロードできれば native 可 |
| プロジェクトスキル | `.claude/skills/<name>/SKILL.md` の存在（probe が機械判定） |
| ビルトインコマンド（`/deep-research` 等） | **まずネイティブで実試行する（try-first）**。失敗・未提供エラー時のみフォールバック。事前列挙は不要 |
| claude CLI・フラグ・env | `python3 tools/native_fallback.py probe <id>` の機械プローブ |

```bash
python3 tools/native_fallback.py probe discussion-review   # 単体判定（JSON 出力）
python3 tools/native_fallback.py probe --all               # 全 capability の一覧
```

probe の exit code（`probe <id>` 単体判定時。`--all` は一覧表示専用で常に 0）: `0` = native 利用可
（または by-design で判定不要）/ `3` = セッション内プローブが必要（出力 JSON の `probes[].detail` の
指示に従い ToolSearch・実試行で確定させる）/ `4` = native 不可 → フォールバックへ / `2` = 引数・レジストリ異常。

> **原則は try-first**: probe は「機械的に分かる範囲の事前判定」であり、セッションツール・ビルトイン
> コマンドの最終確定は **ネイティブをまず試すこと** で行う。試行そのものが最も正確な検出である。

## 2.5. 起動経路 ladder（invocation route ladder・#372）

> 🔴 **「ネイティブが使えるか」だけでなく「ネイティブを *どのツールから* 呼ぶか」も動く。**
> ビルトイン機能は bundled skill ⇄ bundled workflow ⇄ built-in command の間で再分類され、
> `disable-model-invocation` が後から適用されることもある（実例: `/deep-research` は CLI **v2.1.218** で
> `Skill` ツール経由の起動が塞がれ、`Workflow` ツール経由だけが通るようになった・#370）。
> **SSOT に単一の起動形（`Skill(...)` 等）を固定記述しない。** 経路は台帳の `native.routes` に順序付きで持つ。

### ladder の段（上から順に試す）

| 段 | 経路 | 実行形 | 主な失敗の意味 |
|----|------|--------|--------------|
| 1a | `skill` | `Skill(skill="<name>", args=...)` | `disable-model-invocation` → モデル起動が禁止された。1b へ |
| 1b | `workflow` | `Workflow({name: "<name>", args: ...})` | 承認ゲート（`Review dynamic workflow before running`）で止まる → 2 へ |
| 1c | `session-tool` | `ToolSearch "select:<Tool>"` → 直接呼び出し（`SendMessage` 等で同等構成を手組み） | ツール未露出 → 2 へ |
| 2 | `claude-p` | `tools/native_fallback.py headless ...`（既存の専用ランナーでも可） | CLI 不在・timeout → 3 へ |
| 3 | `final` | スキル固有の最終手段（fan-out・DIY・次スロット再試行） | — |

`kind` の語彙は `skill` / `workflow` / `session-tool` / `builtin-command` / `cli`（`builtin-command` は
スラッシュコマンドを直に打つ経路、`cli` は claude 以外の CLI を直接叩く経路）。`status` は
`preferred`（最初に試す・**必ず 1 つ置く**）/ `available` / `unavailable`（既知の不可）/ `unknown`。
語彙の正本は `tools/native_fallback.py` の `VALID_ROUTE_KINDS` / `VALID_ROUTE_STATUS`。

現行の試行順と降格条件は台帳から機械出力できる（**実行前にこれを見る**）:

```bash
python3 tools/native_fallback.py routes deep-research-interactive   # 単体
python3 tools/native_fallback.py routes --json                      # 全 capability + 降格シグネチャ
```

### 降格シグネチャ（実行結果にこの文字列が出たら次段へ）

判定ロジックの正本は `ROUTE_DEMOTION_SIGNATURES`（`tools/native_fallback.py`）。要点:

- `disable-model-invocation` / `cannot be used with Skill tool` → **skill 経路が閉じた**。workflow 経路へ
- `Review dynamic workflow before running` → **非対話セッションで承認できない**。`--allowedTools` に `Workflow` を含めた claude -p へ
- `No such command` / `not found` → **改名・撤去の可能性**。次段を試しつつ `claude-code-spec-sync` レーンで公式 changelog を確認する

### 経路が動いていたと判明したときの後始末（必須）

1. 台帳（`tools/native_capabilities.json`）の該当 `routes` を更新する。**閉じた経路は削除せず
   `status: "unavailable"` + `detail` に「いつ・どのバージョンで・どのエラーで閉じたか」を残す**
   （次にバージョンが動いたときの手掛かりになる）。新しい経路を `status: "preferred"` にする。
2. 利用側 SKILL.md の起動手順を ladder 記述に合わせる（単一経路を書き直すのではなく、段階降格として書く）。
3. `python3 tools/native_fallback.py --self-test` が PASS することを確認する（`native` を持つ
   capability の先頭段が native であることを機械検査している）。

## 3. フォールバック実行の標準 3 ステップ（サイレント禁止）

claude -p フォールバックを持つ処理は、必ずこの順で実行する:

```
Step 1: ネイティブを試す。**単一経路で諦めず §2.5 の ladder を上から順に降格する**
        1a. Skill ツール      Skill(skill="<name>", args=...)
        1b. Workflow ツール   Workflow({name: "<name>", args: ...})
        1c. セッションツール   ToolSearch "select:<Tool>" → 直接呼び出し（同等構成の手組み）
        （試す順は `python3 tools/native_fallback.py routes <id>` が台帳から出力する）
Step 2: ネイティブ経路が全滅 → 退避理由を 1 行ログ（Issue/PR コメント or stderr）→ claude -p へ
        新規実装は共通ラッパーを使う:
        python3 tools/native_fallback.py headless \
          --capability <id> --reason "<ネイティブが使えなかった理由>" \
          --prompt-file <file> --model <model> --timeout <sec>
Step 3: claude -p も失敗 → スキル固有の最終手段（fan-out・DIY・次スロット再試行等）へ退避
        （最終手段はレジストリの fallback.final に記録する）
```

- **全段でログ必須**（サイレントフォールバック禁止・agent-team-summary.md と同一規範）。
  ラッパーは `[native-fallback]` 行を stderr に必ず出す（機械支援）。
- ラッパーの安全対策（個別実装に再発明させないための一本化ポイント）:
  - **cwd = 一時ディレクトリ隔離**（子セッションの SessionStart フックによる未コミット破壊防止・L-100）
  - **`CLAUDECODE` env 除去**（セッション内ネスト起動の許可）
  - **サブスク認証既定**（`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` を除去。`--no-subscription` で API 従量へ。
    `--max-budget-usd` は API 従量経路のみ有効）
  - **タイムアウト既定 600 秒**（超過は exit 124）・長文プロンプトは stdin 渡し（argv 長制限回避）
- 既存の専用ランナー（`run_discussion_review.py`・`run_deep_research_workflow.py`・skill-creator scripts）は
  同等の安全対策を実装済みのため **書き換え不要**（本ルールの準拠実装として扱う）。新規のフォールバックは
  専用ランナーを作らずラッパーを使う。
- 上記「準拠実装」扱いの既存ランナーが `--self-test` 実行時にドリフト（cwd 隔離・timeout 等の後退）していないかを
  `native_fallback.py` の `check_runner_equivalence()` が静的検査する（Issue #200）。現状で未実装の項目
  （例: 専用ランナーの `CLAUDECODE` 除去・skill-creator scripts の cwd 隔離）は既知の許容ギャップとして
  検査対象に含めていない。安全対策コードを変更したら `EQUIVALENCE_TARGETS` の `checks` も合わせて見直すこと。

## 4. Web が追いついたとき（自動切替と撤去判断)

1. **何もしなくてよい**: §2 の判定がランタイムのため、ネイティブの試行が成功するようになった時点で
   自動的にネイティブ経路が使われる（`gap-fallback` の claude -p は自然に休眠する）。
2. **台帳を更新する（次に触るセッションでよい）**: レジストリの `category` を `gap-fallback` →
   `native-default` へ変更し、`notes` に切替確認日を記す。
3. **撤去は任意**: 休眠フォールバックの削除は義務ではない（残しても自動切替に影響しない）。
   削除する場合は安定運用（目安: ネイティブ実行 5 件 or 2 週間）を確認後、
   `modules.yaml` の追跡更新 + 派生リポ告知 Issue とセットで行う
   （撤去手順の詳細は開発リポジトリの提案記録 §6 Phase 3 に保持）。

## 5. 新しいギャップ機能を追加するとき

CLI 新機能が Web で未提供と判明したら（実試行の失敗で検出したら）:

1. `tools/native_capabilities.json` に `category: "gap-fallback"` でエントリ追加
   （`native.routes` に **起動経路 ladder**（§2.5・`kind` / `how` / `status` 必須。`preferred` を 1 つ置く）・
   `native.probes` に判定方法・`fallback.how` に claude -p 実行形・`fallback.final` に最終手段）。
2. 利用側 SKILL.md に §3 の標準 3 ステップを記述（ネイティブ経路 ladder の降格 → ラッパー呼び出し → 最終手段）。
3. 配布物が増えた場合は `modules.yaml` の該当モジュールに追跡を追加。
4. `python3 tools/native_fallback.py --self-test` が PASS することを確認。

## 6. 派生リポジトリへの展開

本リポジトリをベースに `apply-base` / `scripts/apply-to-repo.sh` で運用する派生リポジトリ向け。

- **配布**: `native-fallback` モジュール（`modules.yaml`）として本ルール・ツール・レジストリが
  自動配布される（通常の再適用「claude-code-base を反映して」だけで反映）。
- **派生側の独自 capability**: `tools/native_capabilities.json` は **直接編集しない**
  （ベース同名ファイルは再適用で上書きされるため）。派生側は
  **`tools/native_capabilities.local.json`（配布対象外・上書きされない）** に同スキーマで記述する。 <!-- refcheck:ignore -->
  probe/一覧は base + local を id 単位でマージし、**local が優先** する
  （ベースエントリの category を派生側事情で上書きすることも可能）。
  `.local.json` は **git にコミットして永続化する**（gitignore しない。クラウド環境では
  未コミットファイルはセッション間で消えるため、ignore すると台帳が毎セッション消失する）。
  ただし **シークレット値は書かない**（env probe は変数名参照のみ。probe 出力も値を表示しない）。
- **claude CLI 不在環境**: GitHub Issue/PR 起動のタスクモード等（L-117）では claude CLI 自体が
  使えない場合がある。`probe claude-headless` が fallback 判定を返す環境では claude -p 段を飛ばし、
  スキル固有の最終手段へ直行する。
- **展開時の注意全般**（加算的コピー・prune の限界・告知は Issue で行う）の経緯は
  開発リポジトリの提案記録 §6 に保持している。

## 7. 完了・成功の定義

- [ ] ネイティブの **起動経路が `native.routes` に順序付きで台帳化** され、SSOT に単一の起動形が固定記述されていない（§2.5）
- [ ] 閉じた経路が削除ではなく `status: "unavailable"` + 閉じた経緯付きで残っている
- [ ] claude -p フォールバックの判定がランタイム検出（try-first / probe）で行われている
- [ ] 新規の claude -p 実行が共通ラッパー経由（cwd 隔離・退避ログ強制）になっている
- [ ] フォールバック発生時に退避理由 1 行が記録されている（サイレント禁止）
- [ ] capability がレジストリに台帳登録され、`--self-test` が PASS している
- [ ] Web 追いつき時にコード変更なしでネイティブへ切り替わる（撤去は任意・台帳更新のみ）

## 8. 参照

| ドキュメント | 関係 |
|------------|------|
| `tools/native_fallback.py` / `tools/native_capabilities.json` | 本ルールの機械実装（probe・headless ラッパー・台帳） |

> Web 追いつきの実例（議論型 claude -p → ネイティブ移行）・派生展開・Phase 3 撤去手順の経緯は開発リポジトリの提案記録として保持している。
| `.claude/skills/discussion-review/SKILL.md` | native-default の準拠実装（ネイティブ既定 + claude -p 休眠フォールバック + fan-out 最終退避） |
| `.claude/skills/research-runner/SKILL.md` | isolation-by-design の準拠実装（Step 3b・エンジン選択ポリシー） |
| `docs/rules/agent-team-summary.md` | フォールバック連鎖のログ必須規範（サイレント禁止の出典） |
| `docs/rules/lessons-core.md` L-100 / L-117 | cwd 隔離の根拠 / claude CLI 不在タスクモードの制約 |
