# lessons 運用ルール（肥大化防止の唯一の正本・SSOT）

> **このファイルは「lesson（学習記録）の蓄積・昇格・削除・サイズ管理」の唯一の正本（Single Source of Truth）である。**
> 過去に何度打っても肥大化が止まらなかった根本原因を解消するために新設（Issue #2667・2026-06-06）。
> **常駐させない**（`.claude/rules/` に symlink しない）。lesson を追加・整理するときに Read する。
> Hot 層（lessons-core.md）にルール本文を置くと、それ自体が肥大化（メタ肥大化）するため、本ファイルは Warm 層に留める。
>
> **サマリー＝ポインタ規約（全 `docs/rules/*.md` に適用）**: ある値・リスト・手順の SSOT が別ファイルに存在する場合、
> 派生ファイルはその内容を実体として再掲せず「§N を参照」の 1 行ポインタに留める（完了報告 4 ファイル重複・
> フックイベント一覧二重管理・既約境界外リスト A-1〜A-6 の 3 ファイル重複が同根の drift を起こした反省・Issue #144）。

---

## 0. なぜ肥大化が止まらなかったか（根本原因・専門チーム3並列＋外部リサーチ14本で特定）

過去5回の対策（top15 スコアリング / prune_lessons / lessons-core 抽出 / カテゴリ分割 / archive 移動＋索引）は全て失敗した。理由は3つ。

| # | 根本原因 | 解消策（本ルール） |
|---|---------|------------------|
| **RC-1** | **入口と出口の極端な非対称性**: 追加経路は4つ（retro / retro-try / self-improvement / 手動）で「追記を義務化」、削除経路は1つだけで空振り（`prune_lessons.py` のターゲットが空リダイレクターのまま） | 出口を `lessons_guard.py` に統合し実際に動かす。§3 の「昇格=物理削除」を徹底 |
| **RC-2** | **対策が全て『追加型』だった**: 新ファイル/ツールを足すばかりで、削除メカニズム自体を直す対策がゼロ → 対策自体が肥大化の一部に（メタ肥大化） | 本ルール以降は「足す前に減らす」。新ツール乱立を禁止し `lessons_guard.py` 1本に集約 |
| **RC-3** | **Lv1（ドキュメントに『300行目標』と書くだけ）止まり** で機械的強制がなかった | §4 の Lv3 フック + Lv4 CI でサイズ上限を機械的にブロック（harness-escalation.md 準拠） |

> 外部知見（Generative Agents / MemGPT / Reflexion / Write-Time Gating 等）の結論: **「分割・アーカイブだけでは絶対に止まらない（移動は削除ではない）」**。Write-Time Gating（追記時の摩擦）と Rule Distillation（昇格＝元削除）でしか非対称性は解消できない。

---

## 1. 3 層構造（Hot / Warm / Cold）

| 層 | 実体 | 役割 | サイズ上限 | ロード |
|----|------|------|----------|--------|
| **Hot** | `docs/rules/lessons-core.md` | 全セッション横断で必須・発生で作業停止するクリティカル規範のみ | **350 行 / 15 エントリ（機械強制）** | `.claude/rules/` symlink で常駐 |
| **Warm** | `docs/rules/lessons/<category>.md` | カテゴリ別の詳細レッスン | 上限なし（ただし dedup 推奨） | タスク依存 Read（スキル SKILL.md に記載） |
| **Cold** | git 履歴 | 昇格済み・歴史的エントリ | — | 必要時 `git log` で参照 |

カテゴリ: `pipeline` / `pr-review` / `audio-voice` / `content` / `session` / `sns-youtube` / `agent` /
`agent-delegation` / `meta` / `marketing`

---

## 2. 新規 lesson の追加先（入口）

```
新しい教訓を記録したい
  ↓
全セッション横断で必須？ かつ 発生すると作業が完全停止する？
  ├─ YES → Hot（lessons-core.md）。ただし追加で 350 行 / 15 件を超えるなら §3 で先に減らす
  └─ NO  → Warm（docs/rules/lessons/<category>.md）に追記（デフォルトはこちら）
```

- **デフォルトは Warm**。Hot への追加は「本当に全セッションで必要か」を自問してから（Write-Time Gating）。
- Hot に追加する常駐必須の行動規範には、本文に `**保持理由**: …` を記載する（§3 の prune 誤削除を防ぐ）。

---

### 新しい運用語彙を導入するとき（`docs/CONTEXT.md`）

lesson・ルール・スキルを書くときに **このリポジトリ固有の語**（Hot 層 / 既約境界外リスト /
パイプライン 等）を使うなら、書く前に `docs/CONTEXT.md` を grep して既存表記に揃える。

```bash
grep -n "{使おうとしている語}" docs/CONTEXT.md
```

- **載っている** → `_Avoid_` に自分が書こうとした表記が無いか確認し、正規の表記を使う
- **載っていない** → 既存語の言い換えを新設していないかを確かめる。表記が実際に割れている語だけを
  `docs/CONTEXT.md` に足す（索引は長いほど良いものではない・収録基準は同ファイル冒頭）

この一手間がないと、`docs/CONTEXT.md` は誰も参照しない索引になり、正本側の表記だけが割れ続ける
（`既約境界外リスト` が 4 表記に分裂したのがこのパターン・#490）。

## 3. 昇格 = 物理削除（出口・最重要）

**昇格とは「教訓を実行可能なコード/フック/CLAUDE.md/ルールに落とし込み、元の lesson エントリを物理削除すること」である。**
「archive ファイルへ移動」は総トークン量を減らさない（参照側が読めばコスト増）ため **原則しない**。

