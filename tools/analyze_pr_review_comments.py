#!/usr/bin/env python3
"""analyze_pr_review_comments.py — PR レビューコメント全量分析（週次定期実行用）

過去全 PR の inline レビューコメントを取得し、AI レビュー指摘（既定: Layer 1 セルフレビューの
テンプレート本文で判定。`--legacy-reviewers` で廃止済みの Gemini / Copilot のログイン名判定も併用）を
カテゴリ分類・集計して、セルフレビュー・チェックシートの更新判断材料を生成する。

初回分析（2026-06・Issue #2860）の再現可能版。カテゴリルールは本ツールが正本であり、
チェックシート（docs/rules/self-review-checklist.md）の根拠データを更新する際は
本ツールの出力を使う。前回統計（docs/analysis/pr_review_stats_*.json）との差分も出力する。

分類ルール（CATEGORY_RULES）は `config/pr_review_comment_categories.json` から読み込む
（無い/壊れている場合は組み込み既定値にフォールバック）。プロジェクト固有のカテゴリを
追加したい場合はコード変更不要で同ファイルに追記できる（#420）。

Usage:
    python3 tools/analyze_pr_review_comments.py                    # 取得→集計→サマリー表示
    python3 tools/analyze_pr_review_comments.py --report           # docs/analysis/ にレポート+統計を保存
    python3 tools/analyze_pr_review_comments.py --input /tmp/c.json  # 取得済みファイルを使用
    python3 tools/analyze_pr_review_comments.py --json             # 統計 JSON を stdout 出力
    python3 tools/analyze_pr_review_comments.py --legacy-reviewers # 旧 Gemini / Copilot コメントも AI 指摘に含める
    python3 tools/analyze_pr_review_comments.py --self-test        # 判定ロジックの内蔵テスト

実行タイミング: 毎週月曜の 07:00 スロット ⑤.7（{プロジェクト定義: hourly-routing 相当}・週次化 #2900）
Exit code: 0 = 正常 / 1 = 取得・解析失敗
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

JST = timezone(timedelta(hours=9))
REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "docs" / "analysis"

# AI レビュアーのログイン名部分一致パターン（小文字・lower 比較）。
# "copilot" は Copilot / copilot[bot] / copilot-pull-request-reviewer[bot] を全てカバーする
AI_REVIEWER_PATTERNS = ("gemini-code-assist", "copilot")

# Layer 1 セルフレビュー（.claude/skills/code-review/ Step 3-A）のインラインコメント本文テンプレート
# `**🔴 CRITICAL** ・ **CONFIRMED** ・ 観点: 正確性`。外部 AI レビュアー（Gemini / Copilot）は廃止済み
# （ai-reviewer-strategy.md）のため、AI 指摘の既定の判定は「ログイン名」ではなく「本文がこのテンプレートか」
# で行う（Issue #627 対策 E）。PLAUSIBLE のインライン化は #627 で廃止したが、それ以前の投稿を検出できるよう
# 確度の選択肢に残す。
LAYER1_FINDING_RE = re.compile(
    r"\*\*(?P<sev>🔴 CRITICAL|🟡 WARNING|⚪ NIT)\*\*\s*・\s*\*\*(?P<conf>CONFIRMED|PLAUSIBLE)\*\*"
)
LAYER1_SEVERITY = {"🔴 CRITICAL": "critical", "🟡 WARNING": "warning", "⚪ NIT": "nit"}

# カテゴリ分類ルールの組み込み既定値（config/pr_review_comment_categories.json が無い/壊れている
# ときのフォールバック。内容は同ファイルの既定値と同一に保つ・#420）。
# (key, 表示名, 正規表現)。先勝ち（先頭ほど優先）。
DEFAULT_CATEGORY_RULES = [
    ("fact-check", "fact_check（ランク・出典・断定）",
     r"fact_check|ランク\s*[ABC]|出典|一次ソース|source_url|断定"),
    ("path-convention", "パス・ファイル名規約違反",
     r"二重パス|assets/|パス(の|が)?(規約|誤り|不正|間違)|ファイル名.{0,8}(規約|誤)"),
    ("pagination", "ページネーション・limit 不足",
     r"--limit|per_page|ページネーション|paginate"),
    ("timezone", "timezone/datetime",
     r"タイムゾーン|timezone|datetime\.now|naive.{0,12}datetime|JST|UTC"),
    ("encoding", "encoding 未指定",
     r"encoding"),
    ("error-handling", "エラーハンドリング不足・握りつぶし",
     r"握りつぶ|except|エラー(ハンドリング|処理|を無視)|例外(処理|を)|\|\|\s*true|2>/dev/null"),
    ("null-safety", "null/None/欠損キー未処理",
     r"\bNone\b|\bnull\b|\bundefined\b|KeyError|NoneType|欠損|\.get\(|存在しない場合|ファイル(が)?不在|フォールバック"),
    ("regex", "正規表現の不備",
     r"正規表現|regex|エスケープ"),
    ("security", "セキュリティ（秘匿情報等）",
     r"トークン|シークレット|secret|credential|秘匿|漏えい|漏洩|平文"),
    ("concurrency", "競合・並行実行・ロック",
     r"競合|排他|ロック|並行|TOCTOU|レース(コンディション)?|同時実行"),
    ("dry", "DRY 違反・コード重複",
     r"\bDRY\b|重複(した)?(コード|実装|ロジック|定義)|共通化|再発明"),
    ("hardcode", "ハードコード・マジックナンバー",
     r"ハードコード|マジックナンバー|定数化"),
    ("dialect", "口調・キャラクタートーン一貫性",
     r"口調|です・ます|敬体|キャラ(設定|崩壊)"),
    ("url-link", "URL・リンク切れ/誤リンク",
     r"リンク切れ|リンクが(誤|切れ|無効)|URL が?(誤|無効|存在しない)|404"),
    ("doc-impl-drift", "ドキュメント⇔実装の乖離",
     r"乖離|SSOT|実装と(ドキュメント|一致して|異な)|ドキュメントと(実装|一致して|異な)|SKILL\.md と"),
    ("typo", "typo・誤字脱字",
     r"\btypo\b|誤字|タイポ|脱字|スペルミス"),
    ("numeric-inconsistency", "数値・データ不整合（ファイル間/文中）",
     r"不整合|整合性|一致していま|一致しません|食い違|矛盾|ズレ|合計が|件数が(異な|合わ)"),
    ("logic", "ロジックバグ・条件式誤り",
     r"条件(式|分岐)|ロジック|off-by-one|境界(値|条件)|意図(した|と異なる)挙動|反転"),
]
CATEGORY_RULES_CONFIG = Path(__file__).resolve().parent.parent / "config" / "pr_review_comment_categories.json"


def load_category_rules(path: Path = CATEGORY_RULES_CONFIG) -> "list[tuple[str, str, str]]":
    """分類ルールを config ファイルから読む（無い/壊れていれば組み込み既定値にフォールバック）。

    プロジェクト固有のカテゴリ（例: 音声合成・字幕等ドメイン固有の判定）を追加したい場合は、
    このファイルに `categories` エントリを追記するだけでよい（コード変更不要・#420）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        categories = data["categories"]
        rules = [(c["key"], c["label"], c["pattern"]) for c in categories]
        for _, _, pattern in rules:
            re.compile(pattern)  # classify() の re.search が後で失敗しないよう load 時に検証する
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, re.error):
        return DEFAULT_CATEGORY_RULES
    return rules or DEFAULT_CATEGORY_RULES


