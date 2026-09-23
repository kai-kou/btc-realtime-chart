#!/usr/bin/env python3
"""layer1_findings_report.py — Layer 1 / PR 前レビューの計測 JSONL を週次集計する（Issue #627 対策 E）

`tools/record_layer1_findings.py` が追記する content/analytics/review/layer1_findings.jsonl を読み、
ISO 週（JST・月曜始まり）ごとに次を集計する（GitHub API 不要・クラウドでも動く）:

  - 指摘ゼロ PR 率 = PR 後ラウンド 1 の CONFIRMED が 0 件だった PR / PR 後ラウンド 1 を記録した PR
  - PR あたり CONFIRMED 件数（PR 後ラウンド 1・中央値と平均）
  - severity 別 / 観点別の CONFIRMED 件数、PLAUSIBLE・反証除外の件数
  - PR 前レビュー（self-reviewer Step 3.5）の実施件数と PR 前に修正した件数（シフトレフトの効き）

さらに期間全体で「同種指摘 2 回以上」を列挙する（`config/pr_review_comment_categories.json` のカテゴリで
分類し、2 つ以上の PR で出たものを候補にする。分類できない指摘は 観点 × severity で粗く束ね、こちらは
3 つ以上の PR で出たときだけ候補にする＝粗い束ねの誤検知を抑える）。PR 前レビュー（pre）の行は PR 番号を
持たないため、同じブランチでその後に記録された PR 後（post）の行があればその PR に紐づけて 1 PR と数える
（同一 PR の pre + post を「2 PR」と誤認しない）。
候補をチェックシート（docs/rules/self-review-checklist.md）/ 機械チェック（tools/self_review_check.py）へ
反映するか、Issue 化するかの判断は workflow-health-check の週次ゲート（reference.md 4-e）が行う。

同一レビュー（post: pr + round / pre: branch + round + head_sha）の行が複数あるときは recorded_at が最新の行を
採用する（再実行に耐える）。pre 行はブランチ名が PR をまたいで再利用されうる（同名ブランチを main から切り直す運用）
ため head_sha で区別し、head_sha が無い旧行は recorded_at で区別する。

Usage:
    python3 tools/layer1_findings_report.py                 # 直近 4 週の Markdown サマリー
    python3 tools/layer1_findings_report.py --weeks 8 --json
    python3 tools/layer1_findings_report.py --self-test

Exit code: 0 = 正常（データ 0 件でも 0）/ 1 = 読み込み失敗
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "content" / "analytics" / "review" / "layer1_findings.jsonl"
REPEAT_THRESHOLD = 2         # カテゴリ一致の「同種指摘 2 回以上」（self-review-checklist.md「本シートの育て方」2 と同じ閾値）
REPEAT_THRESHOLD_COARSE = 3  # 観点 × severity の粗い束ねは 3 PR 以上（2 PR では「同じ観点の WARNING」程度で偽陽性が多い）
SEVERITY_KEYS = ("critical", "warning", "nit")

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from analyze_pr_review_comments import classify as _classify  # noqa: E402  カテゴリ分類の SSOT を再利用
except Exception:  # noqa: BLE001 - 分離実行時は観点 × severity だけで束ねる
    _classify = None


# ─────────────────────────── 読み込み ───────────────────────────

def _normalize(rec) -> dict:
    """1 行を集計可能な形に正規化する。型が想定と違う行は ValueError（呼び出し側が読み飛ばして数える）。

    JSON として正しくても phase 欠落・round が非整数・confirmed が dict でない等の行は、後段で
    KeyError / AttributeError になって集計全体を止めるため、ここで弾く（#627 PR-2 レビュー指摘）。
    """
    if not isinstance(rec, dict):
        raise ValueError("record is not an object")
    if rec.get("phase") not in ("pre", "post"):
        raise ValueError("phase must be pre/post")
    try:
        raw_round = rec.get("round")
        rec["round"] = 1 if raw_round is None else int(raw_round)   # `or 1` にすると JSON の 0 が 1 に丸まる
        rec["pr"] = None if rec.get("pr") is None else int(rec["pr"])
    except (TypeError, ValueError, OverflowError) as e:  # OverflowError: JSON の Infinity
        raise ValueError(f"round/pr: {e}") from e
    if rec["round"] < 1:
        raise ValueError("round must be >= 1")
    if rec["phase"] == "post" and rec["pr"] is None:   # 書き込み側（record_layer1_findings.py）と同じ不変条件
        raise ValueError("post requires pr")
    if parse_recorded_at(rec.get("recorded_at", "")) is None:   # 週バケットに入らない行は「壊れた行」として数える
        raise ValueError("recorded_at must be 'YYYY-MM-DD HH:MM JST'")
    confirmed = rec.get("confirmed")
    if not isinstance(confirmed, dict):
        raise ValueError("confirmed must be an object")
    try:
        rec["confirmed"] = {k: int(confirmed.get(k, 0) or 0) for k in SEVERITY_KEYS}
        for k in ("plausible", "refuted", "inline", "fixed", "skipped"):
            rec[k] = int(rec.get(k, 0) or 0)
    except (TypeError, ValueError, OverflowError) as e:  # OverflowError: JSON の Infinity
        raise ValueError(f"count field: {e}") from e
    findings = rec.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    rec["findings"] = [f for f in findings if isinstance(f, dict)]
    rec["branch"] = str(rec.get("branch") or "")
    rec["recorded_at"] = str(rec.get("recorded_at") or "")
    return rec


def load_records(path: Path = DEFAULT_PATH) -> "tuple[list[dict], int]":
    """JSONL を読み、(pr または branch, phase, round) ごとに recorded_at 最新の行だけを返す。

    返り値: (records, skipped_lines)。壊れた行（JSON 不正・不正 UTF-8・型が想定と違う）は数えて
    読み飛ばす（1 行の破損で集計全体を止めない。件数は出力に載せて可視化する）。
    """
    if not path.exists():
        return [], 0
    latest: dict = {}
    skipped = 0
    # 不正な UTF-8 バイト（部分書き込み・同時 append の衝突）は U+FFFD に置換して行単位の判定に落とす
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _normalize(json.loads(line))
            except Exception:  # noqa: BLE001 - JSON 不正・型不正に加え RecursionError（深いネスト）/ OverflowError も縮退対象
                skipped += 1
                continue
            key = _dedup_key(rec)
            prev = latest.get(key)
            if prev is None or str(rec.get("recorded_at", "")) >= str(prev.get("recorded_at", "")):
                latest[key] = rec
    return list(latest.values()), skipped


def _ident(rec: dict) -> str:
    """レビュー対象の識別子。post は PR 番号、pre はブランチ + head_sha（無ければ recorded_at）。"""
    if rec.get("pr") is not None:
        return f"pr:{rec['pr']}"
    tag = rec.get("head_sha") or rec.get("recorded_at") or "?"
    return f"branch:{rec.get('branch') or '?'}@{tag}"


def _dedup_key(rec: dict) -> tuple:
    return (_ident(rec), rec["phase"], rec["round"])


def parse_recorded_at(text: str) -> "date | None":
    try:
        return datetime.strptime(str(text)[:16], "%Y-%m-%d %H:%M").date()
    except ValueError:
        return None


def iso_week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def recent_weeks(n: int, today: "date | None" = None) -> "list[str]":
    today = today or datetime.now(JST).date()
    monday = today - timedelta(days=today.weekday())
    return [iso_week_label(monday - timedelta(weeks=i)) for i in range(n - 1, -1, -1)]


# ─────────────────────────── 集計 ───────────────────────────

def _confirmed_total(rec: dict) -> int:
    c = rec.get("confirmed") or {}
    return sum(int(c.get(k, 0)) for k in SEVERITY_KEYS)


def aggregate(records: "list[dict]", weeks: "list[str]") -> dict:
    """週ごとの指標と期間全体の集計を返す。"""
    by_week: dict = {w: _empty_week(w) for w in weeks}
    for rec in records:
        d = parse_recorded_at(rec.get("recorded_at", ""))
        if d is None:
            continue
        w = iso_week_label(d)
        if w not in by_week:
            continue
        bucket = by_week[w]
        total = _confirmed_total(rec)
        c = rec.get("confirmed") or {}
        if rec["phase"] == "post":
            bucket["post_reviews"] += 1
            if int(rec.get("round") or 1) == 1:
                bucket["_r1"][_ident(rec)] = total
            for sev in SEVERITY_KEYS:
                bucket["post_confirmed"][sev] += int(c.get(sev, 0))
            bucket["post_plausible"] += int(rec.get("plausible", 0))
            bucket["post_refuted"] += int(rec.get("refuted", 0))
            bucket["post_inline"] += int(rec.get("inline", 0))
            bucket["post_fixed"] += int(rec.get("fixed", 0))
            bucket["post_skipped"] += int(rec.get("skipped", 0))
        else:
            bucket["pre_reviews"] += 1
            bucket["pre_confirmed"] += total
            bucket["pre_fixed"] += int(rec.get("fixed", 0))
            bucket["pre_skipped"] += int(rec.get("skipped", 0))
        for fnd in rec.get("findings") or []:
            persp = str(fnd.get("perspective") or "?")
            bucket["by_perspective"][persp] += 1

    for bucket in by_week.values():
        r1 = bucket.pop("_r1")
        bucket["post_r1_prs"] = len(r1)
        bucket["zero_finding_prs"] = sum(1 for v in r1.values() if v == 0)
        bucket["zero_finding_pr_rate"] = (round(bucket["zero_finding_prs"] / len(r1), 4) if r1 else None)
        vals = list(r1.values())
        bucket["confirmed_per_pr_median"] = statistics.median(vals) if vals else None
        bucket["confirmed_per_pr_mean"] = round(sum(vals) / len(vals), 2) if vals else None
        bucket["by_perspective"] = dict(bucket["by_perspective"].most_common())
        bucket["post_confirmed"] = dict(bucket["post_confirmed"])

    return {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "weeks": [by_week[w] for w in weeks],
        "repeated": repeated_patterns(records, weeks),
    }


def _empty_week(label: str) -> dict:
    return {
        "iso_week": label,
        "post_reviews": 0, "post_r1_prs": 0, "zero_finding_prs": 0, "zero_finding_pr_rate": None,
        "confirmed_per_pr_median": None, "confirmed_per_pr_mean": None,
        "post_confirmed": Counter(), "post_plausible": 0, "post_refuted": 0, "post_inline": 0,
        "post_fixed": 0, "post_skipped": 0,
        "pre_reviews": 0, "pre_confirmed": 0, "pre_fixed": 0, "pre_skipped": 0,
        "by_perspective": Counter(),
        "_r1": {},
    }


def classify_finding(fnd: dict) -> "tuple[str, str]":
    """finding → (グループキー, 表示名)。カテゴリ規則に当たればそのカテゴリ、当たらなければ 観点 × severity。"""
    summary = str(fnd.get("summary") or "")
    if _classify is not None:
        try:
            key, label = _classify(summary)
        except Exception:  # noqa: BLE001
            key, label = "unclassified", "未分類"
        if key != "unclassified":
            return f"category:{key}", label
    persp = str(fnd.get("perspective") or "?")
    sev = str(fnd.get("severity") or "?").upper()
    return f"perspective:{persp}/{sev}", f"{persp} × {sev}（未分類）"


def _pr_unit(rec: dict, records: "list[dict]") -> str:
    """同種指摘の「PR 数」を数える単位。post は pr:N。pre は同じブランチでその後に記録された post 行が
    あればその PR（同一 PR の pre + post を 2 PR と数えない）、無ければ branch@head_sha。"""
    if rec.get("pr") is not None:
        return f"pr:{rec['pr']}"
    later_posts = [
        r for r in records
        if r.get("pr") is not None and r.get("branch") and r.get("branch") == rec.get("branch")
        and str(r.get("recorded_at", "")) >= str(rec.get("recorded_at", ""))
    ]
    if later_posts:
        first = min(later_posts, key=lambda r: str(r.get("recorded_at", "")))
        return f"pr:{first['pr']}"
    return _ident(rec)


def repeated_patterns(records: "list[dict]", weeks: "list[str]") -> "list[dict]":
    """期間内の finding を束ね、複数の PR で出たグループを件数順に返す。

    カテゴリ一致は REPEAT_THRESHOLD（2 PR）以上、観点 × severity の粗い束ねは REPEAT_THRESHOLD_COARSE（3 PR）以上。
    """
    groups: dict = defaultdict(lambda: {"label": "", "prs": set(), "count": 0, "examples": []})
    week_set = set(weeks)
    for rec in records:
        d = parse_recorded_at(rec.get("recorded_at", ""))
        if d is None or iso_week_label(d) not in week_set:
            continue
        unit = _pr_unit(rec, records)
        for fnd in rec.get("findings") or []:
            key, label = classify_finding(fnd)
            g = groups[key]
            g["label"] = label
            g["prs"].add(unit)
            g["count"] += 1
            if len(g["examples"]) < 3:
                g["examples"].append(f"{fnd.get('location', '?')} — {fnd.get('summary', '')}")
    out = []
    for key, g in groups.items():
        threshold = REPEAT_THRESHOLD if key.startswith("category:") else REPEAT_THRESHOLD_COARSE
        if len(g["prs"]) >= threshold:
            out.append({"key": key, "label": g["label"], "count": g["count"],
                        "prs": sorted(g["prs"]), "examples": g["examples"]})
    out.sort(key=lambda x: (-len(x["prs"]), -x["count"], x["key"]))
    return out


# ─────────────────────────── 出力 ───────────────────────────

def _pct(v: "float | None") -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


def _num(v) -> str:
    return "-" if v is None else str(v)


def render_markdown(report: dict, skipped_lines: int = 0) -> str:
    lines = [
        f"## Layer 1 計測サマリー（{report['generated_at']}・直近 {len(report['weeks'])} 週）",
        "",
        "| 週 | PR 後 R1 の PR 数 | 指摘ゼロ PR 率 | CONFIRMED / PR（中央値 / 平均） | 🔴 / 🟡 / ⚪ | PLAUSIBLE / 反証 | PR 前レビュー（件 / 修正） |",
        "|----|------|------|------|------|------|------|",
    ]
    for w in report["weeks"]:
        c = w["post_confirmed"]
        lines.append(
            f"| {w['iso_week']} | {w['post_r1_prs']} | {_pct(w['zero_finding_pr_rate'])} | "
            f"{_num(w['confirmed_per_pr_median'])} / {_num(w['confirmed_per_pr_mean'])} | "
            f"{c.get('critical', 0)} / {c.get('warning', 0)} / {c.get('nit', 0)} | "
            f"{w['post_plausible']} / {w['post_refuted']} | {w['pre_reviews']} / {w['pre_fixed']} |"
        )
    persp_total: Counter = Counter()
    for w in report["weeks"]:
        persp_total.update(w["by_perspective"])
    if persp_total:
        cols = [k for k, _ in persp_total.most_common(8)]
        lines += ["", "観点別 CONFIRMED の週次推移（finding 記録分・上位 8 観点）", "",
                  "| 週 | " + " | ".join(cols) + " |",
                  "|----|" + "|".join("------" for _ in cols) + "|"]
        for w in report["weeks"]:
            lines.append(f"| {w['iso_week']} | " + " | ".join(str(w["by_perspective"].get(c, 0)) for c in cols) + " |")
    lines += ["", f"### 同種指摘 {REPEAT_THRESHOLD} 回以上（チェックシート / 機械チェック候補）", ""]
    if report["repeated"]:
        for g in report["repeated"]:
            lines.append(f"- **{g['label']}**: {g['count']} 件 / {len(g['prs'])} PR（{', '.join(g['prs'])}）")
            for ex in g["examples"]:
                lines.append(f"  - {ex}")
    else:
        lines.append("- なし")
    if skipped_lines:
        lines += ["", f"> 読み飛ばした壊れた行: {skipped_lines}"]
    return "\n".join(lines)


# ─────────────────────────── self-test ───────────────────────────

def _rec(pr, phase, rnd, recorded_at, confirmed=(0, 0, 0), fixed=0, skipped=0, findings=(), branch="b",
         plausible=0, refuted=0, inline=0):
    return {
        "recorded_at": recorded_at, "pr": pr, "branch": branch, "phase": phase, "round": rnd,
        "confirmed": {"critical": confirmed[0], "warning": confirmed[1], "nit": confirmed[2]},
        "plausible": plausible, "refuted": refuted, "inline": inline, "fixed": fixed, "skipped": skipped,
        "perspectives": [], "review_md": "none", "findings": list(findings),
    }


def run_self_test() -> int:
    import tempfile
    ok = True
    today = date(2026, 9, 11)  # 金曜（2026-W37）
    weeks = recent_weeks(2, today)
    if weeks != ["2026-W36", "2026-W37"]:
        print(f"FAIL: recent_weeks: {weeks}"); ok = False
    else:
        print("PASS: recent_weeks（ISO 週・昇順）")

    f_timeout_a = {"severity": "WARNING", "perspective": "正確性", "location": "a.sh:10",
                   "summary": "timeout が無く握りつぶしている（2>/dev/null）"}
    f_timeout_b = {"severity": "WARNING", "perspective": "正確性", "location": "b.sh:20",
                   "summary": "except で握りつぶし"}
    f_unique = {"severity": "NIT", "perspective": "簡素化", "location": "c.py:1", "summary": "命名"}
    rows = [
        _rec(1, "post", 1, "2026-09-08 10:00 JST", (0, 0, 0)),                        # 指摘ゼロ
        _rec(2, "post", 1, "2026-09-09 10:00 JST", (1, 1, 0), fixed=2, inline=2, findings=[f_timeout_a], branch="feat/x"),
        _rec(3, "post", 1, "2026-09-10 10:00 JST", (0, 1, 0), fixed=1, inline=1, findings=[f_timeout_b]),
        _rec(3, "post", 1, "2026-09-10 11:00 JST", (0, 0, 1), fixed=1, inline=1, findings=[f_unique]),  # 再実行（最新採用）
        _rec(2, "post", 2, "2026-09-09 12:00 JST", (0, 0, 0)),                        # R2 は率の分母に入れない
        # PR 2 の PR 前レビュー（同じブランチ・post より前）。同じカテゴリの finding を持つが PR 2 と同一単位に紐づく
        _rec(None, "pre", 1, "2026-09-09 09:00 JST", (0, 3, 1), fixed=3, skipped=1, branch="feat/x", findings=[f_timeout_a]),
        # 同名ブランチの別 PR（head_sha が違う）→ 別レビューとして両方数える / 同じ head_sha の再実行 → 最新だけ
        dict(_rec(None, "pre", 1, "2026-09-11 09:00 JST", (0, 1, 0), fixed=1, branch="feat/x"), head_sha="aaaa111"),
        dict(_rec(None, "pre", 1, "2026-09-11 09:30 JST", (0, 2, 0), fixed=2, branch="feat/x"), head_sha="aaaa111"),
        _rec(9, "post", 1, "2026-09-01 10:00 JST", (2, 0, 0), fixed=2),               # 前週（W36）
        _rec(8, "post", 1, "2026-08-20 10:00 JST", (5, 0, 0)),                        # 範囲外
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "l1.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write("{broken json\n")
            # JSON としては正しいが型が想定と違う行 3 種 + phase 欠落行（後段で KeyError / AttributeError に
            # なって集計を止めないことの否定テスト・#627 PR-2 レビュー指摘）
            f.write(json.dumps({"recorded_at": "2026-09-10 10:00 JST", "pr": 5, "phase": "post", "round": "abc",
                                "confirmed": {"critical": 0, "warning": 0, "nit": 0}}) + "\n")
            f.write(json.dumps({"recorded_at": "2026-09-10 10:00 JST", "pr": 6, "phase": "post", "round": 1,
                                "confirmed": [1, 2, 3]}) + "\n")
            f.write(json.dumps({"recorded_at": "2026-09-10 10:00 JST", "pr": 7, "round": 1,
                                "confirmed": {"critical": 0, "warning": 0, "nit": 0}}) + "\n")
        with open(path, "ab") as fb:
            fb.write(b'\xff{"pr": 8, "phase": "post"}\n')   # 不正な UTF-8 バイトを含む行
        with open(path, "a", encoding="utf-8") as f:
            f.write("[" * 5000 + "]" * 5000 + "\n")           # 深いネスト → json.loads が RecursionError
            f.write('{"recorded_at": "2026-09-10 10:00 JST", "pr": 9, "phase": "post", "round": Infinity, '
                    '"confirmed": {"critical": 0, "warning": 0, "nit": 0}}\n')   # Infinity → int() が OverflowError
            # Layer 2 レビュー指摘: JSON 整数 0 の round（falsy で 1 に丸めない）/ post なのに pr 欠落 / recorded_at 不正
            f.write(json.dumps({"recorded_at": "2026-09-10 10:00 JST", "pr": 10, "phase": "post", "round": 0,
                                "confirmed": {"critical": 0, "warning": 0, "nit": 0}}) + "\n")
            f.write(json.dumps({"recorded_at": "2026-09-10 10:00 JST", "pr": None, "phase": "post", "round": 1,
                                "branch": "feat/ghost", "confirmed": {"critical": 0, "warning": 0, "nit": 0}}) + "\n")
            f.write(json.dumps({"recorded_at": "10 Sep 2026", "pr": 11, "phase": "post", "round": 1,
                                "confirmed": {"critical": 0, "warning": 0, "nit": 0}}) + "\n")
        records, skipped = load_records(path)
        if skipped != 10:
            print(f"FAIL: 壊れた行の読み飛ばし数 {skipped}（期待 10: JSON 不正 / round 非整数 / confirmed 非 dict / phase 欠落 / 不正 UTF-8 / 深いネスト / Infinity / round 0 / post の pr 欠落 / recorded_at 不正）"); ok = False
        elif len(records) != len(rows) - 2:
            print(f"FAIL: 重複排除後の件数 {len(records)}（期待 {len(rows) - 2}: PR 3 の再実行と head_sha 同一の pre 再実行を除く）"); ok = False
        else:
            print("PASS: 読み込み（JSON 不正・型不正・phase 欠落・不正 UTF-8・深いネスト・Infinity・round 0・post の pr 欠落・recorded_at 不正の行を読み飛ばし・同一キーは recorded_at 最新を採用）")

        report = aggregate(records, weeks)
        w37 = report["weeks"][1]
        w36 = report["weeks"][0]
        if w37["post_r1_prs"] != 3 or w37["zero_finding_prs"] != 1 or abs(w37["zero_finding_pr_rate"] - round(1 / 3, 4)) > 1e-9:
            print(f"FAIL: 指摘ゼロ PR 率: {w37}"); ok = False
        elif w37["confirmed_per_pr_median"] != 1 or w37["confirmed_per_pr_mean"] != 1.0:
            print(f"FAIL: CONFIRMED / PR: {w37}"); ok = False
        elif w37["post_confirmed"] != {"critical": 1, "warning": 1, "nit": 1}:
            print(f"FAIL: severity 集計（再実行行は最新のみ・R2 も含む）: {w37['post_confirmed']}"); ok = False
        elif w37["post_reviews"] != 4 or w37["post_inline"] != 3 or w37["post_fixed"] != 3:
            print(f"FAIL: post の件数: {w37}"); ok = False
        elif w37["pre_reviews"] != 2 or w37["pre_confirmed"] != 6 or w37["pre_fixed"] != 5 or w37["pre_skipped"] != 1:
            print(f"FAIL: pre の件数: {w37}"); ok = False
        elif w36["post_r1_prs"] != 1 or w36["zero_finding_pr_rate"] != 0.0 or w36["post_confirmed"].get("critical") != 2:
            print(f"FAIL: 前週の集計: {w36}"); ok = False
        elif w37["by_perspective"] != {"正確性": 2, "簡素化": 1}:
            print(f"FAIL: 観点別（再実行で置き換わった finding は数えない）: {w37['by_perspective']}"); ok = False
        else:
            print("PASS: 週次集計（指摘ゼロ率 1/3・中央値 1・severity / 観点別・pre は head_sha 別に数え同一 head の再実行は最新のみ・範囲外除外）")

        rep = report["repeated"]
        # PR 2 の post と pre（同じブランチ・紐づけで同一単位）の f_timeout_a、PR 3 の最新行の f_unique
        # （f_timeout_b は再実行で置き換わり消える）→ 「握りつぶし」カテゴリは 1 PR だけになり候補にならない
        if rep:
            print(f"FAIL: 同種指摘の候補が出ている（期待なし・同一 PR の pre + post を 2 PR と数えない）: {rep}"); ok = False
        else:
            print("PASS: 同種指摘（1 PR だけの指摘・同一 PR の pre + post は候補にしない）")
        # 観点 × severity の粗い束ねは 2 PR では候補にせず 3 PR で候補にする（未分類の要旨 zzz-*）
        coarse = [_rec(40 + i, "post", 1, "2026-09-10 13:00 JST", (0, 1, 0), fixed=1,
                       findings=[{"severity": "WARNING", "perspective": "正確性", "location": f"z{i}.py:1",
                                  "summary": f"zzz-{i}"}]) for i in range(3)]
        rep_c2 = aggregate(records + coarse[:2], weeks)["repeated"]
        rep_c3 = aggregate(records + coarse, weeks)["repeated"]
        if not rep_c2 and len(rep_c3) == 1 and rep_c3[0]["key"] == "perspective:正確性/WARNING" and len(rep_c3[0]["prs"]) == 3:
            print("PASS: 粗い束ね（観点 × severity）は 3 PR 以上で候補化（2 PR では出さない）")
        else:
            print(f"FAIL: 粗い束ねの閾値: 2PR={rep_c2} / 3PR={rep_c3}"); ok = False
        # 2 PR で同カテゴリ → 候補になる
        rows2 = records + [_rec(4, "post", 1, "2026-09-10 12:00 JST", (0, 1, 0), fixed=1, findings=[f_timeout_b])]
        rep2 = aggregate(rows2, weeks)["repeated"]
        # PR 2（pre + post の 2 件・同一単位）と PR 4 → 2 PR・計 3 件で候補になる
        if len(rep2) == 1 and rep2[0]["prs"] == ["pr:2", "pr:4"] and rep2[0]["count"] == 3 \
                and rep2[0]["key"].startswith("category:") == (_classify is not None):
            print(f"PASS: 同種指摘 2 PR 以上を候補化（{rep2[0]['label']}）")
        else:
            print(f"FAIL: 同種指摘の候補化: {rep2}"); ok = False

        md = render_markdown(report, skipped)
        if "指摘ゼロ PR 率" in md and "| 2026-W37 | 3 | 33% |" in md and "読み飛ばした壊れた行: 10" in md \
                and "観点別 CONFIRMED の週次推移" in md and "| 2026-W37 | 2 | 1 |" in md:
            print("PASS: Markdown 出力")
        else:
            print(f"FAIL: Markdown 出力:\n{md}"); ok = False

        empty_report = aggregate([], weeks)
        if empty_report["weeks"][0]["zero_finding_pr_rate"] is None and not empty_report["repeated"]:
            print("PASS: データ 0 件でも例外にならない")
        else:
            print("FAIL: データ 0 件の扱い"); ok = False

    print("=== self-test:", "PASS ===" if ok else "FAIL ===")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weeks", type=int, default=4, help="集計対象週数（既定 4）")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    ap.add_argument("--path", help=f"読み込み元（既定 {DEFAULT_PATH.relative_to(ROOT)}）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return run_self_test()
    path = Path(args.path) if args.path else DEFAULT_PATH
    try:
        records, skipped = load_records(path)
    except OSError as e:
        print(f"[layer1-report] ERROR: 読み込み失敗: {e}", file=sys.stderr)
        return 1
    report = aggregate(records, recent_weeks(max(1, args.weeks)))
    if args.json:
        report["skipped_lines"] = skipped
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
