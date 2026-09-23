#!/usr/bin/env python3
"""git push が HTTP 403 で失敗するクラウド環境向けの REST API フォールバック。

クラウド環境（GCP / Anthropic インフラ）では git の HTTPS プロキシ設定が
push 操作のみ HTTP 403 を返すことがある（L-079）。本ユーティリティは
GitHub Contents API（PUT /repos/{owner}/{repo}/contents/{path}）を使い、
単一ファイルを直接コミット & プッシュするフォールバック手段を提供する。

使い方:
  # 単一ファイル
  python3 tools/github_push_helper.py \\
      --path content/meta/V001_meta.yaml \\
      --branch claude/foo \\
      --message "fix: V001 meta 更新"

  # 複数ファイル（各ファイルが個別コミットになる点に注意）
  python3 tools/github_push_helper.py \\
      --path a.txt --path b.txt \\
      --branch claude/foo \\
      --message "update"

注意:
  - Contents API は 1 ファイル = 1 コミットのため、複数ファイル指定時は
    git push のような単一コミットにはならない。あくまで push 失敗時の
    フォールバック用途で使用する。
  - GH_TOKEN または GITHUB_TOKEN 環境変数が必要。
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

DEFAULT_REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
API_ROOT = "https://api.github.com"


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise RuntimeError("GH_TOKEN / GITHUB_TOKEN が未設定です")
    return token


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"token {_token()}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code != 404:
            try:
                err_msg = e.read().decode("utf-8", errors="ignore")
                print(f"⚠ GitHub API Error ({e.code}): {err_msg}", file=sys.stderr)
            except Exception:
                pass
        raise


def _get_file_sha(repo: str, path: str, branch: str) -> str | None:
    """既存ファイルの blob SHA を取得する。新規ファイルなら None。"""
    q_path = urllib.parse.quote(path)
    q_branch = urllib.parse.quote(branch)
    url = f"{API_ROOT}/repos/{repo}/contents/{q_path}?ref={q_branch}"
    try:
        result = _request("GET", url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return result.get("sha")


def push_file(path: str, branch: str, message: str, repo: str = DEFAULT_REPO) -> str:
    """単一ファイルを GitHub Contents API でコミット & プッシュする。

    既存ファイルなら blob SHA を取得して更新、新規なら作成する。
    成功時はコミット SHA を返す。
    """
    with open(path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")

    sha = _get_file_sha(repo, path, branch)
    payload = {"message": message, "content": content_b64, "branch": branch}
    if sha:
        payload["sha"] = sha

    q_path = urllib.parse.quote(path)
    url = f"{API_ROOT}/repos/{repo}/contents/{q_path}"
    result = _request("PUT", url, payload)
    return result["commit"]["sha"]


def main():
    parser = argparse.ArgumentParser(
        description="git push HTTP 403 フォールバック（GitHub Contents API でファイルを直接プッシュ）"
    )
    parser.add_argument(
        "--path",
        action="append",
        required=True,
        help="プッシュするファイルパス（複数指定可）",
    )
    parser.add_argument("--branch", required=True, help="プッシュ先ブランチ")
    parser.add_argument("--message", required=True, help="コミットメッセージ")
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"owner/repo（既定: {DEFAULT_REPO}）",
    )
    args = parser.parse_args()

    exit_code = 0
    for path in args.path:
        if not os.path.exists(path):
            print(f"❌ ファイルが存在しません: {path}", file=sys.stderr)
            exit_code = 1
            continue
        try:
            commit_sha = push_file(path, args.branch, args.message, args.repo)
            print(f"✅ プッシュ完了: {path} → commit {commit_sha[:8]}")
        except (urllib.error.URLError, OSError, RuntimeError, KeyError) as e:
            print(f"❌ プッシュ失敗: {path} — {e}", file=sys.stderr)
            exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
