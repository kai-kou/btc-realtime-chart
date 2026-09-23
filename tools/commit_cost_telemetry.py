#!/usr/bin/env python3
"""
日次コストテレメトリ永続化ツール（Issue #106 → #242 で PR レーン廃止）
----------------------------------------------------------------------
月次コスト集計（content/analytics/cost_monthly/YYYY-MM.json）を、作業中の feature
ブランチ・main を一切汚さずに、テレメトリ専用データブランチ（telemetry/cost-data）へ
plain git push で永続化する。

【背景・根本原因（#242）】
旧設計（#106）は「1 日 1 回、専用ブランチ → PR → squash マージで main へ永続化」だったが、
クラウド実行環境では gh pr list/create/merge が egress プロキシに 403 でブロックされ
（L-114）、安全側スキップで実質機能していなかった。その結果、Stop hook の未コミット検知に
ナギングされたセッションが手動で PR を作る「トークン浪費の永続化」が常態化していた。

【設計（#242）】
- cost_monthly は gitignore 対象（main では追跡しない）。feature 差分・未コミット検知から
  構造的に消える。
- 永続化先は main ではなくデータブランチ `telemetry/cost-data`（GitHub Actions の
  benchmark データを gh-pages に置くのと同じ「データブランチ」パターン）。
- コミットは git plumbing（hash-object → read-tree/update-index → write-tree →
  commit-tree → push sha:refs/heads/...）で構築し、ワーキングツリー・チェックアウトに
  一切触れない（worktree も作らない）。gh は使わない（クラウドでも全経路が生存）。
- 並行セッション競合は push の non-fast-forward 拒否が排他ロックになる。拒否されたら
  リモートを fetch し直して再マージ → 再 push（最大 4 回・指数バックオフ）。
- 冪等: リモートブランチの「実データ（daily/totals/sessions。last_updated は無視）」と比較し、
  差分が無ければ no-op。
- 正確な同日合算（#244）: 月次 JSON は session_id キーの `sessions` マップを持ち、ローカル寄与は
  cost_log.jsonl のセッションレコードから直接読む。マージは session_id ユニオンなので、
  同日に複数コンテナが走っても正確に合算され、同一セッションの累積スナップショットは
  フィールド毎 max（=最新値）に畳まれて重複計上しない。セッション詳細の無いレガシー日次は
  フロア（フィールド毎 max）として保全する。
- `--gate-daily`: JST 当日に既に実行済みならスキップ（マーカーファイル）。Stop hook から
  毎セッション呼んでも 1 日 1 回に収束する（外部スケジューラ非依存）。

使い方:
  python3 tools/commit_cost_telemetry.py              # 差分があればデータブランチへ push
  python3 tools/commit_cost_telemetry.py --gate-daily # 当日未実行のときだけ実行（Stop hook 用）
  python3 tools/commit_cost_telemetry.py --dry-run    # 差分判定のみ（push しない）
  python3 tools/commit_cost_telemetry.py --self-test  # merge/substance ロジックの自己テスト

データの参照方法:
  git fetch origin telemetry/cost-data
  git show origin/telemetry/cost-data:content/analytics/cost_monthly/YYYY-MM.json
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# calc_daily_cost と定数・換算を共有（DRY）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from calc_daily_cost import _MONTHLY_TOTAL_KEYS, USD_TO_JPY  # type: ignore
except Exception:  # pragma: no cover - フォールバック（import 失敗時も自己完結）
    _MONTHLY_TOTAL_KEYS = (
        "sessions", "input_tokens", "output_tokens",
        "cache_write_tokens", "cache_read_tokens", "cost_usd",
    )
    USD_TO_JPY = 150

JST = timezone(timedelta(hours=9))
MONTHLY_REL_DIR = "content/analytics/cost_monthly"
MARKER_REL = "content/pipeline-state/.cost_telemetry_push_date"
COST_LOG_REL = "content/pipeline-state/cost_log.jsonl"
# セッションレコードの数値フィールド（_MONTHLY_TOTAL_KEYS から件数フィールドを除いたもの）
_SESSION_FIELDS = tuple(k for k in _MONTHLY_TOTAL_KEYS if k != "sessions")
TELEMETRY_BRANCH = "telemetry/cost-data"
TELEMETRY_REF = f"refs/remotes/origin/{TELEMETRY_BRANCH}"
BRANCH_README = (
    "# telemetry/cost-data\n\n"
    "Claude Code セッションの月次コスト集計（機械生成テレメトリ）専用のデータブランチにゃ。\n\n"
    "- 書き込みは `tools/commit_cost_telemetry.py`（Stop hook から 1 日 1 回）のみ。\n"
    "- main とはマージしない（コード履歴を汚さない・#242）。\n"
    "- 参照: `git show origin/telemetry/cost-data:content/analytics/cost_monthly/YYYY-MM.json`\n"
)


def _run(cmd: list, timeout: int = 60, cwd: str | None = None,
         env: dict | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
    """サブプロセス実行（テキスト・タイムアウト付き）。

    git 不在（FileNotFoundError）やタイムアウトでも未ハンドル例外でクラッシュさせず、
    非ゼロ returncode の CompletedProcess を返す（呼び出し側は returncode で判定する）。
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, cwd=cwd, env=env, input=input_text,
        )
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args=cmd, returncode=127, stdout="",
                                           stderr=f"command not found: {e}")
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args=cmd, returncode=124, stdout="",
                                           stderr=f"timeout: {e}")


