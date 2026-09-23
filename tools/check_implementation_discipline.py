#!/usr/bin/env python3
"""実装モデルの実行規律（TDD 側）の機械検査（docs/rules/implementation-discipline.md §2）。

`D-36`（実装モデルの標準は Clean Architecture / DDD / TDD）が定めた標準のうち、
母艦（tsukuru-studio）側で機械化できるのは **「変更したコードにテストの裏付けがあるか」**
の一点である。層構造・依存方向（同ルール §1）は意図的に機械化しない:

  - tsukuru-studio 自身のコードは `tools/` 直下のフラットなスクリプト群で層を持たず、
    層検査を置いても検査対象が 0 件の「空のゲート」になる（`D-12` がまさに禁じた形）
  - 下流プロダクトは技術スタックが `C-4` で初めて決まるため、母艦側に静的解析を
    常設できない。実体が要るのは生成されたプロダクト側（`M-3` 射程）

「テストの裏付け」は本リポジトリに既にある 2 つの規約で判定する（新しい規約を発明しない）:

  - `tools/*.py`      → ソース内の `--self-test`、または `tools/test_<stem>.sh`
  - `*.sh`（フック・スクリプト）→ `tools/test_<stem>.sh`（ハイフンはアンダースコアへ）

どちらも PR 前ゲート（`tools/self_review_check.py`）が差分に含まれるものを実際に実行する
ため、裏付けの存在＝実行される検証の存在になっている。

重大度は `D-36` の段階適用（新規は必須・既存の遡及リファクタはしない）に従って割る:

  - **新規追加ファイル**（git 上 `A`）にテストの裏付けが無い → **Error**（ブロック）
  - **既存ファイルの変更**（`M` 等）にテストの裏付けが無い → **Warning**（ブロックしない）

fail-closed（`D-12`）: 登録ルール（`RULES`）が 0 件のとき、および `--self-test` の
ケースが 0 件のときは **exit 2** で失敗する。「ゲートがあるのに何も検査していない」
状態を成功終了させない。

照合は **厳密一致** で行う（`self_review_check._companion_test_script_targets()` と同じ綴り規約）:

  - 対応テストは `tools/test_<stem>.sh` の **完全一致**。前方一致にすると
    `tools/pr.py` が無関係な `tools/test_pr_confirm_marker.sh` を裏付けと誤認し、
    本ゲート自体を素通りさせる（Layer 1 セルフレビューで実証）
  - `--self-test` は **引用符付きのリテラル**（`"--self-test"` / `'--self-test'`）でだけ数える。
    素の部分文字列一致にすると `# TODO: implement --self-test later` のような
    「未実装であることを述べたコメント」でゲートを通過できてしまう

既知の限界（隠さない）:
  - テストの **中身が意味のある検証か** は判定しない。空の `exit 0` でも通る
  - 「テストを先に書いたか（Red → Green の順序）」は判定できない。同じ PR に
    テストが含まれるかまでしか見えない
  - 完全一致にしたため、`scripts/bootstrap.sh` のように別名のテスト
    （`tools/test_bootstrap_marketplace_guard.sh`）しか持たないファイルは
    「裏付け無し」と報告される。既存ファイルなので Warning 止まりであり、
    綴りを規約へ揃えるか裏付けを足すかは実装者が判断する
  - 下流プロダクト側の層構造・依存方向（同ルール §1）は本チェッカーの対象外

Usage:
  python3 tools/check_implementation_discipline.py --changed
  python3 tools/check_implementation_discipline.py --paths tools/foo.py
  python3 tools/check_implementation_discipline.py --new tools/foo.py
  python3 tools/check_implementation_discipline.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple

TOOLS_DIR = "tools"
TEST_PREFIX = "test_"

# 「--self-test を実装した」とみなすのは引用符付きリテラルだけ（docstring の Usage 行や
# 「まだ実装していない」旨の TODO コメントを裏付けと誤認しないため）。
SELF_TEST_LITERAL_RE = re.compile(r"""['"]--self-test['"]""")

# git の rename / copy ステータス（R100 / C075 …）。数値は類似度スコア（%）。
RENAME_STATUS_RE = re.compile(r"^([RC])(\d{1,3})$")


def _stem_underscored(path: str) -> str:
    return Path(path).stem.replace("-", "_")


class Repo:
    """検査対象リポジトリへの読み取りを 1 箇所へ閉じる（self-test はメモリ実装を差し込む）。"""

    def __init__(self, root: str = ".") -> None:
        self.root = Path(root)

    def read_text(self, rel: str) -> str | None:
        p = self.root / rel
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

    def test_script_names(self) -> set[str]:
        d = self.root / TOOLS_DIR
        try:
            return {p.name for p in d.iterdir() if p.is_file() and p.name.startswith(TEST_PREFIX)
                    and p.suffix == ".sh"}
        except Exception:
            return set()


