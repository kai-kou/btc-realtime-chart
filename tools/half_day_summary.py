#!/usr/bin/env python3
"""
半日アウトカムサマリー生成スクリプト（Issue #2597）

セッション単位の Slack 通知（session-start / session-stop）を廃止した代替として、
「半日〜1日に1回」ユーザーがワークフローの自律稼働とPR消化を把握するための
アウトカム集約サマリーを生成・送信する。

設計原則:
  - 直近約12時間の「アウトカム（フロー）」を集約する（PR消化・パイプライン進捗・SNS・稼働）
  - 各データソースは独立 try/except。1つ失敗しても他を出す
  - git log は必ず取得できるため、最低限のサマリーは常に送れる（＝生存確認＝ハートビート）
  - 要対応ゼロでも必ず1通送る（半日サマリーが届かない＝スケジュールタスク自体の異常、と検知できる）
  - 要対応は triage_notification.py で A-1〜A-6（既約境界外）のみ @mention

使い方:
  python3 tools/half_day_summary.py                       # サマリーを標準出力
  python3 tools/half_day_summary.py --slack               # Slack に半日サマリーを送信
  python3 tools/half_day_summary.py --slack --period morning   # 朝枠（前日19:00〜本日07:00）
  python3 tools/half_day_summary.py --slack --period evening   # 夜枠（本日07:00〜19:00）
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
PROJECT_DIR = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))


def _run(cmd: list, timeout: int = 20) -> str:
    """コマンドを実行して stdout を返す。失敗時は空文字（呼び出し側でフォールバック）。

    失敗は stderr に明示する（サイレント縮退の禁止・Issue #133）。クラウドでは gh が
    403 でブロックされるため（L-114）、空文字＝「0 件」ではなく「取得失敗（欠損）」がありうる。
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        hint = "（クラウドの gh は 403・L-114。該当集計は欠損扱い）" if cmd[:1] == ["gh"] else "（該当集計は欠損扱い）"
        print(f"[warn] {' '.join(cmd[:4])}... failed: {result.stderr.strip()[:160]}" + hint, file=sys.stderr)
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        print(f"[warn] {' '.join(cmd[:4])}... error: {e}", file=sys.stderr)
    return ""


# ──────────────────────────────────────────
# データソース別の収集（各々独立・失敗しても None/空を返す）
# ──────────────────────────────────────────

def collect_commits(hours: int) -> dict:
    """直近 N 時間の git コミットを分類して件数・主要コミットを返す（ローカル完結・最も確実）。"""
    out = _run(["git", "-C", str(PROJECT_DIR), "log", "main", f"--since={hours} hours ago",
                "--pretty=format:%s"], timeout=15)
    if not out:
        # main ローカルが古い/無い場合は現ブランチで再試行
        out = _run(["git", "-C", str(PROJECT_DIR), "log", f"--since={hours} hours ago",
                    "--pretty=format:%s"], timeout=15)
    lines = [l for l in out.splitlines() if l.strip()]
    cats = {"video": 0, "sns": 0, "fix": 0, "ops": 0, "other": 0}
    for s in lines:
        low = s.lower()
        if s.startswith("[V") or any(p in low for p in ("] script:", "] audio:", "] image:",
                                                          "] video:", "] render:", "script:", "audio:")):
            cats["video"] += 1
        elif s.startswith("[sns]") or "[sns]" in low or "organic:" in low:
            cats["sns"] += 1
        elif any(s.startswith(p) for p in ("fix:", "feat:", "improvement:", "refactor:", "docs:")):
            cats["fix"] += 1
        elif s.startswith("[daily]") or s.startswith("[refinement]") or s.startswith("chore"):
            cats["ops"] += 1
        else:
            cats["other"] += 1
    return {"total": len(lines), "cats": cats}