def project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


# ──────────────────────────────────────────────────────────
# マージ / 冪等比較ロジック（self-test 対象）
# ──────────────────────────────────────────────────────────

def recompute_totals(daily: dict) -> dict:
    """マージ後の daily から月次 totals を再計算する（calc_daily_cost と同一規則）。"""
    totals = {key: 0 for key in _MONTHLY_TOTAL_KEYS}
    totals["cost_usd"] = 0.0
    for day in daily.values():
        if not isinstance(day, dict):
            continue
        for key in _MONTHLY_TOTAL_KEYS:
            val = day.get(key)
            if val is None:
                continue
            if key == "cost_usd":
                totals[key] = round(totals[key] + float(val), 6)
            else:
                totals[key] += int(val)
    return totals


def _merge_day(a: dict, b: dict) -> dict:
    """同一日のレコードをフィールド毎の max で統合する（単調性フロア・#243 レビュー）。

    セッション詳細（sessions マップ）が無いレガシー日次データ同士の統合と、
    「セッションユニオン由来の日次値 vs 永続済みフロア」の統合に使う。
    フィールド毎 max により「永続化済みデータは決して後退しない」を機械保証する。
    """
    out: dict = {}
    for key in _MONTHLY_TOTAL_KEYS:
        va, vb = a.get(key), b.get(key)
        if key == "cost_usd":
            out[key] = round(max(float(va or 0), float(vb or 0)), 6)
        else:
            out[key] = max(int(va or 0), int(vb or 0))
    return out


def _merge_session(a: dict, b: dict) -> dict:
    """同一 session_id のレコードを統合する（フィールド毎 max・date は最新）。

    cost_log の同一セッション行は「累積スナップショット」（Stop のたびに増える）なので、
    max = 最新値。別コンテナが同じセッションを別時点で push した場合も同様に収束する。
    """
    out = {"date": max(a.get("date") or "", b.get("date") or "")}
    for key in _SESSION_FIELDS:
        va, vb = a.get(key), b.get(key)
        if key == "cost_usd":
            out[key] = round(max(float(va or 0), float(vb or 0)), 6)
        else:
            out[key] = max(int(va or 0), int(vb or 0))
    return out


