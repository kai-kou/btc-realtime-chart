#!/usr/bin/env python3
"""check_hot_budget.py（汎用ベース）

Hot 層（`.claude/rules/`）の実測サイズを機械チェックするツール（Issue #469）。

背景: `.claude/rules/` の実測が記載予算を 16% 超過した状態が数週間、誰にも検知されず
放置されていた（#467 の作業中に偶然発覚）。増減ログ（`docs/rules/token-optimization-rules.md`
「予算の増減ログ」）は「Hot 層ファイルを追加・追記する PR で 1 行足す」という **人手の運用ルール**
だけに依存しており、守られなくても誰も気づけなかった（今回と同じ「超過が見えない」の再発）。

また `.claude/rules/` だけを数えていたため、**毎ターン常駐するもう一方のコスト**が見えていなかった
（Issue #493）。`.claude/skills/*/SKILL.md` と `.claude/commands/*.md` の frontmatter `description` は
セッション冒頭の一覧に全件展開されるため常時コンテキストに乗るが、集計対象外だった。その結果
「スキルを増やしても Hot 予算に影響しない」という誤った前提が実際の採否判断を歪めた。
本ツールはこれを **参考値として併記** する（rules 側の予算判定の挙動は変えない）。

本ツールは token-optimization-rules.md 自身が定義する「再棚卸しの合図」2 条件を機械判定する:
  ① 増減ログの行数が閾値（既定 4 行 = 基準 1 行 + 追加 3 行）に到達
  ② 実測が「基準」行の値を 10% 以上超過

あわせて「実測とログ最新行の乖離」（ログ更新漏れ＝今回の根本原因そのもの）も検出する。

使い方:
  python3 tools/check_hot_budget.py            # 判定のみ（超過・乖離があれば exit 1）
  python3 tools/check_hot_budget.py --quiet    # 終了コードのみ欲しいとき（stdout 抑制）
  python3 tools/check_hot_budget.py --self-test  # パースロジックのセルフテスト（テーブル書式変更の検知用）

終了コード: 0=予算内・ログ整合 / 1=再棚卸しの合図あり、またはログ未更新の疑い / 2=ツール異常
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOT_DIR = REPO_ROOT / ".claude" / "rules"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"
BUDGET_DOC = REPO_ROOT / "docs" / "rules" / "token-optimization-rules.md"

OVERAGE_THRESHOLD = 0.10  # ②実測が基準比+10%以上
ROW_COUNT_THRESHOLD = 4  # ①増減ログの行数（基準1行+追加3行）

# 増減ログの行: | 2026-08-19 | 79,432 B | +10,719 B | 説明... |
ROW_RE = re.compile(
    r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([\d,]+)\s*B\s*\|\s*([^\|]+?)\s*\|"
)


def measured_hot_bytes() -> int:
    total = 0
    for f in sorted(HOT_DIR.glob("*.md")):
        total += len(f.read_bytes())
    return total


def _frontmatter_description(path: Path) -> str:
    """`---` で囲まれた frontmatter の description 値を返す（無ければ空文字）。

    description は複数行にまたがることがある（次のトップレベルキー、または frontmatter の
    終端まで）。YAML パーサに依存せず、行頭インデントの有無だけで継続行を判定する。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    body: list[str] = []
    collecting = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if collecting:
            # インデント付き、または key: を持たない行は description の継続
            if line.startswith((" ", "\t")) or ":" not in line.split("#")[0]:
                body.append(line.strip())
                continue
            break
        if line.startswith("description:"):
            collecting = True
            body.append(line[len("description:"):].strip())
    return " ".join(b for b in body if b)


def measured_description_bytes() -> tuple[int, int]:
    """(スキル description の合計バイト数, 対象ファイル数) を返す。

    commands の description も同じ理屈で常駐するため合算する。
    """
    total = 0
    count = 0
    targets: list[Path] = []
    if SKILLS_DIR.is_dir():
        targets += sorted(SKILLS_DIR.glob("*/SKILL.md"))
    if COMMANDS_DIR.is_dir():
        targets += sorted(COMMANDS_DIR.glob("*.md"))
    for f in targets:
        desc = _frontmatter_description(f)
        if desc:
            total += len(desc.encode("utf-8"))
            count += 1
    return total, count


def parse_budget_log(text: str) -> list[tuple[str, int, str]]:
    """「予算の増減ログ」テーブルの行を [(日付, 実測B, 差分ラベル), ...] で返す。"""
    rows = []
    in_table = False
    for line in text.splitlines():
        if "予算の増減ログ" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("| 日付"):
            continue
        if line.startswith("|---") or line.startswith("|---:"):
            continue
        m = ROW_RE.match(line.strip())
        if m:
            date, measured, diff_label = m.groups()
            rows.append((date, int(measured.replace(",", "")), diff_label))
            continue
        if in_table and line.strip() and not line.strip().startswith("|"):
            break  # テーブル終了
    return rows


