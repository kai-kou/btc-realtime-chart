#!/usr/bin/env python3
"""
GitHub Repository Variables セットアップスクリプト

既存の環境変数を GitHub Repository Variables に一括登録する。
セッション開始時に session-start.sh が gh variable list で取得し、
CLAUDE_ENV_FILE に書き出すことで環境変数として利用可能になる。

Usage:
    # 現在の環境変数から GitHub Variables に登録（対話式）
    python3 tools/setup_github_variables.py

    # .env ファイルから一括登録
    python3 tools/setup_github_variables.py --from-env-file .env

    # 登録済み変数の一覧表示
    python3 tools/setup_github_variables.py --list

    # 特定の変数を設定
    python3 tools/setup_github_variables.py --set SLACK_BOT_TOKEN=xoxb-...

    # 特定の変数を削除
    python3 tools/setup_github_variables.py --delete SLACK_BOT_TOKEN
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mask_secrets import mask_value  # noqa: E402
from repo_slug import resolve_repo_slug  # noqa: E402

REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")

# 汎用ベースのコア管理変数（slack-notify モジュール）。
# プロジェクト固有の変数（外部 API のトークン等）は環境変数 MANAGED_GITHUB_VARS
# （カンマ区切り）で注入する。これにより汎用ベースのコードにドメイン固有変数を
# ハードコードしない（脱ドメイン）。
#   例: export MANAGED_GITHUB_VARS="YOUR_API_KEY,YOUR_SERVICE_TOKEN"
CORE_MANAGED_VARS = [
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
    "SLACK_APPROVAL_CHANNEL_ID",
    "SLACK_PUBLISH_CHANNEL_ID",
    "SLACK_CODE_CHANNEL_ID",
    "SLACK_MENTION_USER_ID",
]

# GH_TOKEN は GitHub Variables からの読み取りに必要なため、
# Claude.ai 環境変数に直接設定する必要がある（鶏と卵問題）。
# Variables へ登録すると意図しない露出・運用混乱につながるため常に除外する。
BOOTSTRAP_VARS = ["GH_TOKEN"]

_extra_managed = os.environ.get("MANAGED_GITHUB_VARS", "")
# CORE + env 注入を結合し、BOOTSTRAP_VARS を除外したうえで重複を除去（順序保持）。
_combined = CORE_MANAGED_VARS + [v.strip() for v in _extra_managed.split(",") if v.strip()]
MANAGED_VARS = list(dict.fromkeys(v for v in _combined if v not in BOOTSTRAP_VARS))


def run_gh(args: list[str], input_data: str | None = None) -> str:
    """gh CLI を実行して結果を返す"""
    cmd = ["gh"] + args + ["-R", REPO]
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def list_variables() -> dict[str, str]:
    """登録済みの GitHub Variables を取得"""
    output = run_gh(["variable", "list", "--json", "name,value"])
    if not output:
        return {}
    return {v["name"]: v["value"] for v in json.loads(output)}


def set_variable(name: str, value: str) -> None:
    """GitHub Variable を設定"""
    run_gh(["variable", "set", name, "--body", value])


def delete_variable(name: str) -> None:
    """GitHub Variable を削除"""
    run_gh(["variable", "delete", name])


def cmd_list() -> None:
    """登録済み変数の一覧表示"""
    variables = list_variables()
    if not variables:
        print("GitHub Variables に登録済みの変数はありません。")
        return
    print(f"GitHub Variables ({REPO}): {len(variables)} 件")
    print("-" * 60)
    for name in sorted(variables.keys()):
        masked = mask_value(variables[name])
        print(f"  {name} = {masked}")


def cmd_set(pairs: list[str]) -> None:
    """指定された KEY=VALUE ペアを設定"""
    for pair in pairs:
        if "=" not in pair:
            print(f"Error: '{pair}' は KEY=VALUE 形式で指定してください", file=sys.stderr)
            sys.exit(1)
        name, value = pair.split("=", 1)
        set_variable(name, value)
        print(f"  ✅ {name} を設定しました")


def cmd_delete(names: list[str]) -> None:
    """指定された変数を削除"""
    for name in names:
        try:
            delete_variable(name)
            print(f"  ✅ {name} を削除しました")
        except RuntimeError as e:
            print(f"  ❌ {name} の削除に失敗: {e}", file=sys.stderr)


def cmd_from_env() -> None:
    """現在の環境変数から GitHub Variables に登録（対話式）"""
    existing = list_variables()
    to_set: list[tuple[str, str]] = []

    print("現在の環境変数を GitHub Variables に登録します。")
    print(f"対象リポジトリ: {REPO}")
    print()

    for name in MANAGED_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        status = "（上書き）" if name in existing else "（新規）"
        print(f"  {name} = {mask_value(value)} {status}")
        to_set.append((name, value))

    if not to_set:
        print("環境変数に設定済みの管理対象変数がありません。")
        print("Claude.ai の環境変数設定で変数が設定されている状態で実行してください。")
        return

    print()
    print(f"{len(to_set)} 件の変数を GitHub Variables に登録します。")

    for name, value in to_set:
        set_variable(name, value)
        print(f"  ✅ {name}")

    print()
    print("登録完了。")
    print()
    print("次のステップ:")
    print("  1. Claude.ai の環境変数設定から GH_TOKEN 以外の変数を削除できます")
    print("  2. 次回セッションから session-start.sh が自動的に GitHub Variables を読み込みます")
    print()
    print("⚠️  GH_TOKEN だけは Claude.ai 環境変数に残してください（gh CLI 認証用）")


def cmd_from_env_file(filepath: str) -> None:
    """.env ファイルから GitHub Variables に一括登録"""
    if not os.path.exists(filepath):
        print(f"Error: ファイルが見つかりません: {filepath}", file=sys.stderr)
        sys.exit(1)

    pairs: list[tuple[str, str]] = []
    with open(filepath, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(f"Warning: L{line_num}: 無視 (KEY=VALUE 形式ではない): {line}", file=sys.stderr)
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("'\"")
            if name in BOOTSTRAP_VARS:
                print(f"  ⏭️  {name}: スキップ (Claude.ai 環境変数で直接設定が必要)")
                continue
            pairs.append((name, value))

    if not pairs:
        print("登録対象の変数がありません。")
        return

    print(f".env ファイルから {len(pairs)} 件の変数を GitHub Variables に登録します。")
    for name, value in pairs:
        set_variable(name, value)
        print(f"  ✅ {name} = {mask_value(value)}")

    print()
    print("登録完了。")


def main() -> None:
    args = sys.argv[1:]

    if not args:
        cmd_from_env()
    elif args[0] == "--list":
        cmd_list()
    elif args[0] == "--set" and len(args) > 1:
        cmd_set(args[1:])
    elif args[0] == "--delete" and len(args) > 1:
        cmd_delete(args[1:])
    elif args[0] == "--from-env-file" and len(args) > 1:
        cmd_from_env_file(args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