CATEGORY_RULES = load_category_rules()

# Gemini の重大度バッジ実名称（critical のみ -priority なし。security-* も同重大度に合算）
SEVERITY_BADGES = [
    ("critical", ("codereviewagent/critical.svg", "security-critical.svg")),
    ("high", ("high-priority.svg",)),       # security-high-priority.svg も部分一致で拾う
    ("medium", ("medium-priority.svg",)),   # security-medium-priority.svg も部分一致で拾う
]


def fetch_comments() -> str:
    """gh api --paginate で全 inline レビューコメントを取得する。"""
    cmd = ["gh", "api", f"repos/{REPO}/pulls/comments?per_page=100", "--paginate"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             check=True, timeout=1800).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"ERROR: レビューコメント取得失敗: {e}", file=sys.stderr)
        sys.exit(1)
    return out


def parse_concatenated_json(raw: str) -> list:
    """--paginate の連結 JSON 配列を raw_decode ループで解析する。

    注意: `][` → `,` の文字列置換はコメント本文中の同パターンを破壊するため使わない。
    """
    decoder = json.JSONDecoder()
    items = []
    idx = 0
    n = len(raw)
    while idx < n:
        while idx < n and raw[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(raw, idx)
        if isinstance(obj, list):
            items.extend(obj)
        else:
            items.append(obj)
        idx = end
    return items


def is_ai_reviewer(login: str) -> bool:
    """旧経路: 外部 AI レビュアー（Gemini / Copilot）のログイン名判定（--legacy-reviewers 時のみ使う）。"""
    login_lower = login.lower()
    return any(p.lower() in login_lower for p in AI_REVIEWER_PATTERNS)


def is_layer1_finding(body: str) -> bool:
    """本文が Layer 1 セルフレビューの指摘テンプレートか（既定の AI 指摘判定・#627 対策 E）。"""
    return bool(LAYER1_FINDING_RE.search(body or ""))


def reviewer_key(login: str, body: str, legacy: bool = False) -> "str | None":
    """コメントを AI 指摘として数えるなら reviewer キー（layer1 / gemini / copilot）、数えないなら None。"""
    if is_layer1_finding(body):
        return "layer1"
    if legacy and is_ai_reviewer(login):
        return "gemini" if "gemini" in login.lower() else "copilot"
    return None


def classify(body: str) -> tuple[str, str]:
    for key, label, pattern in CATEGORY_RULES:
        if re.search(pattern, body, re.IGNORECASE):
            return key, label
    return "unclassified", "未分類"


def severity_of(body: str) -> "str | None":
    m = LAYER1_FINDING_RE.search(body or "")
    if m:  # Layer 1 テンプレート（critical / warning / nit）
        return LAYER1_SEVERITY[m.group("sev")]
    for sev, badges in SEVERITY_BADGES:  # 旧 Gemini のバッジ画像
        if any(b in body for b in badges):
            return sev
    return None


def analyze(comments: list, legacy: bool = False) -> dict:
    pr_re = re.compile(r"/pulls/(\d+)")
    stats = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "total_comments": len(comments),
        "ai_comments": 0,
        "ai_by_reviewer": {},
        "prs_with_comments": 0,
        "monthly": {},
        "severity": {},
        "categories": {},
    }
    reviewer_counter = Counter()
    monthly = Counter()
    severity = Counter()
    cat_count = Counter()
    cat_prs = defaultdict(set)
    cat_label = {}
    prs = set()

    for c in comments:
        if not isinstance(c, dict):
            continue
        login = (c.get("user") or {}).get("login", "")
        body = c.get("body") or ""
        m = pr_re.search(c.get("pull_request_url") or "")
        pr = m.group(1) if m else None
        if pr:
            prs.add(pr)
        rkey = reviewer_key(login, body, legacy)
        if rkey is None:
            continue
        stats["ai_comments"] += 1
        reviewer_counter[rkey] += 1
        created = c.get("created_at") or ""
        if len(created) >= 7:
            monthly[created[:7]] += 1
        sev = severity_of(body)
        if sev:
            severity[sev] += 1
        key, label = classify(body)
        cat_count[key] += 1
        cat_label[key] = label
        if pr:
            cat_prs[key].add(pr)

    stats["ai_by_reviewer"] = dict(reviewer_counter)
    stats["prs_with_comments"] = len(prs)
    stats["monthly"] = dict(sorted(monthly.items()))
    stats["severity"] = dict(severity)
    stats["categories"] = {
        key: {"label": cat_label[key], "count": cnt, "prs": len(cat_prs[key])}
        for key, cnt in cat_count.most_common()
    }
    return stats


