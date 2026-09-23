# Warm 層 教訓（カテゴリ別）

このディレクトリはプロジェクト固有・カテゴリ別の教訓（lessons）を蓄積する **Warm 層**。
タスク依存で必要時に Read する（常駐しない）。

## 運用

- カテゴリごとに `docs/rules/lessons/<category>.md` を作る（例: `pr-review.md`・`ci.md`・`api-integration.md`）
- 全セッション横断で必須かつクリティカルな教訓だけを Hot 層（`docs/rules/lessons-core.md`）に昇格する
- 昇格 = コード/フック/ルールへ実装したら元エントリは物理削除する（`tools/lessons_guard.py prune --apply`）
- 詳細な運用ルールは `docs/rules/lessons-management.md`（SSOT）を参照

## エントリ書式（推奨）

```markdown
## L-XXX: 一行サマリー（YYYY-MM-DD）

**パターン**: 何が起きるか
**根本原因**: 直接 / 中間 / 根本の3層
**対策**: 再発防止（コード/フック/ルールのどこに昇格したか）
**禁止パターン / 推奨パターン**: ❌ / ✅
```
