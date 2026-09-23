#!/usr/bin/env python3
"""【廃止】tools/lessons_guard.py に統合されました（Issue #2667・2026-06-06）。

このツールはターゲットが空リダイレクター `lessons.md` のまま空振りしていたため、
`tools/lessons_guard.py` に統合して廃止しました。lesson 肥大化対策のツールは
lessons_guard.py 1 本に集約しています（メタ肥大化＝ツール乱立の防止）。

移行:
  python3 tools/prune_lessons.py --archive      → python3 tools/lessons_guard.py prune --apply
  python3 tools/prune_lessons.py --json         → python3 tools/lessons_guard.py prune
  python3 tools/prune_lessons.py --create-issue → python3 tools/lessons_guard.py prune（候補確認のみ）

詳細・運用ルールの SSOT: docs/rules/lessons-management.md
"""
import sys

if __name__ == "__main__":
    print(__doc__)
    print(
        "⚠️ prune_lessons.py は廃止されました。`python3 tools/lessons_guard.py prune` を使ってください。",
        file=sys.stderr,
    )
    sys.exit(0)
