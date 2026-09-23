#!/usr/bin/env python3
"""check_agent_definitions.py — サブエージェント定義の `tools` 指定を公式フィルタ仕様と突合する（#367）

## なぜ必要か

Claude Code はサブエージェントのツールプールを **2 段のフィルタ** で絞る。フィルタで消えたツールは
**エラーを報告しない**（silent removal）。その結果 `tools` に書いたツールが全滅すると、サブエージェントは
ツールなしで起動して **空・意味不明な回答** を返す（v2.1.208 以降は起動拒否のエラーになるが、
いずれにせよ委譲は失敗する）。目視では気づけないため機械検出する。

出典: https://code.claude.com/docs/en/sub-agents （2026-07-29 確認・v2.1.220）

## 判定

- ERROR: `tools` の全エントリが第 1 フィルタで除去される（= zero tools）
- ERROR: `tools` の全エントリが background 実行で除去される（= background で zero tools。
  v2.1.198 以降 **background が既定** なので実質常に発火する）
- WARN:  `tools` が MCP ツールのみ（headless / cron で MCP 未接続なら zero tools になりうる）
- WARN:  第 1 フィルタ / background フィルタで黙って消えるエントリを含む

使い方:
    python3 tools/check_agent_definitions.py            # .claude/agents/*.md を検査
    python3 tools/check_agent_definitions.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# 第 1 フィルタ: 全サブエージェントから無条件に除去される（`tools` に列挙しても消える）
ALWAYS_REMOVED = {
    "AskUserQuestion",
    "EndConversation",
    "EnterPlanMode",
    "ScheduleWakeup",
    "TaskOutput",
    "WaitForMcpServers",
    "Workflow",
}

# 第 1 フィルタのうち **条件付き** で除去されるもの。生存しうるので zero tools 判定には数えず、
# 「黙って消えることがある」WARN の対象にとどめる。
#   Agent        … 深さ上限に達したときに除去される（fork では残るが呼ぶとエラー）
#   ExitPlanMode … `permissionMode: plan` のときだけ残る（下で個別に判定する）
CONDITIONALLY_REMOVED = {"Agent", "ExitPlanMode"}

# 第 2 フィルタ: background 実行のサブエージェントに残る組み込みツール（既定は background）
BACKGROUND_ALLOWED_BUILTINS = {
    "Read", "Grep", "Glob", "Bash", "PowerShell", "Edit", "Write", "NotebookEdit",
    "WebFetch", "WebSearch", "TodoWrite", "Skill", "ToolSearch",
    "EnterWorktree", "ExitWorktree", "Monitor", "TaskStop", "SendMessage", "Artifact",
}

# Agent Teams の teammate は上記に加えてタスク / cron ツールを保持する
TEAMMATE_EXTRA = {
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate",
    "CronCreate", "CronDelete", "CronList",
}

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """最小限の frontmatter パーサ（PyYAML 非依存）。

    `key: value` の 1 行形式に加え、YAML ブロックリスト記法にも対応する:

        tools:
          - Read
          - Grep

    frontmatter を持たないファイル（エージェント定義ではない README 等）は `None` を返す。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    current_key: str | None = None
    list_values: list[str] = []

    def flush() -> None:
        nonlocal list_values
        if current_key and list_values:
            fields[current_key] = ", ".join(list_values)
        list_values = []

    for line in m.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            # インデント行: 直前のキーに属するブロックリスト項目だけ拾う
            if current_key and stripped.startswith("-"):
                item = stripped[1:].strip()
                if item:
                    list_values.append(item)
            continue
        flush()
        key, sep, value = line.partition(":")
        if sep:
            current_key = key.strip()
            fields[current_key] = value.strip()
        else:
            current_key = None
    flush()
    return fields


def split_tools(raw: str) -> list[str]:
    """`tools` の値をツール名リストへ分解する。

    `Read, Grep` / `[Read, Grep]` / `"Read, Grep"` / ブロックリスト由来の `Read, Grep` を
    いずれも同じ結果に正規化する（YAML のフロー配列記法・引用符で誤判定しないため）。
    """
    cleaned = raw.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    tools = []
    for token in re.split(r"[,\s]+", cleaned):
        name = token.strip().strip("\"'")
        if name:
            tools.append(name)
    return tools


