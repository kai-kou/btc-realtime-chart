#!/usr/bin/env python3
"""Claude Code 仕様変更追随レーン（claude-code-spec-sync）

Claude Code 本体のバージョンアップ（anthropics/claude-code releases.atom）を
定期的にキャッチアップし、changelog の各変更を分類して Issue 起票する
軽量検知ツール（LLM 非依存・stdlib + yaml のみ）。

- 破壊的変更（これまでのやり方がエラーになる類）→ [CC-Sync][破壊的変更] Issue（即対応レーン）
- 新機能・新設定 → [CC-Sync][検証] Issue（検証・検討フェーズ）
- その他（バグ修正等）→ 起票しない（ログのみ）

SSOT: docs/rules/claude-code-spec-sync.md
設定: config/claude_code_spec_sync.yaml

使い方:
  python3 tools/check_claude_code_updates.py --create-issue  # 新規があれば Issue 起票（定期スロット）
  python3 tools/check_claude_code_updates.py --json --dry-run  # 読み取り専用の検知確認
  python3 tools/check_claude_code_updates.py --self-test     # ネット非依存セルフテスト
  python3 tools/check_claude_code_updates.py --mark-known 2.1.225  # 既知化のみ（ネット非依存・下記参照）

  ※ --json 単体は state を更新する（既知化）。検知だけ覗くときは必ず --dry-run を併用する。
  ※ --mark-known: gh/REST が両方使えないクラウドセッションでは --create-issue が起票に失敗し、
    当該バージョンの dedup キーが state から自動的に取り消される（次回リトライのため）。
    エージェントが mcp__github__* で Issue 作成・対応・クローズを out-of-band に完遂させた場合は、
    本フラグで dedup キーを直接 state へ書き戻さないと、同じバージョンが次回以降も
    "BREAKING_DETECTED" として再検知され続ける（実例: v2.1.225・2026-08-08・#457）。

終了コード:
  0  = 新バージョンを検知した（破壊的変更/新機能があれば起票済み。バグ修正のみの
       バージョンは起票なしで既知化＝正常）。破壊的変更を含む場合は stdout に
       "BREAKING_DETECTED" 行を出力する（スロット分岐用）
  10 = 新バージョンなし（正常）
  1  = エラー

  起票に失敗（gh + REST 両方）または max_versions_per_issue 超過で今回の Issue に
  乗らなかったバージョンは state から除外し、次回実行で自動リトライする。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

from repo_slug import resolve_repo_slug

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "claude_code_spec_sync.yaml"

_CCS_VER_MARKER_RE = re.compile(r"<!--\s*ccs-ver:\s*(.+?)\s*-->")
_USER_AGENT_API = "claude-code-spec-sync"


# --------------------------------------------------------------------------
# GitHub REST フォールバック（gh 不在/403 環境用。クラウドはプロキシが App 認証を注入・L-114）
# --------------------------------------------------------------------------
def _github_rest_get(path: str, params: str = "") -> list | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    results: list = []
    page = 1
    while page <= 5:
        url = f"https://api.github.com/{path}?per_page=100&page={page}"
        if params:
            url += f"&{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT_API,
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            print(f"[warn] REST フォールバックも失敗 ({path}): {e}", file=sys.stderr)
            return None
        if not isinstance(batch, list):
            return None
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


def _github_rest_post_issue(repo: str, title: str, body: str, labels: list[str]) -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps({"title": title, "body": body, "labels": labels}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT_API,
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("html_url")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"[warn] REST Issue 作成も失敗: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# 設定・状態
# --------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"[warn] state 読込失敗（初期化して継続・Issue dedup が二重起票を防ぐ）: {e}",
                  file=sys.stderr)
    return {"known_ids": []}


def save_state(path: Path, state: dict, max_entries: int) -> None:
    state["known_ids"] = state["known_ids"][-max_entries:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# フィード取得・パース
# --------------------------------------------------------------------------
def fetch_url(url: str, user_agent: str, timeout: int) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - ネットワーク障害は握り潰し継続
        print(f"[warn] fetch failed: {url} ({e})", file=sys.stderr)
        return None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_feed(xml_text: str) -> list[dict]:
    """Atom releases フィードを [{title, link, id, published, content}] にパースする。"""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[warn] feed parse error: {e}", file=sys.stderr)
        return items

    nodes = [el for el in root.iter() if _strip_ns(el.tag) in ("item", "entry")]
    for node in nodes:
        title = link = guid = published = content = ""
        for child in node:
            t = _strip_ns(child.tag)
            if t == "title":
                title = (child.text or "").strip()
            elif t == "link":
                href = child.get("href")
                if href:
                    if not link or child.get("rel") in (None, "alternate"):
                        link = href
                elif child.text:
                    link = child.text.strip()
            elif t in ("guid", "id"):
                guid = (child.text or "").strip()
            elif t in ("pubDate", "published", "updated"):
                if not published:
                    published = (child.text or "").strip()
            elif t in ("summary", "description", "content"):
                if not content:
                    content = (child.text or "").strip()
        if title or link:
            items.append({
                "title": title,
                "link": link,
                "id": guid or link or title,
                "published": published,
                "content": content,
            })
    return items


def parse_pubdate(value: str) -> datetime | None:
    """ISO 8601（GitHub API の created_at/createdAt）を datetime にパースする。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_prerelease(title: str, cfg: dict) -> bool:
    low = title.lower()
    return any(m in low for m in cfg.get("github_prerelease_markers", []))