def self_test() -> int:
    """parse_budget_log() のセルフテスト（テーブル書式が変わったときの沈黙失敗を検知する）。"""
    sample = (
        "#### 予算の増減ログ\n"
        "\n"
        "| 日付 | 実測 | 差分 | 追加の正当化 / 相殺 |\n"
        "|---|---:|---:|---|\n"
        "| 2026-08-04 | 68,713 B | 基準 | 説明1 |\n"
        "| 2026-08-19 | 79,432 B | +10,719 B | 説明2、カンマ,を含む説明 |\n"
        "\n"
        "**記載予算は...**\n"
    )
    rows = parse_budget_log(sample)
    assert rows == [
        ("2026-08-04", 68713, "基準"),
        ("2026-08-19", 79432, "+10,719 B"),
    ], f"parse_budget_log が想定外の結果を返しました: {rows}"

    empty_rows = parse_budget_log("見出しのみで表なし\n")
    assert empty_rows == [], f"表が無いのに行を検出しました: {empty_rows}"

    # frontmatter description の抽出（1 行 / 複数行 / 不在 / frontmatter なし）
    import tempfile

    def _desc(text: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(text)
            tmp = Path(fh.name)
        try:
            return _frontmatter_description(tmp)
        finally:
            tmp.unlink()

    got = _desc("---\nname: a\ndescription: 一行の説明\n---\n本文\n")
    assert got == "一行の説明", f"1 行 description の抽出に失敗: {got!r}"

    got = _desc("---\nname: a\ndescription: 前半\n  後半\nmodel: sonnet\n---\n")
    assert got == "前半 後半", f"複数行 description の抽出に失敗: {got!r}"

    got = _desc("---\nname: a\nmodel: sonnet\n---\n")
    assert got == "", f"description 不在なのに値を返しました: {got!r}"

    got = _desc("# frontmatter なし\ndescription: これは本文\n")
    assert got == "", f"frontmatter が無いのに値を返しました: {got!r}"

    print("[hot-budget] self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="stdout を抑制し終了コードのみ返す")
    ap.add_argument("--self-test", action="store_true", help="パースロジックのセルフテストのみ実行")
    args = ap.parse_args()

    if args.self_test:
        try:
            return self_test()
        except AssertionError as e:
            print(f"[hot-budget] self-test NG: {e}", file=sys.stderr)
            return 1

    def out(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if not HOT_DIR.is_dir():
        print(f"[hot-budget] ERROR: {HOT_DIR} が存在しません", file=sys.stderr)
        return 2
    if not BUDGET_DOC.is_file():
        print(f"[hot-budget] ERROR: {BUDGET_DOC} が存在しません", file=sys.stderr)
        return 2

    actual = measured_hot_bytes()
    rows = parse_budget_log(BUDGET_DOC.read_text(encoding="utf-8"))
    if not rows:
        print(f"[hot-budget] ERROR: {BUDGET_DOC} の増減ログを解析できませんでした", file=sys.stderr)
        return 2

    baseline_rows = [r for r in rows if r[2] == "基準"]
    baseline = baseline_rows[-1][1] if baseline_rows else rows[0][1]
    latest_logged = rows[-1][1]

    triggers: list[str] = []

    overage_rate = (actual - baseline) / baseline if baseline else 0.0
    if overage_rate >= OVERAGE_THRESHOLD:
        triggers.append(
            f"実測 {actual:,}B が基準 {baseline:,}B を {overage_rate:.0%} 超過（再棚卸しの合図 ②・閾値 {OVERAGE_THRESHOLD:.0%}）"
        )

    if len(rows) >= ROW_COUNT_THRESHOLD:
        triggers.append(
            f"増減ログが {len(rows)} 行に到達（再棚卸しの合図 ①・閾値 {ROW_COUNT_THRESHOLD} 行）"
        )

    drift = actual - latest_logged
    if drift != 0:
        triggers.append(
            f"実測 {actual:,}B がログ最新行 {latest_logged:,}B と不一致（差分 {drift:+,}B）"
            " → Hot 層ファイルの追加・追記が増減ログに未記録の可能性。1 行追加してください"
        )

    out(f"[hot-budget] 実測: {actual:,}B / 基準: {baseline:,}B / ログ最新行: {latest_logged:,}B / ログ行数: {len(rows)}")

    # 参考値: skills / commands の description も毎ターン常駐する（#493）。
    # rules 側の予算判定には影響させない（閾値を設けるかは実測を見てから判断する）。
    desc_bytes, desc_count = measured_description_bytes()
    if desc_count:
        out(
            f"[hot-budget] 参考: description 常駐 {desc_bytes:,}B（{desc_count} 件）"
            f" / rules + description 合計 {actual + desc_bytes:,}B"
        )

    if triggers:
        out("[hot-budget] NG:")
        for t in triggers:
            out(f"  - {t}")
        return 1

    out("[hot-budget] OK（予算内・ログ整合）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[hot-budget] checker error: {e}", file=sys.stderr)
        sys.exit(2)
