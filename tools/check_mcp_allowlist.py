#!/usr/bin/env python3
"""check_mcp_allowlist.py — スキルが参照する MCP ツールと `permissions.allow` の突合（#618・#620）

無人ルーティンが呼ぶ MCP ツールが `.claude/settings.json` の `permissions.allow` に無いと、そのツールは
auto モードの classifier に回る（判定は確率的）か、auto でないモードでは承認プロンプトになる。無人セッションには
応答者がいないため、その回の処理が止まる（下流リポジトリの動画系 MCP ツールで実発生・L-130）。
本ツールは **PR 前に静的に** 「参照されているが allow に無い MCP ツール」を洗い出す。

## 何を見るか

- 参照側: `--scan-dirs`（既定 `.claude/skills` `.claude/commands`）配下の Markdown から
  `mcp__<server>__<tool>` 形式のトークンを全て抽出する（frontmatter の `allowed-tools:` と本文の両方）
- 許可側: `--settings`（既定 `.claude/settings.json`）の `permissions.allow` のうち `mcp__` で始まるもの。
  公式のルール構文（https://code.claude.com/docs/en/permissions）に従って解釈する:
    `mcp__<server>`          … サーバの全ツール
    `mcp__<server>__*`       … 同上（ワイルドカード）
    `mcp__<server>__<tool>`  … 個別ツール
    `mcp__<server>__<prefix>*` … リテラルなサーバ名の後のツール名 glob
  サーバ名部分の glob（`mcp__*`）は公式に無効（警告付きでスキップされる）なので一致に使わない
- サーバの母集団: `--mcp-config`（既定 `.mcp.json`）の `mcpServers` キー。ここに無いサーバのツール
  （例: プラットフォーム注入の `mcp__Claude_Code_Remote__*`・claude.ai コネクタ）は **突合対象外** として
  別枠で報告する（`.mcp.json` で管理していないため allow に書く前提が成り立たない）
- 除外リスト: `--ignore`（既定 `config/mcp_allowlist_check_ignore.txt`）。1 行 1 パターン
  （ツール名の完全一致か `mcp__<server>__*` 形式）。`#` 以降はコメント。出自プロジェクトの実例など、
  本リポジトリには存在しないサーバのツール名がドキュメントに残っているケースを除く

## 射程外（過大評価しないこと・L-130）

- `_meta["anthropic/requiresUserInteraction"]` 付きの MCP ツールは **allow に書いても毎回プロンプトになる**
  （全モード共通の例外・`dontAsk` のみ deny）。この注釈は MCP サーバの `tools/list` 応答にしか現れず、
  静的ファイルからは判別できない。したがって本ツールの「未登録」は「allow に足せば直る」を保証しない。
  allow に足しても実測でプロンプトが消えないツールは、無人ルーティンが呼ぶスキルの `allowed-tools` /
  コネクタから外すこと（唯一の恒久策）
- スキル本文に例示として書かれたツール名（実際には呼ばない）も参照として数える。誤検知は除外リストで消す

## 使い方

    python3 tools/check_mcp_allowlist.py            # 人間可読
    python3 tools/check_mcp_allowlist.py --json     # 機械可読（1 行 JSON）
    python3 tools/check_mcp_allowlist.py --self-test

終了コード: 0 = 未登録なし / 1 = 未登録あり（非ブロッキング警告として扱う） / 2 = 入力ファイル不備
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL_RE = re.compile(r"\bmcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_-]+)\b")
_MD_SUFFIXES = (".md",)

REQUIRES_USER_INTERACTION_NOTE = (
    "注意: `_meta[\"anthropic/requiresUserInteraction\"]` 付きのツール（承認 UI に「常に許可」が出ないもの）は "
    "allow に書いても毎回プロンプトになる（全モード共通・dontAsk のみ deny）。静的には判別できないため、"
    "allow 追加後も実測でプロンプトが消えないツールは無人ルーティンが呼ぶスキルの allowed-tools / コネクタから外すこと（L-130）。"
)


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def load_ignore(path: Path) -> list[str]:
    if not path.is_file():
        return []
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_comment(raw)
        if line:
            patterns.append(line)
    return patterns


def load_servers(mcp_config: Path) -> set[str] | None:
    """`.mcp.json` の mcpServers キー。ファイルが無ければ None（母集団を絞らない）。"""
    if not mcp_config.is_file():
        return None
    try:
        data = json.loads(mcp_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{mcp_config}: JSON として読めません（{exc}）") from exc
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{mcp_config}: mcpServers がオブジェクトではありません")
    return set(servers.keys())


def load_allow(settings: Path) -> list[str]:
    if not settings.is_file():
        raise ValueError(f"{settings}: 見つかりません")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{settings}: JSON として読めません（{exc}）") from exc
    allow = ((data.get("permissions") or {}).get("allow")) or []
    if not isinstance(allow, list):
        raise ValueError(f"{settings}: permissions.allow が配列ではありません")
    return [a for a in allow if isinstance(a, str) and a.startswith("mcp__")]


def allow_matches(tool: str, allow_rules: list[str]) -> bool:
    """公式のルール構文に従い、tool（`mcp__server__name`）が allow のいずれかに一致するか。"""
    m = _TOOL_RE.fullmatch(tool)
    if not m:
        return False
    server = m.group(1)
    for rule in allow_rules:
        if rule == tool:
            return True
        if rule == f"mcp__{server}":  # サーバ全体
            return True
        prefix = f"mcp__{server}__"
        if rule.startswith(prefix) and "*" in rule:
            # リテラルなサーバ名の後だけ glob を許す（`mcp__*` のようなサーバ名 glob は無効）
            if fnmatch.fnmatchcase(tool[len(prefix):], rule[len(prefix):]):
                return True
    return False


def ignored(tool: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(tool, p) for p in patterns)


def scan_references(scan_dirs: list[Path]) -> dict[str, list[str]]:
    """ツール名 → 参照元ファイル（相対パス）の一覧。"""
    refs: dict[str, set[str]] = {}
    for base in scan_dirs:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in _MD_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for m in _TOOL_RE.finditer(text):
                tool = m.group(0)
                try:
                    rel = str(path.relative_to(REPO_ROOT))
                except ValueError:
                    rel = str(path)
                refs.setdefault(tool, set()).add(rel)
    return {k: sorted(v) for k, v in sorted(refs.items())}


def run_check(settings: Path, scan_dirs: list[Path], mcp_config: Path, ignore: Path) -> dict:
    allow_rules = load_allow(settings)
    servers = load_servers(mcp_config)
    patterns = load_ignore(ignore)
    refs = scan_references(scan_dirs)

    missing: list[dict] = []
    allowed: list[str] = []
    unmanaged: list[dict] = []
    skipped: list[str] = []
    for tool, files in refs.items():
        server = _TOOL_RE.fullmatch(tool).group(1)  # refs は _TOOL_RE で抽出済み
        if ignored(tool, patterns):
            skipped.append(tool)
            continue
        if allow_matches(tool, allow_rules):
            allowed.append(tool)
            continue
        if servers is not None and server not in servers:
            unmanaged.append({"tool": tool, "server": server, "files": files})
            continue
        missing.append({"tool": tool, "server": server, "files": files})

    return {
        "settings": str(settings),
        "mcp_config": str(mcp_config) if servers is not None else None,
        "servers": sorted(servers) if servers is not None else None,
        "allow_rules": allow_rules,
        "referenced": len(refs),
        "allowed": allowed,
        "missing": missing,
        "unmanaged_server": unmanaged,
        "ignored": skipped,
        "note": REQUIRES_USER_INTERACTION_NOTE,
    }


def print_human(result: dict) -> None:
    print(f"[mcp-allowlist] 参照 {result['referenced']} 件 / allow 一致 {len(result['allowed'])} 件 / "
          f"未登録 {len(result['missing'])} 件 / 管理外サーバ {len(result['unmanaged_server'])} 件 / "
          f"除外 {len(result['ignored'])} 件")
    if result["missing"]:
        print("[mcp-allowlist] ✗ permissions.allow に無い MCP ツール（無人ルーティンが呼ぶなら登録が要る）:")
        for item in result["missing"]:
            print(f"  - {item['tool']}  ← {', '.join(item['files'])}")
        print("  登録例: \"mcp__<server>__<tool>\"（個別）/ \"mcp__<server>\"（サーバ全体。書き込み系も無確認になる点に注意）")
    if result["unmanaged_server"]:
        print("[mcp-allowlist] ℹ .mcp.json に無いサーバのツール（プラットフォーム注入・コネクタ等。突合対象外）:")
        for item in result["unmanaged_server"]:
            print(f"  - {item['tool']}")
    print(f"[mcp-allowlist] {result['note']}")


def _self_test() -> int:
    failures = 0
    # allow_matches の構文解釈
    rules = ["mcp__github__list_issues", "mcp__context7", "mcp__youtube__*", "mcp__slack__read_*"]
    cases = [
        ("mcp__github__list_issues", True),
        ("mcp__github__issue_write", False),
        ("mcp__context7__resolve-library-id", True),
        ("mcp__youtube__list_private", True),
        ("mcp__slack__read_channel", True),
        ("mcp__slack__send_message", False),
        ("mcp__other__tool", False),
    ]
    for tool, expect in cases:
        actual = allow_matches(tool, rules)
        if actual != expect:
            failures += 1
            print(f"  NG allow_matches({tool}) 期待={expect} 実際={actual}")
    # `mcp__*` のようなサーバ名 glob は一致に使わない
    if allow_matches("mcp__github__list_issues", ["mcp__*"]):
        failures += 1
        print("  NG サーバ名 glob `mcp__*` を一致扱いしている")

    # ファイル走査 → 突合の end-to-end
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "skills" / "a").mkdir(parents=True)
        (root / "skills" / "a" / "SKILL.md").write_text(
            "---\nallowed-tools: Bash, mcp__github__list_issues, mcp__github__issue_write\n---\n"
            "本文で mcp__youtube__list_private と mcp__Claude_Code_Remote__list_triggers と "
            "mcp__legacy__old_tool を参照する\n",
            encoding="utf-8",
        )
        (root / "settings.json").write_text(json.dumps({"permissions": {"allow": ["mcp__github__list_issues"]}}), encoding="utf-8")
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {}, "youtube": {}}}), encoding="utf-8")
        (root / "ignore.txt").write_text("# コメント\nmcp__legacy__*\n", encoding="utf-8")
        result = run_check(root / "settings.json", [root / "skills"], root / ".mcp.json", root / "ignore.txt")
        missing = sorted(m["tool"] for m in result["missing"])
        unmanaged = sorted(u["tool"] for u in result["unmanaged_server"])
        if missing != ["mcp__github__issue_write", "mcp__youtube__list_private"]:
            failures += 1
            print(f"  NG missing 期待 2 件 実際={missing}")
        if unmanaged != ["mcp__Claude_Code_Remote__list_triggers"]:
            failures += 1
            print(f"  NG unmanaged 期待 1 件 実際={unmanaged}")
        if result["ignored"] != ["mcp__legacy__old_tool"]:
            failures += 1
            print(f"  NG ignored 実際={result['ignored']}")
        if result["allowed"] != ["mcp__github__list_issues"]:
            failures += 1
            print(f"  NG allowed 実際={result['allowed']}")
        # settings 不備 → ValueError
        try:
            run_check(root / "nope.json", [root / "skills"], root / ".mcp.json", root / "ignore.txt")
            failures += 1
            print("  NG settings 不在で例外にならない")
        except ValueError:
            pass
    print(f"[check_mcp_allowlist --self-test] {'PASS' if failures == 0 else 'FAIL'}（失敗 {failures} 件）")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="スキルが参照する MCP ツールと permissions.allow の突合")
    ap.add_argument("--settings", default=str(REPO_ROOT / ".claude" / "settings.json"))
    ap.add_argument("--scan-dirs", nargs="+", default=[str(REPO_ROOT / ".claude" / "skills"), str(REPO_ROOT / ".claude" / "commands")])
    ap.add_argument("--mcp-config", default=str(REPO_ROOT / ".mcp.json"))
    ap.add_argument("--ignore", default=str(REPO_ROOT / "config" / "mcp_allowlist_check_ignore.txt"))
    ap.add_argument("--json", action="store_true", help="機械可読（1 行 JSON）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    try:
        result = run_check(Path(args.settings), [Path(d) for d in args.scan_dirs], Path(args.mcp_config), Path(args.ignore))
    except ValueError as exc:
        print(f"[mcp-allowlist] 入力不備: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print_human(result)
    return 1 if result["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
