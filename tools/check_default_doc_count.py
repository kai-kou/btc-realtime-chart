#!/usr/bin/env python3
"""AC-4（生成するドキュメントの既定数は 1 本）の静的検査。

判定条件の正本は docs/02_requirements/document-catalog.md §3.3 の S-1〜S-4。
本スクリプトはその静的検査 (1) を機械化したもので、動線実測 (2)（R-1 / R-2）は
動線を 1 回通すたびに人／エージェントが同節のチェックリストで確認する。

S-1: docs/framework-templates/ に commit されている雛形本体が product-brief.md 1 本だけ（D-19）
S-2: document-catalog.md §2 の Tier 0 表のデータ行が 1 行（D-18）
S-3: 同 Tier 1 表が 9 行あり、各行の「生成条件（トリガー）」列が空でない（FR-2）
S-4: upstream-flow スキルが「既定 1 本」「明示要求時のみ追加」の 2 規律を保持（FR-2 / C-2）

Usage:
  python3 tools/check_default_doc_count.py                 # 違反があれば exit 1
  python3 tools/check_default_doc_count.py --root /path    # 検査対象ルートを指定
  python3 tools/check_default_doc_count.py --self-test     # 自己検査
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tempfile

CATALOG_PATH = "docs/02_requirements/document-catalog.md"
TEMPLATES_DIR = "docs/framework-templates"
SKILL_PATH = ".claude/skills/upstream-flow/SKILL.md"

TIER0_HEADING = "### Tier 0:"
TIER1_HEADING = "### Tier 1:"
TIER0_EXPECTED_ROWS = 1
TIER1_EXPECTED_ROWS = 9
TRIGGER_COLUMN_KEYWORD = "生成条件"
ALLOWED_TEMPLATE_BODIES = {"product-brief.md"}
SKILL_ANCHORS = (
    "既定で生成するのは要件ドキュメント 1 本だけ",
    "明示的に要求したときだけ",
)


def _normalize(text: str) -> str:
    """強調記号・空白・改行の揺れを無視して比較するための正規化。"""
    return re.sub(r"[*`\s]+", "", text)


def _section_lines(text: str, heading_prefix: str) -> list[str] | None:
    """見出しから次の同レベル以上の見出しまでの本文行を返す。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("### ") or line.startswith("## "):
            break
        body.append(line)
    return body


def _parse_table(body: list[str]) -> tuple[list[str], list[list[str]]]:
    """Markdown テーブルをヘッダ行とデータ行に分解する（区切り行は捨てる）。"""
    rows = [line.strip() for line in body if line.strip().startswith("|")]
    if len(rows) < 2:
        return [], []
    header = [cell.strip() for cell in rows[0].strip("|").split("|")]
    data = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]
    return header, data


def check_s1(root: pathlib.Path) -> list[str]:
    directory = root / TEMPLATES_DIR
    if not directory.is_dir():
        return [f"S-1: {TEMPLATES_DIR}/ が存在しない"]
    # サブディレクトリへ置く経路も先行配置なので再帰で走査する（D-19 の回避路を作らない）。
    bodies = {
        path.relative_to(directory).as_posix()
        for path in sorted(directory.rglob("*.md"))
        if path.name != "README.md" and not path.name.endswith(".example.md")
    }
    extra = sorted(bodies - ALLOWED_TEMPLATE_BODIES)
    missing = sorted(ALLOWED_TEMPLATE_BODIES - bodies)
    problems = []
    if extra:
        problems.append(
            "S-1: Tier 0 以外の雛形本体が先行配置されている（D-19 違反）: " + ", ".join(extra)
        )
    if missing:
        problems.append("S-1: Tier 0 の雛形が見つからない: " + ", ".join(missing))
    return problems


