#!/usr/bin/env python3
"""【廃止】tools/lessons_guard.py に統合されました（Issue #2667・2026-06-06）。

このツールが生成していた lessons-top15.md は symlink されず誰も読まないまま
形骸化（2026-04-13 生成のまま停止）していたため廃止しました。lesson 肥大化対策の
ツールは lessons_guard.py 1 本に集約しています（メタ肥大化＝ツール乱立の防止）。

移行:
  python3 tools/lessons_scorer.py all      → python3 tools/lessons_guard.py stats
  python3 tools/lessons_scorer.py archive  → python3 tools/lessons_guard.py prune --apply
  python3 tools/lessons_scorer.py top15    → （廃止・Hot 層は上限 350 行で機械強制）

詳細・運用ルールの SSOT: docs/rules/lessons-management.md
"""
import sys

if __name__ == "__main__":
    print(__doc__)
    print(
        "⚠️ lessons_scorer.py は廃止されました。`python3 tools/lessons_guard.py stats` を使ってください。",
        file=sys.stderr,
    )
    sys.exit(0)
