#!/usr/bin/env python3
"""リポジトリ衛生スナップショットと実行ログを永続化する（CP-3 / Issue #2080）。

自律運用の「放置が累積しても誰も気づけない」構造（L-086）を解消するための
可観測性ツール。オープン Issue / PR を gh CLI で取得し、以下を出力する。

1. 衛生スナップショット: content/pipeline-state/snapshots/health_YYYY-MM-DD.json
   - status / type / phase ラベル別の件数
   - waiting-claude の最古滞留日数
   - カテゴリ別バックログ（phase:2-research / type:retro-try / type:improvement / type:bug）
   - オープン PR 数・Orphan PR（24h 超）一覧
2. 実行ログ追記: content/pipeline-state/run_log.jsonl
   - いつ・どのスロットが・何を処理したか（後追い可能にする）
3. 滞留アラート: 閾値超過時に標準出力 + （--slack 指定時）Slack 通知

設計方針:
- gh CLI の失敗を「結果0件」と誤認しない（GhCommandError を送出して終了コード非0）。
  これは L-074 / L-086 の再発防止であり、本ツールの存在意義そのもの。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

GH_REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
JST = timezone(timedelta(hours=9))

REPO_ROOT = Path(__file__).resolve().parent.parent
# 出力先は STATE_DIR 環境変数で上書き可能（既定の content/ は制作系の慣習のため）。
# 相対パスは CWD 依存で出力が迷子になるのを防ぐため REPO_ROOT 基準に正規化（+ ~ 展開）する。
def _anchored_state_dir() -> Path:
    raw = os.environ.get("STATE_DIR")
    if not raw:
        return REPO_ROOT / "content" / "pipeline-state"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


STATE_DIR = _anchored_state_dir()
SNAPSHOT_DIR = STATE_DIR / "snapshots"
RUN_LOG = STATE_DIR / "run_log.jsonl"

# 滞留アラート閾値
WAITING_CLAUDE_ALERT = 50      # waiting-claude がこの件数を超えたら警告
STALE_DAYS_ALERT = 7           # waiting-claude の最古滞留がこの日数を超えたら警告
ORPHAN_PR_HOURS = 24           # PR がこの時間更新されていなければ Orphan 判定

# バックログとして個別追跡するラベル
BACKLOG_LABELS = [
    "phase:2-research",
    "type:retro-try",
    "type:improvement",
    "type:bug",
]


class GhCommandError(Exception):
    """gh CLI の実行失敗を示す例外（データなしでの誤報防止用）。"""


def run_gh_command(args: list[str]) -> str:
    """gh コマンドを実行して stdout を返す。失敗時は GhCommandError を送出する。"""
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise GhCommandError(f"gh command timed out [cmd: gh {' '.join(args)}]")
    except FileNotFoundError:
        raise GhCommandError("gh CLI not found")
    except OSError as e:
        raise GhCommandError(f"gh command execution failed: {e}")
    if result.returncode != 0:
        raise GhCommandError(
            f"gh command failed (exit code {result.returncode}): "
            f"{result.stderr.strip() or 'no stderr output'} "
            f"[cmd: gh {' '.join(args)}]"
        )
    return result.stdout


def _gh_json(args: list[str]) -> list:
    raw = run_gh_command(args)
    if not raw.strip():
        return []
    return json.loads(raw)


def fetch_open_issues() -> list[dict]:
    return _gh_json([
        "issue", "list", "-R", GH_REPO, "--state", "open", "--limit", "1000",
        "--json", "number,title,labels,updatedAt,createdAt",
    ])


def fetch_open_prs() -> list[dict]:
    return _gh_json([
        "pr", "list", "-R", GH_REPO, "--state", "open", "--limit", "1000",
        "--json", "number,title,updatedAt,createdAt,isDraft",
    ])


def _label_names(item: dict) -> list[str]:
    return [lbl["name"] for lbl in item.get("labels", [])]


def _parse_dt(value: str) -> datetime:
    """ISO8601（末尾 Z）を aware datetime にパースする。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _age_days(value: str, now: datetime) -> float:
    return round((now - _parse_dt(value)).total_seconds() / 86400, 1)