def collect_prs(since: datetime) -> dict:
    """直近のマージ済みPR・オープンPR数を gh で取得（gh 不在時は空）。"""
    merged = []
    out = _run(["gh", "pr", "list", "-R", REPO, "--state", "merged",
                "--json", "number,title,mergedAt", "--limit", "1000"])
    if out:
        try:
            for pr in json.loads(out):
                ts = pr.get("mergedAt", "")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt >= since:
                    merged.append({"number": pr.get("number"), "title": pr.get("title", "")})
        except json.JSONDecodeError:
            pass

    open_count = None
    open_titles = []
    out = _run(["gh", "pr", "list", "-R", REPO, "--state", "open",
                "--json", "number,title", "--limit", "1000"])
    if out:
        try:
            data = json.loads(out)
            open_count = len(data)
            open_titles = [{"number": p.get("number"), "title": p.get("title", "")} for p in data[:5]]
        except json.JSONDecodeError:
            pass
    return {"merged": merged, "open_count": open_count, "open_titles": open_titles}


def collect_cost(since: datetime) -> dict:
    """cost_log.jsonl から直近のセッション数を集計（揮発 state file・ベストエフォート）。"""
    log = PROJECT_DIR / "content" / "pipeline-state" / "cost_log.jsonl"
    if not log.exists():
        return {}
    sessions = 0
    cost = 0.0
    has_cost = False
    per_sid: dict = {}  # session_id -> (ts, cost) 最新スナップショット（#244 重複排除）
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
            except json.JSONDecodeError:
                continue
            # タイムスタンプフィールドを複数候補で探す
            ts = ""
            for k in ("timestamp", "ts", "datetime", "date", "ended_at"):
                if row.get(k):
                    ts = str(row[k])
                    break
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=JST)
                    if dt < since:
                        continue
                except ValueError:
                    pass  # パース不可なら期間判定せずカウント（ベストエフォート）
            row_cost = None
            for k in ("cost_usd", "cost", "total_cost", "total_cost_usd"):
                if isinstance(row.get(k), (int, float)):
                    row_cost = float(row[k])
                    break
            # 同一セッションの累積スナップショット行は最新行（ts 最大）に畳む（#244）。
            # session_id 欠落行は従来どおり 1 行 = 1 セッション扱い。
            sid = row.get("session_id")
            if sid:
                cur = per_sid.get(sid)
                if cur is None or ts >= cur[0]:
                    per_sid[sid] = (ts, row_cost)
            else:
                sessions += 1
                if row_cost is not None:
                    cost += row_cost
                    has_cost = True
    except OSError:
        return {}
    sessions += len(per_sid)
    for _ts, row_cost in per_sid.values():
        if row_cost is not None:
            cost += row_cost
            has_cost = True
    out = {"sessions": sessions}
    if has_cost:
        out["cost"] = cost
    return out


def collect_sns(since: datetime) -> dict:
    """SNS オーガニック投稿の直近件数を backlog.jsonl から集計（ベストエフォート）。"""
    log = PROJECT_DIR / "content" / "sns" / "initiatives" / "backlog.jsonl"
    if not log.exists():
        return {}
    counts = {}
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
            except json.JSONDecodeError:
                continue
            if row.get("status") != "posted":
                continue
            ts = ""
            for k in ("posted_at", "updated_at", "posted_time"):
                if row.get(k):
                    ts = str(row[k])
                    break
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=JST)
            except ValueError:
                continue
            if dt < since:
                continue
            plat = row.get("platform", "?")
            counts[plat] = counts.get(plat, 0) + 1
    except OSError:
        return {}
    return counts


