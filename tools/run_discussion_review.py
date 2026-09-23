#!/usr/bin/env python3
"""run_discussion_review.py — 「議論型レビュー」WF主導オーケストレーター。

## 凍結（Phase 3・Issue #195・2026-07-13）

ネイティブ議論型（`.claude/skills/discussion-review/SKILL.md`）が安定運用の目安
（議論型実行 5 件・フォールバック発動 0 件）を満たしたため、本スクリプトは **新規開発を凍結**。
既定経路はネイティブに一本化済みで、本スクリプトは「ネイティブが成立しない場合の手動フォールバック」
としてのみ存置する。削除は行わない（`docs/proposals/native-agent-teams-migration.md` §5 Phase 3 参照）。

専門チームに **役割分担型 fan-out ではなく、敵対的相互レビュー（議論）** をさせ、
各エージェントが共有ホワイトボード（tools/discussion_whiteboard.py）に自由記載し、
議論の整理 + 履歴を git 管理できるようにする。

## なぜ claude -p を「tempdir」で起動するか（実機検証で確定した必須対策・2026-06-06）

- ハーネスのメインセッションには `Workflow`/`TeamCreate`/`SendMessage` が **非露出**だが、
  Bash から `claude -p` を起動すると使える（run_deep_research_workflow.py と同経路）。
- ⚠️ **`claude -p` を cwd=リポジトリで起動すると、その子セッションの SessionStart フック
  （session-start.sh の `git clean -fd` / `git checkout`）が未コミットの作業を破壊する**
  （実機で本ツール自身が消された・#2605/#2643 と同根）。
- 対策: **claude -p は一時ディレクトリ(cwd=tempdir)で起動**し、リポジトリには **絶対パス**で
  読み書きさせる。tempdir には .claude が無いため子セッションは project フックを読まず、
  リポジトリのクリーンアップが走らない（deep-research も同じ理由で tempdir 起動）。
- 議論の中間結果・履歴は共有ホワイトボード（Blackboard・git 管理 Markdown）に集約する。

## スペック（--spec JSON）: 最小例は tools/discussion_specs/example_debate.json を参照。
   プロジェクト固有のレビューは各スキル配下に discussion_review_spec.json を置いて渡す。

  {
    "topic": "...", "brief": "...",
    "participants": [{"name": "...", "model": "sonnet", "lens": "観点プロンプト"} , ...],
    "synthesizer": {"name": "lead", "instruction": "合意/判定の出し方"},
    "verdict_schema": "PASS|WARN|FAIL と critical[] を含む JSON 文字列"
  }

## 使い方

  python3 tools/run_discussion_review.py --id demo-debate \
      --spec tools/discussion_specs/example_debate.json --dry-run
  python3 tools/run_discussion_review.py --id review-001 \
      --spec path/to/discussion_review_spec.json \
      --targets "path/to/target.md" --rounds 2

出力: content/discussions/<id>/whiteboard.md（議論全文）+ stdout に最終 verdict JSON。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WB_TOOL_ABS = str(REPO_ROOT / "tools" / "discussion_whiteboard.py")
JST = _dt.timezone(_dt.timedelta(hours=9))
# discussion_whiteboard.py の author/participant 規約と一致させる（早期に分かりやすく弾く）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# discussion_whiteboard.py の discussion_id 規約（英数字 + _.-・先頭英数字・最大64）と一致させる
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_MODEL = "sonnet"   # エイリアス既定（最新 Sonnet に自動追随・agent-team.md「モデル指定の方針」）
# lead + サブエージェントが使うツール（root では allowedTools で事前許可）。
DEFAULT_ALLOWED_TOOLS = "Bash Read Write Edit Glob Grep Agent Task Workflow WebFetch"

USE_SUBSCRIPTION = os.getenv("DISCUSSION_USE_SUBSCRIPTION", "1") != "0"
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _now_iso() -> str:
    return _dt.datetime.now(JST).isoformat(timespec="seconds")


def _check_name(name: str, label: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"{label} が不正です: {name!r}（許可: 英数字と _ -・先頭は英数字・32字以内・"
            "空白/カンマ不可。discussion_whiteboard の author 規約に一致させること）")


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    parts = spec.get("participants")
    if not parts or len(parts) < 2:
        raise ValueError("議論には participants が 2 名以上必要です")
    for p in parts:
        if not isinstance(p, dict):
            raise ValueError(f"participant は dict である必要があります: {p!r}")
        if not p.get("name") or not p.get("lens"):
            raise ValueError(f"participant に name/lens が必要: {p}")
        _check_name(p["name"], "participant name")
    synth_name = (spec.get("synthesizer") or {}).get("name")
    if synth_name is not None:
        _check_name(synth_name, "synthesizer name")
    return spec


def _abs_target(t: str) -> str:
    p = Path(t)
    return str(p if p.is_absolute() else (REPO_ROOT / p))


def build_lead_prompt(discussion_id: str, spec: dict, targets: list[str], rounds: int) -> str:
    participants = spec["participants"]
    names = [p["name"] for p in participants]
    parts_block = "\n".join(
        f"  - `{p['name']}`（model: {p.get('model', 'sonnet')}）: {p['lens']}"
        for p in participants)
    synth = spec.get("synthesizer", {})
    synth_name = synth.get("name", "lead")
    synth_inst = synth.get("instruction",
                           "全投稿を読み、対立点・合意点を整理し、最終判定(PASS/WARN/FAIL)と critical[] を出す。")
    verdict_schema = spec.get(
        "verdict_schema", '{"verdict":"PASS|WARN|FAIL","critical":[],"consensus":"","summary":""}')
    targets_block = "\n".join(f"  - {_abs_target(t)}" for t in targets) if targets else "  （対象ファイル指定なし）"
    wb_abs = str(REPO_ROOT / "content" / "discussions" / discussion_id / "whiteboard.md")

    # 進行手順を --rounds に追従させる（rounds=1 は反論ラウンドを省略・rounds>=2 は 2..N で相互 rebuttal）
    if rounds >= 2:
        rebuttal_step = (
            f"- ラウンド 2〜{rounds}: 各ラウンド開始時に `render` し、各エージェントに **`show` で他者の投稿を"
            "読ませ、相手の具体的指摘に対する rebuttal/concession を round k（k=2..N）で `post`** させる"
            "（誤検知・過剰指摘を相互に潰す＝敵対的検証）。round2 以降は対象を再読せず、round1 の自分の分析と"
            "ホワイトボード（`show`）のみで反論する（再読トークン削減）。")
    else:
        rebuttal_step = (
            "- （`--rounds 1` のため反論ラウンドは省略。round1 の独立分析のみで合意へ進む）")

    return f"""あなたは「議論型レビュー」の進行役(lead)です。役割分担で各自が独立報告するのではなく、
