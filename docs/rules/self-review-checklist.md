# セルフレビュー・チェックシート（SSOT）

> **PR 作成前に必ずこのシートを通すこと。** 過去全 PR のレビューコメント全量分析
> （2026-06・inline 指摘 9,565 件 / 889 PR、うち AI レビュアー指摘 5,821 件）から
> 頻出指摘を抽出して作成した「1 枚もの」のチェックシートである。
>
> ⚠️ **セクション 1（台本・コンテンツ）・5（Remotion / TypeScript）等のドメイン固有項目は
> 出自プロジェクト（動画制作）の実例**。自プロジェクトの成果物種別のチェック項目に読み替える
> （汎用的に効くのはセクション構成と「頻出指摘を機械化して 1 枚ものに集約する」運用）。
>
> - **機械チェック**: `python3 tools/self_review_check.py` が一括実行する（フック `pre-pr-create-check.sh` からも自動実行）。構文検査（`bash -n` / Python）・変更ツールの `--self-test` と対応 `tools/test_<name>.sh` の自動実行・CJK 半角スペース・危険パターンを含む（#627 で構文検査と対応テストを追加）
> - **PR 前フレッシュ文脈レビュー**: `self-reviewer` Step 3.5（`Skill(code-review)` の `--pre-pr` モード）。較正は `REVIEW.md`（#627）
> - **目視チェック**: 機械化できない項目。変更カテゴリに該当する行だけ確認すればよい
> - 詳細な検出手順・修正候補は `.claude/skills/self-reviewer/SKILL.md`、個別パターンの経緯は プロジェクト定義の自己レビュー教訓ファイル（出自プロジェクトでは `docs/rules/self-review-learnings.md`（P-XX）等）を参照 <!-- refcheck:ignore -->
> - 分析レポート（データの根拠）: `docs/analysis/pr-review-comments-analysis-2026-06.md` <!-- refcheck:ignore -->
> - **運用ルール**: 新しい頻出パターン（同種指摘 2 回以上）を発見したら、本シート + `self_review_check.py`（機械化可能な場合）を **同一 PR で** 更新する（L-094 desync 防止）
>
> ### 分析ツールの責務境界（混同しない・Issue #2905）
> | ツール | 役割 | 出力 |
> |--------|------|------|
> | `tools/analyze_pr_review_comments.py` | **全期間累積** スナップショット・本シート反映の判断材料（カテゴリ別件数） | `docs/analysis/pr_review_stats_*.json` |
> | `tools/pr_review_trends.py` | **週次フローの時系列**・セルフレビューが効いているかの可視化（指摘ゼロPR率の推移を週次で Slack グラフ投稿） | `content/analytics/pr_review_trends.jsonl` |
> | `tools/record_layer1_findings.py` → `tools/layer1_findings_report.py` | **Layer 1 / PR 前レビューの実測**（GitHub API 不要・クラウドでも成立・#627 対策 E）。`code-review` がレビューごとに追記し、週次で指摘ゼロ PR 率・CONFIRMED / PR・同種指摘候補を集計（`workflow-health-check` 4-e） | `content/analytics/review/layer1_findings.jsonl` |
>
> severity / AI 指摘判定ロジックは前者が SSOT で、後者は import 再利用する。AI 指摘の判定は **Layer 1 セルフレビューのテンプレート本文**（`**🔴 CRITICAL** ・ **CONFIRMED**`）で行い、廃止済みの Gemini / Copilot のログイン名判定は `--legacy-reviewers` を付けたときだけ併用する（#627 対策 E）。主指標「指摘ゼロPR率」の分母は **週内マージ全PR数**（指摘ゼロPR も含む）であり、`prs_with_comments`（指摘付きPRのみ）を分母にしない（比率が下がらず無意味になるため・技術監修役指摘）。

---

## 使い方（3 ステップ）

```
1. python3 tools/self_review_check.py        # 機械チェック一括実行（Error が出たら PR 禁止）
2. 下表の「全 PR 共通」+ 変更カテゴリに該当するセクションを目視確認
3. Warning を残す場合は PR 説明文に「## 設計意図・既知の警告」セクションで理由を明記
```

