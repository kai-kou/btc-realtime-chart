#!/usr/bin/env python3
"""check_distribution_boundary.py — 配布境界（SYNC_PATHS / PUBLISH_PATHS / plugin.json）の退行を検知する（Issue #542）

`scripts/apply-to-repo.sh`（下流への同期境界）・`scripts/publish-snapshot.sh`（公開境界）・
`scripts/bootstrap.sh`（下流適用後のベース固有配布物の除去）が守っている前提を機械 assert する。
Layer 1 セルフレビュー（#541）で判明したとおり、これらの前提を破る PR を検知するチェッカーが
存在しなかった（`grep` で見つかるのは `self_review_check.py` の CJK 半角スペース検査だけで、
`SYNC_PATHS` の中身自体は検査していなかった）。

`.sh` ファイルのみを書き換える PR（このファイル自身は diff に含まれない）でも起動できるよう、
`tools/self_review_check.py` の `COMPANION_SELF_TESTS` に登録済み（トリガー: `apply-to-repo.sh` /
`publish-snapshot.sh` / `bootstrap.sh` のいずれかの変更）。

## 検知する退行

1. `SYNC_PATHS` に `REMOVE_PATHS` エントリの祖先ディレクトリが丸ごと列挙されている
   （例: `.claude-plugin/marketplace.json` が REMOVE_PATHS にあるのに `.claude-plugin` /
   `.claude-plugin/`（表記揺れ含む）が SYNC_PATHS にあると、marketplace.json が下流へ渡り、
   下流が「claude-code-base を配布するマーケットプレイス」を名乗ってしまう）
2. `REMOVE_PATHS` から `.claude-plugin/marketplace.json` が抜けている
   （SYNC_PATHS から外しただけでは、過去の適用で下流へ渡ったファイルが残り続ける）
3. `bootstrap.sh` に `REMOVE_PATHS` の各エントリを force フラグ付き（`-f` / `-rf` / `-fr` /
   `--force` いずれかの形式）で削除するステップが無い
   （新規クローンにベース固有の配布物が残り続ける。エントリ 1 件限定ではなく全件を検証する）
4. `PUBLISH_PATHS` と `SYNC_PATHS` の顔ぶれが、意図的な差（`KNOWN_PUBLISH_ONLY_EXTRAS` /
   `KNOWN_SYNC_TO_PUBLISH_MAP` に列挙済みの差）だけで説明できない
   （新しい差分が生まれたら、このファイルへ意図的に追記することを強制する。#448 のように
   コメントで注意喚起するだけでは機械的に検知できない）
5. `KNOWN_SYNC_TO_PUBLISH_MAP` のキーが `SYNC_PATHS` から消えている
   （このキーは無条件に公開境界チェックを免除されるため、キー不在を検知しないと該当エントリの
   SYNC_PATHS からの削除が退行として検知されない）
6. `SYNC_PATHS` / `REMOVE_PATHS` / `PUBLISH_PATHS` が想定書式（単独行の複数行 bash 配列宣言）で
   見つからない（書式変更で他の assert が黙ってすり抜けるのを防ぐ）
7. `extract_bash_array` が行末インラインコメント内のクォート文字列を配列要素として誤抽出する
   （`"a/b"  # "c/d" は対象外` のような行でコメント中の値を要素として拾ってしまう・#542）

## 使い方

    python3 tools/check_distribution_boundary.py            # 人間可読の要約
    python3 tools/check_distribution_boundary.py --json      # 機械可読 JSON
    python3 tools/check_distribution_boundary.py --self-test # 抽出・assert ロジックの自己テスト

## 終了コード

  0 = 全 assert が PASS
  1 = 退行を検知（いずれかの assert が FAIL）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_TO_REPO_SH = REPO_ROOT / "scripts" / "apply-to-repo.sh"
PUBLISH_SNAPSHOT_SH = REPO_ROOT / "scripts" / "publish-snapshot.sh"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "bootstrap.sh"

# PUBLISH_PATHS のうち「第三者への公開に必須だが、下流への同期対象ではない」既知の追加分
# （scripts/publish-snapshot.sh のコメントで説明済みのエントリ）。
KNOWN_PUBLISH_ONLY_EXTRAS = {
    "README.md",
    "LICENSE",
    "NOTICE",
    "CLAUDE.md",
    "REVIEW.md",   # PROTECT_PATHS 経由で配置される較正ファイル（CLAUDE.md と同じ扱い・#627）
    ".gitignore",
    "docs/project-mission.md",
    "docs/CONTEXT.md",
    ".github",
    "docs/apply-to-existing-repo.md",
    "docs/base-update-notes.md",
    "config/publish_events.yaml",
    "config/data_only_path_prefixes.txt",
    "config/pr_review_comment_categories.json",
    ".claude/settings.json",
    # tsukuru がフレームワークとして配る雛形そのもの（母艦固有の実データではない・Issue #59）。
    # 下流へは apply-to-repo.sh で配らず、配布物 kai-kou/tsukuru にだけ入れる。
    "docs/framework-templates",
    # 自律運転設定一式の雛形（FR-23）。framework-templates と分けて置く理由は #112。
    "docs/autonomy-templates",
}

# SYNC_PATHS の値 → それをカバーする PUBLISH_PATHS の値（ディレクトリ丸ごと配布などで
# 文字列が完全一致しない意図的な差）。
KNOWN_SYNC_TO_PUBLISH_MAP = {
    ".claude-plugin/plugin.json": ".claude-plugin",
}


# --- 抽出ロジック（純粋関数・--self-test の対象） -----------------------------------


def strip_inline_comment(line: str) -> str:
    """行末インラインコメント（クォート外の最初の `#` 以降）を切り落とす。

    `"a/b"  # "c/d" は対象外` のような行で、コメント中のダブルクォート文字列を配列要素として
    誤抽出しないための前処理（#542 のレビューで判明した検知漏れ）。ダブルクォートの開閉状態を
    1 パスで追跡し、クォート外で `#` に当たった時点で切り落とす。
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "#" and not in_quotes:
            return line[:i]
    return line