def _daily_from_sessions(sessions: dict) -> dict:
    """セッションユニオンから日次集計を導出する（session_id 単位なので重複計上なし）。"""
    daily: dict = {}
    for rec in sessions.values():
        if not isinstance(rec, dict):
            continue
        day = rec.get("date")
        if not isinstance(day, str) or len(day) != 10:
            continue
        d = daily.setdefault(day, {key: (0.0 if key == "cost_usd" else 0)
                                   for key in _MONTHLY_TOTAL_KEYS})
        d["sessions"] += 1
        for key in _SESSION_FIELDS:
            if key == "cost_usd":
                d[key] = round(d[key] + float(rec.get(key) or 0), 6)
            else:
                d[key] += int(rec.get(key) or 0)
    return daily


def merge_report(month: str, remote_report: dict | None, local_report: dict,
                 local_sessions: dict | None = None) -> dict:
    """リモート（データブランチ）の月次レポートに、ローカルのデータをマージする。

    - sessions: session_id をキーにリモートとローカル（cost_log 由来）をユニオンする（#244）。
      別コンテナのセッションは ID が異なるため正確に合算され、同一セッションの累積
      スナップショットはフィールド毎 max（=最新値）に畳まれて重複計上しない。
    - daily: セッションユニオンからの導出値と、セッション詳細を持たないレガシー日次
      （リモート/ローカルの既存 daily）のフロアをフィールド毎 max で統合する。
      30 日ローテーションで生ログから消えた過去日もリモート側に残っていれば保全される。
    - totals: マージ後 daily から再計算。
    - last_updated: 両者の最大値（単調増加）。
    """
    # 1) セッションユニオン
    merged_sessions: dict = {}
    remote_sessions = (remote_report.get("sessions")
                       if isinstance(remote_report, dict) else None)
    for src in (remote_sessions, local_sessions):
        if not isinstance(src, dict):
            continue
        for sid, rec in src.items():
            if not isinstance(rec, dict):
                continue
            cur = merged_sessions.get(sid)
            merged_sessions[sid] = _merge_session(cur, rec) if cur else _merge_session(rec, rec)

    # 2) レガシー日次フロア（セッション詳細の無い既存データを後退させない）
    floor: dict = {}
    for src_rep in (remote_report, local_report):
        if isinstance(src_rep, dict) and isinstance(src_rep.get("daily"), dict):
            for day, rec in src_rep["daily"].items():
                if not isinstance(rec, dict):
                    continue
                base = floor.get(day)
                floor[day] = _merge_day(base, rec) if isinstance(base, dict) else rec

    # 3) 導出日次とフロアの統合
    derived = _daily_from_sessions(merged_sessions)
    merged_daily: dict = {}
    for day in set(derived) | set(floor):
        merged_daily[day] = _merge_day(derived.get(day) or {}, floor.get(day) or {})

    totals = recompute_totals(merged_daily)
    # last_updated が JSON 上 null（None）でも max() の str 比較で TypeError を出さないよう
    # `or ""` で確実に空文字へフォールバックする。
    remote_lu = (remote_report.get("last_updated") if isinstance(remote_report, dict) else None) or ""
    last_updated = max(remote_lu, local_report.get("last_updated") or "")
    return {
        "month": month,
        "totals": totals,
        "cost_jpy_approx": round(totals["cost_usd"] * USD_TO_JPY),
        "daily": dict(sorted(merged_daily.items())),
        "sessions": dict(sorted(merged_sessions.items())),
        "last_updated": last_updated,
    }


def substance(report: dict | None) -> str:
    """実データ（daily + totals + sessions）だけを正規化した比較キー。last_updated 等の揮発フィールドは無視。

    これにより「コストに実変化が無いのに last_updated だけ進んだ」churn では push しない。
    """
    if not isinstance(report, dict):
        return ""
    core = {
        "daily": report.get("daily", {}),
        "totals": report.get("totals", {}),
        "sessions": report.get("sessions", {}),
    }
    return json.dumps(core, ensure_ascii=False, sort_keys=True)


def serialize(report: dict) -> str:
    """月次レポートを calc_daily_cost と同一フォーマットで直列化する。"""
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


# ──────────────────────────────────────────────────────────
# リモート（データブランチ）側ファイル取得・差分計算
# ──────────────────────────────────────────────────────────

