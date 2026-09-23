# プロンプト自動構造化ルール（prompt-structuring フック・SSOT）

> **このファイルは「ユーザーの生指示を着手前に作業スペックへ自動展開させる UserPromptSubmit フック
> （`.claude/hooks/prompt-structuring.sh`）」の挙動・トグル・スキップ条件の唯一の正本（SSOT）である。**
>
> **Warm 層**（タスク依存・全セッション常駐＝`ESSENTIAL_RULES` には入れない）。挙動サマリーは
> `CLAUDE.md` ハーネス表に、フック仕様の位置づけは `docs/rules/hook-events-reference.md` に併記する。
> 設計判断の議論記録（議題 ID: `prompt-structuring-design-20260709`・Issue #172）は開発リポジトリに保持している（公開リポジトリには含まれない）。

---

## 0. 目的と制約（なぜ「置換」でなく「注入」か）

ユーザーの雑な・簡潔な指示を、着手前に「適切なプロンプト（作業スペック）」へ整形したい。しかし
Claude Code の `UserPromptSubmit` フックは **公式仕様上、生プロンプトの置換ができない**
（"can't replace the prompt"）。フックにできるのは **stdout によるコンテキスト注入** と exit 2 ブロックのみ。

そこで本フックは「置換」ではなく、**構造化ディレクティブ（作業スペックへの展開指示）を注入し、
Claude が着手前に生指示を自分でスペック化する** workaround で自動整形を近似する。注入経路は既存
`user-prompt-submit-guard.sh`（高リスク助言注入）と同じ。

---

## 1. 動作モード（環境変数 `CLAUDE_PROMPT_STRUCTURING`）

| 値 | 挙動 |
|----|------|
| `auto`（既定） | 実質的なタスク指示（アクション動詞を含む）にのみ発火 |
| `off` | 完全無効 |
| `always` | スキップ条件以外の全プロンプトで発火（質問にも注入） |

- 長文スキップ閾値は `CLAUDE_PROMPT_MAX_LEN`（既定 600 文字）で外部化。`auto` で既に詳細な長文は
  整形の価値が薄いため無発火（`always` は長文でも発火）。

---

## 2. スキップ条件（無発火 = exit 0・無出力）

1. 空プロンプト
2. `off` モード
3. スラッシュコマンド（`/` 始まり）← スキル自動ルーティングを阻害しない
4. エスケープ接頭辞（`!` 始まり）← ユーザーがそのターンだけ整形を拒否する明示オプトアウト
5. システム/バックグラウンド注入（`<` タグ始まり・`[SYSTEM NOTIFICATION`・`<task-notification>`・
   `Stop hook feedback`・`<system-reminder>`・`<local-command`）
6. 高リスク入力（main/master 直 push・force push・`rm -rf`・`.env`/秘密情報・`no-verify`・
   `settings.local.json`）← **guard.sh に一元化**。二重バナー防止のため本フックは無発火
7. 短文（CJK を含む場合 3 文字未満 / ASCII のみ 8 文字未満の動的閾値・コードポイント数ベース）
   ← 言語差別を避ける。文字数は UTF-8 ロケールで計測し、POSIX/C 環境のバイト計数崩れを回避する
8. 長文（`auto` のみ・`CLAUDE_PROMPT_MAX_LEN` 超）
9. 純粋な質問（`auto` のみ・アクション動詞なし）← 誤除外は fail-safe 方向のため許容

---

## 3. 発火時に注入される構造化ディレクティブ

- テンプレは **標準 4 項目**（目的 / 成功条件 / 範囲・非対象 / 手順）。単純明快な 1 手順タスク
  （短文・単一節・改行なし）は **2 項目に縮退**（目的 / 成功条件）。
- 「要確認（不可逆リスク A-1〜A-6）」項目は独立させず、**guard.sh の助言に一元化**。
- 注入本文は必ず以下を含む:
  - **スキル優先規則**: 指示が既知スキルの自然文トリガー（例: ディープリサーチ→research-runner、
    ベース反映→apply-base）に該当するなら、テンプレ展開より該当 Skill の起動判断を優先する。
  - **output style 準拠**: 展開は思考内で一度だけ行い、テンプレの見出し・中身をチャット本文に
    出力しない（concise-neko / L-111 内部作業サイレント原則）。
  - **生指示との無矛盾**: 生指示と矛盾する解釈はしない。曖昧点は最も単純な合理的解釈で仮定を
    1 行記録して進める（Think Before Coding）。

---

## 4. 議論型レビューで確定した是正（critical 3 件）

設計は議論型専門チーム（`run_discussion_review.py`）のレビューで **WARN**（基本方針 OK・是正条件付き）。
以下 3 件を実装で是正済み。

| # | critical | 是正 |
|---|----------|------|
| 1 | スキル自然文ルーティングとの衝突（動詞「リサーチ」で deep-research と競合） | 動詞トリガーから「リサーチ/research」を除外 + 注入本文にスキル優先規則を明記 |
| 2 | guard.sh との二重 stdout 注入 | 高リスクパターン検出時は本フックの注入を完全抑制（語順非依存の main+push 共起も抑制） |
| 3 | 短文閾値 12 文字の言語差別性（「実装して」等が誤除外） | CJK 検出で動的閾値（CJK=3 字 / ASCII=8 字・UTF-8 コードポイント数）に切替 |

---

## 5. 完了・成功の定義

- [ ] 実質タスク指示で構造化ディレクティブが自動注入される
- [ ] スラッシュコマンド・純粋な質問・システム通知・高リスク入力で無発火（誤爆なし）
- [ ] 短い日本語タスク（「実装して」等）が言語差別なく発火する
- [ ] output style と非干渉（テンプレ内容をチャットに垂れ流さない文面）
- [ ] `bash -n .claude/hooks/prompt-structuring.sh` が PASS
- [ ] トグル（auto/off/always）と `!` エスケープが機能する

---

## 6. 参照

| ドキュメント | 関係 |
|------------|------|
| `.claude/hooks/prompt-structuring.sh` | 本ルールの実装 |
| `.claude/hooks/user-prompt-submit-guard.sh` | 高リスク助言注入（責務分離・二重注入を本フックが抑制） |
| `docs/rules/hook-events-reference.md` | UserPromptSubmit の stdout 注入・exit code 仕様の検証済み一覧 |
| `docs/rules/output-verbosity-rules.md` | 内部作業サイレント原則（L-111・注入本文が準拠） |
| 開発リポジトリの議論記録（議題 ID: `prompt-structuring-design-20260709`） | 設計の議論型レビュー履歴（Issue #172・公開リポジトリには含まれない） |