def extract_bash_array(text: str, array_name: str) -> list[str] | None:
    """`ARRAY_NAME=( ... )` ブロック（単独行の複数行配列宣言）から、ダブルクォート文字列の
    要素を順に抽出する。

    対応する書式は本リポジトリの実際の配列定義（`NAME=(` が単独行、要素が 1 つ以上の行に
    ダブルクォート文字列として並び、閉じ括弧 `)` が単独行）のみ。1 行完結の配列宣言
    （`NAME=("a" "b")`）・`declare -a` 修飾・`+=` 追記・シングルクォート要素・空文字列要素
    には対応しない。コメント専用行（先頭の空白を除いて `#` で始まる行）は無視する。

    配列自体が想定書式で見つからない場合は **None** を返す（要素ゼロの配列 `[]` と区別する
    ため）。呼び出し側はこれを「書式変更でパースできなくなった」ことを示す失敗として扱うこと
    （黙って `[]` にフォールバックすると、その配列が実際には退行した内容を含んでいても
    検知できなくなり、チェッカーの目的そのものが無効化される・#542 のレビューで判明）。
    """
    lines = text.splitlines()
    start = None
    pattern = re.compile(rf"^\s*{re.escape(array_name)}=\(\s*$")
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i + 1
            break
    if start is None:
        return None
    values: list[str] = []
    for line in lines[start:]:
        if line.strip() == ")":
            break
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        values.extend(re.findall(r'"([^"]+)"', strip_inline_comment(line)))
    return values


def bootstrap_removes_path(bootstrap_text: str, path: str) -> bool:
    """bootstrap.sh に `path` を `rm -f`（force フラグ付き）で消す行があるか判定する。

    バックスラッシュ行継続（`rm -f \\` を挟んで次行にパスが続く書式）は結合してから判定する。
    `-f` 単独だけでなく `-rf` / `-fr` のような複合ショートオプション・`--force` のロングオプション
    も検出する（`"-f" in stripped` だけでは `-rf` を検出できず誤 FAIL する・#542 のレビューで判明）。
    `&&` 連結・変数展開のみで組み立てたパスには対応しない（既知の適用範囲限定。本リポジトリの
    実際の bootstrap.sh の書式で十分）。
    """
    joined = re.sub(r"\\\r?\n", " ", bootstrap_text)
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not (stripped.startswith("rm ") or stripped.startswith("rm\t")):
            continue
        if path not in stripped:
            continue
        tokens = stripped.split()
        has_force = any(
            tok == "--force" or (tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:])
            for tok in tokens[1:]
        )
        if has_force:
            return True
    return False


