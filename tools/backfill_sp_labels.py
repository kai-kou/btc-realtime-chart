#!/usr/bin/env python3
"""過去 Issue への sp ラベルバックフィル + PR↔Issue 突合ベロシティ算出（Issue #3009）。

docs/rules/session-sprint-rules.md §3 の SP スケール・工程別標準 SP に基づき、
sp ラベル未付与の全 Issue をヒューリスティック分類して sp:1/2/3/5 を付与する
（sp:8 は「新スキル設計・アーキテクチャ変更」級で人の判断を要するため自動分類対象外。§3.1 では分割シグナル）。
その後、全マージ済み PR を closingIssuesReferences で Issue と突合し、
PR ごとの SP と週次ベロシティを算出してレポートを出力する。

Usage:
    python3 tools/backfill_sp_labels.py backfill --dry-run   # 分類結果のみ表示
    python3 tools/backfill_sp_labels.py backfill --apply     # ラベルを実際に付与
    python3 tools/backfill_sp_labels.py velocity             # ベロシティ算出・レポート出力
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
OWNER, NAME = REPO.split("/", 1)
JST = timezone(timedelta(hours=9))
SP_VALUES = {1, 2, 3, 5, 8}
OUT_DIR = PROJECT_DIR / "content" / "analytics" / "sprint"


def _run(cmd: list, timeout: int = 60) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            print(f"[warn] {' '.join(cmd[:4])}... failed: {res.stderr.strip()[:200]}", file=sys.stderr)
            return ""
        return res.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[warn] {' '.join(cmd[:4])}... error: {e}", file=sys.stderr)
        return ""


# ──────────────────────────────────────────
# Issue 取得（GraphQL ページネーション）
# ──────────────────────────────────────────

def fetch_all_issues() -> list:
    """全 Issue（open + closed）を GraphQL で取得する。PR は含まれない。"""
    issues = []
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = ('{ repository(owner:"%s", name:"%s") { '
                 'issues(first: 100%s, states: [OPEN, CLOSED], orderBy:{field:CREATED_AT, direction:ASC}) { '
                 'pageInfo { hasNextPage endCursor } '
                 'nodes { number title state createdAt closedAt labels(first:30){nodes{name}} } '
                 '} } }' % (OWNER, NAME, after))
        out = _run(["gh", "api", "graphql", "-f", f"query={query}"], timeout=60)
        if not out:
            break
        try:
            res = json.loads(out)
            data = (res.get("data") or {}).get("repository") or {}
            conn = data.get("issues") or {}
            nodes = conn.get("nodes") or []
        except Exception:
            break
        for n in nodes:
            if not n or not isinstance(n, dict):
                continue
            labels = []
            label_conn = n.get("labels") or {}
            for l in (label_conn.get("nodes") or []):
                if isinstance(l, dict) and l.get("name"):
                    labels.append(l["name"])
            issues.append({
                "number": n.get("number"),
                "title": n.get("title") or "",
                "state": n.get("state") or "",
                "labels": labels,
            })
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            cursor = page.get("endCursor")
            if not cursor:
                break  # API 異常時の無限ループ防止
            print(f"  ... {len(issues)} 件取得済み")
        else:
            break
    return issues


# ──────────────────────────────────────────
# SP 分類ヒューリスティック（docs/rules/session-sprint-rules.md §3）
# ──────────────────────────────────────────

def classify_sp(issue: dict) -> tuple:
    """Issue を sp 値に分類する。戻り値: (sp, 理由) / (None, 理由) はスキップ。

    sp:8 はタイトルからの機械判定が不可能（アーキテクチャ変更級・分割シグナル）のため返さない。
    """
    title = issue["title"]
    labels = set(issue["labels"])
    tl = title.lower()

    # スキップ条件
    for l in labels:
        if l.startswith("sp:"):
            return None, f"既に {l} 付与済み"
    if "phase:1-neta" in labels:
        return None, "ネタ候補（昇格時に sp:3 付与・§3.3）"

    # 工程別標準 SP（§3.2）— [V{ID}] 制作 Issue
    if re.match(r"^\[V\d+\]", title) or "type:content" in labels:
        if re.search(r"phase\s*2|リサーチ|research", tl):
            return 3, "工程別: Phase 2 リサーチ"
        if re.search(r"phase\s*3|台本|script", tl):
            return 5, "工程別: Phase 3 台本"
        if re.search(r"音声|audio", tl):
            return 3, "工程別: Phase 4 音声"
        if re.search(r"画像|image|サムネ", tl):
            return 3, "工程別: Phase 4 画像"
        if re.search(r"phase\s*5|動画|video|レンダ", tl):
            return 3, "工程別: Phase 5 動画"
        if re.search(r"shorts|tiktok|ショート", tl):
            return 2, "工程別: Shorts/TikTok"
        if re.search(r"公開|publish|アップロード|upload", tl):
            return 2, "工程別: 公開・アップロード"
        return 3, "工程別: 制作系（既定）"

    # Shorts / TikTok / SNS 系
    if re.match(r"^\[(shorts|tiktok)", tl):
        return 2, "工程別: Shorts/TikTok"
    if re.match(r"^\[(sns|comment)", tl) or re.search(r"オーガニック投稿|sns-\d+", tl):
        return 1, "工程別: SNS/コメント 1 件"

    # レポート・ログ・状態更新（機械的）
    if re.search(r"週次|日次|レポート|report|ダイジェスト|digest|棚卸し", tl):
        return 1, "機械的: レポート系"

    # retro-try（typo 級の Try が中心・§3.1）
    if "type:retro-try" in labels or title.startswith("[Retro]"):
        return 2, "retro-try（小さな改善）"

    # type ラベル基準
    if "type:bug" in labels or title.startswith("fix:"):
        return 2, "type:bug / fix（小修正）"
    if "type:docs" in labels or title.startswith("docs:"):
        return 2, "type:docs（ドキュメント）"
    if "type:improvement" in labels or title.startswith("improvement:"):
        return 3, "type:improvement（標準）"
    if "type:feature" in labels or title.startswith("feat:"):
        return 5, "type:feature（複合タスク）"
    if re.match(r"^T\d+-\d+:", title):
        return 3, "マイルストーンタスク（標準）"

    return 3, "既定（標準タスク）"


def cmd_backfill(apply: bool) -> int:
    print(f"📥 全 Issue を取得中（{REPO}）...")
    issues = fetch_all_issues()
    print(f"✅ {len(issues)} 件取得完了\n")

    plan = []
    skipped = Counter()
    dist = Counter()
    for iss in issues:
        sp, reason = classify_sp(iss)
        if sp is None:
            skipped[reason.split("（")[0]] += 1
            continue
        plan.append((iss["number"], sp, reason, iss["title"]))
        dist[sp] += 1

    print("── 分類結果 ──")
    for sp in sorted(dist):
        print(f"  sp:{sp} → {dist[sp]} 件")
    print(f"  付与対象合計: {len(plan)} 件")
    for reason, cnt in skipped.most_common():
        print(f"  スキップ（{reason}）: {cnt} 件")

    if not apply:
        print("\n[dry-run] サンプル 20 件:")
        for num, sp, reason, title in plan[:20]:
            print(f"  #{num} sp:{sp} [{reason}] {title[:50]}")
        return 0

    print(f"\n🏷️ {len(plan)} 件にラベル付与開始...")
    done = 0
    failed = []
    for num, sp, reason, _ in plan:
        out = _run(["gh", "api", f"repos/{REPO}/issues/{num}/labels",
                    "--method", "POST", "-f", f"labels[]=sp:{sp}"], timeout=30)
        if out:
            done += 1
        else:
            failed.append(num)
        if done % 50 == 0 and done:
            print(f"  🏷️ 進捗 {done}/{len(plan)} 件（約{done * 100 // len(plan)}%）")
        time.sleep(0.1)  # secondary rate limit 回避
    print(f"✅ 付与完了: {done} 件 / 失敗: {len(failed)} 件")
    if failed:
        print(f"  失敗 Issue: {failed[:30]}")
    return 0 if not failed else 1


# ──────────────────────────────────────────
# ベロシティ算出（マージ済み PR ↔ Issue 突合）
# ──────────────────────────────────────────

def fetch_merged_prs() -> list:
    prs = []
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = ('{ repository(owner:"%s", name:"%s") { '
                 'pullRequests(states: MERGED, first: 100%s, orderBy:{field:CREATED_AT, direction:ASC}) { '
                 'pageInfo { hasNextPage endCursor } '
                 'nodes { number title createdAt mergedAt labels(first:30){nodes{name}} '
                 'closingIssuesReferences(first:10){ nodes { number labels(first:30){ nodes { name } } } } '
                 '} } } }' % (OWNER, NAME, after))
        out = _run(["gh", "api", "graphql", "-f", f"query={query}"], timeout=60)
        if not out:
            break
        try:
            res = json.loads(out)
            conn = ((res.get("data") or {}).get("repository") or {}).get("pullRequests") or {}
            nodes = conn.get("nodes") or []
        except Exception:
            break
        prs.extend(n for n in nodes if isinstance(n, dict))
        page = conn.get("pageInfo") or {}
        if page.get("hasNextPage"):
            cursor = page.get("endCursor")
            if not cursor:
                break  # API 異常時の無限ループ防止
            print(f"  ... {len(prs)} 件取得済み")
        else:
            break
    return prs


def estimate_sp_from_title(title: str) -> int:
    """Issue 未リンク PR 用のタイトルベース SP 推定（classify_sp と同基準 + PR 固有パターン）。"""
    tl = title.lower()
    # 運用・状態ファイル更新 PR（§3.1 sp:1: 機械的変更）
    if re.match(r"^\[(daily|wip|hourly|state)\]", tl) or re.search(r"state file|状態ファイル|自動コミット", tl):
        return 1
    return classify_sp({"title": title, "labels": []})[0] or 3


def _sp_from(label_nodes) -> int:
    if not label_nodes or not isinstance(label_nodes, list):
        return 0
    for l in label_nodes:
        if not l or not isinstance(l, dict):
            continue
        name = l.get("name", "")
        if name.startswith("sp:"):
            try:
                v = int(name.split(":", 1)[1])
                return v if v in SP_VALUES else 0
            except ValueError:
                continue
    return 0


def cmd_velocity() -> int:
    print(f"📥 全マージ済み PR を取得中（{REPO}）...")
    prs = fetch_merged_prs()
    print(f"✅ {len(prs)} 件取得完了\n")

    records = []
    counted_issues = set()
    weekly = defaultdict(lambda: {"sp": 0, "matched_sp": 0, "prs": 0, "sp_prs": 0, "lead_min": 0.0})
    for pr in prs:
        try:
            merged_at = datetime.fromisoformat(str(pr.get("mergedAt", "")).replace("Z", "+00:00"))
            created_at = datetime.fromisoformat(str(pr.get("createdAt", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        pr_sp = 0
        sp_source = None
        linked = []
        closing = pr.get("closingIssuesReferences") or {}
        for iss in (closing.get("nodes") or []) if isinstance(closing, dict) else []:
            if not iss or not isinstance(iss, dict):
                continue
            num = iss.get("number")
            linked.append(num)
            if num in counted_issues:
                continue  # 複数 PR が同一 Issue を閉じる場合の二重計上防止
            iss_labels = iss.get("labels") or {}
            sp = _sp_from(iss_labels.get("nodes") if isinstance(iss_labels, dict) else None)
            if sp:
                counted_issues.add(num)
                pr_sp += sp
        if pr_sp:
            sp_source = "issue"
        else:
            pr_labels = pr.get("labels") or {}
            pr_sp = _sp_from(pr_labels.get("nodes") if isinstance(pr_labels, dict) else None)
            if pr_sp:
                sp_source = "pr_label"
            else:
                pr_sp = estimate_sp_from_title(pr.get("title") or "")
                sp_source = "title_estimate"

        merged_jst = merged_at.astimezone(JST)
        iso = merged_jst.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        lead_min = (merged_at - created_at).total_seconds() / 60.0
        records.append({
            "pr": pr.get("number"), "title": (pr.get("title") or "")[:80],
            "merged_at": merged_jst.isoformat(), "week": week_key,
            "sp": pr_sp, "sp_source": sp_source,
            "linked_issues": linked, "lead_min": round(lead_min, 1),
        })
        w = weekly[week_key]
        w["prs"] += 1
        if pr_sp:
            w["sp"] += pr_sp
            w["sp_prs"] += 1
            w["lead_min"] += lead_min
            if sp_source == "issue":
                w["matched_sp"] += pr_sp

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = OUT_DIR / "velocity_backfill.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_sp = sum(r["sp"] for r in records)
    matched_sp = sum(r["sp"] for r in records if r["sp_source"] == "issue")
    matched_prs = sum(1 for r in records if r["sp_source"] == "issue")
    est_prs = sum(1 for r in records if r["sp_source"] == "title_estimate")
    weeks = sorted(weekly)
    active_weeks = [w for w in weeks if weekly[w]["sp"]]
    avg_weekly_sp = (total_sp / len(active_weeks)) if active_weeks else 0

    lines = [
        "# ベロシティ バックフィルレポート（全履歴・PR↔Issue 突合）",
        "",
        f"> 生成: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')} / Issue #3009",
        "> 算出方法: マージ済み PR の closingIssuesReferences に紐づく Issue の sp ラベル合計（Issue 二重計上防止）。",
        "> Issue 未リンク PR は PR 自身の sp ラベル → タイトルベース推定（`docs/rules/session-sprint-rules.md` §3 基準）の順にフォールバック。",
        "",
        "## サマリー",
        "",
        f"- マージ済み PR: **{len(records)} 件**",
        f"  - Issue 突合: {matched_prs} 件（{matched_prs * 100 // max(len(records), 1)}%） / タイトル推定: {est_prs} 件",
        f"- 累計 done_sp: **{total_sp} SP**（うち Issue 突合分: {matched_sp} SP）",
        f"- 週平均ベロシティ: **{avg_weekly_sp:.1f} SP/週**（{len(active_weeks)} 週）",
        "",
        "## 週次ベロシティ",
        "",
        "| 週 | done_sp | うち突合 SP | PR 数 | SP あり PR | 分/SP（リードタイム） |",
        "|----|---------|------------|-------|-----------|---------------------|",
    ]
    for wk in weeks:
        w = weekly[wk]
        mps = f"{w['lead_min'] / w['sp']:.0f}" if w["sp"] else "—"
        lines.append(f"| {wk} | {w['sp']} | {w['matched_sp']} | {w['prs']} | {w['sp_prs']} | {mps} |")
    lines += [
        "",
        "## 備考",
        "",
        "- sp ラベルは 2026-06-12 のバックフィル（ヒューリスティック分類・`docs/rules/session-sprint-rules.md` §3）で過去 Issue に一括付与したもの。",
        "- 「タイトル推定」は Closes リンクのない過去 PR（初期 feat/docs・運用 state 更新等）の参考値。今後の PR は sp ラベル必須運用（§7）で突合率が上がる。",
        "- 過去 PR のリードタイムには夜間待機・レビュー待ちが含まれるため、分/SP は参考値。",
        "- 今後の正値は日次スプリント報告（`half_day_summary.py`）が `sprint_log.jsonl` に記録する。",
    ]
    report_path = OUT_DIR / "velocity_backfill_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"📊 累計 done_sp: {total_sp} SP（Issue 突合 {matched_sp} SP / 突合 PR {matched_prs}/{len(records)} 件）")
    print(f"📊 週平均ベロシティ: {avg_weekly_sp:.1f} SP/週（{len(active_weeks)} 週）")
    print(f"📝 レポート: {report_path}")
    print(f"📝 明細: {jsonl_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="sp ラベルのバックフィル")
    g = bf.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    sub.add_parser("velocity", help="ベロシティ算出・レポート出力")
    args = ap.parse_args()
    if args.cmd == "backfill":
        return cmd_backfill(apply=args.apply)
    return cmd_velocity()


if __name__ == "__main__":
    sys.exit(main())
