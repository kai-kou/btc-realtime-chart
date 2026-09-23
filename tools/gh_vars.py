"""
gh_vars.py — GitHub Actions Variables 自動ページング取得ユーティリティ

GitHub API の actions/variables エンドポイントは per_page 上限が 30 のため、
変数が多い場合に複数ページへ分断される問題を自動解決する（Issue #1485）。

使い方（他スクリプトからの import）:
    from tools.gh_vars import load_github_variables
    load_github_variables()  # os.environ に自動反映

使い方（コマンドライン・確認用）:
    python3 tools/gh_vars.py --json     # 全変数を JSON 出力（値はマスク）
    python3 tools/gh_vars.py --set-env  # os.environ への反映確認
    python3 tools/gh_vars.py --key VAR  # 特定の変数値のみ出力
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

_DEFAULT_REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
_PER_PAGE = 100  # GitHub API の最大許容値


def get_all_variables(
    repo: str = _DEFAULT_REPO,
    token: Optional[str] = None,
) -> dict[str, str]:
    """GitHub Actions Variables を複数ページ自動統合取得して辞書形式で返す。

    Args:
        repo: "owner/name" 形式のリポジトリ名
        token: GitHub API トークン。省略時は GH_TOKEN 環境変数を使用。

    Returns:
        {変数名: 値} の辞書。取得失敗時は空辞書を返す。
    """
    _token = token or os.environ.get("GH_TOKEN", "")
    if not _token:
        print("GH_TOKEN が未設定のため GitHub Variables の取得をスキップします", file=sys.stderr)
        return {}

    all_vars: dict[str, str] = {}
    page = 1

    while True:
        url = (
            f"https://api.github.com/repos/{repo}/actions/variables"
            f"?per_page={_PER_PAGE}&page={page}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {_token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # クラウドの egress プロキシは actions/variables を 403 ブロックする
                # （2026-07-02 実測・Issue #133）。urllib でも gh でも同じ 403 で、
                # GitHub MCP にも等価ツールがないため、この経路での取得は不能。
                print(
                    "GitHub Variables 取得不可: HTTP 403（クラウドでは actions/variables が"
                    "プロキシにブロックされる。env は Claude.ai 環境設定 / secrets-broker で"
                    "供給する・github-mcp-fallback-patterns.md §2.4）",
                    file=sys.stderr,
                )
            else:
                print(f"GitHub Variables 取得エラー（page={page}）: {e}", file=sys.stderr)
            break
        except Exception as e:
            print(f"GitHub Variables 取得エラー（page={page}）: {e}", file=sys.stderr)
            break

        variables = data.get("variables", [])
        if not variables:
            break

        for v in variables:
            all_vars[v["name"]] = v["value"]

        if len(all_vars) >= data.get("total_count", 0):
            break

        page += 1

    return all_vars


def load_github_variables(
    repo: str = _DEFAULT_REPO,
    token: Optional[str] = None,
    overwrite: bool = False,
) -> int:
    """GitHub Actions Variables を取得して os.environ に反映する。

    Args:
        repo: リポジトリ名（"owner/name" 形式）
        token: GitHub API トークン。省略時は GH_TOKEN 環境変数を使用。
        overwrite: True の場合、既存の環境変数を上書きする（デフォルト: False）

    Returns:
        反映した変数の件数。
    """
    variables = get_all_variables(repo=repo, token=token)
    if not variables:
        return 0

    count = 0
    for name, value in variables.items():
        if overwrite or name not in os.environ:
            os.environ[name] = value
            count += 1

    if count > 0:
        print(f"✅ GitHub Variables を読み込みました（{count}件 / 総{len(variables)}件）")
    return count


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GitHub Actions Variables 一覧取得")
    parser.add_argument("--repo", default=_DEFAULT_REPO, help="リポジトリ名（owner/name）")
    parser.add_argument("--key", help="指定した変数名の値のみ出力")
    parser.add_argument("--set-env", action="store_true", help="os.environ 反映のシミュレーション出力")
    parser.add_argument("--json", dest="json_output", action="store_true", help="JSON 形式で出力")
    args = parser.parse_args()

    variables = get_all_variables(repo=args.repo)

    if not variables:
        print("変数が取得できませんでした（GH_TOKEN を確認してください）", file=sys.stderr)
        sys.exit(1)

    if args.key:
        value = variables.get(args.key)
        if value is None:
            print(f"変数 '{args.key}' は存在しません", file=sys.stderr)
            sys.exit(1)
        # --key は値をそのまま出力（スクリプトへのパイプ用途）
        # ⚠️ ターミナルで実行すると平文が表示されるため、目視確認目的には使わないこと
        print(value)
    elif args.json_output:
        # セキュリティ: 値はマスクしてキー一覧のみ出力（--json でも値は隠す）
        print(json.dumps({k: "***" for k in variables}, ensure_ascii=False, indent=2))
    elif args.set_env:
        print(f"取得した変数数: {len(variables)}")
        for name in sorted(variables.keys()):
            existing = "（既存: 上書き不要）" if os.environ.get(name) else "（新規）"
            print(f"  {name} {existing}")
    else:
        print(f"取得した変数数: {len(variables)}")
        for name in sorted(variables.keys()):
            print(f"  {name}")
    sys.exit(0)
