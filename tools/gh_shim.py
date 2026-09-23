#!/usr/bin/env python3
"""gh シム — クラウド egress プロキシ配下で gh コマンド由来の 403 を排除する PATH ラッパー。

背景（Issue #254・2026-07-14 実機検証）:
- クラウド（CLAUDE_CODE_REMOTE=true）のプロキシは repo スコープ REST（gh api repos/{o}/{r}/...）を
  許可する一方、GraphQL（gh issue/pr list・repo view 等の高レベルコマンドの実体）・search・
  非 repo REST・Actions variables/secrets は 403 のまま。
- プロキシの許可範囲は短期間に変化する（06-30 → 07-02 → 07-13 → 07-14 で 3 回変化）ため、
  静的ブロックリストは腐る。本シムは「GraphQL 依存コマンドの REST 変換」+「実行後の 403 検知 →
  MCP 代替ガイダンス付与（アノテート）」のハイブリッドで、挙動変化に自動追従する。

動作:
1. ローカル（CLAUDE_CODE_REMOTE != true）→ 実 gh へ即 exec（挙動不変）。GH_SHIM=force で強制有効。
2. クラウド:
   a. GraphQL 依存サブコマンド（issue/pr/label/repo/release の read/write 主要形）で、引数を
      完全に解釈できる場合 → repo スコープ REST（実 gh の `gh api`）へ透過変換。
   b. それ以外（未対応フラグ含む）→ 実 gh へパススルーし、403 系エラーを検知したら
      エラーカテゴリ別の MCP 代替ガイダンスを stderr に付与（exit code は gh のまま維持）。
   c. 変換実行中に REST 自体が 403 化（プロキシ回帰）した場合も b と同じアノテートで報告。
3. GH_SHIM=off で完全パススルー（トラブル時の脱出ハッチ）。

診断:
  gh --shim-self-test   … オフライン自己テスト（分類・変換計画・フィールドマップ）
  gh --shim-doctor      … ライブ疎通マトリクス（api user / repos REST / GraphQL / search）
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

# ---------------------------------------------------------------- 基盤


def find_real_gh() -> str | None:
    """自分自身（シム）を除いた PATH から実 gh を探す。

    シム（.claude/bin/gh ラッパー）自身を返すと execv の無限自己ループになるため、
    シム系は必ずスキップし、見つからなければ None を返す（which フォールバック禁止）。
    """
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, "gh")
        if not (os.path.isfile(p) and os.access(p, os.X_OK)):
            continue
        try:
            if os.path.realpath(p) == os.path.realpath(__file__):
                continue
            with open(p, "rb") as f:
                if b"gh_shim.py" in f.read(400):
                    continue
        except OSError:
            continue
        return p
    return None


def is_cloud() -> bool:
    mode = os.environ.get("GH_SHIM", "").lower()
    if mode == "off":
        return False
    if mode == "force":
        return True
    return os.environ.get("CLAUDE_CODE_REMOTE", "") == "true"


def repo_slug(argv_repo: str | None) -> str | None:
    """-R/--repo 指定があればそれを、なければ SSOT（tools/repo_slug.py・#215）で解決する。"""
    if argv_repo:
        return argv_repo
    return _repo_slug_from_env()


