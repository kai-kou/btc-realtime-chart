---
name: audit-runner
description: 外部の監査プロトコル（Claude Code セットアップ監査プロンプト等）を実行のたびに取得して忠実に実行し、指摘を議論型レビューで精査、採用と判断した分だけ Issue 化 → 実装 → PR → マージし、最後に同一プロトコルで再監査して before/after の変化を報告する。「セットアップを監査して」「監査プロトコルを実行して」「audit-runner」「/audit-runner」と依頼された時、または定期実行スロットから起動する時に使用する。監査スコアを鵜呑みにせず指摘ごとに採否を判断するのが本スキルの中核。Agent Skills 資産の構造監査は skill-audit、Issue/PR の状態監査は workflow-health-check が担当するため、本スキルは外部プロトコルによるセットアップ構成監査に限定する。
compatibility: Bash（curl でプロトコル取得・監査コマンド実行）, WebFetch/WebSearch（公式仕様の一次情報確認）, discussion-review スキル（指摘の精査）, mcp__github__*（Issue / PR 操作）
effort: high
disallowed-tools: AskUserQuestion
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず
> repo スコープ REST が 403 になる。本スキルの GitHub 操作は **`mcp__github__*` が一次経路**。
> 以下に `gh` コマンドが出てくる箇所は、クラウドでは対応する MCP ツールへ読み替える
> （可否マトリクスの SSOT: `docs/rules/github-mcp-fallback-patterns.md`）。

# audit-runner — 外部監査プロトコルの実行レーン

第三者が公開している「Claude Code セットアップ監査プロンプト」のようなプロトコルを取り込み、
**取得 → 忠実実行 → 議論による精査 → 採用分のみ実装 → 再監査** の 1 サイクルを自律実行する。

## 設計思想（このスキルの中核）

**監査スコアは改善度の指標にならない。** 外部プロトコルの配点は著者の価値観であり、対象リポジトリの
実態とは独立している。実測された乖離の例:

- 著者独自のコマンド命名を固定文字列で探すため、機能的等価物があっても 0 点になる
- 著者自身のプロダクト（MCP サーバー等）の導入が加点項目に混ざっている
- クラウド実行環境では揮発する設定の不在が減点される
- **逆に、監査が満点近くを付けた次元にこそ実欠陥が隠れていることがある**（「設定が書かれている」と
  「設定が有効である」を監査が区別していないため）

したがって本スキルは **プロトコルを忠実に実行する（Phase を省略・改変しない）が、指摘の採否は
議論型レビューの verdict で決める**。点数を上げるだけの変更は採用しない。

## 起動トリガー

1. ユーザーが「セットアップを監査して」「監査プロトコルを実行して」「audit-runner」「/audit-runner」等と依頼した時
2. 定期実行スロット（ルーティン）から起動された時（§定期実行の登録）
3. 監査プロトコルの更新を検知した時（前回実行時の版と差分がある場合）

## 設定（起動プロンプト or 環境変数で上書き可能）

| 項目 | 既定値 | 上書き方法 |
|------|--------|-----------|
| プロトコル URL | `https://raw.githubusercontent.com/FlorianBruniaux/claude-code-ultimate-guide/main/tools/audit-prompt.md` | 環境変数 `AUDIT_PROTOCOL_URL` / 起動プロンプトで明示 |
| 成果物の保存先 | `content/audits/<YYYY-MM-DD>/` | 環境変数 `AUDIT_OUTPUT_DIR` |
| 議論ラウンド数 | 2 | 起動プロンプトで明示 |
| 実装まで進むか | 進む（採用分のみ） | 起動プロンプトに「監査のみ」と書けば Step 4 以降をスキップ |

複数のプロトコルを回したい場合は、URL を変えて本スキルを複数回起動する（1 実行 = 1 プロトコル）。

---

## Step 0: ロック取得とプロトコル取得

