#!/usr/bin/env python3
"""check_datetime_tz.py — 表示・記録系の TZ 未指定 datetime 残存チェック（Issue #80）

`docs/rules/datetime-rules.md` の SSOT に従い、表示・記録に使うと
コンテナのローカル TZ に依存して不定になる **TZ 未指定（naive）の datetime** を検出する。

検出対象（naive = 表示・記録に使うと壊れる）:
  - `datetime.utcnow()`        … naive UTC（非推奨・datetime-rules.md §2 で明示禁止）
  - `datetime.now()`（引数なし） … naive ローカル時刻
  - `datetime.today()`（引数なし）… naive ローカル時刻（now() と同じく TZ 未指定）

検出しない（正しい使い方）:
  - `datetime.now(timezone.utc)` / `datetime.now(JST)` … aware。機械処理用 UTC も含む
  - `# tz-ok` を同じ行に書いた箇所            … 明示的に許可（レビュー済みの例外）

使い方:
  python3 tools/check_datetime_tz.py            # リポジトリ全体の *.py を検査
  python3 tools/check_datetime_tz.py --changed  # main との差分 *.py のみ検査
  違反があれば exit 1（CI / セルフレビューでガードレール化できる）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (?:\w+\.)? で `datetime.now()` と `_dt.datetime.now()` の両方を拾う。
# 引数が空（\s*）のときだけマッチ → aware な `.now(tz)` は対象外。
NAIVE_NOW = re.compile(r"(?:\w+\.)?datetime\.now\(\s*\)")
NAIVE_UTCNOW = re.compile(r"(?:\w+\.)?datetime\.utcnow\b")  # utcnow は常に naive。参照渡し(default=...utcnow)も検出
NAIVE_TODAY = re.compile(r"(?:\w+\.)?datetime\.today\(\s*\)")
ALLOW_MARKER = "# tz-ok"

# 自分自身とドキュメント例示は検査しない（ルール文の例まで弾かないため）。
EXCLUDE_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env"}
EXCLUDE_FILES = {"check_datetime_tz.py"}


def _py_files_all() -> list[Path]:
    out = []
    for p in REPO_ROOT.rglob("*.py"):
        if p.name in EXCLUDE_FILES:
            continue
        if EXCLUDE_PARTS & set(p.relative_to(REPO_ROOT).parts):
            continue
        out.append(p)
    return out


def _py_files_changed() -> list[Path]:
    try:
        base = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "merge-base", "HEAD", "origin/main"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip() or "origin/main"
        diff = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "--name-only", base, "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if diff.returncode != 0:
            # 失敗を握りつぶすと「検査スキップ」が「検出なし(PASS)」に化ける偽陰性になるため警告する。
            print(f"Warning: git diff 失敗（--changed 検査をスキップ）: {diff.stderr.strip()}", file=sys.stderr)
            return []
        names = diff.stdout.splitlines()
    except Exception as e:
        print(f"Warning: 変更ファイル取得に失敗（--changed 検査をスキップ）: {e}", file=sys.stderr)
        return []
    files = []
    for n in names:
        if not n.endswith(".py") or n in EXCLUDE_FILES:
            continue
        p = REPO_ROOT / n
        if p.exists() and not (EXCLUDE_PARTS & set(Path(n).parts)):
            files.append(p)
    return files


def scan(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return hits
    for i, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        if NAIVE_NOW.search(line) or NAIVE_UTCNOW.search(line) or NAIVE_TODAY.search(line):
            hits.append((i, line.strip()))
    return hits


def main() -> int:
    changed = "--changed" in sys.argv
    files = _py_files_changed() if changed else _py_files_all()

    violations = 0
    for f in sorted(files):
        for lineno, snippet in scan(f):
            violations += 1
            rel = f.relative_to(REPO_ROOT)
            print(f"{rel}:{lineno}: TZ 未指定 datetime（表示・記録に使うと不定）: {snippet}")

    scope = "変更 .py" if changed else "全 .py"
    if violations:
        print(
            f"\n❌ {violations} 件の TZ 未指定 datetime を検出（{scope}・{len(files)} ファイル走査）。\n"
            "   表示・記録用途なら datetime.now(JST)、機械処理用 UTC なら datetime.now(timezone.utc) を使う。\n"
            "   レビュー済みの正当な例外は行末に `# tz-ok` を付けて抑制できる（datetime-rules.md §2）。",
            file=sys.stderr,
        )
        return 1
    print(f"✅ TZ 未指定 datetime は検出なし（{scope}・{len(files)} ファイル走査）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
