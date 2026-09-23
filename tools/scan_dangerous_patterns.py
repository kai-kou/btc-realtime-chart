#!/usr/bin/env python3
"""scan_dangerous_patterns.py — Python の危険コードパターンを AST で決定論検出する（FAIR Layer 0 強化・#56）。

`self_review_check.py`（PR 作成前の Lv3 機械ゲート）から呼ばれ、高シグナルな
セキュリティ欠陥を **決定論的** に検出する。LLM レビュー（Layer 1/2）の前段で、
機械的に確実に分かる事故を取りこぼさないための「外部参照」を厚くする。

設計方針（保守的・誤ブロック回避）:
- **ERROR（PR 作成ブロック）**: ほぼ意図的にコミットすることがない高危険パターンのみ。
  AST 解析で「動的入力かどうか」まで判定し、定数のみの安全なケースは ERROR にしない。
- **WARNING（非ブロック）**: 誤検知が起きやすい/中危険のもの（資格情報ハードコード等）。
  GitGuardian など他ゲートと二重化されるため Warning に留める。

検出（ERROR）:
- `subprocess.{run,call,Popen,...}` / が `shell=True` かつコマンドが **非定数**（連結・f-string・変数）
- `os.system(...)` / `os.popen(...)` が **非定数** 引数
- `eval(...)` / `exec(...)` が **非定数** 引数
- `pickle.load(...)` / `pickle.loads(...)`（信頼できないデータの逆シリアライズ）
- `yaml.load(...)` で `Loader=` 未指定 or 非 Safe ローダ

検出（WARNING）:
- 資格情報ハードコード: 変数名が PASSWORD/SECRET/TOKEN/API_KEY/PASSWD/CREDENTIAL/PRIVATE_KEY 等に
  一致し、非空・非プレースホルダの文字列リテラルを代入
- `shell=True` かつコマンドが定数（中危険）
- `requests.*(..., verify=False)`（TLS 検証無効化）

使い方:
    python3 tools/scan_dangerous_patterns.py <file.py> [<file2.py> ...]
    python3 tools/scan_dangerous_patterns.py --changed     # git 差分の .py を検査
    python3 tools/scan_dangerous_patterns.py --self-test    # 自己テスト

終了コード: 0=ERROR なし / 1=ERROR あり / 2=チェッカー異常
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

# 資格情報っぽい変数名（大文字小文字無視・部分一致）
_SECRET_NAME_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|client[_-]?secret|auth[_-]?token)",
    re.IGNORECASE,
)
# プレースホルダ・明らかにダミーな値（ハードコード扱いしない）
_PLACEHOLDER_VALUES = frozenset({
    "", "none", "null", "changeme", "your_password", "your-password",
    "xxx", "xxxx", "todo", "fixme", "placeholder", "example", "dummy", "test",
    "<password>", "<secret>", "<token>", "<api_key>", "redacted", "********",
})
_SUBPROCESS_FUNCS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
# yaml.load の安全な Loader（部分一致だと MyUnsafeLoader 等を誤って安全判定するため許可リストで判定）
_YAML_SAFE_LOADERS = frozenset({"SafeLoader", "CSafeLoader"})


def _is_constant_str(node: ast.AST) -> bool:
    """ノードが定数文字列（リテラル/定数結合）かを判定する。"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    # "a" + "b"（両辺定数）は定数扱い、変数が混ざれば非定数
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_constant_str(node.left) and _is_constant_str(node.right)
    return False


def _is_dynamic(node: ast.AST) -> bool:
    """コマンド/コード引数が「動的（変数・連結・f-string・format）」かを判定する。

    定数リテラルのみ False（安全寄り）。f-string・連結・.format()・変数参照は True。
    """
    if node is None:
        return False
    if _is_constant_str(node):
        return False
    # f-string は JoinedStr（必ず動的扱い）
    if isinstance(node, ast.JoinedStr):
        return True
    # リスト/タプル（subprocess の ["echo", x] 形式）: 要素のいずれかが動的なら動的
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_is_dynamic(elt) for elt in node.elts)
    # 二項演算（連結 +・乗算 *・フォーマット % など）: 左右いずれかが動的なら動的
    if isinstance(node, ast.BinOp):
        return _is_dynamic(node.left) or _is_dynamic(node.right)
    # "...".format(...)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return True
    # 変数参照・添字・属性・関数呼び出しなど（定数でない限り動的とみなす）
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        return True
    return False