def check_s2_s3(root: pathlib.Path) -> list[str]:
    catalog = root / CATALOG_PATH
    if not catalog.is_file():
        return [f"S-2/S-3: {CATALOG_PATH} が存在しない"]
    text = catalog.read_text(encoding="utf-8")
    problems = []

    tier0 = _section_lines(text, TIER0_HEADING)
    if tier0 is None:
        problems.append(f"S-2: 「{TIER0_HEADING}」の節が見つからない")
    else:
        _, rows = _parse_table(tier0)
        if len(rows) != TIER0_EXPECTED_ROWS:
            problems.append(
                f"S-2: Tier 0（既定生成）が {len(rows)} 行ある（期待: {TIER0_EXPECTED_ROWS} 行・D-18）"
            )

    tier1 = _section_lines(text, TIER1_HEADING)
    if tier1 is None:
        problems.append(f"S-3: 「{TIER1_HEADING}」の節が見つからない")
    else:
        header, rows = _parse_table(tier1)
        if len(rows) != TIER1_EXPECTED_ROWS:
            problems.append(
                f"S-3: Tier 1 が {len(rows)} 行ある（期待: {TIER1_EXPECTED_ROWS} 行）"
            )
        trigger_index = next(
            (i for i, name in enumerate(header) if TRIGGER_COLUMN_KEYWORD in name), None
        )
        if trigger_index is None:
            problems.append(f"S-3: Tier 1 表に「{TRIGGER_COLUMN_KEYWORD}」列が無い")
        else:
            for row in rows:
                name = row[0] if row else "?"
                # 列数が合わない行は列の対応が保証できない（セル内の未エスケープ `|` 等）。
                # 黙って読み飛ばすとトリガー未定義を見逃すため、違反として報告する（fail-closed）。
                if len(row) != len(header):
                    problems.append(
                        f"S-3: Tier 1 の {name} の列数がヘッダと一致しない"
                        f"（{len(row)} 列 / ヘッダ {len(header)} 列。セル内の `|` はエスケープする）"
                    )
                    continue
                if not row[trigger_index]:
                    problems.append(f"S-3: Tier 1 の {name} に生成条件（トリガー）が無い")
    return problems


def check_s4(root: pathlib.Path) -> list[str]:
    skill = root / SKILL_PATH
    if not skill.is_file():
        return [f"S-4: {SKILL_PATH} が存在しない"]
    normalized = _normalize(skill.read_text(encoding="utf-8"))
    return [
        f"S-4: upstream-flow スキルから規律が失われている: 「{anchor}」"
        for anchor in SKILL_ANCHORS
        if _normalize(anchor) not in normalized
    ]


def run_checks(root: pathlib.Path) -> list[str]:
    return check_s1(root) + check_s2_s3(root) + check_s4(root)


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_fixture(root: pathlib.Path) -> None:
    """AC-4 を満たす最小リポジトリを組み立てる。"""
    _write(root / TEMPLATES_DIR / "README.md", "# templates\n")
    _write(root / TEMPLATES_DIR / "product-brief.md", "# product brief\n")
    _write(root / TEMPLATES_DIR / "product-brief.example.md", "# example\n")
    tier1_rows = "\n".join(
        f"| **T1-{i}** | doc{i} | 責務{i} | トリガー{i} | `C-1` |" for i in range(1, 10)
    )
    _write(
        root / CATALOG_PATH,
        "## 2. カタログ本体（10 本）\n\n"
        "### Tier 0: 既定生成（1 本）\n\n"
        "| ID | ドキュメント | 唯一の正本として持つもの | 対応 `C-n` |\n"
        "|---|---|---|---|\n"
        "| **T0-1** | 要件定義書 | 要件 ID | `C-1` |\n\n"
        "### Tier 1: トリガー式（9 本）\n\n"
        "| ID | ドキュメント | 唯一の正本として持つもの | 生成条件（トリガー） | 対応 `C-n` |\n"
        "|---|---|---|---|---|\n"
        f"{tier1_rows}\n\n"
        "### 派生スロット（カタログに数えない）\n",
    )
    _write(
        root / SKILL_PATH,
        "🔴 **既定で生成するのは要件ドキュメント 1 本だけ**。\n"
        "追加は、ユーザーが **明示的に要求したときだけ**\n行う。\n",
    )


