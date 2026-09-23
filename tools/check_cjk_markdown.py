#!/usr/bin/env python3
"""check_cjk_markdown.py（汎用ベース）

CLAUDE.md「Markdown 出力ルール」の機械チェック＆自動整形ツール。

ルール（SSOT: CLAUDE.md「Markdown 出力ルール」）:
  CJK テキスト内の **強調** や `コード` 等の記法前後に半角スペースを入れる
  （例: `これは **重要** です`）。

このルールは従来 self-review-checklist.md で「目視」チェック扱いだったため、
大規模ドキュメントで人手の見落としが頻発し、AI レビュアー（Gemini 等）に毎回
同種指摘を受けてレビューコストが高かった（根本原因: 規範はあるが実行支援が無い）。
本ツールで検出を機械化し、`--fix` で自動整形できるようにして再発を防ぐ。

使い方:
  python3 tools/check_cjk_markdown.py <file.md> [<file2.md> ...]   # 検出のみ（違反あれば exit 1）
  python3 tools/check_cjk_markdown.py --fix <file.md> ...          # 自動整形して上書き
  python3 tools/check_cjk_markdown.py --changed                    # 変更された .md を対象に検出
  python3 tools/check_cjk_markdown.py --fix --changed              # 変更された .md を自動整形
  python3 tools/check_cjk_markdown.py --self-test                  # セルフテスト

設計（誤検出を避けるための厳格化）:
  - フェンスドコードブロック（``` / ~~~）内は一切触らない
  - YAML フロントマター（先頭 --- ... ---）は触らない
  - 検査対象記法は **強調**（bold）と `インラインコード` の 2 種のみ
    （* 1 個の斜体は誤検出が多いので対象外）
  - 記法スパンの「外側」が CJK の「単語文字」（かな・漢字等）に直接隣接する場合のみ
    半角スペースを要求する。約物（、。「」（）等）が隣接する場合は要求しない
    （日本語の約物の前後にスペースを入れるとかえって不自然なため）

終了コード: 0=違反なし（または --fix で修正完了） / 1=違反あり（検出モード） / 2=ツール異常
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# CJK「単語文字」: ひらがな・カタカナ・漢字・全角英数など。約物は意図的に除外する。
CJK_WORD = (
    r"ぁ-ゖ"   # ひらがな
    r"ァ-ヺ"   # カタカナ
    r"ー"          # 長音符
    r"㐀-䶿"   # CJK 拡張 A
    r"一-鿿"   # CJK 統合漢字
    r"豈-﫿"   # CJK 互換漢字
    r"Ａ-Ｚ"   # 全角英大文字
    r"ａ-ｚ"   # 全角英小文字
    r"０-９"   # 全角数字
    r"가-힣"   # ハングル音節（CJK の K）
    r"ㄱ-ㅣ"   # ハングル互換字母
)
CJK_WORD_RE = re.compile(f"[{CJK_WORD}]")

# 検査対象の記法スパン: **bold** と `code`。
# bold は最短一致・改行を含まない。code はバッククォート1個で囲まれた範囲。
SPAN_RE = re.compile(r"(\*\*(?!\s)(?:[^*]|\*(?!\*))+?(?<!\s)\*\*|`[^`\n]+?`)")


def is_cjk_word(ch: str) -> bool:
    return bool(ch) and bool(CJK_WORD_RE.match(ch))


def _process_line(line: str, fix: bool) -> tuple[str, int]:
    """1 行を処理して (整形後の行, 違反件数) を返す。

    fix=False のときは行を変えず違反件数のみ数える。
    """
    violations = 0
    out: list[str] = []
    idx = 0
    for m in SPAN_RE.finditer(line):
        start, end = m.start(), m.end()
        span = m.group(0)
        # スパン前のテキストを確定（原文をそのまま積む）
        out.append(line[idx:start])
        # --- 開きの境界チェック（原文インデックスで直前の 1 文字を見る）---
        prev_ch = line[start - 1] if start > 0 else ""
        if is_cjk_word(prev_ch):
            violations += 1
            if fix:
                out.append(" ")
        out.append(span)
        # --- 閉じの境界チェック ---
        next_ch = line[end] if end < len(line) else ""
        if is_cjk_word(next_ch):
            violations += 1
            if fix:
                out.append(" ")
        idx = end
    out.append(line[idx:])
    return "".join(out), violations


def process_text(text: str, fix: bool) -> tuple[str, list[tuple[int, str]]]:
    """テキスト全体を処理。(整形後テキスト, [(行番号, 元行), ...] 違反行) を返す。"""
    lines = text.split("\n")
    in_fence = False
    fence_marker = ""
    in_frontmatter = False
    violations: list[tuple[int, str]] = []
    result: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # YAML フロントマター（先頭行が ---）
        if i == 0 and stripped == "---":
            in_frontmatter = True
            result.append(line)
            continue
        if in_frontmatter:
            result.append(line)
            if stripped == "---":
                in_frontmatter = False
            continue
        # フェンスドコードブロック開閉
        fence_match = re.match(r"^(```+|~~~+)", stripped)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            # CommonMark: 閉じフェンスは開きと同じ文字種で、同じ長さ以上のときのみ閉じる
            # （4 個以上のバッククォートで囲んだブロック内の ``` で誤閉鎖しない）
            elif marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                in_fence = False
                fence_marker = ""
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue
        new_line, count = _process_line(line, fix)
        if count > 0:
            violations.append((i + 1, line))
        result.append(new_line)

    return "\n".join(result), violations


def changed_md_files() -> list[str]:
    """git diff から変更された .md ファイル一覧を取得（origin/<default>...HEAD + 作業ツリー）。"""
    def sh(args):
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    base = "main"
    r = sh(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        base = r.stdout.strip().split("/")[-1]

    files: list[str] = []
    for args in (
        ["git", "diff", "--name-only", f"origin/{base}...HEAD"],
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        # 未追跡（git add 前の新規ファイル）も対象に含める。git diff は untracked を
        # 出力しないため、これが無いと新規 .md が PR 前整形から漏れる（#63）
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        rr = sh(args)
        if rr.returncode == 0:
            # split() ではなく splitlines()。スペースを含むパスを 1 件として扱う
            files += rr.stdout.splitlines()
    seen, out = set(), []
    for f in files:
        if f.endswith(".md") and f not in seen and Path(f).is_file():
            seen.add(f)
            out.append(f)
    return out


def check_files(paths: list[str], fix: bool) -> int:
    total_violations = 0
    fixed_files = 0
    for path in paths:
        p = Path(path)
        if not p.is_file():
            print(f"[cjk-md] スキップ（不在）: {path}", file=sys.stderr)
            continue
        # Markdown 専用ツール。.py 等を誤って渡しても内容を壊さないよう .md 限定にする
        if p.suffix.lower() not in (".md", ".markdown"):
            print(f"[cjk-md] スキップ（.md 以外）: {path}", file=sys.stderr)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            print(f"[cjk-md] スキップ（読み込み失敗: {e}）: {path}", file=sys.stderr)
            continue
        new_text, violations = process_text(text, fix)
        if not violations:
            continue
        total_violations += len(violations)
        if fix:
            p.write_text(new_text, encoding="utf-8")
            fixed_files += 1
            print(f"[cjk-md] 整形: {path}（{len(violations)} 行）")
        else:
            for ln, content in violations[:30]:
                print(f"[cjk-md] {path}:{ln}: CJK 記法の前後に半角スペース不足 → {content.strip()[:80]}")

    if fix:
        if fixed_files:
            print(f"[cjk-md] 自動整形完了: {fixed_files} ファイル / {total_violations} 行")
        else:
            print("[cjk-md] 整形対象なし（OK）")
        return 0
    if total_violations:
        print(f"\n[cjk-md] Warning: CJK 半角スペース違反 {total_violations} 行。")
        print("  → 自動整形: python3 tools/check_cjk_markdown.py --fix <file>")
        return 1
    print("[cjk-md] OK（CJK 半角スペース違反なし）")
    return 0


def self_test() -> int:
    # 注意: 入力（左）は「スペース未挿入」の原文。本ツールを self_test 行に対して
    # --fix で走らせると入力が壊れるため、check_files は .md 以外をスキップする。
    cases = [
        # (入力, 期待出力)
        ("これは" + "**重要**" + "です", "これは **重要** です"),
        ("これは `コード` です", "これは `コード` です"),  # 既に正しい → 不変
        ("`コード`" + "を使う", "`コード` を使う"),
        ("使う" + "`コード`", "使う `コード`"),
        ("英語 **bold** text", "英語 **bold** text"),  # 英数字隣接 → 不変
        ("**先頭強調**" + "から始まる", "**先頭強調** から始まる"),
        ("句点の前" + "**強調**" + "。", "句点の前 **強調**。"),  # 約物（。）はスペース不要
        ("「**強調**」", "「**強調**」"),  # 括弧（約物）はスペース不要
        ("English**bold**english", "English**bold**english"),  # 非 CJK → 不変
        ("値は" + "`x`" + "、次は" + "`y`" + "です", "値は `x`、次は `y` です"),
        ("한국어" + "**볼드**" + "입니다", "한국어 **볼드** 입니다"),  # ハングル（CJK の K）
    ]
    passed = 0
    failed = 0
    for src, expected in cases:
        got, _ = process_text(src, fix=True)
        if got == expected:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {src!r}\n  expected: {expected!r}\n  got:      {got!r}")

    # フェンス内は触らない
    fence_src = "```\nこれは**重要**です\n```\n本文の **強調** です"
    fence_got, _ = process_text(fence_src, fix=True)
    if "```\nこれは**重要**です\n```" in fence_got and "本文の **強調** です" in fence_got:
        passed += 1
    else:
        failed += 1
        print(f"FAIL(fence): {fence_got!r}")

    # 4 個のバッククォートで囲んだブロック内の ``` で誤閉鎖しない（CommonMark）
    bt = "`" * 4
    bt3 = "`" * 3
    fence4 = f"{bt}\n{bt3}\n中の**強調**は触らない\n{bt3}\n{bt}\n外の**強調**です"
    fence4_got, _ = process_text(fence4, fix=True)
    if "中の**強調**は触らない" in fence4_got and "外の **強調** です" in fence4_got:
        passed += 1
    else:
        failed += 1
        print(f"FAIL(fence4): {fence4_got!r}")

    # 検出モードで違反行が数えられること
    _, v = process_text("これは" + "**重要**" + "です", fix=False)
    if len(v) == 1:
        passed += 1
    else:
        failed += 1
        print(f"FAIL(detect): {v!r}")

    print(f"\n[cjk-md] self-test: {passed} passed / {failed} failed")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="CJK Markdown 半角スペース チェッカー＆整形")
    ap.add_argument("files", nargs="*", help="対象 .md ファイル")
    ap.add_argument("--fix", action="store_true", help="自動整形して上書き")
    ap.add_argument("--changed", action="store_true", help="git で変更された .md を対象にする")
    ap.add_argument("--self-test", action="store_true", help="セルフテストを実行")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    paths = list(args.files)
    if args.changed:
        paths += changed_md_files()
    if not paths:
        print("対象ファイルがありません（ファイル指定または --changed が必要）", file=sys.stderr)
        return 0

    return check_files(paths, args.fix)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[cjk-md] checker error: {e}", file=sys.stderr)
        sys.exit(2)