_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")


def extract_version(title: str) -> str:
    m = _VERSION_RE.search(title)
    return m.group(1) if m else title.strip()


# --------------------------------------------------------------------------
# changelog 本文 → 変更行の抽出・分類
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_lines(content: str) -> list[str]:
    """release notes（HTML or Markdown）を変更行のリストへ正規化する。"""
    text = unescape(content)
    # <li> を行区切りに正規化してからタグ除去
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<(p|br|/li|/ul|/ol|h\d)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if len(line) >= 8:  # 短すぎる断片はノイズ
            lines.append(line)
    return lines


_FIX_PREFIX_RE = re.compile(r"^(fixed|fixes|fix)\b", re.IGNORECASE)


def classify_line(line: str, cfg: dict) -> str:
    """変更行を breaking / feature / other に分類する（breaking 優先）。

    「Fixed ...」で始まる行はバグ修正であり、本文中の "no longer" / "removed" 等で
    破壊的変更に誤分類されやすい（実測: v2.1.203 で 7 件中 5 件が誤検知）ため、
    明示的に "breaking" を含む場合を除き other へデモートする。
    """
    low = line.lower()
    if _FIX_PREFIX_RE.match(line.strip()):
        return "breaking" if "breaking" in low else "other"
    if any(k.lower() in low for k in cfg.get("breaking_keywords", [])):
        return "breaking"
    if any(k.lower() in low for k in cfg.get("feature_keywords", [])):
        return "feature"
    return "other"


def area_hints(line: str, cfg: dict) -> list[str]:
    """変更行が本プロジェクトのどの資産領域に関係しそうかを注記する（分類には不使用）。"""
    low = line.lower()
    hits: list[str] = []
    for area, patterns in (cfg.get("project_area_hints") or {}).items():
        if any(p.lower() in low for p in patterns):
            hits.append(area)
    return hits


# `## 2.1.203` / `## [2.1.203] - 2026-07-17` / `## v2.1.203 (2026-07-17)` 等の変種を許容する
_CHANGELOG_HEADER_RE = re.compile(r"^#{2,3}\s+\[?v?(\d+\.\d+\.\d+[^\]\s]*)\]?(?:\s+.*)?$")


def parse_changelog_releases(text: str, repo: str, source_url: str, max_versions: int) -> list[dict]:
    """CHANGELOG.md（`## X.Y.Z` セクション + bullet 行）を parse_feed と同型の
    エントリリストへ正規化する（releases.atom が取得不可な環境のフォールバック）。"""
    entries: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _CHANGELOG_HEADER_RE.match(line.strip())
        if m:
            if cur:
                entries.append(cur)
            if len(entries) >= max_versions:
                cur = None
                break
            ver = m.group(1)
            cur = {
                "title": f"v{ver}",
                "link": source_url,
                "id": f"changelog:{repo}:{ver}",
                "published": "",
                "content": "",
            }
            continue
        if cur is not None and line.strip().startswith(("-", "*")):
            cur["content"] += line + "\n"
    if cur and len(entries) < max_versions:
        entries.append(cur)
    return entries