def sync_remote_ref() -> str:
    """データブランチのリモート追跡 ref を最新化し、状態を返す。

    - "ok"    : fetch 成功（ref は最新）
    - "absent": リモートにブランチが存在しない（初回。parentless コミットを作ってよい）
    - "error" : ネットワーク等の失敗（absent と区別する。この状態で parentless コミットを
                作ると、実在するブランチに対して non-FF が確定する無駄玉になる・#243 レビュー）
    """
    cp = _run(["git", "fetch", "origin",
               f"+refs/heads/{TELEMETRY_BRANCH}:{TELEMETRY_REF}"],
              timeout=45, cwd=str(project_dir()))
    if cp.returncode == 0:
        return "ok"
    ls = _run(["git", "ls-remote", "--heads", "origin", TELEMETRY_BRANCH],
              timeout=30, cwd=str(project_dir()))
    if ls.returncode == 0 and not ls.stdout.strip():
        return "absent"
    return "error"


def remote_branch_sha() -> str | None:
    cp = _run(["git", "rev-parse", "--verify", "--quiet", TELEMETRY_REF],
              timeout=15, cwd=str(project_dir()))
    sha = cp.stdout.strip()
    return sha if cp.returncode == 0 and sha else None


def read_local_monthly() -> dict:
    """ローカル作業ツリーの月次レポートを {month: report} で返す。"""
    out: dict = {}
    d = project_dir() / MONTHLY_REL_DIR
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(rep, dict):
                out[f.stem] = rep
        except (json.JSONDecodeError, OSError):
            continue
    return out


def read_local_sessions() -> dict:
    """cost_log.jsonl のセッションレコードを {month: {session_id: record}} で返す（#244）。

    同一セッションの複数行（Stop のたびに追記される累積スナップショット）は
    「最新行（timestamp 最大・タイブレークは cost_usd）」に畳み、セッションを最終行の
    date（= 終了日）の月へ 1 回だけ帰属させる。行ごとに月へ振り分けると、日・月を跨ぐ
    セッションが複数月の sessions マップへ重複計上される（#245 レビュー）。record は
    {date, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd}。
    """
    p = project_dir() / COST_LOG_REL
    if not p.is_file():
        return {}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    best: dict = {}  # sid -> (timestamp, record)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        sid = raw.get("session_id")
        date = raw.get("date")
        if not isinstance(sid, str) or not sid or not isinstance(date, str) or len(date) != 10:
            continue
        # 破損行耐性: 数値フィールドは安全に強制変換する（"12.5" 等の文字列が 1 行でも
        # あると int() の ValueError で全月の永続化が停止するため・#245 レビュー）
        rec = {"date": date}
        for k in _SESSION_FIELDS:
            try:
                v = float(raw.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            rec[k] = round(v, 6) if k == "cost_usd" else int(v)
        ts = str(raw.get("timestamp") or "")
        cur = best.get(sid)
        if cur is None or (ts, float(rec["cost_usd"])) >= (cur[0], float(cur[1]["cost_usd"])):
            best[sid] = (ts, rec)
    out: dict = {}
    for sid, (_ts, rec) in best.items():
        out.setdefault(rec["date"][:7], {})[sid] = rec
    return out


def _json_at(git_ref_path: str) -> dict | None:
    """`git show <ref>:<path>` の JSON を dict で返す（不在・破損は None）。"""
    cp = _run(["git", "show", git_ref_path], timeout=15, cwd=str(project_dir()))
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    try:
        rep = json.loads(cp.stdout)
        return rep if isinstance(rep, dict) else None
    except json.JSONDecodeError:
        return None


def read_remote_monthly(month: str) -> dict | None:
    """永続化済みの月次レポートを取得する（不在なら None）。

    参照順（移行フォールバック・#243 レビュー）:
    1. データブランチ（正本）
    2. origin/main の追跡コピー（apply-base 派生リポが #242 移行前の場合）
    3. origin/main 履歴上の最終追跡版（派生リポが追跡削除だけ先に取り込んだ場合の種データ。
       これが無いと初回 push が「現コンテナの部分ビューのみ」になり過去履歴が失われる）
    """
    rel = f"{MONTHLY_REL_DIR}/{month}.json"
    rep = _json_at(f"{TELEMETRY_REF}:{rel}")
    if rep is not None:
        return rep
    rep = _json_at(f"origin/main:{rel}")
    if rep is not None:
        return rep
    rl = _run(["git", "rev-list", "-1", "origin/main", "--", rel],
              timeout=15, cwd=str(project_dir()))
    sha = rl.stdout.strip()
    if rl.returncode == 0 and sha:
        return _json_at(f"{sha}:{rel}")
    return None


def compute_changes() -> dict:
    """永続化が必要な月次レポートを {month: merged_report} で返す（実データ差分のみ）。"""
    changes: dict = {}
    local_monthly = read_local_monthly()
    local_sessions = read_local_sessions()
    for month in sorted(set(local_monthly) | set(local_sessions)):
        remote_rep = read_remote_monthly(month)
        merged = merge_report(month, remote_rep, local_monthly.get(month, {}),
                              local_sessions.get(month))
        if substance(merged) != substance(remote_rep):
            changes[month] = merged
    return changes


# ──────────────────────────────────────────────────────────
# 日次ゲート
# ──────────────────────────────────────────────────────────

def marker_path() -> Path:
    return project_dir() / MARKER_REL


def already_ran_today() -> bool:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    try:
        return marker_path().read_text(encoding="utf-8").strip() == today
    except OSError:
        return False


def stamp_today() -> None:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    p = marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(today + "\n", encoding="utf-8")
    except OSError:
        pass


# ──────────────────────────────────────────────────────────
# データブランチへの push（git plumbing・ワーキングツリー非接触）
# ──────────────────────────────────────────────────────────

def _git_env() -> dict:
    """commit-tree 用の identity をフォールバック付きで用意する（既存設定は尊重）。"""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "claude-code-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "claude-code-bot@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", "claude-code-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "claude-code-bot@users.noreply.github.com")
    return env


