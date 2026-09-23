# セッションスプリント運用ルール（Hot 層サマリー）

> **SSOT**: 詳細（Dynamic 補正の根拠・PO 権限境界・メトリクス実装・週次較正手順）は
> `docs/rules/session-sprint-rules-detail.md` を参照。

## 原則: 1 セッション = 1 スプリント

- 全セッションは 1 回のスプリントとして扱う
- **対象がないセッションは no-op**（理由 1 行記録して終了。宣言の儀式を強制しない）

## スプリントプランニング（対象 Issue が存在する場合のみ）

`status:in-progress` ロック取得と同時に対象 Issue へ投稿:

```markdown
## 🏃 Session Sprint Planning
- **ゴール**: {1 文}
- **対象**: #{Issue 番号}（sp:{N}）
- **編成**: {メイン + サブエージェント役割}
```

PR 本文に必須: `Sprint Goal:` 1 行・`sp:N`・`Session-Id: {UUID}`（`echo $CLAUDE_CODE_SESSION_ID`）。
`Session-Id:` は sprint_session_metrics.py の突合と `--mine` 所有判定に使う（省略禁止）。

## SP スケール（複雑性 × レビュー負荷 × リスク）

| ラベル | 目安 |
|--------|------|
| `sp:1` | 1 ファイル・機械的変更 |
| `sp:2` | 小さな改善・単一スキル軽修正 |
| `sp:3` | 標準タスク・単一パイプライン 1 工程 |
| `sp:5` | 複数ファイル改修・新ツール追加 |
| `sp:8` | 大型タスク（**8 超なら分割**） |

**Dynamic 補正**: 要リサーチ・仕様未確定・新規領域 → **+1〜2 SP** を加える。

## SP 付与経路（全経路で必須）

| 経路 | ルール |
|------|--------|
| バックログ Issue 作成 | `sp:3`（工程別標準） |
| ユーザー指示の Issue 化 | SP スケール基準で付与 |
| retrospective / self-improvement-loop 起票 | small→`sp:2` / medium→`sp:3` / large→`sp:5` |
| sp なし Issue に着手するとき | プランニング宣言時に付与 |