def find_previous_stats(exclude: "Path | None" = None) -> "Path | None":
    candidates = sorted(ANALYSIS_DIR.glob("pr_review_stats_*.json"))
    if exclude is not None:
        candidates = [p for p in candidates if p.resolve() != exclude.resolve()]
    return candidates[-1] if candidates else None


def prune_old_outputs(keep: int = 8) -> "list[str]":
    """週次運用での肥大化防止: 自動生成の stats/レポートを直近 keep 世代だけ残して削除する。

    対象は YYYY-MM-DD 付きの自動生成ファイルのみ（手動分析 pr-review-comments-analysis-2026-06.md
    のような日なしファイルは対象外）。Git 管理下なら git rm で削除をステージングし、
    失敗時（未追跡・git 不在等）は unlink にフォールバックする。削除したパスのリストを返す。
    """
    removed = []
    repo_root = ANALYSIS_DIR.parent.parent
    for pattern in ("pr_review_stats_????-??-??.json",
                    "pr-review-comments-analysis-????-??-??.md"):
        candidates = sorted(ANALYSIS_DIR.glob(pattern))
        # スライスは len <= keep なら空を返すため条件分岐は不要
        for old in candidates[:-keep]:
            rel = str(old.relative_to(repo_root))
            try:
                subprocess.run(["git", "rm", "-f", "--quiet", str(old)],
                               capture_output=True, check=True, timeout=30)
                removed.append(rel)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                try:
                    old.unlink()
                    removed.append(rel)
                except OSError as e:
                    print(f"WARN: プルーニング失敗 {old.name}: {e}", file=sys.stderr)
    return removed