def collect_action_items() -> list:
    """要対応 Issue を取得し triage で A-1〜A-6（既約境界外）のみ抽出。"""
    items = []
    for label in ("status:waiting-user", "status:blocked"):
        out = _run(["gh", "issue", "list", "-R", REPO, "--label", label,
                    "--state", "open", "--json", "number,title,labels", "--limit", "1000"])
        if not out:
            continue
        try:
            for iss in json.loads(out):
                labels = [l.get("name", "") for l in iss.get("labels", [])]
                items.append({"number": iss.get("number"), "title": iss.get("title", ""), "labels": labels})
        except json.JSONDecodeError:
            continue

    # triage で A 区分のみ残す
    a_items = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from triage_notification import classify_item  # type: ignore
        for it in items:
            res = classify_item(it["title"], it["labels"])
            if res.get("mention"):
                a_items.append(it)
    except ImportError:
        # triage 不在時はフェイルセーフで「全て自律処理（@mention しない）」扱い
        a_items = []
    return a_items


def collect_inflight() -> dict:
    """制作中の動画（type:content オープン Issue）の件数を取得（旧 daily-progress の制作中スナップショットを吸収）。"""
    out = _run(["gh", "issue", "list", "-R", REPO, "--label", "type:content",
                "--state", "open", "--json", "number", "--limit", "1000"])
    if out:
        try:
            return {"inflight": len(json.loads(out))}
        except json.JSONDecodeError:
            pass
    return {}


def _sum_tokens(since: datetime) -> int:
    """cost_log.jsonl から期間内の入出力トークン合計を返す（揮発 state file・ベストエフォート）。

    timestamp が欠落・パース不能な行は期間判定できないため集計から除外する。
    """
    log = PROJECT_DIR / "content" / "pipeline-state" / "cost_log.jsonl"
    if not log.exists():
        return 0
    total = 0
    per_sid: dict = {}  # session_id -> (ts, tokens) 最新スナップショット（#244 重複排除）
    try:
        with log.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                except json.JSONDecodeError:
                    continue
                ts = ""
                for k in ("timestamp", "ts", "datetime", "date", "ended_at"):
                    if row.get(k):
                        ts = str(row[k])
                        break
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=JST)
                    if dt < since:
                        continue
                except (ValueError, TypeError):
                    continue
                row_tokens = sum(int(row[k]) for k in ("input_tokens", "output_tokens")
                                 if isinstance(row.get(k), (int, float)))
                # 同一セッションの累積スナップショット行は最新行（ts 最大）に畳む（#244）。
                # session_id 欠落行は従来どおり行単位で加算する。
                sid = row.get("session_id")
                if sid:
                    cur = per_sid.get(sid)
                    if cur is None or ts >= cur[0]:
                        per_sid[sid] = (ts, row_tokens)
                else:
                    total += row_tokens
    except OSError:
        return 0
    return total + sum(tok for _ts, tok in per_sid.values())


