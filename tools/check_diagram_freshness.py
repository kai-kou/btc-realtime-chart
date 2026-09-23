#!/usr/bin/env python3
"""check_diagram_freshness.py — 横断図の鮮度ゲート（`D-25` M-1 段階・`Q-5` → `D-40`）。

`docs/02_requirements/document-catalog.md` §5.2 が定義する **横断図**（文書相関図・ADR マップ・
アーキテクチャフロー等、複数の正本にまたがり co-location が成立しない図）は、`git diff` では
変更漏れを検知できない（図が描く複数の正本のうちどれか 1 本だけを直しても、図自身のファイルには
差分が出ないため）。本ツールは `D-25` の M-1 段階（git コミット時刻比較）を実装し、登録済みの
横断図が参照元ソースより古いままになっていないかを機械検知する。

M-2 段階（内容ハッシュ）はグラレコ風画像専用の `tools/generate_infographic.py --check` が別スコープ
として持つ。両者を 1 本のツールへ統合しない設計判断（対象ドメインが「単一リポジトリ内の 1 図:N
ソース」と「複数リポジトリ間のルール差分」で異なる）は `docs/02_requirements/open-questions.md`
`D-40` を参照。

登録は `config/diagram_targets.yaml`（既定空・明示登録した組のみ検査。`config/infographic.yaml` と
同じ opt-in 方式）に持つ。**未登録の横断図を検出することはしない**（fail-closed の対象は
「登録済みなのにソースより古い」ケースのみ）。

使い方:
  python3 tools/check_diagram_freshness.py                  # 登録済み全ターゲットを検査
  python3 tools/check_diagram_freshness.py --target <path>  # 1 件だけ検査（diagram のパスで指定）
  python3 tools/check_diagram_freshness.py --json           # 機械可読出力
  python3 tools/check_diagram_freshness.py --self-test      # 実 git 呼び出しなしの単体テスト
違反（登録済みだがソースより古い、または diagram/source が存在しない）があれば exit 1。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 環境依存
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "diagram_targets.yaml"


@dataclass
class DiagramTarget:
    diagram: str
    sources: list[str] = field(default_factory=list)


@dataclass
class FreshnessResult:
    diagram: str
    stale: bool
    stale_sources: list[str] = field(default_factory=list)
    error: str | None = None


def load_targets(config_path: Path = DEFAULT_CONFIG_PATH) -> list[DiagramTarget]:
    """config/diagram_targets.yaml を読み、DiagramTarget のリストを返す（純粋・I/O は読み込みのみ）。"""
    if not config_path.exists():
        return []
    if yaml is None:
        raise RuntimeError("PyYAML が未インストールです（requirements.txt を確認してください）")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw_targets = data.get("targets") or []
    out: list[DiagramTarget] = []
    for t in raw_targets:
        diagram = t.get("diagram") if isinstance(t, dict) else None
        sources = t.get("sources") if isinstance(t, dict) else None
        if not diagram or not sources:
            raise ValueError(f"config/diagram_targets.yaml の不正なエントリ: {t!r}（diagram と sources は必須）")
        out.append(DiagramTarget(diagram=diagram, sources=list(sources)))
    return out


def _git_commit_epoch(relative_path: str) -> int | None:
    """relative_path の最終コミット時刻（epoch 秒）。git 管理外・未コミットなら None（I/O）。"""
    abs_path = REPO_ROOT / relative_path
    if not abs_path.exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct", "--", relative_path],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _stale_sources(diagram_ts: int, source_epochs: dict[str, int | None]) -> list[str]:
    """diagram_ts より新しい（または存在しない）source を返す（純粋関数・--self-test の対象）。

    存在しない / git 管理外の source（epoch=None）も stale 扱いにする — 図が参照しているのに
    ソース自体が消えている状態を「鮮度は問題なし」と誤判定しないため。
    """
    stale = []
    for src, ts in source_epochs.items():
        if ts is None or ts > diagram_ts:
            stale.append(src)
    return stale


def check_target(target: DiagramTarget) -> FreshnessResult:
    diagram_ts = _git_commit_epoch(target.diagram)
    if diagram_ts is None:
        return FreshnessResult(target.diagram, stale=True,
                                error=f"{target.diagram} が存在しないか git 管理外です")
    source_epochs = {src: _git_commit_epoch(src) for src in target.sources}
    stale = _stale_sources(diagram_ts, source_epochs)
    return FreshnessResult(target.diagram, stale=bool(stale), stale_sources=stale)


def run(target_filter: str | None = None, config_path: Path = DEFAULT_CONFIG_PATH) -> list[FreshnessResult]:
    targets = load_targets(config_path)
    if target_filter:
        targets = [t for t in targets if t.diagram == target_filter]
        if not targets:
            raise ValueError(
                f"{target_filter} は {config_path} に登録されていません"
                "（--target は登録済みエントリの diagram と完全一致させる）"
            )
    return [check_target(t) for t in targets]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="1 件だけ検査する横断図ファイルのパス（config 登録済みのキーと一致させる）")
    parser.add_argument("--json", action="store_true", help="機械可読出力")
    parser.add_argument("--self-test", action="store_true", help="実 git 呼び出しなしの単体テスト")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    try:
        results = run(target_filter=args.target)
    except (RuntimeError, ValueError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))

    stale = [r for r in results if r.stale]
    if stale:
        for r in stale:
            if r.error:
                print(f"❌ {r.diagram}: {r.error}", file=sys.stderr)
            else:
                shown = "、".join(r.stale_sources)
                print(
                    f"❌ {r.diagram}: ソースより新しい可能性がある参照元があります（{shown}）。"
                    "図の内容を確認し、更新したら図のファイルも再コミットしてください。",
                    file=sys.stderr,
                )
        return 1

    scope = f"{len(results)} 件（--target {args.target}）" if args.target else f"{len(results)} 件"
    print(f"✅ 横断図の鮮度ゲート: {scope} 検査、違反なし。")
    return 0


def _self_test() -> int:
    """実 git 呼び出し・実ファイルなしの単体テスト（登録ロジックとゲート判定の純粋関数のみを検証）。"""
    import tempfile

    failures: list[str] = []

    # ケース 1: 空 config は PASS 相当（targets 0 件・opt-in の未登録は fail-closed の対象外）
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "empty.yaml"
        cfg.write_text("targets: []\n", encoding="utf-8")
        if load_targets(cfg) != []:
            failures.append("空 config（targets: []）で load_targets が空リストを返さない")

    # ケース 2: config 自体が無い（未作成）ときも空リスト
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "does-not-exist.yaml"
        if load_targets(missing) != []:
            failures.append("config 不在時に load_targets が空リストを返さない")

    # ケース 3: diagram / sources 欠落エントリは ValueError（fail-closed）
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "bad.yaml"
        cfg.write_text("targets:\n  - diagram: docs/foo.md\n", encoding="utf-8")
        try:
            load_targets(cfg)
            failures.append("sources 欠落エントリが ValueError にならない（fail-closed 違反）")
        except ValueError:
            pass

    # ケース 4: 正常な 2 件登録を読み込める
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "ok.yaml"
        cfg.write_text(
            "targets:\n"
            "  - diagram: docs/a.md\n"
            "    sources: [docs/b.md, docs/c.md]\n"
            "  - diagram: docs/d.md\n"
            "    sources: [docs/e.md]\n",
            encoding="utf-8",
        )
        targets = load_targets(cfg)
        if len(targets) != 2 or targets[0].sources != ["docs/b.md", "docs/c.md"]:
            failures.append(f"正常 config の読み込み結果が期待と不一致: {targets}")

    # ケース 5: --target が登録済みキーに 1 件も一致しないとき run() は ValueError（fail-closed）。
    # 一致しない --target を「0 件検査・違反なし」で黙って通すと、指定ミス（typo・未登録）に
    # 気づけないまま「検査した」と誤認させる fail-open になるため、run() の時点で例外にする。
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "one.yaml"
        cfg.write_text("targets:\n  - diagram: docs/a.md\n    sources: [docs/b.md]\n", encoding="utf-8")
        try:
            run(target_filter="docs/does-not-exist.md", config_path=cfg)
            failures.append("--target が未登録キーに一致しないのに run() が例外を出さない（fail-open）")
        except ValueError:
            pass

    # ケース 6〜9: _stale_sources の純粋な判定ロジック（git I/O なし）
    if _stale_sources(diagram_ts=100, source_epochs={"a": 50, "b": 90}) != []:
        failures.append("全ソースが図より古いのに stale 判定された")
    if sorted(_stale_sources(diagram_ts=100, source_epochs={"a": 150, "b": 50})) != ["a"]:
        failures.append("図より新しいソースだけを stale として拾えていない")
    if _stale_sources(diagram_ts=100, source_epochs={"a": None}) != ["a"]:
        failures.append("存在しない（None）ソースが stale 扱いになっていない")
    if _stale_sources(diagram_ts=100, source_epochs={"a": 100}) != []:
        failures.append("同時刻（同一コミット）を誤って stale 判定した")

    if failures:
        print("❌ --self-test 失敗:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✅ --self-test 成功（9 ケース）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