def diff_summary(stats: dict, prev_path: Path) -> "list[str]":
    try:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"前回統計（{prev_path.name}）の読み込みに失敗: {e}"]
    lines = [f"前回統計: {prev_path.name}（{prev.get('generated_at', '?')}）"]
    prev_cats = prev.get("categories", {})
    if not isinstance(prev_cats, dict):
        prev_cats = {}
    for key, cur in list(stats["categories"].items())[:15]:
        prev_cat = prev_cats.get(key)
        prev_cnt = prev_cat.get("count", 0) if isinstance(prev_cat, dict) else 0
        delta = cur["count"] - prev_cnt
        sign = "+" if delta >= 0 else ""
        lines.append(f"- {cur['label']}: {prev_cnt} → {cur['count']}（{sign}{delta}）")
    return lines


def render_report(stats: dict, prev_path: "Path | None") -> str:
    stamp = datetime.now(JST).strftime("%Y-%m-%d")
    lines = [
        f"# 過去 PR レビューコメント全量分析（{stamp}・週次定期再分析）",
        "",
        "> 生成: `python3 tools/analyze_pr_review_comments.py --report`（週次定期実行）",
        "> チェックシート SSOT: `docs/rules/self-review-checklist.md` / 初回分析: `pr-review-comments-analysis-2026-06.md`",
        "",
        "## 1. データ概要",
        "",
        "| 項目 | 値 |",
        "|------|-----|",
        f"| 取得日 | {stats['generated_at']} |",
        f"| inline レビューコメント総数 | {stats['total_comments']:,} 件 |",
        f"| 対象 PR 数（コメント付き） | {stats['prs_with_comments']:,} PR |",
        f"| AI レビュー指摘 | {stats['ai_comments']:,} 件"
        f"（Layer 1 {stats['ai_by_reviewer'].get('layer1', 0):,} / 旧 Gemini {stats['ai_by_reviewer'].get('gemini', 0):,}"
        f" / 旧 Copilot {stats['ai_by_reviewer'].get('copilot', 0):,}） |",
        f"| 重大度 | Layer 1: critical {stats['severity'].get('critical', 0)}"
        f" / warning {stats['severity'].get('warning', 0)} / nit {stats['severity'].get('nit', 0)}"
        f"（旧 Gemini: high {stats['severity'].get('high', 0)} / medium {stats['severity'].get('medium', 0)}） |",
        "",
        "## 2. カテゴリ別分布（AI 指摘・件数順）",
        "",
        "| カテゴリ | 件数 | PR数 |",
        "|---------|------|------|",
    ]
    for key, cat in stats["categories"].items():
        lines.append(f"| {cat['label']} | {cat['count']} | {cat['prs']} |")
    lines += ["", "## 3. 前回からの変化（上位 15 カテゴリ）", ""]
    if prev_path:
        lines += diff_summary(stats, prev_path)
    else:
        lines.append("（前回統計なし・初回実行）")
    lines += [
        "",
        "## 4. 次のアクション（実行セッションが判断・実施する）",
        "",
        "- [ ] 機械化済みカテゴリ（`tools/self_review_check.py` 対応分）が減少しているか確認する",
        "- [ ] 増加カテゴリ・新出パターンがあれば `docs/rules/self-review-checklist.md` に行を追加する",
        "- [ ] 機械化可能な新パターンは `tools/self_review_check.py` にチェックを追加する（チェックシートと同一 PR で・L-094）",
        "- [ ] 同種指摘 3 回以上のパターンは Lv3 フック昇格を検討する（`docs/rules/harness-escalation.md`）",
        "",
    ]
    return "\n".join(lines)