def build_commit(changes: dict, parent_sha: str | None) -> str | None:
    """changes を含むコミットオブジェクトを plumbing で構築し、その SHA を返す。

    一時 index ファイルを使うため、通常の index・ワーキングツリーには一切影響しない。
    """
    pdir = str(project_dir())
    env = _git_env()
    with tempfile.NamedTemporaryFile(prefix="cost-telemetry-index-", delete=False) as tf:
        index_file = tf.name
    env["GIT_INDEX_FILE"] = index_file
    try:
        # 1) ベース tree を一時 index に読み込む（初回は空 index から開始）
        if parent_sha:
            rt = _run(["git", "read-tree", f"{parent_sha}^{{tree}}"], timeout=30, cwd=pdir, env=env)
        else:
            rt = _run(["git", "read-tree", "--empty"], timeout=30, cwd=pdir, env=env)
        if rt.returncode != 0:
            print(f"[cost-telemetry] read-tree 失敗: {rt.stderr.strip()}", file=sys.stderr)
            return None

        # 2) 月次 JSON（+ 初回のみ README）を blob 化して index に登録
        entries = {f"{MONTHLY_REL_DIR}/{month}.json": serialize(report)
                   for month, report in changes.items()}
        if not parent_sha:
            entries["README.md"] = BRANCH_README
        for rel_path, content in entries.items():
            ho = _run(["git", "hash-object", "-w", "--stdin"], timeout=30, cwd=pdir,
                      env=env, input_text=content)
            blob = ho.stdout.strip()
            if ho.returncode != 0 or not blob:
                print(f"[cost-telemetry] hash-object 失敗: {ho.stderr.strip()}", file=sys.stderr)
                return None
            ui = _run(["git", "update-index", "--add", "--cacheinfo",
                       f"100644,{blob},{rel_path}"], timeout=30, cwd=pdir, env=env)
            if ui.returncode != 0:
                print(f"[cost-telemetry] update-index 失敗: {ui.stderr.strip()}", file=sys.stderr)
                return None

        # 3) tree → commit オブジェクトを構築
        wt = _run(["git", "write-tree"], timeout=30, cwd=pdir, env=env)
        tree = wt.stdout.strip()
        if wt.returncode != 0 or not tree:
            print(f"[cost-telemetry] write-tree 失敗: {wt.stderr.strip()}", file=sys.stderr)
            return None
        months = ", ".join(sorted(changes))
        msg = f"chore(telemetry): 月次コスト集計を更新（{months}）"
        ct_cmd = ["git", "commit-tree", tree, "-m", msg]
        if parent_sha:
            ct_cmd[3:3] = ["-p", parent_sha]
        ct = _run(ct_cmd, timeout=30, cwd=pdir, env=env)
        commit = ct.stdout.strip()
        if ct.returncode != 0 or not commit:
            print(f"[cost-telemetry] commit-tree 失敗: {ct.stderr.strip()}", file=sys.stderr)
            return None
        return commit
    finally:
        try:
            os.unlink(index_file)
        except OSError:
            pass


