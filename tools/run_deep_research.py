#!/usr/bin/env python3
"""tools/run_deep_research.py

Deep Research の最終フォールバックランナー（DIY・ウェブリサーチ）。

主エンジンはネイティブ /deep-research（対話起動は Skill 直接呼び出し・
自律起動は `tools/run_deep_research_workflow.py` の `claude -p` サブプロセス）。
本ファイルはそれらが真に失敗したときの最終フォールバックを担う:
- 最終フォールバック: DIY (Sonnet 5 + WebSearch/WebFetch) 本ファイル内の DIY 実装
- 外部 LLM API（Gemini 等）によるディープリサーチは行わない（Issue #260 で廃止）
- 月コスト上限（API 従量経路時）: $50（warning $45・breaker $50）

呼び出し方法（{ID} は任意のリサーチ識別子 slug）:
    python3 tools/run_deep_research.py {ID}
    python3 tools/run_deep_research.py {ID} --dry-run
    python3 tools/run_deep_research.py {ID} --fallback-reason "workflow EXIT=4" --dry-run  # 発動記録のみ

出力:
    content/research/{ID}_deep_research.md (Markdown 正規形式)
    content/research/{ID}_deep_research.json (research_schema.json 準拠)

制約:
- DIY 実装は WebSearch / WebFetch を「呼び出す側」で実行する想定（Claude Code 内蔵ツール）。
  単独 Python 実行ではモック動作のみ。実運用は Claude Code セッション内から呼び出す。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
# CWD 相対だと実行場所により出力が迷子になるため REPO_ROOT にアンカーする。
REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO_ROOT / "content" / "research"
COST_LOG = REPO_ROOT / "content" / "pipeline-state" / "research_cost_log.jsonl"
# /deep-research→DIY フォールバック発動の記録（L-094 可視化・サイレントフォールバック防止）
FALLBACK_LOG = REPO_ROOT / "content" / "pipeline-state" / "research_fallback_log.jsonl"
MONTHLY_BUDGET_USD = 50.0
MONTHLY_WARNING_USD = 45.0

# サブスク週次枠経路（#2562/#2563）: 実課金しない /deep-research のコストは
# run_deep_research_workflow.py の log_cost が cost_usd=0.0 で記録する（対策A）。本ランナーは
# DIY（実課金）用のため check_budget はバイパスせず常に有効にしておく（対策B）。


@dataclass
class RunResult:
    """ランナー実行結果。schema 準拠の JSON 出力に使用する。"""

    research_id: str
    theme: str
    engine: str
    engine_version: str
    sections: list[dict] = field(default_factory=list)
    official_names: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    fact_check_flags: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    search_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_schema(self) -> dict:
        rank_dist = {"A": 0, "B": 0, "C": 0}
        for src in self.sources:
            r = src.get("rank", "C")
            if r not in rank_dist:  # A/B/C 以外は C 扱い（schema の rank_distribution を A/B/C に限定）
                r = "C"
            rank_dist[r] += 1
        return {
            "research_id": self.research_id,
            "theme": self.theme,
            "generated_at": datetime.now(JST).isoformat(),
            "engine": self.engine,
            "engine_version": self.engine_version,
            "sections": self.sections,
            "official_names": self.official_names,
            "sources": self.sources,
            "fact_check_flags": self.fact_check_flags,
            "metrics": {
                "duration_seconds": self.duration_seconds,
                "cost_usd": self.cost_usd,
                "search_count": self.search_count,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "rank_distribution": rank_dist,
            },
        }


def _repo_slug() -> str:
    """対象リポジトリ slug（owner/repo）を返す。

    bootstrap 済み（プレースホルダ解決済み）ならその値を最優先でそのまま返す（下流
    リポジトリの既定動作）。未解決（本リポジトリ自身への自己ホスト実行等・#215）の
    場合のみ `GITHUB_REPOSITORY` → git remote → 雛形プレースホルダ `kai-kou/btc-realtime-chart`
    のまま、の順で解決する（優先順位の実装は tools/repo_slug.py が正本）。
    """
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from repo_slug import resolve_repo_slug

    return resolve_repo_slug("kai-kou/btc-realtime-chart")


def _extract_prompt_from_issue_body(body: str) -> str | None:
    """Issue 本文から `## Deep Research プロンプト` セクション以降を抽出する。

    上流（プロンプト生成プロセス）が DR プロンプトを Issue 本文の
    `## Deep Research プロンプト` 見出し以降に埋め込む運用の場合のフォールバック。
    その見出し以降の全文をプロンプト本体として返す。
    """
    marker = "## Deep Research プロンプト"
    idx = body.find(marker)
    if idx == -1:
        return None
    after = body[idx + len(marker):]
    # 実際のプロンプト本体（`# {テーマ名} Deep Research プロンプト` 行）以降のみを抽出し、
    # 見出し直後に紛れ込む注意書き・テンプレート指示を除外する。
    # これにより refinement が正規生成する prompt.md と同一形式で自己治癒できる。
    lines = after.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# ") and "Deep Research" in line:
            return "\n".join(lines[i:]).strip() or None
    return after.strip() or None


def _fetch_issue_body_for_prompt(research_id: str) -> str | None:
    """gh CLI で research_id を含むオープン Issue 本文を取得する（フォールバック用）。

    プロンプトファイル未生成時に、上流が Issue 本文へ埋め込んだ
    `## Deep Research プロンプト` セクションを復元するために使う。
    """
    try:
        # `--search` のクエリには `[` `]` を含めない（GitHub 検索で特殊文字扱いされ結果が
        # 不完全になるため）。サーバー側で research_id を含む Issue に絞り込み、
        # さらに下の厳密フィルタでクライアント側でタイトル一致を確認する二段構え。
        completed = subprocess.run(
            [
                "gh", "issue", "list", "-R", _repo_slug(),
                "--search", f"{research_id} in:title", "--state", "open",
                "--json", "number,title,body", "--limit", "1000",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:  # noqa: BLE001
        sys.stderr.write(f"[WARN] Issue 検索に失敗: {exc}\n")
        return None
    try:
        issues = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(issues, list):
        return None
    # タイトルに research_id を含む Issue を優先採用する
    for it in issues:
        if not isinstance(it, dict):
            continue
        title = it.get("title", "")
        if research_id in title:
            return it.get("body", "")
    return None


def load_prompt(research_id: str) -> tuple[str, str]:
    """`content/research/{ID}_prompt.md` を読み、(theme, full_prompt_markdown) を返す。

    ファイル不在時は research_id を含むオープン Issue 本文から
    `## Deep Research プロンプト` セクションを抽出してフォールバックする。
    抽出に成功した場合はファイルにも保存し、次回以降の再取得を省く。
    """
    path = RESEARCH_DIR / f"{research_id}_prompt.md"
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        body = _fetch_issue_body_for_prompt(research_id)
        prompt = _extract_prompt_from_issue_body(body) if body else None
        if not prompt:
            raise FileNotFoundError(
                f"プロンプトファイルが存在せず、Issue 本文からも抽出できなかった: {path}. "
                f"上流のプロンプト生成（{path} の作成）を確認してください。"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        text = prompt
        sys.stderr.write(
            f"[INFO] {path} を Issue 本文から復元しました（フォールバック）\n"
        )
    theme = ""
    for line in text.splitlines():
        if line.startswith("# ") and "Deep Research" in line:
            theme = line.lstrip("# ").replace("Deep Research プロンプト", "").strip(" 　-")
            break
    return theme or research_id, text


def get_monthly_cost_total() -> float:
    """当月のリサーチ累計コストを返す（cost_log.jsonl から算出）。"""
    if not COST_LOG.exists():
        return 0.0
    now = datetime.now(JST)
    total = 0.0
    for line in COST_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if ts.year == now.year and ts.month == now.month:
                total += float(entry.get("cost_usd", 0.0))
        except (ValueError, TypeError):
            # 破損行・手動編集・timestamp 欠落に対する堅牢性（行をスキップ）
            continue
    return total


def append_cost_log(result: RunResult) -> None:
    """コスト記録を JSON Lines に追記する。"""
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(JST).isoformat(),
        "research_id": result.research_id,
        "engine": result.engine,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "search_count": result.search_count,
    }
    with COST_LOG.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_fallback_log(
    research_id: str,
    reason: str,
    from_engine: str = "claude-deep-research-workflow",
    to_engine: str = "diy-sonnet-websearch",
    exit_code: int | None = None,
) -> None:
    """フォールバック発動を記録する（L-094 可視化）。

    サイレントフォールバック（[WARN] のみで握りつぶし）は発動状態の見逃しを招く
    （Issue #2364 の教訓）。発動理由を JSON Lines に永続記録し、
    フォールバック発動率の監視・恒久対応の判断材料にする。
    （#4699: run_deep_research_workflow.py と共用する唯一のロガーに統合。
    exit_code は主エンジンの終了コード（1/4/5/6）・DIY 等では None。
    EXIT=6 は to_engine="skip-retry-claude-p"＝スキップ再試行であり DIY 降下ではない）
    """
    FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(JST).isoformat(),
        "research_id": research_id,
        "from_engine": from_engine,
        "to_engine": to_engine,
        "exit_code": exit_code,
        "reason": reason[:1000],
    }
    with FALLBACK_LOG.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_budget(estimated_cost: float) -> str | None:
    """予算超過判定。サーキットブレーカー時は理由文字列を返す（None ならOK）。"""
    # サブスク経路（#2562）の /deep-research コストは research_cost_log.jsonl に cost_usd=0.0 で
    # 記録される（#2563 対策A・run_deep_research_workflow.py log_cost）ため、get_monthly_cost_total は
    # 実課金（DIY）のみを集計する。よって本ブレーカーはサブスク経路でも誤発火せず、
    # フォールバック（実課金）に対する $50 安全ガードを常に維持する。
    current = get_monthly_cost_total()
    projected = current + estimated_cost
    if projected >= MONTHLY_BUDGET_USD:
        return (
            f"サーキットブレーカー発動: 当月累計 ${current:.2f} + 見積 ${estimated_cost:.2f} = "
            f"${projected:.2f} >= 上限 ${MONTHLY_BUDGET_USD:.2f}"
        )
    if projected >= MONTHLY_WARNING_USD:
        sys.stderr.write(
            f"[WARN] 当月累計が ${projected:.2f} に到達。$45 警告ライン超過（上限$50）。\n"
        )
    return None


def run_diy(research_id: str, theme: str, prompt_text: str) -> RunResult:
    """DIY フォールバック実装（Sonnet 5 + WebSearch / WebFetch）。

    Phase A: スケルトン（呼び出し元の Claude Code セッション内で WebSearch を実行する設計）。
    Phase B で並列 sub-agent + Anthropic Messages API への移行を行う。
    """
    start = time.perf_counter()  # 単調増加クロックで経過時間を計測（システム時刻変更の影響を受けない）
    result = RunResult(
        research_id=research_id,
        theme=theme,
        engine="diy-sonnet-websearch",
        engine_version="sonnet",   # エイリアス既定（agent-team.md「モデル指定の方針」）
    )
    # Phase A: 実装は research-runner SKILL.md の手順に従い、Claude Code セッション側で
    # WebSearch / WebFetch を実行して本ランナーに引き渡す形で完成させる。
    # 本ファイルは I/O 層・メトリクス層・スキーマ整形層を担当する。
    sys.stderr.write(
        "[INFO] DIY ランナー Phase A スケルトン: research-runner SKILL.md に従い "
        "Claude Code セッション内で sections/sources/official_names を埋めてください。\n"
    )
    result.duration_seconds = time.perf_counter() - start
    result.cost_usd = 0.55  # 想定単価（Sonnet 5 60K入力+25K出力）
    result.search_count = 8  # 想定値
    return result


def write_outputs(result: RunResult) -> tuple[Path, Path]:
    """JSON とMarkdown を `content/research/` に書き出す。"""
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESEARCH_DIR / f"{result.research_id}_deep_research.json"
    md_path = RESEARCH_DIR / f"{result.research_id}_deep_research.md"
    schema = result.to_schema()
    json_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(schema), encoding="utf-8")
    return json_path, md_path


def render_markdown(schema: dict) -> str:
    """research_schema.json 準拠の dict を Markdown に整形する（research-rules.md と互換）。"""
    lines: list[str] = []
    lines.append(f"# {schema['research_id']} Deep Research 結果")
    lines.append("")
    lines.append(f"- 生成日時: {schema['generated_at']}")
    lines.append(f"- 実行エンジン: `{schema['engine']}` (`{schema.get('engine_version','')}`)")
    metrics = schema.get("metrics", {})
    lines.append(
        f"- メトリクス: {metrics.get('duration_seconds',0):.1f}s / "
        f"${metrics.get('cost_usd',0):.2f} / 検索 {metrics.get('search_count',0)} 回"
    )
    rank = metrics.get("rank_distribution", {})
    lines.append(
        f"- ランク分布: A={rank.get('A',0)} B={rank.get('B',0)} C={rank.get('C',0)}"
    )
    lines.append("")
    for sec in schema.get("sections", []):
        lines.append(f"## {sec['heading']}")
        lines.append("")
        lines.append(sec["body_markdown"])
        lines.append("")
    if schema.get("official_names"):
        lines.append("## 正式名称確認リスト")
        lines.append("")
        for n in schema["official_names"]:
            ja = f" / {n.get('japanese')}" if n.get("japanese") else ""
            lines.append(f"- **{n['term']}** → {n['official']}{ja} (`{n['source_id']}`)")
        lines.append("")
    if schema.get("fact_check_flags"):
        lines.append("## fact_check_flags（要確認）")
        lines.append("")
        for f in schema["fact_check_flags"]:
            lines.append(f"- [{f['rank']}] {f['claim']} — {f['reason']}")
        lines.append("")
    lines.append("## 出典")
    lines.append("")
    for src in schema.get("sources", []):
        lines.append(
            f"- `{src['id']}` [{src['rank']}] [{src['title']}]({src['url']})"
            + (f" ({src.get('publisher')})" if src.get("publisher") else "")
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deep Research 最終フォールバックランナー（DIY・ウェブリサーチ）"
    )
    parser.add_argument("research_id", help="リサーチ ID（任意の slug・例: my-topic）")
    parser.add_argument(
        "--engine",
        choices=["diy"],
        default="diy",
        help="エンジン（diy のみ。外部 LLM API 経路は Issue #260 で廃止）",
    )
    parser.add_argument("--dry-run", action="store_true", help="実行せず予算チェックのみ")
    parser.add_argument(
        "--fallback-reason",
        default=None,
        help="主エンジン（/deep-research）からのフォールバック発動理由。"
        "指定時は research_fallback_log.jsonl に永続記録する（サイレントフォールバック禁止・L-094）",
    )
    args = parser.parse_args()

    # フォールバック発動記録は load_prompt より先に行う（プロンプト復元失敗で
    # 例外終了しても発動理由が必ず残るように・L-094 サイレントフォールバック防止）
    if args.fallback_reason:
        append_fallback_log(args.research_id, args.fallback_reason)
        sys.stderr.write(f"[INFO] フォールバック発動を {FALLBACK_LOG} に記録しました\n")
    theme, prompt_text = load_prompt(args.research_id)
    estimated = 0.55
    breaker = check_budget(estimated)
    if breaker:
        sys.stderr.write(f"[ERROR] {breaker}\n")
        return 2
    if args.dry_run:
        print(f"[DRY-RUN] {args.research_id} / engine={args.engine} / 見積 ${estimated:.2f}")
        return 0

    result = run_diy(args.research_id, theme, prompt_text)

    # B: 空 sections の場合はファイルを書かずエラー終了（Issue #2127）
    # DIY Phase A スケルトンが空結果を返した場合に
    # 空ファイルが生成されて「研究済み」フィルタをすり抜けるのを防止する。
    if not result.sections:
        sys.stderr.write(
            f"[ERROR] リサーチ結果が空（sections=0）。ファイルは書きません。"
            f" engine={result.engine}\n"
        )
        return 1

    json_path, md_path = write_outputs(result)
    # C: 実際に sections がある結果のみコストを記録（空スケルトンの bogus コストを除外）
    append_cost_log(result)
    print(f"[OK] 生成完了: {md_path}")
    print(f"     {json_path}")
    print(
        f"     エンジン: {result.engine} / コスト ${result.cost_usd:.2f} / "
        f"出典 {len(result.sources)} 件 / fact_check_flags {len(result.fact_check_flags)} 件"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