1. **最初のアクションとして CP-4 論理ロックを取る**（`docs/rules/session-concurrency-rules.md`。
   ロック取得を処理の途中まで遅延させない＝同禁止事項）:
   - まず **他セッションが同じサイクルを走らせていないか確認する**:
     `mcp__github__list_issues`（`status:in-progress`）と `mcp__github__list_pull_requests`（open）に
     同一プロトコルの実行 Issue / PR があれば、**介入せず終了する**（CP-4・L-109）
   - 実行対象の Issue があれば `status:in-progress` へ変更する
   - 無ければ **この時点で** 実行トラッキング Issue を起票する
     （`type:improvement` + `status:in-progress` + `sp:3`。タイトル例
     `improvement: 監査プロトコル実行（<プロトコル識別子>・<YYYY-MM-DD>）`）。
     **採用指摘の個別 Issue は Step 3 で別途起票する**（本 Issue はサイクル全体のロック兼記録）
2. **プロトコルを毎回取得する**（キャッシュしない。更新前提）:

```bash
OUT="${AUDIT_OUTPUT_DIR:-content/audits/$(TZ=Asia/Tokyo date +%Y-%m-%d)}"
mkdir -p "$OUT"
URL="${AUDIT_PROTOCOL_URL:-https://raw.githubusercontent.com/FlorianBruniaux/claude-code-ultimate-guide/main/tools/audit-prompt.md}"
curl -sSL "$URL" -o "$OUT/protocol.md" && wc -l "$OUT/protocol.md"
```

3. `protocol.md` を **全文 Read する**（要約版・WebFetch の要約で代替しない。WebFetch は小型モデルが
   要約するため実行手順が失われる）。プロトコルが定義する Phase 構成・採点基準・報告フォーマットを把握する。
4. 取得に失敗したら `docs/rules/problem-investigation-protocol.md` の 5 ステップを実施する
   （ネットワーク・URL 変更・リポジトリ移動を疑う）。自己解決できなければ Issue 化して終了する。

## Step 1: プロトコルの忠実実行（before 監査）

プロトコルが定義する手順を **省略・改変せずに** 実行する。

- インベントリ収集・各次元の採点・報告フォーマットは、プロトコルの記述どおりに行う
- プロトコルが「特定のスキル/コマンドが導入済みなら委譲せよ」と指示する場合、実際に導入済みかを
  確認してから分岐する（未導入ならプロトコルが指定するフォールバック手順を使う）
- 採点結果は **プロトコルの配点表のまま** 記録する（この時点で自分の判断を混ぜない）
- 結果を `$OUT/before.md` に保存する（スコアカード + 各次元の所見 + 実行モード＝フル/フォールバック）

> **Phase 4（ユーザー承認要求）の扱い**: 多くの監査プロトコルは最後に「変更してよいか」をユーザーへ
> 尋ねる Phase を持つ。本プロジェクトは **PR 作成〜マージを恒久委任済み**（SSOT: `CLAUDE.md`
> 「PR 作成の完全自律化」）であるため、**この承認者を Step 2 の議論型レビューの verdict に置き換える**。
> ただし提案が A-1〜A-6（`docs/rules/user-confirmation-minimization.md` §1）に触れる場合のみ、
> 従来どおりユーザー確認へ回す。プロトコルの Phase 1〜3 は省略しない。

## Step 2: 議論型レビューによる精査（専門チーム）

**指摘の採否は必ず議論型レビューに委ねる**（メインセッションが独断で採否を決めない）。

1. `docs/rules/discussion-whiteboard-rules.md` を Read する。
2. 同梱テンプレートから実行用 spec を生成する。`{{AUDIT_RESULT}}` には監査結果の Markdown
   （引用符・コロン・改行・バッククォートを含む）がそのまま入るため、**必ず JSON パーサ経由で
   置換する**。`sed` 等の素朴な文字列置換は未エスケープの `"` や改行で JSON を壊す:

```bash
SPEC_SRC=".claude/skills/audit-runner/discussion_review_spec.json"   # テンプレート本体は書き換えない
SPEC_OUT="$OUT/discussion_spec.json"
REPO_SLUG="$(git remote get-url origin | sed 's#.*[:/]\([^/]*/[^/]*\)$#\1#; s#\.git$##')"
AUDIT_MD="$OUT/before.md" PROTO_URL="$URL" SLUG="$REPO_SLUG" python3 -c '
import json, os, sys
spec = json.load(open(sys.argv[1], encoding="utf-8"))
repl = {
    "{{AUDIT_RESULT}}": open(os.environ["AUDIT_MD"], encoding="utf-8").read(),
    "{{PROTOCOL_URL}}": os.environ["PROTO_URL"],
    "kai-kou/btc-realtime-chart": os.environ["SLUG"],
}
for field in ("topic", "brief"):
    for k, v in repl.items():
        spec[field] = spec[field].replace(k, v)
spec.pop("_comment", None)
json.dump(spec, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("spec written:", sys.argv[2])
' "$SPEC_SRC" "$SPEC_OUT"
```

   `before.md` の内容に加え、監査に対する一次的な疑問点（どの次元の採点が実態と合っていないと
   感じるか）を `before.md` 末尾に追記してから生成すると、議論の起点が明確になる。