def _push_retryable(push: subprocess.CompletedProcess) -> bool:
    """リトライで解決しうる push 失敗か（non-FF 競合 / タイムアウト系のみ）。

    403・認証等の恒久失敗まで 4 回リトライすると Stop hook の実行予算を浪費するため、
    それらは初回で打ち切る（#243 レビュー）。
    """
    if push.returncode == 124:  # timeout
        return True
    err = (push.stderr or "").lower()
    return any(s in err for s in ("non-fast-forward", "fetch first", "cannot lock ref",
                                  "failed to push some refs"))


def push_to_telemetry_branch(dry_run: bool = False) -> bool:
    """ローカル月次データをデータブランチへ push する（競合時は再マージ・リトライ）。

    non-fast-forward 拒否を並行セッションとの排他ロックとして扱い、拒否されたら
    リモートを fetch し直して再マージ → 再構築 → 再 push する。成功で True。
    dry_run=True は fetch + 差分判定・表示のみ（push しない・1 パス）。
    """
    pdir = str(project_dir())
    attempts = (0,) if dry_run else (0, 2, 4, 8)
    for attempt, wait in enumerate(attempts):
        if wait:
            time.sleep(wait)
        state = sync_remote_ref()
        if state == "error" and not dry_run:
            # absent と区別できないまま parentless コミットを作らない（non-FF 確定の無駄玉）
            print(f"[cost-telemetry] fetch 失敗（試行 {attempt + 1}/{len(attempts)}・"
                  "ネットワーク要因の可能性）", file=sys.stderr)
            continue
        parent = remote_branch_sha()
        # 毎試行、最新リモートに対して再マージ（並行 push で進んだ分を吸収）
        changes = compute_changes()
        if not changes:
            print("[cost-telemetry] 永続化対象の差分なし（no-op）")
            return True
        if dry_run:
            note = "" if state == "ok" else f"（注: リモート ref 未取得 state={state}・全月差分扱いの可能性）"
            print(f"[cost-telemetry] dry-run: 差分のある月 = {', '.join(sorted(changes))}{note}")
            return True
        commit = build_commit(changes, parent)
        if commit is None:
            return False
        push = _run(["git", "push", "origin", f"{commit}:refs/heads/{TELEMETRY_BRANCH}"],
                    timeout=45, cwd=pdir)
        if push.returncode == 0:
            print(f"[cost-telemetry] {TELEMETRY_BRANCH} へ push 完了"
                  f"（{', '.join(sorted(changes))} / {commit[:12]}）")
            return True
        print(f"[cost-telemetry] push 失敗（試行 {attempt + 1}/{len(attempts)}）: "
              f"{push.stderr.strip()}", file=sys.stderr)
        if not _push_retryable(push):
            break
    print("[cost-telemetry] 永続化失敗（当日中は次セッションの Stop hook が再試行する）",
          file=sys.stderr)
    return False


# ──────────────────────────────────────────────────────────
# self-test
# ──────────────────────────────────────────────────────────

