#!/usr/bin/env python3
"""フェーズ逆流検知ゲート（docs/rules/phase-boundary-rules-detail.md §4）。

実装フェーズ中に「製品として何を作るかそのもの」に関わる判断（要件の再定義・
スコープの拡張・技術選定のやり直し等）が湧いた場合の機械ゲート。

置ける機構は形式的な差分検知のみ: 要件ドキュメント（`product-brief.md` 形式。
FR-n / NFR-n / AC-n を §3〜§4 に、決定ログ D-n を §5 に同居させる単一ファイル
雛形）に **新しい要件 ID** を追加する差分を含む PR には、**同じ差分に** 対応する
決定ログエントリ（D-n 行の追加）が無ければ違反として報告する。

「新しい要件 ID」は **変更前スナップショット（差分の pre-image）に存在しない ID** で
判定する（Issue #61）。追加行に現れた ID をそのまま新規とみなすと、要件ドキュメントの
本文で既存 ID を参照しただけの差分が常に違反として報告され、正当な差分がブロックされる。
pre-image は `git show {base_ref}:{path}` の全文（`base_ref` の既定は
`merge-base(origin/{default}, HEAD)`）と、差分の削除行から集める。

🔴 差分のコンテキスト行を pre-image に **使ってはならない**。呼び出し元
`self_review_check.py` は `origin/{default}...HEAD`・`--cached`・作業ツリーの 3 経路の
差分を連結して渡すため、経路ごとに pre-image が違う。作業ツリー差分のコンテキスト行には
「ブランチで既にコミット済みの新規 ID」が含まれ、それを既存扱いすると真の違反を
見逃す（実測で再現・セルフテストの連結ケースで固定）。

既知の限界（隠さない・phase-boundary-rules-detail.md §4 と要旨を一致させる。
逐語一致ではないので、片方を直したらもう片方の要旨も確認すること）:
  - 形式的な ID 差分でしか発火しない。要件 ID を追加せず実装だけをこっそり
    進めるケースや、既存 ID の意味だけを変えるケースは捕捉できない
  - 決定ログを内包しない「分割型」（`prd.md` + `open-questions.md` のような、
    tsukuru-studio 自身が採る 2 ファイル構成）はスコープ外。`product-brief.md`
    形式（ファイル名に "product-brief" を含むもの）だけを対象とする
  - pre-image に「まだ定義されていない ID」への言及（`TODO: FR-2 を足す` 等）が
    既にあると、その ID の実際の追加を新規と見なせない。ID 表記だけを手がかりに
    する以上、言及と定義は区別できない
  - `base_ref` を解決できない環境（git が無い・origin が無い）では pre-image が
    削除行だけに退行し、Issue #61 の偽陽性が再び起こりうる（検知側に倒す安全側の
    退行であり、沈黙はしない）

Usage:
  git diff origin/main...HEAD | python3 tools/check_phase_reversal_gate.py
  python3 tools/check_phase_reversal_gate.py --diff-file some.diff
  python3 tools/check_phase_reversal_gate.py --self-test
"""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from typing import Callable, Optional

DOC_PATTERN = re.compile(r"product-brief.*\.md$")
REQ_ID_RE = re.compile(r"\b(?:FR|NFR|AC)-\d+\b")
DECISION_ID_RE = re.compile(r"\bD-\d+\b")

FILE_HEADER_RE = re.compile(r"^\+\+\+ [ab]/(.+)$")
OLD_FILE_HEADER_RE = re.compile(r"^--- [ab]/(.+)$")

BaselineResolver = Callable[[str], Optional[str]]