# --- assert ロジック（純粋関数・--self-test の対象） --------------------------------


def check_boundary(
    sync_paths: list[str],
    remove_paths: list[str],
    publish_paths: list[str] | None,
    bootstrap_text: str,
) -> list[str]:
    """検知した退行の説明文リストを返す（空リスト＝退行なし）。

    `publish_paths` が None のときは公開境界（PUBLISH_PATHS との突き合わせ）の assert を
    スキップし、同期境界の assert だけを実行する。`scripts/publish-snapshot.sh` は
    公開スナップショットを出す側（ベース）だけが持つスクリプトで、下流リポジトリには
    存在しないため（下流で常に「ファイルが見つかりません」で落ちるのを避ける）。
    """
    failures: list[str] = []

    sync_dirs = {p.rstrip("/") for p in sync_paths}
    regressed_ancestors: set[str] = set()
    for r in remove_paths:
        parts = r.rstrip("/").split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if ancestor in sync_dirs:
                regressed_ancestors.add(ancestor)
    if regressed_ancestors:
        failures.append(
            "SYNC_PATHS に REMOVE_PATHS のエントリの祖先ディレクトリ "
            f"{sorted(regressed_ancestors)} が丸ごと列挙されている"
            "（該当ファイルが下流へ渡ってしまう。個別ファイルのみを列挙すること）"
        )

    if ".claude-plugin/marketplace.json" not in remove_paths:
        failures.append(
            "REMOVE_PATHS に '.claude-plugin/marketplace.json' が含まれていない"
            "（過去に配布した marketplace.json が下流に残り続ける）"
        )

    missing_bootstrap_removal = sorted(
        p for p in remove_paths if not bootstrap_removes_path(bootstrap_text, p)
    )
    if missing_bootstrap_removal:
        failures.append(
            f"bootstrap.sh に REMOVE_PATHS のエントリ {missing_bootstrap_removal} を rm -f する"
            "ステップが見つからない（新規クローンにベース固有の配布物が残り続ける）"
        )

    if publish_paths is None:
        return failures

    sync_set = set(sync_paths)
    publish_set = set(publish_paths)

    missing_mapped = sorted(k for k in KNOWN_SYNC_TO_PUBLISH_MAP if k not in sync_set)
    if missing_mapped:
        failures.append(
            "KNOWN_SYNC_TO_PUBLISH_MAP のキー "
            f"{missing_mapped} が SYNC_PATHS に見つからない"
            "（このキーは無条件に公開境界チェックを免除されるため、SYNC_PATHS から該当エントリが"
            "消えても退行として検知されない。SYNC_PATHS 側の変更に合わせて "
            "KNOWN_SYNC_TO_PUBLISH_MAP を更新すること）"
        )

    sync_only = {
        s for s in sync_set
        if s not in publish_set and KNOWN_SYNC_TO_PUBLISH_MAP.get(s) not in publish_set
    }
    if sync_only:
        failures.append(
            "SYNC_PATHS に PUBLISH_PATHS でカバーされない未知のエントリがある: "
            f"{sorted(sync_only)}（新しい同期対象を公開境界からも意図的に含めるか除外するか判断し、"
            "publish-snapshot.sh の PUBLISH_PATHS を更新すること）"
        )

    mapped_publish_values = set(KNOWN_SYNC_TO_PUBLISH_MAP.values())
    publish_only = publish_set - sync_set - mapped_publish_values
    unexplained = publish_only - KNOWN_PUBLISH_ONLY_EXTRAS
    if unexplained:
        failures.append(
            "PUBLISH_PATHS に SYNC_PATHS にも KNOWN_PUBLISH_ONLY_EXTRAS にも無い未知のエントリがある: "
            f"{sorted(unexplained)}（意図的な公開専用エントリなら "
            "tools/check_distribution_boundary.py の KNOWN_PUBLISH_ONLY_EXTRAS に追記すること）"
        )

    return failures