def run_self_test() -> int:
    """AI 指摘の判定（Layer 1 テンプレート / 旧ログイン名）と severity 抽出の内蔵テスト（#627 対策 E）。"""
    ok = True
    l1_body = "**🟡 WARNING** ・ **CONFIRMED** ・ 観点: 正確性\n\n欠陥の 1 文"
    l1_plausible = "**⚪ NIT** ・ **PLAUSIBLE** ・ 観点: 簡素化"
    gemini_body = "![critical](https://.../codereviewagent/critical.svg) 指摘"
    cases = [
        (("kai-kou", l1_body, False), "layer1"),
        (("kai-kou", l1_plausible, False), "layer1"),
        (("kai-kou", "ふつうの人手コメント", False), None),
        (("gemini-code-assist[bot]", gemini_body, False), None),   # 既定では旧レビュアーを数えない
        (("gemini-code-assist[bot]", gemini_body, True), "gemini"),
        (("copilot-pull-request-reviewer[bot]", "x", True), "copilot"),
        (("copilot-pull-request-reviewer[bot]", l1_body, False), "layer1"),  # 本文優先
    ]
    failed = 0
    for (login, body, legacy), expected in cases:
        got = reviewer_key(login, body, legacy)
        if got != expected:
            print(f"FAIL: reviewer_key({login!r}, legacy={legacy}) = {got!r}（期待 {expected!r}）"); ok = False; failed += 1
    if not failed:
        print(f"PASS: reviewer_key {len(cases)} ケース（Layer 1 テンプレート優先・旧レビュアーは --legacy-reviewers 時のみ）")
    sev_cases = [(l1_body, "warning"), (l1_plausible, "nit"),
                 ("**🔴 CRITICAL** ・ **CONFIRMED** ・ 観点: セキュリティ", "critical"),
                 (gemini_body, "critical"), ("high-priority.svg", "high"), ("なし", None)]
    failed = 0
    for body, expected in sev_cases:
        if severity_of(body) != expected:
            print(f"FAIL: severity_of({body[:30]!r}) = {severity_of(body)!r}（期待 {expected!r}）"); ok = False; failed += 1
    if not failed:
        print(f"PASS: severity_of {len(sev_cases)} ケース（Layer 1 の 3 段階 + 旧 Gemini バッジ）")
    comments = [
        {"user": {"login": "kai-kou"}, "body": l1_body, "pull_request_url": "https://x/pulls/10",
         "created_at": "2026-09-01T00:00:00Z"},
        {"user": {"login": "kai-kou"}, "body": "人手", "pull_request_url": "https://x/pulls/10",
         "created_at": "2026-09-01T00:00:00Z"},
        {"user": {"login": "gemini-code-assist[bot]"}, "body": gemini_body,
         "pull_request_url": "https://x/pulls/11", "created_at": "2026-06-01T00:00:00Z"},
    ]
    st = analyze(comments)
    st_legacy = analyze(comments, legacy=True)
    if st["ai_comments"] == 1 and st["ai_by_reviewer"] == {"layer1": 1} and st["severity"] == {"warning": 1} \
            and st_legacy["ai_comments"] == 2 and st_legacy["ai_by_reviewer"] == {"layer1": 1, "gemini": 1}:
        print("PASS: analyze（既定は Layer 1 のみ・--legacy-reviewers で旧コメントも加算）")
    else:
        print(f"FAIL: analyze: {st['ai_by_reviewer']} / legacy {st_legacy['ai_by_reviewer']}"); ok = False
    print("=== self-test:", "PASS ===" if ok else "FAIL ===")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", help="取得済み JSON ファイル（gh api --paginate の生出力）")
    ap.add_argument("--report", action="store_true",
                    help="docs/analysis/ にレポート Markdown + 統計 JSON を保存")
    ap.add_argument("--json", action="store_true", help="統計 JSON を stdout 出力")
    ap.add_argument("--legacy-reviewers", action="store_true",
                    help="廃止済みの Gemini / Copilot のログイン名判定も AI 指摘に含める（過去分析用）")
    ap.add_argument("--self-test", action="store_true", help="判定ロジックの内蔵テスト")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    if args.input:
        try:
            raw = Path(args.input).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"ERROR: 入力ファイル読み込み失敗: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[analyze] gh api --paginate で全レビューコメントを取得中（数分かかる）...",
              file=sys.stderr)
        raw = fetch_comments()

    try:
        comments = parse_concatenated_json(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON 解析失敗: {e}", file=sys.stderr)
        sys.exit(1)

    stats = analyze(comments, legacy=args.legacy_reviewers)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        sys.exit(0)

    stamp = datetime.now(JST).strftime("%Y-%m-%d")
    if args.report:
        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        stats_path = ANALYSIS_DIR / f"pr_review_stats_{stamp}.json"
        prev_path = find_previous_stats(exclude=stats_path)
        report_path = ANALYSIS_DIR / f"pr-review-comments-analysis-{stamp}.md"
        report = render_report(stats, prev_path)
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        report_path.write_text(report + "\n", encoding="utf-8")
        print(f"[analyze] レポート: {report_path}")
        print(f"[analyze] 統計 JSON: {stats_path}")
        for removed in prune_old_outputs():
            print(f"[analyze] プルーニング（{removed} を削除しました）")
    else:
        print(f"総コメント {stats['total_comments']:,} 件 / AI 指摘 {stats['ai_comments']:,} 件 / "
              f"対象 {stats['prs_with_comments']:,} PR")
        for key, cat in list(stats["categories"].items())[:10]:
            print(f"  {cat['label']}: {cat['count']} 件（{cat['prs']} PR）")
        prev = find_previous_stats()
        if prev:
            print("\n".join(diff_summary(stats, prev)))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)  # main() 内で明示 exit 済みだが規約（成功時 exit 0 明示）に従い冗長に明示
    except Exception as e:  # 内部エラーはメッセージを出して縮退（I-14）
        print(f"[analyze] 内部エラー: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
