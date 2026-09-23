#!/usr/bin/env python3
"""apply-base 同期後の自己検査スクリプト（Issue #3）。

apply-base（scripts/apply-to-repo.sh）による同期は、成功しても失敗しても「正常系のログ
1行」として流れてしまい、壊れていても気づく契機が無かった。founding 議論で特定した
2つの実害経路（① 子固有資産が失われる ② project.name が非公開母艦名へ化ける。後者は
実際に再現確認済み）を含め、同期直後に壊れやすい 4 項目を検査する。

検査項目:
  1. local-modules.yaml（子固有資産の台帳・Issue #2）に列挙されたスキル・ルール・
     ツールが全件ディスク上に残っているか
  2. .claude/rules/ の symlink が壊れていないか（リンク切れ／台帳の hot:true ルールの
     symlink 欠落）
  3. modules.yaml の project.name / repo / timezone が期待値のままか（repo は
     git remote origin と照合。プレースホルダ未置換も検出）
  4. scripts/bootstrap.sh（および、まだ残存していれば旧・modules.yaml 専用マージスクリプト）の
     プレースホルダ保護構造が実プロジェクト名へ化けていないか

fail-closed: 判定材料が揃わない場合（台帳ファイルが無い・git remote を解決できない等）も
PASS 扱いにせず非ゼロ終了する（「たぶん大丈夫」で通さない）。

Usage:
  python3 tools/check_base_sync_integrity.py              # カレントリポジトリを検査
  python3 tools/check_base_sync_integrity.py --root PATH  # 対象リポジトリのルートを指定
  python3 tools/check_base_sync_integrity.py --self-test  # 変異テスト込みの自己テスト
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "--quiet"])
    import yaml


class CheckResult:
    def __init__(self, name: str, ok: bool, details: list[str]):
        self.name = name
        self.ok = ok
        self.details = details


# --- 1. 子固有資産（local-modules.yaml 台帳）の残存確認 ---

def check_local_assets(root: Path) -> CheckResult:
    name = "子固有資産の残存（local-modules.yaml）"
    ledger = root / "local-modules.yaml"
    if not ledger.exists():
        return CheckResult(name, False, [f"{ledger} が存在しません（台帳自体が失われています）"])
    try:
        data = yaml.safe_load(ledger.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return CheckResult(name, False, [f"{ledger} の YAML 解析に失敗しました: {e}"])

    assets = data.get("assets") or {}
    if not isinstance(assets, dict):
        return CheckResult(name, False, ["台帳の assets がマッピング形式ではありません（台帳の構造が壊れている可能性）"])
    missing: list[str] = []
    checked = 0
    for category, entries in assets.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("path", "detail_path"):
                rel = entry.get(key)
                if not rel or not isinstance(rel, str):
                    continue
                checked += 1
                # 台帳はリポジトリ管理下のファイルだが、リポジトリ外を指すパスは
                # 誤記・改変の兆候として扱う（fail-closed）。
                resolved = (root / rel).resolve()
                try:
                    resolved.relative_to(root.resolve())
                except ValueError:
                    missing.append(f"{category}/{entry.get('name', '?')}: {rel} はリポジトリ外を指しています")
                    continue
                if not resolved.exists():
                    missing.append(f"{category}/{entry.get('name', '?')}: {rel} が存在しません")

    if checked == 0:
        return CheckResult(name, False, ["台帳に検査対象の資産パスが 1 件もありません（台帳の構造が壊れている可能性）"])
    if missing:
        return CheckResult(name, False, missing)
    return CheckResult(name, True, [f"{checked} 件の資産パスが全て残存しています"])


# --- 2. .claude/rules/ symlink の健全性 ---

def check_rules_symlinks(root: Path) -> CheckResult:
    name = ".claude/rules/ symlink 健全性"
    claude_rules = root / ".claude" / "rules"
    if not claude_rules.is_dir():
        return CheckResult(name, False, [f"{claude_rules} が存在しません"])

    problems: list[str] = []
    checked = 0
    for link in sorted(claude_rules.glob("*.md")):
        checked += 1
        if not link.is_symlink():
            problems.append(f"{link.name}: symlink ではなく実ファイルです")
            continue
        if not link.exists():  # symlink かつ resolve 先が無い = リンク切れ
            problems.append(f"{link.name}: リンク切れです")

    # 台帳側の hot:true ルールが symlink 自体を持っているかも検査する（正方向・fail-closed）
    ledger = root / "local-modules.yaml"
    if ledger.exists():
        try:
            data = yaml.safe_load(ledger.read_text(encoding="utf-8")) or {}
            assets = data.get("assets") if isinstance(data, dict) else None
            rules = assets.get("rules") if isinstance(assets, dict) else None
            for entry in rules or []:
                if not isinstance(entry, dict) or not entry.get("hot"):
                    continue
                rule_name = entry.get("name")
                # name はファイル名の一部として使うため、パスセパレータを含む値は拒否する
                if not isinstance(rule_name, str) or not rule_name or "/" in rule_name or "\\" in rule_name:
                    continue
                if not (claude_rules / f"{rule_name}.md").exists():
                    problems.append(f"{rule_name}: hot:true だが .claude/rules/{rule_name}.md が存在しません")
        except yaml.YAMLError:
            pass  # 台帳の YAML 解析失敗は check_local_assets 側で報告済み

    if checked == 0 and not problems:
        return CheckResult(name, False, [".claude/rules/ に .md が 1 件もありません"])
    if problems:
        return CheckResult(name, False, problems)
    return CheckResult(name, True, [f"{checked} 件の symlink が全て健全です"])


# --- 3. modules.yaml の project.* が期待値のままか ---

_PLACEHOLDER_TOKENS = {"", "kai-kou/btc-realtime-chart", "BTC Realtime Chart", "kai-kou/btc-realtime-chart", "BTC の価格変動をリアルタイムに確認できるトレーディング参考用 Web アプリ"}


def git_remote_repo_slug(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group("slug") if m else None


def check_modules_yaml_project(root: Path, expected_repo: str | None = None) -> CheckResult:
    name = "modules.yaml の project.*"
    path = root / "modules.yaml"
    if not path.exists():
        return CheckResult(name, False, [f"{path} が存在しません"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return CheckResult(name, False, [f"YAML 解析に失敗しました: {e}"])

    project = data.get("project") or {}
    if not isinstance(project, dict):
        return CheckResult(name, False, [f"project がマッピング形式ではありません（型: {type(project).__name__}）"])
    problems: list[str] = []
    for key in ("name", "repo", "timezone"):
        val = project.get(key)
        if not isinstance(val, str) or not val.strip():
            problems.append(f"project.{key} が空です")
        elif val in _PLACEHOLDER_TOKENS:
            problems.append(f"project.{key} が未置換のプレースホルダのままです: {val!r}")

    repo = project.get("repo")
    if expected_repo is None:
        expected_repo = git_remote_repo_slug(root)
    if expected_repo is None:
        problems.append("git remote origin から owner/repo を判定できず、project.repo の照合ができません（fail-closed）")
    elif isinstance(repo, str) and repo.strip() and repo not in _PLACEHOLDER_TOKENS and repo != expected_repo:
        problems.append(
            f"project.repo が git remote と食い違っています: modules.yaml={repo!r} / git={expected_repo!r}"
            "（母艦名等への化けを疑ってください）"
        )

    if problems:
        return CheckResult(name, False, problems)
    return CheckResult(name, True, [f"project.name/repo/timezone は期待どおりです（repo={repo!r}）"])


# --- 4. bootstrap.sh（および、まだ残存していれば旧マージスクリプト）のプレースホルダ保護 ---

# 以下の定数は scripts/bootstrap.sh 側のプレースホルダ表記をそのまま検査用に写している。
# プレースホルダの表記自体を変更する場合は、bootstrap.sh の実装とあわせて以下も更新すること
# （検査対象と実装が食い違うと誤検出・見逃しの原因になる）。
_BOOTSTRAP_MARKERS = ["kai-kou/btc-realtime-chart", "kai-kou/btc-realtime-chart", "BTC Realtime Chart", "BTC の価格変動をリアルタイムに確認できるトレーディング参考用 Web アプリ"]
# 旧・modules.yaml 専用マージスクリプト（親ベースの Issue #509 で 3 方向マージへ統合・廃止され、
# 本リポジトリでは Issue #43 で本体を削除済み）は、bootstrap.sh の sed がプレースホルダ定数
# 自身を誤置換しないよう、文字列を分割して埋め込んでいた。ファイルが残っている下流（未同期の
# スナップショット等）でだけ、この難読化構造が連続したプレースホルダ文字列に戻っていないかを
# 検査する。ファイルが無ければ正常（廃止済み）なのでスキップする。
_MERGE_OBFUSCATED_MARKERS = ['"{{" + "PROJECT_NAME" + "}}"', '"{{" + "REPO_SLUG" + "}}"', '"__" + "OWNER" + "__"']
_MERGE_FORBIDDEN_LITERALS = ["BTC Realtime Chart", "kai-kou/btc-realtime-chart", "kai-kou/btc-realtime-chart"]


def check_placeholder_protection(root: Path) -> CheckResult:
    name = "プレースホルダ保護（bootstrap.sh / 旧マージスクリプト残存時）"
    problems: list[str] = []

    bootstrap = root / "scripts" / "bootstrap.sh"
    if not bootstrap.exists():
        problems.append(f"{bootstrap} が存在しません")
    else:
        text = bootstrap.read_text(encoding="utf-8")
        for marker in _BOOTSTRAP_MARKERS:
            if marker not in text:
                problems.append(
                    f"scripts/bootstrap.sh からプレースホルダ {marker!r} が消えています"
                    "（自己置換で実プロジェクト名へ化けた疑い）"
                )

    merge_py = root / "scripts" / "merge_modules_yaml.py"
    if merge_py.exists():
        text = merge_py.read_text(encoding="utf-8")
        for marker in _MERGE_OBFUSCATED_MARKERS:
            if marker not in text:
                problems.append(
                    f"{merge_py} の難読化定数 {marker!r} が消えています"
                    "（置換保護が壊れている疑い）"
                )
        for literal in _MERGE_FORBIDDEN_LITERALS:
            if literal in text:
                problems.append(
                    f"{merge_py} に難読化されていないプレースホルダ {literal!r} が"
                    "直接出現しています（今後の bootstrap.sh 実行で誤置換される恐れ）"
                )
    # merge_py が存在しない場合は廃止済みで正常（Issue #43）なのでスキップする。

    if problems:
        return CheckResult(name, False, problems)
    return CheckResult(name, True, ["プレースホルダ保護が健全です"])


# --- 5. 置換除外台帳（.bootstrap-no-replace）に載せたファイルの保護 ---

# bootstrap.sh のプレースホルダ置換は、プレースホルダ表記を「説明のための文字列」として持つファイル
# （仕様書の引用・検査ツールの定数・逆変換スクリプトの置換先）まで実 slug へ書き換えてしまう。
# `.bootstrap-no-replace` はそれを防ぐ台帳だが、登録漏れやパス変更があるとベース同期のたびに静かに壊れる。
# 実測（2026-09-15 の同期）: publish-sync レーンの 4 ファイルが未登録で、逆変換の置換先 `kai-kou/btc-realtime-chart`
# が実 slug へ化けて逆変換が no-op になり、同時に書き換わったテスト側が自己成就で PASS した。
# 台帳に載っているファイルからプレースホルダ表記が 1 つも消えていないことを検査する。
_NO_REPLACE_LEDGER = ".bootstrap-no-replace"
# bootstrap.sh の置換は 2 パス構成で、2 パス目（`scripts/bootstrap.sh` の `s#kai-kou#…#g` /
# `s#btc-realtime-chart#…#g`）は **単独形** も置換する。結合形だけを見ると、単独形しか持たないファイル
# （「owner 名は kai-kou に置換される」のような説明文）を「化けた」と誤判定するため、
# 本検査だけ単独形を足した集合で判定する。`_BOOTSTRAP_MARKERS` 自体は
# `check_placeholder_protection` の全件一致判定に使うので広げない。
_NO_REPLACE_MARKERS = _BOOTSTRAP_MARKERS + ["kai-kou", "btc-realtime-chart"]


def _read_no_replace_entries(ledger: Path) -> list[str]:
    """台帳を bootstrap.sh と同じ規則で読む（`#` 以降はコメント・空行は無視）。"""
    entries: list[str] = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def check_no_replace_protection(root: Path) -> CheckResult:
    name = "置換除外台帳（.bootstrap-no-replace）の保護"
    ledger = root / _NO_REPLACE_LEDGER
    if not ledger.exists():
        # プレースホルダを説明として持つファイルが無い下流では台帳自体が無い。正常。
        return CheckResult(name, True, [f"{_NO_REPLACE_LEDGER} が無いため検査をスキップしました"])

    entries = _read_no_replace_entries(ledger)
    if not entries:
        return CheckResult(name, True, [f"{_NO_REPLACE_LEDGER} に有効なエントリがありません"])

    problems: list[str] = []
    for entry in entries:
        target = root / entry
        if not target.exists():
            problems.append(
                f"{_NO_REPLACE_LEDGER} のエントリが存在しません: {entry}"
                "（ファイルの移動・削除で台帳が腐っています。台帳を更新してください）"
            )
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            problems.append(f"{entry} を読み取れません: {exc}")
            continue
        if not any(marker in text for marker in _NO_REPLACE_MARKERS):
            problems.append(
                f"{entry} からプレースホルダ表記が 1 つも見つかりません"
                "（bootstrap.sh の置換で実プロジェクト名へ化けた疑い。"
                "化けていないなら除外が不要なので台帳から外してください）"
            )

    if problems:
        return CheckResult(name, False, problems)
    return CheckResult(
        name, True, [f"{len(entries)} 件の除外エントリが全てプレースホルダ表記を保持しています"]
    )


def run_all(root: Path, expected_repo: str | None = None) -> list[CheckResult]:
    return [
        check_local_assets(root),
        check_rules_symlinks(root),
        check_modules_yaml_project(root, expected_repo=expected_repo),
        check_placeholder_protection(root),
        check_no_replace_protection(root),
    ]


# --- 自己テスト（変異テスト込み） ---

_LOCAL_MODULES_FIXTURE = """project: "dummy-project"

