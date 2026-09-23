#!/usr/bin/env python3
"""週次ゲートの時刻窓と cron 発火間隔の整合検査（docs/routines.md §2.4・Issue #106）。

`docs/routines.md` は R-1 の週次ゲート（`workflow-health-check` 完全版 /
`self-improvement-loop` 整理モード）を **時刻窓だけで判定する** 設計を採る。状態ファイルを
持たない代わりに、窓幅と cron の発火間隔が噛み合っていることが前提になる。

その前提が崩れた実例が Issue #106: 窓が 00〜02 時台（3 時間）、cron が `24 */6 * * *`
（6 時間間隔・JST 03:24 / 09:24 / 15:24 / 21:24）で、**発火枠が一度も窓に入らず週次ゲートが
稼働開始以来 1 度も実行されなかった**。窓幅が発火間隔より狭いと、判定結果が cron のアンカー
時刻に依存する（§1 のとおり分は作成時刻にアンカーされ、時も 6 時間間隔のどこに落ちるかは
作成時刻で決まる）。

不変条件（これを機械検査する）:

  1. 窓幅 == 発火間隔 — 窓幅が狭いと週 0 回になりうる（#106 の実害）。広いと同じ窓に 2 回
     以上の発火が入り、週次ゲートが重複実行されうる。等号のときだけ「週にちょうど 1 回」が
     アンカー時刻に関わらず保証される
  2. 24 % 発火間隔 == 0 — 割り切れないと cron の発火が日付をまたぐところで不均等になり
     （例 `*/5` は 20 時 → 翌 0 時が 4 時間）、窓幅 == 間隔でも 1 回の保証が崩れる
  3. 文書内の窓定義が全て同一で、かつ **2 行以上で宣言されている** — 窓は §2.1 順位 6 と
     §2.4 に二重管理されているため、片方だけ直す事故を検出する。🔴 同一性の検査だけでは
     不十分で、片方の表記が抽出パターンから外れると「もう片方から取れた 1 件」だけが残り
     食い違い検査が素通りする（実測で再現・#106 の Layer 1 指摘）。宣言行数の下限も要求して
     fail-closed にする

既知の限界（隠さない）:
  - cron の時フィールドは `*` と `*/N` だけを解釈する。リスト表記（`0,6,12,18`）や範囲表記は
    **判定不能として違反扱いにする**（黙って通さない安全側の退行）。必要になったら解釈を足す
  - 検査対象は `docs/routines.md` に控えられた cron 文字列であり、Routines の実設定ではない
    （§1 のとおり正本は Claude の Routines 設定で、発火セッションからは読めない）。控えが実設定と
    乖離している場合は本チェッカーでは検出できない
  - 窓の抽出は表記パターン（`HH:00〜HH:59` と `` `%H` が HH〜HH ``）に依存する。別の書き方へ
    変えると宣言行数が下限を割って違反報告になる（沈黙はしない）。窓を書く箇所を増減するときは
    どちらかの表記を使う
  - この 2 表記は文書内で **現行の窓しか指せない**（不変条件 3 の副作用）。過去値や反例を同じ
    表記で書くと不一致として報告される。歴史的な記述は `00〜02 時台` のように別表記で書く

Usage:
  python3 tools/check_routine_gate_window.py
  python3 tools/check_routine_gate_window.py --file docs/routines.md
  python3 tools/check_routine_gate_window.py --self-test
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

DEFAULT_TARGET = "docs/routines.md"

# 窓の宣言に要求する最小行数。§2.1 順位 6（対象選定の表）と §2.4（判定方法）の 2 箇所が
# 二重管理になっているため、双方が抽出可能な表記で書かれていることまで確かめる（不変条件 3）。
MIN_WINDOW_MENTION_LINES = 2

# §1 の設定値テーブルの cron 行: `| cron | `24 */6 * * *`（…）|`
CRON_ROW_RE = re.compile(r"^\|\s*cron\s*\|\s*`([^`]+)`", re.MULTILINE)

# 窓定義の 2 表記。どちらも「時」の範囲を閉区間で表す
WINDOW_CLOCK_RE = re.compile(r"(\d{2}):00〜(\d{2}):59")
WINDOW_HOUR_RE = re.compile(r"`%H`\s*が\s*(\d{2})〜(\d{2})")


def parse_cron_interval_hours(cron: str) -> int | None:
    """cron 式の時フィールドから発火間隔（時間）を返す。解釈できなければ None。"""
    fields = cron.split()
    if len(fields) != 5:
        return None
    hour_field = fields[1]
    if hour_field == "*":
        return 1
    m = re.fullmatch(r"\*/(\d+)", hour_field)
    if m:
        interval = int(m.group(1))
        return interval if 1 <= interval <= 24 else None
    return None


def collect_windows(text: str) -> list[tuple[int, int, int]]:
    """窓定義を (行番号, 開始時, 終了時) で全て集める（行番号は 1-origin）。

    行番号まで返すのは、同一性だけでなく「§2.1 と §2.4 の 2 箇所で宣言されているか」を
    数えるため（不変条件 3）。同じ行に両表記が並んでいても 1 箇所として扱う。
    """
    found: list[tuple[int, int, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in (WINDOW_CLOCK_RE, WINDOW_HOUR_RE):
            for m in pattern.finditer(line):
                found.append((lineno, int(m.group(1)), int(m.group(2))))
    return found


def check_text(text: str) -> list[str]:
    """整合検査の違反メッセージを返す（空なら pass）。"""
    errors: list[str] = []

    cron_rows = CRON_ROW_RE.findall(text)
    if not cron_rows:
        return ["cron の控えが見つかりません（§1 の設定値テーブルに `| cron | `…`` 行が必要）"]
    cron = cron_rows[0]

    interval = parse_cron_interval_hours(cron)
    if interval is None:
        errors.append(
            f"cron `{cron}` の時フィールドを解釈できません"
            "（`*` と `*/N` のみ対応。判定不能は違反扱いにする・本ファイルの既知の限界）"
        )

    windows = collect_windows(text)
    if not windows:
        errors.append(
            "週次ゲートの窓定義が見つかりません"
            "（`HH:00〜HH:59` か `` `%H` が HH〜HH `` の表記が §2.1 順位 6 / §2.4 に必要）"
        )
        return errors

    distinct = sorted({(start, end) for _, start, end in windows})
    if len(distinct) > 1:
        errors.append(
            f"窓定義が文書内で一致していません: {distinct}"
            "（§2.1 順位 6 と §2.4 は同じ窓を指す必要がある）"
        )
        return errors

    mention_lines = {lineno for lineno, _, _ in windows}
    if len(mention_lines) < MIN_WINDOW_MENTION_LINES:
        errors.append(
            f"窓定義が {len(mention_lines)} 行でしか宣言されていません"
            f"（{MIN_WINDOW_MENTION_LINES} 行以上必要: §2.1 順位 6 と §2.4）。"
            "片方が抽出できない表記になると、二重管理の食い違いを検出できなくなる"
        )
        return errors

    start, end = distinct[0]
    if not (0 <= start <= 23 and 0 <= end <= 23):
        errors.append(f"窓定義の時が範囲外です: {start:02d}〜{end:02d}")
        return errors
    if end < start:
        errors.append(
            f"窓定義が日付をまたいでいます: {start:02d}〜{end:02d}"
            "（またぐ窓は幅の判定が曖昧になるため採らない）"
        )
        return errors

    width = end - start + 1
    if interval is not None:
        if width != interval:
            errors.append(
                f"窓幅 {width} 時間が発火間隔 {interval} 時間と一致しません"
                f"（cron `{cron}` / 窓 {start:02d}:00〜{end:02d}:59）。"
                "狭いと週 0 回になり（#106）、広いと重複実行されうる"
            )
        if 24 % interval != 0:
            errors.append(
                f"発火間隔 {interval} 時間が 24 を割り切らないため、cron の発火が日付をまたぐ"
                "ところで不均等になり「週にちょうど 1 回」を保証できません"
            )
    return errors


def _fixture(cron: str, primary: tuple[int, int], secondary: tuple[int, int] | None) -> str:
    """self-test 用の擬似文書。primary は §2.1 順位 6 相当行、secondary は §2.4 相当行。

    secondary に None を渡すと §2.4 相当行を省いた文書になる（宣言行数の下限を試すケース）。
    """
    lines = [
        f"| cron | `{cron}`（控え） |",
        f"| 6 | 週次ゲート | JST の月曜 {primary[0]:02d}:00〜{primary[1]:02d}:59"
        f"（`%H` が {primary[0]:02d}〜{primary[1]:02d}）のときだけ |",
    ]
    if secondary is not None:
        lines.append(
            f"**判定方法**: `%H` が {secondary[0]:02d}〜{secondary[1]:02d} のときだけ実行する。"
        )
    return "\n".join(lines) + "\n"


def _self_test() -> int:
    """内蔵テスト。違反ケースは **どのガードが発火したか** をエラー文の断片で固定する。

    🔴 pass / fail の 2 値だけを見ると、あるガードを外しても別のガードが代わりに落とすケースで
    テストが green のまま通り、**そのガード固有の否定テストにならない**（#107 の Layer 1 指摘。
    例: 幅も違う食い違いケースは同一性ガードを外しても幅不一致で落ちる / 日付をまたぐ窓は幅が
    負になるので、またぎガードを外しても必ず幅不一致で落ちる）。断片まで検証すれば、各ガードを
    無効化したときに必ずどれかのケースが落ちる。
    """
    # (ケース名, 文書, 期待するエラー文の断片。None は pass 期待)
    cases: list[tuple[str, str, str | None]] = [
        ("現行値（窓 6h / 間隔 6h・2 箇所一致）", _fixture("24 */6 * * *", (0, 5), (0, 5)), None),
        ("#106 の欠陥（窓 3h < 間隔 6h）", _fixture("24 */6 * * *", (0, 2), (0, 2)),
         "窓幅 3 時間が発火間隔 6 時間と一致しません"),
        ("窓が広すぎる（窓 12h > 間隔 6h）", _fixture("24 */6 * * *", (0, 11), (0, 11)),
         "窓幅 12 時間が発火間隔 6 時間と一致しません"),
        # 幅を揃えた食い違いにする（幅も違えると幅不一致検査が代わりに落ちて否定テストにならない）
        ("2 箇所の窓定義が食い違う（幅は同じで開始位置だけ違う）",
         _fixture("24 */6 * * *", (0, 5), (6, 11)), "窓定義が文書内で一致していません"),
        ("§2.4 側の窓宣言が消えた（抽出できない表記への変更・fail-closed）",
         _fixture("24 */6 * * *", (0, 5), None), "窓定義が 1 行でしか宣言されていません"),
        ("cron 側だけ変えて窓を直し忘れた（間隔 3h / 窓 6h）",
         _fixture("24 */3 * * *", (0, 5), (0, 5)), "窓幅 6 時間が発火間隔 3 時間と一致しません"),
        ("cron 変更に窓を追随させた（間隔 3h / 窓 3h）",
         _fixture("24 */3 * * *", (0, 2), (0, 2)), None),
        ("毎時発火（時フィールド `*` / 窓 1h）", _fixture("24 * * * *", (0, 0), (0, 0)), None),
        ("24 を割り切らない間隔（*/5 / 窓 5h）", _fixture("24 */5 * * *", (0, 4), (0, 4)),
         "24 を割り切らない"),
        ("解釈できない cron 時フィールド（リスト表記）",
         _fixture("24 0,6,12,18 * * *", (0, 5), (0, 5)), "時フィールドを解釈できません"),
        # またぎ窓は幅が負になり幅不一致でも落ちるため、断片で「またぎ」側の発火を固定する
        ("窓が日付をまたぐ", _fixture("24 */6 * * *", (22, 3), (22, 3)),
         "窓定義が日付をまたいでいます"),
        # 2 桁なら 24〜99 も表記パターンに一致するため、範囲外ガードは到達可能な入口を守る。
        # 幅 6・宣言 2 行を満たす値にして、範囲外ガードだけが発火する形にする
        ("窓定義の時が範囲外（幅と宣言行数は満たす）",
         _fixture("24 */6 * * *", (24, 29), (24, 29)), "窓定義の時が範囲外です"),
        (
            "cron の控えが無い",
            "| 6 | 週次ゲート | JST の月曜 00:00〜05:59（`%H` が 00〜05）|\n"
            "**判定方法**: `%H` が 00〜05 のときだけ実行する。\n",
            "cron の控えが見つかりません",
        ),
        ("窓定義が無い", "| cron | `24 */6 * * *`（控え） |\n週次ゲートは月曜に実行する\n",
         "週次ゲートの窓定義が見つかりません"),
    ]
    failures = 0
    for name, text, expect_fragment in cases:
        errs = check_text(text)
        if expect_fragment is None:
            ok = not errs
            expected = "pass"
        else:
            ok = any(expect_fragment in e for e in errs)
            expected = f"fail（{expect_fragment}）"
        if not ok:
            failures += 1
            print(f"[FAIL] {name}: 期待={expected} 実際={errs or 'pass'}", file=sys.stderr)
        else:
            print(f"[OK] {name}")

    # 実ファイルも self-test の対象にする（現行の docs/routines.md が不変条件を満たすこと）
    target = Path(DEFAULT_TARGET)
    if target.exists():
        errs = check_text(target.read_text(encoding="utf-8"))
        if errs:
            failures += 1
            print(f"[FAIL] {DEFAULT_TARGET}: {errs}", file=sys.stderr)
        else:
            print(f"[OK] {DEFAULT_TARGET}")

    if failures:
        print(f"self-test 失敗: {failures} 件", file=sys.stderr)
        return 1
    print("self-test 成功")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=DEFAULT_TARGET, help=f"検査対象（既定 {DEFAULT_TARGET}）")
    ap.add_argument("--self-test", action="store_true", help="内蔵テストを実行する")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    target = Path(args.file)
    if not target.exists():
        print(f"[routine-gate] 検査対象が見つかりません: {target}", file=sys.stderr)
        return 1

    errors = check_text(target.read_text(encoding="utf-8"))
    if errors:
        print(f"[routine-gate] 週次ゲートの窓と cron が整合していません（{target}）", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"[routine-gate] OK: 窓幅と発火間隔が一致しています（{target}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