3. `discussion-review` スキルを起動する（ネイティブ Agent Teams が既定。失敗時のみ
   `tools/run_discussion_review.py` へ退避し、退避理由を 1 行ログに残す）。渡すもの:
   - `--spec` = `$SPEC_OUT`
   - `targets` = **リポジトリルートの絶対パス**（監査対象は構成全体）。
     監査結果そのものは brief に埋め込み済みなので `before.md` を targets に含める必要はない
   - `rounds` = 2（既定）／議題 ID = `audit-<プロトコル識別子>-<YYYY-MM-DD>` 形式
4. verdict JSON（`adopt` / `defer` / `reject` / `critical`）を受け取る。

4 レンズの既定構成（テンプレート同梱）: 仕様準拠 / 設計整合 / セキュリティ / コスト対効果。
対象プロトコルの性質に応じてレンズを追減してよい。

## Step 3: Issue 化

verdict の `adopt` と `defer` を Issue 化する（`reject` は起票しない。ただし Step 6 の報告には残す）。

- `adopt` → `status:in-progress` + `sp:N`（verdict の見積もりを使う）。本文に **議論での採用理由** と
  **完了条件** を書く
- `defer` → `status:waiting-claude` + `sp:N`。本文に「起票のみ・実装は別スコープ」と明記する
- `critical` があれば `type:bug`、それ以外は `type:improvement` / `type:feature`
- 作成は `mcp__github__issue_write`（クラウド一次経路）

Issue 本文には必ず **監査プロトコルの URL と実行日**、および **議論記録のパス** を含める
（後から「なぜこの変更をしたか」を追跡できるようにするため）。

## Step 4: 実装 → PR → マージ

`adopt` の Issue を実装する。以降は既存フローに従う:

1. 作業ブランチで実装 → `python3 tools/check_cjk_markdown.py --fix --changed` → 各種チェック
2. `mcp__github__create_pull_request`（本文に `Sprint Goal:` / `sp:N` / `Session-Id:` / `Closes #N`）
3. **Layer 1 セルフレビュー**（`Skill(code-review)`）→ 指摘対応 → マージ（`docs/rules/pr-review-flow-summary.md`）
4. 下流影響（設定ファイル・配線ファイルの変更）があれば、ベースリポジトリ側の移行ノート
   （base-update-notes・下流には配布されず apply-to-repo.sh が実行時に読み上げる）へ同一 PR で追記する

**採用しなかった指摘も PR 本文に理由付きで記録する**（次回監査で同じ議論を繰り返さないため）。

## Step 5: 再監査（after）とスコア差分

マージ後、**Step 1 と同一のプロトコル・同一の手順** で再実行する。

- プロトコルは **再取得しない**（Step 0 で保存した `$OUT/protocol.md` を使う）。同一版で測らないと
  差分がプロトコル更新由来なのか改善由来なのか区別できない
- 結果を `$OUT/after.md` に保存する
- `$OUT/diff.md` に **スコア差分表** と、**スコアに現れない実態の変化** を対比して書く:

```markdown
| # | 次元 | before | after | 差 | 実態の変化 |
|---|------|--------|-------|-----|-----------|
```

**スコアが動かなかった改善を必ず明示する**（監査の測定範囲外で何が良くなったか）。逆に
「点数は上がるが採用しなかった項目」と、その合計点も記載する（採点軸への迎合を避けた記録）。

## Step 6: 完了報告

`docs/rules/completion-report-rules.md` に従う。冒頭に依頼の再掲、次にアウトカム。
**スコアを主役にしない**（「56 → 59」より「サンドボックスが初めて実際に動くようになった」を先に書く）。