```
教訓を昇格先（.py / .sh / .yml / CLAUDE.md / docs/rules/）に実装した
  ↓
昇格日を本文に記載（**昇格先**: … （昇格日: YYYY-MM-DD））
  ↓
30 日経過したら lessons_guard.py prune --apply で Hot 層から物理削除（git 履歴に残る）
  ↓
常駐し続けたい行動規範だけ **保持理由** を付けて残す
```

### lessons_guard.py（出口・機械強制の統合ツール）

```bash
python3 tools/lessons_guard.py check          # Hot 層の上限 + Hot/Warm 横断の L-NNN 番号重複を検証（超過/重複で exit 1・CI/フック用・#340）
python3 tools/lessons_guard.py stats          # 各層の行数・エントリ数・分類を表示
python3 tools/lessons_guard.py prune          # 物理削除候補を表示（dry-run）
python3 tools/lessons_guard.py prune --apply  # 昇格済み実装済み 30 日経過エントリを物理削除
python3 tools/lessons_guard.py dedup          # タイトル類似の重複候補を検出（統合用）
```

- `prune --apply` は対象ファイルに未コミット変更がないときのみ実行する（誤削除時に git 復元できる状態を保証）。
- 本文に `**保持理由**` を含むエントリは分類に関わらず prune されない。

---

## 4. 機械強制（Lv3 フック主軸・Lv4 CI 任意・RC-3 対策）

| レベル | 実装 | 挙動 |
|--------|------|------|
| **Lv3 フック**（主軸・実働） | `.claude/hooks/post-tool-use-validate.sh` | lessons-core.md または Warm 層（`docs/rules/lessons/*.md`）を Write/Edit した直後に `lessons_guard.py check` を実行。上限超過または L-NNN 番号の Hot/Warm 横断重複があれば exit 2 で警告し、是正（prune / Warm 降格 / 番号振り直し）を促す。本ベースの機械強制の **最終ゲート**（#340） |
| **Lv4 CI**（任意・派生環境向け） | Actions 有効な派生プロジェクトのみ | **本ベースは GitHub Actions を運用に使わない**（`.github/workflows/` を持たず、クラウド自律実行 + フックで完結する）ため、Lv4 CI は既定で設けず Lv3 フックを最終ゲートとする。Actions を使う派生プロジェクトは `lessons-core.md` / `tools/lessons_guard.py` の変更時に `python3 tools/lessons_guard.py check` を走らせる workflow を追加してよい |

「ドキュメントに目標を書くだけ」では守られない（過去の失敗）。サイズ上限は Lv3 フックで **機械が物理的に拒否する**。

---

## 5. 定期運用（自律実行）

| タイミング | アクション | 担当 |
|-----------|-----------|------|
| 月木 05:00（self-improvement-loop 発見モード） | `lessons_guard.py stats` で肥大化兆候を確認、`prune` 候補があれば `--apply` | self-improvement-loop |
| 週次（project-sync） | `lessons_guard.py check` と `dedup` を実行、重複統合候補を報告 | project-sync |
| lesson 追加時 | 本ファイルの §2/§3 に従う。Hot 上限近接なら先に prune | 全セッション |

---

## 6. 廃止したもの（メタ肥大化の解消・RC-2）

以下は `lessons_guard.py` に統合・廃止した。新規に lessons 管理ツールを増やさない。

| 廃止物 | 理由 | 後継 |
|--------|------|------|
| `tools/prune_lessons.py` | ターゲットが空リダイレクター `lessons.md` のまま空振りしていた | `lessons_guard.py prune` | <!-- refcheck:ignore -->
| `tools/lessons_scorer.py` + `lessons-top15.md` | 形骸化（symlink なしで誰も読まない・生成停止） | `lessons_guard.py stats` |
| `tools/split_lessons.py` | 一度きりの分割用・役目終了 | （不要） |
| `docs/rules/lessons.md`（26 行リダイレクター） | Warm 層への単なる案内・案内ハブとして内容更新 | 本ファイル + lessons.md（ハブ） | <!-- refcheck:ignore -->

> アーカイブのファイル/ディレクトリ二重化（`lessons-archive.md` と `lessons-archive/`）の整理は別 Issue で扱う（本 PR のスコープは入口/出口/機械強制）。 <!-- refcheck:ignore -->
> ⚠️ なお `docs/rules/lessons-archive/` 配下の各ファイル先頭ヘッダーは旧 `prune_lessons.py --archive` を前提にした記述のまま残っている（現在は stub 化され実行しても何も起きない）。この古いヘッダー記述の修正も上記の別 Issue で扱う。

---

## 7. 完了・成功の定義

- [ ] lessons-core.md が `lessons_guard.py check` で常に上限内（350 行 / 15 エントリ）
- [ ] 昇格時に元エントリが物理削除される（archive へ移動せず git 履歴に委ねる＝総量が増えない）
- [ ] Lv3 フック（`post-tool-use-validate.sh`）が上限超過を機械的にブロックする（Lv4 CI は Actions 利用の派生環境のみ任意）
- [ ] lesson 管理ツールが `lessons_guard.py` 1 本に集約され、形骸化ツールが廃止されている

---

## 8. 参照

| ドキュメント | 関係 |
|------------|------|
| `docs/rules/lessons-core.md` | Hot 層の実体（本ルールの収録基準に従う） |
| `docs/rules/harness-escalation.md` | Lv1→Lv4 昇格の原則（RC-3 の根拠） |
| git 履歴（`git log`） | Cold 層（昇格済みの歴史的記録）。**ファイルとしての実体は持たない**（§1 の 3 層構造に一本化済み） |
| `tools/lessons_guard.py` | 出口・機械強制の統合実装 |
| `CLAUDE.md`「セルフ改善ループ」 | lesson 追加の入口ルール（本ファイルを参照） |