def self_test() -> int:
    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _build_fixture(root)
        expect(run_checks(root) == [], f"合格 fixture が落ちた: {run_checks(root)}")

        # S-1: Tier 1 の雛形を先行配置すると落ちる
        extra = root / TEMPLATES_DIR / "lean-canvas.md"
        _write(extra, "# lean canvas\n")
        expect(any(v.startswith("S-1") for v in run_checks(root)), "S-1 が先行配置を検出しない")
        extra.unlink()
        expect(run_checks(root) == [], "S-1 の復元に失敗")

        # S-1: サブディレクトリへ逃がした先行配置も検出する
        nested = root / TEMPLATES_DIR / "lean-canvas" / "template.md"
        _write(nested, "# lean canvas\n")
        expect(
            any(v.startswith("S-1") for v in run_checks(root)),
            "S-1 がサブディレクトリの先行配置を検出しない",
        )
        nested.unlink()
        nested.parent.rmdir()
        expect(run_checks(root) == [], "S-1（サブディレクトリ）の復元に失敗")

        # S-2: 既定生成が 2 本になると落ちる
        catalog = root / CATALOG_PATH
        original = catalog.read_text(encoding="utf-8")
        catalog.write_text(
            original.replace(
                "| **T0-1** | 要件定義書 | 要件 ID | `C-1` |",
                "| **T0-1** | 要件定義書 | 要件 ID | `C-1` |\n| **T0-2** | 追加 | 何か | `C-1` |",
            ),
            encoding="utf-8",
        )
        expect(any(v.startswith("S-2") for v in run_checks(root)), "S-2 が既定 2 本を検出しない")

        # S-3: トリガー列が空になると落ちる
        catalog.write_text(
            original.replace("| トリガー1 |", "|  |"), encoding="utf-8"
        )
        expect(any(v.startswith("S-3") for v in run_checks(root)), "S-3 が空トリガーを検出しない")
        # S-3: セル内の未エスケープ `|` で列がずれると落ちる（トリガー空の見逃し防止）
        catalog.write_text(
            original.replace(
                "| **T1-9** | doc9 | 責務9 | トリガー9 | `C-1` |",
                "| **T1-9|extra** | doc9 | 責務9 |  | `C-1` |",
            ),
            encoding="utf-8",
        )
        expect(any(v.startswith("S-3") for v in run_checks(root)), "S-3 が列ずれを検出しない")
        catalog.write_text(original, encoding="utf-8")

        # S-4: スキルから規律が消えると落ちる
        skill = root / SKILL_PATH
        skill_text = skill.read_text(encoding="utf-8")
        skill.write_text(skill_text.replace("明示的に要求したときだけ", "都度判断する"), encoding="utf-8")
        expect(any(v.startswith("S-4") for v in run_checks(root)), "S-4 が規律の欠落を検出しない")
        skill.write_text(skill_text, encoding="utf-8")
        expect(run_checks(root) == [], "fixture の復元に失敗")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: check_default_doc_count self-test (7 cases)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AC-4（既定 1 本）の静的検査")
    parser.add_argument("--root", default=None, help="検査対象のリポジトリルート（既定: 本スクリプトの親ディレクトリ）")
    parser.add_argument("--self-test", action="store_true", help="チェッカ自身の自己検査を実行する")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    root = pathlib.Path(args.root) if args.root else pathlib.Path(__file__).resolve().parent.parent
    violations = run_checks(root)
    if violations:
        print("FAIL: AC-4（既定 1 本）の静的検査に違反があります")
        for violation in violations:
            print(f"  - {violation}")
        print("判定条件の正本: docs/02_requirements/document-catalog.md §3.3")
        return 1
    print("PASS: AC-4 静的検査 S-1〜S-4（既定 1 本・先行配置なし・トリガー定義あり）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