def fetch_changelog_entries(repo: str, cfg: dict, ua: str, timeout: int) -> list[dict]:
    fcfg = cfg.get("changelog_fallback", {}) or {}
    branch = fcfg.get("branch", "main")
    path = fcfg.get("path", "CHANGELOG.md")
    max_versions = int(fcfg.get("max_versions", 10))
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    text = fetch_url(url, ua, timeout)
    if not text:
        return []
    return parse_changelog_releases(text, repo, url, max_versions)


def _ver_key(repo: str, version: str) -> str:
    """検知経路（atom / changelog）が変わっても再検知しないバージョン単位の dedup キー。
    collect_new_releases の dedup 判定は `eid in known or vkey in known` という OR 条件のため、
    このキー単体で検知経路（atom の eid / changelog の "changelog:repo:version"）に関わらず
    再検知を防げる（--mark-known が経路別 eid を書かない理由）。"""
    return f"ver:{repo}:{version}"


def collect_new_releases(cfg: dict, state: dict) -> list[dict]:
    """新規リリースを検知し、変更行を分類して返す。state.known_ids を更新する。"""
    http = cfg.get("http", {})
    ua = http.get("user_agent", "curl/8.5.0")
    timeout = int(http.get("timeout_sec", 15))
    known: set[str] = set(state.get("known_ids", []))
    known_list: list[str] = list(state.get("known_ids", []))

    releases: list[dict] = []
    for repo in (cfg.get("sources", {}) or {}).get("github_releases", []):
        xml_text = fetch_url(f"https://github.com/{repo}/releases.atom", ua, timeout)
        entries = parse_feed(xml_text) if xml_text else []
        if not entries:
            # クラウドプロキシがスコープ外 github.com を 403 にする環境では
            # raw.githubusercontent.com の CHANGELOG.md へフォールバックする（実測 2026-07-17）
            entries = fetch_changelog_entries(repo, cfg, ua, timeout)
            if entries:
                print(f"[info] releases.atom 不可のため CHANGELOG.md フォールバックを使用: {repo}",
                      file=sys.stderr)
        for entry in entries:
            eid = entry["id"]
            version = extract_version(entry["title"])
            vkey = _ver_key(repo, version)
            if eid in known or vkey in known:
                continue
            if is_prerelease(entry["title"], cfg):
                for k in (eid, vkey):
                    known_list.append(k)
                    known.add(k)
                continue
            breaking: list[dict] = []
            features: list[dict] = []
            others = 0
            others_hinted: list[dict] = []
            for line in html_to_lines(entry.get("content", "")):
                kind = classify_line(line, cfg)
                hints = area_hints(line, cfg)
                if kind == "breaking":
                    breaking.append({"text": line, "areas": hints})
                elif kind == "feature":
                    features.append({"text": line, "areas": hints})
                else:
                    others += 1
                    # 辞書のキーワードに一致せず「その他」に落ちた行でも、影響領域ヒントに
                    # 一致するものは棚卸し・精読の起点として残す（#561）。辞書の逐次追加は
                    # いたちごっこになるため、拾えなかった行を消さずに次回の辞書拡充材料にする。
                    if hints:
                        others_hinted.append({"text": line, "areas": hints})
            releases.append({
                "version": version,
                "title": entry["title"],
                "link": entry["link"] or f"https://github.com/{repo}/releases",
                "published": entry.get("published", ""),
                "breaking": breaking,
                "features": features,
                "others_count": others,
                "others_hinted": others_hinted,
                "_dedup_keys": [eid, vkey],
            })
            for k in (eid, vkey):
                known_list.append(k)
                known.add(k)

    state["known_ids"] = known_list
    # 古い順（changelog の時系列どおり）に返す
    return list(reversed(releases))


