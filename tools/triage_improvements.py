#!/usr/bin/env python3
"""
triage_improvements.py - 改善 Issue 棚卸し（grooming）支援ツール

self-improvement-loop スキル（整理モード）の「重い処理」層。type:improvement の Issue を一括取得し、
集計・カテゴリ自動分類・重複検出・priority/sp 欠損検出・Epic 候補抽出を行い、
機械可読 JSON と人間可読 Markdown レポートを出力する。

判断（実際のラベル付与・クローズ・Epic 作成）は SKILL.md 側で Claude（+ @owner PO）が行う。
本ツールは「現状をデータで可視化する」ことに徹し、副作用（Issue の変更）は持たない。

使い方:
  python3 tools/triage_improvements.py                  # Markdown レポートを stdout
  python3 tools/triage_improvements.py --json           # JSON を stdout
  python3 tools/triage_improvements.py --out report.md  # Markdown をファイル出力
  python3 tools/triage_improvements.py --label type:improvement --state open
  python3 tools/triage_improvements.py --self-test      # 純粋関数のセルフテスト

設計方針:
  - 副作用なし（読み取り専用）。GitHub への書き込みは一切しない
  - gh CLI を主経路、不在時は GitHub API（urllib）にフォールバック
  - リポジトリは PROJECT_REPO / GITHUB_REPOSITORY env で解決（雛形プレースホルダにフォールバック）
  - カテゴリは「監査タグ（[監査PX/DOMAIN-NN]）」を最優先、なければキーワードクラスタ
  - 重複検出は ① 監査ドメインコードの重複 ② 正規化タイトルのトークン Jaccard 類似度
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

# 優先順: bootstrap 済みプレースホルダ解決値（最優先・下流リポジトリの既定動作）→
# 未解決の場合のみ PROJECT_REPO → GITHUB_REPOSITORY → git remote の URL 解析 →
# 雛形プレースホルダのまま（解決ロジックの正本は tools/repo_slug.py・#215）。
REPO = resolve_repo_slug("kai-kou/btc-realtime-chart", env_vars=("PROJECT_REPO", "GITHUB_REPOSITORY"))

# 監査タグ [監査PX/DOMAIN-NN] の DOMAIN コード → 日本語ラベル（任意機能）。
# 監査タグ運用を採るプロジェクトのみ意味を持つ。未知コードはコードのまま素通しするため、
# ここに無いコードを使っても破綻しない（ドメイン固有ラベルは各プロジェクトで追記する）。
DOMAIN_LABELS = {
    "QUAL": "品質/レビュー",
    "COST": "コスト/予算",
    "SAFE": "安全/セキュリティ",
    "SEC": "セキュリティ",
    "OBS": "可観測性",
    "TEST": "テスト",
    "A11Y": "アクセシビリティ",
    "DOC": "ドキュメント",
    "PIPE": "パイプライン基盤",
}

# 非監査 Issue 用のキーワードクラスタ（先勝ちで1カテゴリに割当）。
# 汎用 Claude Code 運用ベースの構成要素（ハーネス/ルール/スキル/ツール/CI 等）に基づく。
# プロジェクト固有のドメイン分類を増やしたい場合は本リストの先頭側に追記する。
KEYWORD_CLUSTERS = [
    ("ハーネス/フック", ["hook", "フック", "harness", "ハーネス", "pretooluse", "posttooluse",
                         "pre-tool", "post-tool", "stop-", "settings.json", "ガードレール"]),
    ("ルール/ドキュメント", ["rule", "ルール", "docs", "ドキュメント", "claude.md", "ssot",
                            "readme", "ガイド"]),
    ("スキル整備", ["skill", "スキル", "skill-creator", "description", "gotchas", "サブエージェント",
                   "subagent", "agent"]),
    ("ツール/スクリプト", ["tool", "ツール", "script", "スクリプト", "helper", "ヘルパー",
                         "リファクタ", "module", "モジュール", "共通化", "ユーティリティ"]),
    ("CI/テスト", ["test", "テスト", "self-test", "セルフテスト", "workflow", "ワークフロー",
                  "lint", "pytest", "アクション", "actions", "ci/"]),
    ("PR/レビュー", ["プルリク", "レビュー", "review", "merge", "マージ", "self-review",
                    "セルフレビュー", "pull request"]),
    ("Issue/バックログ運用", ["issue", "ラベル", "label", "milestone", "マイルストーン", "project",
                            "backlog", "バックログ", "sprint", "スプリント", "見積", "棚卸"]),
    ("計測/分析", ["analytics", "計測", "metric", "メトリク", "kpi", "cost", "コスト", "token",
                  "トークン", "ダッシュボード", "予算"]),
    ("セッション/安全", ["session", "セッション", "compaction", "圧縮", "checkpoint", "チェックポイント",
                       "timeout", "タイムアウト", "復帰", "コミット"]),
    ("通知/連携", ["slack", "通知", "notification", "mention", "メンション", "webhook", "連携"]),
    ("セキュリティ/認証", ["security", "セキュリティ", "secret", "シークレット", "credential",
                         "認証", "サンドボックス", "sandbox", "権限"]),
    ("リサーチ/調査", ["research", "リサーチ", "調査", "deep research", "deep-research"]),
]

# ストップワード（タイトル類似度計算で無視する一般語・記号語）
_STOPWORDS = {
    "improvement", "feat", "fix", "docs", "epic", "の", "を", "に", "と", "が", "は",
    "で", "improvement:", "監査", "追加", "実装", "対応", "強化", "改善", "見直し",
    "最適化", "化", "新設", "定義", "統一", "導入",
}


def run_gh(args):
    """gh CLI を実行して stdout を返す。失敗時は空文字。"""
    try:
        result = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        print(f"WARNING: gh failed: gh {' '.join(args)}\n  {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def _fetch_via_api(label, state):
    """gh 不在時のフォールバック（GitHub REST API・GH_TOKEN 必要）。"""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: gh CLI も GH_TOKEN も利用できません", file=sys.stderr)
        return []
    issues = []
    page = 1
    state_q = "all" if state == "all" else state
    while True:
        url = (
            f"https://api.github.com/repos/{REPO}/issues"
            f"?labels={urllib.parse.quote(label)}&state={state_q}&per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "curl/8.5.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                batch = json.loads(resp.read())
        except urllib.error.URLError as e:
            print(f"ERROR: API fetch failed: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for it in batch:
            if "pull_request" in it:
                continue  # PR を除外
            issues.append({
                "number": it["number"],
                "title": it["title"],
                "labels": [{"name": l["name"]} for l in it.get("labels", [])],
                "createdAt": it.get("created_at"),
                "updatedAt": it.get("updated_at"),
                "milestone": ({"title": it["milestone"]["title"]} if it.get("milestone") else None),
                "comments": it.get("comments", 0),
            })
        page += 1
    return issues


def fetch_issues(label, state):
    """type:improvement Issue を取得する（gh 優先・API フォールバック）。"""
    out = run_gh([
        "issue", "list", "-R", REPO,
        "--label", label, "--state", state,
        "--limit", "500",
        "--json", "number,title,labels,createdAt,updatedAt,milestone,comments",
    ])
    if out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            print("WARNING: gh 出力の JSON 解析に失敗。API へフォールバック", file=sys.stderr)
    return _fetch_via_api(label, state)


def label_names(issue):
    return [(l["name"] if isinstance(l, dict) else l) for l in issue.get("labels", [])]


def get_tag(labels, prefix):
    for l in labels:
        if l.startswith(prefix):
            return l[len(prefix):]
    return None


def normalize_tokens(title):
    """タイトルを類似度計算用のトークン集合に正規化する。"""
    # 監査タグ・記号を除去
    t = re.sub(r"\[[^\]]*\]", " ", title)
    t = re.sub(r"[（）()【】・,，、。:：/／\-—–#0-9]", " ", t)
    t = t.lower()
    toks = {w for w in t.split() if len(w) >= 2 and w not in _STOPWORDS}
    return toks


def categorize(issue):
    """Issue を (カテゴリ名, 監査フェーズ or None) に分類する。"""
    title = issue["title"]
    labels = label_names(issue)
    if "[Epic]" in title or "type:epic" in labels:
        return ("（Epic/親追跡）", None)
    m = re.search(r"\[監査(P\d)/([A-Z0-9]+)", title)
    if m:
        phase = m.group(1)
        domain = re.match(r"[A-Z]+", m.group(2)).group(0)
        return (f"監査:{DOMAIN_LABELS.get(domain, domain)}", phase)
    low = title.lower()
    for name, kws in KEYWORD_CLUSTERS:
        if any(k in low for k in kws):
            return (name, None)
    return ("（その他/未分類）", None)


def detect_duplicates(rows):
    """重複/酷似ペアを検出する。"""
    dups = []
    # ① 監査ドメインコードの完全重複（例: SNS-12 が2件）
    code_map = defaultdict(list)
    for r in rows:
        m = re.search(r"\[監査P\d/([A-Z]+-?\d+)", r["title"])
        if m:
            code_map[m.group(1)].append(r["num"])
    for code, nums in code_map.items():
        if len(nums) > 1:
            dups.append({"type": "audit-code", "key": code, "issues": sorted(nums)})
    # ② タイトルトークンの Jaccard 類似度（>= 0.6 を酷似とみなす）
    toks = {r["num"]: normalize_tokens(r["title"]) for r in rows}
    nums = [r["num"] for r in rows]
    for i in range(len(nums)):
        a = nums[i]
        if not toks[a]:
            continue
        for j in range(i + 1, len(nums)):
            b = nums[j]
            if not toks[b]:
                continue
            inter = len(toks[a] & toks[b])
            union = len(toks[a] | toks[b])
            if union == 0:
                continue
            jac = inter / union
            if jac >= 0.6 and inter >= 2:
                dups.append({"type": "title-similar", "score": round(jac, 2), "issues": [a, b]})
    return dups


def build_report(issues, epic_threshold):
    rows = []
    for it in issues:
        labels = label_names(it)
        cat, phase = categorize(it)
        rows.append({
            "num": it["number"],
            "title": it["title"],
            "labels": labels,
            "priority": get_tag(labels, "priority:"),
            "sp": get_tag(labels, "sp:"),
            "milestone": (it["milestone"]["title"] if it.get("milestone") else None),
            "created": (it.get("createdAt") or "")[:10],
            "updated": (it.get("updatedAt") or "")[:10],
            "category": cat,
            "audit_phase": phase,
            "is_epic": "[Epic]" in it["title"],
        })
    rows.sort(key=lambda r: r["num"])

    pri = Counter(r["priority"] or "(なし)" for r in rows)
    sp = Counter(r["sp"] or "(なし)" for r in rows)
    phase = Counter(r["audit_phase"] for r in rows if r["audit_phase"])
    cats = Counter(r["category"] for r in rows)

    missing_priority = [r["num"] for r in rows if not r["priority"] and not r["is_epic"]]
    missing_sp = [r["num"] for r in rows if not r["sp"] and not r["is_epic"]]

    dups = detect_duplicates(rows)

    # Epic 候補: 同一カテゴリに閾値以上の非 Epic Issue が集中
    cat_members = defaultdict(list)
    for r in rows:
        if not r["is_epic"]:
            cat_members[r["category"]].append(r["num"])
    epic_candidates = {c: sorted(nums) for c, nums in cat_members.items()
                       if len(nums) >= epic_threshold and not c.startswith("（")}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": REPO,
        "total": len(rows),
        "priority_dist": dict(pri),
        "sp_dist": dict(sp),
        "audit_phase_dist": dict(phase),
        "category_dist": dict(cats),
        "missing_priority": missing_priority,
        "missing_sp": missing_sp,
        "duplicates": dups,
        "epic_candidates": epic_candidates,
        "rows": rows,
    }


def render_markdown(rep):
    L = []
    L.append(f"# 改善 Issue 棚卸しレポート（{rep['total']} 件）")
    L.append("")
    L.append(f"_生成: {rep['generated_at']}_")
    L.append("")
    L.append("## 集計")
    L.append("")
    L.append("| 軸 | 内訳 |")
    L.append("|----|------|")
    L.append(f"| priority | {_fmt(rep['priority_dist'])} |")
    L.append(f"| sp | {_fmt(rep['sp_dist'])} |")
    if rep["audit_phase_dist"]:
        L.append(f"| 監査フェーズ | {_fmt(rep['audit_phase_dist'])} |")
    L.append("")
    L.append("## カテゴリ別件数")
    L.append("")
    for c, n in sorted(rep["category_dist"].items(), key=lambda x: -x[1]):
        L.append(f"- **{c}**: {n} 件")
    L.append("")
    if rep["epic_candidates"]:
        L.append("## 🧩 Epic 統合候補（同一カテゴリ集中）")
        L.append("")
        for c, nums in sorted(rep["epic_candidates"].items(), key=lambda x: -len(x[1])):
            preview = ", ".join(f"#{n}" for n in nums[:12])
            more = f" …他 {len(nums)-12} 件" if len(nums) > 12 else ""
            L.append(f"- **{c}**（{len(nums)} 件）: {preview}{more}")
        L.append("")
    if rep["duplicates"]:
        L.append("## ⚠️ 重複/酷似の検出")
        L.append("")
        for d in rep["duplicates"]:
            if d["type"] == "audit-code":
                L.append(f"- 監査コード `{d['key']}` が重複: {', '.join('#'+str(n) for n in d['issues'])}")
            else:
                a, b = d["issues"]
                L.append(f"- 酷似（類似度 {d['score']}）: #{a} ↔ #{b}")
        L.append("")
    if rep["missing_priority"] or rep["missing_sp"]:
        L.append("## 🏷 ラベル欠損（@owner PO 補完対象）")
        L.append("")
        if rep["missing_priority"]:
            L.append(f"- **priority 未設定** {len(rep['missing_priority'])} 件: "
                     + ", ".join(f"#{n}" for n in rep["missing_priority"][:25])
                     + (" …" if len(rep["missing_priority"]) > 25 else ""))
        if rep["missing_sp"]:
            L.append(f"- **sp 未設定** {len(rep['missing_sp'])} 件: "
                     + ", ".join(f"#{n}" for n in rep["missing_sp"][:25])
                     + (" …" if len(rep["missing_sp"]) > 25 else ""))
        L.append("")
    return "\n".join(L)


def _fmt(d):
    return " / ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda x: (-x[1], x[0])))


def _self_test():
    """純粋関数（API 非依存）のセルフテスト。"""
    fail = 0

    def check(cond, msg):
        nonlocal fail
        if not cond:
            print(f"FAIL: {msg}", file=sys.stderr)
            fail += 1

    # categorize: 監査タグ優先
    cat, phase = categorize({"title": "[監査P2/SEC-03] 認証強化", "labels": []})
    check(cat == "監査:セキュリティ" and phase == "P2", f"audit tag categorize ({cat},{phase})")
    # categorize: 未知ドメインコードは素通し
    cat, _ = categorize({"title": "[監査P1/FOO-01] なにか", "labels": []})
    check(cat == "監査:FOO", f"unknown domain passthrough ({cat})")
    # categorize: キーワードクラスタ（汎用カテゴリ）
    cat, _ = categorize({"title": "improvement: stop-router フックの統合", "labels": []})
    check(cat == "ハーネス/フック", f"keyword cluster hook ({cat})")
    cat, _ = categorize({"title": "self-reviewer スキルの description 改善", "labels": []})
    check(cat == "スキル整備", f"keyword cluster skill ({cat})")
    # categorize: Epic
    cat, _ = categorize({"title": "[Epic] 改善統合追跡", "labels": []})
    check(cat == "（Epic/親追跡）", f"epic ({cat})")
    # categorize: 未分類
    cat, _ = categorize({"title": "なんらかのよくわからない件", "labels": []})
    check(cat == "（その他/未分類）", f"uncategorized ({cat})")
    # normalize_tokens: ストップワード・記号除去
    toks = normalize_tokens("[Epic] フック の 統合 improvement:")
    check("フック" in toks and "improvement" not in toks and "の" not in toks,
          f"normalize_tokens ({toks})")
    # detect_duplicates: 監査コード重複
    dups = detect_duplicates([
        {"num": 1, "title": "[監査P1/SEC-01] a"},
        {"num": 2, "title": "[監査P1/SEC-01] b"},
    ])
    check(any(d["type"] == "audit-code" for d in dups), f"dup audit-code ({dups})")
    # detect_duplicates: タイトル酷似
    dups = detect_duplicates([
        {"num": 3, "title": "stop-router フック 統合 改善"},
        {"num": 4, "title": "stop-router フック 統合 強化"},
    ])
    check(any(d["type"] == "title-similar" for d in dups), f"dup title-similar ({dups})")

    if fail == 0:
        print("PASS: triage_improvements self-test (9 checks)")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description="改善 Issue 棚卸し支援ツール（読み取り専用）")
    ap.add_argument("--label", default="type:improvement", help="対象ラベル（既定: type:improvement）")
    ap.add_argument("--state", default="open", choices=["open", "closed", "all"], help="Issue 状態")
    ap.add_argument("--json", action="store_true", help="JSON を出力")
    ap.add_argument("--out", help="Markdown レポートの出力先パス")
    ap.add_argument("--epic-threshold", type=int, default=6,
                    help="Epic 統合候補とみなす同一カテゴリの最小件数（既定: 6）")
    ap.add_argument("--self-test", action="store_true", help="純粋関数のセルフテストを実行")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(_self_test())

    issues = fetch_issues(args.label, args.state)
    if not issues:
        print("対象 Issue が取得できませんでした（0 件 or 取得失敗）", file=sys.stderr)
        sys.exit(1)

    rep = build_report(issues, args.epic_threshold)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    md = render_markdown(rep)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"レポートを {args.out} に出力しました（{rep['total']} 件）")
    else:
        print(md)


if __name__ == "__main__":
    main()