assets:
  skills:
    - name: dummy-skill
      path: .claude/skills/dummy-skill/SKILL.md
      summary: "test"
      added_by: "test"

  rules:
    - name: dummy-rule
      path: docs/rules/dummy-rule.md
      hot: true
      summary: "test"
      added_by: "test"

  tools:
    - name: dummy_tool.py
      path: tools/dummy_tool.py
      summary: "test"
      added_by: "test"
"""

_MODULES_YAML_FIXTURE = """project:
  name: "dummy-project"
  repo: "{repo}"
  timezone: "Asia/Tokyo"

modules:
  core-principles:
    enabled: true
"""

_BOOTSTRAP_FIXTURE = """#!/usr/bin/env bash
sed -i \\
  -e "s#kai-kou/btc-realtime-chart#${ESC_SLUG}#g" \\
  -e "s#kai-kou/btc-realtime-chart#${ESC_SLUG}#g" \\
  -e "s#BTC Realtime Chart#${ESC_NAME}#g" \\
  -e "s#BTC の価格変動をリアルタイムに確認できるトレーディング参考用 Web アプリ#${ESC_DESC}#g" \\
  "$f"
"""

_MERGE_FIXTURE = """_PLACEHOLDER_VALUES = {
    "{{" + "PROJECT_NAME" + "}}",
    "{{" + "REPO_SLUG" + "}}",
    "__" + "OWNER" + "__" + "/" + "__" + "REPO" + "__",
    "",
}
"""

_FIXTURE_REPO = "kai-kou/dummy-repo"


def _write_good_fixture(root: Path) -> None:
    (root / "docs" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "skills" / "dummy-skill").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)

    (root / "local-modules.yaml").write_text(_LOCAL_MODULES_FIXTURE, encoding="utf-8")
    (root / "modules.yaml").write_text(_MODULES_YAML_FIXTURE.format(repo=_FIXTURE_REPO), encoding="utf-8")
    (root / "scripts" / "bootstrap.sh").write_text(_BOOTSTRAP_FIXTURE, encoding="utf-8")
    # 旧・modules.yaml 専用マージスクリプトは Issue #43 で削除済みなので、健全な baseline には
    # 含めない（ファイル不在が正常な状態）。残存ケースは _case_merge_present_deobfuscated が
    # 個別に作成する。

    (root / ".claude" / "skills" / "dummy-skill" / "SKILL.md").write_text("dummy", encoding="utf-8")
    (root / "docs" / "rules" / "dummy-rule.md").write_text("dummy rule body", encoding="utf-8")
    (root / "tools" / "dummy_tool.py").write_text("# dummy tool", encoding="utf-8")

    (root / ".claude" / "rules" / "dummy-rule.md").symlink_to(Path("..") / ".." / "docs" / "rules" / "dummy-rule.md")

    # 置換除外台帳と、そこに登録された「プレースホルダを説明として持つ」ファイル
    (root / "docs" / "placeholder-spec.md").write_text(
        "配布時は `kai-kou/btc-realtime-chart` へ戻す（説明のための表記であって置換対象ではない）\n",
        encoding="utf-8",
    )
    (root / ".bootstrap-no-replace").write_text(
        "# コメント行\n\ndocs/placeholder-spec.md\n", encoding="utf-8"
    )


def _case_baseline(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    return "ベースライン（健全な状態）→ 全件 PASS", results, all(r.ok for r in results)


def _case_missing_asset(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / "tools" / "dummy_tool.py").unlink()
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("子固有資産"))
    return "資産ファイル欠落（変異）→ 子固有資産チェックが FAIL", results, not target.ok


def _case_broken_symlink(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / "docs" / "rules" / "dummy-rule.md").unlink()  # symlink はそのまま = リンク切れになる
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith(".claude/rules/"))
    return "symlink のリンク先削除（変異）→ symlink 健全性チェックが FAIL", results, not target.ok


def _case_missing_symlink(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / ".claude" / "rules" / "dummy-rule.md").unlink()  # symlink 自体を消す
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith(".claude/rules/"))
    return "hot:true ルールの symlink 欠落（変異）→ symlink 健全性チェックが FAIL", results, not target.ok


def _case_repo_mismatch(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / "modules.yaml").write_text(_MODULES_YAML_FIXTURE.format(repo="someorg/mothership-private"), encoding="utf-8")
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("modules.yaml"))
    return "project.repo が母艦名へ化ける（変異）→ project.* チェックが FAIL", results, not target.ok


def _case_bootstrap_self_replaced(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    text = (root / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    text = text.replace("kai-kou/btc-realtime-chart", _FIXTURE_REPO)  # 自己置換バグのシミュレーション
    (root / "scripts" / "bootstrap.sh").write_text(text, encoding="utf-8")
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("プレースホルダ保護"))
    return "bootstrap.sh のプレースホルダが実プロジェクト名へ化ける（変異）→ プレースホルダ保護チェックが FAIL", results, not target.ok


def _case_merge_absent_ok(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    # 旧・modules.yaml 専用マージスクリプトは削除済みが正常な状態（Issue #43）。
    # ファイルが無いことを理由に FAIL 扱いしないことを確認する。
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("プレースホルダ保護"))
    return "旧マージスクリプトが不在（正常・廃止済み）→ プレースホルダ保護チェックは PASS のまま", results, target.ok


def _case_merge_present_deobfuscated(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    # 未同期のスナップショット等で旧マージスクリプトがまだ残っているケースを模擬しつつ、
    # 難読化を解いて連続したプレースホルダ文字列に戻す変異（今後 bootstrap.sh の sed に拾われる状態）
    deobfuscated = '_PLACEHOLDER_VALUES = {\n    "BTC Realtime Chart",\n    "",\n}\n'
    (root / "scripts" / "merge_modules_yaml.py").write_text(deobfuscated, encoding="utf-8")
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("プレースホルダ保護"))
    return "残存する旧マージスクリプトの難読化が解ける（変異）→ プレースホルダ保護チェックが FAIL", results, not target.ok


def _case_no_replace_target_replaced(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    spec = root / "docs" / "placeholder-spec.md"
    # 台帳に載っているのに置換されてしまった状態を模擬する（登録漏れ・パス変更で実際に起きる）
    spec.write_text(spec.read_text(encoding="utf-8").replace("kai-kou/btc-realtime-chart", _FIXTURE_REPO), encoding="utf-8")
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("置換除外台帳"))
    return "除外台帳のファイルが実プロジェクト名へ化ける（変異）→ 置換除外台帳チェックが FAIL", results, not target.ok


def _case_no_replace_entry_missing(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / "docs" / "placeholder-spec.md").unlink()  # 台帳が腐った状態（移動・削除）
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("置換除外台帳"))
    return "除外台帳のエントリが存在しない（変異）→ 置換除外台帳チェックが FAIL", results, not target.ok


def _case_no_replace_solo_marker_ok(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    # bootstrap.sh の 2 パス目が置換する単独形だけを説明として持つファイル（結合形を含まない）。
    # `_NO_REPLACE_MARKERS` から単独形を落とすとこのケースが誤 NG になる（Layer 1 指摘）。
    solo = root / "docs" / "solo-placeholder-spec.md"
    solo.write_text("owner 名は kai-kou に、repo 名は btc-realtime-chart に置換される\n", encoding="utf-8")
    ledger = root / ".bootstrap-no-replace"
    ledger.write_text(ledger.read_text(encoding="utf-8") + "docs/solo-placeholder-spec.md\n", encoding="utf-8")
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("置換除外台帳"))
    return "単独形 kai-kou / btc-realtime-chart だけを持つエントリ（正常）→ 置換除外台帳チェックは PASS のまま", results, target.ok


def _case_no_replace_ledger_absent(root: Path) -> tuple[str, list[CheckResult], bool]:
    _write_good_fixture(root)
    (root / ".bootstrap-no-replace").unlink()  # 台帳を持たない下流は正常
    results = run_all(root, expected_repo=_FIXTURE_REPO)
    target = next(r for r in results if r.name.startswith("置換除外台帳"))
    return "除外台帳が不在（正常・台帳を持たない下流）→ 置換除外台帳チェックは PASS のまま", results, target.ok


def _run_self_test() -> int:
    cases = [
        _case_baseline,
        _case_missing_asset,
        _case_broken_symlink,
        _case_missing_symlink,
        _case_repo_mismatch,
        _case_bootstrap_self_replaced,
        _case_merge_absent_ok,
        _case_merge_present_deobfuscated,
        _case_no_replace_target_replaced,
        _case_no_replace_entry_missing,
        _case_no_replace_solo_marker_ok,
        _case_no_replace_ledger_absent,
    ]
    failed = 0
    for case_fn in cases:
        with tempfile.TemporaryDirectory() as tmp_str:
            desc, results, passed = case_fn(Path(tmp_str))
        print(f"{'PASS' if passed else 'FAIL'}: {desc}")
        if not passed:
            failed += 1
            for r in results:
                print(f"      [{'OK' if r.ok else 'NG'}] {r.name}: {'; '.join(r.details)}")
    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS（変異ケースが意図どおり各チェックの FAIL を検知したことを確認済み）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="検査対象リポジトリのルート（既定: カレントディレクトリ）")
    parser.add_argument("--self-test", action="store_true", help="変異テスト込みの自己テストを実行する")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    root = Path(args.root).resolve()
    results = run_all(root)
    ok = all(r.ok for r in results)
    for r in results:
        print(f"[{'OK' if r.ok else 'NG'}] {r.name}")
        for detail in r.details:
            print(f"    - {detail}")
    if ok:
        print("\n[OK] apply-base 同期後の自己検査は全件 PASS しました")
        sys.exit(0)
    print("\n[NG] apply-base 同期後の自己検査で異常を検出しました（上記を確認してください）", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