class MemoryRepo(Repo):
    """self-test 用。ファイルパス → 内容の辞書だけで振る舞う。"""

    def __init__(self, files: dict[str, str]) -> None:  # noqa: D107 - super の root を使わない
        self.files = dict(files)

    def read_text(self, rel: str) -> str | None:
        return self.files.get(rel)

    def test_script_names(self) -> set[str]:
        return {
            Path(f).name
            for f in self.files
            if Path(f).parent.as_posix() == TOOLS_DIR
            and Path(f).name.startswith(TEST_PREFIX)
            and f.endswith(".sh")
        }


def _has_companion_test(path: str, repo: Repo) -> bool:
    """tools/test_<stem>.sh が存在するか（**完全一致**）。

    前方一致にすると `tools/pr.py` が無関係な `tools/test_pr_confirm_marker.sh` に一致して
    しまい、テストの裏付けが無い新規ツールが Error を素通りする（モジュール docstring の
    「照合は厳密一致で行う」を参照）。綴り規約は self_review_check の
    `_companion_test_script_targets()` と同じ（ハイフンはアンダースコアへ）。
    """
    return (TEST_PREFIX + _stem_underscored(path) + ".sh") in repo.test_script_names()


def _has_self_test(path: str, repo: Repo) -> bool:
    """ソースが `--self-test` を **引用符付きリテラルとして** 含むか。

    素の部分文字列一致だと「`# TODO: implement --self-test later`」のようなコメントや、
    docstring の Usage 行だけで裏付けありと誤判定する。argparse / sys.argv で実装すれば
    必ず引用符付きで現れるため、これを実装の痕跡として使う。
    """
    text = repo.read_text(path)
    return bool(text) and SELF_TEST_LITERAL_RE.search(text) is not None


def is_new_status(status: str | None) -> bool:
    """git の name-status が「新規のコードが増えた」を意味するかを判定する。

    `A`（追加）はもちろん新規。`R` / `C`（rename / copy）は **類似度が 100 未満なら新規扱い**
    にする — `git mv old.py tools/new.py` + 内容の書き換えは、base に存在しないパスへ
    書き換え済みのコードを置く操作であり、実質「新規に書くコード」（`D-36`）だからである。
    類似度 100（純粋なリネーム）は新規のコードが 1 行も増えていないので既存扱いにする
    （リネームしただけでテスト新設を強制するのは `D-36` が禁じた遡及リファクタになる）。
    スコアを伴わない `R` / `C` は安全側（新規扱い）へ倒す。
    """
    s = (status or "M").upper()
    if s.startswith("A"):
        return True
    m = RENAME_STATUS_RE.match(s)
    if m:
        return int(m.group(2)) < 100
    return s in ("R", "C")


class Rule(NamedTuple):
    rule_id: str
    target: str
    matches: Callable[[str], bool]
    satisfied: Callable[[str, Repo], bool]
    hint: str


def _is_python_tool(path: str) -> bool:
    p = Path(path)
    return p.parent.as_posix() == TOOLS_DIR and p.suffix == ".py" and not p.name.startswith(TEST_PREFIX)


def _is_hook_shell(path: str) -> bool:
    p = Path(path)
    if p.suffix != ".sh":
        return False
    parent = p.parent.as_posix()
    return parent == ".claude/hooks" or parent.startswith(".claude/hooks/")


def _is_repo_script(path: str) -> bool:
    p = Path(path)
    return p.parent.as_posix() == "scripts" and p.suffix == ".sh"


# 🔴 登録ルールが 0 件になったら exit 2（fail-closed・D-12）。ルールを一時的に外すときは
# 「なぜ外すか」を docs/rules/implementation-discipline.md §3 へ書いてから外すこと。
RULES: list[Rule] = [
    Rule(
        rule_id="TDD-1",
        target="tools/*.py",
        matches=_is_python_tool,
        satisfied=lambda p, r: _has_self_test(p, r) or _has_companion_test(p, r),
        hint="ソースへ `--self-test` を実装するか、tools/test_<name>.sh を追加する",
    ),
    Rule(
        rule_id="TDD-2",
        target=".claude/hooks/**/*.sh",
        matches=_is_hook_shell,
        satisfied=lambda p, r: _has_companion_test(p, r),
        hint="tools/test_<name>.sh を追加する（ハイフンはアンダースコアへ）",
    ),
    Rule(
        rule_id="TDD-3",
        target="scripts/*.sh",
        matches=_is_repo_script,
        satisfied=lambda p, r: _has_companion_test(p, r),
        hint="tools/test_<name>.sh を追加する（ハイフンはアンダースコアへ）",
    ),
]


