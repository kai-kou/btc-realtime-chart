#!/usr/bin/env python3
"""merge_three_way.py — 祖先つき 3 方向マージと安全性検証（apply-to-repo.sh から呼ばれる）。

apply-to-repo.sh は長らく「ベース最新 → 下流」の 2 方向コピーだけで同期しており、下流が
SYNC_PATHS 配下のファイルへ加えた変更（.claude/settings.json の独自フック matcher・
.mcp.json の独自サーバ・docs/rules/*.md の独自節など）が再適用のたびに無条件で消えていた。
本スクリプトは「前回適用時のベース内容」を祖先として `git merge-file` に渡し、
クリーンにマージできたときだけ結果を採用する。

**衝突マーカーをワークツリーに書かない** のが設計の中核。マージが衝突した場合や検証に
失敗した場合は何も出力せず、呼び出し側が下流のファイルを温存する（壊れた settings.json で
下流のセッションが起動不能になる／ルールファイルがマーカー付きのまま規範として読まれる、
という失敗モードを構造的に排除する）。

検証は 4 段:
  1. 衝突マーカー（<<<<<<< / ======= / >>>>>>>）が残っていないこと
  2. JSON なら構文が妥当であること
  3. JSON なら **重複キーが無いこと**
  4. JSON かつ ORDER_SENSITIVE_ARRAY_PATHS 対象なら **順序依存配列の相対順序が両側とも未決定でないこと**
3 が要るのは、同一階層の別位置に両側が同名キーを追加すると merge-file が衝突なしと判定し、
構文的にも妥当な「重複キー JSON」が生成されるため。この JSON はパース時に片方の値が
サイレントに消える（Python は最後の出現が勝つ）。構文検証だけでは検出できない。
4 が要るのは、`hooks.PreToolUse` のように要素順が実行順序を意味する配列で、両側が別位置に
新規エントリを挿入すると行としては非重複（=衝突なし・構文妥当）になるため。マージ後の
相対順序はどちらの入力も決めていない、たまたま生成された並びに過ぎない。

終了コード:
  0 … クリーンにマージでき検証も通った（--output へ書き出し済み）
  1 … 衝突または検証失敗（--output へは何も書かない）
  2 … 使い方・実行環境のエラー
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 衝突マーカーの検出は `<<<<<<< ` / `>>>>>>> ` の行頭一致だけで行う。
# 区切りの `=======` は Markdown の setext 見出し下線や水平線として本文に普通に
# 現れるため、これを見ると正当なマージを誤って捨てる。衝突そのものは
# git merge-file の終了コードで先に弾いているので、この走査は
# 「入力側に元から壊れたマーカーが混ざっていた場合」の二重防御にすぎない。
CONFLICT_PREFIXES = ("<<<<<<< ", ">>>>>>> ")
JSON_SUFFIXES = {".json"}
YAML_SUFFIXES = {".yaml", ".yml"}

# 要素順が実行順序を意味するため、両側が別位置に新規追加した要素が混在すると
# どちらの入力も相対順序を決めていない「未決定な順序」が生まれるフィールド。
# リポジトリルートからの相対パス（apply-to-repo.sh が --path-hint に渡す $rel と同じ形式）
# → (親キー, 子キー) のタプルで列挙する。子キーが "*" なら親オブジェクトの全キーに一致する
# （例: hooks.PreToolUse / hooks.PostToolUse ...）。
# permissions.allow のような「順序に意味の無い配列」はここに列挙しない限り対象外。
#
# 🔴 basename ではなく相対パスをキーにする（Issue #50）: 以前は basename "settings.json" だけで
# 一致させていたため、同じ basename を持つ下流の別ファイル（例: .vscode/settings.json）まで
# 本チェックの対象になり、hooks 配下に配列を持つだけで誤ってマージを拒否するリスクがあった。
ORDER_SENSITIVE_ARRAY_PATHS: dict[str, tuple[tuple[str, str], ...]] = {
    ".claude/settings.json": (("hooks", "*"),),
}


class ValidationError(Exception):
    """マージ結果が採用できないと判定された理由を持つ例外。"""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValidationError(f"重複キー: {key}")
        seen.add(key)
    return dict(pairs)


def _validate_yaml(text: str) -> None:
    """YAML の構文と重複キーを検証する（JSON と同じ理由で重複キーは値が消える）。

    オブジェクトを構築しない `yaml.compose_all`（SafeLoader）でノード木だけを組み、
    マッピングノードのキー重複を走査する。`yaml.load` を使わないので任意オブジェクト
    生成の危険が無い。
    """
    try:
        import yaml
    except ImportError as exc:
        # 検証できないものは採用しない（呼び出し側が下流を温存する側へ倒す）
        raise ValidationError("PyYAML が無いため YAML のマージ結果を検証できない") from exc

    def walk(node) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, value_node in node.value:
                key = getattr(key_node, "value", None)
                if isinstance(key, str):
                    if key in seen:
                        raise ValidationError(f"重複キー: {key}")
                    seen.add(key)
                walk(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    try:
        for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
            if doc is not None:
                walk(doc)
    except ValidationError:
        raise
    except yaml.YAMLError as exc:
        raise ValidationError(f"YAML 構文エラー: {exc}") from exc


def _canon(item: object) -> str:
    """配列要素の同一性比較用に正規化する（キー順序の違いを無視する）。"""
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _order_sensitive_arrays(doc: object, path_spec: tuple[tuple[str, str], ...]) -> dict[str, list]:
    """path_spec に一致する配列を {"親キー.子キー": [...]} で返す（一致しなければ空）。"""
    arrays: dict[str, list] = {}
    if not isinstance(doc, dict):
        return arrays
    for parent_key, child_key in path_spec:
        parent = doc.get(parent_key)
        if not isinstance(parent, dict):
            continue
        children = parent.items() if child_key == "*" else [(child_key, parent.get(child_key))]
        for key, value in children:
            if isinstance(value, list):
                arrays[f"{parent_key}.{key}"] = value
    return arrays


def _detect_order_ambiguity(
    base_arrays: dict[str, list],
    ours_arrays: dict[str, list],
    theirs_arrays: dict[str, list],
    merged_arrays: dict[str, list],
) -> list[str]:
    """ours・theirs がそれぞれ別の新規要素を同一配列に追加した場合、そのパスを返す。

    どちらの入力も「自分が追加した要素」と「相手が追加した要素」の相対順序を
    レビューしていないため、テキストマージがたまたま生成した並び順を採用しない。
    """
    ambiguous: list[str] = []
    for path, merged_list in merged_arrays.items():
        base_canon = {_canon(e) for e in base_arrays.get(path, [])}
        ours_added = {_canon(e) for e in ours_arrays.get(path, []) if _canon(e) not in base_canon}
        theirs_added = {_canon(e) for e in theirs_arrays.get(path, []) if _canon(e) not in base_canon}
        ours_only = ours_added - theirs_added
        theirs_only = theirs_added - ours_added
        if not ours_only or not theirs_only:
            continue
        merged_canon = {_canon(e) for e in merged_list}
        if (ours_only & merged_canon) and (theirs_only & merged_canon):
            ambiguous.append(path)
    return ambiguous


def _load_json_or_none(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def check_order_sensitive_arrays(
    ours: Path, base: Path, theirs: Path, merged_text: str, path_hint: Path
) -> None:
    """順序依存配列（ORDER_SENSITIVE_ARRAY_PATHS）に未決定な相対順序が無いか検証する。

    検出したら ValidationError を投げ、呼び出し側の既存の衝突処理（下流を温存し
    <path>.base-latest を併置）にそのまま合流させる。
    """
    path_spec = ORDER_SENSITIVE_ARRAY_PATHS.get(path_hint.as_posix())
    if not path_spec:
        return
    base_doc = _load_json_or_none(base)
    ours_doc = _load_json_or_none(ours)
    theirs_doc = _load_json_or_none(theirs)
    if base_doc is None or ours_doc is None or theirs_doc is None:
        # 入力側が JSON として読めない＝この関数の前提が成立しないため、
        # 「疑わしければ採用しない」という本モジュール全体の方針に合わせて拒否する
        # （黙ってチェックを飛ばして未検証のまま採用しない）。
        raise ValidationError(
            f"{path_hint.as_posix()} の入力（ours/base/theirs のいずれか）が JSON として読めないため、"
            "順序依存配列チェックを実施できない"
        )
    # merged_text は validate() が同じ suffix 条件で既に json.loads() を通しているため、
    # ここでの再パースが JSONDecodeError を投げることはない。
    merged_doc = json.loads(merged_text)
    ambiguous = _detect_order_ambiguity(
        _order_sensitive_arrays(base_doc, path_spec),
        _order_sensitive_arrays(ours_doc, path_spec),
        _order_sensitive_arrays(theirs_doc, path_spec),
        _order_sensitive_arrays(merged_doc, path_spec),
    )
    if ambiguous:
        raise ValidationError(
            "順序依存配列の相対順序が双方未決定: " + ", ".join(sorted(ambiguous))
        )


def validate(text: str, path_hint: Path) -> None:
    """採用可否を判定する。問題があれば ValidationError を投げる。"""
    for line in text.splitlines():
        if line.startswith(CONFLICT_PREFIXES):
            raise ValidationError("衝突マーカーが残っている")
    suffix = path_hint.suffix.lower()
    if suffix in JSON_SUFFIXES:
        try:
            json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except ValidationError:
            raise
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 構文エラー: {exc}") from exc
    elif suffix in YAML_SUFFIXES:
        _validate_yaml(text)


def merge(ours: Path, base: Path, theirs: Path, path_hint: Path) -> bytes:
    """3 方向マージ結果をバイト列で返す。衝突・検証失敗は ValidationError。

    バイト列で扱うのは意図的。text=True はロケール依存のデコードと改行の
    ユニバーサル変換を伴い、非 UTF-8 ファイルで例外を投げ、CRLF のファイルを
    黙って LF へ書き換えてしまう。検証はデコードしたテキストに対して行い、
    書き出しは元のバイト列のまま行う。
    """
    proc = subprocess.run(
        ["git", "merge-file", "-p", "--quiet", str(ours), str(base), str(theirs)],
        capture_output=True,
    )
    # git merge-file は衝突数を返す（128 以上・負値は実行エラー）
    if proc.returncode < 0 or proc.returncode > 127:
        raise ValidationError(f"git merge-file が異常終了した（code={proc.returncode}）")
    if proc.returncode > 0:
        raise ValidationError(f"衝突 {proc.returncode} 件")
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        # バイナリ・非 UTF-8 は行ベースのマージ結果を検証できないので採用しない
        raise ValidationError("UTF-8 として読めないためマージ結果を採用しない")
    validate(text, path_hint)
    if path_hint.suffix.lower() in JSON_SUFFIXES:
        check_order_sensitive_arrays(ours, base, theirs, text, path_hint)
    return proc.stdout


def _self_test() -> int:
    """代表ケースを一時ディレクトリで検証する（CI・セルフレビュー用）。"""
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        def write(name: str, text: str) -> Path:
            p = d / name
            p.write_text(text, encoding="utf-8")
            return p

        # 1. 離れた箇所の編集はクリーンにマージされる
        anc = write("a.json", '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n')
        ours = write("o.json", '{\n  "a": 1,\n  "b": 2,\n  "c": 3,\n  "mine": true\n}\n')
        theirs = write("t.json", '{\n  "a": 9,\n  "b": 2,\n  "c": 3\n}\n')
        try:
            merged = merge(ours, anc, theirs, Path("x.json")).decode("utf-8")
            data = json.loads(merged)
            check("離れた編集が両方保持される", data == {"a": 9, "b": 2, "c": 3, "mine": True})
        except ValidationError as exc:
            failures.append(f"離れた編集がマージできない: {exc}")

        # 2. 同一行の衝突は採用しない
        ours2 = write("o2.json", '{\n  "a": 111\n}\n')
        theirs2 = write("t2.json", '{\n  "a": 222\n}\n')
        anc2 = write("a2.json", '{\n  "a": 1\n}\n')
        try:
            merge(ours2, anc2, theirs2, Path("x.json"))
            failures.append("同一行の衝突が検出されない")
        except ValidationError:
            pass

        # 3. 重複キー JSON（衝突ゼロ・構文妥当）を採用しない
        anc3 = write("a3.json", '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n')
        ours3 = write(
            "o3.json", '{\n  "a": 1,\n  "cache": {"ttl": 60},\n  "b": 2,\n  "c": 3\n}\n'
        )
        theirs3 = write(
            "t3.json", '{\n  "a": 1,\n  "b": 2,\n  "c": 3,\n  "cache": {"ttl": 300}\n}\n'
        )
        try:
            merge(ours3, anc3, theirs3, Path("x.json"))
            failures.append("重複キー JSON が採用されてしまう")
        except ValidationError as exc:
            check("重複キーが理由として報告される", "重複キー" in str(exc))

        # 4. Markdown も同じ経路で扱える（節の追加は衝突しない）
        md_anc = "# T\n\n## A\n\n本文 A\n\n## B\n\n本文 B\n"
        anc4 = write("a.md", md_anc)
        ours4 = write("o.md", md_anc + "\n## 下流独自\n\n下流の節\n")
        theirs4 = write("t.md", md_anc.replace("本文 A", "本文 A（ベース更新）"))
        try:
            merged4 = merge(ours4, anc4, theirs4, Path("x.md")).decode("utf-8")
            check("下流の節が残る", "## 下流独自" in merged4)
            check("ベースの更新が入る", "本文 A（ベース更新）" in merged4)
        except ValidationError as exc:
            failures.append(f"Markdown がマージできない: {exc}")

        # 5. 単体挙動の確認: 祖先が空（add/add 相当）で内容が異なれば採用しない
        #    （呼び出し側の apply-to-repo.sh は祖先にパスが無い時点で衝突扱いにするため、
        #     この経路は本スクリプト単体の耐性を確かめるもの）
        empty = write("empty", "")
        ours5 = write("o5.md", "下流版\n")
        theirs5 = write("t5.md", "ベース版\n")
        try:
            merge(ours5, empty, theirs5, Path("x.md"))
            failures.append("add/add の相違が検出されない")
        except ValidationError:
            pass

        # 6. 非 UTF-8 は例外を投げずに採用拒否として扱う
        body = b"\xff\xfe A\nB\nC\nD\nE\n"
        b_anc = d / "b_anc.bin"; b_anc.write_bytes(body)
        b_ours = d / "b_ours.bin"; b_ours.write_bytes(body + b"F\n")
        b_theirs = d / "b_theirs.bin"; b_theirs.write_bytes(body.replace(b"A\n", b"A2\n"))
        try:
            merge(b_ours, b_anc, b_theirs, Path("x.bin"))
            failures.append("非 UTF-8 が採用されてしまう")
        except ValidationError as exc:
            check("非 UTF-8 が理由として報告される", "UTF-8" in str(exc))
        except UnicodeDecodeError:
            failures.append("非 UTF-8 で未捕捉の UnicodeDecodeError が出る")

        # 7. CRLF が LF へ書き換えられない
        crlf_anc = d / "c_anc.md"; crlf_anc.write_bytes(b"# T\r\n\r\nA\r\n\r\nB\r\n")
        crlf_ours = d / "c_ours.md"; crlf_ours.write_bytes(b"# T\r\n\r\nA\r\n\r\nB\r\n\r\nmine\r\n")
        crlf_theirs = d / "c_theirs.md"; crlf_theirs.write_bytes(b"# T\r\n\r\nA2\r\n\r\nB\r\n")
        try:
            out = merge(crlf_ours, crlf_anc, crlf_theirs, Path("x.md"))
            check("CRLF が保たれる", b"\r\n" in out and out.replace(b"\r\n", b"").count(b"\n") == 0)
        except ValidationError as exc:
            failures.append(f"CRLF がマージできない: {exc}")

        # 8. YAML の重複キー（衝突ゼロ・構文妥当）を採用しない
        y_anc = d / "y_anc.yaml"; y_anc.write_text("modules:\n  a:\n    enabled: true\n  b:\n    enabled: true\n", encoding="utf-8")
        y_ours = d / "y_ours.yaml"; y_ours.write_text("modules:\n  a:\n    enabled: true\n  qux:\n    enabled: false\n  b:\n    enabled: true\n", encoding="utf-8")
        y_theirs = d / "y_theirs.yaml"; y_theirs.write_text("modules:\n  a:\n    enabled: true\n  b:\n    enabled: true\n  qux:\n    enabled: true\n", encoding="utf-8")
        try:
            merge(y_ours, y_anc, y_theirs, Path("x.yaml"))
            failures.append("YAML の重複キーが採用されてしまう")
        except ValidationError as exc:
            check("YAML の重複キーが理由として報告される", "重複キー" in str(exc) or "PyYAML" in str(exc))

        # 9. 本文中の `=======` を衝突マーカーと誤判定しない
        m_anc = d / "m_anc.md"; m_anc.write_text("見出し\n=======\n\nA\n\nB\n", encoding="utf-8")
        m_ours = d / "m_ours.md"; m_ours.write_text("見出し\n=======\n\nA\n\nB\n\n独自\n", encoding="utf-8")
        m_theirs = d / "m_theirs.md"; m_theirs.write_text("見出し\n=======\n\nA2\n\nB\n", encoding="utf-8")
        try:
            m_out = merge(m_ours, m_anc, m_theirs, Path("x.md")).decode("utf-8")
            check("本文の ======= を誤判定しない", "独自" in m_out and "A2" in m_out)
        except ValidationError as exc:
            failures.append(f"本文の ======= を衝突と誤判定した: {exc}")

        # 10. 実行権限がマージ結果へ引き継がれる（フックの +x を落とさない）
        import os
        import stat as _stat
        sh_anc = d / "h_anc.sh"; sh_anc.write_text("#!/bin/sh\necho a\necho b\necho c\n", encoding="utf-8")
        sh_ours = d / "h_ours.sh"; sh_ours.write_text("#!/bin/sh\necho a\necho b\necho c\necho mine\n", encoding="utf-8")
        sh_theirs = d / "h_theirs.sh"; sh_theirs.write_text("#!/bin/sh\necho A2\necho b\necho c\n", encoding="utf-8")
        os.chmod(sh_ours, 0o755)
        sh_out = d / "h_out.sh"
        rc = main(["--ours", str(sh_ours), "--base", str(sh_anc), "--theirs", str(sh_theirs),
                   "--output", str(sh_out), "--path-hint", "hook.sh"])
        check("実行可能ファイルのマージが成功する", rc == 0)
        if rc == 0:
            check("実行権限が引き継がれる", bool(sh_out.stat().st_mode & _stat.S_IXUSR))

        # 11. settings.json の hooks.* に両側が別位置へ新規エントリを挿入すると、
        #     行としては非重複（クリーンマージ）でも相対順序が未決定として拒否される。
        #     本チェック（check_order_sensitive_arrays）を呼ばなければ、この anc11/ours11/theirs11 は
        #     git merge-file 単体で EXIT=0（クリーン）と判定され、validate() の衝突マーカー・JSON構文・
        #     重複キー検証もすべて通ってしまう（#507 の再現ケース）。
        def hooks_doc(entries: list[str]) -> str:
            items = ",\n      ".join(
                f'{{"matcher": "{m}", "hooks": [{{"type": "command", "command": "{m}.sh"}}]}}'
                for m in entries
            )
            return f'{{\n  "hooks": {{\n    "PreToolUse": [\n      {items}\n    ]\n  }}\n}}\n'

        anc11 = write("anc11.json", hooks_doc(["A", "B", "C"]))
        ours11 = write("ours11.json", hooks_doc(["A", "X", "B", "C"]))
        theirs11 = write("theirs11.json", hooks_doc(["A", "B", "C", "Y"]))
        try:
            merge(ours11, anc11, theirs11, Path(".claude/settings.json"))
            failures.append("順序依存配列（hooks.PreToolUse）の未決定な相対順序を検出できない")
        except ValidationError as exc:
            check("hooks.PreToolUse が理由に含まれる", "hooks.PreToolUse" in str(exc))

        # 12. 同じ挿入パターンでも順序に意味の無い配列（permissions.allow 等）は対象外
        def perms_doc(entries: list[str]) -> str:
            items = ",\n      ".join(f'"{e}"' for e in entries)
            return f'{{\n  "permissions": {{\n    "allow": [\n      {items}\n    ]\n  }}\n}}\n'

        anc12 = write("anc12.json", perms_doc(["Bash(ls)", "Bash(pwd)", "Bash(whoami)"]))
        ours12 = write("ours12.json", perms_doc(["Bash(ls)", "Bash(git status)", "Bash(pwd)", "Bash(whoami)"]))
        theirs12 = write("theirs12.json", perms_doc(["Bash(ls)", "Bash(pwd)", "Bash(whoami)", "Bash(echo hi)"]))
        try:
            merged12 = merge(ours12, anc12, theirs12, Path(".claude/settings.json")).decode("utf-8")
            check("permissions.allow は順序依存扱いにならない", "Bash(git status)" in merged12 and "Bash(echo hi)" in merged12)
        except ValidationError as exc:
            failures.append(f"順序に意味の無い配列を過剰に要確認へ倒した: {exc}")

        # 13. hooks.* のワイルドカードは複数イベント名に一致するが、
        #     別々のイベント配列をそれぞれ片側だけが変更した場合はキーをまたいで誤検知しない
        def multi_event_doc(pre_entries: list[str], post_entries: list[str]) -> str:
            def block(name: str, entries: list[str]) -> str:
                items = ",\n      ".join(f'"{e}"' for e in entries)
                return f'    "{name}": [\n      {items}\n    ]'

            return (
                "{\n  \"hooks\": {\n"
                + block("PreToolUse", pre_entries)
                + ",\n"
                + block("PostToolUse", post_entries)
                + "\n  }\n}\n"
            )

        anc13 = write("anc13.json", multi_event_doc(["A", "B"], ["P", "Q"]))
        ours13 = write("ours13.json", multi_event_doc(["A", "X", "B"], ["P", "Q"]))
        theirs13 = write("theirs13.json", multi_event_doc(["A", "B"], ["P", "Q", "Z"]))
        try:
            merge(ours13, anc13, theirs13, Path(".claude/settings.json"))
        except ValidationError as exc:
            failures.append(f"別イベント名を別側が独立に変更しただけなのに誤検知した: {exc}")

        # 14. basename が同じ "settings.json" でも .claude/ 配下でない別ファイルは対象外（Issue #50）。
        #     11 と同じ hooks.PreToolUse の未決定パターンでも、path_hint が相対パスで一致しなければ
        #     順序依存配列チェック自体が発動せず、クリーンマージとして採用される。
        anc14 = write("anc14.json", hooks_doc(["A", "B", "C"]))
        ours14 = write("ours14.json", hooks_doc(["A", "X", "B", "C"]))
        theirs14 = write("theirs14.json", hooks_doc(["A", "B", "C", "Y"]))
        try:
            merge(ours14, anc14, theirs14, Path(".vscode/settings.json"))
        except ValidationError as exc:
            failures.append(f"basename 一致だけで .claude/settings.json 以外まで誤検知した: {exc}")

    if failures:
        for f in failures:
            print(f"❌ {f}", file=sys.stderr)
        return 1
    print("✅ merge_three_way self-test: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ours", help="下流の現在のファイル")
    parser.add_argument("--base", help="祖先（前回適用時のベース）のファイル")
    parser.add_argument("--theirs", help="ベース最新のファイル")
    parser.add_argument("--output", help="マージ結果の書き出し先（クリーン時のみ書く）")
    parser.add_argument(
        "--path-hint",
        default="",
        help="検証の種別判定に使う論理パス（省略時は --output の名前を使う）",
    )
    parser.add_argument("--self-test", action="store_true", help="内蔵テストを実行する")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    missing = [n for n in ("ours", "base", "theirs", "output") if not getattr(args, n)]
    if missing:
        parser.error(f"--{' --'.join(missing)} は必須です（--self-test 以外）")

    paths = {n: Path(getattr(args, n)) for n in ("ours", "base", "theirs")}
    for name, path in paths.items():
        if not path.is_file():
            print(f"[merge3] 入力が読めません（--{name}）: {path}", file=sys.stderr)
            return 2

    hint = Path(args.path_hint or args.output)
    try:
        merged = merge(paths["ours"], paths["base"], paths["theirs"], hint)
    except ValidationError as exc:
        print(f"[merge3] 採用しません（{exc}）: {hint}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("[merge3] git が見つかりません", file=sys.stderr)
        return 2

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(merged)
    # パーミッションを下流の現行ファイルから引き継ぐ。呼び出し側はこの出力を cp -a で
    # 配置するため、新規作成のまま（644）だと実行可能なフック・スクリプトの +x が落ち、
    # 次のセッションで無言のまま起動しなくなる。
    shutil.copymode(paths["ours"], out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
