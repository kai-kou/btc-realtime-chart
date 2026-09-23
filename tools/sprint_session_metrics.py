#!/usr/bin/env python3
"""セッション単位の SP × トークン × 対応時間ベロシティ計測（Issue #3024）。

cost_log.jsonl（Stop フックがセッション ID 付きで追記）とマージ済み PR を突合し、
1 セッション = 1 スプリントの実測ベロシティ（done_sp・tokens/sp・分/sp・$/sp）を算出する。

PR↔セッションの紐付け:
  1. PR 本文の `Session-Id: {UUID}` トレーラー（session-sprint-rules.md §2 で必須化・正確）
  2. フォールバック: mergedAt がセッション活動時間窓（±30分）に入る唯一のセッション（best effort）

Usage:
    python3 tools/sprint_session_metrics.py current             # 現セッションのトークン消費を表示
    python3 tools/sprint_session_metrics.py collect --hours 24  # 期間内のセッション別メトリクス
    python3 tools/sprint_session_metrics.py collect --hours 24 --persist  # + jsonl 永続化
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
OWNER, NAME = REPO.split("/", 1)
JST = timezone(timedelta(hours=9))
SP_VALUES = {1, 2, 3, 5, 8}
def _anchored(env_name: str, default: Path) -> Path:
    """環境変数で上書きされたパスを解決する。相対パスは実行場所（CWD）依存で
    出力が迷子になるのを防ぐため PROJECT_DIR 基準に正規化し、~ も展開する。"""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (PROJECT_DIR / p)


# 出力先は STATE_DIR / ANALYTICS_DIR 環境変数で上書き可能（既定の content/ は制作系の慣習）。
_STATE_DIR = _anchored("STATE_DIR", PROJECT_DIR / "content" / "pipeline-state")
_ANALYTICS_DIR = _anchored("ANALYTICS_DIR", PROJECT_DIR / "content" / "analytics")
COST_LOG = _STATE_DIR / "cost_log.jsonl"
OUT_PATH = _ANALYTICS_DIR / "sprint" / "session_metrics.jsonl"
SESSION_ID_RE = re.compile(r"Session-Id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
ATTRIBUTION_GRACE = timedelta(minutes=30)


def _run(cmd: list, timeout: int = 60) -> str:
    # 失敗は stderr に明示する（サイレント縮退の禁止・Issue #133）。クラウドでは gh が
    # 403 でブロックされるため（L-114）、空文字＝「0 件」ではなく「取得失敗」がありうる。
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if res.returncode != 0:
            print(f"[warn] {' '.join(cmd[:4])}... failed: {res.stderr.strip()[:160]}"
                  "（クラウドの gh は 403・L-114。メトリクスは欠損扱い）", file=sys.stderr)
            return ""
        return res.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[warn] {' '.join(cmd[:4])}... error: {e}", file=sys.stderr)
        return ""


def _parse_ts(value: str | int | float | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────
# cost_log のセッション別集約
# ──────────────────────────────────────────

def load_sessions() -> dict:
    """cost_log.jsonl を全行読み込み、セッション ID 別に集約する。

    期間フィルタは呼び出し側（collect_sessions）が `last_ts >= since` で行う。
    行単位で since フィルタすると期間境界をまたぐセッションのトークンが切り捨てられるため、ここでは行わない。
    """
    sessions: dict = {}
    if not COST_LOG.exists():
        return sessions
    try:
        with COST_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                sid = row.get("session_id")
                ts = _parse_ts(row.get("timestamp") or row.get("ts"))
                if not sid or ts is None:
                    continue
                s = sessions.setdefault(sid, {
                    "session_id": sid, "segments": 0, "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
                    "first_ts": ts, "last_ts": ts, "model": row.get("model", ""),
                })
                s["segments"] += 1
                # cost_log の同一セッション行は累積スナップショット（Stop のたびに増える）。
                # 行を += すると prefix-sum の過大計上になるため、最新行の値を採用する（#244）
                if ts >= s["last_ts"]:
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                        if isinstance(row.get(k), (int, float)):
                            s[k] = int(row[k])
                    if isinstance(row.get("cost_usd"), (int, float)):
                        s["cost_usd"] = float(row["cost_usd"])
                    if row.get("model"):
                        s["model"] = row.get("model", "")
                if ts < s["first_ts"]:
                    s["first_ts"] = ts
                if ts > s["last_ts"]:
                    s["last_ts"] = ts
    except OSError:
        return sessions
    return sessions


def cmd_current() -> int:
    """現セッション（CLAUDE_CODE_SESSION_ID）のトークン消費を表示する。"""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        print("CLAUDE_CODE_SESSION_ID が未設定（クラウドセッション外）")
        return 1
    s = load_sessions().get(sid)
    if not s:
        print(f"session {sid[:8]}…: cost_log に記録なし（Stop フック未発火・進行中セグメントは未計上）")
        return 0
    print(f"session {sid[:8]}…（{s['segments']} セグメント記録済み・進行中分は未計上）")
    print(f"  入力: {s['input_tokens']:,} tok / 出力: {s['output_tokens']:,} tok")
    print(f"  cache read: {s['cache_read_tokens']:,} tok / cache write: {s['cache_write_tokens']:,} tok")
    print(f"  コスト: ${s['cost_usd']:.2f} / モデル: {s['model']}")
    return 0


# ──────────────────────────────────────────
# マージ済み PR の取得と SP 突合
# ──────────────────────────────────────────

def _sp_from(label_nodes: list | None) -> int:
    if not label_nodes or not isinstance(label_nodes, list):
        return 0
    for l in label_nodes:
        if not l or not isinstance(l, dict):
            continue
        name = l.get("name", "")
        if name.startswith("sp:"):
            try:
                v = int(name.split(":", 1)[1])
                if v in SP_VALUES:
                    return v
            except ValueError:
                continue
    return 0


def fetch_period_prs(since: datetime) -> list:
    """期間内にマージされた PR（body 含む）を取得する。

    UPDATED_AT 降順でページネーションし、updatedAt < since のノードに達したら打ち切る
    （mergedAt <= updatedAt のため取りこぼしなし。1 ページ 100 件超の繁忙日も対応）。
    """
    result = []
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = ('{ repository(owner:"%s", name:"%s") { '
                 'pullRequests(states: MERGED, first: 100%s, orderBy:{field:UPDATED_AT, direction:DESC}) { '
                 'pageInfo { hasNextPage endCursor } '
                 'nodes { number title body mergedAt createdAt updatedAt labels(first:30){nodes{name}} '
                 'closingIssuesReferences(first:10){ nodes { number labels(first:30){ nodes { name } } } } '
                 '} } } }' % (OWNER, NAME, after))
        out = _run(["gh", "api", "graphql", "-f", f"query={query}"], timeout=60)
        if not out:
            break
        try:
            res = json.loads(out)
            conn = (((res.get("data") or {}).get("repository") or {}).get("pullRequests") or {})
            nodes = conn.get("nodes") or []
        except Exception:
            break
        reached_old = False
        for pr in nodes:
            if not pr or not isinstance(pr, dict):
                continue
            updated_at = _parse_ts(pr.get("updatedAt"))
            if updated_at is not None and updated_at < since:
                reached_old = True
                break
            merged_at = _parse_ts(pr.get("mergedAt"))
            if merged_at is None or merged_at < since:
                continue
            pr["_merged_at"] = merged_at
            result.append(pr)
        page = conn.get("pageInfo") or {}
        if reached_old or not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break  # API 異常時の無限ループ防止
    return result


def collect_sessions(hours: int = 24) -> dict:
    """期間内のセッション別ベロシティを算出する。戻り値: {sessions: [...], unattributed: {...}}"""
    since = datetime.now(JST) - timedelta(hours=hours)
    # 期間内に活動があったセッションのみ対象（期間境界をまたぐセッションも全行集約済み）
    sessions = {sid: s for sid, s in load_sessions().items() if s["last_ts"] >= since}
    prs = fetch_period_prs(since)

    counted_issues: set = set()
    per_session = defaultdict(lambda: {"done_sp": 0, "prs": [], "lead_min": 0.0})
    unattributed = {"done_sp": 0, "prs": []}

    for pr in prs:
        pr_sp = 0
        closing = pr.get("closingIssuesReferences") or {}
        for iss in (closing.get("nodes") or []) if isinstance(closing, dict) else []:
            if not iss or not isinstance(iss, dict):
                continue
            num = iss.get("number")
            if num in counted_issues:
                continue
            iss_labels = iss.get("labels") or {}
            sp = _sp_from(iss_labels.get("nodes") if isinstance(iss_labels, dict) else None)
            if sp:
                counted_issues.add(num)
                pr_sp += sp
        if not pr_sp:
            pr_labels = pr.get("labels") or {}
            pr_sp = _sp_from(pr_labels.get("nodes") if isinstance(pr_labels, dict) else None)
        if not pr_sp:
            continue  # 日次正値は sp ラベルのみ（タイトル推定はバックフィル専用）

        # 紐付け①: Session-Id トレーラー（明示記載があれば時間窓フォールバックは行わない＝誤帰属防止）
        sid = None
        attribution = None
        m = SESSION_ID_RE.search(pr.get("body") or "")
        if m:
            parsed_sid = m.group(1).lower()
            if parsed_sid in sessions:
                sid = parsed_sid
                attribution = "trailer"
            # ローカル cost_log に無いセッション（他コンテナ）→ 帰属不明のまま（誤帰属させない）
        else:
            # 紐付け②: 時間窓フォールバック（唯一のセッションに入る場合のみ）
            merged_at = pr["_merged_at"]
            hits = [s for s in sessions.values()
                    if s["first_ts"] - ATTRIBUTION_GRACE <= merged_at <= s["last_ts"] + ATTRIBUTION_GRACE]
            if len(hits) == 1:
                sid = hits[0]["session_id"]
                attribution = "time"

        created_at = _parse_ts(pr.get("createdAt"))
        lead = ((pr["_merged_at"] - created_at).total_seconds() / 60.0) if created_at else 0.0
        entry = {"pr": pr.get("number"), "sp": pr_sp, "attribution": attribution}
        if sid:
            per_session[sid]["done_sp"] += pr_sp
            per_session[sid]["prs"].append(entry)
            per_session[sid]["lead_min"] += lead
        else:
            unattributed["done_sp"] += pr_sp
            unattributed["prs"].append(entry)

    results = []
    for sid, s in sessions.items():
        agg = per_session.get(sid, {"done_sp": 0, "prs": [], "lead_min": 0.0})
        tokens = s["input_tokens"] + s["output_tokens"]
        duration_min = (s["last_ts"] - s["first_ts"]).total_seconds() / 60.0
        rec = {
            "session_id": sid,
            "first_ts": s["first_ts"].isoformat(),
            "last_ts": s["last_ts"].isoformat(),
            "duration_min": round(duration_min, 1),
            "segments": s["segments"],
            "input_tokens": s["input_tokens"],
            "output_tokens": s["output_tokens"],
            "cache_read_tokens": s["cache_read_tokens"],
            "cache_write_tokens": s["cache_write_tokens"],
            "cost_usd": round(s["cost_usd"], 4),
            "done_sp": agg["done_sp"],
            "prs": agg["prs"],
        }
        if agg["done_sp"]:
            rec["tokens_per_sp"] = round(tokens / agg["done_sp"])
            rec["cost_per_sp"] = round(s["cost_usd"] / agg["done_sp"], 3)
            # 2 指標を明示分離: PR リードタイム基準（レビュー待ち含む） / セッション稼働時間基準
            rec["lead_min_per_sp"] = round(agg["lead_min"] / agg["done_sp"], 1)
            rec["active_min_per_sp"] = round(duration_min / agg["done_sp"], 1)
        results.append(rec)
    results.sort(key=lambda r: r["first_ts"])
    return {"sessions": results, "unattributed": unattributed,
            "period_hours": hours, "generated_at": datetime.now(JST).isoformat()}


def persist(data: dict) -> None:
    """セッション別メトリクスを永続化する（cost_log は揮発するため・週次較正のデータ源）。"""
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a", encoding="utf-8") as f:
        for rec in data["sessions"]:
            entry = {"recorded_at": data["generated_at"], "period_hours": data["period_hours"]}
            entry.update(rec)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def slack_lines(data: dict, limit: int = 6) -> list:
    """half_day_summary 埋め込み用のセッション別 1 行サマリー。"""
    lines = []
    for r in data["sessions"][:limit]:
        sp = r["done_sp"]
        tok = r["input_tokens"] + r["output_tokens"]
        base = (f"  • `{r['session_id'][:8]}` sp {sp} / {tok/1000:.0f}K tok / "
                f"${r['cost_usd']:.1f} / 稼働 {r['duration_min']:.0f} 分")
        if sp:
            base += f" → {r['tokens_per_sp']/1000:.0f}K tok/sp・${r['cost_per_sp']:.1f}/sp"
        lines.append(base)
    if data["unattributed"]["done_sp"]:
        lines.append(f"  • （セッション帰属不明: {data['unattributed']['done_sp']} sp・"
                     f"PR 本文の Session-Id 記載漏れ）")
    return lines


def cmd_collect(hours: int, do_persist: bool, as_slack: bool) -> int:
    data = collect_sessions(hours=hours)
    if as_slack:
        for line in slack_lines(data):
            print(line)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    if do_persist:
        persist(data)
        print(f"📝 永続化: {OUT_PATH}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("current", help="現セッションのトークン消費を表示")
    col = sub.add_parser("collect", help="期間内のセッション別メトリクスを算出")
    col.add_argument("--hours", type=int, default=24)
    col.add_argument("--persist", action="store_true")
    col.add_argument("--slack-lines", action="store_true")
    args = ap.parse_args()
    if args.cmd == "current":
        return cmd_current()
    return cmd_collect(args.hours, args.persist, args.slack_lines)


if __name__ == "__main__":
    sys.exit(main())