def _extract_required_array(
    text: str, array_name: str, source_name: str, failures: list[str]
) -> list[str]:
    """`extract_bash_array` を呼び、None（書式不一致）なら failures に追加して空リストへ
    フォールバックする（呼び出し元が黙って空リストのまま assert を続けないための境界）。"""
    values = extract_bash_array(text, array_name)
    if values is None:
        failures.append(
            f"{source_name} から '{array_name}=(' の想定書式（単独行の複数行配列宣言）が"
            "見つかりません。書式が変わった場合は tools/check_distribution_boundary.py の"
            "抽出ロジックを更新してください。"
        )
        return []
    return values


# 🔻 下流差分（tsukuru-studio・台帳: local-modules.yaml の base_local_diffs）
# publish-snapshot.sh は公開スナップショットを出す側（ベース）だけが持つため、下流では
# 公開境界の assert をスキップする。この分岐を落とすと下流の PR 作成が常にブロックされる。
def run_check(
    apply_to_repo_sh: Path = APPLY_TO_REPO_SH,
    publish_snapshot_sh: Path = PUBLISH_SNAPSHOT_SH,
    bootstrap_sh: Path = BOOTSTRAP_SH,
) -> tuple[list[str], dict]:
    # publish-snapshot.sh は公開側（ベース）だけが持つ。下流では不在が正常なのでスキップする。
    missing = [str(p) for p in (apply_to_repo_sh, bootstrap_sh) if not p.exists()]
    if missing:
        reason = f"以下のファイルが見つかりません: {missing}"
        return [reason], {
            "sync_paths": [],
            "remove_paths": [],
            "publish_paths": [],
            "publish_boundary": "n/a",
        }

    apply_text = apply_to_repo_sh.read_text(encoding="utf-8")
    bootstrap_text = bootstrap_sh.read_text(encoding="utf-8")

    failures: list[str] = []
    sync_paths = _extract_required_array(apply_text, "SYNC_PATHS", "apply-to-repo.sh", failures)
    remove_paths = _extract_required_array(apply_text, "REMOVE_PATHS", "apply-to-repo.sh", failures)
    if publish_snapshot_sh.exists():
        publish_text = publish_snapshot_sh.read_text(encoding="utf-8")
        publish_paths = _extract_required_array(
            publish_text, "PUBLISH_PATHS", "publish-snapshot.sh", failures
        )
    else:
        publish_paths = None

    failures.extend(check_boundary(sync_paths, remove_paths, publish_paths, bootstrap_text))
    detail = {
        "sync_paths": sync_paths,
        "remove_paths": remove_paths,
        # None は「公開境界そのものが無い（下流リポジトリ）」。[] は「PUBLISH_PATHS が空＝退行」
        # なので潰さずに区別する（--json の消費者が「空になった」と誤読しないため）。
        "publish_paths": publish_paths,
        "publish_boundary": "checked" if publish_paths is not None else "n/a",
    }
    return failures, detail


# --- self-test ----------------------------------------------------------------