# --------------------------------------------------------------------------
# Issue 起票（バージョン単位の dedup マーカー共有）
# --------------------------------------------------------------------------
def ver_marker(version: str, kind: str) -> str:
    """kind 別マーカー（breaking / feature）。片方の起票だけ成功した場合に
    もう片方が dedup で握り潰されないよう、バージョン+種別で dedup する。"""
    return f"<!-- ccs-ver: {version}#{kind} -->"


def fetch_issued_versions(repo: str, labels: list[str], lookback_hours: int) -> set[str]:
    """直近 lookback_hours の lane Issue 本文から起票済み version#kind を集める
    （gh → REST 降格・古い Issue は cutoff で除外）。"""
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    issued: set[str] = set()
    rows: list[tuple[str, str]] = []  # (created_at, body)
    for label in labels:
        try:
            out = subprocess.run(
                ["gh", "issue", "list", "-R", repo, "--label", label,
                 "--state", "all", "--limit", "1000", "--json", "body,createdAt"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                rows.extend((x.get("createdAt", ""), x.get("body", ""))
                            for x in json.loads(out.stdout or "[]"))
                continue
        except (FileNotFoundError, subprocess.SubprocessError, ValueError) as e:
            print(f"[warn] gh issue list 失敗（REST フォールバックへ降格）: {e}", file=sys.stderr)
        rest = _github_rest_get(f"repos/{repo}/issues",
                                f"state=all&labels={urllib.parse.quote(label)}")
        if rest:
            rows.extend((x.get("created_at", ""), x.get("body") or "") for x in rest)
    for created_at, body in rows:
        dt = parse_pubdate(created_at)
        if dt is not None and dt.timestamp() < cutoff:
            continue
        for m in _CCS_VER_MARKER_RE.finditer(body or ""):
            issued.add(m.group(1))
    return issued


def _format_change_lines(changes: list[dict]) -> list[str]:
    lines = []
    for c in changes:
        hint = f"（影響領域ヒント: {', '.join(c['areas'])}）" if c.get("areas") else ""
        lines.append(f"  - {c['text']}{hint}")
    return lines


# kind 別の文面（本文の骨格は build_issue が共通で組み立てる）
_ISSUE_TEXTS: dict[str, dict] = {
    "breaking": {
        "changes_key": "breaking",
        "default_prefix": "[CC-Sync][破壊的変更]",
        "count_label": "即対応が必要な変更",
        "intro": [
            "## 破壊的変更の検知（即対応レーン）",
            "",
            "Claude Code の新バージョンに **これまでのやり方がエラーになりうる変更** を検知した。",
            "`claude-code-spec-sync` スキルの **即対応フロー**（影響調査 → 修正 → PR → マージ）を実行すること。",
            "",
        ],
        "checklist": [
            "## 即対応フロー（SSOT: docs/rules/claude-code-spec-sync.md）",
            "",
            "- [ ] 影響調査: 変更キーワードで CLAUDE.md / docs/rules/ / .claude/(skills|hooks|agents|rules|settings.json) / tools/ を横断 Grep",
            "- [ ] 公式裏取り: changelog 原文 + 公式 Docs（code.claude.com/docs）で仕様を確定",
            "- [ ] 影響あり → 最小差分で修正 → 検証（self-test / dry-run）→ PR → L1 セルフレビュー（自前 code-review スキル・Skill(code-review)） → マージ",
            "- [ ] 影響なし → 判定理由をコメントしてクローズ",
        ],
    },
    "feature": {
        "changes_key": "features",
        "default_prefix": "[CC-Sync][検証]",
        "count_label": "新機能・新設定の検証・検討",
        "intro": [
            "## 新機能・新設定の検知（検証・検討フェーズ）",
            "",
            "Claude Code の新バージョンに新機能・新設定を検知した。**即反映はせず**、",
            "`claude-code-spec-sync` スキルの **検証・検討フェーズ** を経てから採用/見送りを判定する。",
            "",
        ],
        "checklist": [
            "## 検証・検討チェックリスト（SSOT: docs/rules/claude-code-spec-sync.md）",
            "",
            "- [ ] 公式 Docs / changelog 原文で仕様・前提条件を確認",
            "- [ ] 本プロジェクトへの適用価値を評価（CP-5 貢献 / CP-6 自律性向上 / コスト / リスク）",
            "- [ ] 挙動・設計への影響が大きい場合は議論型レビュー（`discussion-review` スキル）で採否判定",
            "- [ ] 採用 → rules/skills/settings へ反映 + `docs/rules/claude-code-optimization.md` に記録 → PR",
            "- [ ] 見送り → 理由をコメントしてクローズ",
        ],
    },
}


def build_issue(releases: list[dict], kind: str, cfg: dict) -> tuple[str, str]:
    texts = _ISSUE_TEXTS[kind]
    changes_key = texts["changes_key"]
    total = sum(len(r[changes_key]) for r in releases)
    versions = " / ".join(f"v{r['version']}" for r in releases)
    icfg = cfg.get("issue", {}).get(kind, {})
    prefix = icfg.get("title_prefix", texts["default_prefix"])
    title = f"{prefix} Claude Code {versions}: {texts['count_label']} {total}件"
    lines = list(texts["intro"])
    for r in releases:
        lines.append(f"### [{r['title']}]({r['link']})")
        lines.extend(_format_change_lines(r[changes_key]))
        # breaking と feature が同一バージョンで両方立つ場合、others_hinted を両方の Issue に
        # 丸ごと重複掲載すると二重トリアージになる（レビュー指摘・#561）。breaking Issue 側にのみ
        # 掲載し、feature 側は参照だけ残す（breaking が無い＝feature 単独のバージョンでは通常どおり掲載）。
        if r.get("others_hinted"):
            if kind == "feature" and r.get("breaking"):
                lines.append("")
                lines.append("_（辞書未一致の「その他」ヒント行は同バージョンの破壊的変更 Issue 側に記載・重複回避）_")
            else:
                lines.append("")
                lines.append("<details><summary>⚠️ 辞書には未一致だが影響領域ヒントに一致した「その他」の変更（要精読・#561）</summary>")
                lines.append("")
                lines.extend(_format_change_lines(r["others_hinted"]))
                lines.append("")
                lines.append("</details>")
        lines.append("")
    lines += texts["checklist"]
    lines += [
        "",
        "> 自動起票: `tools/check_claude_code_updates.py`",
        "",
        "<!-- ccs dedup markers (重複起票防止・削除しないこと) -->",
    ]
    lines.extend(ver_marker(r["version"], kind) for r in releases)
    return title, "\n".join(lines)


def create_issue(title: str, body: str, labels: list[str], repo: str) -> str | None:
    try:
        out = subprocess.run(
            ["gh", "issue", "create", "-R", repo,
             "--title", title, "--body", body, "--label", ",".join(labels)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            return out.stdout.strip()
        print(f"[warn] gh issue create failed (REST フォールバック試行): "
              f"{out.stderr.strip()}", file=sys.stderr)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"[warn] gh unavailable (REST フォールバック試行): {e}", file=sys.stderr)
    return _github_rest_post_issue(repo, title, body, labels)


# --------------------------------------------------------------------------
# セルフテスト（ネット非依存）
# --------------------------------------------------------------------------
_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/v9.9.9</id>
    <title>v9.9.9</title>
    <link rel="alternate" href="https://github.com/anthropics/claude-code/releases/tag/v9.9.9"/>
    <updated>2099-01-01T00:00:00Z</updated>
    <content type="html">&lt;ul&gt;
      &lt;li&gt;Breaking: the --foo flag was removed, use --bar instead&lt;/li&gt;
      &lt;li&gt;Added new settings.json option sandbox.allowUnixSockets&lt;/li&gt;
      &lt;li&gt;Fixed a crash when resuming sessions&lt;/li&gt;
    &lt;/ul&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v9.9.10-beta</id>
    <title>v9.9.10-beta</title>
    <link rel="alternate" href="https://github.com/anthropics/claude-code/releases/tag/v9.9.10-beta"/>
    <content type="html">&lt;li&gt;Added prerelease thing&lt;/li&gt;</content>
  </entry>
</feed>
"""


def self_test() -> int:
    cfg = load_config(DEFAULT_CONFIG)
    entries = parse_feed(_SAMPLE_ATOM)
    assert len(entries) == 2, f"parse_feed: {len(entries)} entries"
    assert is_prerelease(entries[1]["title"], cfg), "prerelease 除外が効いていない"
    lines = html_to_lines(entries[0]["content"])
    assert len(lines) == 3, f"html_to_lines: {lines}"
    kinds = [classify_line(x, cfg) for x in lines]
    assert kinds == ["breaking", "feature", "other"], f"classify: {kinds}"
    # Fixed 行のデモーション（"no longer"/"removed" を含むバグ修正を breaking にしない）
    assert classify_line("Fixed: the indicator no longer re-renders", cfg) == "other"
    assert classify_line("Fixed a breaking regression in hooks", cfg) == "breaking"
    assert "settings" in area_hints(lines[1], cfg), "area_hints(settings) 不検出"
    # revert 同義語（#561・辞書の逐次追加はいたちごっこなので同義語群を先取り）
    assert classify_line("Rolled back the permission change from 2.1.259", cfg) == "breaking"
    assert classify_line("Restored the previous default for sandbox mode", cfg) == "breaking"
    assert classify_line("Restored the prior timeout for hooks", cfg) == "breaking"
    assert classify_line("Undid the change to hook timeout", cfg) == "breaking"
    # 動詞原形は新機能紹介文と衝突するため辞書に採用しない（レビュー指摘・#561）
    assert classify_line("Added an undo button to the diff viewer", cfg) != "breaking", (
        "undo 単体一致は禁止（UI機能名との誤検知回避・#561）"
    )
    assert classify_line("Added the ability to roll back recent file edits", cfg) != "breaking", (
        "roll back 単体一致は禁止（ロールバック機能の新機能紹介文との誤検知回避・#561）"
    )
    assert classify_line("You can now roll back a deployment from the command palette", cfg) != "breaking"
    # 「その他」行でも影響領域ヒントは計算する（起票対象外の見落とし防止・#561）
    other_line = "Tweaked internal telemetry for settings.json validation"
    assert classify_line(other_line, cfg) == "other"
    assert "settings" in area_hints(other_line, cfg), "other 行でも area_hints は計算されるべき"
    assert extract_version(entries[0]["title"]) == "9.9.9"
    assert parse_pubdate("2099-01-01T00:00:00Z") is not None
    assert parse_pubdate("") is None
    # state 一巡（dedup）
    state = {"known_ids": []}
    # collect はネットを叩くため、パース済みエントリで known 化ロジックのみ検証
    state["known_ids"].append(entries[0]["id"])
    assert entries[0]["id"] in state["known_ids"]
    # CHANGELOG.md フォールバックのパース（ブラケット・日付付きヘッダー変種を含む）
    sample_changelog = (
        "# Changelog\n\n## [9.9.9] - 2026-07-17\n\n"
        "- Breaking: the --foo flag was removed, use --bar instead\n"
        "- Added new settings.json option sandbox.allowUnixSockets\n\n"
        "## 9.9.8 (2026-07-17)\n\n- Fixed a crash\n"
    )
    cl = parse_changelog_releases(sample_changelog, "anthropics/claude-code", "https://example.com", 10)
    assert [e["title"] for e in cl] == ["v9.9.9", "v9.9.8"], f"changelog parse: {cl}"
    assert parse_changelog_releases("## v9.9.7\n- Added x\n", "r", "u", 10)[0]["title"] == "v9.9.7"
    assert len(html_to_lines(cl[0]["content"])) == 2, f"changelog content: {cl[0]['content']!r}"
    assert parse_changelog_releases(sample_changelog, "r", "u", 1)[0]["title"] == "v9.9.9"
    assert len(parse_changelog_releases(sample_changelog, "r", "u", 1)) == 1
    # バージョン単位 dedup キー（atom ↔ changelog の経路差を吸収）
    assert _ver_key("anthropics/claude-code", "9.9.9") == "ver:anthropics/claude-code:9.9.9"
    # --mark-known の "v" プレフィックス正規化（extract_version と同じ規則で揃える）
    assert extract_version("v2.1.225") == "2.1.225"
    assert extract_version("2.1.225") == "2.1.225"
    # Issue 本文とマーカー
    rel = {"version": "9.9.9", "title": "v9.9.9", "link": "https://example.com",
           "published": "", "breaking": [{"text": lines[0], "areas": []}],
           "features": [{"text": lines[1], "areas": ["settings"]}], "others_count": 1,
           "others_hinted": [{"text": other_line, "areas": ["settings"]}],
           "_dedup_keys": [entries[0]["id"], _ver_key("anthropics/claude-code", "9.9.9")]}
    t1, b1 = build_issue([rel], "breaking", cfg)
    t2, b2 = build_issue([rel], "feature", cfg)
    assert ver_marker("9.9.9", "breaking") in b1 and ver_marker("9.9.9", "feature") in b2
    assert "[CC-Sync][破壊的変更]" in t1 and "[CC-Sync][検証]" in t2
    assert _CCS_VER_MARKER_RE.search(b1).group(1) == "9.9.9#breaking"
    assert "即対応フロー" in b1 and "検証・検討チェックリスト" in b2
    # 「その他」ヒント一致行が Issue 本文に精読対象として注記される（#561）。ただし breaking と
    # feature が同一バージョンで両方立つ場合は breaking 側にのみ掲載し、feature 側には重複させない
    # （二重トリアージ防止・レビュー指摘）
    assert other_line in b1, "others_hinted が breaking Issue 本文に注記されていない"
    assert "要精読" in b1
    assert other_line not in b2, "breaking がある場合、feature Issue に others_hinted を重複掲載してはならない"
    assert "破壊的変更 Issue 側に記載" in b2
    # breaking が無い（feature 単独の）バージョンでは feature 側にそのまま掲載する
    rel_feature_only = {**rel, "breaking": []}
    _, b3 = build_issue([rel_feature_only], "feature", cfg)
    assert other_line in b3 and "要精読" in b3, "feature 単独バージョンで others_hinted が欠落している"
    # others_hinted が空リストのときは「要精読」セクションを出力しない
    rel_no_hint = {**rel, "others_hinted": []}
    _, b4 = build_issue([rel_no_hint], "breaking", cfg)
    assert "要精読" not in b4, "others_hinted が空でも要精読セクションが出力されている"
    # repo 解決（本リポジトリ = プレースホルダ未置換でも git remote から導出できる）
    repo = resolve_repo_slug(cfg.get("issue", {}).get("repo", ""))
    assert repo and "/" in repo, f"repo 解決失敗: {repo!r}"
    print(f"self-test: OK (repo={repo})")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code 仕様変更追随レーン（spec-sync）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--create-issue", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="状態ファイルを書き込まない")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--mark-known", nargs="+", metavar="VERSION",
                     help="指定バージョンの dedup キーをネット非依存で state に直接追記し終了する"
                          "（--create-issue が gh/REST 不可で失敗した後、エージェントが MCP で"
                          "out-of-band に Issue 対応を完遂した際の再検知ループ止め用）")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg = load_config(Path(args.config))
    state_path = ROOT / cfg.get("state_file", "config/claude_code_spec_state.json")
    state = load_state(state_path)

    if args.mark_known:
        sources = (cfg.get("sources", {}) or {}).get("github_releases", [])
        if len(sources) != 1:
            print(f"[error] --mark-known は追跡リポジトリが1件のときのみ暗黙解決できます "
                  f"（現在 {len(sources)}件: {sources}）。誤った repo へ既知化しないよう中止します。",
                  file=sys.stderr)
            return 1
        repo = sources[0]
        known_ids = state.setdefault("known_ids", [])
        added = []
        for raw_version in args.mark_known:
            version = extract_version(raw_version)  # "v2.1.225" 表記と "2.1.225" を同一視する
            key = _ver_key(repo, version)
            if key not in known_ids:
                known_ids.append(key)
                added.append(key)
        if not args.dry_run:
            save_state(state_path, state, int(cfg.get("max_known_entries", 400)))
        print(f"[info] mark-known: {len(added)}件の dedup キーを追記しました: {', '.join(added) or '(既知済み・追加なし)'}")
        return 0

    releases = collect_new_releases(cfg, state)

    icfg = cfg.get("issue", {})
    # bootstrap 済み下流はプレースホルダ置換値をそのまま使い、本リポジトリ自身は
    # git remote から動的導出する（tools/repo_slug.py が正本・#215）
    repo = resolve_repo_slug(icfg.get("repo", "") or "kai-kou/btc-realtime-chart")

    # Issue レベル dedup（state 消失・並行セッション対策・kind 別マーカー）
    if releases:
        issued = fetch_issued_versions(
            repo, icfg.get("dedup_labels", ["lane:claude-code-spec"]),
            int(icfg.get("dedup_lookback_hours", 336)))
        for r in releases:
            if f"{r['version']}#breaking" in issued:
                r["breaking"] = []
            if f"{r['version']}#feature" in issued:
                r["features"] = []

    max_per = int(cfg.get("max_versions_per_issue", 8))
    breaking_all = [r for r in releases if r["breaking"]]
    feature_all = [r for r in releases if r["features"]]
    breaking_rel = breaking_all[:max_per]
    feature_rel = feature_all[:max_per]
    breaking_total = sum(len(r["breaking"]) for r in breaking_rel)
    feature_total = sum(len(r["features"]) for r in feature_rel)

    # 今回の Issue に乗らないバージョンは state から外して次回リトライさせる
    # （max_versions_per_issue 超過分・起票失敗分。既知化したままだと永久喪失する）
    retry_eids: set[str] = {k for r in breaking_all[max_per:] for k in r["_dedup_keys"]}
    retry_eids |= {k for r in feature_all[max_per:] for k in r["_dedup_keys"]}
    if breaking_all[max_per:] or feature_all[max_per:]:
        print(f"[warn] max_versions_per_issue={max_per} 超過分 "
              f"{len(breaking_all[max_per:]) + len(feature_all[max_per:])}件は次回に繰り越し",
              file=sys.stderr)

    created: list[str] = []
    if args.create_issue:
        for kind, rel in (("breaking", breaking_rel), ("feature", feature_rel)):
            if not rel:
                continue
            t, b = build_issue(rel, kind, cfg)
            labels = icfg.get(kind, {}).get("labels", ["lane:claude-code-spec"])
            url = create_issue(t, b, labels, repo)
            if url:
                created.append(url)
            else:
                # 起票失敗 → 既知化を取り消して次回リトライ
                retry_eids.update(k for r in rel for k in r["_dedup_keys"])
    else:
        # 起票なしの実行（--json 単体等）では起票対象を既知化しない
        # （「その他のみ」のバージョンだけ既知化する＝検知の覗き見で Issue が失われない）
        retry_eids.update(k for r in breaking_all for k in r["_dedup_keys"])
        retry_eids.update(k for r in feature_all for k in r["_dedup_keys"])

    # 辞書には未一致だが影響領域ヒントに一致した「その他」行は、Issue 化されない
    # バージョン（その他のみ）でも見落とさないよう必ずログへ残す（#561）
    for r in releases:
        if r.get("others_hinted"):
            areas = sorted({a for h in r["others_hinted"] for a in h["areas"]})
            print(f"[warn] v{r['version']}: 辞書未一致だが影響領域ヒント一致の「その他」行 "
                  f"{len(r['others_hinted'])}件（areas={areas}）・要精読", file=sys.stderr)

    if retry_eids:
        state["known_ids"] = [i for i in state["known_ids"] if i not in retry_eids]

    if not args.dry_run:
        save_state(state_path, state, int(cfg.get("max_known_entries", 400)))

    result = {
        "new_versions": [{k: v for k, v in r.items() if k != "_dedup_keys"} for r in releases],
        "breaking_total": breaking_total,
        "feature_total": feature_total,
        "breaking_detected": breaking_total > 0,
        "issues_created": created,
    }
    if args.json:
        # stdout を有効な JSON に保つ（BREAKING_DETECTED マーカーは breaking_detected キーで代替）
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"新バージョン: {len(releases)}件 / 破壊的変更: {breaking_total}件 / "
              f"新機能・新設定: {feature_total}件 / 起票: {len(created)}件")
        for url in created:
            print(f"  issue: {url}")
        if breaking_total > 0:
            print("BREAKING_DETECTED")
    return 0 if releases else 10


if __name__ == "__main__":
    sys.exit(main())
