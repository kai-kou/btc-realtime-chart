#!/usr/bin/env python3
"""【廃止】一度きりの分割用ツールで役目を終えました（Issue #2667・2026-06-06）。

lessons.md → docs/rules/lessons/<category>.md（Warm 層）への分割は完了済みです。
lesson 肥大化対策のツールは tools/lessons_guard.py 1 本に集約しています。

詳細・運用ルールの SSOT: docs/rules/lessons-management.md
"""
import sys

if __name__ == "__main__":
    print(__doc__)
    print(
        "⚠️ split_lessons.py は廃止されました（分割は完了済み）。",
        file=sys.stderr,
    )
    sys.exit(0)
