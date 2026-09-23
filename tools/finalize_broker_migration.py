#!/usr/bin/env python3
"""finalize_broker_migration.py — secrets-broker 移行の最終フリップ（生キー削除）を仕組み化

飼い主ゴール（2026-06-20）:
  「すべての処理で問題がないことが確認できたら最終移行されるように仕組み化」

本ツールは「最終移行（GitHub Variables から生 API キーを削除）」を **動作確認ゲート（verify_broker_migration.py）
を通過したときだけ** 実行できるよう仕組み化する。ゲートが NOT_READY の間は --apply でも削除しない。

安全設計（多層）:
  1) 削除前に必ず verify_broker_migration の gate を実行し、READY でなければ中止（生キー削除＝フォールバック喪失のため）。
  2) bootstrap（SECRETS_BROKER_URL/TOKEN・GH_TOKEN）と運用フラグは削除対象から除外。
  3) 既定は --dry-run（削除候補の表示のみ）。実削除は --apply 必須。
  4) --canary: 非クリティカルな1キー（既定 DASHBOARD_TOKEN）だけ削除し、次セッションで生キー削除後の
     ブローカー経路を本番で確認する（段階移行の第一歩）。
  5) 削除した値はブローカーに保持されているため復旧可能（--restore で broker から Variables へ書き戻す）。
  6) 値は一切表示しない（P-12）。

使い方:
  python3 tools/finalize_broker_migration.py                 # ゲート確認 + 削除候補のドライラン
  python3 tools/finalize_broker_migration.py --canary --apply # カナリア1キーのみ削除（要 READY）
  python3 tools/finalize_broker_migration.py --drain --apply  # 全生キー削除（要 READY・最終移行）
  python3 tools/finalize_broker_migration.py --restore KEY    # broker 値を Variables に書き戻す（復旧）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

CANARY_DEFAULT = os.environ.get("BROKER_CANARY_KEY", "DASHBOARD_TOKEN")  # 非クリティカル・段階移行の第一歩（base は env で上書き可・#3482）

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify_broker_migration.py")

# verify_broker_migration.py と同じ除外・接尾辞・リポジトリ検出（ドリフト防止のため import して使う）
sys.path.insert(0, HERE)
from verify_broker_migration import is_migrated_key, STAGING_SUFFIX, _detect_repo  # noqa: E402

REPO = _detect_repo()  # 汎用化（#3482）: git remote / env から自動検出


def run_gate() -> tuple[bool, str]:
    try:
        out = subprocess.run(
            [sys.executable, VERIFY, "--gate"],
            capture_output=True, text=True, timeout=40,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"ゲート実行失敗: {e}"
    return out.returncode == 0, (out.stdout or out.stderr).strip()


def list_migrated_var_names() -> list[str]:
    out = subprocess.run(
        ["gh", "variable", "list", "-R", REPO, "--json", "name", "--jq", ".[].name"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        return []
    return [n for n in out.stdout.split() if is_migrated_key(n)]


def list_staged_var_names(suffix: str) -> list[str]:
    """rename ステージング済みの温存キー（`*{suffix}`）を列挙する。"""
    out = subprocess.run(
        ["gh", "variable", "list", "-R", REPO, "--json", "name", "--jq", ".[].name"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        return []
    return [n for n in out.stdout.split() if n.endswith(suffix)]


def broker_value(key: str) -> str | None:
    url = os.environ.get("SECRETS_BROKER_URL", "").strip()
    token = os.environ.get("SECRETS_BROKER_TOKEN", "").strip()
    if not url or not token:
        return None
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}scope={key}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "curl/8.5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get(key)
    except Exception:  # noqa: BLE001
        return None


def delete_var(name: str) -> bool:
    out = subprocess.run(
        ["gh", "variable", "delete", name, "-R", REPO],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        print(f"  ❌ {name}: 削除失敗 ({out.stderr.strip()[:100]})")
        return False
    print(f"  🗑️  {name}: 削除完了（値はブローカーに保持・復旧可）")
    return True


def variable_value(name: str) -> str | None:
    """GitHub Variables の現在値を取得（値は呼び出し側で表示しないこと・P-12）。"""
    out = subprocess.run(
        ["gh", "variable", "list", "-R", REPO, "--json", "name,value"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        return None
    try:
        arr = json.loads(out.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None
    return next((v["value"] for v in arr if v["name"] == name), None)


def set_var_stdin(name: str, value: str) -> bool:
    """値を stdin 経由で gh variable set する（コマンドライン引数に値を載せない・P-12）。"""
    out = subprocess.run(
        ["gh", "variable", "set", name, "-R", REPO],
        input=value, capture_output=True, text=True, timeout=20,
    )
    return out.returncode == 0


def rename_var(name: str, suffix: str) -> bool:
    """生キーを delete する代わりに `{name}{suffix}` へ改名（ソフトデリート・復旧はブローカー非依存）。

    手順: 現値を取得 → `{name}{suffix}` に温存コピー → 元の `{name}` を削除。
    これで元の名前が消えてブローカー経路が発火し、値は GitHub 内に残るため `--restore-rename` で即復旧できる。
    """
    staged = f"{name}{suffix}"
    val = variable_value(name)
    if val is None:
        print(f"  ❌ {name}: 現値を取得できない（既に削除済み? Variables 未登録?）")
        return False
    if not set_var_stdin(staged, val):
        print(f"  ❌ {name}: 温存コピー {staged} の作成に失敗")
        return False
    out = subprocess.run(
        ["gh", "variable", "delete", name, "-R", REPO],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        print(f"  ❌ {name}: 元キー削除に失敗（{out.stderr.strip()[:100]}）。"
              f"温存コピー {staged} は作成済み — 手動確認推奨")
        return False
    print(f"  🔁 {name} → {staged}（改名・値は GitHub 内に温存・ブローカー経路が発火）")
    return True


def restore_rename(name: str, suffix: str) -> int:
    """rename カナリアを元に戻す（`{name}{suffix}` → `{name}`）。ブローカー非依存の復旧。"""
    staged = f"{name}{suffix}"
    val = variable_value(staged)
    if val is None:
        print(f"復旧失敗: 温存キー {staged} が見つからない")
        return 1
    if not set_var_stdin(name, val):
        print(f"復旧失敗: {name} の再作成に失敗")
        return 1
    subprocess.run(["gh", "variable", "delete", staged, "-R", REPO],
                   capture_output=True, text=True, timeout=20)
    print(f"✅ {staged} → {name} に名前を戻して復旧（値は非表示・ブローカー非依存）")
    return 0


def restore_var(name: str) -> int:
    val = broker_value(name)
    if val is None:
        print(f"復旧失敗: {name} をブローカーから取得できない")
        return 1
    out = subprocess.run(
        ["gh", "variable", "set", name, "-R", REPO, "--body", val],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0:
        print(f"復旧失敗: gh variable set {name}: {out.stderr.strip()[:100]}")
        return 1
    print(f"✅ {name} をブローカー値で Variables に復旧（値は非表示）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="secrets-broker 最終移行（生キー削除）の仕組み化")
    ap.add_argument("--apply", action="store_true", help="実削除（未指定はドライラン）")
    ap.add_argument("--canary", action="store_true", help="カナリア1キーのみ削除")
    ap.add_argument("--canary-rename", action="store_true",
                    help="カナリア1キーを削除せず改名（ソフトデリート・復旧はブローカー非依存で安全）")
    ap.add_argument("--stage-all-rename", action="store_true",
                    help="全生キーを {key}{suffix} へ改名（2段階移行の第1段・値は GitHub 内に温存）")
    ap.add_argument("--purge-staged", action="store_true",
                    help="ステージング済み温存キー（*{suffix}）を全削除（2段階移行の第2段・平文を最終排除）")
    ap.add_argument("--restore-all-staged", action="store_true",
                    help="全ステージング済みキーを元名に戻す（一括復旧・ブローカー非依存）")
    ap.add_argument("--canary-key", default=CANARY_DEFAULT, help=f"カナリア対象キー（既定 {CANARY_DEFAULT}）")
    ap.add_argument("--suffix", default=STAGING_SUFFIX, help=f"改名カナリアの接尾辞（既定 {STAGING_SUFFIX}）")
    ap.add_argument("--drain", action="store_true", help="全生キー削除（最終移行）")
    ap.add_argument("--restore", metavar="KEY", help="broker 値を Variables へ書き戻す（復旧）")
    ap.add_argument("--restore-rename", metavar="KEY",
                    help="改名カナリアを元に戻す（{KEY}{suffix} → {KEY}・ブローカー非依存）")
    ap.add_argument("--skip-gate", action="store_true", help="ゲートを省略（非推奨・復旧時のみ）")
    args = ap.parse_args()

    if not REPO:
        print("リポジトリを自動検出できません（env SECRETS_BROKER_REPO か GITHUB_REPOSITORY を設定）。"
              "誤ったプロジェクトへの破壊的操作を防ぐため中止します。", file=sys.stderr)
        return 1

    if args.restore:
        return restore_var(args.restore)
    if args.restore_rename:
        return restore_rename(args.restore_rename, args.suffix)
    if args.restore_all_staged:
        staged = list_staged_var_names(args.suffix)
        if not staged:
            print("ステージング済みキーなし（復旧対象なし）")
            return 0
        print(f"一括復旧: {len(staged)} キーを元名へ戻す")
        rc = 0
        for s in staged:
            base = s[: -len(args.suffix)]
            if restore_rename(base, args.suffix) != 0:
                rc = 1
        return rc

    # ステージング済み温存キーの最終削除（2段階移行 第2段）: 平文を GitHub から最終排除。
    # 汎用ゲート（Variables の生キー parity）はステージング後に生キーが消えて NOT_READY になるため使えない。
    # 代わりに「各温存キー {base}{suffix} に対応する {base} がブローカーに同値で存在するか」を独自検証する。
    if args.purge_staged:
        staged = list_staged_var_names(args.suffix)
        print(f"\n温存キー最終削除（平文の最終排除）: 対象 {len(staged)} キー（*{args.suffix}）")
        if not staged:
            print("ステージング済みキーなし。先に --stage-all-rename --apply を実行する。")
            return 0
        # 独自ゲート: 全温存キーの base がブローカーに同値で存在することを確認（値非表示）
        unsafe = []
        for s in staged:
            base = s[: -len(args.suffix)]
            sval = variable_value(s)
            bval = broker_value(base)
            if sval is None or bval is None or \
               hashlib.sha256(sval.encode()).hexdigest() != hashlib.sha256(bval.encode()).hexdigest():
                unsafe.append(base)
        if unsafe and not args.skip_gate:
            print(f"🔴 中止: ブローカーに同値が無い温存キーがある（{', '.join(unsafe)}）。"
                  "削除するとフォールバック喪失のため purge しない。")
            return 10
        print("🟢 全温存キーの base がブローカーに同値で存在（削除しても broker で復元可）")
        if not args.apply:
            for n in staged:
                print(f"  - {n}（dry-run）")
            print("（--apply で実削除。削除後の復旧はブローカー経由 --restore <KEY> --skip-gate）")
            return 0
        ok = 0
        for n in staged:
            if delete_var(n):
                ok += 1
        print(f"\n最終削除完了: {ok}/{len(staged)} キー。平文を GitHub から排除した。")
        return 0 if ok == len(staged) else 1

    # --- 動作確認ゲート（最重要・READY でなければ削除しない） ---
    if not args.skip_gate:
        ready, msg = run_gate()
        print(f"ゲート: {'🟢 READY' if ready else '🔴 NOT_READY'} — {msg}")
        if not ready:
            print("動作確認が未完了のため削除を中止する（フォールバック喪失を防ぐ）。")
            return 10

    # 改名カナリア（ソフトデリート）: delete せず {key}{suffix} へ温存
    if args.canary_rename:
        key = args.canary_key
        staged = f"{key}{args.suffix}"
        print(f"\n改名カナリア: {key} → {staged}（ソフトデリート・復旧はブローカー非依存）")
        if not args.apply:
            print(f"  - {key} → {staged}（dry-run）")
            print("（--apply で実行 / 復旧は --restore-rename "
                  f"{key} --skip-gate）")
            return 0
        ok = rename_var(key, args.suffix)
        if ok:
            print("次セッションの session-start.sh stderr に "
                  "'secrets-broker: loaded N secret(s)' が出れば本番でブローカー経路が機能。")
            print(f"復旧: python3 tools/finalize_broker_migration.py --restore-rename {key} --skip-gate")
        return 0 if ok else 1

    # 全件 rename ステージング（2段階移行 第1段）: 全生キーを {key}{suffix} へ温存改名
    if args.stage_all_rename:
        targets = list_migrated_var_names()
        print(f"\n全件 rename ステージング: {len(targets)} キーを {{key}}{args.suffix} へ改名"
              "（値は GitHub 内に温存・復旧はブローカー非依存）")
        if not args.apply:
            for n in targets:
                print(f"  - {n} → {n}{args.suffix}（dry-run）")
            print("（--apply で実行 / 一括復旧は --restore-all-staged --skip-gate）")
            return 0
        ok = 0
        for n in targets:
            if rename_var(n, args.suffix):
                ok += 1
        print(f"\nステージング完了: {ok}/{len(targets)} キー")
        print("次セッションの session-start.sh stderr に "
              "'secrets-broker: loaded N secret(s)'（N≈33）が出れば全件ブローカー経路で稼働。")
        print("確認後の最終排除: python3 tools/finalize_broker_migration.py --purge-staged --apply")
        print("一括復旧: python3 tools/finalize_broker_migration.py --restore-all-staged --skip-gate")
        return 0 if ok == len(targets) else 1

    # 削除対象の決定
    if args.canary:
        targets = [args.canary_key]
    elif args.drain:
        targets = list_migrated_var_names()
    else:
        targets = list_migrated_var_names()
        print(f"削除候補（移行対象生キー）: {len(targets)} 個")
        for n in targets:
            print(f"  - {n}")
        print("\n（ドライラン。--canary --apply で1キー / --drain --apply で全削除）")
        return 0

    print(f"\n削除対象: {len(targets)} キー  mode={'CANARY' if args.canary else 'DRAIN'}")
    if not args.apply:
        for n in targets:
            print(f"  - {n}（dry-run）")
        print("（--apply で実削除）")
        return 0

    ok = 0
    for n in targets:
        if delete_var(n):
            ok += 1
    print(f"\n削除完了: {ok}/{len(targets)} キー")
    print("次セッションの session-start.sh stderr に "
          "'secrets-broker: loaded N secret(s)' が出れば本番でブローカー経路が機能（N≈33）。")
    print("復旧が必要な場合: python3 tools/finalize_broker_migration.py --restore <KEY> --skip-gate")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