---

## 0. 全 PR 共通（指摘件数が最も多い 2 大カテゴリ）

| ✓ | チェック項目 | 検証 | 根拠（指摘数） |
|---|------------|------|--------------|
| ☐ | **同一 PR 内・関連ファイル間で数値/日付/名称が一致しているか**（diff に同じ数値が複数ファイルで登場したら全箇所を grep して突合する） | 目視 | **601 件**（最頻出。例: リサーチ間の金額乖離、台本とメタの尺不一致、スコアカード数値ズレ） |
| ☐ | **None/null/欠損キー/ファイル不在時の挙動を確認したか**（`.get()` フォールバック・空リスト・ファイル不在の 3 パターンを頭の中で実行する） | 目視 + 機械（一部） | **573 件**（2 位。KeyError・NoneType・undefined） |
| ☐ | **挙動を変える編集をした場合、仕様・テスト・コードの意図が一致しているか**（Intent Gate: `INTENT: code does X / check expects Y / spec says Z` を確認。仕様と矛盾するテストを通すためにコードを黙って書き換えていないか。権威順 ユーザー明示 > 仕様 > テスト > 現行コード・`docs/rules/intent-gate-rules.md`） | 目視 | fable-method 反映（spec betrayal 防止） |
| ☐ | 未コミット・未追跡・未 push がないか | 機械（フック） | フックでブロック済み |
| ☐ | タスク外ファイルが diff に混入していないか（`git diff origin/main...HEAD --name-only`） | 目視 | `core-principles.md` CP-1（スコープ外の改善は別 Issue を立ててから着手する） |
| ☐ | リポジトリ名・パス・コマンドの typo（tsukuru-studio の末尾 i 重複等） | 機械 | 実指摘あり（誤操作リスク） |
| ☐ | 対象 Issue の Done Criteria（Issue 本文または最初のコミットメッセージのいずれかに記載）を diff が満たしているか（どちらにも未記載なら理由を1行記録してスキップ） | 目視 | self-reviewer SKILL.md Step 1 追加（verification loops 記事の spec validation 縮小採用・#297・#302） |
| ☐ | **PR 前フレッシュ文脈レビューを通したか**（`has_code` または `high_risk` の差分。データのみはスキップ。`self-reviewer` Step 3.5 → `Skill(code-review)` を `--pre-pr` で実行 → CONFIRMED を修正 → PR 本文に `PR 前レビュー:` 1 行） | 機械（フックが記録欠落を Warning） | #627（直近 22 PR の指摘 121 件は修正率 79%・同一文脈チェックの死角。対照例 #586 は PR 後 0 件） |
| ☐ | 新規・変更したチェッカー / ガード / フックに「ロジックを外すと落ちる」否定テストがあるか（テストが緑でも、そのテストが何を検出できないかを 1 回問う） | 目視 + 機械（対応 `tools/test_<name>.sh` を自動実行） | #627（テスト・検証欠落 16%。vacuous test の実例 #550） |
| ☐ | fail-open になっていないか（読めない・解析できない入力で黙ってスキップしていないか）。既存の fail-closed 方針と一貫しているか | 目視 | #627（実例 #577 / #579） |
| ☐ | ドキュメントに書いた件数・数値・手順が差分後の実測値と一致しているか（テストや項目を追加したら数字も更新する） | 目視 | #627（ドキュメント整合 17%。実例 #619 の回帰テスト件数） |
| ☐ | 「議論で合意した」「完了条件は別 Issue へ送る」と書くとき、その根拠（Issue / 議論記録の該当箇所）を実際に開いて確認したか | 目視 | #627（Spec 忠実性 9%。実例 #619） |
| ☐ | 許可リスト（`permissions.allow` 等）への追加は、そのコマンドで何が書き換え・読み出し可能になるかまで確認したか | 目視 | #627（セキュリティ 21%。実例 #547） |

## 1. 台本・コンテンツ（`content/scripts/` `content/meta/`）

