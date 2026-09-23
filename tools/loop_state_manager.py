#!/usr/bin/env python3
"""loop_state_manager.py — ループ状態の機械可読 JSON を GitHub Variables に読み書きする（ローカル専用）。

ローカル環境（gh CLI が GitHub Variables に到達できる環境）専用。クラウド実行環境では
`gh variable get/set` が Actions パスとして egress プロキシに 403 でブロックされ常に失敗する
（Issue #133・L-114）。クラウドでは checkpoint スキルが Step 3 の Issue コメント末尾に
機械可読 JSON ブロックを直接埋め込む方式（GitHub Variables 不使用）に統一済み（Issue #161）。
ループ工学の State File / cross-session memory パターンの実装（Issue #99）。

Usage:
  python3 tools/loop_state_manager.py read [--key KEY]
  python3 tools/loop_state_manager.py write \\
      --phase PHASE --session-id SID \\
      [--key KEY] [--loop-count N] [--next-steps "step1" "step2"]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
VAR_PREFIX = "LOOP_STATE"


def _get_repo() -> str:
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else ""


def _var_name(key: str) -> str:
    base = key.upper().replace("-", "_")
    return VAR_PREFIX if base == "LATEST" else f"{VAR_PREFIX}_{base}"


def read_state(key: str = "latest") -> dict:
    """GitHub Variables から構造化ループ状態を読み込む。存在しない場合は空 dict を返す。"""
    repo = _get_repo()
    repo_flag = ["-R", repo] if repo else []
    result = subprocess.run(
        ["gh", "variable", "get", _var_name(key), *repo_flag],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # 「状態なし」と「取得失敗」を混同しないよう stderr に明示する（戻り値は互換のため空 dict）。
        # クラウドでは gh variable get が 403 でブロックされ常にここへ来る（Issue #133・
        # github-mcp-fallback-patterns.md §2.4。GitHub MCP にも variables の等価ツールなし）。
        print(
            f"⚠️ ループ状態の取得に失敗（gh variable get {_var_name(key)}）。"
            "クラウドでは GitHub Variables が 403 で利用不可のため「状態なし」と断定しないこと。",
            file=sys.stderr,
        )
        return {}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {}


def write_state(
    phase: str,
    session_id: str,
    loop_count: int = 0,
    next_steps: list[str] | None = None,
    key: str = "latest",
    **extra,
) -> bool:
    """構造化ループ状態を GitHub Variables に書き込む。成功時 True を返す。"""
    state = {
        "phase": phase,
        "session_id": session_id,
        "loop_count": loop_count,
        "next_steps": next_steps or [],
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        **extra,
    }
    repo = _get_repo()
    repo_flag = ["-R", repo] if repo else []
    result = subprocess.run(
        ["gh", "variable", "set", _var_name(key),
         "--body", json.dumps(state, ensure_ascii=False),
         *repo_flag],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ループ状態を GitHub Variables で管理する（Issue #99）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read", help="状態を読み込んで JSON で出力する")
    r.add_argument("--key", default="latest", help="状態キー（デフォルト: latest）")

    w = sub.add_parser("write", help="状態を書き込む")
    w.add_argument("--phase", required=True, help="現在のフェーズ名")
    w.add_argument("--session-id", required=True, dest="session_id",
                   help="Claude Code セッション ID（$CLAUDE_CODE_SESSION_ID）")
    w.add_argument("--loop-count", type=int, default=0, dest="loop_count",
                   help="ループ回数（デフォルト: 0）")
    w.add_argument("--next-steps", nargs="*", default=[], dest="next_steps",
                   help="次セッションで実行すべきコマンド一覧（スペース区切りで複数指定可）")
    w.add_argument("--key", default="latest", help="状態キー（デフォルト: latest）")

    args = parser.parse_args()

    if args.cmd == "read":
        state = read_state(args.key)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    elif args.cmd == "write":
        ok = write_state(
            phase=args.phase,
            session_id=args.session_id,
            loop_count=args.loop_count,
            next_steps=args.next_steps,
            key=args.key,
        )
        if not ok:
            print("⚠️ GitHub Variables への書き込みに失敗しました"
                  "（クラウドでは 403 で利用不可・github-mcp-fallback-patterns.md §2.4。"
                  "ローカルでは gh CLI 未設定 or 権限不足）",
                  file=sys.stderr)
            sys.exit(1)
        print(f"✅ ループ状態を書き込みました（key={args.key!r}, phase={args.phase!r}）")


if __name__ == "__main__":
    main()
