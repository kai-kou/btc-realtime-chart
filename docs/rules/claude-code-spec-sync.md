# Claude Code 仕様変更追随レーン（claude-code-spec-sync・SSOT）

> **このファイルは「Claude Code 本体のバージョンアップに本プロジェクトの内部資産（ルール・スキル定義・ハーネス・設定）を追随させる仕組み」の唯一の正本（SSOT）である。**
> ユーザー明示指示（2026-07-17・Issue #264・下流プロジェクトのレーンを汎用化して移植）に基づき新設。
> タスク依存ルール（`.claude/rules/` symlink なし・`claude-code-spec-sync` スキルが起動時に Read する）。

## 0. 目的

Claude Code はアップデート頻度が高く、破壊的変更が既存のフック・スキル・設定・運用ルールを
サイレントに壊すことがある（実例: L-101 ツールコールパース退行 / L-106 CLAUDE_ENV_FILE 肥大 /
Opus 4.8 の effort デフォルト変更）。なお **新モデル世代のリリース自体は原則として追随作業が不要** （運用ファイルのモデル指定はエイリアス既定・`agent-team.md`「モデル指定の方針」）。追随が要るのは エイリアスの解決先を記した参考表と、effort 既定や API 仕様の破壊的変更のみ。本レーンは定期的にバージョンアップをキャッチアップし、
**内部資産の仕様追随** を自律実行する。

- 検知対象は `anthropics/claude-code` の releases/changelog のみ（新モデル発表等は対象外）
- 本レーンは **発信しない**（内部資産の追随専用。対外発信レーンを持つ派生プロジェクトは
  そちらと Issue ラベルで分離する。dedup は `lane:claude-code-spec` 内で完結）

## 1. アーキテクチャ（検知と対応の分離・2つの速度）

```
定期実行（R-1 ルーティンのプリフライト。実行スケジュールの定義は開発リポジトリの運用メモに保持）または手動 /claude-code-spec-sync
  ↓
tools/check_claude_code_updates.py --create-issue   ← 検知（LLM 非依存・軽量）
  ├─ 新バージョンなし（exit 10）→ 何もしない
  ├─ 新バージョンあり（exit 0）→ 分類とは独立に tools/probe_permission_prompts.sh を実行（権限プローブ・実挙動の実測）
  │    exit 1 → [CC-Sync][破壊的変更] 権限プローブ検知 Issue を追加起票（Step 1 へ）/ exit 2 → 判定不能を Issue に明記
  ├─ 破壊的変更を検知 → [CC-Sync][破壊的変更] Issue 起票 + "BREAKING_DETECTED" 出力
  │    → 同セッションが claude-code-spec-sync スキル Step 1 を即実行（即対応レーン）
  │       影響調査（横断 Grep）→ 公式裏取り → 最小差分修正 → PR → L1 レビュー → マージ
  └─ 新機能・新設定を検知 → [CC-Sync][検証] Issue 起票（検証チェックリスト付き）
       → スキル Step 2 が 1 回 1 件 で消化（検証・検討フェーズ）
          公式裏取り → 適用価値評価 → 採用 / 要議論（議論型レビュー）/ 見送り
```

- **破壊的変更 = 即対応**: 「これまでのやり方がエラーになる」類は放置日数がそのまま障害リスクになる。
  検知した日のセッションがマージまで完遂する
- **新機能・新設定 = 検証・検討フェーズ必須**: 検知即反映は禁止。Issue 上でチェックリストを消化し、
  採用判定を経てから反映する（新機能の既知バグ（L-101 型）を掴まないための安全弁）

## 2. 分類基準（tools/check_claude_code_updates.py が機械分類）

| 分類 | 判定（changelog 行のキーワード・`config/claude_code_spec_sync.yaml` が正本） | 出口 |
|------|------|------|
| **破壊的変更** | breaking / deprecated / removed / no longer / renamed / now requires / dropped / default changed / incompatible 等 | `[CC-Sync][破壊的変更]` Issue → 即対応 |
| **新機能・新設定** | added / new / introducing / support for / you can now / experimental 等（breaking 非該当のみ） | `[CC-Sync][検証]` Issue → 検証・検討 |
| その他（バグ修正等） | 上記いずれにも非該当 | 起票しない（ログのみ・バージョンは既知化） |

- 機械分類は取りこぼし側に倒す（誤って「その他」に落ちた破壊的変更は、障害として顕在化した時点で
  L-077 プロトコル + 本レーンのキーワード辞書更新で回収する）
