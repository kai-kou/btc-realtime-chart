#!/usr/bin/env python3
"""`status:waiting-user` の open Issue のうち長期未更新（既定 72 時間以上）のものを検出する。

正本: docs/routines.md §2.6（R-1 順位 7.5・#128）。R-1 の空転対策として、ユーザー回答待ちのまま
長期滞留した Issue を検出し、督促（A 区分のみ）または自律再分類（B/C 誤分類の是正）へつなぐ。

判定は `updated_at`（GitHub API の代理指標。ラベル付与時刻は取得できないため既存の慣行を踏襲・
docs/routines.md §2.1 順位 2 の Stale ロック判定と同じ考え方）だけで行う。

クラウド実行環境では `gh` が使えないことが多いため、`mcp__github__list_issues` で取得した
Issue 配列（number/title/labels/updated_at を持つ dict のリスト）を JSON として渡す。

Usage:
  python3 tools/check_stale_waiting_user.py --issues-json issues.json
  echo '[...]' | python3 tools/check_stale_waiting_user.py --issues-json -
  python3 tools/check_stale_waiting_user.py --issues-json - --threshold-hours 48
  python3 tools/check_stale_waiting_user.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

DEFAULT_THRESHOLD_HOURS = 72.0
TARGET_LABEL = "status:waiting-user"


def parse_iso8601(ts: str) -> datetime:
    """ISO 8601 文字列を aware な datetime へ変換する（末尾 `Z` を許容）。"""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_since(updated_at: str, now: datetime) -> float:
    return (now - parse_iso8601(updated_at)).total_seconds() / 3600.0


def is_stale(issue: dict, now: datetime, threshold_hours: float = DEFAULT_THRESHOLD_HOURS) -> bool:
    """`status:waiting-user` を持ち、`updated_at` が閾値以上前の Issue か判定する。"""
    labels = issue.get("labels") or []
    if TARGET_LABEL not in labels:
        return False
    updated_at = issue.get("updated_at")
    if not updated_at:
        return False
    return hours_since(updated_at, now) >= threshold_hours


def filter_stale(issues: list, now: datetime, threshold_hours: float = DEFAULT_THRESHOLD_HOURS) -> list:
    """滞留 Issue を抽出し、滞留時間が長い順（督促の優先順）に並べて返す。"""
    stale = []
    for issue in issues:
        labels = issue.get("labels") or []
        updated_at = issue.get("updated_at")
        if TARGET_LABEL not in labels or not updated_at:
            continue
        hours = hours_since(updated_at, now)
        if hours < threshold_hours:
            continue
        stale.append({
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "labels": labels,
            "updated_at": updated_at,
            "hours_stale": round(hours, 1),
        })
    stale.sort(key=lambda x: x["hours_stale"], reverse=True)
    return stale


def _run_self_test() -> int:
    now = parse_iso8601("2026-09-22T00:00:00Z")
    cases = [
        # (issue, expected_stale, 説明)
        ({"number": 1, "labels": ["status:waiting-user"], "updated_at": "2026-09-01T00:00:00Z"}, True, "21日前"),
        ({"number": 2, "labels": ["status:waiting-user"], "updated_at": "2026-09-21T12:00:00Z"}, False, "12時間前"),
        ({"number": 3, "labels": ["status:waiting-user"], "updated_at": "2026-09-19T00:00:00Z"}, True, "ちょうど72時間前（境界含む）"),
        ({"number": 4, "labels": ["status:waiting-user"], "updated_at": "2026-09-19T00:00:01Z"}, False, "72時間未満（境界1秒手前）"),
        ({"number": 5, "labels": ["status:in-progress"], "updated_at": "2026-09-01T00:00:00Z"}, False, "waiting-user ラベルなし"),
        ({"number": 6, "labels": ["status:waiting-user"]}, False, "updated_at 欠落"),
        ({"number": 7, "labels": ["status:waiting-user"], "updated_at": "2026-09-15T00:00:00Z"}, True, "7日前"),
    ]
    failed = 0
    for issue, expected, desc in cases:
        actual = is_stale(issue, now)
        ok = actual == expected
        print(f"{'PASS' if ok else 'FAIL'}: #{issue.get('number')} ({desc}) expected={expected} actual={actual}")
        if not ok:
            failed += 1

    issues = [c[0] for c in cases]
    result = filter_stale(issues, now)
    expected_order = [1, 7, 3]  # 滞留が長い順（waiting-user かつ stale のものだけ）
    actual_order = [r["number"] for r in result]
    ok_sorted = actual_order == expected_order
    print(f"{'PASS' if ok_sorted else 'FAIL'}: filter_stale ソート順 expected={expected_order} actual={actual_order}")
    if not ok_sorted:
        failed += 1

    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--issues-json", help="Issue 配列の JSON ファイルパス（'-' で stdin）。各要素は number/title/labels/updated_at を持つ")
    parser.add_argument("--threshold-hours", type=float, default=DEFAULT_THRESHOLD_HOURS, help=f"滞留とみなす時間閾値（既定 {DEFAULT_THRESHOLD_HOURS:.0f} 時間）")
    parser.add_argument("--now", help="判定基準時刻（ISO8601・省略時は現在時刻 UTC）")
    parser.add_argument("--self-test", action="store_true", help="判定ロジックの自己テストを実行する")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    if not args.issues_json:
        parser.error("--issues-json か --self-test のいずれかを指定してください")
        return
    if args.threshold_hours < 0:
        parser.error("--threshold-hours は 0 以上を指定してください")
        return

    raw = sys.stdin.read() if args.issues_json == "-" else open(args.issues_json, encoding="utf-8").read()
    issues = json.loads(raw)

    now = parse_iso8601(args.now) if args.now else datetime.now(timezone.utc)
    stale = filter_stale(issues, now, args.threshold_hours)
    print(json.dumps({
        "threshold_hours": args.threshold_hours,
        "checked_at": now.isoformat(),
        "stale_count": len(stale),
        "stale_issues": stale,
    }, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