専門家どうしを **敵対的に議論（互いの指摘を批判検証）** させ、共有ホワイトボードに記録しながら
合意を形成してください。中間結果はあなたの最終文ではなく **ホワイトボードに残す**こと。

## 重要: 全てのファイル参照は絶対パスで行うこと
- リポジトリルート: {REPO_ROOT}
- 参照ドキュメント等はこのルート配下の絶対パスで Read する（例: {REPO_ROOT}/docs/project-mission.md）。

## 議題
- ID: {discussion_id}
- テーマ: {spec.get('topic', discussion_id)}
- 論点: {spec.get('brief', '')}

## レビュー対象（絶対パス）
{targets_block}

## 参加する専門家（レンズ）
{parts_block}

## 共有ホワイトボード（Blackboard）の使い方（厳守）
- 投稿は **必ず**次の Bash コマンドで行う（whiteboard.md を直接 Write/Edit しない・同時書き込み破損防止）:
  `python3 {WB_TOOL_ABS} post {discussion_id} --author <name> --round <n> --kind <claim|evidence|rebuttal|question|concession|consensus|verdict> --body-file <path>`
- **本文が複数行・引用符を含む場合は必ず `--body-file <path>` か stdin（`... post ... < file`）を使う**
  （`--body "<本文>"` は Bash のクォートが壊れて投稿に失敗しうるため、1 行の短文のときだけにする）。
- ラウンド境界では集約: `python3 {WB_TOOL_ABS} render {discussion_id}`
- 他者の意見を読むには: `python3 {WB_TOOL_ABS} show {discussion_id}`

## 進行手順（{rounds} ラウンド + 合意）
1. 各専門家エージェント（{', '.join(names)}）を Agent ツールで spawn する。各エージェントは
   自分のレンズで対象を分析し、**自分の名前で round 1 の claim/evidence を `post`** する。
   （各 post はユニークファイルなので並列でも安全。whiteboard.md は直接編集しない）
{rebuttal_step}
- 合意: 進行役 `{synth_name}` として全投稿を読み、{synth_inst}
  合意点を kind=consensus、最終判定を kind=verdict で `post` する。
- 締め: 最後に `render` する。

## 最終出力（stdout の最後に必ず1個だけ JSON を出す）
{verdict_schema}
（critical は「議論を経ても残った真の問題」のみ。相互検証で否定された指摘は除外する）