- **キーワード辞書で拾えない挙動変化は権限プローブが補完する**（`tools/probe_permission_prompts.sh`・新バージョン検知時に
  分類結果に関わらず実行）。v2.1.260 の "Reverted the 2.1.259 change applying Read() deny rules to Bash arguments" 行は
  どの辞書にも一致せず「その他」に落ちて影響なしと記録された（#558）。辞書の追加はいたちごっこになるため、
  権限・deny・Bash 引数の領域は実測で検知する
- **役割分担（#561・棚卸し結果）**: 「辞書でしか拾えない領域」と「実測プローブで補完できる領域」を切り分ける。
  - 実測プローブで補完できる領域（権限・deny・Bash 引数など、`tools/probe_permission_prompts.sh` が実行して
    挙動を直接観測できるもの）は、辞書の同義語を無限に追加し続けない。プローブの検知結果を正とする。
  - 辞書でしか拾えない領域（プローブが存在しない仕様変更全般。UI 文言・設定キー名の変更・非権限系の
    挙動変更等）は、**revert 系の同義語をまとめて先取りする** ことで取りこぼしを減らす。
    `breaking_keywords` に "revert" だけでなく `rolled back` / `restored the previous` /
    `restored the prior` / `undid the` を追加済み（v2.1.260 の "Reverted ..." 行を教訓に、動詞の
    言い換えパターンを一括収録）。**動詞原形（"undo" / "roll back" / "rolling back"）は追加しない**
    （undo/redo・rollback は UI 機能の一般名詞でもあり、「Added an undo button」「Added the ability to
    roll back recent edits」のような新機能紹介文と衝突して誤検知率が上がるため、過去形・受動態の
    フレーズ限定にとどめる。レビューで実際に "roll back" が誤検知することを確認済み）。
  - **「その他」に分類された行も、辞書には未一致でも `project_area_hints` に一致すれば
    `others_hinted` として保持し、起票される Issue 本文に「⚠️ 要精読」セクションで注記する**
    （`tools/check_claude_code_updates.py` の `others_hinted`。起票されない「その他のみ」バージョンでも
    stderr へ警告ログを残す）。分類（起票するかどうか）には使わない＝取りこぼし側に倒す方針は変えず、
    見落としを **後から発見できる形で残す** ことが目的（以前は `others_count` の数だけが残り、
    どの行が見落とされたか追跡できなかった）。同一バージョンが breaking と feature の両方を持つ場合は
    `others_hinted` を breaking Issue 側にのみ掲載し、feature Issue 側は参照だけ残す（両方に丸ごと
    重複掲載すると二重にトリアージすることになるため）。
- 例外: 「Fixed ...」で始まる行はバグ修正のため、明示的に "breaking" を含まない限り「その他」へ
  デモートする（"no longer" / "removed" を本文に含むだけの修正行の誤検知が多いため・2026-07-17 実測）
- 🔴 **記録の「N件」＝ツールが breaking 判定した件数**（CHANGELOG 全行の精読ではない）。v2.1.257 で
  「3件を横断確認済み」と記録しながら `breaking_keywords` 不一致の関連変更 4 件が欠落し矛盾指摘された（#548）。
  記録時は「N 件」の意味を明示し、関連する Fixed/Added 行が別途見つかった場合はそちらへの参照を添える
- 🔴 **クラウドセッションでの既知化漏れ（実例: v2.1.225・#457）**: `gh`/REST が両方不可な環境では
  `--create-issue` の起票が失敗し、当該バージョンの dedup キーが state から自動的に取り消される
  （次回リトライ用の設計）。エージェントが `mcp__github__*` で Issue 対応を out-of-band に完遂しても
  この取り消しは覆らないため、**対応完了後は必ず `--mark-known {バージョン}` を実行してから
  state 変更をコミットする**（`.claude/skills/claude-code-spec-sync/SKILL.md` Step 1 項目 6 / Step 3）。
  省略すると同一バージョンが以後の全 R-1 サイクルで無限に再検知される
- 検知経路は releases.atom が一次、`raw.githubusercontent.com` の CHANGELOG.md がフォールバック
  （クラウドプロキシはスコープ外リポジトリの github.com を 403 にするが raw は通る・実測）。
  経路差による二重検知はバージョン単位 dedup キー + Issue 本文マーカーで防ぐ
