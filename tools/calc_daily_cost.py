#!/usr/bin/env python3
"""
Claude Code セッション日次コスト計算ツール（#1213）
--------------------------------------------------
Stop フックの stdin JSON（transcript_path を含む）またはコマンドライン引数から
トランスクリプト JSONL を読み込み、トークン使用量とコストを計算する。

【マルチセッション競合対策】
従来の read-modify-write 方式（TOCTOU 競合の原因）を廃止し、
追記専用 JSONL（cost_log.jsonl）方式に移行した。

  セッション終了時: cost_log.jsonl に1行を O_APPEND でアトミック追記するのみ
  19:00 スロット : cost_log.jsonl を全スキャンして daily_cost_stats.json に集計
                   → さらに月次集計レポートを content/analytics/cost_monthly/ に書き出し
  Slack サマリー : aggregate_daily_stats_from_log() でリアルタイム集計

【永続化方針（#2263・#106・#242）】
  - cost_log.jsonl / daily_cost_stats.json は「揮発する非追跡 state file」
    （.gitignore 対象・git 追跡しない）。クラウドのコンテナ再生成で消える前提。
    直近 30 日のローリング保持のみで、デバッグ・当日予算監視の一次データとして使う。
  - コスト履歴のトレンド分析に必要な一次資料は、月次集計レポート
    content/analytics/cost_monthly/YYYY-MM.json として永続化する。
  - 【重要・#242】月次レポートも gitignore 対象（main では追跡しない）。永続化は
    tools/commit_cost_telemetry.py がテレメトリ専用データブランチ telemetry/cost-data へ
    「1 日 1 回の plain git push」で行う（gh 非依存）。feature ブランチ・main には
    一切混入させない。--flush はローカルの月次 JSON を更新するだけで、commit はしない。

計算結果は content/pipeline-state/cost_log.jsonl に追記保存し、
--summary-only オプションで当日サマリー文字列を標準出力に返す。
--flush オプションで cost_log.jsonl を集計して daily_cost_stats.json に書き出し、
月次集計レポート（content/analytics/cost_monthly/*.json）を upsert する。

使い方:
  echo '{"transcript_path": "/path/to/transcript.jsonl"}' | python3 tools/calc_daily_cost.py --summary-only
  python3 tools/calc_daily_cost.py --transcript-path /path/to/transcript.jsonl --summary-only
  python3 tools/calc_daily_cost.py --flush   # 集計してローカル月次 JSON を更新（commit はしない・#106）
"""

import argparse
try:
    import fcntl  # POSIX 専用。Windows 等では未提供のためガードする。