def _age_hours(value: str, now: datetime) -> float:
    return round((now - _parse_dt(value)).total_seconds() / 3600, 1)


def compute_metrics(issues: list[dict], prs: list[dict], now: datetime) -> dict:
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}

    for issue in issues:
        for name in _label_names(issue):
            if name.startswith("status:"):
                status_counts[name] = status_counts.get(name, 0) + 1
            elif name.startswith("type:"):
                type_counts[name] = type_counts.get(name, 0) + 1
            elif name.startswith("phase:"):
                phase_counts[name] = phase_counts.get(name, 0) + 1

    # waiting-claude の最古滞留
    waiting_claude = [
        i for i in issues if "status:waiting-claude" in _label_names(i)
    ]
    oldest_waiting = None
    if waiting_claude:
        oldest = max(waiting_claude, key=lambda i: _age_days(i["updatedAt"], now))
        oldest_waiting = {
            "number": oldest["number"],
            "title": oldest["title"][:60],
            "updatedAt": oldest["updatedAt"],
            "stale_days": _age_days(oldest["updatedAt"], now),
        }

    # カテゴリ別バックログ（waiting-claude に限定して「未消化」を測る）
    backlog = {}
    for label in BACKLOG_LABELS:
        members = [
            i for i in waiting_claude if label in _label_names(i)
        ]
        if members:
            oldest_member = max(members, key=lambda i: _age_days(i["updatedAt"], now))
            backlog[label] = {
                "count": len(members),
                "oldest_number": oldest_member["number"],
                "oldest_stale_days": _age_days(oldest_member["updatedAt"], now),
            }
        else:
            backlog[label] = {"count": 0, "oldest_number": None, "oldest_stale_days": 0}

    # Orphan PR（24h 超未更新・ドラフト除く）
    orphan_prs = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        hours = _age_hours(pr["updatedAt"], now)
        if hours >= ORPHAN_PR_HOURS:
            orphan_prs.append({
                "number": pr["number"],
                "title": pr["title"][:60],
                "stale_hours": hours,
            })

    return {
        "generated_at": now.isoformat(),
        "total_open_issues": len(issues),
        "status_counts": dict(sorted(status_counts.items(), key=lambda kv: -kv[1])),
        "type_counts": dict(sorted(type_counts.items(), key=lambda kv: -kv[1])),
        "phase_counts": dict(sorted(phase_counts.items(), key=lambda kv: -kv[1])),
        "waiting_claude_count": len(waiting_claude),
        "oldest_waiting_claude": oldest_waiting,
        "backlog": backlog,
        "open_pr_count": len(prs),
        "orphan_pr_count": len(orphan_prs),
        "orphan_prs": orphan_prs,
    }


def evaluate_alerts(metrics: dict) -> list[str]:
    alerts: list[str] = []
    wc = metrics["waiting_claude_count"]
    if wc > WAITING_CLAUDE_ALERT:
        alerts.append(
            f"waiting-claude が {wc} 件（閾値 {WAITING_CLAUDE_ALERT} 超）— 消化が追いついていない"
        )
    oldest = metrics.get("oldest_waiting_claude")
    if oldest and oldest["stale_days"] > STALE_DAYS_ALERT:
        alerts.append(
            f"最古の waiting-claude #{oldest['number']} が {oldest['stale_days']} 日放置"
            f"（閾値 {STALE_DAYS_ALERT} 日超）"
        )
    for label, info in metrics["backlog"].items():
        if info["count"] > WAITING_CLAUDE_ALERT:
            alerts.append(
                f"{label} のバックログが {info['count']} 件（閾値 {WAITING_CLAUDE_ALERT} 超）"
            )
    if metrics["orphan_pr_count"] > 0:
        nums = ", ".join(f"#{p['number']}" for p in metrics["orphan_prs"])
        alerts.append(f"Orphan PR {metrics['orphan_pr_count']} 件（{ORPHAN_PR_HOURS}h 超未更新）: {nums}")
    return alerts