def _func_name(node: ast.Call) -> str:
    """呼び出しノードの関数名を 'mod.attr' 形式で取り出す（取れなければ末端名）。"""
    f = node.func
    if isinstance(f, ast.Attribute):
        parts = [f.attr]
        cur = f.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _kw(node: ast.Call, name: str) -> ast.AST | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _first_arg(node: ast.Call) -> ast.AST | None:
    return node.args[0] if node.args else None


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        # (lineno, severity, code, message)
        self.findings: list[tuple[int, str, str, str]] = []

    def _add(self, node: ast.AST, sev: str, code: str, msg: str) -> None:
        self.findings.append((getattr(node, "lineno", 0), sev, code, msg))

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _func_name(node)
        tail = name.split(".")[-1]

        # subprocess.* / Popen の shell=True
        # エイリアスインポート（from subprocess import run）も捕捉するため tail のみで判定する。
        # 実トリガは shell=True キーワードの存在であり、これは subprocess 系に固有のため誤検知は小さい。
        if tail in _SUBPROCESS_FUNCS:
            shell = _kw(node, "shell")
            if isinstance(shell, ast.Constant) and shell.value is True:
                cmd = _first_arg(node)
                if _is_dynamic(cmd):
                    self._add(node, "ERROR", "DP101",
                              "subprocess の shell=True に非定数コマンドを渡しています（コマンドインジェクション）。"
                              "引数をリストで渡し shell=False にしてください")
                else:
                    self._add(node, "WARNING", "DP102",
                              "subprocess の shell=True（コマンドは定数だが shell 実行は非推奨）")

        # os.system / os.popen
        elif name in ("os.system", "os.popen"):
            if _is_dynamic(_first_arg(node)):
                self._add(node, "ERROR", "DP103",
                          f"{name} に非定数引数を渡しています（コマンドインジェクション）。subprocess のリスト渡しを使ってください")

        # eval / exec（builtins 経由も捕捉）
        elif tail in ("eval", "exec") and name in ("eval", "exec", "builtins.eval", "builtins.exec"):
            if _is_dynamic(_first_arg(node)):
                self._add(node, "ERROR", "DP104",
                          f"{tail}() に非定数式を渡しています（任意コード実行）。設計を見直してください")

        # pickle.load / loads
        elif name in ("pickle.load", "pickle.loads", "cPickle.load", "cPickle.loads"):
            self._add(node, "ERROR", "DP105",
                      f"{name}（信頼できないデータの逆シリアライズは RCE になりえます）。json 等を検討してください")

        # yaml.load without safe loader（Loader はキーワード or 位置引数#2 の両方を見る）
        elif name in ("yaml.load",):
            loader = _kw(node, "Loader")
            if loader is None and len(node.args) >= 2:
                loader = node.args[1]
            loader_name = (getattr(loader, "attr", "") or getattr(loader, "id", "")) if loader is not None else ""
            safe = loader_name in _YAML_SAFE_LOADERS
            if loader is None or not safe:
                self._add(node, "ERROR", "DP106",
                          "yaml.load に Safe ローダが指定されていません（任意オブジェクト生成）。yaml.safe_load を使ってください")

        # requests verify=False（requests.* / session.get(...) の両方を捕捉）
        elif tail in ("get", "post", "put", "delete", "patch", "head", "request", "Session") and \
                ("requests" in name or "session" in name.lower()):
            v = _kw(node, "verify")
            if isinstance(v, ast.Constant) and v.value is False:
                self._add(node, "WARNING", "DP107",
                          "requests で verify=False（TLS 証明書検証を無効化しています）")

        self.generic_visit(node)

    def _check_secret_assign(self, target: ast.AST, value: ast.AST) -> None:
        # target 名（Name or Attribute）を取り出す
        tname = None
        if isinstance(target, ast.Name):
            tname = target.id
        elif isinstance(target, ast.Attribute):
            tname = target.attr
        if not tname or not _SECRET_NAME_RE.search(tname):
            return
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            v = value.value.strip()
            if v.lower() in _PLACEHOLDER_VALUES or len(v) < 6:
                return
            # 環境変数取得などは Call なのでここには来ない（Constant のみ対象）
            self._add(target, "WARNING", "DP201",
                      f"資格情報のハードコードの可能性: 変数 '{tname}' に文字列リテラルを直接代入しています。"
                      "環境変数/シークレットマネージャから取得してください")

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for t in node.targets:
            self._check_secret_assign(t, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self._check_secret_assign(node.target, node.value)
        self.generic_visit(node)


def scan_text(text: str, filename: str = "<text>") -> list[tuple[int, str, str, str]]:
    """Python ソースを走査し findings を返す。構文エラーは空（py_compile 側が拾う）。"""
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError:
        return []
    v = _Visitor()
    v.visit(tree)
    return sorted(v.findings, key=lambda x: (x[0], x[2]))


def scan_file(path: str) -> list[tuple[int, str, str, str]]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        # セキュリティゲートとして「検査できなかった」を無音にしない（誤った安心を防ぐ）
        print(f"[scan] WARNING: {path} を読み込めずスキップ: {e}", file=sys.stderr)
        return []
    return scan_text(text, path)


def _changed_python_files() -> list[str]:
    out: list[str] = []
    try:
        base = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                              capture_output=True, text=True, timeout=10)
        ref = base.stdout.strip().split("/")[-1] if base.returncode == 0 else "main"
        cmds = [["git", "diff", "--name-only", f"origin/{ref}...HEAD"],
                ["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]]
        seen: set[str] = set()
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                continue
            for f in r.stdout.split():
                if f.endswith(".py") and f not in seen and Path(f).is_file():
                    seen.add(f)
                    out.append(f)
    except Exception:
        pass
    return out


# --- 自己テスト --------------------------------------------------------------
_SELFTEST_CASES = [
    # (source, expected_codes_subset, must_not_contain_codes)
    ('import subprocess\nsubprocess.run("echo " + x, shell=True)\n', {"DP101"}, set()),
    ('import subprocess\nsubprocess.run(["echo", x])\n', set(), {"DP101", "DP102"}),
    ('import subprocess\nsubprocess.run("ls -la", shell=True)\n', {"DP102"}, {"DP101"}),
    # リスト形式 + shell=True: 動的要素ありは DP101、全定数は DP102
    ('import subprocess\nsubprocess.run(["echo", x], shell=True)\n', {"DP101"}, {"DP102"}),
    ('import subprocess\nsubprocess.run(["echo", "hi"], shell=True)\n', {"DP102"}, {"DP101"}),
    # %/* 連結 + shell=True も動的扱い
    ('import subprocess\nsubprocess.run("echo %s" % x, shell=True)\n', {"DP101"}, set()),
    # エイリアスインポート（from subprocess import run）も捕捉
    ('from subprocess import run\nrun("rm " + x, shell=True)\n', {"DP101"}, set()),
    ('import os\nos.system("rm " + path)\n', {"DP103"}, set()),
    ('import os\nos.system("ls")\n', set(), {"DP103"}),
    ('eval(user_input)\n', {"DP104"}, set()),
    ('import builtins\nbuiltins.eval(user_input)\n', {"DP104"}, set()),
    ('eval("1 + 1")\n', set(), {"DP104"}),
    ('import pickle\npickle.loads(blob)\n', {"DP105"}, set()),
    ('import yaml\nyaml.load(s)\n', {"DP106"}, set()),
    ('import yaml\nyaml.load(s, Loader=yaml.SafeLoader)\n', set(), {"DP106"}),
    ('import yaml\nyaml.load(s, yaml.SafeLoader)\n', set(), {"DP106"}),  # 位置引数の Safe ローダ
    ('import yaml\nyaml.load(s, Loader=MyUnsafeLoader)\n', {"DP106"}, set()),  # "safe" 部分一致の誤判定防止
    ('import requests\nsession = requests.Session()\nsession.get("url", verify=False)\n', {"DP107"}, set()),
    ('DB_PASSWORD = "admin-pass-123"\n', {"DP201"}, set()),
    ('import os\nDB_PASSWORD = os.getenv("DB_PASSWORD", "")\n', set(), {"DP201"}),
    ('PASSWORD = ""\n', set(), {"DP201"}),
    ('PASSWORD_FIELD = "test"\n', set(), {"DP201"}),  # プレースホルダ/短い → 検出しない
    ('def average(n):\n    return sum(n) / len(n)\n', set(), {"DP101", "DP104"}),  # 論理バグは対象外
]


def _self_test() -> int:
    failures = 0
    for i, (src, expect, forbid) in enumerate(_SELFTEST_CASES, 1):
        codes = {c for _, _, c, _ in scan_text(src)}
        missing = expect - codes
        unexpected = forbid & codes
        if missing or unexpected:
            failures += 1
            print(f"  ✗ case {i}: missing={missing or '-'} unexpected={unexpected or '-'} (got {codes or '∅'})")
    if failures:
        print(f"[scan] self-test FAILED: {failures}/{len(_SELFTEST_CASES)}")
        return 1
    print(f"[scan] self-test PASSED: {len(_SELFTEST_CASES)} cases")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Python 危険パターン検出（FAIR Layer 0・#56）")
    ap.add_argument("files", nargs="*", help="検査する .py ファイル")
    ap.add_argument("--changed", action="store_true", help="git 差分の .py を検査")
    ap.add_argument("--self-test", action="store_true", help="自己テストを実行")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    targets = list(args.files)
    if args.changed:
        targets += _changed_python_files()
    targets = [t for t in dict.fromkeys(targets) if t.endswith(".py")]
    if not targets:
        print("[scan] 対象 .py ファイルなし")
        return 0

    n_error = 0
    for t in targets:
        for lineno, sev, code, msg in scan_file(t):
            print(f"[scan] {sev} {code} {t}:{lineno} {msg}")
            if sev == "ERROR":
                n_error += 1
    if n_error:
        print(f"[scan] ERROR {n_error} 件（要修正）")
        return 1
    print("[scan] ERROR なし")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[scan] checker error: {e}", file=sys.stderr)
        sys.exit(2)