except ImportError:
    fcntl = None
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ───────────────────────────────
# モデル別トークン単価 (USD / 1M tokens)
# ───────────────────────────────
PRICING: dict[str, dict[str, float]] = {
    # エイリアス指定（opus/sonnet/haiku）でも単価を引けるようにする（agent-team.md「モデル指定の方針」）
    "opus":                          {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "sonnet":                        {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},
    "haiku":                         {"input": 1.0,  "cache_write": 1.25, "cache_read": 0.1,  "output": 5.0},
    "claude-opus-5":                 {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-8":               {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-7":               {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-6":               {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-5":               {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-sonnet-5":               {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},
    "claude-sonnet-4-6":             {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},  # legacy（過去ログ集計用）
    "claude-sonnet-4-5":             {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},
    "claude-haiku-4-5-20251001":     {"input": 1.0,  "cache_write": 1.25, "cache_read": 0.1,  "output": 5.0},
    "claude-haiku-4-5":              {"input": 1.0,  "cache_write": 1.25, "cache_read": 0.1,  "output": 5.0},
}
DEFAULT_MODEL = "claude-sonnet-5"

# 1 USD → 概算 JPY（レポート用、精度重要でない）
USD_TO_JPY = 150

JST = timezone(timedelta(hours=9))


def _safe_num(value, as_float: bool = False):
    """破損行耐性の数値変換（"12.5" 等の文字列や None でも例外を出さない・#245 レビュー）。"""
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        f = 0.0
    return f if as_float else int(f)


def get_stats_file() -> Path:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    stats_path = Path(project_dir) / "content" / "pipeline-state" / "daily_cost_stats.json"
    return stats_path


def get_log_file() -> Path:
    """追記専用 JSONL ファイルパスを返す。"""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    return Path(project_dir) / "content" / "pipeline-state" / "cost_log.jsonl"


def _generate_session_id(transcript_path: str | None) -> str:
    """
    セッション識別子を生成する。
    優先順位: transcript_path のファイル名ステム → CLAUDE_SESSION_ID 環境変数 → PID+タイムスタンプ
    """
    if transcript_path:
        stem = Path(transcript_path).stem
        if stem:
            return stem[:32]
    env_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if env_id:
        return env_id[:32]
    pid = os.getpid()
    now_ts = int(datetime.now(JST).timestamp())
    return f"pid{pid}-{now_ts}"


def parse_transcript(transcript_path: str) -> dict:
    """
    JSONL トランスクリプトを解析してトークン使用量を合計する。

    Claude Code transcript の各行は以下のような形式:
      {"type": "assistant", "message": {"model": "...", "usage": {"input_tokens": N, ...}}, ...}
    """
    total: dict[str, int | str] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "model": DEFAULT_MODEL,
    }

    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # assistant メッセージの usage を集計
                if msg.get("type") == "assistant":
                    inner = msg.get("message", {})
                    usage = inner.get("usage", {})
                    total["input_tokens"] += usage.get("input_tokens", 0)  # type: ignore[operator]
                    total["output_tokens"] += usage.get("output_tokens", 0)  # type: ignore[operator]
                    total["cache_write_tokens"] += usage.get("cache_creation_input_tokens", 0)  # type: ignore[operator]
                    total["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)  # type: ignore[operator]
                    # 最後のモデル名を使用する
                    if "model" in inner:
                        total["model"] = inner["model"]

                # tool_result の usage（一部モデルで出現）
                elif msg.get("type") == "tool_result":
                    usage = msg.get("usage", {})
                    total["input_tokens"] += usage.get("input_tokens", 0)  # type: ignore[operator]
                    total["output_tokens"] += usage.get("output_tokens", 0)  # type: ignore[operator]

    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[calc_daily_cost] Warning: transcript parse error: {e}", file=sys.stderr)

    return total


def calc_cost(usage: dict) -> float:
    """トークン使用量と単価からコスト (USD) を計算する。"""
    model_name = str(usage.get("model", DEFAULT_MODEL))
    # モデル名に部分マッチするエントリを探す
    pricing = PRICING.get(model_name)
    if pricing is None:
        for key, val in PRICING.items():
            # モデルファミリー（opus/sonnet/haiku）で照合（バージョン番号の違いに対応）
            if any(family in key and family in model_name.lower() for family in ["opus", "sonnet", "haiku"]):
                pricing = val
                break
    if pricing is None:
        pricing = PRICING[DEFAULT_MODEL]

    cost = (
        int(usage.get("input_tokens", 0)) * pricing["input"]
        + int(usage.get("output_tokens", 0)) * pricing["output"]
        + int(usage.get("cache_write_tokens", 0)) * pricing.get("cache_write", 0)
        + int(usage.get("cache_read_tokens", 0)) * pricing.get("cache_read", 0)
    ) / 1_000_000

    return round(cost, 6)


# ──────────────────────────────────────────────────────────
# 追記型 JSONL 方式（マルチセッション競合対策・メイン方式）
# ──────────────────────────────────────────────────────────

def append_session_log(session_usage: dict, session_id: str) -> None:
    """
    セッション終了時に1行 JSON を O_APPEND でアトミック追記する。
    read-modify-write を一切行わないため TOCTOU 競合が発生しない。

    安全性の根拠:
    - LOCK_SH で rotate_log() の LOCK_EX と相互排他制御する
    - os.write() をループして partial write に対応する
    - JSONDecodeError で不正行を無視する設計で整合性を維持する
    """
    log_file = get_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "date": datetime.now(JST).strftime("%Y-%m-%d"),
        "session_id": session_id,
        "input_tokens": int(session_usage.get("input_tokens", 0)),
        "output_tokens": int(session_usage.get("output_tokens", 0)),
        "cache_write_tokens": int(session_usage.get("cache_write_tokens", 0)),
        "cache_read_tokens": int(session_usage.get("cache_read_tokens", 0)),
        "cost_usd": round(float(session_usage.get("cost_usd", 0.0)), 6),
        "model": str(session_usage.get("model", DEFAULT_MODEL)),
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
    }

    line_bytes = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    # O_APPEND + O_CREAT: 追記専用オープン
    fd = os.open(str(log_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        # LOCK_SH: rotate_log() の LOCK_EX と協調して inode 入れ替え競合を防ぐ
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_SH)
        # partial write 対応: 全バイトを確実に書き込む
        buf = line_bytes
        while buf:
            written = os.write(fd, buf)
            buf = buf[written:]
    finally:
        os.close(fd)


def aggregate_daily_stats_from_log(target_date: str | None = None) -> dict:
    """
    cost_log.jsonl から target_date の行を読み込み、session_id 単位で重複排除して集計する。
    target_date が None の場合は当日（JST）。

    【#244 修正】Stop hook はセッション中に何度も発火し、同一セッションの行は
    「累積スナップショット」として複数回追記される。行を無条件に合算すると
    1 セッションが行数分のセッション数・コスト和に水増しされるため、
    session_id 毎にフィールド毎 max（= 最新スナップショット）へ畳んでから合算する。
    """
    today = target_date or datetime.now(JST).strftime("%Y-%m-%d")
    log_file = get_log_file()

    totals: dict = {
        "sessions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "last_updated": "",
    }

    if not log_file.exists():
        return totals

    per_session: dict = {}
    with open(log_file, encoding="utf-8", errors="replace") as f:
        for i, raw_line in enumerate(f):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            # session_id 欠落行は行番号で独立セッション扱い（旧形式ログとの互換）
            sid = record.get("session_id") or f"line-{i}"
            cur = per_session.get(sid)
            # 最新行（timestamp 最大）が最終累積値。日跨ぎセッションも「最終行の date」
            # に 1 回だけ帰属させる（date 毎に畳むと両日に重複計上される・#245 レビュー）
            if cur is None or (record.get("timestamp") or "") >= (cur.get("timestamp") or ""):
                per_session[sid] = record

    for record in per_session.values():
        if record.get("date") != today:
            continue
        totals["sessions"] += 1
        totals["input_tokens"] += _safe_num(record.get("input_tokens"))
        totals["output_tokens"] += _safe_num(record.get("output_tokens"))
        totals["cache_write_tokens"] += _safe_num(record.get("cache_write_tokens"))
        totals["cache_read_tokens"] += _safe_num(record.get("cache_read_tokens"))
        totals["cost_usd"] = round(totals["cost_usd"] + _safe_num(record.get("cost_usd"), as_float=True), 6)
        if record.get("timestamp", "") > totals["last_updated"]:
            totals["last_updated"] = record["timestamp"]

    return totals


def flush_to_stats_json() -> dict:
    """
    cost_log.jsonl を1パスで全スキャンして daily_cost_stats.json に書き出す。
    19:00 スロットの定期バッチから呼び出す（後方互換性維持）。

    daily_cost_stats.json は「参照用スナップショット」として維持する。
    書き込みはアトミック（tmp → rename）で行い、読み込みとの競合を防ぐ。
    """
    log_file = get_log_file()
    stats_file = get_stats_file()

    # 1パスで全日付を集計（O(N) — aggregate_daily_stats_from_log の O(N×D) を回避）
    # 【#244 修正】同一セッションの累積スナップショット行を session_id 単位で
    # 「最新行（timestamp 最大）」に畳んでから日次合算する（行数分の水増しを防ぐ）。
    # 日跨ぎセッションも「最終行の date」に 1 回だけ帰属させる（date 毎に畳むと
    # 両日に重複計上される・#245 レビュー）。
    per_session: dict[str, dict] = {}
    if log_file.exists():
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for i, raw_line in enumerate(f):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[calc_daily_cost] JSONDecodeError: 不正行をスキップ: {raw_line[:80]!r}", file=sys.stderr)
                    continue
                except Exception as exc:
                    print(f"[calc_daily_cost] 予期しないエラー: {exc}", file=sys.stderr)
                    continue

                date = record.get("date", "")
                if not date:
                    continue
                # session_id 欠落行は行番号で独立セッション扱い（旧形式ログとの互換）
                sid = record.get("session_id") or f"line-{i}"
                cur = per_session.get(sid)
                if cur is None or (record.get("timestamp") or "") >= (cur.get("timestamp") or ""):
                    per_session[sid] = record

    accumulated: dict[str, dict] = {}
    for record in per_session.values():
        date = record.get("date", "")
        if date not in accumulated:
            accumulated[date] = {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "last_updated": "",
            }
        acc = accumulated[date]
        acc["sessions"] += 1
        acc["input_tokens"] += _safe_num(record.get("input_tokens"))
        acc["output_tokens"] += _safe_num(record.get("output_tokens"))
        acc["cache_write_tokens"] += _safe_num(record.get("cache_write_tokens"))
        acc["cache_read_tokens"] += _safe_num(record.get("cache_read_tokens"))
        acc["cost_usd"] = round(acc["cost_usd"] + _safe_num(record.get("cost_usd"), as_float=True), 6)
        ts = record.get("timestamp", "")
        if ts > acc["last_updated"]:
            acc["last_updated"] = ts

    # 既存 JSON を読み込んで JSONL 由来データで上書き
    stats: dict = {}
    if stats_file.exists():
        try:
            stats = json.loads(stats_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[calc_daily_cost] JSONDecodeError: daily_cost_stats.json を初期化します", file=sys.stderr)
            stats = {}
        except Exception as exc:
            print(f"[calc_daily_cost] stats 読み込みエラー: {exc}", file=sys.stderr)
            stats = {}

    for date, day in accumulated.items():
        if day["sessions"] > 0:
            stats[date] = day

    # 直近 30 日分のみ保持
    for old_date in sorted(stats.keys())[:-30]:
        del stats[old_date]

    # アトミック書き込み（tmp → rename）
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = stats_file.with_suffix(".json.tmp")
    tmp_file.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_file.replace(stats_file)

    # 当日の集計を返す
    today = datetime.now(JST).strftime("%Y-%m-%d")
    return stats.get(today, accumulated.get(today, {
        "sessions": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0, "last_updated": "",
    }))


def rotate_log(keep_days: int = 30) -> int:
    """
    cost_log.jsonl から keep_days 日より古い行を削除する。
    19:00 スロットの flush_to_stats_json() 呼び出し後に実行する。
    戻り値: 削除した行数。

    LOCK_EX で append_session_log() の LOCK_SH と協調して inode 入れ替え競合を防ぐ。
    rename 後の新規ファイルは append_session_log() が O_CREAT で自動作成する。
    """
    log_file = get_log_file()
    if not log_file.exists():
        return 0

    # keep_days - 1 で cutoff を計算して正確に 30 日分を保持する（off-by-one 対策）
    cutoff = (datetime.now(JST) - timedelta(days=keep_days - 1)).strftime("%Y-%m-%d")

    lines_kept = []
    lines_removed = 0

    # LOCK_EX: append と排他制御して rename 中の追記競合を防ぐ
    fd = os.open(str(log_file), os.O_RDONLY)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            fd = None  # fdopen が所有権を持つため二重クローズを防ぐ
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("date", "") >= cutoff:
                        lines_kept.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
                    else:
                        lines_removed += 1
                except json.JSONDecodeError:
                    print(f"[calc_daily_cost] JSONDecodeError: rotate 中に不正行をスキップ: {raw_line[:80]!r}", file=sys.stderr)
                    lines_kept.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
                except Exception as exc:
                    print(f"[calc_daily_cost] rotate 中の予期しないエラー: {exc}", file=sys.stderr)
                    lines_kept.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
    finally:
        if fd is not None:
            os.close(fd)

    if lines_removed > 0:
        tmp_file = log_file.with_suffix(".jsonl.tmp")
        tmp_file.write_text("".join(lines_kept), encoding="utf-8")
        tmp_file.replace(log_file)

    return lines_removed


# ──────────────────────────────────────────────────────────
# 月次集計レポート（telemetry/cost-data ブランチへ永続化・#242）
# ──────────────────────────────────────────────────────────
# cost_log.jsonl / daily_cost_stats.json は揮発する非追跡 state file（クラウドの
# コンテナ再生成で消える）。コスト履歴のトレンド分析に必要な一次資料は、
# content/analytics/cost_monthly/YYYY-MM.json に「月次集計レポート」として書き出し、
# commit_cost_telemetry.py がテレメトリ専用データブランチへ永続化する
# （gitignore 対象・main では追跡しない・#242）。
# 19:00 スロットの --flush 後に呼び出し、当月分を upsert で更新する。

_MONTHLY_TOTAL_KEYS = (
    "sessions",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "cost_usd",
)


def get_monthly_report_dir() -> Path:
    """月次コストレポートの出力ディレクトリ（gitignore 対象・データブランチへ永続化）を返す。"""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    return Path(project_dir) / "content" / "analytics" / "cost_monthly"


def write_monthly_reports() -> list[Path]:
    """
    daily_cost_stats.json を月単位で集計し、月次レポートを upsert する。

    - 出力先: content/analytics/cost_monthly/YYYY-MM.json（gitignore 対象・
      telemetry/cost-data ブランチへ commit_cost_telemetry.py が永続化・#242）
    - 既存レポートの daily を読み込み、daily_cost_stats.json 由来の最新日次データで
      上書きマージする（30 日ローテーションで生ログから消えた過去日を保全するため、
      既存 daily は破棄せず温存する）
    - totals はマージ後の daily から再計算する

    戻り値: 書き出した（または更新した）レポートファイルのパス一覧。
    """
    stats_file = get_stats_file()
    if not stats_file.exists():
        return []

    try:
        stats = json.loads(stats_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[calc_daily_cost] 月次レポート: stats 読み込み失敗: {exc}", file=sys.stderr)
        return []
    if not isinstance(stats, dict):
        print("[calc_daily_cost] 月次レポート: stats が辞書形式ではありません", file=sys.stderr)
        return []

    # 日次データを月（YYYY-MM）でグルーピング
    by_month: dict[str, dict[str, dict]] = {}
    for date, day in stats.items():
        if not isinstance(day, dict) or len(date) < 7:
            continue
        by_month.setdefault(date[:7], {})[date] = day

    report_dir = get_monthly_report_dir()
    report_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for month, fresh_daily in by_month.items():
        report_file = report_dir / f"{month}.json"

        # 既存レポートの daily を温存（生ログから消えた過去日を保全）
        merged_daily: dict[str, dict] = {}
        if report_file.exists():
            try:
                existing = json.loads(report_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("daily"), dict):
                    merged_daily.update(existing["daily"])
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[calc_daily_cost] 月次レポート: 既存 {report_file.name} 読み込み失敗: {exc}", file=sys.stderr)

        # 最新の日次データで上書き
        merged_daily.update(fresh_daily)

        # totals をマージ後 daily から再計算
        totals = {key: 0 for key in _MONTHLY_TOTAL_KEYS}
        totals["cost_usd"] = 0.0
        for day in merged_daily.values():
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

        report = {
            "month": month,
            "totals": totals,
            "cost_jpy_approx": round(totals["cost_usd"] * USD_TO_JPY),
            "daily": dict(sorted(merged_daily.items())),
            "last_updated": datetime.now(JST).isoformat(timespec="seconds"),
        }

        # アトミック書き込み（tmp → rename）
        tmp_file = report_file.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_file.replace(report_file)
        written.append(report_file)

    return written


# ──────────────────────────────────────────────────────────
# 後方互換ラッパー（既存の stop-slack-notify.sh 呼び出しは変更不要）
# ──────────────────────────────────────────────────────────

def update_daily_stats(session_usage: dict, session_id: str | None = None) -> dict:
    """
    後方互換ラッパー。内部では append_session_log() を呼ぶ（read-modify-write なし）。
    戻り値は aggregate_daily_stats_from_log() による当日集計。
    """
    if session_id is None:
        import time as _time
        session_id = f"pid{os.getpid()}-{int(_time.time())}"

    append_session_log(session_usage, session_id)
    return aggregate_daily_stats_from_log()


def build_summary(day: dict) -> str:
    """Slack 通知用の1行サマリー文字列を生成する。"""
    cost_usd = day.get("cost_usd", 0.0)
    cost_jpy = cost_usd * USD_TO_JPY
    in_tok = day.get("input_tokens", 0)
    out_tok = day.get("output_tokens", 0)
    cr_tok = day.get("cache_read_tokens", 0)
    sessions = day.get("sessions", 0)
    today = day.get("last_updated", "")[:10] or datetime.now(JST).strftime("%Y-%m-%d")

    parts = [
        f"${cost_usd:.4f}（≈¥{cost_jpy:.0f}）",
        f"入力 {in_tok:,} / 出力 {out_tok:,} tok",
    ]
    if cr_tok > 0:
        parts.append(f"キャッシュ読 {cr_tok:,} tok")
    parts.append(f"{sessions} セッション")

    return f"💴 {today} コスト累計: {' | '.join(parts)}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code セッション日次コスト計算ツール"
    )
    parser.add_argument(
        "--transcript-path",
        help="トランスクリプト JSONL ファイルのパス（未指定時は stdin の JSON から取得）",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="当日サマリー文字列のみを stdout に出力する",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="stats ファイルを更新しない（ドライラン用）",
    )
    parser.add_argument(
        "--flush",
        action="store_true",
        help="cost_log.jsonl を集計して daily_cost_stats.json に書き出す（19:00 スロット用）",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="cost_log.jsonl の 30 日超行を削除する（--flush と組み合わせて使用）",
    )
    args = parser.parse_args()

    # --flush: 19:00 スロット用バッチ処理
    if args.flush:
        day = flush_to_stats_json()
        # 月次集計レポートをローカル更新（永続化は commit_cost_telemetry.py・#242）
        reports = write_monthly_reports()
        if reports:
            print(f"[calc_daily_cost] 月次レポート更新: {', '.join(p.name for p in reports)}", file=sys.stderr)
        if args.rotate:
            removed = rotate_log()
            if removed > 0:
                print(f"[calc_daily_cost] {removed} 件の古い行を削除しました", file=sys.stderr)
        if args.summary_only:
            print(build_summary(day))
        else:
            today = datetime.now(JST).strftime("%Y-%m-%d")
            print(json.dumps(day, ensure_ascii=False, indent=2))
        return

    transcript_path = args.transcript_path
    session_id: str | None = None

    # stdin から transcript_path を取得（Stop フック経由）
    if not transcript_path and not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            transcript_path = hook_input.get("transcript_path")
            # transcript_path のファイル名ステムを SESSION_ID として使う
            if transcript_path:
                session_id = Path(transcript_path).stem
        except json.JSONDecodeError:
            print("[calc_daily_cost] JSONDecodeError: stdin から transcript_path を取得できませんでした", file=sys.stderr)
        except Exception as exc:
            print(f"[calc_daily_cost] stdin 読み込みエラー: {exc}", file=sys.stderr)

    if not transcript_path or not os.path.exists(transcript_path):
        if args.summary_only:
            # トランスクリプトなし: 当日の既存累計のみ表示（cost_log.jsonl から集計）
            day = aggregate_daily_stats_from_log()
            if day["sessions"] > 0:
                print(build_summary(day))
            else:
                # フォールバック: daily_cost_stats.json から読む（移行期対応）
                stats_file = get_stats_file()
                today = datetime.now(JST).strftime("%Y-%m-%d")
                if stats_file.exists():
                    try:
                        stats = json.loads(stats_file.read_text(encoding="utf-8"))
                        day_legacy = stats.get(today)
                        if day_legacy:
                            print(build_summary(day_legacy))
                            return
                    except Exception:
                        pass
                print("💴 コスト情報なし（トランスクリプト未取得）")
        return

    # 使用量を集計
    usage = parse_transcript(transcript_path)
    usage["cost_usd"] = calc_cost(usage)

    if not args.no_update:
        # session_id が未取得の場合は transcript_path から生成
        if session_id is None:
            session_id = _generate_session_id(transcript_path)
        day = update_daily_stats(usage, session_id)
    else:
        # 更新なし: セッション分のみで仮計算
        today = datetime.now(JST).strftime("%Y-%m-%d")
        day = {
            "sessions": 1,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_write_tokens": int(usage.get("cache_write_tokens", 0)),
            "cache_read_tokens": int(usage.get("cache_read_tokens", 0)),
            "cost_usd": usage["cost_usd"],
            "last_updated": today,
        }

    if args.summary_only:
        print(build_summary(day))
    else:
        print(json.dumps(day, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