def write_snapshot(metrics: dict, date_str: str) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"health_{date_str}.json"
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def append_run_log(slot: str, result: str, metrics: dict, alerts: list[str], now: datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": now.isoformat(),
        "slot": slot,
        "result": result or "hygiene-snapshot",
        "waiting_claude": metrics["waiting_claude_count"],
        "open_pr": metrics["open_pr_count"],
        "orphan_pr": metrics["orphan_pr_count"],
        "alerts": len(alerts),
    }
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def maybe_send_slack(alerts: list[str], now: datetime) -> None:
    if not alerts:
        return
    text = "🧹 リポジトリ衛生アラート（" + now.strftime("%Y-%m-%d %H:%M JST") + "）\n" + "\n".join(
        f"- {a}" for a in alerts
    )
    try:
        subprocess.run(
            ["python3", str(REPO_ROOT / "tools" / "slack_notify.py"), "message", "--text", text],
            timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[slack 通知スキップ] {e}", file=sys.stderr)


def format_report(metrics: dict, alerts: list[str]) -> str:
    lines = ["リポジトリ衛生レポート:"]
    lines.append(f"- オープン Issue 数: {metrics['total_open_issues']} 件")
    for label, count in metrics["status_counts"].items():
        lines.append(f"  - {label}: {count} 件")
    lines.append(f"- オープン PR 数: {metrics['open_pr_count']} 件（うち Orphan: {metrics['orphan_pr_count']} 件）")
    oldest = metrics.get("oldest_waiting_claude")
    if oldest:
        lines.append(
            f"- 最古 waiting-claude: #{oldest['number']} ({oldest['stale_days']} 日) {oldest['title']}"
        )
    lines.append("- カテゴリ別バックログ（waiting-claude 内）:")
    for label, info in metrics["backlog"].items():
        lines.append(f"  - {label}: {info['count']} 件（最古 {info['oldest_stale_days']} 日）")
    if alerts:
        lines.append("🔴 アラート:")
        lines.extend(f"  - {a}" for a in alerts)
    else:
        lines.append("✅ 閾値超過なし")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="リポジトリ衛生スナップショット・実行ログ永続化")
    parser.add_argument("--slot", default="manual", help="呼び出し元スロット名（例: 07:00-project-sync）")
    parser.add_argument("--result", default="", help="run_log に記録する処理結果サマリ")
    parser.add_argument("--slack", action="store_true", help="アラート時に Slack へ通知する")
    parser.add_argument("--print-only", action="store_true", help="ファイルへ書き込まず標準出力のみ")
    args = parser.parse_args()

    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")

    try:
        issues = fetch_open_issues()
        prs = fetch_open_prs()
    except GhCommandError as e:
        # 失敗を「0件」と誤報しないため、ここで明示的に異常終了する（L-074 / L-086）
        print(f"[ERROR] gh 取得に失敗したため衛生スナップショットを中止: {e}", file=sys.stderr)
        return 1

    metrics = compute_metrics(issues, prs, now)
    alerts = evaluate_alerts(metrics)

    if not args.print_only:
        snapshot_path = write_snapshot(metrics, date_str)
        append_run_log(args.slot, args.result, metrics, alerts, now)
        print(f"[OK] スナップショット保存: {snapshot_path.relative_to(REPO_ROOT)}")
        print(f"[OK] 実行ログ追記: {RUN_LOG.relative_to(REPO_ROOT)}")

    print(format_report(metrics, alerts))

    if args.slack:
        maybe_send_slack(alerts, now)

    return 0


if __name__ == "__main__":
    sys.exit(main())