- プレリリース（rc/beta/preview 等）は検知対象外
- 影響領域ヒント（hooks/skills/settings/mcp/headless/model 等）を Issue 本文に注記する（調査の起点）

## 3. 追随対象資産（影響調査の横断 Grep 対象）

| 資産 | パス |
|------|------|
| ルール | `CLAUDE.md` / `docs/rules/*.md`（symlink 先 `.claude/rules/` 含む） |
| スキル定義 | `.claude/skills/*/SKILL.md` / `.claude/commands/` |
| ハーネス | `.claude/hooks/*.sh` / `.claude/agents/*.md` / `.claude/output-styles/` |
| 設定 | `.claude/settings.json`（env・permissions・sandbox・hooks 登録） |
| headless 経路 | `tools/*.py` の `claude -p` 呼び出し（run_deep_research_workflow.py / run_discussion_review.py / native_fallback.py 等） |

## 4. 検証・検討フェーズの判定基準（新機能・新設定）

- **CP-5 貢献**: `docs/project-mission.md` のミッション・KPI への効果があるか
- **CP-6 自律性向上**: ユーザー介入・確認を減らせるか
- **コスト**: トークン・実行時間・保守負担
- **リスク**: 退行・既知バグ（`claude-code-optimization.md` の CC-BUG-NN と突合）・挙動の一貫性

判定は 3 択: **採用**（反映 PR + `claude-code-optimization.md` 記録）/ **要議論**（挙動・設計への影響大
→ `discussion-review` スキル（フォールバック: `tools/run_discussion_review.py`）で採否判定）/
**見送り**（理由 + 再検討条件をコメントしてクローズ）。

## 5. スケジュール・ラベル・成果物

| 項目 | 値 |
|------|-----|
| 実行頻度 | R-1 ルーティンのプリフライト（4 時間ごと・dedup により実質日次相当・具体的なスロット定義は開発リポジトリの運用メモが保持）＋手動 |
| 検知ツール | `tools/check_claude_code_updates.py`（設定: `config/claude_code_spec_sync.yaml`） |
| state | `config/claude_code_spec_state.json`（dedup 用・**コミット対象**。初回実行時に無ければ自動生成される） <!-- refcheck:ignore --> |
| Issue ラベル | `lane:claude-code-spec` + `type:improvement` + `status:waiting-claude` + `sp:2` |
| 対応スキル | `.claude/skills/claude-code-spec-sync/SKILL.md` |
| 採用の記録先 | `docs/rules/claude-code-optimization.md`「バージョン差分ログ」セクション |

> `[CC-Sync]` Issue は `type:improvement` + `status:waiting-claude` を持つため R-1 の消化対象にも
> 自然に乗る。R-1 が `[CC-Sync]` プレフィックスの Issue を選んだ場合は、`self-improvement-loop` では
> なく本レーンのスキル（Step 1 / Step 2）に従って消化する（R-1 の具体手順は開発リポジトリの運用メモ参照）。

## 6. ガードレール（不変）

- `.claude/settings.json` の `permissions.deny` / A-1〜A-6 境界（`user-confirmation-minimization.md` §1）に
  触れる変更は自動反映しない → 検証 Issue を `status:waiting-user` に倒してユーザー判断へ
- main 直 push 禁止（即対応でも PR 経由）/ サーキットブレーカー 2 サイクル（A-4）
- 公式一次ソース（changelog / code.claude.com/docs / anthropic.com）で裏取りできない変更は反映しない
- 同種の破壊的変更が 2 回以上再発 → Lv3 フック昇格を検討（`harness-escalation.md`）

## 7. 関連ファイル

| ファイル | 関係 |
|---------|------|
| `tools/check_claude_code_updates.py` | 検知ツール（本レーンの入口） |
| `config/claude_code_spec_sync.yaml` | 検知設定（キーワード辞書・ラベル・dedup） |
| `.claude/skills/claude-code-spec-sync/SKILL.md` | 対応フロー（即対応 / 検証・検討） |
| `docs/rules/claude-code-optimization.md` | 採用・対応の記録先（バージョン差分ログ・CC-BUG-NN） |
| `docs/rules/native-fallback-rules.md` | 隣接機構（Web 未提供機能のフォールバック。本レーンは仕様追随が責務） |

> R-1 プリフライトへの組込み（定期実行の配線）は、下流プロジェクトごとの運用メモ（開発リポジトリ側で保持）に定義する。
