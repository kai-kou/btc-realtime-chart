#!/usr/bin/env python3
"""Combine per-video check result files (/tmp/check_vid_*.json) into /tmp/check_result.json."""
import json, glob, sys

results = []
for fp in sorted(glob.glob('/tmp/check_vid_*.json')):
    try:
        with open(fp) as f:
            data = json.load(f)
        if isinstance(data, dict):
            results.append(data)
        elif isinstance(data, list):
            results.extend(data)
    except Exception as e:
        print(f"parse error ({fp}): {e}", file=sys.stderr)

with open('/tmp/check_result.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False)
