# research-runner 詳細リファレンス

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


> `research-runner` スキルの **フォールバック分岐の詳細手順**（Step 3.6 / Step 4・
> 失敗モード一覧）を切り出したもの（progressive disclosure・#165）。
> 主エンジン（ネイティブ `/deep-research`）が成功する大多数のケースでは本ファイルを読む必要はない。
> `SKILL.md` の Step 3b が **EXIT=1/4/5 → Step 4** / **EXIT=6 → Step 3.6** に分岐したときに Read する。
>
> 🔴 フォールバック連鎖は **①ネイティブ `/deep-research`（Step 3a・`Workflow` ツールで直接呼び出し）→
> ② `claude -p` サブプロセス経由の `/deep-research`（Step 3b）→ ③ DIY（ウェブリサーチ・Step 4）** の三層のみ。
> **外部 LLM API（Gemini 等）によるディープリサーチは行わない**（飼い主決定・Issue #260・旧 Step 3.5 は廃止済み）。
>
> 🔴 本リポジトリでは `content/research/{ID}_*` 規約・`phase:research` ラベルへの依存があるため、
> 非リサーチ系プロジェクトではこのフォールバック連鎖自体が発火しない点に注意（本文の分岐は
> リサーチパイプラインを採用するプロジェクトでのみ意味を持つ）。

---

## Step 3.6: レート枠超過時のスキップ判定（ネイティブ `/deep-research` を主に維持）

Step 3 が **EXIT=6（レート枠超過・capacity）** を返したときのみ実行する（EXIT=1/4/5 は Step 4 へ直行）。

> **ポリシー**: ディープリサーチは **必ずネイティブ `/deep-research` で行う**。**レート枠超過は DIY に即フォールバックせず「スキップ」して次スロットで再試行** し、**連続3回スキップしたときだけ DIY（Step 4）へ** 降りる。

skip カウンタは **対象 Issue のラベル `research-skip:N`（N=1..3）** で永続化する（クラウド・セッション横断で安全。state ファイルは使わない＝L-100 消失・cross-session 共有問題を回避）。

MCP（クラウド・一次経路）: 現在の skip 値は `mcp__github__issue_read(method="get_labels", issue_number={ISSUE_NUM})` で取得し、
ラベルの付け替えは `mcp__github__issue_write(method="update", labels=[フルリスト])`（全置換・SKILL.md §2.2）で行う。
`research-skip:${NEXT}` ラベルが未作成の場合、MCP にラベル作成の等価ツールはないが、`issue_write` の `labels` に
未存在ラベル名を渡すと GitHub 側で自動作成される（色は既定値）。以下はローカル環境（gh CLI 到達可能時）の代替:

```bash
SKIP=$(gh issue view {ISSUE_NUM} -R kai-kou/btc-realtime-chart --json labels \
  --jq '[.labels[].name | select(startswith("research-skip:")) | sub("research-skip:";"") | tonumber] | max // 0')
NEXT=$((SKIP + 1))
if [ "$NEXT" -lt 3 ]; then
  # スキップ: ラベルを実際に付け替え、status を waiting-claude に戻して次スロットで /deep-research 再試行
  gh label create "research-skip:${NEXT}" -R kai-kou/btc-realtime-chart --color FBCA04 2>/dev/null || true
  [ "$SKIP" -gt 0 ] && gh issue edit {ISSUE_NUM} -R kai-kou/btc-realtime-chart --remove-label "research-skip:${SKIP}"
  gh issue edit {ISSUE_NUM} -R kai-kou/btc-realtime-chart \
    --add-label "research-skip:${NEXT}" \
    --remove-label "status:in-progress" --add-label "status:waiting-claude"
  # → Issue に「⏳ レート枠超過によりスキップ (${NEXT}/3)」とコメントして STOP
else
  # 3連続到達: skip カウンタを全削除して Step 4（DIY・ウェブリサーチ）へ
  [ "$SKIP" -gt 0 ] && gh issue edit {ISSUE_NUM} -R kai-kou/btc-realtime-chart --remove-label "research-skip:${SKIP}"
  # → Issue に「⚠️ /deep-research が3連続レート枠超過。DIY（ウェブリサーチ）にフォールバック」とコメントして Step 4 へ
fi
```

| NEXT（連続何回目のスキップか） | アクション |
|---|---|
| **1 または 2（< 3）** | ① 旧 `research-skip:*` を全削除し `research-skip:${NEXT}` を付与 ② `status:in-progress` を外して `status:waiting-claude` に戻す（次スロットで再ロック → `/deep-research` 再試行） ③ Issue に「⏳ レート枠超過によりスキップ (${NEXT}/3)。次スロットで再試行する」とコメント ④ **STOP** |
| **3（== 3）** | ① `research-skip:*` を全削除（カウンタリセット） ② Issue に「⚠️ `/deep-research` が3連続レート枠超過。今回は DIY（ウェブリサーチ）にフォールバックする」とコメント ③ **Step 4（DIY）へ進む** |

