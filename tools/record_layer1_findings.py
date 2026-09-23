#!/usr/bin/env python3
"""record_layer1_findings.py — Layer 1 / PR 前レビューの結果を JSONL に 1 行追記する（Issue #627 対策 E）

`code-review` スキルがレビュー 1 回（pre-pr / pr の各ラウンド）ごとに呼ぶ。GitHub API に依存しないため
クラウド（gh が 403 の環境）・ローカルのどちらでも計測が成立する。集計は `tools/layer1_findings_report.py`。

保存先: content/analytics/review/layer1_findings.jsonl（`.gitignore` で再包含済み・コミット対象）。
同一レビュー（post: pr + round / pre: branch + round + head_sha）の行が複数あれば集計側が recorded_at 最新を
採用する（追記は冪等でなくてよい）。pre 行は PR 番号を持たず、ブランチ名は PR をまたいで再利用されうるため、
head_sha（省略時は git から自動取得）が識別子になる。

Usage:
    # PR 後（Layer 1）ラウンド 2・CONFIRMED 🔴0 🟡4 ⚪0・インライン 4 件・修正 4 件
    python3 tools/record_layer1_findings.py --pr 629 --phase post --round 2 --head-sha 5ca3b84 \\
        --confirmed 0,4,0 --plausible 0 --refuted 0 --inline 4 --fixed 4 --skipped 0 \\
        --perspectives "正確性,セキュリティ,テスト・検証" --review-md not_in_base \\
        --finding "WARNING|正確性|.claude/hooks/pre-pr-create-check.sh:436|PR 前レビュー判定の裸一致"
    # PR 前（self-reviewer Step 3.5）・PR 番号は無いので branch で識別（省略時は git から自動取得）
    python3 tools/record_layer1_findings.py --phase pre --confirmed 0,12,4 --plausible 0 --fixed 13 --skipped 3 \\
        --perspectives "正確性,セキュリティ,簡素化・再利用,テスト・検証,ドキュメント整合,Spec 忠実性"
    python3 tools/record_layer1_findings.py --self-test

レコード（1 行 = 1 レビュー実行）:
    {"recorded_at": "2026-09-11 14:10 JST", "pr": 629, "branch": "claude/...", "phase": "post", "round": 2,
     "head_sha": "5ca3b84", "session_id": "...", "confirmed": {"critical": 0, "warning": 4, "nit": 0},
     "plausible": 0, "refuted": 0, "inline": 4, "fixed": 4, "skipped": 0,
     "perspectives": ["正確性", ...], "review_md": "not_in_base",
     "findings": [{"severity": "WARNING", "perspective": "正確性", "location": "path:line", "summary": "..."}]}

`--finding` は CONFIRMED の各指摘を `severity|観点|path:line|要旨` で 1 件ずつ渡す（集計側の「同種指摘 2 回以上」
検出に使う。省略可・件数の集計には影響しない）。

Exit code: 0 = 追記成功 / 1 = 引数不正・書き込み失敗
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "content" / "analytics" / "review" / "layer1_findings.jsonl"
PHASES = ("pre", "post")
SEVERITIES = ("CRITICAL", "WARNING", "NIT")
REVIEW_MD_STATES = ("applied", "not_in_base", "none")
FINDING_KEYS = ("severity", "perspective", "location", "summary")


def parse_triplet(text: str) -> dict:
    """'critical,warning,nit' → {"critical": c, "warning": w, "nit": n}（非負整数 3 つ以外は ValueError）。"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"--confirmed は 'critical,warning,nit' の非負整数 3 つ（例 0,4,0）: {text!r}")
    c, w, n = (int(p) for p in parts)
    return {"critical": c, "warning": w, "nit": n}


def parse_finding(text: str) -> dict:
    """'severity|観点|path:line|要旨' → dict（要旨に '|' を含んでよい。4 項目未満・空項目は ValueError）。"""
    parts = [p.strip() for p in text.split("|", 3)]
    if len(parts) != 4 or not all(parts):
        raise ValueError(f"--finding は 'severity|観点|path:line|要旨' の 4 項目: {text!r}")
    return _validate_finding({"severity": parts[0], "perspective": parts[1],
                              "location": parts[2], "summary": parts[3]})