def collect_sprint(since: datetime, cost: dict) -> dict:
    """日次スプリントメトリクス（1セッション=1スプリント・session-sprint-rules.md §5）。

    マージ済み PR ↔ 紐づく Issue（closingIssuesReferences）の sp:N ラベルを GraphQL で突合し、
    done_sp（ベロシティ）・SP あたり処理時間（PR リードタイム基準）・SP あたりコスト/トークンを返す。
    Issue に sp が無い場合は PR 自身の sp ラベルにフォールバックする。
    """
    owner, name = REPO.split("/", 1)
    # last:100 は GraphQL の 1 ページ上限。日次窓のマージ数（実測 ~20/日）に対して十分な余裕
    query = ('{ repository(owner:"%s", name:"%s") { '
             'pullRequests(states: MERGED, last: 100, orderBy:{field:UPDATED_AT, direction:ASC}) { nodes { '
             'number mergedAt createdAt labels(first:30){nodes{name}} '
             'closingIssuesReferences(first:10){ nodes { number labels(first:30){ nodes { name } } } } '
             '} } } }' % (owner, name))
    out = _run(["gh", "api", "graphql", "-f", f"query={query}"], timeout=30)
    if not out:
        return {}

    def _sp_from(label_nodes) -> int:
        if not label_nodes or not isinstance(label_nodes, list):
            return 0
        for l in label_nodes:
            if not l or not isinstance(l, dict):
                continue
            name = l.get("name", "")
            if name.startswith("sp:"):
                try:
                    return int(name.split(":", 1)[1])
                except ValueError:
                    continue
        return 0

    done_sp = 0
    sp_prs = 0
    total_prs = 0
    lead_minutes = 0.0
    counted_issues = set()
    try:
        res = json.loads(out)
        data = res.get("data") or {}
        repo = data.get("repository") or {}
        prs = repo.get("pullRequests") or {}
        nodes = prs.get("nodes") or []
    except Exception:
        return {}
    for pr in nodes:
        if not pr or not isinstance(pr, dict):
            continue
        try:
            merged_at = datetime.fromisoformat(str(pr.get("mergedAt", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if merged_at < since:
            continue
        total_prs += 1
        pr_sp = 0
        closing = pr.get("closingIssuesReferences") or {}
        iss_nodes = closing.get("nodes") if isinstance(closing, dict) else None
        for iss in iss_nodes or []:
            if not iss or not isinstance(iss, dict):
                continue
            num = iss.get("number")
            if num in counted_issues:
                continue  # 複数 PR が同一 Issue を閉じる場合の二重計上防止
            iss_labels = iss.get("labels") or {}
            sp = _sp_from(iss_labels.get("nodes") if isinstance(iss_labels, dict) else None)
            if sp:
                counted_issues.add(num)
                pr_sp += sp
        if not pr_sp:
            pr_labels = pr.get("labels") or {}
            pr_sp = _sp_from(pr_labels.get("nodes") if isinstance(pr_labels, dict) else None)
        if not pr_sp:
            continue
        done_sp += pr_sp
        sp_prs += 1
        try:
            created_at = datetime.fromisoformat(str(pr.get("createdAt", "")).replace("Z", "+00:00"))
            lead_minutes += (merged_at - created_at).total_seconds() / 60.0
        except ValueError:
            pass

    metrics = {"done_sp": done_sp, "sp_prs": sp_prs, "total_prs": total_prs,
               "sessions": cost.get("sessions", 0)}
    if done_sp:
        metrics["min_per_sp"] = lead_minutes / done_sp
        if "cost" in cost:
            metrics["cost_per_sp"] = cost["cost"] / done_sp
        tokens = _sum_tokens(since)
        if tokens:
            metrics["tokens_per_sp"] = tokens / done_sp
        if metrics["sessions"]:
            metrics["sp_per_session"] = done_sp / metrics["sessions"]

    # no-op スプリント（run_log.jsonl の result:"no-op"・session-sprint-rules.md §5）
    noop = 0
    run_log = PROJECT_DIR / "content" / "pipeline-state" / "run_log.jsonl"
    if run_log.exists():
        try:
            with run_log.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line.strip())
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if not isinstance(row, dict) or row.get("result") != "no-op":
                        continue
                    try:
                        dt = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=JST)
                        if dt >= since:
                            noop += 1
                    except (ValueError, TypeError):
                        continue
        except OSError:
            pass
    metrics["noop"] = noop
    return metrics


def persist_sprint_log(metrics: dict, period_label: str) -> None:
    """スプリントメトリクスを永続化（cost_log.jsonl は揮発するため・週次較正のデータ源）。"""
    if not metrics:
        return
    out_dir = PROJECT_DIR / "content" / "analytics" / "sprint"
    out_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(JST).isoformat(), "period": period_label}
    entry.update({k: (round(v, 3) if isinstance(v, float) else v) for k, v in metrics.items()})
    with (out_dir / "sprint_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ──────────────────────────────────────────
# 本文組み立て
# ──────────────────────────────────────────

def build_summary_body(commits: dict, prs: dict, cost: dict, sns: dict, inflight: dict,
                       sprint: dict | None = None, session_lines: list | None = None) -> str:
    lines = []

    # PR 消化（ユーザー最重要関心：レビューしない代わりに「何件消化したか」を見る）
    merged = prs.get("merged", [])
    pr_lines = [f"*📋 PR消化*: マージ {len(merged)}件"]
    for pr in merged[:6]:
        pr_lines.append(f"  ✅ #{pr['number']} {pr['title'][:54]}")
    if prs.get("open_count") is not None:
        pr_lines.append(f"  ⏳ オープン {prs['open_count']}件" + (
            "（レビュー待ち）" if prs["open_count"] else "（なし＝消化済み）"))
    lines.append("\n".join(pr_lines))

    # パイプライン進捗（コミット分類）
    c = commits.get("cats", {})
    parts = []
    if c.get("video"):
        parts.append(f"🎬 動画制作 {c['video']}")
    if c.get("sns"):
        parts.append(f"📣 SNS {c['sns']}")
    if c.get("fix"):
        parts.append(f"🔧 改善/修正 {c['fix']}")
    if c.get("ops"):
        parts.append(f"🗂 運用 {c['ops']}")
    if c.get("other"):
        parts.append(f"・その他 {c['other']}")
    commit_line = f"*🛠 コミット*: 計 {commits.get('total', 0)}件"
    if parts:
        commit_line += "（" + " / ".join(parts) + "）"
    lines.append(commit_line)

    # SNS 投稿
    if sns:
        sns_str = " / ".join(f"{k} {v}件" for k, v in sns.items())
        lines.append(f"*📣 SNS投稿*: {sns_str}")

    # 制作中バックログ（旧 daily-progress の制作中スナップショットを吸収）
    if inflight.get("inflight") is not None:
        lines.append(f"*📦 制作中*: {inflight['inflight']}本（type:content オープン）")

    # 稼働（セッション数＝自律稼働の証跡）
    if cost:
        cost_line = f"*🤖 稼働*: {cost.get('sessions', 0)}セッション"
        if "cost" in cost:
            cost_line += f" / コスト ${cost['cost']:.2f}"
        lines.append(cost_line)

    # 日次スプリント報告（1セッション=1スプリント・session-sprint-rules.md §5）
    if sprint and sprint.get("done_sp"):
        sp_lines = [f"*🏃 Session Sprint*: done_sp {sprint['done_sp']}"
                    f"（sp 付き PR {sprint['sp_prs']}/{sprint['total_prs']}件）"]
        detail = []
        if sprint.get("sp_per_session"):
            detail.append(f"ベロシティ {sprint['sp_per_session']:.1f} sp/セッション")
        if sprint.get("min_per_sp"):
            detail.append(f"{sprint['min_per_sp']:.1f} 分/sp（PRリードタイム）")
        if sprint.get("cost_per_sp"):
            detail.append(f"${sprint['cost_per_sp']:.2f}/sp")
        if sprint.get("tokens_per_sp"):
            detail.append(f"{sprint['tokens_per_sp'] / 1000:.1f}K tokens/sp")
        if detail:
            sp_lines.append("  " + " / ".join(detail))
        if sprint.get("noop"):
            sp_lines.append(f"  no-op スプリント: {sprint['noop']}件")
        lines.append("\n".join(sp_lines))
    elif sprint is not None and sprint.get("total_prs"):
        lines.append(f"*🏃 Session Sprint*: done_sp 0（マージ {sprint['total_prs']}件に sp ラベルなし — 付与経路を確認）")

    # セッション別内訳は done_sp 0 でも表示する（稼働・コストの可視化として有用・#3024）
    if session_lines:
        lines.append("\n".join(["*🧭 セッション別*（sprint_session_metrics）:"] + session_lines))

    if not lines:
        lines.append("（直近12時間のアクティビティは検出されませんでした）")

    return "\n\n".join(lines)


def format_action_items(a_items: list) -> str:
    if not a_items:
        return ""
    out = []
    for it in a_items[:5]:
        out.append(f"• #{it['number']} {it['title'][:60]}")
    if len(a_items) > 5:
        out.append(f"…他 {len(a_items) - 5} 件")
    return "\n".join(out)


# ──────────────────────────────────────────
# メイン
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="半日アウトカムサマリー生成")
    parser.add_argument("--slack", action="store_true", help="Slack に半日サマリーを送信")
    parser.add_argument("--period", choices=["morning", "evening"], default="",
                        help="集計期間ラベル（morning=前日19:00〜本日07:00 / evening=本日07:00〜19:00）")
    parser.add_argument("--hours", type=int, default=12, help="集計対象の遡及時間（既定12時間）")
    args = parser.parse_args()

    now = datetime.now(JST)
    since = now - timedelta(hours=args.hours)

    if args.period == "morning":
        period_label = "前日19:00〜本日07:00"
    elif args.period == "evening":
        period_label = "本日07:00〜19:00"
    else:
        period_label = f"直近{args.hours}時間"

    # 各データソースを独立収集（1つが想定外例外でも他を出す＝ハートビート保証）
    try:
        commits = collect_commits(args.hours)
    except Exception as e:
        print(f"Warning: collect_commits failed: {e}", file=sys.stderr)
        commits = {"total": 0, "cats": {}}
    try:
        prs = collect_prs(since)
    except Exception as e:
        print(f"Warning: collect_prs failed: {e}", file=sys.stderr)
        prs = {"merged": [], "open_count": None, "open_titles": []}
    try:
        cost = collect_cost(since)
    except Exception as e:
        print(f"Warning: collect_cost failed: {e}", file=sys.stderr)
        cost = {}
    try:
        sns = collect_sns(since)
    except Exception as e:
        print(f"Warning: collect_sns failed: {e}", file=sys.stderr)
        sns = {}
    try:
        inflight = collect_inflight()
    except Exception as e:
        print(f"Warning: collect_inflight failed: {e}", file=sys.stderr)
        inflight = {}
    try:
        a_items = collect_action_items()
    except Exception as e:
        print(f"Warning: collect_action_items failed: {e}", file=sys.stderr)
        a_items = []
    try:
        sprint = collect_sprint(since, cost)
    except Exception as e:
        print(f"Warning: collect_sprint failed: {e}", file=sys.stderr)
        sprint = {}
    try:
        persist_sprint_log(sprint, period_label)
    except Exception as e:
        print(f"Warning: persist_sprint_log failed: {e}", file=sys.stderr)
    # セッション単位ベロシティ（tokens/sp・$/sp・分/sp）— Issue #3024
    session_lines = []
    try:
        from sprint_session_metrics import collect_sessions, persist as persist_sessions, slack_lines
        session_data = collect_sessions(hours=args.hours)
        persist_sessions(session_data)
        session_lines = slack_lines(session_data)
    except Exception as e:
        print(f"Warning: sprint_session_metrics failed: {e}", file=sys.stderr)

    body = build_summary_body(commits, prs, cost, sns, inflight, sprint, session_lines)
    action_text = format_action_items(a_items)

    if not args.slack:
        print(f"=== 半日アウトカムサマリー（{period_label}）===\n")
        print(body)
        if action_text:
            print("\n=== ⚠️ 要対応（A区分） ===")
            print(action_text)
        return

    # Slack 送信（要対応ゼロでも必ず1通＝ハートビート）
    slack_script = str(PROJECT_DIR / "tools" / "slack_notify.py")
    cmd = [sys.executable, slack_script, "half-day-summary",
           "--summary", body, "--period-label", period_label]
    if action_text:
        cmd.extend(["--action-items", action_text])
    else:
        cmd.append("--no-mention")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode == 0:
            print(result.stdout)
            sys.exit(0)
        else:
            print(f"Error: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: Slack notification timed out", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