def parse_diff(diff_text: str) -> dict[str, dict[str, list[str]]]:
    """unified diff を { filepath: {"added": [...], "removed": [...], "old_paths": [...]} } に分解する。

    `+++ /dev/null`（ファイル削除）は `FILE_HEADER_RE` にマッチしないため、ここで
    明示的に `current_file` を None へリセットする。省くと削除ファイルの差分行が
    直前のファイルへ誤って混入する（レビュー指摘・実測で確認済み）。

    `old_paths` はリネーム時の旧パス（`--- a/{old}` が `+++ b/{new}` と異なる場合）。
    pre-image は旧パスにしか存在しないため、これを持たないと `git show {ref}:{new}` が
    必ず失敗し、リネームを伴う差分で Issue #61 の偽陽性が再発する。

    コンテキスト行は **意図的に集めない**（モジュール docstring の 🔴 を参照）。
    """
    files: dict[str, dict[str, list[str]]] = {}
    current_file: str | None = None
    pending_old: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            m = FILE_HEADER_RE.match(line)
            current_file = m.group(1) if m else None
            if current_file is not None:
                entry = files.setdefault(current_file, {"added": [], "removed": [], "old_paths": []})
                if pending_old is not None and pending_old != current_file \
                        and pending_old not in entry["old_paths"]:
                    entry["old_paths"].append(pending_old)
            pending_old = None
            continue
        if line.startswith("--- "):
            m = OLD_FILE_HEADER_RE.match(line)
            pending_old = m.group(1) if m else None
            continue
        if current_file is None:
            continue
        if line.startswith("+"):
            files[current_file]["added"].append(line[1:])
        elif line.startswith("-"):
            files[current_file]["removed"].append(line[1:])
    return files


