#!/usr/bin/env python3
"""親ベース（claude-code-repository-base）との無自覚なドリフトを機械検知する（Issue #4）。

founding 議論の実測で、公開ベースと下流の間で共通ルール 53 本中 26 本（49%）が差分を持ち、
かつ同期マーカー（.claude/base-sync-state.json）が更新されないまま放置されていた事例が
確認されている。本ツールは「差分ゼロ」を強制するのではなく（下流が独自に育てるのは正常）、
差分と最終同期からの経過が **無自覚に** 積み上がっていないかを 1 コマンドで可視化し、
閾値超過時は fail-closed（非ゼロ終了）で知らせる。

比較対象は scripts/apply-to-repo.sh の SYNC_PATHS と同じ集合（両ファイルの一覧がずれると
本ツールの判定と実際の同期対象が食い違うため、SYNC_PATHS を変更したらこちらも更新すること）。

Usage:
  python3 tools/check_base_drift.py                       # 既定の閾値で検査
  python3 tools/check_base_drift.py --max-days 30 --max-diff-files 20
  python3 tools/check_base_drift.py --json                # 機械可読出力
  python3 tools/check_base_drift.py --self-test            # 実 clone なしの単体テスト
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# scripts/apply-to-repo.sh の SYNC_PATHS と同じ集合を維持すること（同期対象の定義が正本）。
SYNC_PATHS = [
    "docs/rules",
    ".claude/rules",
    ".claude/hooks",
    ".claude/skills",
    ".claude/agents",
    ".claude/output-styles",
    ".claude/commands",
    ".claude-plugin",
    "tools",
    "scripts",
    "modules.yaml",
    ".mcp.json",
    "requirements.txt",
    "config/claude_code_spec_sync.yaml",
    "config/broker_workflows.json.example",
]

DEFAULT_BASE_REPO = "kai-kou/claude-code-repository-base"
DEFAULT_REF = "main"
DEFAULT_MAX_DAYS = 30
# diff 件数は既定では判定に使わない（None）。実測では初回同期直後でも数十件の差分が
# 出る（プレースホルダ置換・下流カスタマイズ由来）ため、既定で閾値を持たせると常時
# fail する「オオカミ少年」になる。「差分ゼロが目的ではない」という Issue の方針どおり、
# 主軸は最終同期からの経過日数にし、diff 件数の閾値は --max-diff-files を明示した
# ときだけ効かせる（情報としては常に件数・一覧を出す）。
DEFAULT_MAX_DIFF_FILES = None


@dataclass
class DriftResult:
    diff_files: list[str] = field(default_factory=list)
    parent_only: list[str] = field(default_factory=list)
    child_only: list[str] = field(default_factory=list)
    same_count: int = 0

    @property
    def diff_count(self) -> int:
        return len(self.diff_files)


def _iter_files(root: Path, rel: str) -> list[str]:
    """SYNC_PATHS の 1 エントリを root 配下の相対ファイルパス一覧へ展開する。"""
    target = root / rel
    if target.is_file():
        return [rel]
    if not target.is_dir():
        return []
    out = []
    for p in sorted(target.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            out.append(str(p.relative_to(root)))
    return out


def compute_drift(base_root: Path, target_root: Path, sync_paths: list[str] | None = None) -> DriftResult:
    """base_root（親）と target_root（子）の SYNC_PATHS 配下を比較する。"""
    sync_paths = sync_paths if sync_paths is not None else SYNC_PATHS
    base_files: set[str] = set()
    target_files: set[str] = set()
    for rel in sync_paths:
        base_files.update(_iter_files(base_root, rel))
        target_files.update(_iter_files(target_root, rel))

    result = DriftResult()
    result.parent_only = sorted(base_files - target_files)
    result.child_only = sorted(target_files - base_files)

    for rel in sorted(base_files & target_files):
        b = (base_root / rel).read_bytes()
        t = (target_root / rel).read_bytes()
        if b != t:
            result.diff_files.append(rel)
        else:
            result.same_count += 1
    return result


def days_since(applied_at: str) -> float | None:
    try:
        dt = datetime.fromisoformat(applied_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def read_sync_state(root: Path) -> dict:
    state_file = root / ".claude" / "base-sync-state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[WARN] {state_file} の解析に失敗しました（未同期扱いにフォールバック）: {e}", file=sys.stderr)
        return {}


_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def clone_base(base_repo: str, ref: str, dest: Path) -> None:
    if not _REPO_SLUG_RE.match(base_repo):
        raise ValueError(f"--base は owner/repo 形式で指定してください: {base_repo!r}")
    url = f"https://github.com/{base_repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, url, str(dest)],
        check=True, capture_output=True, text=True, timeout=120,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="検査対象（子）リポジトリのルート（既定: カレントディレクトリ）")
    parser.add_argument("--base", default=DEFAULT_BASE_REPO, help="親ベースリポジトリの owner/repo")
    parser.add_argument("--ref", default=DEFAULT_REF, help="親ベースの ref（既定: main）")
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS, help="最終同期からの経過日数の閾値（超過で fail-closed）")
    parser.add_argument("--max-diff-files", type=int, default=DEFAULT_MAX_DIFF_FILES, help="差分ファイル数の閾値（超過で fail-closed。既定は None＝判定に使わず情報表示のみ）")
    parser.add_argument("--json", action="store_true", help="機械可読な JSON で出力する")
    parser.add_argument("--self-test", action="store_true", help="実 clone を行わない単体テストを実行する")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    if args.max_days <= 0:
        parser.error("--max-days は正の整数で指定してください")
    if args.max_diff_files is not None and args.max_diff_files <= 0:
        parser.error("--max-diff-files は正の整数で指定してください")

    root = Path(args.root).resolve()
    state = read_sync_state(root)
    applied_at = state.get("applied_at")
    elapsed_days = days_since(applied_at) if applied_at else None

    with tempfile.TemporaryDirectory() as tmp:
        base_root = Path(tmp) / "base"
        try:
            clone_base(args.base, args.ref, base_root)
        except ValueError as e:
            print(f"[NG] {e}", file=sys.stderr)
            sys.exit(1)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
            print(f"[NG] 親ベース（{args.base}@{args.ref}）の取得に失敗しました: {detail}", file=sys.stderr)
            sys.exit(1)
        drift = compute_drift(base_root, root)

    problems: list[str] = []
    if elapsed_days is None:
        problems.append(
            f".claude/base-sync-state.json の applied_at が読めません（未同期または壊れています。fail-closed）"
        )
    elif elapsed_days > args.max_days:
        problems.append(f"最終同期から {elapsed_days:.1f} 日経過しています（閾値 {args.max_days} 日）")
    if args.max_diff_files is not None and drift.diff_count > args.max_diff_files:
        problems.append(f"共通ファイルの差分が {drift.diff_count} 件あります（閾値 {args.max_diff_files} 件）")

    ok = not problems
    if args.json:
        print(json.dumps({
            "ok": ok,
            "base_repo": args.base,
            "ref": args.ref,
            "applied_at": applied_at,
            "elapsed_days": elapsed_days,
            "diff_count": drift.diff_count,
            "diff_files": drift.diff_files,
            "parent_only": drift.parent_only,
            "child_only": drift.child_only,
            "same_count": drift.same_count,
            "problems": problems,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"── 親ベースとのドリフト検査（{args.base}@{args.ref}）──")
        print(f"最終同期: {applied_at or '不明'}" + (f"（{elapsed_days:.1f} 日前）" if elapsed_days is not None else ""))
        print(f"共通ファイル: 一致 {drift.same_count} 件 / 差分 {drift.diff_count} 件")
        if drift.diff_files:
            print("  差分ファイル:")
            for rel in drift.diff_files:
                print(f"    - {rel}")
        print(f"親にしか無いファイル: {len(drift.parent_only)} 件")
        for rel in drift.parent_only:
            print(f"    - {rel}")
        print(f"子にしか無いファイル: {len(drift.child_only)} 件（下流固有資産。異常ではない）")
        for rel in drift.child_only:
            print(f"    - {rel}")
        if ok:
            print("\n[OK] ドリフトは閾値内です")
        else:
            print("\n[NG] ドリフト検査で閾値超過を検出しました:", file=sys.stderr)
            for p in problems:
                print(f"    - {p}", file=sys.stderr)

    sys.exit(0 if ok else 1)


# --- 自己テスト（実 clone なし） ---

def _run_self_test() -> int:
    cases = [_case_identical, _case_diff_and_unique_files, _case_missing_state]
    failed = 0
    for case_fn in cases:
        desc, passed = case_fn()
        print(f"{'PASS' if passed else 'FAIL'}: {desc}")
        if not passed:
            failed += 1
    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS")
    return 0


def _case_identical() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        base_root, target_root = Path(a), Path(b)
        (base_root / "docs" / "rules").mkdir(parents=True)
        (target_root / "docs" / "rules").mkdir(parents=True)
        (base_root / "docs" / "rules" / "x.md").write_text("同じ内容", encoding="utf-8")
        (target_root / "docs" / "rules" / "x.md").write_text("同じ内容", encoding="utf-8")
        drift = compute_drift(base_root, target_root, sync_paths=["docs/rules"])
        ok = drift.diff_count == 0 and not drift.parent_only and not drift.child_only and drift.same_count == 1
        return "内容が完全一致 → 差分ゼロ・親/子のみ双方ゼロ", ok


def _case_diff_and_unique_files() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        base_root, target_root = Path(a), Path(b)
        (base_root / "docs" / "rules").mkdir(parents=True)
        (target_root / "docs" / "rules").mkdir(parents=True)
        (base_root / "docs" / "rules" / "x.md").write_text("親の内容", encoding="utf-8")
        (target_root / "docs" / "rules" / "x.md").write_text("子で変更済み", encoding="utf-8")
        (base_root / "docs" / "rules" / "parent-only.md").write_text("親にしか無い", encoding="utf-8")
        (target_root / "docs" / "rules" / "child-only.md").write_text("子にしか無い", encoding="utf-8")
        drift = compute_drift(base_root, target_root, sync_paths=["docs/rules"])
        ok = (
            drift.diff_files == ["docs/rules/x.md"]
            and drift.parent_only == ["docs/rules/parent-only.md"]
            and drift.child_only == ["docs/rules/child-only.md"]
        )
        return "差分・親のみ・子のみファイルがそれぞれ正しく区別される", ok


def _case_missing_state() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        state = read_sync_state(root)
        ok = state == {} and days_since("not-a-date") is None
        return "同期マーカー無し / 不正な日付 → fail-closed 側（None）を返す", ok


if __name__ == "__main__":
    main()
