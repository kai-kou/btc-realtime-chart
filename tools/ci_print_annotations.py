#!/usr/bin/env python3
"""Print GitHub Actions annotations for failed script quality checks (excluding V001)."""
import json, sys

try:
    with open("/tmp/check_result.json") as f:
        results = json.load(f)
except Exception as e:
    print(f"JSON parse error: {e}", file=sys.stderr)
    sys.exit(1)

if isinstance(results, dict):
    results = [results]

for r in results:
    if r.get("video_id") == "V001":
        continue
    if r.get("passed"):
        continue
    script_path = r.get("script_path", "")
    if "/home/runner/work/" in script_path:
        parts = script_path.split("/")
        try:
            idx = parts.index("work")
            rel_path = "/".join(parts[idx + 3:])
        except ValueError:
            rel_path = script_path
    else:
        rel_path = script_path.lstrip("/")

    for error in r.get("errors", []):
        print(f"::error file={rel_path}::{r['video_id']}: {error}")
    for warning in r.get("warnings", []):
        print(f"::warning file={rel_path}::{r['video_id']}: {warning}")