ホワイトボード(絶対パス): {wb_abs}
"""


def run_claude(prompt: str, model: str, allowed_tools: str,
               max_budget_usd: float | None, timeout: int) -> dict:
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--allowedTools", allowed_tools, "--fallback-model", "haiku"]
    if max_budget_usd is not None and not USE_SUBSCRIPTION:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    child_env = None
    if USE_SUBSCRIPTION:
        child_env = {k: v for k, v in os.environ.items() if k not in _API_KEY_ENV_VARS}
    # ⚠️ cwd は一時ディレクトリ（リポジトリにすると子セッションの SessionStart フックが
    # 未コミット作業を git clean で破壊する）。リポジトリへは絶対パスで読み書きさせる。
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                cmd, cwd=tmp, capture_output=True, env=child_env,
                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            return {"is_error": True, "result": "", "total_cost_usd": 0.0,
                    "errors": f"subprocess failed: {exc}", "_returncode": -1}
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {"result": out, "total_cost_usd": 0.0}
    data["_returncode"] = proc.returncode
    data["_stderr_tail"] = (proc.stderr or "")[-2000:]
    if proc.returncode != 0:
        data["is_error"] = True
    return data


def _extract_verdict(text: str) -> dict | None:
    end = text.rfind("}")
    if end == -1:
        return None
    for s in range(end, -1, -1):
        if text[s] == "{":
            try:
                return json.loads(text[s:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="議論型レビュー（WF主導・ホワイトボード）")
    ap.add_argument("--id", required=True, help="議題ID（content/discussions/<id>/ に保存）")
    ap.add_argument("--spec", required=True, help="議論スペック JSON のパス")
    ap.add_argument("--targets", default="", help="レビュー対象パス（カンマ区切り）")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-budget-usd", type=float, default=8.0)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--allowed-tools", default=DEFAULT_ALLOWED_TOOLS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # --id を早期検証（不正 ID で lead プロンプト生成や init まで進ませず分かりやすく落とす）
    if not _ID_RE.match(args.id or ""):
        print(f"ERROR: --id が不正です: {args.id!r}（許可: 英数字と _ . -・先頭は英数字・64字以内・"
              "スラッシュ/空白不可。discussion_whiteboard の discussion_id 規約に一致させること）",
              file=sys.stderr)
        return 2
    if args.rounds < 1:
        print(f"ERROR: --rounds は 1 以上が必要です: {args.rounds}", file=sys.stderr)
        return 2

    try:
        spec = load_spec(args.spec)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: spec 読み込み/検証に失敗: {exc}", file=sys.stderr)
        return 2
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    # 起動前に targets の存在を検証（claude 実行後に Read 失敗で高コストに落ちるのを防ぐ）
    missing = [t for t in targets if not Path(_abs_target(t)).exists()]
    if missing:
        print(f"ERROR: 存在しない --targets: {missing}（絶対パス化して確認: "
              f"{[_abs_target(t) for t in missing]}）", file=sys.stderr)
        return 2
    participants = ",".join(p["name"] for p in spec["participants"])

    init_cmd = ["python3", WB_TOOL_ABS, "init", args.id,
                "--topic", spec.get("topic", args.id),
                "--participants", participants, "--brief", spec.get("brief", "")]
    prompt = build_lead_prompt(args.id, spec, targets, args.rounds)

    if args.dry_run:
        print("[dry-run] init:", " ".join(init_cmd))
        print("[dry-run] claude -p は cwd=一時ディレクトリ で起動（リポジトリへは絶対パス）")
        print("[dry-run] model:", args.model, "/ allowedTools:", args.allowed_tools)
        print("[dry-run] budget:", "サブスク認証" if USE_SUBSCRIPTION else f"${args.max_budget_usd}")
        print("\n===== lead プロンプト =====\n")
        print(prompt)
        return 0

    subprocess.run(init_cmd, cwd=str(REPO_ROOT), check=True)
    print(f"[1/2] 議論実行中（claude -p model={args.model}・rounds={args.rounds}・cwd=tempdir）...")
    data = run_claude(prompt, args.model, args.allowed_tools, args.max_budget_usd, args.timeout)
    result = (data.get("result") or "").strip()
    cost = float(data.get("total_cost_usd") or 0.0)

    # 締めの render（discussion_whiteboard.py 側にクロバー防止ガードがあるため安全）
    subprocess.run(["python3", WB_TOOL_ABS, "render", args.id, "--quiet"],
                   cwd=str(REPO_ROOT), check=False)

    wb_path = REPO_ROOT / "content" / "discussions" / args.id / "whiteboard.md"
    print(f"[2/2] ホワイトボード: {wb_path}（cost=${cost:.3f}）")

    if data.get("is_error") and not result:
        print(f"ERROR: 議論実行に失敗: {data.get('errors') or data.get('_stderr_tail')}", file=sys.stderr)
        return 1

    verdict = _extract_verdict(result)
    if verdict is not None:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0
    print("⚠️ 最終 verdict JSON を抽出できませんでした。whiteboard.md を確認してください。", file=sys.stderr)
    print(result[-1500:], file=sys.stderr)  # 失敗時ログは stdout を汚さないよう stderr へ
    return 1


if __name__ == "__main__":
    sys.exit(main())
