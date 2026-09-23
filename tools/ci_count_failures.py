#!/usr/bin/env python3
"""Count failures in /tmp/check_result.json. Prints count to stdout.

スキップ対象 ID は環境変数 CI_SKIP_IDS（カンマ区切り）で指定する（既定: なし）。
各結果の "id" または "video_id" フィールドが CI_SKIP_IDS に含まれる場合は除外する。
"""
import json, os, sys

try:
    with open("/tmp/check_result.json") as f:
        results = json.load(f)
except Exception as e:
    print(f"JSON parse error: {e}", file=sys.stderr)
    sys.exit(1)

if isinstance(results, dict):
    results = [results]

skip_ids = {s.strip() for s in os.environ.get("CI_SKIP_IDS", "").split(",") if s.strip()}
failures = [
    r for r in results
    if not any(str(r.get(k)) in skip_ids for k in ("id", "video_id") if r.get(k) is not None)
    and not r.get("passed", True)
]
print(len(failures))