成果物一式（`protocol.md` / `before.md` / `after.md` / `diff.md` / 議論記録）をコミットする。

---

## ガードレール

1. **プロトコルを改変しない**: Phase の省略・採点基準の書き換えをしない。合わないと感じた点は
   議論の材料にし、`reject` として記録する（プロトコル側を勝手に直さない）。
2. **スコアのために変更しない**: 議論で `reject` された指摘は、点数が上がるとしても実装しない。
3. **before / after は同一版で測る**: 再監査でプロトコルを取り直さない。
4. **サーキットブレーカー**: 実装の修正サイクルが 2 回を超えたら STOP しユーザー報告（A-4）。
5. **A-1〜A-6 に触れる提案はユーザー確認へ**: 特に課金・アカウント設定（A-6）を要求する指摘は
   自動実行せず、`docs/rules/user-notification-triage.md` §3 の必須要件を満たして通知する。
   本スキルは `disallowed-tools: AskUserQuestion`（定期実行で対話待ちに入らないため）なので、
   確認は **Issue を `status:waiting-user` にして Slack 通知（`tools/slack_notify.py waiting`）** で行う。
   該当 Issue 以外はブロックせず、残りの採用分は通常どおり実装まで進める。
6. **監査対象外への波及を見落とさない**: セキュリティ設定を変えたら、それを「有効な防御」として
   参照している他ドキュメントが desync していないか必ず確認する（Layer 1 レビューの観点に含める）。

## 定期実行の登録（リポジトリごとに選択）

**手動実行のみで運用する場合** は何も登録しない（自然文 / `/audit-runner` で起動）。

**定期実行する場合** は、次のいずれかで登録する:

1. **Routine（クラウド・推奨）**: `mcp__Claude_Code_Remote__create_trigger` で
   `create_new_session_on_fire=true` の Routine を作り、プロンプトに
   「`audit-runner` スキルで監査プロトコルを実行し、議論 → 対応 → 再監査まで完遂する」と書く。
   `permission_mode="auto"` を明示する（未指定時の継承先は API から断定できないため。
   `bypassPermissions` / `dontAsk` は不採用・詳細は `docs/rules/lessons/permissions.md` L-129/L-130）。
   cron は **UTC 指定**（JST から 9 時間引く・`docs/rules/datetime-rules.md`）。
   推奨頻度は **月次**（プロトコルの更新頻度と、1 サイクルのコストに見合う間隔）。
2. **既存ルーティンのスロットに追加**: プロジェクトの運用メモ（本ベースには含まれない
   リポジトリ固有ファイル）にスロットを 1 行追加し、そのスロットから本スキルを起動する。

> 頻度を上げすぎない。監査は毎回サブエージェント 4〜6 体分のトークンを消費する。
> 週次以上の頻度が要るのは、プロトコル側が頻繁に更新される場合だけ。

## 他リポジトリへの展開

本スキルは `.claude/skills/` 配下にあるため `scripts/apply-to-repo.sh` / `apply-base` で下流へ配布される。
展開先で追加設定は不要（プロトコル URL は既定値を持ち、リポジトリ名は実行時に `git remote` から解決する）。

下流固有の調整が要るのは次の 2 点だけ:

- 監査対象に含めたくない領域がある場合、起動プロンプトで除外を明示する
- 定期実行するかどうか（上記「定期実行の登録」）

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `.claude/skills/audit-runner/discussion_review_spec.json` | 議論スペックのテンプレート（4 レンズ・置換前の状態を保つ） |
| `docs/rules/discussion-whiteboard-rules.md` | 議論ホワイトボード規約の SSOT（Step 2 で Read） |
| `docs/rules/pr-review-flow-summary.md` | PR 作成 → Layer 1 レビュー → 自動マージ（Step 4） |
| `docs/rules/user-confirmation-minimization.md` | A-1〜A-6 既約境界外（ガードレール 5） |
| `docs/rules/completion-report-rules.md` | 完了報告の構造（Step 6） |
| `docs/rules/improvement-lane-map.md` | 改善・監査レーンの責務境界（本スキルは単発オペレーション扱い） |
| `docs/rules/problem-investigation-protocol.md` | 取得失敗時の自己解決（Step 0） |
