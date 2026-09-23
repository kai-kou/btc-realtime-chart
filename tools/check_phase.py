#!/usr/bin/env python3
"""フェーズ判定コマンド（要件定義フェーズ / 実装フェーズ）。

判定基準（正本: docs/rules/phase-boundary-rules.md・D-15）:
  `sp:N` ラベルがあり、かつ `status:in-progress` で着手中 → 実装フェーズ
  それ以外（`sp:N` が無い、または `sp:N` はあるが未着手）      → 要件定義フェーズ

判定は既存の Issue ラベル状態だけで行う。新しい状態（専用フラグ等）は発明しない。

クラウド実行環境では `gh` が使えないことが多い（CLAUDE.md「gh CLI / GitHub 操作」）。
その場合は `mcp__github__issue_read` 等で取得したラベル一覧を `--labels` に渡す。

Usage:
  python3 tools/check_phase.py --labels "sp:3,status:in-progress"
  python3 tools/check_phase.py --issue 6                 # ローカル実行・gh 疎通時のみ
  python3 tools/check_phase.py --self-test
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys

REQUIREMENTS = "requirements"
IMPLEMENTATION = "implementation"

PHASE_LABEL_JA = {
    REQUIREMENTS: "要件定義フェーズ",
    IMPLEMENTATION: "実装フェーズ",
}

SP_LABEL_RE = re.compile(r"^sp:\d+$")


def determine_phase(labels: list[str]) -> str:
    """既存の Issue ラベル状態だけでフェーズを判定する（新しい状態を発明しない・D-15）。"""
    has_sp = any(SP_LABEL_RE.match(label) for label in labels)
    in_progress = "status:in-progress" in labels
    if has_sp and in_progress:
        return IMPLEMENTATION
    return REQUIREMENTS


def fetch_labels_via_gh(issue_number: int, repo: str | None) -> list[str]:
    """`gh` 経由でラベルを取得する（ローカル実行・gh 疎通時のみ）。

    クラウドで `gh` が不在/403 の場合は例外を投げ、呼び出し元へ
    `mcp__github__issue_read` フォールバックを案内する（L-114）。
    """
    cmd = ["gh", "issue", "view", str(issue_number), "--json", "labels"]
    if repo:
        cmd += ["-R", repo]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as e:
        raise RuntimeError(
            "GH_UNAVAILABLE: gh コマンドが見つかりません（クラウド既定・CLAUDE.md 参照）。"
            " mcp__github__issue_read(method=\"get\") でラベルを取得し --labels に渡してください。"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            "GH_UNAVAILABLE: gh でラベルを取得できませんでした（クラウドの 403 等）。"
            " mcp__github__issue_read(method=\"get\") でラベルを取得し --labels に渡してください。\n"
            f"{result.stderr.strip()}"
        )
    data = json.loads(result.stdout)
    return [label["name"] for label in data.get("labels", [])]


def _run_self_test() -> int:
    cases: list[tuple[list[str], str]] = [
        (["sp:3", "status:in-progress"], IMPLEMENTATION),
        (["sp:3"], REQUIREMENTS),
        ([], REQUIREMENTS),
        (["status:in-progress"], REQUIREMENTS),
        (["sp:3", "status:waiting-claude"], REQUIREMENTS),
        (["sp:1", "status:in-progress", "priority:high"], IMPLEMENTATION),
        (["sp:8", "status:waiting-user"], REQUIREMENTS),
    ]
    failed = 0
    for labels, expected in cases:
        actual = determine_phase(labels)
        ok = actual == expected
        print(f"{'PASS' if ok else 'FAIL'}: labels={labels!r} expected={expected} actual={actual}")
        if not ok:
            failed += 1
    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", help="カンマ区切りのラベル一覧（例: sp:3,status:in-progress）")
    parser.add_argument("--issue", type=int, help="Issue 番号（gh 疎通時のみ。クラウドでは --labels を使う）")
    parser.add_argument("--repo", help="owner/repo（--issue と併用）")
    parser.add_argument("--self-test", action="store_true", help="判定ロジックの自己テストを実行する")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    if args.labels is not None:
        labels = [label.strip() for label in args.labels.split(",") if label.strip()]
    elif args.issue is not None:
        try:
            labels = fetch_labels_via_gh(args.issue, args.repo)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(3)
    else:
        parser.error("--labels か --issue か --self-test のいずれかを指定してください")
        return

    phase = determine_phase(labels)
    print(PHASE_LABEL_JA[phase])
    sys.exit(0)


if __name__ == "__main__":
    main()