def check_agent(path: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    rel = str(path.relative_to(REPO_ROOT))
    try:
        fields = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return [{"level": "WARN", "file": rel, "message": f"読み込めないため検査をスキップしました: {e}"}]

    # frontmatter を持たない .md はエージェント定義ではない（README 等）ので検査しない
    if fields is None:
        return findings

    if not fields.get("name"):
        findings.append({"level": "ERROR", "file": rel, "message": "frontmatter に必須フィールド `name` がない"})
    if not fields.get("description"):
        findings.append({"level": "ERROR", "file": rel, "message": "frontmatter に必須フィールド `description` がない"})

    raw_tools = fields.get("tools")
    if not raw_tools:
        return findings  # 未指定 = 全ツール継承。フィルタで全滅することはない

    tools = split_tools(raw_tools)
    plan_mode = fields.get("permissionMode") == "plan"

    always_removed = [t for t in tools if t in ALWAYS_REMOVED]
    # 条件付き除去（Agent / plan モード以外の ExitPlanMode）は生存しうるので zero tools には数えない
    conditionally_removed = [
        t for t in tools
        if t in CONDITIONALLY_REMOVED and not (t == "ExitPlanMode" and plan_mode)
    ]
    survivors_fg = [t for t in tools if t not in always_removed]
    # `Agent` と `ExitPlanMode` は「どこで動いても第 1 フィルタの条件に従う」（= background 固有の
    # 除去対象ではない）ため、background の生存判定でも残す（公式仕様）。
    survivors_bg = [
        t for t in survivors_fg
        if is_mcp_tool(t)
        or t in BACKGROUND_ALLOWED_BUILTINS
        or t in TEAMMATE_EXTRA
        or t in CONDITIONALLY_REMOVED
    ]

    if not survivors_fg:
        findings.append({
            "level": "ERROR", "file": rel,
            "message": f"`tools` の全エントリが第 1 フィルタで除去される（zero tools → 空回答）: {', '.join(tools)}",
        })
    elif not survivors_bg:
        findings.append({
            "level": "ERROR", "file": rel,
            "message": (
                "`tools` の全エントリが background 実行で除去される（v2.1.198 以降 background が既定 → 空回答）: "
                f"{', '.join(survivors_fg)}"
            ),
        })
    else:
        if always_removed:
            findings.append({
                "level": "WARN", "file": rel,
                "message": f"第 1 フィルタで黙って消えるツールを指定している: {', '.join(always_removed)}",
            })
        if conditionally_removed:
            findings.append({
                "level": "WARN", "file": rel,
                "message": (
                    "条件付きで消えるツールを指定している（`Agent` は深さ上限時、`ExitPlanMode` は "
                    f"`permissionMode: plan` 以外で除去される）: {', '.join(conditionally_removed)}"
                ),
            })
        dropped_bg = [t for t in survivors_fg if t not in survivors_bg]
        if dropped_bg:
            findings.append({
                "level": "WARN", "file": rel,
                "message": f"background 実行（既定）で黙って消えるツールを指定している: {', '.join(dropped_bg)}",
            })
        if all(t in TEAMMATE_EXTRA for t in survivors_bg):
            findings.append({
                "level": "WARN", "file": rel,
                "message": (
                    "background で残るのが Agent Teams 専用ツールのみ。teammate としては動くが、"
                    "通常のサブエージェント委譲では zero tools になり空回答になる"
                ),
            })
        if all(is_mcp_tool(t) for t in survivors_bg):
            findings.append({
                "level": "WARN", "file": rel,
                "message": (
                    "`tools` が MCP ツールのみ。MCP 未接続の headless / cron 実行では zero tools になり空回答に"
                    "なりうる（`Read` 等の組み込みツールを 1 つ以上足すと安全）"
                ),
            })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    args = parser.parse_args()

    if not AGENTS_DIR.is_dir():
        print("ℹ️  .claude/agents/ が存在しないためスキップします")
        return 0

    findings: list[dict[str, str]] = []
    agent_files = sorted(AGENTS_DIR.glob("*.md"))
    for path in agent_files:
        findings.extend(check_agent(path))

    errors = [f for f in findings if f["level"] == "ERROR"]

    if args.json:
        print(json.dumps({"checked": len(agent_files), "findings": findings}, ensure_ascii=False, indent=2))
        return 1 if errors else 0

    if not findings:
        print(f"✅ サブエージェント定義 {len(agent_files)} 件: 問題なし")
        return 0

    for f in findings:
        icon = "❌" if f["level"] == "ERROR" else "⚠️"
        print(f"{icon} [{f['level']}] {f['file']}: {f['message']}")
    print(f"\n検査 {len(agent_files)} 件 / ERROR {len(errors)} 件 / WARN {len(findings) - len(errors)} 件")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