@functools.lru_cache(maxsize=1)
def _repo_slug_from_env() -> str | None:
    """owner/repo の解決。実装の正本は tools/repo_slug.py（再実装 drift 防止・#215）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from repo_slug import has_placeholder, resolve_repo_slug
    except ImportError:
        return None
    slug = resolve_repo_slug(
        "kai-kou/btc-realtime-chart", env_vars=("PROJECT_REPO", "GITHUB_REPOSITORY")
    )
    if not slug or "/" not in slug or has_placeholder(slug):
        return None
    return slug


# ---------------------------------------------------------------- 403 アノテート

# 403 エラーカテゴリ → MCP 代替ガイダンス（文言はプロキシの実メッセージに合わせる・2026-07-14 実測）
ERROR_GUIDANCE: list[tuple[str, str]] = [
    (
        "GraphQL query is not enabled",
        "GraphQL はプロキシで遮断。このエラーが出た時点でシムの変換対象外（または PATH に .claude/bin が"
        "未注入・`gh --shim-doctor` で確認）。同じコマンドの再実行では解決しない → repo スコープ REST"
        "（gh api repos/{o}/{r}/...）か MCP（mcp__github__*）を使う"
        "（SSOT: docs/rules/github-mcp-fallback-patterns.md §2）",
    ),
    (
        "GraphQL proxying is not enabled",
        "GraphQL はプロキシで遮断。→ MCP（mcp__github__*）または repo スコープ REST（gh api repos/{o}/{r}/...）を使う",
    ),
    (
        "sessions are bound to their configured repositories",
        "非 repo REST / search はプロキシで遮断。→ mcp__github__search_issues / search_code / "
        "search_pull_requests / search_users（repo: 修飾でスコープ内に限定）を使う",
    ),
    (
        "GitHub Actions path is not permitted",
        "Actions variables/secrets はプロキシで遮断（MCP にも等価ツールなし）。→ env は Claude.ai 環境設定 / "
        "secrets-broker で供給する（docs/rules/github-mcp-fallback-patterns.md §2.4）",
    ),
    (
        "Resource not accessible by integration",
        "プロキシは通過したが GitHub App トークンの権限不足（checks/actions read 等）。→ "
        "mcp__github__actions_list / actions_get / get_job_logs / get_check_run を使う",
    ),
    (
        "GitHub access is not enabled for this session",
        "repo スコープ REST がプロキシで遮断（許可範囲が再変更された可能性）。→ MCP（mcp__github__*）へ切替。"
        "docs/rules/github-mcp-fallback-patterns.md の検証マトリクス更新も検討",
    ),
    (
        "none of the git remotes configured",
        "クラウドの origin は git プロキシ URL のため gh が repo を推定できない。→ `-R {owner}/{repo}` を明示するか、"
        "MCP（mcp__github__*）を使う",
    ),
]

# パススルー時に -R を自動注入する repo スコープ サブコマンド（gh が proxy 形式 origin から
# リポジトリを推定できないため。-R/--repo 明示時は注入しない）
REPO_SCOPED_SUBCOMMANDS = {"issue", "pr", "label", "release", "run", "workflow",
                           "variable", "secret", "milestone"}


def peek_repo_flag(rest: list[str]) -> str | None:
    """サブコマンド引数列から -R/--repo の値を先読みする（値なしの末尾 -R は None）。"""
    for i, a in enumerate(rest):
        if a in ("-R", "--repo") and i + 1 < len(rest):
            return rest[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return None


def maybe_inject_repo(argv: list[str]) -> list[str]:
    if len(argv) < 2 or argv[0] not in REPO_SCOPED_SUBCOMMANDS:
        return argv
    if any(a in ("-R", "--repo") or a.startswith("--repo=") for a in argv):
        return argv
    slug = repo_slug(None)
    return [*argv, "-R", slug] if slug else argv


def annotate_and_exit(stdout: bytes, stderr: bytes, code: int) -> None:
    """パススルー実行の結果を中継し、403 系エラーなら stderr にガイダンスを付与する。"""
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    combined = (stdout + b"\n" + stderr).decode("utf-8", "replace")
    if code != 0:
        for signature, guidance in ERROR_GUIDANCE:
            if signature in combined:
                sys.stderr.write(f"\n[gh-shim] {guidance}\n")
                break
        else:
            # 既知シグネチャに一致しない 403（プロキシ文言は変動する・07-13 実例）にも
            # 汎用ガイダンスを出す（「403 = トークン権限不足」の誤診断・リトライ浪費を防ぐ）
            if "HTTP 403" in combined or '"status": "403"' in combined:
                sys.stderr.write(
                    "\n[gh-shim] 未知の 403（プロキシ文言が変化した可能性）。リトライでは解決しない。"
                    "`gh --shim-doctor` で現在の許可範囲を確認し、MCP（mcp__github__*）へ切替する"
                    "（SSOT: docs/rules/github-mcp-fallback-patterns.md）\n"
                )
    sys.exit(code)


def passthrough(real_gh: str, argv: list[str]) -> None:
    proc = subprocess.run([real_gh, *maybe_inject_repo(argv)], capture_output=True)
    annotate_and_exit(proc.stdout, proc.stderr, proc.returncode)


def exec_real(real_gh: str, argv: list[str]) -> None:
    os.execv(real_gh, [real_gh, *argv])


# ---------------------------------------------------------------- REST ヘルパ

class RestError(Exception):
    def __init__(self, stdout: bytes, stderr: bytes, code: int):
        super().__init__("gh api failed")
        self.stdout, self.stderr, self.code = stdout, stderr, code


def gh_api(real_gh: str, path: str, method: str = "GET",
           body: dict | None = None) -> object:
    """repo スコープ REST を実 gh の `gh api` で呼ぶ（ページングは gh_api_list が担う）。"""
    cmd = [real_gh, "api", "--method", method, path]
    if body is not None:
        cmd += ["--input", "-"]
        proc = subprocess.run(cmd, input=json.dumps(body).encode(), capture_output=True)
    else:
        proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RestError(proc.stdout, proc.stderr, proc.returncode)
    out = proc.stdout.decode("utf-8", "replace").strip()
    return json.loads(out) if out else None


def gh_api_list(real_gh: str, path: str, params: dict, limit: int) -> list:
    """limit 件まで REST リストを取得（per_page + page ループ）。"""
    results: list = []
    page = 1
    per_page = min(limit, 100)
    while len(results) < limit:
        q = dict(params, per_page=per_page, page=page)
        qs = urllib.parse.urlencode(q)
        chunk = gh_api(real_gh, f"{path}?{qs}")
        if not isinstance(chunk, list) or not chunk:
            break
        results.extend(chunk)
        if len(chunk) < per_page:
            break
        page += 1
    return results[:limit]


# ---------------------------------------------------------------- フィールドマップ（REST → gh --json）

def _actor(u: dict | None) -> dict:
    u = u or {}
    return {"login": u.get("login", ""), "is_bot": u.get("type") == "Bot",
            "id": u.get("node_id", ""), "name": ""}


def _labels(item: dict) -> list:
    return [{"id": l.get("node_id", ""), "name": l.get("name", ""),
             "color": l.get("color", ""), "description": l.get("description") or ""}
            for l in item.get("labels", [])]


def _milestone(item: dict) -> dict | None:
    m = item.get("milestone")
    if not m:
        return None
    return {"number": m.get("number"), "title": m.get("title", ""),
            "description": m.get("description") or "", "dueOn": m.get("due_on")}


ISSUE_FIELDS = {
    "number": lambda i: i.get("number"),
    "title": lambda i: i.get("title", ""),
    "body": lambda i: i.get("body") or "",
    "state": lambda i: (i.get("state") or "").upper(),
    "stateReason": lambda i: i.get("state_reason"),
    "url": lambda i: i.get("html_url", ""),
    "createdAt": lambda i: i.get("created_at"),
    "updatedAt": lambda i: i.get("updated_at"),
    "closedAt": lambda i: i.get("closed_at"),
    "author": lambda i: _actor(i.get("user")),
    "labels": _labels,
    "assignees": lambda i: [_actor(a) for a in i.get("assignees", [])],
    "milestone": _milestone,
    "comments": lambda i: i.get("comments", 0),
    "id": lambda i: i.get("node_id", ""),
}

PR_FIELDS = {
    "number": lambda p: p.get("number"),
    "title": lambda p: p.get("title", ""),
    "body": lambda p: p.get("body") or "",
    "state": lambda p: "MERGED" if p.get("merged_at") else (p.get("state") or "").upper(),
    "url": lambda p: p.get("html_url", ""),
    "createdAt": lambda p: p.get("created_at"),
    "updatedAt": lambda p: p.get("updated_at"),
    "closedAt": lambda p: p.get("closed_at"),
    "mergedAt": lambda p: p.get("merged_at"),
    "isDraft": lambda p: p.get("draft", False),
    "headRefName": lambda p: (p.get("head") or {}).get("ref", ""),
    "baseRefName": lambda p: (p.get("base") or {}).get("ref", ""),
    "headRefOid": lambda p: (p.get("head") or {}).get("sha", ""),
    "author": lambda p: _actor(p.get("user")),
    # PR 著者の GitHub 上の立場（OWNER/MEMBER/COLLABORATOR/CONTRIBUTOR/NONE 等）。
    # REST の一覧・単体取得応答どちらにも author_association が含まれる（#379・
    # check_pending_pr_reviews.py の _is_claude_branch 著者検証が依存する）。
    "authorAssociation": lambda p: p.get("author_association") or "",
    "labels": _labels,
    "assignees": lambda p: [_actor(a) for a in p.get("assignees", [])],
    "milestone": _milestone,
    "reviewRequests": lambda p: [_actor(u) for u in p.get("requested_reviewers", [])],
    "mergeable": lambda p: {True: "MERGEABLE", False: "CONFLICTING"}.get(p.get("mergeable"), "UNKNOWN"),
    "id": lambda p: p.get("node_id", ""),
    # additions/deletions/changedFiles は REST では単体 GET（pr view）にのみ含まれる。
    # list 応答には無いため None（欠測）を返す（0 と偽装しない）
    "additions": lambda p: p.get("additions"),
    "deletions": lambda p: p.get("deletions"),
    "changedFiles": lambda p: p.get("changed_files"),
}

REPO_FIELDS = {
    "name": lambda r: r.get("name", ""),
    "nameWithOwner": lambda r: r.get("full_name", ""),
    "owner": lambda r: {"login": (r.get("owner") or {}).get("login", "")},
    "description": lambda r: r.get("description") or "",
    "url": lambda r: r.get("html_url", ""),
    "isPrivate": lambda r: r.get("private", False),
    "defaultBranchRef": lambda r: {"name": r.get("default_branch", "")},
    "sshUrl": lambda r: r.get("ssh_url", ""),
}


def map_fields(item: dict, fields: list[str], table: dict) -> dict:
    out = {}
    for f in fields:
        fn = table.get(f)
        if fn is None:
            raise KeyError(f)
        out[f] = fn(item)
    return out


# ---------------------------------------------------------------- 出力

def emit(data: object, jq_expr: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False)
    if jq_expr:
        jq = shutil.which("jq")
        if not jq:
            # --jq を履行できないのに exit 0 で生 JSON を返すと呼び出し側が黙って壊れる
            sys.stderr.write("[gh-shim] jq が見つからないため --jq を評価できません（生 JSON へ切替せず失敗させる）\n")
            sys.exit(1)
        proc = subprocess.run([jq, "-r", jq_expr], input=text.encode(), capture_output=True)
        sys.stdout.buffer.write(proc.stdout)
        sys.stderr.buffer.write(proc.stderr)
        sys.exit(proc.returncode)
    print(text)


def emit_table(rows: list[dict], columns: list[str]) -> None:
    for r in rows:
        print("\t".join(str(r.get(c, "")) for c in columns))


# ---------------------------------------------------------------- 引数解釈

class Args:
    """gh サブコマンド引数の最小パーサ。未対応フラグ検出時は Unsupported を投げる。"""

    def __init__(self, argv: list[str]):
        self.argv = argv

    def parse(self, spec: dict[str, bool]) -> tuple[dict, list[str]]:
        """spec: フラグ名 → 値を取るか。戻り値: (フラグ辞書, 位置引数)。"""
        flags: dict[str, list[str]] = {}
        pos: list[str] = []
        i = 0
        while i < len(self.argv):
            a = self.argv[i]
            if a == "--":
                pos.extend(self.argv[i + 1:])
                break
            if a.startswith("-"):
                name, eq, val = a.partition("=")
                if name not in spec:
                    raise Unsupported(a)
                if spec[name]:  # 値を取る
                    if eq:
                        flags.setdefault(name, []).append(val)
                    else:
                        i += 1
                        if i >= len(self.argv):
                            raise Unsupported(a)
                        flags.setdefault(name, []).append(self.argv[i])
                else:
                    flags.setdefault(name, []).append("")
            else:
                pos.append(a)
            i += 1
        return flags, pos


class Unsupported(Exception):
    pass


def first(flags: dict, *names: str, default: str | None = None) -> str | None:
    for n in names:
        if n in flags:
            return flags[n][0]
    return default


def all_of(flags: dict, *names: str) -> list[str]:
    out: list[str] = []
    for n in names:
        out.extend(flags.get(n, []))
    return out


# ---------------------------------------------------------------- 変換（translate）

def t_issue_list(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({
        "-R": True, "--repo": True, "--state": True, "-s": True,
        "--label": True, "-l": True, "--limit": True, "-L": True,
        "--assignee": True, "-a": True, "--author": True, "-A": True,
        "--json": True, "--jq": True, "-q": True,
    })
    if pos:
        raise Unsupported(pos[0])
    limit = int(first(flags, "--limit", "-L", default="30"))
    params: dict = {"state": first(flags, "--state", "-s", default="open")}
    labels = all_of(flags, "--label", "-l")
    if labels:
        params["labels"] = ",".join(labels)  # REST の labels は AND 意味論（gh と同じ）
    if first(flags, "--assignee", "-a"):
        params["assignee"] = first(flags, "--assignee", "-a")
    if first(flags, "--author", "-A"):
        params["creator"] = first(flags, "--author", "-A")
    items = gh_api_list(real_gh, f"repos/{slug}/issues", params, limit + 50)
    items = [i for i in items if "pull_request" not in i][:limit]  # /issues は PR を含むため除外
    json_fields = first(flags, "--json")
    if json_fields:
        rows = [map_fields(i, json_fields.split(","), ISSUE_FIELDS) for i in items]
        emit(rows, first(flags, "--jq", "-q"))
    else:
        emit_table([{"number": i["number"], "state": i["state"].upper(),
                     "title": i["title"],
                     "labels": ",".join(l["name"] for l in i.get("labels", []))}
                    for i in items], ["number", "state", "title", "labels"])


def t_issue_view(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--json": True,
                             "--jq": True, "-q": True, "--comments": False})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("issue view <number> 以外の形")
    num = pos[0]
    item = gh_api(real_gh, f"repos/{slug}/issues/{num}")
    json_fields = first(flags, "--json")
    if "--comments" in flags or (json_fields and "comments" in json_fields.split(",")):
        comments = gh_api(real_gh, f"repos/{slug}/issues/{num}/comments?per_page=100") or []
        item["_comments"] = comments
    if json_fields:
        fields = json_fields.split(",")
        table = dict(ISSUE_FIELDS)
        if "_comments" in item:
            table["comments"] = lambda i: [
                {"author": _actor(c.get("user")), "body": c.get("body") or "",
                 "createdAt": c.get("created_at"), "url": c.get("html_url", "")}
                for c in i["_comments"]]
        emit(map_fields(item, fields, table), first(flags, "--jq", "-q"))
    else:
        print(f"#{item['number']} {item['title']} [{item['state']}]")
        print(item.get("body") or "")
        for c in item.get("_comments", []):
            print(f"\n--- {(c.get('user') or {}).get('login','')} ({c.get('created_at')}):\n{c.get('body') or ''}")


def t_issue_create(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--title": True, "-t": True,
                             "--body": True, "-b": True, "--body-file": True, "-F": True,
                             "--label": True, "-l": True, "--assignee": True, "-a": True})
    if pos:
        raise Unsupported(pos[0])
    title = first(flags, "--title", "-t")
    if not title:
        raise Unsupported("--title 必須（対話モード非対応）")
    body = first(flags, "--body", "-b", default="")
    body_file = first(flags, "--body-file", "-F")
    if body_file:
        body = sys.stdin.read() if body_file == "-" else open(body_file, encoding="utf-8").read()
    payload: dict = {"title": title, "body": body}
    labels = all_of(flags, "--label", "-l")
    if labels:
        payload["labels"] = [s for l in labels for s in l.split(",")]
    assignees = all_of(flags, "--assignee", "-a")
    if assignees:
        payload["assignees"] = assignees
    item = gh_api(real_gh, f"repos/{slug}/issues", "POST", payload)
    print(item["html_url"])


def t_issue_comment(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--body": True, "-b": True,
                             "--body-file": True, "-F": True})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("issue comment <number> 以外の形")
    body = first(flags, "--body", "-b")
    body_file = first(flags, "--body-file", "-F")
    if body_file:
        body = sys.stdin.read() if body_file == "-" else open(body_file, encoding="utf-8").read()
    if not body:
        raise Unsupported("--body 必須")
    item = gh_api(real_gh, f"repos/{slug}/issues/{pos[0]}/comments", "POST", {"body": body})
    print(item["html_url"])


def t_issue_edit(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--add-label": True,
                             "--remove-label": True, "--title": True, "-t": True,
                             "--body": True, "-b": True})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("issue edit <number> 以外の形")
    num = pos[0]
    add = [s for l in all_of(flags, "--add-label") for s in l.split(",")]
    remove = [s for l in all_of(flags, "--remove-label") for s in l.split(",")]
    if add:
        gh_api(real_gh, f"repos/{slug}/issues/{num}/labels", "POST", {"labels": add})
    for name in remove:
        try:
            gh_api(real_gh,
                   f"repos/{slug}/issues/{num}/labels/{urllib.parse.quote(name, safe='')}",
                   "DELETE")
        except RestError as e:
            if b"Not Found" not in e.stdout + e.stderr:  # 未付与ラベルの除去は gh 同様に許容
                raise
    patch: dict = {}
    if first(flags, "--title", "-t"):
        patch["title"] = first(flags, "--title", "-t")
    if first(flags, "--body", "-b") is not None:
        patch["body"] = first(flags, "--body", "-b")
    if patch:
        gh_api(real_gh, f"repos/{slug}/issues/{num}", "PATCH", patch)
    print(f"https://github.com/{slug}/issues/{num}")


def t_issue_close(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--comment": True, "-c": True,
                             "--reason": True, "-r": True})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("issue close <number> 以外の形")
    num = pos[0]
    comment = first(flags, "--comment", "-c")
    if comment:
        gh_api(real_gh, f"repos/{slug}/issues/{num}/comments", "POST", {"body": comment})
    reason = first(flags, "--reason", "-r", default="completed")
    reason = {"completed": "completed", "not planned": "not_planned",
              "not_planned": "not_planned"}.get(reason, "completed")
    gh_api(real_gh, f"repos/{slug}/issues/{num}", "PATCH",
           {"state": "closed", "state_reason": reason})
    print(f"https://github.com/{slug}/issues/{num}")


def t_pr_list(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({
        "-R": True, "--repo": True, "--state": True, "-s": True,
        "--head": True, "-H": True, "--base": True, "-B": True,
        "--limit": True, "-L": True, "--json": True, "--jq": True, "-q": True,
    })
    if pos:
        raise Unsupported(pos[0])
    limit = int(first(flags, "--limit", "-L", default="30"))
    state = first(flags, "--state", "-s", default="open")
    merged_only = state == "merged"
    params: dict = {"state": "closed" if merged_only else state,
                    "sort": "created", "direction": "desc"}
    head = first(flags, "--head", "-H")
    if head:
        params["head"] = head if ":" in head else f"{slug.split('/')[0]}:{head}"
    if first(flags, "--base", "-B"):
        params["base"] = first(flags, "--base", "-B")
    items = gh_api_list(real_gh, f"repos/{slug}/pulls", params, limit if not merged_only else limit + 100)
    if merged_only:
        items = [p for p in items if p.get("merged_at")][:limit]
    json_fields = first(flags, "--json")
    if json_fields:
        rows = [map_fields(p, json_fields.split(","), PR_FIELDS) for p in items]
        emit(rows, first(flags, "--jq", "-q"))
    else:
        emit_table([{"number": p["number"],
                     "state": "MERGED" if p.get("merged_at") else p["state"].upper(),
                     "title": p["title"], "headRefName": (p.get("head") or {}).get("ref", "")}
                    for p in items], ["number", "state", "title", "headRefName"])


def t_pr_view(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--json": True,
                             "--jq": True, "-q": True})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("pr view <number> 以外の形")
    num = pos[0]
    item = gh_api(real_gh, f"repos/{slug}/pulls/{num}")
    json_fields = first(flags, "--json")
    if json_fields:
        fields = json_fields.split(",")
        table = dict(PR_FIELDS)
        if "reviews" in fields:
            reviews = gh_api(real_gh, f"repos/{slug}/pulls/{num}/reviews?per_page=100") or []
            table["reviews"] = lambda p: [
                {"author": _actor(r.get("user")), "state": r.get("state", ""),
                 "body": r.get("body") or "", "submittedAt": r.get("submitted_at")}
                for r in reviews]
        if "comments" in fields:
            comments = gh_api(real_gh, f"repos/{slug}/issues/{num}/comments?per_page=100") or []
            table["comments"] = lambda p: [
                {"author": _actor(c.get("user")), "body": c.get("body") or "",
                 "createdAt": c.get("created_at"), "url": c.get("html_url", "")}
                for c in comments]
        if "files" in fields:
            files = gh_api(real_gh, f"repos/{slug}/pulls/{num}/files?per_page=100") or []
            table["files"] = lambda p: [
                {"path": f.get("filename", ""), "additions": f.get("additions", 0),
                 "deletions": f.get("deletions", 0)} for f in files]
        emit(map_fields(item, fields, table), first(flags, "--jq", "-q"))
    else:
        state = "MERGED" if item.get("merged_at") else item["state"].upper()
        print(f"#{item['number']} {item['title']} [{state}] {(item.get('head') or {}).get('ref','')} -> {(item.get('base') or {}).get('ref','')}")
        print(item.get("body") or "")


def t_pr_create(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--title": True, "-t": True,
                             "--body": True, "-b": True, "--body-file": True, "-F": True,
                             "--head": True, "-H": True, "--base": True, "-B": True,
                             "--draft": False, "-d": False})
    if pos:
        raise Unsupported(pos[0])
    title = first(flags, "--title", "-t")
    if not title:
        raise Unsupported("--title 必須（対話モード非対応）")
    body = first(flags, "--body", "-b", default="")
    body_file = first(flags, "--body-file", "-F")
    if body_file:
        body = sys.stdin.read() if body_file == "-" else open(body_file, encoding="utf-8").read()
    head = first(flags, "--head", "-H")
    if not head:
        head = subprocess.run(["git", "branch", "--show-current"],
                              capture_output=True, text=True).stdout.strip()
    if not head:
        raise Unsupported("--head 不明")
    payload = {"title": title, "body": body, "head": head,
               "base": first(flags, "--base", "-B", default="main"),
               "draft": "--draft" in flags or "-d" in flags}
    item = gh_api(real_gh, f"repos/{slug}/pulls", "POST", payload)
    print(item["html_url"])


def t_pr_merge(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--squash": False, "-s": False,
                             "--merge": False, "-m": False, "--rebase": False, "-r": False,
                             "--delete-branch": False, "-d": False, "--subject": True, "-t": True,
                             "--body": True, "-b": True})
    if len(pos) != 1 or not pos[0].isdigit():
        raise Unsupported("pr merge <number> 以外の形")
    num = pos[0]
    if "--squash" in flags or "-s" in flags:
        method = "squash"
    elif "--rebase" in flags or "-r" in flags:
        method = "rebase"
    elif "--merge" in flags or "-m" in flags:
        method = "merge"
    else:
        # 実 gh の非対話挙動と同じくフラグ必須（黙って merge commit にしない・不可逆操作）
        sys.stderr.write("[gh-shim] pr merge には --squash / --merge / --rebase のいずれかが必須\n")
        sys.exit(1)
    payload: dict = {"merge_method": method}
    if first(flags, "--subject", "-t"):
        payload["commit_title"] = first(flags, "--subject", "-t")
    if first(flags, "--body", "-b"):
        payload["commit_message"] = first(flags, "--body", "-b")
    delete_branch = "--delete-branch" in flags or "-d" in flags
    pr = gh_api(real_gh, f"repos/{slug}/pulls/{num}") if delete_branch else None
    gh_api(real_gh, f"repos/{slug}/pulls/{num}/merge", "PUT", payload)
    if delete_branch:
        ref = (pr.get("head") or {}).get("ref", "")
        if ref:
            try:
                gh_api(real_gh, f"repos/{slug}/git/refs/heads/{urllib.parse.quote(ref)}", "DELETE")
            except RestError:
                sys.stderr.write(f"[gh-shim] ブランチ削除失敗（{ref}）。手動確認を推奨\n")
    print(f"https://github.com/{slug}/pull/{num} merged ({method})")


def t_label_list(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--limit": True, "-L": True,
                             "--json": True, "--jq": True, "-q": True})
    if pos:
        raise Unsupported(pos[0])
    limit = int(first(flags, "--limit", "-L", default="30"))
    items = gh_api_list(real_gh, f"repos/{slug}/labels", {}, limit)
    json_fields = first(flags, "--json")
    if json_fields:
        table = {"name": lambda l: l.get("name", ""), "color": lambda l: l.get("color", ""),
                 "description": lambda l: l.get("description") or "",
                 "id": lambda l: l.get("node_id", "")}
        emit([map_fields(l, json_fields.split(","), table) for l in items],
             first(flags, "--jq", "-q"))
    else:
        emit_table([{"name": l["name"], "description": l.get("description") or ""}
                    for l in items], ["name", "description"])


def t_repo_view(real_gh: str, slug: str | None, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--json": True,
                             "--jq": True, "-q": True})
    if pos:
        if "/" not in pos[0]:
            # bare 名を黙ってカレントリポジトリ扱いにすると誤データを返すため変換しない
            raise Unsupported(f"repo view の owner/repo でない位置引数（{pos[0]}）")
        slug = pos[0]
    if not slug:
        raise Unsupported("リポジトリ不明")
    item = gh_api(real_gh, f"repos/{slug}")
    json_fields = first(flags, "--json")
    if json_fields:
        emit(map_fields(item, json_fields.split(","), REPO_FIELDS),
             first(flags, "--jq", "-q"))
    else:
        print(f"{item['full_name']}\n{item.get('description') or ''}\n{item['html_url']}")


def t_release_list(real_gh: str, slug: str, args: Args) -> None:
    flags, pos = args.parse({"-R": True, "--repo": True, "--limit": True, "-L": True,
                             "--json": True, "--jq": True, "-q": True})
    if pos:
        raise Unsupported(pos[0])
    limit = int(first(flags, "--limit", "-L", default="30"))
    items = gh_api_list(real_gh, f"repos/{slug}/releases", {}, limit)
    json_fields = first(flags, "--json")
    if json_fields:
        table = {"name": lambda r: r.get("name") or "", "tagName": lambda r: r.get("tag_name", ""),
                 "isDraft": lambda r: r.get("draft", False),
                 "isPrerelease": lambda r: r.get("prerelease", False),
                 "publishedAt": lambda r: r.get("published_at"),
                 "createdAt": lambda r: r.get("created_at")}
        emit([map_fields(r, json_fields.split(","), table) for r in items],
             first(flags, "--jq", "-q"))
    else:
        emit_table([{"tagName": r["tag_name"], "name": r.get("name") or ""}
                    for r in items], ["tagName", "name"])


TRANSLATORS: dict[tuple[str, str], object] = {
    ("issue", "list"): t_issue_list,
    ("issue", "view"): t_issue_view,
    ("issue", "create"): t_issue_create,
    ("issue", "comment"): t_issue_comment,
    ("issue", "edit"): t_issue_edit,
    ("issue", "close"): t_issue_close,
    ("pr", "list"): t_pr_list,
    ("pr", "view"): t_pr_view,
    ("pr", "create"): t_pr_create,
    ("pr", "merge"): t_pr_merge,
    ("pr", "comment"): t_issue_comment,  # PR コメントは issues API と同一
    ("label", "list"): t_label_list,
    ("repo", "view"): t_repo_view,
    ("release", "list"): t_release_list,
}


# ---------------------------------------------------------------- 診断

def doctor(real_gh: str) -> None:
    slug = repo_slug(None)
    probes = [
        ("gh api user（生存確認）", ["api", "user", "--jq", ".login"]),
        ("GraphQL（想定: 403）", ["api", "graphql", "-f", "query=query{viewer{login}}"]),
        ("search REST（想定: 403）", ["api", "search/issues?q=test&per_page=1"]),
    ]
    if slug:
        probes[1:1] = [
            (f"repo REST read（repos/{slug}）", ["api", f"repos/{slug}", "--jq", ".full_name"]),
            (f"repo REST issues（repos/{slug}/issues）", ["api", f"repos/{slug}/issues?per_page=1", "--jq", "length"]),
        ]
        probes.append(("Actions variables（想定: 403）", ["api", f"repos/{slug}/actions/variables"]))
    print(f"gh-shim doctor（cloud={is_cloud()}・repo={slug or '未解決'}）")
    if not slug:
        print(
            "  ⚠ repo slug を解決できませんでした（PROJECT_REPO/GITHUB_REPOSITORY 未設定・"
            "git remote からも解決不可）。repo スコープのプローブはスキップします。"
            "PROJECT_REPO=owner/repo を設定するか git remote を確認してください。"
        )
    for label, cmd in probes:
        proc = subprocess.run([real_gh, *cmd], capture_output=True, text=True, timeout=30)
        status = "✅" if proc.returncode == 0 else "❌"
        detail = (proc.stdout or proc.stderr).strip().splitlines()
        print(f"  {status} {label}: {detail[0][:100] if detail else ''}")


def self_test() -> None:
    """オフライン自己テスト（ネットワーク不要）: 分類・フィールドマップ・引数パース。"""
    failures: list[str] = []

    def check(name: str, cond: bool):
        if not cond:
            failures.append(name)

    # フィールドマップ
    pr = {"number": 5, "title": "t", "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
          "head": {"ref": "feat/x", "sha": "abc"}, "base": {"ref": "main"},
          "user": {"login": "u", "type": "User"}, "labels": [{"name": "sp:3", "color": "fff"}],
          "requested_reviewers": [{"login": "r"}], "draft": False,
          "author_association": "OWNER",
          "created_at": "c", "html_url": "https://x"}
    m = map_fields(pr, ["number", "state", "headRefName", "author", "authorAssociation", "reviewRequests", "labels", "createdAt"], PR_FIELDS)
    check("pr.state=MERGED", m["state"] == "MERGED")
    check("pr.headRefName", m["headRefName"] == "feat/x")
    check("pr.author.login", m["author"]["login"] == "u")
    # #379: check_pending_pr_reviews.py の著者検証が依存するフィールド。REST の
    # author_association 欠落時は空文字（fail closed。None ではなく "" にして
    # 呼び出し側の `not author_association` 判定と一致させる）
    check("pr.authorAssociation", m["authorAssociation"] == "OWNER")
    check("pr.authorAssociation missing → ''", map_fields({**pr, "author_association": None}, ["authorAssociation"], PR_FIELDS)["authorAssociation"] == "")
    check("pr.reviewRequests", m["reviewRequests"][0]["login"] == "r")
    check("pr.labels", m["labels"][0]["name"] == "sp:3")

    issue = {"number": 1, "title": "i", "state": "open", "user": {"login": "u"},
             "labels": [], "comments": 2, "html_url": "https://x", "created_at": "c"}
    mi = map_fields(issue, ["number", "state", "comments"], ISSUE_FIELDS)
    check("issue.state=OPEN", mi["state"] == "OPEN")
    check("issue.comments", mi["comments"] == 2)

    # 引数パーサ
    a = Args(["--state", "open", "--label", "a", "--label", "b", "--json", "number,title", "--limit", "5"])
    flags, pos = a.parse({"--state": True, "--label": True, "--json": True, "--limit": True})
    check("args.labels", flags["--label"] == ["a", "b"])
    check("args.pos-empty", pos == [])
    try:
        Args(["--unknown-flag"]).parse({"--state": True})
        check("args.unsupported-raises", False)
    except Unsupported:
        pass

    # -R 先読み（値なしの末尾 -R でクラッシュしない・#255 レビュー指摘）
    check("peek.-R", peek_repo_flag(["-R", "o/r", "--state", "open"]) == "o/r")
    check("peek.--repo=", peek_repo_flag(["--repo=o/r"]) == "o/r")
    check("peek.trailing-R", peek_repo_flag(["--state", "open", "-R"]) is None)
    check("peek.none", peek_repo_flag(["--state", "open"]) is None)

    # 分類（翻訳対象の存在）
    for key in [("issue", "list"), ("pr", "list"), ("pr", "view"), ("label", "list"),
                ("repo", "view"), ("issue", "edit"), ("pr", "merge")]:
        check(f"translator:{key}", key in TRANSLATORS)

    # 403 ガイダンスのシグネチャ
    for sig in ["GraphQL query is not enabled", "sessions are bound",
                "GitHub Actions path is not permitted", "Resource not accessible by integration"]:
        check(f"guidance:{sig[:20]}", any(sig in s for s, _ in ERROR_GUIDANCE))

    if failures:
        print("SELF-TEST FAILED:\n  " + "\n  ".join(failures))
        sys.exit(1)
    print("SELF-TEST OK (gh_shim)")


# ---------------------------------------------------------------- main

def main() -> None:
    argv = sys.argv[1:]

    if argv and argv[0] == "--shim-self-test":
        self_test()
        return

    real_gh = find_real_gh()
    if not real_gh:
        msg = "[gh-shim] 実 gh が見つかりません（PATH に gh 本体が必要）\n"
        if is_cloud():
            # 実 gh 本体が無いと本シムは repo スコープ REST 変換すら実行できない
            # （subprocess.run([real_gh, ...]) の対象が無い）。GH_TOKEN 等を使った
            # urllib/curl 直叩きも「GitHub access is not enabled for this session」で
            # 同じくプロキシに 403 で拒否される（実測・#313）ため、代替は MCP のみ。
            msg += (
                "[gh-shim] このクラウド実行環境には gh 本体が無いため repo スコープ REST 変換も "
                "実行できません。GH_TOKEN を使った urllib/curl 直叩きもプロキシに 403 で拒否されます"
                "（\"GitHub access is not enabled for this session\"）。"
                "mcp__github__* ツールを使ってください"
                "（SSOT: docs/rules/github-mcp-fallback-patterns.md）\n"
            )
        sys.stderr.write(msg)
        sys.exit(127)

    if argv and argv[0] == "--shim-doctor":
        doctor(real_gh)
        return

    # ローカル or 明示 off → 完全パススルー（exec・オーバーヘッドゼロ）
    if not is_cloud():
        exec_real(real_gh, argv)

    # クラウド: 変換対象なら REST 変換を試み、未対応形ならパススルー + アノテート
    if len(argv) >= 2:
        translator = TRANSLATORS.get((argv[0], argv[1]))
        if translator:
            slug = repo_slug(peek_repo_flag(argv[2:]))
            if slug:
                try:
                    translator(real_gh, slug, Args(argv[2:]))
                    return
                except Unsupported as e:
                    sys.stderr.write(f"[gh-shim] 未対応の引数形（{e}）のためパススルー実行\n")
                except (KeyError, ValueError, OSError) as e:
                    # 未収載の --json フィールド（KeyError）・非数値 --limit（ValueError）・
                    # --body-file 不在（OSError）は変換不能として実 gh へ退避する
                    # （Python トレースバックで落とさず、実 gh のエラーメッセージに任せる）
                    sys.stderr.write(f"[gh-shim] 変換不能な引数値（{e!r}）のためパススルー実行\n")
                except RestError as e:
                    annotate_and_exit(e.stdout, e.stderr, e.code)
                except BrokenPipeError:
                    sys.exit(0)

    passthrough(real_gh, argv)


if __name__ == "__main__":
    main()