class Finding(NamedTuple):
    severity: str  # "error" | "warning"
    rule_id: str
    path: str
    message: str


def evaluate(entries: dict[str, str], repo: Repo) -> list[Finding]:
    """変更エントリ（パス → git の name-status 文字）を評価して findings を返す。"""
    findings: list[Finding] = []
    for path in sorted(entries):
        for rule in RULES:
            if not rule.matches(path):
                continue
            if rule.satisfied(path, repo):
                continue
            is_new = is_new_status(entries[path])
            severity = "error" if is_new else "warning"
            kind = "新規追加" if is_new else "既存ファイルの変更"
            findings.append(Finding(
                severity=severity,
                rule_id=rule.rule_id,
                path=path,
                message=(f"[{rule.rule_id}] テストの裏付けが無い（{kind}・対象 {rule.target}）"
                         f" → {rule.hint}"),
            ))
            break
    return findings


# PR 前ゲート（self_review_check.py → pre-pr-create-check.sh）は全体を外側 timeout 90 秒で
# 包んでおり、超過すると exit=124 で PR 作成が無警告に通る。git 呼び出しは通常 1 秒未満だが、
# 遅い環境でも合計が予算を食い潰さないよう 1 呼び出しあたりの上限を短く固定する（呼び出しは 5 回）。
GIT_CALL_TIMEOUT = 5