def run_self_test() -> int:
    failures = 0

    def check(name: str, cond: bool) -> None:
        nonlocal failures
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures += 1

    # --- extract_bash_array ---
    sample = '''
SYNC_PATHS=(
  "docs/rules"
  # "not/a/real/path" はコメント中でも無視される
  ".claude-plugin/plugin.json"
  "tools"
  "combo/a" "combo/b"
)
PROTECT_PATHS=(
  "CLAUDE.md"
)
EMPTY_ARR=(
)
'''
    extracted = extract_bash_array(sample, "SYNC_PATHS")
    check(
        "extract_bash_array がコメント行を無視し同一行複数クォートも抽出する",
        extracted == ["docs/rules", ".claude-plugin/plugin.json", "tools", "combo/a", "combo/b"],
    )
    check(
        "extract_bash_array は他の配列を混同しない",
        extract_bash_array(sample, "PROTECT_PATHS") == ["CLAUDE.md"],
    )
    check(
        "extract_bash_array は要素ゼロの配列を空リストで返す",
        extract_bash_array(sample, "EMPTY_ARR") == [],
    )
    check(
        "extract_bash_array は配列自体が見つからない場合 None を返す（[] と区別する）",
        extract_bash_array(sample, "NO_SUCH_ARRAY") is None,
    )
    check(
        "extract_bash_array は1行完結の配列宣言を書式不一致として None を返す",
        extract_bash_array('SYNC_PATHS=("a" "b")\n', "SYNC_PATHS") is None,
    )
    check(
        "extract_bash_array は行末インラインコメント内のクォート文字列を要素として抽出しない",
        extract_bash_array('SYNC_PATHS=(\n  "a/b"  # "c/d" は対象外\n)\n', "SYNC_PATHS") == ["a/b"],
    )

    # --- _extract_required_array ---
    f1: list[str] = []
    result1 = _extract_required_array(sample, "SYNC_PATHS", "test.sh", f1)
    check(
        "_extract_required_array は正常系で failures に追加しない",
        f1 == [] and result1 == extracted,
    )
    f2: list[str] = []
    result2 = _extract_required_array('SYNC_PATHS=("a")\n', "SYNC_PATHS", "test.sh", f2)
    check(
        "_extract_required_array は書式不一致を failures に追加し空リストへフォールバックする",
        result2 == [] and len(f2) == 1 and "test.sh" in f2[0],
    )

    # --- bootstrap_removes_path ---
    good_bootstrap = 'rm -f "$ROOT/.claude-plugin/marketplace.json"\n'
    check(
        "bootstrap_removes_path が正常系を検出する",
        bootstrap_removes_path(good_bootstrap, ".claude-plugin/marketplace.json"),
    )
    check(
        "bootstrap_removes_path はコメントアウトされた行を検出しない",
        not bootstrap_removes_path(
            '# rm -f "$ROOT/.claude-plugin/marketplace.json"\n', ".claude-plugin/marketplace.json"
        ),
    )
    check(
        "bootstrap_removes_path は無関係な rm を誤検出しない",
        not bootstrap_removes_path('rm -f "$ROOT/some/other/file"\n', ".claude-plugin/marketplace.json"),
    )
    continuation_bootstrap = 'rm -f \\\n  "$ROOT/.claude-plugin/marketplace.json"\n'
    check(
        "bootstrap_removes_path はバックスラッシュ行継続を結合して検出する",
        bootstrap_removes_path(continuation_bootstrap, ".claude-plugin/marketplace.json"),
    )
    check(
        "bootstrap_removes_path は rm -rf 形式（複合ショートオプション）を検出する",
        bootstrap_removes_path(
            'rm -rf "$ROOT/.claude-plugin/marketplace.json"\n', ".claude-plugin/marketplace.json"
        ),
    )
    check(
        "bootstrap_removes_path は --force ロングオプションを検出する",
        bootstrap_removes_path(
            'rm --force "$ROOT/.claude-plugin/marketplace.json"\n', ".claude-plugin/marketplace.json"
        ),
    )

    # --- check_boundary: 正常系（現行の実データ相当の最小フィクスチャ）---
    good_sync = [".claude-plugin/plugin.json", "tools", "config/x.yaml"]
    good_remove = [".claude-plugin/marketplace.json"]
    good_publish = [".claude-plugin", "tools", "config/x.yaml", "README.md"]
    good_bootstrap_text = 'rm -f "$ROOT/.claude-plugin/marketplace.json"\n'

    normal_failures = check_boundary(good_sync, good_remove, good_publish, good_bootstrap_text)
    check("正常系（既知の差分のみ）は失敗ゼロ", normal_failures == [])

    with_unknown_extra = good_publish + ["unknown-extra.txt"]
    unknown_failures = check_boundary(good_sync, good_remove, with_unknown_extra, good_bootstrap_text)
    check(
        "未知の PUBLISH_PATHS 追加を検知する",
        any("unknown-extra.txt" in f for f in unknown_failures),
    )

    # --- check_boundary: 退行系 ---
    regressed_sync = good_sync + [".claude-plugin"]
    r1 = check_boundary(regressed_sync, good_remove, good_publish, good_bootstrap_text)
    check(
        "SYNC_PATHS に '.claude-plugin' が丸ごと入る退行を検知する",
        any(".claude-plugin" in f and "丸ごと" in f for f in r1),
    )

    regressed_sync_trailing_slash = good_sync + [".claude-plugin/"]
    r1b = check_boundary(regressed_sync_trailing_slash, good_remove, good_publish, good_bootstrap_text)
    check(
        "SYNC_PATHS に '.claude-plugin/'（末尾スラッシュの表記揺れ）が丸ごと入る退行も検知する",
        any(".claude-plugin" in f and "丸ごと" in f for f in r1b),
    )

    r2 = check_boundary(good_sync, [], good_publish, good_bootstrap_text)
    check(
        "REMOVE_PATHS から marketplace.json が消える退行を検知する",
        any("REMOVE_PATHS" in f and "marketplace.json" in f for f in r2),
    )

    r3 = check_boundary(good_sync, good_remove, good_publish, "echo no-op\n")
    check(
        "bootstrap.sh から削除ステップが消える退行を検知する",
        any("bootstrap.sh に REMOVE_PATHS" in f for f in r3),
    )

    r3b = check_boundary(
        good_sync, good_remove + [".claude-plugin/other.json"], good_publish, good_bootstrap_text
    )
    check(
        "REMOVE_PATHS の新規エントリが bootstrap.sh に追従していない退行を検知する（1件限定でなく全件検証）",
        any(".claude-plugin/other.json" in f for f in r3b),
    )

    unexplained_sync = good_sync + ["some/new/sync/only/path"]
    r4 = check_boundary(unexplained_sync, good_remove, good_publish, good_bootstrap_text)
    check(
        "SYNC_PATHS 独自の未説明パスを検知する",
        any("some/new/sync/only/path" in f for f in r4),
    )

    sync_missing_mapped_key = [p for p in good_sync if p != ".claude-plugin/plugin.json"]
    r5 = check_boundary(sync_missing_mapped_key, good_remove, good_publish, good_bootstrap_text)
    check(
        "KNOWN_SYNC_TO_PUBLISH_MAP のキーが SYNC_PATHS から消える退行を検知する"
        "（値が無条件免除されるため、キー不在を assert しないと PASS してしまう）",
        any("KNOWN_SYNC_TO_PUBLISH_MAP" in f for f in r5),
    )

    # --- run_check: 実ファイルに対する疎通確認（ファイルが存在し、抽出が空でないこと）---
    real_failures, real_detail = run_check()
    check(
        "実ファイルの SYNC_PATHS 抽出が空でない（パーサ疎通確認）",
        len(real_detail["sync_paths"]) > 0,
    )
    check(
        "実ファイルの PUBLISH_PATHS 抽出が空でない（パーサ疎通確認・公開境界がある場合のみ）",
        len(real_detail["publish_paths"] or []) > 0
        if real_detail["publish_boundary"] == "checked"
        else real_detail["publish_paths"] is None,
    )
    check("現行リポジトリの実データで assert が全て PASS する（退行なし）", real_failures == [])

    # --- run_check: 対象ファイル不在時の missing 分岐 ---
    nonexistent = REPO_ROOT / "tools" / "__no_such_file_for_selftest__.sh"
    missing_failures, missing_detail = run_check(apply_to_repo_sh=nonexistent)
    check(
        "run_check はファイル不在時に missing を failures に含める",
        len(missing_failures) == 1 and str(nonexistent) in missing_failures[0],
    )
    check(
        "run_check はファイル不在時に detail を空のまま返す",
        missing_detail == {
            "sync_paths": [],
            "remove_paths": [],
            "publish_paths": [],
            "publish_boundary": "n/a",
        },
    )

    status = "✅ self-test PASS" if not failures else f"❌ self-test FAIL: {failures} 件"
    print(f"\n{status}")
    return 0 if not failures else 1


# --- main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="機械可読 JSON で出力する")
    parser.add_argument("--self-test", action="store_true", help="抽出・assert ロジックの自己テストを実行する")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    failures, detail = run_check()

    if args.json:
        print(json.dumps({"ok": not failures, "failures": failures, **detail}, ensure_ascii=False, indent=2))
        return 0 if not failures else 1

    if detail.get("publish_boundary") == "n/a":
        print(
            "[check_distribution_boundary] 公開境界の検査はスキップしました"
            f"（{PUBLISH_SNAPSHOT_SH.name} が無い＝公開スナップショットを出さないリポジトリ）"
        )

    if not failures:
        print("[check_distribution_boundary] OK（配布境界の退行なし）")
        return 0

    print("[check_distribution_boundary] 配布境界の退行を検知しました:")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