> 🔴 **カウンタのリセット条件**: Step 3 が **EXIT=0（成功）したら、Step 6 のコミット前に必ず `research-skip:*` ラベルを全削除** する（成功＝連続スキップが途切れたため）。

---

## Step 4: 最終フォールバック（DIY・ウェブリサーチ＝Sonnet 5 + WebSearch）【Step 3（3a/3b）が失敗時のみ】

Step 3 のネイティブ `/deep-research`（対話起動なら 3a 直接呼び出し → 3b `claude -p` の両方、自律起動なら 3b）が
**実際に失敗した場合のみ** 実行する。これらを試さずに本 Step を実行するのは禁止（安易な DIY 直行禁止）。

> **フォールバック発動時の必須記録**: `content/pipeline-state/research_fallback_log.jsonl` に
> `/deep-research` の失敗理由が記録されていること（`run_deep_research_workflow.py` が
> EXIT=1/4/5/6 で自動記録・#4699）を確認し、PR 本文に「DIY フォールバック理由: {理由・EXIT コード}」を
> 明記する（サイレントフォールバック禁止・L-094）。手動追記
> （`python3 tools/run_deep_research.py {ID} --fallback-reason "{理由}" --dry-run`）は
> **自動記録が失敗した（stderr に `[WARN] fallback log 書き込み失敗` が出た）場合のみ** 使う
> （二重記録すると反復検知の連続カウントを汚す）。
> **EXIT=4/5 が異なるリサーチ ID で 2 連続以上続いたら `type:bug` Issue を起票** し、
> CLI インターフェース変化（下流プロジェクトの実障害と同型）の恒久対応を検討する
> （反復検知ルールの SSOT は `SKILL.md` Step 3b。本ファイルの記載は参照）。

このスキル自身が Sonnet 5 として、本セッション内で WebSearch / WebFetch を実行し、
`research_schema.json` に準拠した JSON を組み立てて `tools/run_deep_research.py` に引き渡す。

実行手順:

1. プロンプトの調査項目（5〜7 項目 + 正式名称確認リスト）を識別
2. **項目ごとに並列 sub-agent**（Haiku 4.5 推奨）を起動して WebSearch（5〜10クエリ）+ WebFetch（主要 2-3 URL）
3. 各 sub-agent は以下のフォーマットで返す:

```json
{
  "heading": "セクション見出し",
  "body_markdown": "本文（[A]/[B]/[C] タグと URL を併記）",
  "source_ids": ["s001", "s002"]
}
```

4. メインスキルが結果を統合し、official_names・sources・fact_check_flags を生成
5. JSON / Markdown を `content/research/{ID}_deep_research.{md,json}` に書き出し

---

## 失敗モードと対応

| 失敗モード | 検出 | 対応 |
|---|---|---|
| Step 3a（`Workflow` ツールで直接呼び出し）失敗（エラー・本文欠落・WebSearch 未許可） | 呼び出し結果・本文長 | Step 3b（`claude -p` サブプロセス）で再試行 |
| Step 3b `claude -p` 実行失敗（EXIT=1/4/5） | `run_deep_research_workflow.py` の終了コード | `research_fallback_log.jsonl` に自動記録 → DIY（Step 4）切替 + 通知。**EXIT=4/5 が異なるリサーチ ID で 2 連続以上なら `type:bug` Issue 起票**（CLI インターフェース変化を疑う・#4699） |
| ワークフロー起動不能（`DEEP_RESEARCH_UNAVAILABLE` センチネル・EXIT=5） | エラー出力のセンチネル文字列 | CLI の起動インターフェース変化が確定 → `type:bug` Issue 起票（`dr_prompt` / `SEARCH_ALLOWED_TOOLS` の現行 CLI 追従が必要）+ DIY 切替 |
| Workflow 起動後の早期リターン（EXIT=4・elapsed が timeout より大幅に短い） | `research_fallback_log.jsonl` エントリの `reason` 文字列内 `elapsed=`（EXIT=4/6 のエラーメッセージに埋め込まれる。独立フィールドではない） | サブプロセスがポーリング指示（#4722）を無視した可能性 → 1 回再試行 → 再発なら `type:bug` Issue 起票 |
| レート枠超過（EXIT=6・capacity） | 同上 | Step 3.6 のスキップ判定（3連続で初めて DIY） |
| 品質劣化（fact_check_flags ≥ 5 / rank C ≥ 1） | Step 5 の品質ゲート | 主エンジンで 1回再試行 → それでも NG なら waiting-user |
| 月予算超過（API 従量経路） | `check_budget()` | `$50` で即停止 + 通知（境界外） |
| 同テーマ並行実行 | Step 1 のラベル取得失敗 | 即終了（CP-4 パターン） |
| プロンプト未生成 | Step 2 のファイル不在 + Issue 復元失敗 | 上流のプロンプト生成を起動するか、ユーザーに報告 |
