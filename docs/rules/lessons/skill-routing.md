# Warm 層 教訓 — スキルルーティング（skill-routing）

スキル選択・トリガー競合・ルーティングに関する教訓を蓄積する。タスク依存で必要時に Read する（常駐しない）。

---

## L-123: ビルトイン機能の「起動経路」はバージョンで移動する（Skill → Workflow）（2026-07-29）

**パターン**: これまで `Skill` ツールで呼べていたビルトイン機能が、CLI のバージョンアップで
`Workflow` ツール経由でしか呼べなくなる。実例: `/deep-research` は **v2.1.218** で
`disable-model-invocation` 相当が適用され、`Skill(skill="deep-research")` が
`cannot be used with Skill tool due to disable-model-invocation` で即失敗するようになった
（公式: *"`/deep-research` runs only when you invoke it. Before v2.1.218, Claude could also start it on its own."*）。
一方 `Workflow({name: "deep-research", args: ...})` は通る。SSOT（SKILL.md）に単一の起動形が
固定記述されていたため、**実行不能な手順がルールとして残った**（#370）。

**判定基準**: 「この起動形は台帳（`native.routes`）から来ているか、それとも文書に直書きされた
1 経路か？」後者なら移動に耐えられない。

**対策**:
- 起動経路は `tools/native_capabilities.json` の `native.routes` に **順序付きで台帳化** する。
  実行前に `python3 tools/native_fallback.py routes <id>` で現行の試行順を確認する。
- ladder（`skill` → `workflow` → `session-tool` → `claude -p` → 最終手段）を上から降格する。
  降格シグネチャは `ROUTE_DEMOTION_SIGNATURES`（`tools/native_fallback.py`）が正本。
- 閉じた経路は **削除せず `status: "unavailable"`** + いつ・どのバージョン・どのエラーで閉じたかを残す。
- SSOT の詳細は `docs/rules/native-fallback-rules.md` §2.5。

## L-116: ほぼ同名のスキルが共存しルーティング規定が無いと、軽い経路に倒れる（2026-06-21）

**パターン**: ユーザーが「ディープリサーチして」と依頼したのに、`/deep-research` を起動する
`research-runner` ではなく、ビルトイン `deep-research`（自セッション内 WebSearch fan-out）や
素の `WebSearch` が選ばれ、期待より浅いリサーチになった。

**根本原因（3層）**:
- **直接原因**: 指示が `research-runner`（`tools/run_deep_research_workflow.py`）にルーティングされなかった。
- **中間原因**: ① ビルトイン `deep-research`（名前が「ディープリサーチ」と直接一致）と `research-runner`
  （description に「ディープリサーチして」を含む）が同一トリガー語に反応するのに優先順位の SSOT が無い。
  ② `research-runner` の起動条件が「Issue / プロンプトファイル前提」のみで、対話起点のアドホック依頼の
  入口が無く「使えない」と判断され軽い経路に倒れた。③ 常駐コンテキスト（CLAUDE.md）に
  「ディープリサーチ＝claude -p 既定」の方針が無かった。
- **根本原因**: 既定エンジンを定める SSOT が未明文化・未常駐。ほぼ同名の 2 スキルが共存するのに
  ルーティング規定が無く、スキル選択がモデル判断に委ねられた。

**対策（再発防止・昇格先）**:
- CLAUDE.md「Agent Skills」直後に **ルーティング SSOT を常駐化**（「ディープリサーチ＝`research-runner`
  が既定。クラウド環境でも `/deep-research` を `claude -p` なしで直接実行可能。素の WebSearch / ビルトイン `deep-research` を既定にしない」）。
- `research-runner` SKILL.md に **§0 ルーティング表** と **対話起点のアドホック起動フロー**
  （プロンプトファイル/Issue が無くても指示文から `{ID}_prompt.md` を生成して主エンジン起動）を追加。

**❌ 禁止 / ✅ 推奨**:
```
❌ 「ディープリサーチして」に対し research-runner を起動せず素の WebSearch で済ませる
❌ ビルトイン deep-research をいきなり使う（research-runner の DIY フォールバック相当＝最終手段）
✅ 既定で research-runner（/deep-research を直接実行）を最初に起動する
✅ コスト/速度優先・対象が軽微なときだけ、理由を1行述べて簡易リサーチに切り替える
```

**横展開**: 同種の「ほぼ同名スキルの共存」を見つけたら、どちらを既定にするかの振り分け SSOT を
（`agent-team-summary.md` の議論型 vs fan-out 振り分けと同様に）必ず明文化する。
