#!/usr/bin/env python3
"""verify_broker_migration.py — secrets-broker 移行の動作確認ゲート（仕組み化の中核）

目的（飼い主ゴール 2026-06-20）:
  「ひと通りのワークフローで動作確認できてから環境変数を削除したい。チェックリストで確認・管理し、
   すべての処理で問題がないと確認できたら最終移行されるように仕組み化」

設計の鍵（消費側の一元化）:
  secrets-broker の取得は session-start.sh に一元化されており、ワークフローは個別に GitHub や
  ブローカーへ問い合わせない（os.environ を読むだけ）。よって「ブローカーが GitHub Variables と
  同一のキー集合・同一の値を返す」ことを 1 回証明すれば、いま Variables で動いている全ワークフローは
  ブローカー経由でも動く（= 全処理を一括でカバーする論理的保証）。

  本ツールはそれを「値を一切表示せずに（P-12）」検証する:
    1) ブローカー（認証付き GET・チャンク結合復元）から取得したキー束
    2) gh variable list（name+value）の移行対象キー
  の「キー名の一致」と「値の SHA-256 一致」を突合する。

さらにワークフロー→必要キーのマップを持ち、各ワークフローが「必要キーすべてをブローカーが
正しい値で返せるか」をチェックリストとして表示する（人間可読の動作確認管理）。

ゲート:
  - 全移行対象キーがブローカーに存在し、かつ値ハッシュが一致 → READY（exit 0）
  - 1 つでも欠落・不一致 → NOT_READY（exit 10）。理由をキー名で表示（値は出さない）

使い方:
  python3 tools/verify_broker_migration.py            # 人間可読レポート
  python3 tools/verify_broker_migration.py --json     # 機械可読（Issue 更新・CI 用）
  python3 tools/verify_broker_migration.py --gate      # ゲート専用（READY なら exit 0・else 10）
  python3 tools/verify_broker_migration.py --self-test # 内部ロジックの自己テスト（ネットワーク不要）

安全:
  - 値は一切 stdout/stderr に出さない。比較は SHA-256 ハッシュのみ。
  - 取得失敗・未設定時は graceful に NOT_READY（exit 10）で返す（破壊操作の前段ゲートのため安全側）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request


def _detect_repo() -> str:
    """対象リポジトリ（owner/repo）を自動検出する（汎用化・#3482）。

    優先順: env SECRETS_BROKER_REPO → env GITHUB_REPOSITORY → git remote の URL 解析。
    どれも解決できない場合は **空文字を返す**（特定プロジェクトをハードコードで既定にしない）。
    空のまま gh 操作に進むと安全側に失敗する（誤ったプロジェクトの broker を対象にしない）。
    クラウドプロキシ環境では `gh repo view` が使えないため git remote を直接解析する。
    """
    for env in ("SECRETS_BROKER_REPO", "GITHUB_REPOSITORY"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        # 末尾 .git を除去し、最後の owner/repo を抽出
        url = re.sub(r"\.git$", "", url)
        m = re.search(r"[/:]([^/]+/[^/]+)$", url)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


REPO = _detect_repo()

# bootstrap・broker 自身・非機密の運用フラグ（移行対象から除外＝ブローカーに入れない/削除しない）
# setup_secrets_broker.sh の EXCLUDE_REGEX と一致させること（ドリフト防止・L-094）
EXCLUDE_REGEX = re.compile(
    r"^(GH_TOKEN|SECRETS_BROKER_URL|SECRETS_BROKER_TOKEN|CLOUDFLARE_API_TOKEN|"
    r"TZ|GIT_TERMINAL_PROMPT|HOOK_PROFILE|PATH|HOME|PWD|SHLVL|_)$"
)

# rename カナリア（ソフトデリート）のステージング接尾辞。
# 生キーを delete する代わりに「{name}{STAGING_SUFFIX}」へ改名して GitHub 内に温存すると、
# 元の名前が消えてブローカー経路が発火しつつ、復旧は「名前を戻すだけ」（ブローカー非依存）になる。
# 改名後の温存キーは移行対象でも broker 欠落でもないため、parity/ゲート判定から除外する。
STAGING_SUFFIX = "_PREMIGRATE"

# ワークフロー → そのワークフローが必要とする環境変数（チェックリスト表示用・人間可読の動作確認管理）。
# プロジェクト固有のため config/broker_workflows.json に外出しする（汎用化・#3482）。
# base へコピーしたプロジェクトは JSON を差し替えるだけでよい（コード変更不要）。
# ゲート判定は「全移行対象キーの parity」で行うため、このマップは網羅でなくてもゲートの厳密性は落ちない
# （マップに無いキーも parity チェックで必ず検証される）。JSON が空 or 不在ならチェックリスト表示のみスキップ。
def load_workflow_keys() -> dict[str, list[str]]:
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "broker_workflows.json"
    )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}
    except Exception:  # noqa: BLE001
        return {}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_migrated_key(name: str) -> bool:
    """移行対象（ブローカーに入れる/最終的に Variables から消すべき）キーか。

    rename カナリアで温存した `{name}{STAGING_SUFFIX}` は移行対象ではない（ブローカーに無いのが正常）
    ため除外する。これがないと parity が「broker 欠落」と誤判定しゲートが NOT_READY に落ちる。
    """
    if name.endswith(STAGING_SUFFIX):
        return False
    return bool(re.match(r"^[A-Z]", name)) and not EXCLUDE_REGEX.match(name)


def fetch_broker() -> tuple[dict[str, str] | None, str]:
    url = os.environ.get("SECRETS_BROKER_URL", "").strip()
    token = os.environ.get("SECRETS_BROKER_TOKEN", "").strip()
    if not url or not token:
        return None, "SECRETS_BROKER_URL / SECRETS_BROKER_TOKEN が未設定（このセッションでは検証不可）"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "curl/8.5.0",  # CF WAF 1010 回避（L-084 / L-087）
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        return None, f"ブローカー取得失敗: {type(e).__name__}（認証/結合復元/到達性のいずれか）"
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, "ブローカー応答が JSON でない（チャンク結合の復元に失敗の可能性）"
    if not isinstance(data, dict):
        return None, "ブローカー応答が dict でない"
    return {str(k): str(v) for k, v in data.items()}, "ok"


def fetch_variables() -> tuple[dict[str, str] | None, str]:
    if not REPO:
        return None, "リポジトリを自動検出できません（env SECRETS_BROKER_REPO か GITHUB_REPOSITORY を設定）"
    try:
        out = subprocess.run(
            ["gh", "variable", "list", "-R", REPO, "--json", "name,value"],
            capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        return None, "gh CLI 不在"
    except subprocess.TimeoutExpired:
        return None, "gh variable list タイムアウト"
    if out.returncode != 0:
        return None, f"gh variable list 失敗: {out.stderr.strip()[:120]}"
    try:
        arr = json.loads(out.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None, "gh variable list の JSON パース失敗"
    return {v["name"]: v["value"] for v in arr}, "ok"


def build_report() -> dict:
    broker, b_msg = fetch_broker()
    variables, v_msg = fetch_variables()

    report: dict = {
        "broker_ok": broker is not None,
        "broker_msg": b_msg,
        "variables_ok": variables is not None,
        "variables_msg": v_msg,
        "ready": False,
        "reasons": [],
        "parity": {"matched": [], "missing_in_broker": [], "value_mismatch": [], "extra_in_broker": []},
        "broker_key_count": len(broker) if broker else 0,
        "workflows": {},
    }

    if broker is None:
        report["reasons"].append(b_msg)
    if variables is None:
        report["reasons"].append(v_msg)
    if broker is None or variables is None:
        return report

    migrated = {n: val for n, val in variables.items() if is_migrated_key(n)}

    broker_hashes = {k: _sha(v) for k, v in broker.items()}
    for name, val in sorted(migrated.items()):
        if name not in broker:
            report["parity"]["missing_in_broker"].append(name)
        elif broker_hashes[name] != _sha(val):
            report["parity"]["value_mismatch"].append(name)
        else:
            report["parity"]["matched"].append(name)

    # ブローカーにあるが Variables の移行対象に無い余剰キー（情報・ゲートには影響させない）
    for name in sorted(broker):
        if name not in migrated:
            report["parity"]["extra_in_broker"].append(name)

    # ワークフロー別カバレッジ（チェックリスト表示）
    # 判定基準は「ブローカーが正しい値で供給できるか」。移行のゴールはキーを Variables から
    # ブローカーへ移すことなので、Variables 不在（=移行済み/カナリア改名済み）は問題ではない。
    # Variables にも残っている場合のみ値の一致を追加検証する。
    for wf, keys in load_workflow_keys().items():
        covered, problems = [], []
        for k in keys:
            if k not in broker:
                if k in variables:
                    problems.append(f"{k}(broker欠落・bundle再投入が必要)")
                else:
                    problems.append(f"{k}(broker・Variables両方に無い)")
            elif k in variables and broker_hashes[k] != _sha(variables[k]):
                problems.append(f"{k}(値不一致)")
            else:
                covered.append(k)  # ブローカーが供給可能（Variables 不在＝移行済みでも OK）
        report["workflows"][wf] = {
            "ok": len(problems) == 0,
            "covered": covered,
            "problems": problems,
        }

    p = report["parity"]
    if not p["missing_in_broker"] and not p["value_mismatch"] and migrated:
        report["ready"] = True
    else:
        if not migrated:
            report["reasons"].append("移行対象キーが Variables に存在しない")
        if p["missing_in_broker"]:
            report["reasons"].append(f"ブローカー欠落: {', '.join(p['missing_in_broker'])}")
        if p["value_mismatch"]:
            report["reasons"].append(f"値不一致: {', '.join(p['value_mismatch'])}")
    return report


def print_human(report: dict) -> None:
    print("== secrets-broker 移行 動作確認 ==")
    print(f"ブローカー取得: {'OK' if report['broker_ok'] else 'NG'}（{report['broker_msg']}）"
          f"  キー数={report['broker_key_count']}")
    print(f"Variables 取得: {'OK' if report['variables_ok'] else 'NG'}（{report['variables_msg']}）")
    p = report["parity"]
    print(f"\n-- parity（値は SHA-256 で照合・平文非表示） --")
    print(f"  一致: {len(p['matched'])} キー")
    if p["missing_in_broker"]:
        print(f"  ⚠️ ブローカー欠落: {', '.join(p['missing_in_broker'])}")
    if p["value_mismatch"]:
        print(f"  ⚠️ 値不一致: {', '.join(p['value_mismatch'])}")
    if p["extra_in_broker"]:
        print(f"  （参考）ブローカー側余剰: {', '.join(p['extra_in_broker'])}")
    print(f"\n-- ワークフロー別チェックリスト --")
    for wf, st in report["workflows"].items():
        mark = "✅" if st["ok"] else "❌"
        detail = "" if st["ok"] else f"  → {', '.join(st['problems'])}"
        print(f"  {mark} {wf}（{len(st['covered'])}/{len(st['covered']) + len(st['problems'])} キー）{detail}")
    print()
    if report["ready"]:
        print("🟢 READY: 全移行対象キーがブローカーで一致。生キー削除（Phase 3）に進める。")
    else:
        print("🔴 NOT_READY: 以下を解消するまで生キーを削除しない:")
        for r in report["reasons"]:
            print(f"  - {r}")


def self_test() -> int:
    assert is_migrated_key("OPENAI_API_KEY")
    assert not is_migrated_key("GH_TOKEN")
    assert not is_migrated_key("SECRETS_BROKER_TOKEN")
    assert not is_migrated_key("lowercase_var")
    # rename カナリアの温存キーは parity から除外される
    assert not is_migrated_key("DASHBOARD_TOKEN" + STAGING_SUFFIX)
    assert _sha("abc") == _sha("abc")
    assert _sha("abc") != _sha("abd")
    # parity ロジック（擬似データ）
    broker = {"OPENAI_API_KEY": "v1", "EXAMPLE_SERVICE_TOKEN": "v2"}
    variables = {"OPENAI_API_KEY": "v1", "EXAMPLE_SERVICE_TOKEN": "vX", "GH_TOKEN": "boot"}
    migrated = {n: v for n, v in variables.items() if is_migrated_key(n)}
    mism = [n for n, v in migrated.items() if broker.get(n) and _sha(broker[n]) != _sha(v)]
    assert mism == ["EXAMPLE_SERVICE_TOKEN"], mism
    print("self-test: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="secrets-broker 移行の動作確認ゲート")
    ap.add_argument("--json", action="store_true", help="機械可読 JSON 出力")
    ap.add_argument("--gate", action="store_true", help="ゲート専用（READY なら exit 0・else 10）")
    ap.add_argument("--self-test", action="store_true", help="ネットワーク非依存の自己テスト")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    report = build_report()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.gate:
        print_human(report)

    if args.gate:
        if report["ready"]:
            print("READY")
            return 0
        print("NOT_READY: " + " / ".join(report["reasons"]))
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