def _sh(args: list[str], timeout: int = GIT_CALL_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _default_branch() -> str:
    r = _sh(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    return "main"


def changed_entries() -> dict[str, str]:
    """コミット済み・ステージ済み・作業ツリー・未追跡の 4 経路から パス → status を集める。

    self_review_check.changed_files() と同じ 4 経路を見る（PR 前ゲートは未コミット編集が
    大半のため HEAD 比較だけでは見落とす）。同じパスが複数経路に出たら **新規側を優先**
    する（`is_new_status()` が真になる status を後勝ちで潰さない。ブランチで追加した
    ファイルを既存扱いにして Warning へ落とさないため）。
    """
    entries: dict[str, str] = {}

    def _merge(path: str, status: str) -> None:
        prev = entries.get(path)
        if prev is not None and is_new_status(prev):
            return
        entries[path] = status

    base = f"origin/{_default_branch()}"
    for args in (["git", "diff", "--name-status", f"{base}...HEAD"],
                 ["git", "diff", "--name-status", "--cached"],
                 ["git", "diff", "--name-status"]):
        r = _sh(args)
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0], parts[-1]
            if status.startswith("D"):
                continue
            _merge(path, status)

    ru = _sh(["git", "ls-files", "--others", "--exclude-standard"])
    if ru.returncode == 0:
        for path in ru.stdout.splitlines():
            if path:
                _merge(path, "A")

    return {p: s for p, s in entries.items() if Path(p).is_file()}


# --------------------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------------------

def _self_test_cases() -> list[tuple[str, dict[str, str], dict[str, str], list[tuple[str, str]]]]:
    """(名前, リポジトリ内容, 変更エントリ, 期待する (severity, rule_id) 一覧)。"""
    return [
        (
            "新規の tools/*.py に裏付けが無ければ Error",
            {"tools/foo.py": "print('hi')\n"},
            {"tools/foo.py": "A"},
            [("error", "TDD-1")],
        ),
        (
            "既存の tools/*.py の変更に裏付けが無ければ Warning",
            {"tools/foo.py": "print('hi')\n"},
            {"tools/foo.py": "M"},
            [("warning", "TDD-1")],
        ),
        (
            "ソース内の --self-test リテラルが裏付けとして数えられる",
            {"tools/foo.py": "if '--self-test' in sys.argv: pass\n"},
            {"tools/foo.py": "A"},
            [],
        ),
        (
            "引用符なしの言及（未実装の TODO コメント）は裏付けにしない",
            {"tools/foo.py": "# TODO: implement --self-test later, not done yet\nprint('hi')\n"},
            {"tools/foo.py": "A"},
            [("error", "TDD-1")],
        ),
        (
            "docstring の Usage 行だけの言及も裏付けにしない",
            {"tools/foo.py": '"""Usage:\n  python3 tools/foo.py --self-test\n"""\nprint(1)\n'},
            {"tools/foo.py": "A"},
            [("error", "TDD-1")],
        ),
        (
            "tools/test_<stem>.sh が裏付けとして数えられる",
            {"tools/foo.py": "print('hi')\n", "tools/test_foo.sh": "exit 0\n"},
            {"tools/foo.py": "A"},
            [],
        ),
        (
            "前方一致では裏付けにしない（tools/pr.py ↔ test_pr_confirm_marker.sh の衝突）",
            {"tools/pr.py": "print('hi')\n", "tools/test_pr_confirm_marker.sh": "exit 0\n"},
            {"tools/pr.py": "A"},
            [("error", "TDD-1")],
        ),
        (
            "別名のテストしか無い scripts/*.sh も裏付け無しと判定する（完全一致）",
            {"scripts/bootstrap.sh": "echo hi\n",
             "tools/test_bootstrap_marketplace_guard.sh": "exit 0\n"},
            {"scripts/bootstrap.sh": "M"},
            [("warning", "TDD-3")],
        ),
        (
            "書き換えを伴う rename（R083）は新規扱いで Error",
            {"tools/renamed_tool.py": "print('hi')\n"},
            {"tools/renamed_tool.py": "R083"},
            [("error", "TDD-1")],
        ),
        (
            "純粋な rename（R100）は新規のコードが増えていないので Warning 止まり",
            {"tools/renamed_tool.py": "print('hi')\n"},
            {"tools/renamed_tool.py": "R100"},
            [("warning", "TDD-1")],
        ),
        (
            "スコアの無い R は安全側（新規扱い）へ倒す",
            {"tools/renamed_tool.py": "print('hi')\n"},
            {"tools/renamed_tool.py": "R"},
            [("error", "TDD-1")],
        ),
        (
            "フックはハイフンをアンダースコアへ読み替えて照合する",
            {".claude/hooks/pre-pr-create-check.sh": "echo hi\n",
             "tools/test_pre_pr_create_check.sh": "exit 0\n"},
            {".claude/hooks/pre-pr-create-check.sh": "M"},
            [],
        ),
        (
            "新規フックに裏付けが無ければ Error",
            {".claude/hooks/new-guard.sh": "echo hi\n"},
            {".claude/hooks/new-guard.sh": "A"},
            [("error", "TDD-2")],
        ),
        (
            "テストスクリプト自身は対象外（テストにテストを要求しない）",
            {"tools/test_foo.sh": "exit 0\n"},
            {"tools/test_foo.sh": "A"},
            [],
        ),
        (
            "対象外の拡張子・ディレクトリは無視する",
            {"docs/rules/foo.md": "# hi\n", "config/foo.yaml": "a: 1\n"},
            {"docs/rules/foo.md": "A", "config/foo.yaml": "A"},
            [],
        ),
    ]


def run_self_test() -> int:
    if not RULES:
        print("❌ fail-closed: 登録ルールが 0 件です（D-12）", file=sys.stderr)
        return 2
    cases = _self_test_cases()
    if not cases:
        print("❌ fail-closed: self-test のケースが 0 件です（D-12）", file=sys.stderr)
        return 2

    failed = 0
    for name, files, entries, expected in cases:
        got = [(f.severity, f.rule_id) for f in evaluate(entries, MemoryRepo(files))]
        if got != expected:
            failed += 1
            print(f"❌ {name}\n   expected={expected}\n   got     ={got}", file=sys.stderr)

    total = len(cases)
    if failed:
        print(f"❌ self-test: {total - failed}/{total} passed", file=sys.stderr)
        return 1
    print(f"✅ self-test: {total}/{total} passed（ルール {len(RULES)} 件）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="実装モデルの実行規律（TDD 側）の機械検査")
    ap.add_argument("--changed", action="store_true", help="git の変更ファイルを対象にする（既定）")
    ap.add_argument("--paths", nargs="*", default=[], help="既存ファイルとして明示指定する")
    ap.add_argument("--new", nargs="*", default=[], help="新規ファイルとして明示指定する")
    ap.add_argument("--json", action="store_true", help="findings を JSON で出力する")
    ap.add_argument("--self-test", action="store_true", help="自己テストを実行する")
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    # fail-closed（D-12）: ゲートは存在するのに検査ルールが空、という状態を成功させない
    if not RULES:
        print("❌ fail-closed: 登録ルールが 0 件です（D-12・docs/rules/implementation-discipline.md §3）",
              file=sys.stderr)
        return 2

    entries: dict[str, str] = {}
    if args.paths or args.new:
        entries.update({p: "M" for p in args.paths})
        entries.update({p: "A" for p in args.new})
    else:
        entries = changed_entries()

    findings = evaluate(entries, Repo("."))

    if args.json:
        print(json.dumps([f._asdict() for f in findings], ensure_ascii=False, indent=2))
    else:
        for f in findings:
            mark = "❌" if f.severity == "error" else "⚠️"
            print(f"{mark} {f.path}: {f.message}")
        if not findings:
            print(f"✅ 実装規律 TDD 検査: 違反なし（対象 {len(entries)} ファイル / ルール {len(RULES)} 件）")

    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