def _git(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess | None:
    """git を実行する。git が無い・失敗した場合は None を返す（例外を漏らさない）。

    `errors="replace"` を明示するのは、非 UTF-8 バイト列を含む要件ドキュメントを
    `git show` したときのデコード例外で、呼び出し元（`self_review_check.py`）を
    生トレースバックで落とさないため。他の任意チェックと同じ「壊れても止めない」に揃える。

    `self_review_check.py` の `sh()` / `default_branch()` と重複しているのは意図的。
    あちらが本ファイルを import する側なので、共有すると循環 import になる。
    """
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _default_branch() -> str:
    r = _git(["symbolic-ref", "refs/remotes/origin/HEAD"])
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    return "main"


def resolve_base_ref() -> str | None:
    """差分の pre-image に当たるコミットを解決する。解決できなければ None。

    🔴 `HEAD` を使ってはならない。`self_review_check.py` が渡す差分は
    `origin/{default}...HEAD`（＋ステージ・作業ツリー）起点なので、pre-image は
    **merge-base** である。`HEAD` を pre-image にすると、ブランチ上で既にコミット済みの
    新規要件 ID が「変更前から存在した」と判定され、ゲートが素通りする（偽陰性）。
    """
    base = f"origin/{_default_branch()}"
    r = _git(["merge-base", base, "HEAD"])
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _git(["rev-parse", "--verify", f"{base}^{{commit}}"])
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def make_git_baseline_resolver(base_ref: str | None = None) -> BaselineResolver:
    """`git show {base_ref}:{path}` で変更前の全文を返す resolver を作る。

    返り値が None なのは「その ref にファイルが無い（新規ファイル）」場合と
    「base_ref を解決できない（git が無い等）」場合の両方。どちらも pre-image の
    ID 集合を増やさない＝検知側に倒れるため、区別せず None にまとめる。
    """
    ref = base_ref if base_ref is not None else resolve_base_ref()
    cache: dict[str, str | None] = {}

    def resolver(path: str) -> str | None:
        if ref is None:
            return None
        if path not in cache:
            r = _git(["show", f"{ref}:{path}"])
            cache[path] = r.stdout if (r is not None and r.returncode == 0) else None
        return cache[path]

    return resolver


def check_gate(
    diff_text: str,
    doc_pattern: re.Pattern = DOC_PATTERN,
    baseline_resolver: BaselineResolver | None = None,
) -> tuple[bool, str]:
    """(ok, message) を返す。ok=False は「対応する決定ログエントリが無い」違反。

    `baseline_resolver` はパスを受け取り変更前の全文（無ければ None）を返す呼び出し可能。
    省略時は git 経由の resolver（`make_git_baseline_resolver()`）を使う。git を引かせたく
    ないテストは、常に None を返す resolver（`lambda _path: None`）を渡す。
    """
    if baseline_resolver is None:
        baseline_resolver = make_git_baseline_resolver()
    files = parse_diff(diff_text)
    violations = []
    for path, hunks in files.items():
        if not doc_pattern.search(path):
            continue
        added_req_ids = set(REQ_ID_RE.findall("\n".join(hunks["added"])))
        if not added_req_ids:
            continue
        # pre-image に存在する ID は「新規追加」ではない（既存 ID への言及・移動を含む）
        preexisting_req_ids = set(REQ_ID_RE.findall("\n".join(hunks["removed"])))
        for lookup_path in [path, *hunks["old_paths"]]:
            baseline_text = baseline_resolver(lookup_path)
            if baseline_text:
                preexisting_req_ids |= set(REQ_ID_RE.findall(baseline_text))
        new_req_ids = added_req_ids - preexisting_req_ids
        if not new_req_ids:
            continue
        added_decision_ids = set(DECISION_ID_RE.findall("\n".join(hunks["added"])))
        removed_decision_ids = set(DECISION_ID_RE.findall("\n".join(hunks["removed"])))
        new_decision_ids = added_decision_ids - removed_decision_ids
        if not new_decision_ids:
            violations.append(
                f"{path}: 新しい要件 ID {sorted(new_req_ids)} が追加されていますが、"
                "対応する決定ログエントリ（D-n 行の追加）が同じ差分に見つかりません"
                "（docs/rules/phase-boundary-rules-detail.md §4）"
            )
    if violations:
        return False, "\n".join(violations)
    return True, "OK: 新規要件 ID には対応する決定ログエントリが伴っています（または新規 ID がありません）"


def _run_self_test() -> int:
    # (名前, 差分, 期待する ok, baseline resolver)。`_no_baseline` は常に None を返す
    # resolver＝git を一切引かず、差分の削除行だけを pre-image とする（テストを環境非依存にする）。
    # 本番経路（実 git 呼び出し）は末尾の統合テストで別途検証する。
    def _no_baseline(_path: str) -> str | None:
        return None

    cases: list[tuple[str, str, bool, BaselineResolver]] = []

    ok_diff = """diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -39,3 +39,4 @@
 - `FR-1`: 既存
+- `FR-2`: 新規要件
@@ -64,3 +65,4 @@
 | `D-1` | 既存決定 | ... | いいえ |
+| `D-2` | 新規決定 | ... | いいえ |
"""
    cases.append(("新規要件 ID + 対応する決定ログあり → 通過", ok_diff, True, _no_baseline))

    # 変異: 決定ログの追加行だけを取り除く（要件 ID の追加は残す）→ ゲートが FAIL を検知するはず
    mutated_diff = """diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -39,3 +39,4 @@
 - `FR-1`: 既存
+- `FR-2`: 新規要件
"""
    cases.append(("新規要件 ID のみ・決定ログ追加なし（変異） → 違反検知", mutated_diff, False, _no_baseline))

    unrelated_diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 hello
+world
"""
    cases.append(("非対象ファイルの差分 → 通過", unrelated_diff, True, _no_baseline))

    other_doc_diff = """diff --git a/docs/02_requirements/prd.md b/docs/02_requirements/prd.md
--- a/docs/02_requirements/prd.md
+++ b/docs/02_requirements/prd.md
@@ -1,1 +1,2 @@
 既存
+`FR-99`: 新規（スコープ外ファイル）
"""
    cases.append(("スコープ外ファイル（prd.md 分割構成） → 通過（既知の限界どおり）", other_doc_diff, True, _no_baseline))

    id_removed_only_diff = """diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -39,4 +39,3 @@
 - `FR-1`: 既存
-- `FR-2`: 削除する要件
"""
    cases.append(("要件 ID の削除のみ（新規追加なし） → 通過", id_removed_only_diff, True, _no_baseline))

    # レビュー指摘の回帰テスト: `+++ /dev/null`（ファイル削除）の直後に current_file が
    # リセットされないと、削除ファイルの内容が直前のファイル（product-brief.md）へ誤って
    # 帰属し、偶然 ID 文字列が一致すると違反が見逃される（false negative）。
    deleted_file_leak_diff = """diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -39,3 +39,4 @@
 - `FR-1`: 既存
+- `FR-2`: 新規要件（決定ログ追加なし）
diff --git a/docs/scratch/old-notes.md b/docs/scratch/old-notes.md
--- a/docs/scratch/old-notes.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# 旧メモ
-FR-2 についての雑談メモ（削除ファイル。product-brief.md の要件追加とは無関係）
"""
    cases.append(("削除ファイルの内容が直前ファイルへ誤帰属しない（回帰） → 違反検知", deleted_file_leak_diff, False, _no_baseline))

    # Issue #61 の偽陽性反例: 既存 ID を本文で参照しただけ（定義は pre-image 全文にある）
    existing_id_reference_diff = """diff --git a/docs/framework-templates/product-brief.example.md b/docs/framework-templates/product-brief.example.md
--- a/docs/framework-templates/product-brief.example.md
+++ b/docs/framework-templates/product-brief.example.md
@@ -120,3 +120,4 @@
 ## 7. リサーチ
+この節の調査は `FR-2` の裏取りのために行った。
"""
    baseline_with_fr2: BaselineResolver = lambda _path: "## 3. 要件\n- `FR-1`: 既存\n- `FR-2`: 既存\n"
    cases.append((
        "既存 ID を本文で参照しただけ（pre-image に定義あり） → 通過（#61 偽陽性の反例）",
        existing_id_reference_diff, True, baseline_with_fr2,
    ))

    # 偽陰性を新たに作らないことの確認: 同じ差分でも pre-image に定義が無ければ違反
    baseline_without_fr2: BaselineResolver = lambda _path: "## 3. 要件\n- `FR-1`: 既存\n"
    cases.append((
        "pre-image に定義が無い ID の追加・決定ログなし → 違反検知（偽陰性を作らない）",
        existing_id_reference_diff, False, baseline_without_fr2,
    ))

    # リネーム回帰: pre-image は旧パスにしかないので、新パスだけを引くと偽陽性になる
    renamed_diff = """diff --git a/docs/framework-templates/old-name.md b/docs/framework-templates/product-brief.md
similarity index 92%
rename from docs/framework-templates/old-name.md
rename to docs/framework-templates/product-brief.md
--- a/docs/framework-templates/old-name.md
+++ b/docs/framework-templates/product-brief.md
@@ -120,3 +120,4 @@
 ## 7. リサーチ
+この節の調査は `FR-2` の裏取りのために行った。
"""

    def _baseline_only_old_name(path: str) -> str | None:
        # 旧パスにだけ pre-image がある（リネーム後の名前ではまだ存在しない）
        if path == "docs/framework-templates/old-name.md":
            return "## 3. 要件\n- `FR-1`: 既存\n- `FR-2`: 既存\n"
        return None

    cases.append((
        "リネーム後ファイルで既存 ID を参照 → 通過（旧パスの pre-image を引く・回帰）",
        renamed_diff, True, _baseline_only_old_name,
    ))

    # 🔴 連結差分の回帰（レビュー指摘・実測で再現）: self_review_check.py は
    # `origin/main...HEAD`・`--cached`・作業ツリーの 3 経路の差分を連結して渡す。
    # 作業ツリー差分のコンテキスト行には「コミット済みの新規 ID」が現れるため、
    # コンテキスト行を pre-image 扱いすると真の違反を見逃す。
    concatenated_sources_diff = """diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -39,3 +39,4 @@
 - `FR-1`: 既存
+- `FR-3`: 新規要件（コミット済み・決定ログなし＝真の違反）
diff --git a/docs/framework-templates/product-brief.md b/docs/framework-templates/product-brief.md
--- a/docs/framework-templates/product-brief.md
+++ b/docs/framework-templates/product-brief.md
@@ -38,4 +38,4 @@
 - `FR-1`: 既存
 - `FR-3`: 新規要件（コミット済み・決定ログなし＝真の違反）
-誤字
+誤字修正（未コミットの無関係な編集）
"""
    cases.append((
        "3 経路連結差分でコミット済みの新規 ID を見逃さない（回帰） → 違反検知",
        concatenated_sources_diff, False, _no_baseline,
    ))

    # 新規ファイル（pre-image が存在しない）は追加行の ID をすべて新規とみなす
    new_file_diff = """diff --git a/docs/framework-templates/product-brief.new.md b/docs/framework-templates/product-brief.new.md
--- /dev/null
+++ b/docs/framework-templates/product-brief.new.md
@@ -0,0 +1,2 @@
+# 新規の要件ドキュメント
+- `FR-1`: 新規要件（決定ログ追加なし）
"""
    cases.append((
        "新規ファイルの要件 ID 追加・決定ログなし → 違反検知",
        new_file_diff, False, _no_baseline,
    ))

    failed = 0
    for name, diff_text, expected_ok, resolver in cases:
        ok, message = check_gate(diff_text, baseline_resolver=resolver)
        passed = ok == expected_ok
        print(f"{'PASS' if passed else 'FAIL'}: {name} -> ok={ok} (expected {expected_ok})")
        if not passed:
            print(f"      {message}")
            failed += 1
    failed += _run_git_integration_test()

    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS（変異ケースが意図どおり違反を検知したことを確認済み）")
    return 0


def _run_git_integration_test() -> int:
    """本番経路（`resolve_base_ref` → `make_git_baseline_resolver` → 実 `git show`）を
    使い捨てリポジトリで実際に動かす。失敗件数を返す。

    上のケース群は resolver をスタブしているため、既定経路が壊れても全件 PASS になる
    （レビュー指摘: 「テストのためのテスト」になっていないかの穴）。ここだけはスタブしない。
    """
    import os
    import tempfile

    if _git(["--version"]) is None:
        print("SKIP: git 統合テスト（git を実行できない環境）")
        return 0

    doc = "docs/framework-templates/product-brief.md"
    base_body = "# 要件\n\n## 3. 要件\n- `FR-1`: 既存\n- `FR-2`: 既存\n\n## 5. 決定ログ\n| `D-1` | 既存決定 |\n\n## 7. リサーチ\n"
    cwd = os.getcwd()
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        def run(*args: str) -> bool:
            r = _git(["-C", tmp, *args])
            return r is not None and r.returncode == 0

        os.makedirs(os.path.join(tmp, os.path.dirname(doc)), exist_ok=True)
        with open(os.path.join(tmp, doc), "w", encoding="utf-8") as f:
            f.write(base_body)
        setup_ok = (
            run("init", "-q", "-b", "main")
            and run("config", "user.email", "selftest@example.com")
            and run("config", "user.name", "selftest")
            and run("add", "-A")
            and run("commit", "-q", "-m", "base")
            and run("update-ref", "refs/remotes/origin/main", "HEAD")
            and run("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            and run("checkout", "-q", "-b", "feature")
        )
        if not setup_ok:
            print("SKIP: git 統合テスト（使い捨てリポジトリを準備できなかった）")
            return 0

        def gate_on_head() -> tuple[bool, str]:
            """本番と同じ経路: 既定 resolver（引数なし）で origin/main...HEAD を判定する。"""
            r = _git(["-C", tmp, "diff", "origin/main...HEAD"])
            diff_text = r.stdout if (r is not None and r.returncode == 0) else ""
            os.chdir(tmp)
            try:
                return check_gate(diff_text)
            finally:
                os.chdir(cwd)

        checks: list[tuple[str, str, bool]] = [
            ("既存 ID を本文で参照しただけ → 通過", "この節の調査は `FR-2` の裏取りのために行った。\n", True),
            ("真に新規の ID を決定ログなしで追加 → 違反検知", "- `FR-9`: 新規要件\n", False),
        ]
        for name, appended, expected_ok in checks:
            with open(os.path.join(tmp, doc), "w", encoding="utf-8") as f:
                f.write(base_body + appended)
            if not (run("add", "-A") and run("commit", "-q", "-m", name)):
                print(f"SKIP: git 統合テスト（コミットできなかった）: {name}")
                return failed
            ok, message = gate_on_head()
            passed = ok == expected_ok
            print(f"{'PASS' if passed else 'FAIL'}: [git 統合] {name} -> ok={ok} (expected {expected_ok})")
            if not passed:
                print(f"      {message}")
                failed += 1
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--diff-file", help="unified diff を読み込むファイル（省略時は stdin）")
    parser.add_argument("--self-test", action="store_true", help="ゲート判定ロジックの自己テスト（変異テスト込み）を実行する")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8") as f:
            diff_text = f.read()
    else:
        diff_text = sys.stdin.read()

    ok, message = check_gate(diff_text)
    print(message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
