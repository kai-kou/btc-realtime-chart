#!/usr/bin/env python3
"""generate_project_context.py（汎用ベース）

プロジェクトの現状スナップショットを生成し content/context/project_state.md に保存する。
SessionStart フックがこの出力を stdout でセッションコンテキストに注入し、
セッション開始時の現状把握コスト（gh issue list 等の手動実行）を削減する。

収集内容（repo スコープ REST＝`gh api repos/...` 経由。ローカル・クラウド共通経路・Issue #254）:
  - status:in-progress の Issue
  - status:waiting-claude の Issue（上位 N 件）
  - status:waiting-user の Issue
  - オープン PR
  - 直近のコミット

リポジトリは bootstrap 済み（プレースホルダ解決済み）ならその値を最優先でそのまま使う
（下流リポジトリの既定動作）。未解決の場合のみ環境変数 PROJECT_REPO → GITHUB_REPOSITORY →
git remote → kai-kou/btc-realtime-chart のまま、の順で解決する（優先順位の実装は tools/repo_slug.py が正本）。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

# 日時は JST 統一（datetime-rules.md）。日本は DST がないため固定オフセットで正確。
JST = timezone(timedelta(hours=9))
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

REPO = resolve_repo_slug("kai-kou/btc-realtime-chart", env_vars=("PROJECT_REPO", "GITHUB_REPOSITORY"))
REPO_ROOT = Path(__file__).resolve().parent.parent
# 出力先は PROJECT_CONTEXT_OUT で上書き可能（既定の content/ は制作系の慣習のため）。
# 相対パスは CWD 依存で出力が迷子になるのを防ぐため REPO_ROOT 基準に正規化（+ ~ 展開）する。
def _anchored_out() -> Path:
    raw = os.environ.get("PROJECT_CONTEXT_OUT")
    if not raw:
        return REPO_ROOT / "content" / "context" / "project_state.md"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


OUT = _anchored_out()
WAITING_LIMIT = int(os.environ.get("PROJECT_CONTEXT_WAITING_LIMIT", "15"))


def rest_json(path_qs: str):
    """repo スコープ REST（gh api）で JSON を取得。失敗は None（0 件と区別）。

    ローカル実行の一次経路。**クラウドでは gh が未導入で repo スコープ REST も 403 になるため
    通常は必ず失敗する**（2026-07-26 実測・Issue #338/#342）。その場合は render_remote() の
    縮退スナップショット（MCP での確認を促すポインタ）へ落ちる。失敗を空リストに縮退させると
    スナップショットが「（なし）」と誤表示され現状把握を静かに壊すため、None をセンチネルとして返す。
    """
    try:
        out = subprocess.run(
            ["gh", "api", path_qs], capture_output=True, text=True, timeout=20
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout or "null")
    except Exception:
        return None


def fetch_open_issues():
    """open Issue 全件（最大 300 件）を取得し PR を除外する。取得失敗は None。

    ラベル別に 3 回クエリせず 1 回の取得系列でまとめて取り、Python 側でラベル振り分けする
    （SessionStart のタイムアウト内に収める + ラベル付き PR がページ枠を食って Issue が
    無警告欠落する過少報告を防ぐ）。
    """
    items: list = []
    for page in (1, 2, 3):
        chunk = rest_json(f"repos/{REPO}/issues?state=open&per_page=100&page={page}")
        if chunk is None:
            if page == 1:
                return None  # 初回失敗は「取得失敗」（0 件と区別）
            break  # 2 ページ目以降の失敗は取得済み分で打ち切る（部分欠落を許容）
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return [i for i in items if "pull_request" not in i]


def by_label(open_issues, label: str, limit: int):
    """取得済み open Issue からラベル該当分を抽出する（open_issues が None なら None）。"""
    if open_issues is None:
        return None
    return [
        {"number": i.get("number"), "title": i.get("title"),
         "updatedAt": i.get("updated_at")}
        for i in open_issues
        if any(l.get("name") == label for l in i.get("labels", []))
    ][:limit]


def open_prs():
    raw = rest_json(f"repos/{REPO}/pulls?state=open&per_page=30")
    if raw is None:
        return None
    return [
        {"number": p.get("number"), "title": p.get("title"),
         "updatedAt": p.get("updated_at"), "isDraft": p.get("draft", False)}
        for p in raw
    ]


FETCH_FAILED = (
    "⚠ 取得失敗（gh 403/エラー）。0 件と混同しないこと。"
    "クラウドでは `mcp__github__list_issues` / `list_pull_requests` で直接確認する（L-114）\n"
)


def recent_commits():
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "--oneline", "-8"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip().splitlines() if out.returncode == 0 else []
    except Exception:
        return []


def fmt(items, empty="（なし）"):
    if items is None:
        return FETCH_FAILED
    if not items:
        return empty + "\n"
    lines = []
    for it in items:
        title = (it.get("title") or "")[:70]
        lines.append(f"- #{it.get('number')}: {title}")
    return "\n".join(lines) + "\n"


def render_remote(now: str) -> str:
    """縮退スナップショット（REST で Issue/PR が取れない場合のフォールバック）。

    **クラウドでは gh 未導入・repo スコープ REST 403 のため、これが通常経路になる**
    （2026-07-26 実測・Issue #338/#342）。4 ブロックの無情報警告を注入する代わりに、
    MCP 経由確認を促す 1 行ポインタ + git 由来の直近コミットだけを注入する（Issue #249）。
    プロキシが repo REST を再び許可した場合は完全スナップショット側が使われる。
    """
    commits = recent_commits()
    md = [
        f"# プロジェクト状態スナップショット（{now} 更新）\n",
        "> SessionStart フックが自動注入。最新化は `python3 tools/generate_project_context.py`。\n",
        "\n## Issue / PR\n",
        "クラウドでは gh が 403 のため未取得。`mcp__github__list_issues` / "
        "`mcp__github__list_pull_requests` で直接確認する（status:in-progress / "
        "status:waiting-claude / status:waiting-user / open PR・L-114）。\n",
        "\n## 直近のコミット\n",
        ("\n".join(f"- {c}" for c in commits) + "\n") if commits else "（なし）\n",
    ]
    return "".join(md)


def render_full(now: str) -> str | None:
    """完全スナップショット（全環境共通・repo スコープ REST で収集）。

    REST が全滅（クラウドのプロキシ 403 回帰・ローカルの gh 未認証等）した場合は None を
    返し、呼び出し元が render_remote（縮退版）へフォールバックする。
    """
    open_issues = fetch_open_issues()
    in_progress = by_label(open_issues, "status:in-progress", 20)
    waiting_claude = by_label(open_issues, "status:waiting-claude", WAITING_LIMIT)
    waiting_user = by_label(open_issues, "status:waiting-user", 20)
    prs = open_prs()
    if open_issues is None and prs is None:
        return None
    commits = recent_commits()

    md = []
    md.append(f"# プロジェクト状態スナップショット（{now} 更新）\n")
    md.append("> SessionStart フックが自動注入。最新化は `python3 tools/generate_project_context.py`。\n")
    md.append("\n## 作業中 Issue（status:in-progress）\n")
    md.append(fmt(in_progress))
    md.append(f"\n## Claude 対応待ち Issue（status:waiting-claude・上位 {WAITING_LIMIT}）\n")
    md.append(fmt(waiting_claude))
    md.append("\n## ユーザー対応待ち Issue（status:waiting-user）\n")
    md.append(fmt(waiting_user))
    md.append("\n## オープン PR\n")
    if prs is None:
        md.append(FETCH_FAILED)
    elif prs:
        pr_lines = []
        for p in prs:
            draft = "（draft）" if p.get("isDraft") else ""
            pr_lines.append(f"- PR #{p.get('number')}: {(p.get('title') or '')[:70]} {draft}")
        md.append("\n".join(pr_lines) + "\n")
    else:
        md.append("（なし）\n")
    md.append("\n## 直近のコミット\n")
    md.append(("\n".join(f"- {c}" for c in commits) + "\n") if commits else "（なし）\n")
    return "".join(md)


def main():
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    # クラウドも repo スコープ REST で完全スナップショットを生成する（Issue #254）。
    # REST が 403 に回帰して全滅した場合のみ縮退版へフォールバック（Issue #249 のノイズ削減を維持）。
    content = render_full(now) or render_remote(now)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"[project-context] wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