def _validate_finding(item: dict) -> dict:
    if not isinstance(item, dict) or any(k not in item for k in FINDING_KEYS):
        raise ValueError(f"finding は {FINDING_KEYS} の 4 キーを持つ dict: {item!r}")
    sev = str(item["severity"]).strip().upper()
    if sev not in SEVERITIES:
        raise ValueError(f"finding の severity は CRITICAL / WARNING / NIT: {item['severity']!r}")
    out = {k: str(item[k]).strip() for k in FINDING_KEYS}
    out["severity"] = sev
    if not all(out.values()):
        raise ValueError(f"finding の各項目は空にできない: {item!r}")
    return out


def _git_output(args: "list[str]", cwd: Path = ROOT) -> str:
    """git コマンドの stdout（失敗・不在・タイムアウトは空文字。記録の識別子用なので例外にしない）。"""
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                             timeout=10, cwd=cwd)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def current_branch(cwd: Path = ROOT) -> str:
    return _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def current_head(cwd: Path = ROOT) -> str:
    return _git_output(["rev-parse", "--short", "HEAD"], cwd)


def _non_negative(name: str, value: int) -> int:
    if value is None or value < 0:
        raise ValueError(f"{name} は 0 以上の整数")
    return int(value)


def build_record(args: argparse.Namespace, now: "datetime | None" = None) -> dict:
    """引数を検証してレコード dict を組み立てる（検証 NG は ValueError）。"""
    if args.phase not in PHASES:
        raise ValueError(f"--phase は {PHASES} のいずれか: {args.phase!r}")
    if args.phase == "post" and args.pr is None:
        raise ValueError("--phase post には --pr が必須（PR 前レビューは --phase pre）")
    if args.pr is not None and args.pr < 1:
        raise ValueError("--pr は 1 以上")
    if args.round < 1:
        raise ValueError("--round は 1 以上")
    if args.review_md not in REVIEW_MD_STATES:
        raise ValueError(f"--review-md は {REVIEW_MD_STATES} のいずれか: {args.review_md!r}")
    confirmed = parse_triplet(args.confirmed)
    total = sum(confirmed.values())
    plausible = _non_negative("--plausible", args.plausible)
    refuted = _non_negative("--refuted", args.refuted)
    inline = _non_negative("--inline", args.inline)
    fixed = _non_negative("--fixed", args.fixed)
    skipped = _non_negative("--skipped", args.skipped)
    if inline > total:
        raise ValueError(f"--inline（{inline}）は CONFIRMED 合計（{total}）以下")
    if fixed + skipped > total:
        raise ValueError(f"--fixed + --skipped（{fixed + skipped}）は CONFIRMED 合計（{total}）以下")
    findings = [parse_finding(f) for f in (args.finding or [])]
    if len(findings) > total:
        raise ValueError(f"finding の件数（{len(findings)}）が CONFIRMED 合計（{total}）を超えている")
    perspectives = [p.strip() for p in (args.perspectives or "").split(",") if p.strip()]
    branch = (args.branch or "").strip() or current_branch()
    if args.phase == "pre" and not branch:
        raise ValueError("--phase pre は --branch が必須（git から自動取得できなかった）")
    stamp = (now or datetime.now(JST)).strftime("%Y-%m-%d %H:%M JST")
    return {
        "recorded_at": stamp,
        "pr": args.pr,
        "branch": branch,
        "phase": args.phase,
        "round": args.round,
        # pre 行は PR 番号が無くブランチ名は PR をまたいで再利用されうる（同名ブランチを main から切り直す運用）ため、
        # head_sha を識別子に含める（集計側の dedup キー）。省略時は git から自動取得する
        "head_sha": (args.head_sha or "").strip() or current_head(),
        "session_id": (args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")).strip(),
        "confirmed": confirmed,
        "plausible": plausible,
        "refuted": refuted,
        "inline": inline,
        "fixed": fixed,
        "skipped": skipped,
        "perspectives": perspectives,
        "review_md": args.review_md,
        "findings": findings,
    }


def append_record(record: dict, path: Path = DEFAULT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, help="PR 番号（--phase post では必須）")
    ap.add_argument("--branch", help="ブランチ名（省略時は git から自動取得。--phase pre の識別子）")
    ap.add_argument("--phase", choices=PHASES, help="pre = PR 前レビュー（Step 3.5）/ post = PR 後 Layer 1")
    ap.add_argument("--round", type=int, default=1, help="PR 後レビューのラウンド（既定 1）")
    ap.add_argument("--head-sha", default="", help="レビュー対象の head SHA（省略時は git rev-parse --short HEAD）")
    ap.add_argument("--session-id", default="", help="省略時は $CLAUDE_CODE_SESSION_ID")
    ap.add_argument("--confirmed", default="0,0,0", help="CONFIRMED の件数 'critical,warning,nit'")
    ap.add_argument("--plausible", type=int, default=0)
    ap.add_argument("--refuted", type=int, default=0, help="反証で除外した候補数")
    ap.add_argument("--inline", type=int, default=0, help="インラインコメントにした件数（post のみ意味を持つ）")
    ap.add_argument("--fixed", type=int, default=0, help="修正した CONFIRMED 件数")
    ap.add_argument("--skipped", type=int, default=0, help="理由付きで見送った CONFIRMED 件数")
    ap.add_argument("--perspectives", default="", help="実施した観点（カンマ区切り）")
    ap.add_argument("--review-md", default="none", choices=REVIEW_MD_STATES,
                    help="REVIEW.md の適用状態（applied / not_in_base / none）")
    ap.add_argument("--finding", action="append", help="CONFIRMED 1 件 'severity|観点|path:line|要旨'（複数可）")
    ap.add_argument("--path", help=f"追記先（既定 {DEFAULT_PATH.relative_to(ROOT)}）")
    ap.add_argument("--self-test", action="store_true")
    return ap


def run_self_test() -> int:
    import tempfile
    ok = True
    ap = build_parser()
    fixed_now = datetime(2026, 9, 11, 14, 10, tzinfo=JST)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sub" / "l1.jsonl"
        # 1. post レコードの追記（finding 付き）
        args = ap.parse_args([
            "--pr", "629", "--phase", "post", "--round", "2", "--head-sha", "5ca3b84",
            "--confirmed", "0,4,0", "--plausible", "0", "--refuted", "0", "--inline", "4",
            "--fixed", "4", "--skipped", "0", "--perspectives", "正確性, セキュリティ,テスト・検証",
            "--review-md", "not_in_base", "--branch", "feat/x",
            "--finding", "WARNING|正確性|.claude/hooks/pre-pr-create-check.sh:436|裸の「実施」一致 | 補足",
        ])
        rec = build_record(args, now=fixed_now)
        append_record(rec, path)
        # 2. pre レコード（PR 番号なし・branch 明示）
        args2 = ap.parse_args(["--phase", "pre", "--branch", "feat/x", "--confirmed", "0,12,4",
                               "--fixed", "13", "--skipped", "3", "--perspectives", "正確性"])
        append_record(build_record(args2, now=fixed_now), path)
        lines = path.read_text(encoding="utf-8").splitlines()
        back = [json.loads(ln) for ln in lines]
        if len(back) != 2:
            print(f"FAIL: 追記行数 {len(back)}（期待 2）"); ok = False
        elif back[0]["pr"] != 629 or back[0]["phase"] != "post" or back[0]["round"] != 2:
            print(f"FAIL: post レコードの識別子: {back[0]}"); ok = False
        elif back[0]["confirmed"] != {"critical": 0, "warning": 4, "nit": 0} or back[0]["inline"] != 4:
            print(f"FAIL: 件数フィールド: {back[0]}"); ok = False
        elif back[0]["perspectives"] != ["正確性", "セキュリティ", "テスト・検証"]:
            print(f"FAIL: 観点の分割: {back[0]['perspectives']}"); ok = False
        elif back[0]["findings"] != [{"severity": "WARNING", "perspective": "正確性",
                                       "location": ".claude/hooks/pre-pr-create-check.sh:436",
                                       "summary": "裸の「実施」一致 | 補足"}]:
            print(f"FAIL: finding の分割（要旨内の '|' を保持）: {back[0]['findings']}"); ok = False
        elif back[0]["recorded_at"] != "2026-09-11 14:10 JST":
            print(f"FAIL: recorded_at の JST 書式: {back[0]['recorded_at']}"); ok = False
        elif back[1]["pr"] is not None or back[1]["phase"] != "pre" or back[1]["branch"] != "feat/x":
            print(f"FAIL: pre レコード: {back[1]}"); ok = False
        else:
            print("PASS: post / pre レコードの追記と読み戻し（JST・観点分割・finding の '|' 保持）")

        # 3. 検証 NG（引数不正）→ ValueError
        bad_cases = [
            (["--phase", "post", "--confirmed", "0,1,0"], "post に --pr 無し"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,x,0"], "confirmed が非整数"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--fixed", "1", "--skipped", "1"],
             "fixed + skipped が CONFIRMED 合計超"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--inline", "2"], "inline が合計超"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--finding", "WARNING|正確性|要旨だけ"],
             "finding が 3 項目"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--finding", "BLOCKER|a|b|c"],
             "finding の severity が不正"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,0,0", "--finding", "NIT|a|b|c"],
             "finding 件数が CONFIRMED 合計超"),
            # 負数（inline > total の判定は -1 > total が常に偽で素通りするため、_non_negative が唯一のガード）
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--inline", "-1"], "inline が負数"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--plausible", "-2"], "plausible が負数"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--fixed", "-1"], "fixed が負数"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--refuted", "-1"], "refuted が負数"),
            (["--pr", "1", "--phase", "post", "--confirmed", "0,1,0", "--skipped", "-1"], "skipped が負数"),
            # 下限 1（round=0 は集計側で round=1 に読み替えられ dedup キーが衝突する）
            (["--pr", "0", "--phase", "post", "--confirmed", "0,1,0"], "pr が 0"),
            (["--pr", "1", "--phase", "post", "--round", "0", "--confirmed", "0,1,0"], "round が 0"),
        ]
        bad_failures = 0
        for argv, label in bad_cases:
            try:
                build_record(ap.parse_args(argv), now=fixed_now)
                print(f"FAIL: {label} が ValueError にならない"); ok = False; bad_failures += 1
            except ValueError:
                pass
        if not bad_failures:
            print(f"PASS: 引数不正 {len(bad_cases)} 件が ValueError（サイレントに壊れた行を残さない）")

        # 4. severity の大文字化（--finding の小文字指定）
        args3 = ap.parse_args(["--pr", "2", "--phase", "post", "--confirmed", "0,0,1",
                               "--finding", "nit|簡素化|a.py:1|重複", "--branch", "b"])
        rec3 = build_record(args3, now=fixed_now)
        if rec3["findings"] == [{"severity": "NIT", "perspective": "簡素化", "location": "a.py:1", "summary": "重複"}]:
            print("PASS: --finding の severity を大文字化して保存")
        else:
            print(f"FAIL: severity の正規化: {rec3['findings']}"); ok = False

    print("=== self-test:", "PASS ===" if ok else "FAIL ===")
    return 0 if ok else 1


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    if args.self_test:
        return run_self_test()
    if not args.phase:
        ap.error("--phase は必須（pre / post）")
    try:
        record = build_record(args)
        path = append_record(record, Path(args.path) if args.path else DEFAULT_PATH)
    except ValueError as e:
        print(f"[record-layer1] ERROR: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[record-layer1] ERROR: 書き込み失敗: {e}", file=sys.stderr)
        return 1
    ident = f"pr={record['pr']}" if record["pr"] is not None else f"branch={record['branch']}"
    total = sum(record["confirmed"].values())
    print(f"[record-layer1] 追記: {path}（{ident} phase={record['phase']} round={record['round']}"
          f" CONFIRMED {total} 件 / 修正 {record['fixed']} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