def self_test() -> int:
    failures = []

    # 1) 新規月: リモート不在 → local がそのまま採用され、差分ありと判定される
    local = {"month": "2099-01", "totals": {}, "daily": {
        "2099-01-01": {"sessions": 1, "input_tokens": 10, "output_tokens": 20,
                        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.001},
    }, "last_updated": "2099-01-01T10:00:00+09:00"}
    merged = merge_report("2099-01", None, local)
    if merged["totals"]["sessions"] != 1 or merged["totals"]["input_tokens"] != 10:
        failures.append("新規月の totals 再計算が不正")
    if substance(merged) == substance(None):
        failures.append("新規月で差分なしと誤判定")

    # 2) リモートの過去日を保全しつつ local で当日を上書き
    remote_rep = {"month": "2099-01", "totals": {}, "daily": {
        "2099-01-01": {"sessions": 1, "input_tokens": 10, "output_tokens": 20,
                        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.001},
        "2099-01-02": {"sessions": 1, "input_tokens": 5, "output_tokens": 5,
                        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.002},
    }, "last_updated": "2099-01-02T10:00:00+09:00"}
    local2 = {"month": "2099-01", "totals": {}, "daily": {
        "2099-01-02": {"sessions": 3, "input_tokens": 50, "output_tokens": 60,
                        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.009},
    }, "last_updated": "2099-01-02T18:00:00+09:00"}
    m2 = merge_report("2099-01", remote_rep, local2)
    if set(m2["daily"]) != {"2099-01-01", "2099-01-02"}:
        failures.append("過去日の保全に失敗")
    if m2["daily"]["2099-01-02"]["sessions"] != 3:
        failures.append("当日上書きに失敗")
    if m2["totals"]["sessions"] != 4:  # 1（01日）+ 3（02日・上書き後）
        failures.append(f"マージ後 totals が不正: {m2['totals']['sessions']}")

    # 3) 冪等: last_updated だけ違い実データ同一なら差分なし
    #    本番のリモート側ファイルは totals 再計算済みなので、テストでも再計算した totals を持たせる
    persisted = {
        "month": "2099-01",
        "totals": recompute_totals(remote_rep["daily"]),
        "daily": remote_rep["daily"],
        "last_updated": "2099-01-02T10:00:00+09:00",
    }
    bumped = merge_report("2099-01", persisted, {
        "month": "2099-01", "daily": remote_rep["daily"],
        "last_updated": "2099-01-09T23:59:59+09:00",
    })
    if substance(bumped) != substance(persisted):
        failures.append("last_updated のみ変化で差分ありと誤判定（冪等性違反）")

    # 4) serialize は calc_daily_cost と同じ末尾改行付き
    if not serialize({"month": "x"}).endswith("}\n"):
        failures.append("serialize の整形が不正")

    # 5) last_updated が None（JSON null）でも TypeError を出さずマージできる
    try:
        m5 = merge_report("2099-02",
                          {"daily": {}, "last_updated": None},
                          {"daily": {}, "last_updated": None})
        if m5["last_updated"] != "":
            failures.append("last_updated=None のフォールバックが不正")
    except TypeError:
        failures.append("last_updated=None で TypeError（None 比較の回帰）")

    # 6) 単調性: fresh コンテナの部分ビュー（同日・少ないセッション/コスト）が
    #    永続化済みのリッチな値を後退させない（#243 レビュー・実害確認済みの回帰）
    rich = {"month": "2099-03", "totals": {}, "daily": {
        "2099-03-01": {"sessions": 3, "input_tokens": 100, "output_tokens": 200,
                        "cache_write_tokens": 10, "cache_read_tokens": 20, "cost_usd": 28.33},
    }, "last_updated": "2099-03-01T12:00:00+09:00"}
    partial = {"month": "2099-03", "daily": {
        "2099-03-01": {"sessions": 1, "input_tokens": 5, "output_tokens": 8,
                        "cache_write_tokens": 1, "cache_read_tokens": 2, "cost_usd": 7.61},
    }, "last_updated": "2099-03-01T18:00:00+09:00"}
    m6 = merge_report("2099-03", rich, partial)
    if m6["daily"]["2099-03-01"]["sessions"] != 3 or m6["daily"]["2099-03-01"]["cost_usd"] != 28.33:
        failures.append("部分ビューで永続化済みデータが後退（単調性違反）")

    # 7) 単調性の逆方向: local の方がリッチならフィールド毎 max で local 値が採用される
    m7 = merge_report("2099-03", partial, rich)
    if m7["daily"]["2099-03-01"]["sessions"] != 3 or m7["daily"]["2099-03-01"]["input_tokens"] != 100:
        failures.append("リッチな local 値の採用に失敗（フィールド毎 max 不正）")

    # 8) クロスコンテナ正確合算（#244）: 別コンテナの別セッションは session_id ユニオンで加算される
    s1 = {"date": "2099-04-01", "input_tokens": 10, "output_tokens": 20,
          "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 5.0}
    s2 = {"date": "2099-04-01", "input_tokens": 30, "output_tokens": 40,
          "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 7.0}
    remote_a = merge_report("2099-04", None, {}, {"sess-a": s1})  # コンテナ A が push 済み
    m8 = merge_report("2099-04", remote_a, {}, {"sess-b": s2})    # コンテナ B（fresh）が push
    d8 = m8["daily"]["2099-04-01"]
    if d8["sessions"] != 2 or d8["cost_usd"] != 12.0 or d8["input_tokens"] != 40:
        failures.append(f"クロスコンテナ合算が不正: {d8}")

    # 9) 同一セッションの重複排除: 累積スナップショットはフィールド毎 max（=最新値）に畳まれる
    s1_later = dict(s1, cost_usd=9.0, output_tokens=50)
    m9 = merge_report("2099-04", remote_a, {}, {"sess-a": s1_later})
    d9 = m9["daily"]["2099-04-01"]
    if len(m9["sessions"]) != 1 or d9["sessions"] != 1 or d9["cost_usd"] != 9.0:
        failures.append(f"同一セッションの重複排除が不正: {d9}")

    # 10) レガシーフロア保全: セッション詳細の無い既存 daily はセッション導出値より
    #     リッチなら維持される（過去データを後退させない）
    legacy = {"month": "2099-04", "totals": {}, "daily": {
        "2099-04-01": {"sessions": 3, "input_tokens": 500, "output_tokens": 600,
                        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 28.33},
    }, "last_updated": "2099-04-01T10:00:00+09:00"}
    m10 = merge_report("2099-04", legacy, {}, {"sess-c": s2})
    d10 = m10["daily"]["2099-04-01"]
    if d10["sessions"] != 3 or d10["cost_usd"] != 28.33 or d10["input_tokens"] != 500:
        failures.append(f"レガシーフロア保全に失敗: {d10}")

    if failures:
        for f in failures:
            print(f"  ✗ {f}", file=sys.stderr)
        print(f"self-test FAILED（{len(failures)} 件）", file=sys.stderr)
        return 1
    print("self-test PASSED（10 ケース）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="月次コスト集計をデータブランチ（telemetry/cost-data）へ永続化する")
    ap.add_argument("--gate-daily", action="store_true",
                    help="JST 当日に既に実行済みならスキップ（Stop hook から毎回呼ぶ用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="差分判定のみ。push しない")
    ap.add_argument("--self-test", action="store_true", help="ロジックの自己テスト")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.gate_daily and already_ran_today():
        return 0

    ok = push_to_telemetry_branch(dry_run=args.dry_run)
    # マーカーは「成功後」に stamp する（#243 レビュー）。旧設計は失敗時の orphan PR 量産を
    # 恐れて事前 stamp していたが、新設計の失敗は成果物を残さないため、失敗日は同日中の
    # 次セッション Stop hook がそのまま再試行できる（データ消失ウィンドウを作らない）。
    if ok and args.gate_daily and not args.dry_run:
        stamp_today()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