| ✓ | チェック項目 | 検証 | 根拠 |
|---|------------|------|------|
| ☐ | emotion / action / voicevox_speaker_id が有効値か | **機械** | 238 件（P-02/P-09/P-25） |
| ☐ | audio_file が `assets/` で始まっていないか | **機械** | P-24（Lv3 フックも検出） |
| ☐ | セリフ 100 文字超なし・90 文字超は字幕リスク確認 | **機械** | 177 件（文字数・尺・字幕） |
| ☐ | timed.json と script.json の text 同期（`python3 tools/sync_timed_text.py {ID} --check`） | 機械（別ツール） | 50 件（P-22） |
| ☐ | ブランド口調: キャラクター/ブランドの口調規約違反が混入していないか（プロジェクト例: キャラに設定外の方言・文体） | 目視（Lv3 フック併用） | 80 件 |
| ☐ | JSON / YAML 構文 | **機械** | L-004 |

## 2. リサーチ・ファクト（`content/research/`）

| ✓ | チェック項目 | 検証 | 根拠 |
|---|------------|------|------|
| ☐ | fact_check_flags: ランク C のみの情報を断定表現で書いていないか | 目視 + SKILL 4-C | 306 件（P-03） |
| ☐ | source_url が実在・到達可能か（`python3 tools/validate_fact_check_urls.py content/scripts/V{ID}_script.json`） | 機械（別ツール） | L-044 |
| ☐ | X 投稿にランク A を付けていないか / `[A]https://` リンク形式崩れ | 目視 + grep | P-04 |
| ☐ | 同一エンティティの数値がファイル間で乖離していないか | 目視 | P-06（0 章と同根） |

## 3. Python / シェル（`tools/` `scripts/`）

| ✓ | チェック項目 | 検証 | 根拠 |
|---|------------|------|------|
| ☐ | 構文エラー・インライン `__import__()` | **機械** | P-07/P-18 |
| ☐ | `except: pass` 握りつぶし（HTTPError は Error） | **機械** | 139 件（P-11） |
| ☐ | `datetime.now()` タイムゾーン未指定 | **機械** | 90 件（P-13） |
| ☐ | `open()` encoding 未指定（テキストモード） | **機械** | 52 件（P-14） |
| ☐ | `gh issue/pr list` の `--limit` 不足（クラウドは MCP 一次経路のため主にローカル用スクリプトが対象・L-114） | **機械** | 100 件（P-12） |
| ☐ | シェルのエラー抑制 5 箇所以上 | **機械** | P-11 |
| ☐ | 正規表現: エスケープ漏れ・量指定子の意図（`grep -E` の要否） | 目視 | 134 件 |
| ☐ | 条件式・境界値のロジック確認（off-by-one・否定の反転） | 目視 | 135 件 |
| ☐ | DRY: if/else 両ブランチ重複・既存関数の再発明 | 目視 | 129 件（P-19） |
| ☐ | 秘匿情報（トークン・Cookie・パスワード）のハードコード | **機械**（一部・`scan_dangerous_patterns.py` DP201・Warning）+ 目視 | 123 件（#56 で資格情報代入を機械化） |
| ☐ | コマンドインジェクション・任意コード実行（`shell=True`+非定数・`eval`/`exec`・`os.system`・`pickle.load`・`yaml.load` 非 Safe） | **機械**（`scan_dangerous_patterns.py` DP101-106・Error でブロック） | #56（FAIR Layer 0 強化・動作確認 #55 で見逃し検出） |
| ☐ | 並行セッション競合（ローカルファイル排他・TOCTOU） | 目視 | 90 件（CP-4） |
| ☐ | ハードコード値の定数化・出所コメント | 目視 | 86 件 |
| ☐ | 変更した `tools/`・`scripts/` の Python ファイルが `--self-test` を持つ場合、その self-test が通るか | **機械**（`self_review_check.py` の `self_test_errors()`・Error でブロック。`SELF_REVIEW_SELFTEST=warn` で非ブロック化可） | Issue #508 |

## 4. ドキュメント・スキル（`docs/` `.claude/` `CLAUDE.md`）

