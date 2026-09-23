#!/usr/bin/env python3
"""native_fallback.py — Web 未提供機能の native-first 判定と claude -p 安全実行の共通機構。

SSOT ルール: docs/rules/native-fallback-rules.md（Issue #198）

サブコマンド:
  probe [<capability-id>] [--all] [--json]
      レジストリ（tools/native_capabilities.json + 任意の .local.json オーバーレイ）に基づき
      ネイティブ機能の可用性を機械判定する。
      exit 0 = native 利用可 / 3 = セッション内プローブが必要（probes[].detail の指示に従う）/
      4 = native 不可（フォールバックへ）/ 2 = 引数・レジストリ異常
  routes [<capability-id>] [--json]
      起動経路 ladder（Skill → Workflow → セッションツール → claude -p → 最終手段）を
      試す順に表示する。ネイティブ機能は「どのツールから呼ぶか」がバージョンで移動するため
      （#370: /deep-research が v2.1.218 で Skill 経由 → Workflow 経由へ）、単一経路を
      前提にせず本コマンドで現行の試行順と降格シグネチャを確認してから実行する。
  headless [オプション]
      claude -p を安全に実行する共通ラッパー（cwd=一時ディレクトリ隔離・CLAUDECODE 除去・
      サブスク認証・タイムアウト・退避ログ 1 行の強制出力）。
  --self-test
      レジストリ検証 + probe --all + headless --dry-run + 既存ランナー等価性チェック
      （Issue #200・run_discussion_review.py 等が headless() の安全対策からドリフトして
      いないかの静的検査）の煙テスト（claude 実起動なし）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "tools" / "native_capabilities.json"
LOCAL_OVERLAY_PATH = REPO_ROOT / "tools" / "native_capabilities.local.json"

VALID_CATEGORIES = {"native-default", "gap-fallback", "isolation-by-design", "substrate"}
MECHANICAL_PROBES = {"cli", "cli-flag", "project-skill", "file", "env"}
SESSION_PROBES = {"session-tool", "builtin-command"}
# probe type ごとの必須キー（欠落を validate 時に検出し run_probe の KeyError を防ぐ）
REQUIRED_PROBE_KEY = {"cli-flag": "flag", "project-skill": "skill", "file": "path",
                      "env": "name", "session-tool": "tool", "builtin-command": "command"}

# 起動経路 ladder（native.routes）。ネイティブ機能は「どのツールから呼ぶか」がバージョンで
# 移動するため（例: /deep-research が v2.1.218 で Skill 経由 → Workflow 経由へ）、単一の
# how 文字列に固定せず順序付きの経路として台帳化する（#372）。
VALID_ROUTE_KINDS = {"skill", "workflow", "session-tool", "builtin-command", "cli"}
# preferred=既定で最初に試す / available=使えるが既定でない / unavailable=既知の不可（記録として残す）
# / unknown=未検証（試して確かめる）
VALID_ROUTE_STATUS = {"preferred", "available", "unavailable", "unknown"}
REQUIRED_ROUTE_KEYS = ("kind", "how")

# 経路降格シグネチャ: 実行時にこの文字列が返ったら「その経路は死んでいる」と判定し次段へ進む。
# 台帳の route.detail に個別事情を書き、ここには経路種別に共通する汎用シグネチャだけを持つ。
ROUTE_DEMOTION_SIGNATURES = [
    {"pattern": "disable-model-invocation",
     "meaning": "その機能がモデル起動禁止に変更された（bundled 側の仕様変更）",
     "next": "同名の workflow 経路 → セッションツール直呼び → claude -p の順に降格する"},
    {"pattern": "cannot be used with Skill tool",
     "meaning": "Skill ツールからは起動できない（skill 経路が閉じた）",
     "next": "workflow 経路（Workflow({name: ...})）を試す"},
    {"pattern": "Review dynamic workflow before running",
     "meaning": "Workflow 実行が承認ゲートで止められた（非対話セッションでは承認できない）",
     "next": "claude -p 経路へ降格し --allowedTools に Workflow を含める"},
    {"pattern": "No such command",
     "meaning": "スラッシュコマンド／ワークフロー名が現行 CLI に存在しない（改名・撤去）",
     "next": "公式 changelog を claude-code-spec-sync レーンで確認し、台帳の routes を更新する"},
    {"pattern": "not found",
     "meaning": "経路の名前解決に失敗した（改名・未提供の可能性）",
     "next": "次段の経路を試し、恒常的なら台帳の status を unavailable にして記録する"},
]

# claude -p の子セッションをネスト起動するために除去する env（対話端末ガード用のため
# プログラム的サブプロセスでは安全に外せる。skill-creator scripts と同一パターン）
_NEST_GUARD_ENV = ("CLAUDECODE",)
# サブスク認証を強制する場合に除去する API キー系 env（run_discussion_review.py と同一）
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

EXIT_NATIVE = 0
EXIT_USAGE = 2
EXIT_SESSION_PROBE = 3
EXIT_FALLBACK = 4
EXIT_TIMEOUT = 124


# ---------------------------------------------------------------- registry

def _merge_caps(caps: dict, entries: list, source_name: str) -> None:
    """capability エントリ群を id 単位で caps へマージする（id 欠落は明示エラー）。"""
    for cap in entries:
        cid = cap.get("id")
        if not cid:
            print(f"レジストリ異常: {source_name} に id のない capability がある",
                  file=sys.stderr)
            sys.exit(EXIT_USAGE)
        caps[cid] = cap


def load_registry() -> dict:
    """ベースレジストリに .local.json オーバーレイ（id 単位で追加/上書き）をマージして返す。"""
    with REGISTRY_PATH.open(encoding="utf-8") as f:
        registry = json.load(f)
    caps: dict = {}
    _merge_caps(caps, registry.get("capabilities", []), REGISTRY_PATH.name)
    if LOCAL_OVERLAY_PATH.exists():
        with LOCAL_OVERLAY_PATH.open(encoding="utf-8") as f:
            overlay = json.load(f)
        # 派生リポ側のエントリが優先
        _merge_caps(caps, overlay.get("capabilities", []), LOCAL_OVERLAY_PATH.name)
    registry["capabilities"] = list(caps.values())
    return registry


def validate_registry(registry: dict) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for cap in registry.get("capabilities", []):
        cid = cap.get("id")
        if not cid:
            errors.append("id のない capability がある")
            continue
        if cid in seen:
            errors.append(f"id 重複: {cid}")
        seen.add(cid)
        if cap.get("category") not in VALID_CATEGORIES:
            errors.append(f"{cid}: 不正 category {cap.get('category')!r}")
        native = cap.get("native")
        if native is not None:
            routes = native.get("routes", [])
            if routes and not any(r.get("status") == "preferred" for r in routes):
                errors.append(f"{cid}: routes に status=preferred の経路がない"
                              "（最初に試す経路を 1 つ決めること）")
            for route in routes:
                missing = [k for k in REQUIRED_ROUTE_KEYS if k not in route]
                if missing:
                    errors.append(f"{cid}: route に必須キーがない: {', '.join(missing)}")
                if route.get("kind") not in VALID_ROUTE_KINDS:
                    errors.append(f"{cid}: 不正 route kind {route.get('kind')!r}")
                if route.get("status", "unknown") not in VALID_ROUTE_STATUS:
                    errors.append(f"{cid}: 不正 route status {route.get('status')!r}")
            for probe in native.get("probes", []):
                ptype = probe.get("type")
                if ptype not in MECHANICAL_PROBES | SESSION_PROBES:
                    errors.append(f"{cid}: 不正 probe type {ptype!r}")
                elif ptype in REQUIRED_PROBE_KEY and REQUIRED_PROBE_KEY[ptype] not in probe:
                    errors.append(f"{cid}: {ptype} プローブに必須キー "
                                  f"{REQUIRED_PROBE_KEY[ptype]!r} がない")
        elif cap.get("category") not in ("isolation-by-design",):
            errors.append(f"{cid}: native なしは isolation-by-design のみ許可")
    return errors


# ---------------------------------------------------------------- probe

def _claude_help() -> str:
    try:
        proc = subprocess.run(["claude", "--help"], capture_output=True,
                              text=True, timeout=30)
        return proc.stdout + proc.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def run_probe(probe: dict, help_cache: dict) -> dict:
    """1 プローブを評価。result: pass / fail / session"""
    ptype = probe["type"]
    if ptype == "cli":
        ok = shutil.which("claude") is not None
        return {"probe": probe, "result": "pass" if ok else "fail",
                "detail": "claude CLI on PATH" if ok else "claude CLI が PATH にない"}
    if ptype == "cli-flag":
        if "help" not in help_cache:
            help_cache["help"] = _claude_help()
        if not help_cache["help"]:
            # 実行失敗と「フラグが無い」を区別する（誤診断防止）。判定不能は保守的に fail
            return {"probe": probe, "result": "fail",
                    "detail": "claude --help の実行に失敗（フラグ有無は判定不能）"}
        # 語境界必須（部分文字列一致だと "-p" が --permission-mode 等へ false positive する）
        ok = re.search(rf"(?<![\w-]){re.escape(probe['flag'])}(?![\w-])",
                       help_cache["help"]) is not None
        return {"probe": probe, "result": "pass" if ok else "fail",
                "detail": f"claude --help に {probe['flag']} が{'ある' if ok else 'ない'}"}
    if ptype == "project-skill":
        path = REPO_ROOT / ".claude" / "skills" / probe["skill"] / "SKILL.md"
        ok = path.exists()
        return {"probe": probe, "result": "pass" if ok else "fail",
                "detail": str(path.relative_to(REPO_ROOT))}
    if ptype == "file":
        resolved = (REPO_ROOT / probe["path"]).resolve()
        if not resolved.is_relative_to(REPO_ROOT):
            return {"probe": probe, "result": "fail",
                    "detail": f"リポジトリ外パスは検査不可: {probe['path']}"}
        ok = resolved.exists()
        return {"probe": probe, "result": "pass" if ok else "fail",
                "detail": probe["path"]}
    if ptype == "env":
        val = os.environ.get(probe["name"])
        ok = (val == probe["equals"]) if "equals" in probe else bool(val)
        # 値そのものは出さない（probe 出力は Issue/PR コメントへ転記されうるため・秘匿事故防止）
        return {"probe": probe, "result": "pass" if ok else "fail",
                "detail": f"{probe['name']} は{'設定済み' if ok else '未設定/不一致'}"}
    if ptype == "session-tool":
        return {"probe": probe, "result": "session",
                "detail": f"セッション内で確認: ToolSearch \"select:{probe['tool']}\" でロードできれば native 可"}
    if ptype == "builtin-command":
        return {"probe": probe, "result": "session",
                "detail": f"セッション内で確認: {probe['command']} をまず実試行し、失敗時のみフォールバック"}
    return {"probe": probe, "result": "fail", "detail": f"未知の probe type: {ptype}"}


def probe_capability(cap: dict, help_cache: dict) -> dict:
    """capability 全体の判定。verdict: native / session / fallback / by-design"""
    native = cap.get("native")
    if native is None:
        return {"id": cap["id"], "category": cap["category"], "verdict": "by-design",
                "native": None, "routes": [], "probes": [], "fallback": cap.get("fallback"),
                "notes": cap.get("notes", "")}
    results = [run_probe(p, help_cache) for p in native.get("probes", [])]
    if any(r["result"] == "fail" for r in results):
        verdict = "fallback"
    elif any(r["result"] == "session" for r in results):
        verdict = "session"
    else:
        verdict = "native"
    return {"id": cap["id"], "category": cap["category"], "verdict": verdict,
            "native": native.get("how"), "routes": native.get("routes", []),
            "probes": results, "fallback": cap.get("fallback"),
            "notes": cap.get("notes", "")}


def cmd_probe(args: argparse.Namespace) -> int:
    registry = load_registry()
    errors = validate_registry(registry)
    if errors:
        print("レジストリ異常:\n  " + "\n  ".join(errors), file=sys.stderr)
        return EXIT_USAGE
    caps = registry["capabilities"]
    help_cache: dict = {}
    if args.all or not args.capability:
        reports = [probe_capability(c, help_cache) for c in caps]
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, indent=2))
        else:
            for r in reports:
                print(f"[{r['verdict']:>9}] {r['id']} ({r['category']})")
                for pr in r["probes"]:
                    print(f"    - {pr['probe']['type']}: {pr['result']} ({pr['detail']})")
        return EXIT_NATIVE
    target = next((c for c in caps if c["id"] == args.capability), None)
    if target is None:
        known = ", ".join(c["id"] for c in caps)
        print(f"未知の capability: {args.capability}（既知: {known}）", file=sys.stderr)
        return EXIT_USAGE
    report = probe_capability(target, help_cache)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"native": EXIT_NATIVE, "session": EXIT_SESSION_PROBE,
            "fallback": EXIT_FALLBACK, "by-design": EXIT_NATIVE}[report["verdict"]]


# ---------------------------------------------------------------- routes

_STATUS_ORDER = {"preferred": 0, "available": 1, "unknown": 2, "unavailable": 3}


def route_ladder(cap: dict) -> list[dict]:
    """capability の起動経路 ladder（試す順）を組み立てる。

    native.routes（status 順）→ claude -p → 最終手段、の段階降格を 1 本のリストで返す。
    status=unavailable の経路も `skip: True` で残す（「なぜその経路を使わないか」の記録が
    次にバージョンが動いたときの手掛かりになるため・#372）。
    """
    ladder: list[dict] = []
    native = cap.get("native") or {}
    routes = sorted(native.get("routes", []),
                    key=lambda r: _STATUS_ORDER.get(r.get("status", "unknown"), 2))
    for route in routes:
        status = route.get("status", "unknown")
        ladder.append({"stage": "native", "kind": route["kind"], "name": route.get("name"),
                       "how": route["how"], "status": status,
                       "skip": status == "unavailable", "detail": route.get("detail", "")})
    if not routes and native.get("how"):
        # routes 未記載の capability は既存の how を単一経路として扱う（後方互換）
        ladder.append({"stage": "native", "kind": "unspecified", "name": cap["id"],
                       "how": native["how"], "status": "unknown", "skip": False,
                       "detail": "routes 未記載（native.how からの後方互換フォールバック）"})
    fallback = cap.get("fallback") or {}
    if fallback.get("how"):
        ladder.append({"stage": "claude-p", "kind": fallback.get("kind", "claude-p"),
                       "name": None, "how": fallback["how"], "status": "available",
                       "skip": False, "detail": "ネイティブ経路が全滅したときの隔離サブプロセス経路"})
    if fallback.get("final"):
        ladder.append({"stage": "final", "kind": "skill-specific", "name": None,
                       "how": fallback["final"], "status": "available", "skip": False,
                       "detail": "claude -p も失敗したときのスキル固有の最終手段"})
    return ladder


def cmd_routes(args: argparse.Namespace) -> int:
    registry = load_registry()
    errors = validate_registry(registry)
    if errors:
        print("レジストリ異常:\n  " + "\n  ".join(errors), file=sys.stderr)
        return EXIT_USAGE
    caps = registry["capabilities"]
    if args.capability:
        target = next((c for c in caps if c["id"] == args.capability), None)
        if target is None:
            known = ", ".join(c["id"] for c in caps)
            print(f"未知の capability: {args.capability}（既知: {known}）", file=sys.stderr)
            return EXIT_USAGE
        targets = [target]
    else:
        targets = caps
    reports = [{"id": c["id"], "category": c["category"], "ladder": route_ladder(c)}
               for c in targets]
    if args.json:
        print(json.dumps({"ladders": reports, "demotion_signatures": ROUTE_DEMOTION_SIGNATURES},
                         ensure_ascii=False, indent=2))
        return EXIT_NATIVE
    for rep in reports:
        print(f"# {rep['id']} ({rep['category']})")
        step = 0
        for entry in rep["ladder"]:
            if entry["skip"]:
                print(f"    -  [skip] {entry['kind']}: {entry['how']}")
                print(f"              理由: {entry['detail']}")
                continue
            step += 1
            print(f"    {step}. [{entry['stage']}] {entry['kind']}: {entry['how']}")
            if entry["detail"]:
                print(f"       {entry['detail']}")
        if not rep["ladder"]:
            print("    （経路未登録: native も fallback も記載がない）")
        print()
    print("降格シグネチャ（実行結果にこの文字列が出たら次段へ）:")
    for sig in ROUTE_DEMOTION_SIGNATURES:
        print(f"  - {sig['pattern']!r}: {sig['meaning']} → {sig['next']}")
    return EXIT_NATIVE


# ---------------------------------------------------------------- headless

def build_headless_cmd(args: argparse.Namespace) -> list[str]:
    cmd = ["claude", "-p", "--output-format", args.output_format]
    if args.model:
        cmd += ["--model", args.model]
    if args.fallback_model:
        cmd += ["--fallback-model", args.fallback_model]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    if args.max_budget_usd is not None and not args.use_subscription:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]
    if args.extra:
        cmd += args.extra
    return cmd


def cmd_headless(args: argparse.Namespace) -> int:
    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            print(f"プロンプトファイルが見つからない: {args.prompt_file}", file=sys.stderr)
            return EXIT_USAGE
        prompt = prompt_path.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        print("プロンプト未指定（--prompt / --prompt-file / stdin のいずれかが必要）",
              file=sys.stderr)
        return EXIT_USAGE
    if not prompt.strip():
        print("プロンプトが空", file=sys.stderr)
        return EXIT_USAGE

    if args.max_budget_usd is not None and args.use_subscription:
        print("[native-fallback] 警告: --max-budget-usd はサブスク経路では無視される"
              "（有効にするには --no-subscription を併用）", file=sys.stderr)

    # 退避ログ 1 行（サイレントフォールバック禁止の機械支援）: 常に stderr へ出す
    print(f"[native-fallback] claude -p 実行: capability={args.capability or '-'} "
          f"reason={args.reason or '-'} model={args.model or '(default)'} "
          f"timeout={args.timeout}s subscription={'on' if args.use_subscription else 'off'}",
          file=sys.stderr)

    cmd = build_headless_cmd(args)
    if args.dry_run:
        print(json.dumps({"cmd": cmd, "cwd": "<tempdir>", "stdin": f"<prompt {len(prompt)} chars>",
                          "env_removed": list(_NEST_GUARD_ENV) + (
                              list(_API_KEY_ENV_VARS) if args.use_subscription else [])},
                         ensure_ascii=False, indent=2))
        return EXIT_NATIVE

    removed = set(_NEST_GUARD_ENV) | (set(_API_KEY_ENV_VARS) if args.use_subscription else set())
    child_env = {k: v for k, v in os.environ.items() if k not in removed}
    # ⚠️ cwd は必ず一時ディレクトリ（リポジトリ cwd だと子セッションの SessionStart フックが
    # 未コミット作業を git clean で破壊する・L-100）。リポジトリへは絶対パスで読み書きさせる。
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(cmd, cwd=tmp, input=prompt, capture_output=True,
                                  env=child_env, text=True, encoding="utf-8",
                                  errors="replace", timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"[native-fallback] タイムアウト（{args.timeout}s）", file=sys.stderr)
            return EXIT_TIMEOUT
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"[native-fallback] サブプロセス失敗: {exc}", file=sys.stderr)
            return 1
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode != 0:
        print(f"[native-fallback] claude -p exit={proc.returncode} "
              f"stderr(tail): {(proc.stderr or '')[-1000:]}", file=sys.stderr)
    elif args.output_format == "json":
        try:
            json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            print("[native-fallback] 警告: stdout が JSON として解析できない", file=sys.stderr)
    return proc.returncode


# ---------------------------------------------------------------- equivalence

# 既存の専用 claude -p ランナー（PR #199 で「準拠実装」として書き換えを見送ったもの・Issue #200）
# が headless() の安全対策（cwd 隔離・timeout・CLAUDECODE 除去）から将来ドリフトしていないかを
# 静的検査する。"checks" は各ファイルの **現状** で確認できているパターンのみを列挙する
# （既知の未実装は列挙しないことで許容し、現状からの後退のみを検出する・Option B）。
_CWD_ISOLATION_RE = re.compile(r"tempfile\.(TemporaryDirectory|mkdtemp)")
_CLAUDECODE_REMOVAL_RE = re.compile(r'!=\s*"CLAUDECODE"|not in\s*\{?\(?"CLAUDECODE"')
_TIMEOUT_RE = re.compile(r"timeout\s*=")

_EQUIVALENCE_PATTERNS = {
    "cwd_isolation": _CWD_ISOLATION_RE,
    "claudecode_removal": _CLAUDECODE_REMOVAL_RE,
    "timeout": _TIMEOUT_RE,
}

EQUIVALENCE_TARGETS = [
    {
        # cwd 隔離・timeout は実装済み。CLAUDECODE 除去は未実装（API キー系のみ除去）だが、
        # ネスト起動は実運用（discussion-review 5 件超）で問題なく動いており未実装を許容する。
        "path": "tools/run_discussion_review.py",
        "checks": ("cwd_isolation", "timeout"),
    },
    {
        "path": "tools/run_deep_research_workflow.py",
        "checks": ("cwd_isolation", "timeout"),
    },
    {
        # 上流 Anthropic 公式スキルのコピー（skill-creator）。cwd 隔離は未実装
        # （書き換えは上流追従性とのトレードオフのため見送り・Issue #169）。
        "path": ".claude/skills/skill-creator/scripts/improve_description.py",
        "checks": ("claudecode_removal", "timeout"),
    },
    {
        "path": ".claude/skills/skill-creator/scripts/run_eval.py",
        "checks": ("claudecode_removal", "timeout"),
    },
]


def check_runner_equivalence() -> list[str]:
    """既存ランナーの安全対策ドリフトを検出する。戻り値: 失敗メッセージ一覧（空なら全 OK）。"""
    failures: list[str] = []
    for target in EQUIVALENCE_TARGETS:
        path = REPO_ROOT / target["path"]
        if not path.is_file():
            failures.append(f"{target['path']}: ファイルが見つからない"
                            "（削除済みなら EQUIVALENCE_TARGETS からも除去すること）")
            continue
        src = path.read_text(encoding="utf-8")
        for check in target["checks"]:
            if not _EQUIVALENCE_PATTERNS[check].search(src):
                failures.append(f"{target['path']}: {check} 相当の実装が見つからない"
                                "（native_fallback.py headless の安全対策からドリフトした可能性）")
    return failures


# ---------------------------------------------------------------- self-test

def cmd_self_test() -> int:
    failures: list[str] = []
    try:
        registry = load_registry()
        errors = validate_registry(registry)
        if errors:
            failures.append("registry: " + "; ".join(errors))
        else:
            print(f"✓ レジストリ検証 OK（{len(registry['capabilities'])} capability）")
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        failures.append(f"registry load: {exc}")
        registry = {"capabilities": []}

    help_cache: dict = {}
    try:
        for cap in registry["capabilities"]:
            report = probe_capability(cap, help_cache)
            if report["verdict"] not in ("native", "session", "fallback", "by-design"):
                failures.append(f"probe {cap['id']}: 不正 verdict {report['verdict']}")
        print(f"✓ probe --all 実行 OK")
    except Exception as exc:  # noqa: BLE001 - 煙テストは全例外を失敗として報告
        failures.append(f"probe: {exc}")

    try:
        registry = load_registry()
        for cap in registry["capabilities"]:
            ladder = route_ladder(cap)
            usable = [e for e in ladder if not e["skip"]]
            if not usable:
                failures.append(f"routes {cap['id']}: 実行可能な経路が 1 つもない")
                continue
            # native-first の機械検査: native を持つ capability で claude -p が先頭に来たら
            # ladder の順序が壊れている（isolation-by-design は native なしなので対象外）
            if cap.get("native") is not None and usable[0]["stage"] != "native":
                failures.append(f"routes {cap['id']}: native があるのに先頭段が "
                                f"{usable[0]['stage']}（native-first 違反）")
        print(f"✓ routes ladder 検証 OK（{len(registry['capabilities'])} capability）")
    except Exception as exc:  # noqa: BLE001 - 煙テストは全例外を失敗として報告
        failures.append(f"routes: {exc}")

    try:
        eq_failures = check_runner_equivalence()
        if eq_failures:
            failures.extend(eq_failures)
        else:
            print(f"✓ 既存ランナー等価性チェック OK（{len(EQUIVALENCE_TARGETS)} ファイル）")
    except Exception as exc:  # noqa: BLE001 - 煙テストは全例外を失敗として報告
        failures.append(f"equivalence check: {exc}")

    try:
        ns = argparse.Namespace(prompt="self-test", prompt_file=None, model=None,
                                fallback_model=None, allowed_tools=None,
                                max_budget_usd=None, use_subscription=True,
                                output_format="json", timeout=60, capability="self-test",
                                reason="self-test", dry_run=True, extra=[])
        rc = cmd_headless(ns)
        if rc != 0:
            failures.append(f"headless --dry-run: exit={rc}")
        else:
            print("✓ headless --dry-run OK")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"headless dry-run: {exc}")

    if failures:
        print("SELF-TEST FAIL:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print("SELF-TEST PASS")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="煙テスト（claude 実起動なし）")
    sub = parser.add_subparsers(dest="command")

    p_probe = sub.add_parser("probe", help="capability の可用性を機械判定")
    p_probe.add_argument("capability", nargs="?", help="capability id（省略時は --all 相当）")
    p_probe.add_argument("--all", action="store_true", help="全 capability を一覧判定")
    p_probe.add_argument("--json", action="store_true", help="一覧を JSON で出力")

    p_routes = sub.add_parser("routes",
                              help="起動経路 ladder（試す順）と降格シグネチャを表示")
    p_routes.add_argument("capability", nargs="?", help="capability id（省略時は全件）")
    p_routes.add_argument("--json", action="store_true", help="JSON で出力")

    default_sub = os.getenv("NATIVE_FALLBACK_USE_SUBSCRIPTION", "1") != "0"
    p_head = sub.add_parser("headless", help="claude -p 安全実行ラッパー")
    p_head.add_argument("--prompt", help="プロンプト本文（短文向け）")
    p_head.add_argument("--prompt-file", help="プロンプトファイル（長文は必ずこちらか stdin）")
    p_head.add_argument("--model", help="子セッションのモデル")
    p_head.add_argument("--fallback-model", help="claude CLI の --fallback-model")
    p_head.add_argument("--allowed-tools", help="claude CLI の --allowedTools")
    p_head.add_argument("--max-budget-usd", type=float,
                        help="API 従量経路のみ有効（サブスク経路では無視）")
    p_head.add_argument("--timeout", type=int, default=600, help="秒（既定 600）")
    p_head.add_argument("--output-format", choices=["json", "text"], default="json")
    p_head.add_argument("--capability", help="退避ログ用の capability id")
    p_head.add_argument("--reason", help="退避理由（ネイティブが使えなかった理由を 1 行で）")
    p_head.add_argument("--use-subscription", dest="use_subscription",
                        action="store_true", default=default_sub,
                        help="サブスク認証を強制（API キー env を除去・既定 ON）")
    p_head.add_argument("--no-subscription", dest="use_subscription", action="store_false",
                        help="API 従量経路を使う")
    p_head.add_argument("--dry-run", action="store_true", help="コマンドを表示して終了")
    p_head.add_argument("extra", nargs="*", help="claude CLI へのパススルー引数（-- の後に置く）")

    args = parser.parse_args(argv)
    if args.self_test:
        return cmd_self_test()
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "routes":
        return cmd_routes(args)
    if args.command == "headless":
        return cmd_headless(args)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
