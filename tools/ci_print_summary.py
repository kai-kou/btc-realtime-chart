#!/usr/bin/env python3
"""Write GitHub Step Summary for script quality check results."""
import json, os, sys

try:
    with open("/tmp/check_result.json") as f:
        results = json.load(f)
except Exception as e:
    print(f"JSON parse error: {e}", file=sys.stderr)
    sys.exit(0)

if isinstance(results, dict):
    results = [results]

summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")

lines = []
lines.append("# 台本品質チェック結果\n")
lines.append("| 動画ID | 判定 | 文字数 | 実測尺 | セクション数 | エラー | 警告 |")
lines.append("|--------|------|--------|--------|------------|--------|------|")

passed = 0
failed_non_v001 = 0

for r in results:
    vid = r.get("video_id", "?")
    is_v001 = vid == "V001"
    p = r.get("passed", False)
    metrics = r.get("metrics", {})
    chars = metrics.get("total_chars", "-")
    duration = metrics.get("actual_duration_min", "-")
    sections = metrics.get("section_count", "-")
    errors = r.get("errors", [])
    warnings = r.get("warnings", [])

    if is_v001:
        status = "⏭️ SKIP (pilot)"
    elif p:
        status = "✅ PASS"
        passed += 1
    else:
        status = "❌ FAIL"
        failed_non_v001 += 1

    err_str = "<br>".join(errors) if errors else "-"
    warn_str = "<br>".join(warnings) if warnings else "-"
    duration_str = f"{duration}分" if isinstance(duration, (int, float)) else str(duration)

    lines.append(f"| {vid} | {status} | {chars}文字 | {duration_str} | {sections} | {err_str} | {warn_str} |")

lines.append("")
if failed_non_v001 == 0:
    lines.append("## ✅ 品質ゲート: PASSED")
    lines.append("チェック対象の台本が品質基準を満たしています。")
else:
    lines.append("## ❌ 品質ゲート: FAILED")
    lines.append(f"V001 を除く {failed_non_v001} 本の台本が品質基準を満たしていません。")

with open(summary_path, "w") as f:
    f.write("\n".join(lines) + "\n")
