#!/usr/bin/env python3
"""discussion_review_trigger.py — Layer 2 議論型レビューの自動トリガー（判定器）。

PR の差分行数またはラベルに基づいて Layer 2 議論型レビューの要否を判定する。
pr-review-watcher スキルが PR 作成後に呼び出す（Issue #97）。

既定（ネイティブ経路・Issue #193）: トリガー該当時は「実行プラン JSON」を stdout に出力して
終了する。呼び出し元のエージェントがこのプランを使って discussion-review スキル
（ネイティブ Agent Teams）を実行する。本スクリプトはサブプロセスを起動しない。

--legacy 指定時（フォールバック）: 旧経路（run_discussion_review.py = claude -p 駆動）を
サブプロセスとして直接起動する。ネイティブ経路が成立しない場合のみ使う。

トリガー条件（いずれか 1 つで起動）:
  - 差分行数（追加 + 削除）が TRIGGER_DIFF_LINES（300行）以上
  - PR ラベルに TRIGGER_LABELS（type:security / type:breaking-change）が含まれる
  - high_risk: フック・CI・権限境界・認証関連パスの変更（detect_pr_diff_type）。
    判定は tools/detect_pr_diff_type.py の assess_risk() を再利用する（認証・秘密情報パス・
    公開 API / スキーマ / DB 関連パス・フック・CI・権限境界パス・差分 500 行以上・変更ファイル 20 件以上の
    いずれかで true。
    Issue #627 対策 D「リスク階層化」）。--changed-files 指定時はそのパス一覧から判定し、
    --high-risk で明示指定もできる（自動判定との OR）

## クラウド環境での使い方（gh CLI 不可・MCP ツールで事前取得必須）

クラウド実行環境では gh CLI の GraphQL/REST が無効なため、エージェントが
mcp__github__pull_request_read で取得した値を引数として渡す（--changed-files のパス一覧から
high_risk も自動判定される）:

  python3 tools/discussion_review_trigger.py \\
      --pr 42 \\
      --diff-lines 450 \\
      --labels "type:improvement" \\
      --changed-files "tools/foo.py,docs/bar.md"

  # high_risk を明示指定する場合（detect_pr_diff_type 等で事前判定済みのとき）
  python3 tools/discussion_review_trigger.py \\
      --pr 42 --diff-lines 10 --labels "" --changed-files "" --high-risk

## ローカル環境での使い方（gh CLI 有効時）

  python3 tools/discussion_review_trigger.py --pr 42
  python3 tools/discussion_review_trigger.py --pr 42 --dry-run

## セルフテスト

  python3 tools/discussion_review_trigger.py --self-test
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "tools" / "discussion_specs" / "code_review.json"
TRIGGER_DIFF_LINES = 300
TRIGGER_LABELS = {"type:security", "type:breaking-change"}

# 高リスク判定（フック・CI・権限境界・認証・秘密情報パス等）は detect_pr_diff_type.py の
# assess_risk() を再利用する（判定ロジックの二重実装を避ける・Issue #627 対策 D「リスク階層化」）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from detect_pr_diff_type import assess_risk as _assess_risk
except ImportError:
    # ツール自体が無い場合のみ黙って無効化（行数・ラベルの既存 2 条件で代替カバレッジがある）
    _assess_risk = None
except Exception as _e:  # noqa: BLE001
    print(f"⚠️ detect_pr_diff_type の読み込みに失敗（high_risk 判定を無効化）: {_e}", file=sys.stderr)
    _assess_risk = None


def _get_repo() -> str:
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else ""


def _gh(*args: str, repo: str = "") -> tuple[int, str]:
    repo_flag = ["-R", repo] if repo else []
    result = subprocess.run(
        ["gh", *args, *repo_flag],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return result.returncode, result.stdout.strip()


def get_pr_info_gh(pr_number: int, repo: str) -> dict:
    """gh CLI で PR 情報を取得する（ローカル環境用）。"""
    rc, out = _gh("pr", "view", str(pr_number),
                  "--json", "labels,additions,deletions,headRefName,number",
                  repo=repo)
    if rc != 0:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def get_changed_files_gh(pr_number: int, repo: str) -> list[str]:
    """gh CLI で変更ファイル一覧を取得する（ローカル環境用）。"""
    rc, out = _gh("pr", "diff", str(pr_number), "--name-only", repo=repo)
    if rc != 0:
        return []
    return [f for f in out.splitlines() if f.strip()]


def detect_high_risk_from_files(changed_files: list[str], diff_lines: int) -> tuple[bool, list[str]]:
    """変更ファイルパス一覧から high_risk を判定する（クラウド環境・--changed-files 指定時用）。

    detect_pr_diff_type.assess_risk() を再利用する。import 失敗時・changed_files 未指定時は
    False を返す（既存の行数・ラベル条件で代替カバレッジがあるため、判定不能を高リスク扱いには
    しない）。
    """
    if _assess_risk is None or not changed_files:
        return False, []
    risk = _assess_risk(changed_files, diff_lines)
    return bool(risk.get("high_risk", False)), list(risk.get("risk_reasons", []))


def resolve_high_risk(explicit: bool, detected: bool) -> bool:
    """CLI の --high-risk（明示指定）と自動判定の OR。

    main() の配線ミス（例: 判定結果の握り潰し）をセルフテストで検出できるよう関数に切り出す。
    """
    return bool(explicit or detected)


def decide(diff_lines: int, labels: set[str], detected_high_risk: bool,
           explicit_high_risk: bool) -> tuple[bool, str, bool]:
    """起動要否の最終判定（main() のクラウド経路・gh 経路の共通部分）。

    resolve_high_risk → should_trigger の配線をここに集約し、セルフテストが main() と同じ経路を
    通るようにする（main() 側だけを壊しても検出できない、という穴を塞ぐ・#627 レビュー指摘）。
    """
    high_risk = resolve_high_risk(explicit_high_risk, detected_high_risk)
    trigger, reason = should_trigger(diff_lines, labels, high_risk=high_risk)
    return trigger, reason, high_risk


def get_high_risk_gh() -> bool:
    """gh CLI が使えるローカル環境用: detect_pr_diff_type.py --risk-only の判定結果を使う。

    ローカルにチェックアウト済みの git（既定 base=origin/main・head=HEAD）が前提。
    サブプロセス失敗時は False で継続する（既存の行数・ラベル条件のカバレッジに委ねる）。
    """
    try:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "detect_pr_diff_type.py"), "--risk-only"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def should_trigger(diff_lines: int, labels: set[str], high_risk: bool = False) -> tuple[bool, str]:
    """Layer 2 議論型レビューの起動要否を判定する（3 条件のいずれか 1 つで起動）。

    条件: ① PR ラベルに TRIGGER_LABELS が含まれる ② 差分行数が TRIGGER_DIFF_LINES 以上
    ③ high_risk=True（detect_pr_diff_type.assess_risk() によるフック・CI・権限境界・
    認証・秘密情報パス等の判定。Issue #627 対策 D「リスク階層化」）。
    """
    matched = labels & TRIGGER_LABELS
    if matched:
        return True, f"ラベル {sorted(matched)} 検出"
    if diff_lines >= TRIGGER_DIFF_LINES:
        return True, f"差分 {diff_lines} 行（閾値 {TRIGGER_DIFF_LINES} 行）"
    if high_risk:
        return True, "high_risk: フック・CI・権限境界・認証関連パスの変更（detect_pr_diff_type）"
    return False, f"差分 {diff_lines} 行・対象ラベルなし・high_risk 該当なし（閾値未達）"


# should_trigger の判定条件ごとの検証ケース（#627 対策 D: high_risk 条件追加分を含む）。
# (説明, diff_lines, labels, high_risk, 期待される起動要否, reason に含まれるべき部分文字列)
_SELF_TEST_CASES: list[tuple[str, int, set[str], bool, bool, str | None]] = [
    ("行数のみ（閾値以上）", 300, set(), False, True, "差分"),
    ("ラベルのみ（type:security）", 10, {"type:security"}, False, True, "ラベル"),
    ("high_risk のみ", 10, set(), True, True, "high_risk"),
    ("いずれも無し", 10, set(), False, False, None),
]


def run_self_test() -> int:
    """should_trigger の 4 ケース（行数のみ・ラベルのみ・high_risk のみ・いずれも無し）と、
    CLI 配線の 5 ケース（decide による明示 / 自動判定の合成 3 件・detect_high_risk_from_files のパス判定 2 件）を検証する。
    """
    passed, failed = 0, 0
    for desc, diff_lines, labels, high_risk, expect_trigger, reason_substr in _SELF_TEST_CASES:
        trigger, reason = should_trigger(diff_lines, labels, high_risk=high_risk)
        ok = trigger == expect_trigger and (reason_substr is None or reason_substr in reason)
        if ok:
            passed += 1
        else:
            failed += 1
            print(
                f"FAIL: {desc}（diff_lines={diff_lines}, labels={labels}, high_risk={high_risk}）\n"
                f"  expected trigger={expect_trigger} reason_substr={reason_substr!r}\n"
                f"  got      trigger={trigger} reason={reason!r}",
                file=sys.stderr,
            )
    # CLI 配線の検証（#627 レビュー指摘）: 明示フラグと自動判定の合成、パス一覧からの high_risk 判定。
    # detect の「フックパス」ケースは assess_risk の import が壊れると False になり、ここで FAIL する。
    extra_cases = [
        ("decide: 明示 --high-risk のみで起動", decide(10, set(), False, True)[0], True),
        ("decide: 自動判定のみで起動", decide(10, set(), True, False)[0], True),
        ("decide: いずれも無しは起動しない", decide(10, set(), False, False)[0], False),
        ("detect: フックパス", detect_high_risk_from_files([".claude/hooks/x.sh"], 10)[0], True),
        ("detect: ドキュメントのみ", detect_high_risk_from_files(["docs/a.md"], 10)[0], False),
    ]
    for desc, got, expect in extra_cases:
        if got == expect:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {desc}: got={got} expected={expect}", file=sys.stderr)
    total = len(_SELF_TEST_CASES) + len(extra_cases)
    print(f"セルフテスト: {passed} passed, {failed} failed / {total} cases")
    return 0 if failed == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Layer 2 議論型レビュー自動トリガー（Issue #97 / high_risk 条件は #627 対策 D）",
    )
    parser.add_argument("--pr", type=int, default=None, help="PR 番号（--self-test 時は不要）")
    parser.add_argument("--dry-run", action="store_true",
                        help="判定のみ・実際にはレビューを実行しない")
    # クラウド環境用: mcp__github__pull_request_read で取得した値を直接渡す
    parser.add_argument("--diff-lines", type=int, default=None,
                        help="差分行数（追加+削除）。省略時は gh CLI で取得を試みる")
    parser.add_argument("--labels", default="",
                        help="カンマ区切りのラベル名一覧。省略時は gh CLI で取得を試みる")
    parser.add_argument("--changed-files", default="",
                        help="カンマ区切りの変更ファイルパス一覧。省略時は gh CLI で取得を試みる。"
                             "--diff-lines 指定時はこのパス一覧から high_risk も判定する")
    parser.add_argument("--high-risk", action="store_true",
                        help="high_risk を明示指定する（detect_pr_diff_type 等で事前判定済みの"
                             "場合に使う。自動判定結果との OR）")
    parser.add_argument("--legacy", action="store_true",
                        help="旧経路（run_discussion_review.py = claude -p）を直接起動する（フォールバック用）")
    parser.add_argument("--self-test", action="store_true",
                        help="should_trigger のセルフテストを実行して終了する（--pr 不要）")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    if args.pr is None:
        parser.error("--pr は必須です（--self-test 実行時を除く）")

    risk_reasons: list[str] = []

    # 引数で直接提供された場合はそれを使う（クラウド環境）
    if args.diff_lines is not None:
        diff_lines = args.diff_lines
        labels = {la.strip() for la in args.labels.split(",") if la.strip()}
        changed_files = [f.strip() for f in args.changed_files.split(",") if f.strip()]
        detected_high_risk, risk_reasons = detect_high_risk_from_files(changed_files, diff_lines)
    else:
        # gh CLI で取得を試みる（ローカル環境）
        repo = _get_repo()
        pr_info = get_pr_info_gh(args.pr, repo)
        if not pr_info:
            print(
                f"⚠️ PR #{args.pr} の情報を取得できませんでした。\n"
                "クラウド環境では --diff-lines / --labels / --changed-files を指定してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        diff_lines = pr_info.get("additions", 0) + pr_info.get("deletions", 0)
        labels = {la["name"] for la in pr_info.get("labels", [])}
        changed_files = get_changed_files_gh(args.pr, repo)
        detected_high_risk = get_high_risk_gh()

    trigger, reason, high_risk = decide(diff_lines, labels, detected_high_risk, args.high_risk)
    if not trigger:
        print(f"ℹ️ Layer 2 レビュー不要: {reason}")
        sys.exit(0)

    # 実行プラン JSON（stdout）と混ざらないよう、進捗ログは stderr へ出す
    detail = f"（{'; '.join(risk_reasons)}）" if high_risk and risk_reasons else ""
    print(f"🔍 Layer 2 レビュー起動: {reason}{detail}", file=sys.stderr)

    if args.dry_run:
        print(f"(dry-run: 実行しません。high_risk={high_risk})")
        sys.exit(0)

    # 変更ファイルのうちリポジトリに存在するものだけターゲットに含める
    existing = [f for f in changed_files if (REPO_ROOT / f).exists()]
    targets = ",".join(existing) if existing else ""

    if not args.legacy:
        # ネイティブ経路（既定・Issue #193）: 実行プランを出力し、呼び出し元エージェントが
        # discussion-review スキル（ネイティブ Agent Teams）でこのプランを実行する。
        fallback_command = (
            f"python3 tools/discussion_review_trigger.py --pr {args.pr} "
            f"--diff-lines {diff_lines} --labels \"{','.join(sorted(labels))}\" "
            f"--changed-files \"{','.join(changed_files)}\" --legacy"
        )
        if args.high_risk:
            fallback_command += " --high-risk"
        plan = {
            "action": "run_native_discussion_review",
            "skill": "discussion-review",
            "id": f"pr-{args.pr}",
            "spec": str(SPEC_PATH),
            "targets": existing,
            "rounds": 2,
            "reason": reason,
            "high_risk": high_risk,
            "fallback_command": fallback_command,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("▶ 上記プランに従い discussion-review スキル（ネイティブ）で Layer 2 を実行してください。",
              file=sys.stderr)
        sys.exit(0)

    # --legacy: 旧経路（claude -p 駆動）をサブプロセス起動（フォールバック）
    target_args = ["--targets", targets] if targets else []
    rc = subprocess.call(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "run_discussion_review.py"),
            "--id", f"pr-{args.pr}",
            "--spec", str(SPEC_PATH),
            *target_args,
            "--rounds", "2",
        ],
        cwd=str(REPO_ROOT),
    )

    if rc != 0:
        print(
            f"⚠️ Layer 2 レビュー失敗（exit {rc}）。"
            "Layer 1 / Layer 3 レビューで継続します。",
            file=sys.stderr,
        )
        sys.exit(rc)

    print("✅ Layer 2 レビュー完了")


if __name__ == "__main__":
    main()