| ✓ | チェック項目 | 検証 | 根拠 |
|---|------------|------|------|
| ☐ | **記載したコマンド・パス・ファイル名が実在するか**（コピペ実行可能か） | 目視 | 170 件（表記揺れ・参照誤り） |
| ☐ | プレースホルダ表記の統一（`{PR番号}` vs `{pr_number}` 混在禁止） | 目視 | 同上 |
| ☐ | ドキュメントと実装（コード/SKILL.md）の乖離がないか（変更したら参照元も更新） | 目視 | 116 件（L-094） |
| ☐ | gh コマンドは `-R kai-kou/tsukuru-studio` 統一（`--repo` 不可） | **機械** | CLAUDE.md 規約 |
| ☐ | 既存 SSOT との重複定義を作っていないか（要約を置くなら SSOT 参照を併記） | 目視 | L-095 |
| ☐ | 配布対象ドキュメント（`docs/rules/` `.claude/skills/`）の新規記述で Issue/PR 番号を **単独の論拠** にしていないか（番号は下流リポジトリでは別 Issue を誤指しする。恒久的に意味が通る説明を本文に書き、番号は出典補足に留める） | 目視 | Issue #211（下流誤リンク） |
| ☐ | CJK 強調記法の前後半角スペース（`python3 tools/check_cjk_markdown.py --fix --changed` で自動整形） | **機械** | CLAUDE.md 規約（旧「目視」ですり抜け頻発 → 機械化） |

## 5. Remotion / TypeScript（`remotion/`）

| ✓ | チェック項目 | 検証 | 根拠 |
|---|------------|------|------|
| ☐ | `Root.tsx` / `data/index.ts` への動画 ID 登録漏れ | 目視 + grep | #550 |
| ☐ | fullscreenCueIds が lineId（数値文字列）か | 目視 + SKILL 3-7 | L-039 |
| ☐ | title_call テーマ画像ゲート（`python3 tools/check_theme_image.py --video V{ID}`） | 機械（別ツール） | #2677 |
| ☐ | imageMap に backgrounds/ パスが混入していないか | grep | design-spec |

---

## 深刻度と対応（self-reviewer SKILL.md Step 5 と同一）

| 深刻度 | 対応 |
|--------|------|
| **Error** | 修正するまで PR を作成しない（`self_review_check.py` exit 1） |
| **Warning** | 修正する。残す場合は PR 説明文「## 設計意図・既知の警告」に理由を明記（AI レビュアーの再指摘防止） |
| **Info** | PR 説明文に参考情報として記載 |

## 本シートの育て方（フィードバックループ）

1. PR マージ後、self-reviewer スキル Step F-1〜F-4（AI レビュー指摘の学習）を実行する
2. 同種指摘が 2 回以上 → 本シートに行を追加 + 機械化可能なら `self_review_check.py` にチェックを追加（**同一 PR で**）
3. 同種指摘が 3 回以上 → Lv3 フックへの昇格を検討（`docs/rules/harness-escalation.md`。Lv4 CI は現時点不採用・飼い主決定 #298 のため昇格先にしない）
4. **週次定期再分析（自動スケジュール済み・Issue #2870/#2900）**: 毎週月曜の 07:00 スロット ⑤.7 で `python3 tools/analyze_pr_review_comments.py --report` と `python3 tools/layer1_findings_report.py --weeks 4`（Layer 1 実測・同種指摘 2 回以上の候補・#627 対策 E）を実行し、前週比デルタ（カテゴリ +10 件/週以上・新出カテゴリ・機械化済みカテゴリの増加）を確認して本シート + チェッカーを更新する。週 ~90 PR ペースのため週次でないと同種指摘が頻発する。手順詳細は プロジェクト定義のスケジューリング詳細ファイル（例: `{プロジェクト定義: hourly-routing 相当}` ⑤.7 等）、分類ルールの正本は `config/pr_review_comment_categories.json`（無い/壊れている場合のみ同ツールの `DEFAULT_CATEGORY_RULES` にフォールバック・#420）
