#!/usr/bin/env python3
"""owner/repo（GitHub repo slug）解決の共有ヘルパー（Issue #215）。

【背景】
配布テンプレート提供元である本リポジトリ自身は bootstrap.sh を自分に適用しない
（適用すると下流の apply-base 取り込み時にプレースホルダごと失われるため）。
本リポジトリ自身に対して運用ルーティンを実行するケース（R-1 等）では
`kai-kou/btc-realtime-chart` 等の未置換プレースホルダが残るため、その場合のみ
git remote から動的に補う。下流（bootstrap 済み）リポジトリでは git 呼び出し・
env 参照をせず、決定論的にプレースホルダ置換後の値をそのまま返す。

【正本】
owner/repo 解決ロジックの実装はこのファイルが唯一の正本。個別ツールでの
再実装は drift の温床になるため行わない（#215）。既存ツールから呼び出す場合は
`resolve_repo_slug()` を import して使う。

使い方:
  python3 tools/repo_slug.py --self-test
"""

from __future__ import annotations

import os
import re
import subprocess


def has_placeholder(value: str) -> bool:
    return "__" in value


def parse_owner_repo(url: str) -> tuple[str, str] | None:
    """git remote origin の URL 文字列から (owner, repo) を抽出する（純関数・テスト容易）。

    github.com URL（`git@github.com:owner/repo.git` / `https://github.com/owner/repo`）と、
    scheduled trigger 実行時のローカル git プロキシ形式
    （例: `http://local_proxy@127.0.0.1:{port}/git/{owner}/{repo}`）の双方に対応する。
    """
    url = url.strip()
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", url)
    if not m:
        m = re.search(r"/git/([^/]+)/(.+?)(?:\.git)?/?$", url)
    if not m:
        return None
    owner, repo_name = m.group(1), m.group(2)
    if has_placeholder(owner) or has_placeholder(repo_name):
        return None
    return owner, repo_name


def repo_from_git_remote(cwd: str | None = None, timeout: int = 5) -> str | None:
    """git remote origin から owner/repo を導出する（失敗時は None）。"""
    try:
        out = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    parsed = parse_owner_repo(out.stdout)
    return f"{parsed[0]}/{parsed[1]}" if parsed else None


def resolve_repo_slug(
    placeholder: str = "kai-kou/btc-realtime-chart",
    env_vars: tuple = ("GITHUB_REPOSITORY",),
    cwd: str | None = None,
) -> str:
    """owner/repo を解決する。優先順位: env_vars → git remote → placeholder のまま。

    placeholder が既に置換済み（"__" を含まない = bootstrap 済み下流リポジトリ）なら
    git 呼び出し・env 参照を一切せず即座にそれを返す（下流の決定論的動作を維持）。
    """
    if not has_placeholder(placeholder):
        return placeholder
    for env_var in env_vars:
        v = os.environ.get(env_var)
        if v and "/" in v and not has_placeholder(v):
            return v
    return repo_from_git_remote(cwd=cwd) or placeholder


def _self_test() -> None:
    # bootstrap の sed 置換（s#kai-kou/btc-realtime-chart#...#g 等）はファイル全体を対象にするため、
    # 「未置換プレースホルダ」を検証する意図のリテラルをそのまま書くと、下流（bootstrap 済み）
    # リポジトリではこの self-test 自身のリテラルまで実スラッグに書き換えられ、以降の assert が
    # 恒久 FAIL する（#226）。実行時の文字列結合で組み立て、sed が拾えない形にする。
    _ph = "".join(["__", "OWNER", "__", "/", "__", "REPO", "__"])

    # 置換済み（下流リポジトリ）は git/env に触れず即返す
    assert resolve_repo_slug("example-owner/example-repo") == "example-owner/example-repo"

    # github.com URL 形式
    assert parse_owner_repo("git@github.com:example-owner/example-repo.git") == (
        "example-owner", "example-repo",
    )
    assert parse_owner_repo("https://github.com/example-owner/example-repo") == (
        "example-owner", "example-repo",
    )
    # ローカル git プロキシ形式（scheduled trigger 実行時・#220）
    assert parse_owner_repo(
        "http://local_proxy@127.0.0.1:12345/git/example-owner/example-repo"
    ) == ("example-owner", "example-repo")
    # 未置換プレースホルダの remote（テスト用等）は None
    assert parse_owner_repo(f"https://github.com/{_ph}") is None
    # 認識できない形式は None
    assert parse_owner_repo("not a url") is None

    # env_vars 優先順位（プレースホルダのままの env は無視）
    os.environ["_REPO_SLUG_SELFTEST"] = "acme/widgets"
    try:
        assert resolve_repo_slug(
            _ph, env_vars=("_REPO_SLUG_SELFTEST",)
        ) == "acme/widgets"
    finally:
        del os.environ["_REPO_SLUG_SELFTEST"]

    # env なし・git remote 解決不可（存在しない cwd）→ placeholder のまま
    assert resolve_repo_slug(
        _ph, env_vars=(), cwd="/nonexistent-dir-for-selftest",
    ) == _ph

    print("OK: repo_slug self-test passed")


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    print(resolve_repo_slug())
