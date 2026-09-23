#!/usr/bin/env python3
"""
run_deep_research_workflow.py — Deep Research の「主エンジン」。

Claude Code のネイティブ /deep-research（Dynamic Workflows）を起動して引用付きレポートを取得し、
`tools/research_schema.json` 準拠の JSON に正規化する。

背景・位置づけ（research-runner SKILL.md「採用方針」「Step 3」）:
  - 主エンジン Step 3a（対話起動）: メインセッションから `Workflow` ツール
    （`Workflow({name: 'deep-research', args: ...})`）で /deep-research を直接呼ぶ。
    `Skill` ツール経由は CLI v2.1.218 以降 disable-model-invocation で不可（実機検証 2026-07-29）
    （本ツールは経由しない。生レポートを保存後は本ツールの --normalize-only だけを使う）
  - 主エンジン Step 3b（本ツール・自律/バッチ起動）: /deep-research を claude -p サブプロセス経由で
    実行 + 正規化。Opus 明示指定（#2417）・レート枠スキップカウンタ・月次コストログ等の既存インフラを
    再利用するため、cross-session 実行が必要な自律パイプラインではこちらを使い続ける
  - フォールバック: DIY（ウェブリサーチ・Step 3a/3b が失敗 or 月次予算ゲート超過時の最終手段）。
    外部 LLM API（Gemini 等）によるディープリサーチ経路は Issue #260 で廃止済み

実行モデル（2026-07-03 公式ドキュメント確認・確定）:
  code.claude.com/docs/en/workflows・/en/commands を Fetch して事実確認した結果:
  - /deep-research は公式に「Workflow」に分類される（Skill ではない）。CLI・Desktop・IDE拡張・
    claude -p（非対話）・Agent SDK のいずれでも同一に動作し、クラウド実行環境（本ハーネス）からも
    claude -p サブプロセスを介さず直接 invoke できることが確定した（詳細: docs/rules/dynamic-workflows-rules.md）。
  - モデルは公式には「セッションのモデルを使う（スクリプトが明示的に別モデルへ routing しない限り）」。
    本ツールが DEFAULT_ENGINE_MODEL=opus（エイリアス）を --model で明示指定しているのは
    **本プロジェクトの選択**であり、ネイティブ /deep-research 自体が Opus に固定されている
    という確認は取れていない（過去の「Opus orchestrator」という言い回しは本ツールの説明として使う）。
  - 本ツール（claude -p サブプロセス経由）は Step 3b 専用として存続する。対話起動（Step 3a）は
    直接呼び出しに一本化し、本ツールの起動自体は行わない（--normalize-only のみ再利用）。
  実績: V167 実走で「23ソース取得→92主張抽出→25主張を3票 adversarial 検証（refute 0）」の
  引用付きレポートを生成（fixtures/ にサンプル保存）。

起動形式の追従（2026-07-16 下流プロジェクトの実運用障害を反映・Issue #262）:
  CLI v2.1.2xx で /deep-research のスラッシュコマンド解釈が廃止され、bundled workflow を
  Workflow ツール（`Workflow({name: 'deep-research', args: ...})`）で起動する形式に変わった。
  旧形式プロンプト `/deep-research {質問}` は素の短文回答しか返さず EXIT=4 が恒常化するため、
  dr_prompt はツール起動を明示指示する（起動不能時は DEEP_RESEARCH_UNAVAILABLE センチネルで
  確定検知・完了までのポーリング継続指示付き）。EXIT=1/4/5/6 は research_fallback_log.jsonl に
  自動記録し、反復（CLI インターフェース再変化）を検知可能にする。

使い方（{ID} は任意のリサーチ識別子 slug）:
  # フル（検索 + 正規化）— 予算上限なし（検証用途では上限を付けない方針）
  python3 tools/run_deep_research_workflow.py {ID}

  # 予算上限を付ける（API 従量経路時の保険・サブスク経路では無視される）
  python3 tools/run_deep_research_workflow.py {ID} --max-budget-usd 8

  # 正規化のみ（既存の生レポートを schema 化）— normalize NG → 再正規化に便利
  python3 tools/run_deep_research_workflow.py {ID} \
      --normalize-only --report content/research/{ID}_research_raw.md

  # ドライラン（コマンドを表示するだけ）
  python3 tools/run_deep_research_workflow.py {ID} --dry-run

注意:
  - root 実行環境では `--dangerously-skip-permissions` / `--permission-mode bypassPermissions`
    が使えないため、`--allowedTools` で必要ツールを事前許可する方式を採る。
  - 検索サブプロセスは CLAUDE.md 読み込みコストを避けるため一時ディレクトリを cwd にして起動する。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO_ROOT / "content" / "research"
SCHEMA_PATH = REPO_ROOT / "tools" / "research_schema.json"
COST_LOG = REPO_ROOT / "content" / "pipeline-state" / "research_cost_log.jsonl"

DEFAULT_ENGINE_MODEL = "opus"        # エイリアス既定（最新 Opus に自動追随・agent-team.md「モデル指定の方針」）
DEFAULT_NORMALIZE_MODEL = "sonnet"   # 同上（最新 Sonnet に自動追随）

# サブスク週次枠経路（#2562・ユーザー指示 2026-06-04）:
# claude -p サブプロセスの env から ANTHROPIC_API_KEY を除去して Claude Code Max の
# サブスク認証を強制し、追加 $ ゼロ（週次使用量制限の枠内）で /deep-research を実行する。
# 既定 ON。DEEP_RESEARCH_USE_SUBSCRIPTION=0 で従来の API 従量経路（$18.74/本）に戻せる。
USE_SUBSCRIPTION = os.getenv("DEEP_RESEARCH_USE_SUBSCRIPTION", "1") != "0"
# サブスク認証を強制するため子プロセス env から除去する API キー系変数
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# コストガード（#2411 飼い主指示で /deep-research を主エンジン化・モデルは #2417 で Opus 確定）:
# 既定 orchestrator は Opus（実測 ~$18.7/本・約36分 wall-clock・#2429 V167 実測）。Sonnet 降格は実測でコスト不変
# （$18.8≒$18.7）かつ Opus より遅い（Sonnet 約52分 vs Opus 約36分・≒1.4倍）ため不採用 → 速度優先で Opus（モデル確定 #2417）。
# 主エンジンだが月 $50 枠を DIY と共有するため、月次ゲートを「deep-research→DIY 切替点」として使う:
# - 1本あたり既定上限 $20（Opus 実走 ~$18.7 を許容しつつ暴走を防止。検証時は上書き可）
# - 当月の本エンジン累計が $40 を超えたら起動拒否 → DIY にフォールバック（Opus ~$18.7 なら約2本/月で切替）
DEFAULT_MAX_BUDGET_USD = 20.0
MONTHLY_BUDGET_GATE_USD = 40.0   # /deep-research 単体の月次ゲート（超過→DIY フォールバック）
MONTHLY_BUDGET_LIMIT_USD = 50.0  # プロジェクト全体の月次上限（run_deep_research.py と整合）
WORKFLOW_ENGINE_PREFIX = "deep-research-workflow"
# /deep-research のサブエージェントが使う組み込みツール（root では allowedTools で事前許可する）。
# 🔴 Bash/Write は必須: /deep-research（Dynamic Workflows）はオーケストレーションスクリプトの
#   実行に Bash を、中間成果物の保存に Write を使う。これらを外すと -p 非対話モードで
#   権限待ちハングする（2026-06-01 実機検証: 除外で 7分超 [1/3] 停止・付与で ~90秒完走）。
# 🔴 Workflow が必須（#4699）: CLI v2.1.2xx で deep-research の起動形式が bundled workflow の
#   `Workflow({name: 'deep-research', args: ...})` に変わったため。
#   なお `Skill("deep-research")` は CLI v2.1.218 以降 disable-model-invocation で失敗する
#   （実機検証 2026-07-29 / v2.1.220・#370）。allowedTools の `Skill` は他スキル起動用に残す。
# 🔴 TaskOutput/TaskGet も必須（#4722）: dr_prompt がバックグラウンド Workflow の完了ポーリングに
#   指示するツール。未許可だと -p 非対話モードで権限待ちになり、ポーリングできず早期リターンする。
# プロンプトインジェクション（取得ページ経由の任意コマンド実行）対策は、サブプロセスを
#   一時 cwd（TemporaryDirectory）に隔離し + `--max-budget-usd` 上限で暴走を抑制することで緩和する。
SEARCH_ALLOWED_TOOLS = "Workflow Agent Task TaskOutput TaskGet Skill WebSearch WebFetch Read Bash Write"

# /deep-research に「全文をこのファイルへ Write せよ」と指示する作業ファイル名（#2704）。
# レポート本文の正本はこのファイル（永続 work_dir に書かせて harvest する）。`--output-format json`
# の result（最終アシスタントメッセージ）は中間ターンを全て捨てるため保険扱いにする。
REPORT_FILENAME = "deep_research_report.md"
# 本文回収の最低文字数ガード（#2704）。成功例は 12,000〜24,000 字（V167:21k / V158:15k / V136:24k）。
# これを下回る場合は出力欠落（V183 の 438 字問題）とみなし、高コストな normalize に進む前に
# 即フォールバック（DIY）へ誘導する。生レポートが取れている限りまず発火しない非常ベル。
MIN_REPORT_CHARS = 3000

# /deep-research サブプロセスの timeout（#2704 followup・#2704 検証で判明した根因対策）。
# サブスク経路は 5時間レート制限ウィンドウ（overage 不可・org_level_disabled）の影響で wall-clock が
# 大きく変動する（V183 実測: allowed 時で約56分 / 枠が逼迫すると 60分超でストール）。旧既定 3600s では
# フル本文の生成完了前に切られて 438字化・全損していた。完遂優先で既定 90分に延長する
# （サブスクは追加$ゼロ・research-runner は run_in_background + heartbeat 監視前提）。
# DEEP_RESEARCH_TIMEOUT_SEC で上書き可（例: hourly スロットを優先するなら短く設定）。
try:
    DEFAULT_TIMEOUT_SEC = int(os.getenv("DEEP_RESEARCH_TIMEOUT_SEC", "5400"))
except ValueError:
    # 非数値・空文字が設定されてもインポート時クラッシュさせず既定にフォールバック（Gemini レビュー）
    DEFAULT_TIMEOUT_SEC = 5400

JST = _dt.timezone(_dt.timedelta(hours=9))


class ReportTooShortError(RuntimeError):
    """harvest/result どちらでも本文が MIN_REPORT_CHARS 未満（出力欠落）の確定エラー。

    #2704: 「本文回収失敗」専用の例外。権限不足・サブプロセス実行エラー（別の RuntimeError）と
    終了コードを区別するため独立クラスにする（main で EXIT=4 にマップ）。
    """


class RateLimitedError(RuntimeError):
    """claude -p がサブスク 5 時間レート枠超過（usage limit reached）で本文を出せなかった確定エラー。

    #2814: ユーザー指示「ディープリサーチは必ず claude -p で行う。レート枠超過はスキップし、
    連続3回スキップして初めてフォールバック（DIY）を使う」を実装するための専用例外。capacity（容量）起因の
    一時的失敗であり、schema NG（EXIT=1）・本文回収失敗（EXIT=4）・実行エラー（EXIT=5）のような
    「やり直しても直らない失敗」とは性質が異なる。main で **EXIT=6** にマップし、research-runner は
    これを「DIY へ即フォールバックせず、次スロットで claude -p を再試行（スキップ）」と解釈する。
    """


# レート枠超過（capacity）のシグネチャ。successful な長文レポートが本文中で偶然「rate limit」に
# 言及するケースを誤検知しないよう、検出は **失敗時（is_error / 非ゼロ終了 / 本文が極端に短い）に限定** する
# （run_deep_research 内で len(report) < MIN_REPORT_CHARS ガードと併用）。
_RATE_LIMIT_PATTERNS = (
    r"usage limit",            # "Claude usage limit reached"（CLI のサブスク枠枯渇メッセージ）
    r"rate.?limit(?:ed)?",     # rate limit / rate_limit / ratelimited
    r"\b5[\s-]?hour\b",        # 5-hour / 5 hour（five_hour window）
    r"five[_\s]hour",          # five_hour
    # 誤検知防止（Gemini レビュー #2815）: 単独の "limit reached" は "recursion limit reached" 等の
    # 実行エラー（本来 EXIT=5）に誤マッチするため、usage/rate を前置した形に限定する
    r"\b(?:usage|rate)\s+limit\s+reached\b",
    r"too many requests",      # HTTP 429 文言
    # 誤検知防止（Gemini レビュー #2815）: 裸の \b429\b は動画ID（V429）やプロンプト内の数値に
    # 誤マッチするため、HTTP ステータス文脈に限定する
    r"status(?:_code)?[\"']?\s*[:=]\s*429|http\s+(?:status\s+)?429",
    r"\bquota\b",              # quota exhausted
    r"overage.{0,20}reject",   # overageStatus: rejected（org_level_disabled で overage 不可）
    r"resets?\s+at",           # "resets at <epoch>"（レート枠リセット時刻の併記）
)


def _is_rate_limited(data: dict) -> bool:
    """claude -p の失敗出力がレート枠超過（capacity）起因かを判定する（#2814）。

    判定対象は失敗シグナルを含むフィールドのみ（_stderr_tail / errors / result）。
    成功した長文レポート本文は対象にしない（呼び出し側が len(report) < MIN_REPORT_CHARS でガード）。
    """
    blob = " ".join(
        str(data.get(k) or "") for k in ("_stderr_tail", "errors", "result")
    ).lower()
    if not blob.strip():
        return False
    return any(re.search(p, blob) for p in _RATE_LIMIT_PATTERNS)


def _now_iso() -> str:
    return _dt.datetime.now(JST).isoformat()


def _log_engine_failure(research_id: str, exit_code: int, reason: str) -> None:
    """主エンジン（/deep-research）失敗を research_fallback_log.jsonl に永続記録する（下流プロジェクトの実障害を契機に追加）。

    下流プロジェクトで 2026-07 に発生した EXIT=4 恒常化はこのログに一切記録されず、フォールバック理由の
    記録源が PR 本文だけだったため横断検知が遅れた。EXIT=1/4/5/6 の全失敗経路で必ず記録し、
    同型失敗の反復（CLI インターフェース変化等）を検知可能にする。
    記録の実体は run_deep_research.append_fallback_log に一本化（FALLBACK_LOG は
    同モジュール側で絶対パスにアンカー済み＝cwd 非依存）。書き込み失敗でリサーチ本体は止めない。
    """
    try:
        # tools/ 直下の sibling として import（本スクリプトを直接実行した場合は
        # sys.path[0]=tools/ で解決する。他 cwd からの import 用に保険で追加。
        # 重複挿入を防ぐ（長寿命プロセスで sys.path が失敗のたびに伸びないように）
        tools_dir = str(Path(__file__).resolve().parent)
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from run_deep_research import append_fallback_log
        # to_engine は EXIT コードごとの実際の次アクションを記録する（誤集計防止）:
        #   6=レート枠超過 → DIY に降りず「スキップ→次スロット再試行」
        #   1=normalize/schema 失敗 → まず --normalize-only 再試行（$0）。DIY 直行ではない
        #   4/5=真の失敗 → DIY フォールバック
        to_engine = {6: "skip-retry-claude-p", 1: "normalize-only-retry"}.get(
            exit_code, "diy-sonnet-websearch")
        append_fallback_log(research_id, reason, to_engine=to_engine, exit_code=exit_code)
    except Exception as exc:  # noqa: BLE001 — ログ失敗でリサーチ本体を止めない
        print(f"[WARN] fallback log 書き込み失敗: {exc}", file=sys.stderr)


def read_prompt(research_id: str) -> str:
    path = RESEARCH_DIR / f"{research_id}_prompt.md"
    if not path.exists():
        raise FileNotFoundError(
            f"プロンプト未生成: {path}（上流のプロンプト生成が未実行の可能性）"
        )
    return path.read_text(encoding="utf-8")


def _run_claude(prompt: str, model: str, allowed_tools: str | None,
                max_budget_usd: float | None, timeout: int,
                work_dir: str | None = None) -> dict:
    """claude -p を JSON 出力で実行し、パース済み結果 dict を返す。

    work_dir を渡すと、その永続ディレクトリを cwd にして実行する（呼び出し側がライフサイクルを
    管理し、サブプロセスが Write したファイルを harvest する用途・#2704）。None の場合は従来どおり
    使い捨て TemporaryDirectory を cwd にする（normalize 等のファイル回収が不要な呼び出し向け）。
    """
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--fallback-model", "sonnet"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    # サブスク経路（#2562）では API 課金用の予算上限を付けない（上限は週次クォータが担保）。
    # 従来 API 経路（USE_SUBSCRIPTION=0）では従来どおり --max-budget-usd を付与する。
    if max_budget_usd is not None and not USE_SUBSCRIPTION:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    # サブスク認証を強制する場合、子プロセス env から API キー系変数を除去する（#2562）。
    # API キーが残っていると API 従量課金経路になるため確定的に除去する。
    child_env = None
    if USE_SUBSCRIPTION:
        child_env = {k: v for k, v in os.environ.items() if k not in _API_KEY_ENV_VARS}

    def _exec(cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, env=child_env,
            text=True, encoding="utf-8", errors="replace", timeout=timeout
        )

    # wall-clock 実測（早期リターン vs timeout 到達を切り分けるため・#4722）
    started = time.monotonic()
    try:
        if work_dir is not None:
            # 永続 cwd（呼び出し側が削除・harvest を管理する）
            proc = _exec(work_dir)
        else:
            # CLAUDE.md 自動読込のコストを避けるため使い捨て一時ディレクトリで実行
            with tempfile.TemporaryDirectory() as tmp:
                proc = _exec(tmp)
    except (subprocess.SubprocessError, OSError) as exc:
        # タイムアウト（TimeoutExpired）・claude 不在（OSError）等でクラッシュさせない（styleguide L20）
        return {
            "is_error": True,
            "result": "",
            "total_cost_usd": 0.0,
            "errors": f"Subprocess execution failed: {exc}",
            "_returncode": -1,
            "_stderr_tail": str(exc)[-2000:],
            "_elapsed_sec": round(time.monotonic() - started, 1),
        }
    elapsed_sec = round(time.monotonic() - started, 1)
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # --output-format json が崩れた場合は生テキストを result とみなす
        data = {"result": out, "total_cost_usd": 0.0}
    # returncode / stderr 末尾は常に保存し、非ゼロ終了なら is_error を強制（失敗検知の安定化）
    data["_returncode"] = proc.returncode
    data["_stderr_tail"] = (proc.stderr or "")[-2000:]
    data["_elapsed_sec"] = elapsed_sec
    if proc.returncode != 0:
        data["is_error"] = True
    return data


def _harvest_report_file(work_dir: Path) -> tuple[str, str | None]:
    """work_dir に書き出されたレポート本文を探し (本文, 採用ファイル名) を返す。

    #2704: /deep-research は中間成果物・最終レポートを Write でファイル保存する（:74-79 のコメント参照）。
    従来は cwd が使い捨て TemporaryDirectory で実行直後に削除され、本文がファイルに書かれた回は
    丸ごと喪失していた（result には短い締めだけ残る = V183 の 438 字問題）。永続 work_dir を harvest して
    本文を回収する。優先順: ① 指示ファイル REPORT_FILENAME → ② 最大の .md → ③ 最大のテキストファイル。
    走査は「作業ディレクトリ直下のみ」（指示は直下保存・サブディレクトリの中間成果物を誤採用しない/高速）。
    """
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    text_exts = (".md", ".markdown", ".txt")
    candidates = [p for p in sorted(work_dir.iterdir())
                  if p.is_file() and p.suffix.lower() in text_exts]
    if not candidates:
        return "", None
    # ① 指示ファイルを最優先（中身が十分にあれば即採用）。is_file で同名ディレクトリ時の誤動作を防ぐ
    preferred = work_dir / REPORT_FILENAME
    if preferred.is_file():
        txt = _read(preferred)
        if len(txt) >= MIN_REPORT_CHARS:
            return txt, preferred.name
    # ②/③ .md を優先し、なければ全テキストファイルから最大サイズを採用
    md_files = [p for p in candidates if p.suffix.lower() in (".md", ".markdown")]
    best = max(md_files or candidates, key=lambda p: p.stat().st_size)
    return _read(best), best.name


def _build_dr_prompt(prompt: str) -> str:
    """/deep-research 起動用の作業指示プロンプトを組み立てる（実行・--dry-run 共用）。

    出力指示（リサーチ範囲ではなく作業指示）を明示し、全文を REPORT_FILENAME へ Write させる。
    🔴 起動形式（下流プロジェクトの実障害を踏まえた根本原因対策）: 旧形式 `/deep-research {質問}` は CLI v2.1.2xx で
    ワークフローとして解釈されなくなり、モデルが素の短文回答（735〜1183字）を返して
    EXIT=4（本文回収失敗）が恒常化していた（下流プロジェクトで 2026-07 に観測された実障害）。
    現行 CLI では deep-research は bundled workflow を Workflow ツールで起動する
    （バイナリ内エラー文字列: "Pass it as args: Workflow({name: 'deep-research', args: '<question>'})"）。
    スラッシュコマンド解釈に依存せず、ツール起動を明示指示する形へ変更した。
    """
    return (
        "あなたはリサーチ実行器。以下の手順を実行せよ。\n"
        "1. Workflow ツールを `Workflow({name: 'deep-research', args: <質問文>})` の形で呼び出し、"
        "下記の【質問文】全文を args に渡してディープリサーチを実行する"
        "（`Skill(\"deep-research\")` は CLI v2.1.218 以降 disable-model-invocation で失敗するため使わない）。\n"
        "2. Workflow ツールは即座に『バックグラウンドで実行中です』という応答を返すことがあるが、"
        "その1文だけで応答を終えてはならない。同一ターン内で TaskOutput / TaskGet ツール等を使い、"
        "ワークフローが完了するまでポーリングを繰り返してから手順3へ進むこと"
        "（応答を早期に終了すると本文が回収できず失敗扱いになる・#4722）。\n"
        f"3. 完了後、最終的な引用付きレポートの全文を、作業ディレクトリ直下のファイル "
        f"`{REPORT_FILENAME}` に Write ツールで保存する。要約や省略をせず本文をそのまま書き出す。\n"
        "4. ワークフローを起動できない場合は、素の回答を生成せず "
        "`DEEP_RESEARCH_UNAVAILABLE: <理由>` とだけ出力して終了する（勝手に自前リサーチしない）。\n\n"
        f"【質問文】\n{prompt}"
    )


def run_deep_research(prompt: str, model: str, max_budget_usd: float | None,
                      timeout: int = DEFAULT_TIMEOUT_SEC,
                      allowed_tools: str = SEARCH_ALLOWED_TOOLS) -> tuple[str, float, dict]:
    """ネイティブ /deep-research をサブプロセス起動し、(report_md, cost, meta) を返す。

    #2704: 本文の正本は「永続 work_dir に Write されたファイル」。result（最終メッセージ）は保険として
    比較し、長い方を採用する。本文が MIN_REPORT_CHARS 未満なら出力欠落とみなし即エラー（長さガード）→
    呼び出し側が DIY フォールバックへ誘導する（高コストな normalize の前で確定的に落とす）。
    """
    dr_prompt = _build_dr_prompt(prompt)
    work_dir = Path(tempfile.mkdtemp(prefix="deepresearch_"))
    try:
        data = _run_claude(dr_prompt, model, allowed_tools, max_budget_usd,
                           timeout, work_dir=str(work_dir))
        result_text = (data.get("result") or "").strip()
        cost = float(data.get("total_cost_usd") or 0.0)
        file_text, harvested = _harvest_report_file(work_dir)
    finally:
        # 永続 work_dir はインジェクション隔離のため必ず破棄する
        shutil.rmtree(work_dir, ignore_errors=True)

    # 正本＝ファイル（result は保険）。長い方を本文に採用する。
    if len(file_text) >= len(result_text):
        report, source = file_text, (f"file:{harvested}" if harvested else "file")
    else:
        report, source = result_text, "result"
    data["_report_source"] = source
    data["_result_len"] = len(result_text)
    data["_file_len"] = len(file_text)

    # レート枠超過（capacity）検出（#2814）: 本文を出せず失敗し、かつ失敗出力がレート枠超過の
    # シグネチャを含むなら RateLimitedError（EXIT=6）。DIY へ落とさず「スキップ→次スロットで
    # claude -p 再試行」とするためのシグナル。誤検知防止に3条件を AND:
    #   ① len(report) < MIN_REPORT_CHARS（本文を出せていない）
    #   ② data.is_error（claude -p 実行自体が失敗終了）— Gemini レビュー #2815: 「レート制限がテーマの
    #      リサーチが成功したが3000字未満」のケースを EXIT=6 と誤判定しないための前提条件
    #   ③ _is_rate_limited（失敗出力がレート枠超過シグネチャを含む）
    if len(report) < MIN_REPORT_CHARS and data.get("is_error") and _is_rate_limited(data):
        raise RateLimitedError(
            "claude -p がレート枠超過（usage/rate limit）で本文を生成できなかった。"
            f"内訳 result={len(result_text)}字 / file={len(file_text)}字 / rc={data.get('_returncode')} / "
            f"elapsed={data.get('_elapsed_sec')}秒。"
            f"stderr末尾: {(data.get('_stderr_tail') or '')[-300:]}"
        )
    # ワークフロー起動不能の明示シグナル（#4699）: dr_prompt の指示によりサブプロセスは
    # deep-research ワークフローを起動できないとき素の回答を生成せず本センチネルを返す。
    # CLI 更新でワークフローの起動インターフェースが再び変わった場合をここで確定検知する。
    # 検査は result と harvest ファイルの両方の冒頭に対して行う（センチネルは通常 result 側に
    # しか現れないため、迷いファイルが harvest されて report=file_text になった場合でも
    # すり抜けない）。本文が十分に長い（MIN_REPORT_CHARS 以上）成功ケースでは検査しない
    # （リサーチ対象や最終メッセージがセンチネル文字列に言及しただけの正常レポートを、
    # EXIT=5 で誤破棄しないため・レビュー指摘）。
    if len(report) < MIN_REPORT_CHARS:
        _sentinel = "DEEP_RESEARCH_UNAVAILABLE"
        for _src in (result_text, file_text):
            if _sentinel in _src[:500]:
                raise RuntimeError(
                    f"deep-research ワークフローを起動できない（CLI インターフェース変化の可能性）: "
                    f"{_src[:300]}。tools/run_deep_research_workflow.py の dr_prompt / "
                    "SEARCH_ALLOWED_TOOLS を現行 CLI 仕様に合わせて更新すること（#4699 と同型）。"
                )
    # 権限ブロックで途中停止していないかの簡易検知
    if re.search(r"(WebSearch|WebFetch).{0,40}(denied|許可|permission)", report) and len(report) < 1500:
        raise RuntimeError(
            "deep-research が権限不足で停止した可能性（WebSearch/WebFetch 未許可）。"
            "--allowedTools を確認すること。"
        )
    if data.get("is_error") and not report:
        raise RuntimeError(f"deep-research 実行エラー: {data.get('errors') or data}")
    # 長さガード（#2704）: 本文回収失敗を normalize 前に確定的に検知してフォールバックへ。
    # 専用例外（ReportTooShortError）で送出し、権限不足・実行エラー（RuntimeError）と EXIT を区別する。
    # 診断用に本文冒頭を含める（#4699: 「素の短文回答」か「エラー出力」かを事後ログだけで判別可能にする）。
    # elapsed は早期リターンか timeout 到達かの切り分け用（#4722）。
    if len(report) < MIN_REPORT_CHARS:
        raise ReportTooShortError(
            f"本文回収失敗: deep-research 本文が {len(report)} 文字（最低 {MIN_REPORT_CHARS} 文字必要）。"
            f"内訳 result={len(result_text)}字 / harvestファイル={len(file_text)}字（採用={source}）。"
            f"本文冒頭: 「{report[:200]}」。elapsed={data.get('_elapsed_sec')}秒 / rc={data.get('_returncode')}"
            f"（timeout={timeout}秒に対する実測）。出力欠落のためフォールバック（DIY）へ。"
        )
    return report, cost, data


def _schema_field_hint() -> str:
    """research_schema.json から正規化指示用の必須フィールド要約を組み立てる。"""
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        schema = {}
    required = schema.get("required", [])
    return (
        "JSON は以下のトップレベル必須キーを持つこと: " + ", ".join(required) + "。\n"
        "- sections: 5〜7要素。各 {heading, body_markdown(各事実に[A]/[B]/[C]と出典URL併記), source_ids[]}\n"
        "- official_names: [{term, official, japanese?, source_id}]\n"
        "- sources: 8件以上。各 {id(例 s001), url, title, rank(A|B|C), publisher?, published_at?, language?}\n"
        "- fact_check_flags: 元レポートの『未確認/未確証/⚠️』項目を必ず claim 化。各 {claim, rank, reason, source_id?}\n"
        "- metrics: {duration_seconds, cost_usd, search_count?, rank_distribution:{A,B,C}}\n"
    )


def normalize_to_schema(report_md: str, research_id: str, theme: str, model: str,
                        engine_version: str, timeout: int = 1200) -> dict:
    """引用付きレポート markdown を research_schema.json 準拠の dict に正規化する。"""
    instruction = (
        "あなたはリサーチ正規化器。次の『引用付きレポート』を、本プロジェクトの "
        "research_schema.json 準拠 JSON に変換せよ。創作・推測で事実を足さない。"
        "レポートにある出典URL・ランク・未確証フラグを忠実に写像する。\n\n"
        f"{_schema_field_hint()}\n"
        f'固定値: research_id="{research_id}", theme="{theme}", '
        f'engine="claude-deep-research-workflow", engine_version="{engine_version}", '
        f'generated_at="{_now_iso()}"。\n'
        "（engine は実態に合わせて claude-deep-research-workflow を用いる。"
        "research_schema.json の engine enum にも本値を追加済み）\n"
        "出力は **JSON のみ**（前後に説明文・コードフェンスを付けない）。\n\n"
        "=== 引用付きレポート ここから ===\n"
        f"{report_md}\n"
        "=== 引用付きレポート ここまで ==="
    )
    data = _run_claude(instruction, model, allowed_tools="", max_budget_usd=None, timeout=timeout)
    raw = (data.get("result") or "").strip()
    schema_obj = _extract_json(raw)
    if schema_obj is None:
        raise RuntimeError("正規化結果から JSON を抽出できなかった")
    # メトリクスのコストは呼び出し側で上書きする
    schema_obj.setdefault("metrics", {})
    return schema_obj


def _extract_json(text: str) -> dict | None:
    """テキストから最初の JSON オブジェクトを取り出す（コードフェンス対応）。"""
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def validate_schema(obj: dict) -> list[str]:
    """jsonschema があれば検証、無ければ最低限の手動チェック。エラー文字列のリストを返す。"""
    errors: list[str] = []
    try:
        import jsonschema  # type: ignore
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        errors = [f"{'/'.join(map(str, e.path))}: {e.message}"
                  for e in validator.iter_errors(obj)]
    except ImportError:
        for key in ("research_id", "theme", "generated_at", "engine", "sections",
                    "sources", "official_names", "fact_check_flags", "metrics"):
            if key not in obj:
                errors.append(f"必須キー欠落: {key}")
        if len(obj.get("sections", [])) < 5:
            errors.append("sections が 5 未満")
        if len(obj.get("sources", [])) < 8:
            errors.append("sources が 8 未満")
    return errors


def write_outputs(research_id: str, report_md: str, schema_obj: dict,
                  out_dir: Path | None = None, success: bool = True) -> tuple[Path, Path]:
    """成功時のみ正規ファイル名で書き出す。schema NG 時は *_workflow_failed.* に退避し、
    後段の存在判定フロー（discover）を誤起動させない。"""
    target = out_dir or RESEARCH_DIR
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{research_id}_deep_research" if success else f"{research_id}_deep_research_workflow_failed"
    md_path = target / f"{stem}.md"
    json_path = target / f"{stem}.json"
    md_path.write_text(report_md, encoding="utf-8")
    json_path.write_text(json.dumps(schema_obj, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return md_path, json_path


def log_cost(research_id: str, engine: str, cost: float, duration: float) -> None:
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    # 既存集計（run_deep_research.py）が timestamp キー前提のため timestamp で書き込む
    # （ts だと月次コスト集計・$50 サーキットブレーカーが本エンジン分を取りこぼす）
    # サブスク経路（#2562）は実課金が発生しない（週次クォータの枠内）ため、budget 集計対象の
    # cost_usd は 0.0 とし、表示用の実測トークン価値は virtual_cost_usd に分離記録する
    # （#2563 レビュー対策A）。これにより get_monthly_cost_total / month_to_date_cost が
    # 実課金（DIY）のみを集計し、$50 サーキットブレーカーが正しく機能する。
    rec = {"timestamp": _now_iso(), "research_id": research_id, "engine": engine,
           "cost_usd": 0.0 if USE_SUBSCRIPTION else round(cost, 6),
           "duration_seconds": round(duration, 1)}
    if USE_SUBSCRIPTION:
        rec["virtual_cost_usd"] = round(cost, 6)
    with COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def month_to_date_cost(engine_prefix: str = WORKFLOW_ENGINE_PREFIX) -> float:
    """当月（JST）の本エンジン累計コスト USD を research_cost_log.jsonl から集計する。
    既存集計と整合させるため timestamp キー（無ければ ts）を見る。"""
    if not COST_LOG.exists():
        return 0.0
    ym = _dt.datetime.now(JST).strftime("%Y-%m")
    total = 0.0
    for line in COST_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                continue
        except json.JSONDecodeError:
            continue
        if not str(rec.get("engine") or "").startswith(engine_prefix):
            continue
        ts = str(rec.get("timestamp") or rec.get("ts") or "")
        if ts[:7] == ym:
            try:
                total += float(rec.get("cost_usd") or 0.0)
            except (ValueError, TypeError):
                pass
    return total


def _theme_from_prompt(prompt: str, research_id: str) -> str:
    m = re.search(r"\*\*テーマ\*\*[:：]\s*(.+)", prompt)
    if m:
        return m.group(1).strip()
    m = re.search(r"^#\s*(.+)", prompt, re.MULTILINE)
    if m:
        # load_prompt と同じくサフィックスを除去してテーマ表記を揃える
        return m.group(1).replace("Deep Research プロンプト", "").strip(" 　-") or f"{research_id} research"
    return f"{research_id} research"


def _self_test() -> int:
    """_is_rate_limited のレート枠超過検出を検証する（#2814・SKILL.md の skip 判定の土台）。"""
    positives = [
        {"_stderr_tail": "Claude usage limit reached. resets at 1780822800"},
        {"result": "Error: rate_limit exceeded for five_hour window"},
        {"errors": "429 Too Many Requests"},
        {"_stderr_tail": "overageStatus: rejected (org_level_disabled)"},
        {"result": "5-hour limit reached, please try again later"},
        {"errors": "quota exhausted"},
    ]
    negatives = [
        {},
        {"_stderr_tail": "schema validation error: sections < 5"},
        {"result": "deep research completed successfully with 22 sources"},
        {"errors": "normalize 失敗: JSON を抽出できなかった"},
        # 誤検知防止の回帰テスト（Gemini レビュー #2815）:
        {"_stderr_tail": "RecursionError: recursion limit reached"},   # 実行エラー（EXIT=5）→ 誤って 6 にしない
        {"errors": "depth limit reached during traversal"},            # 同上
        {"result": "research about model V429 failed: schema invalid"},  # 動画ID/数値の 429 に誤マッチしない
    ]
    failures = 0
    for case in positives:
        if not _is_rate_limited(case):
            print(f"  ❌ FALSE NEGATIVE: {case}", file=sys.stderr)
            failures += 1
    for case in negatives:
        if _is_rate_limited(case):
            print(f"  ❌ FALSE POSITIVE: {case}", file=sys.stderr)
            failures += 1
    if failures:
        print(f"❌ self-test FAILED: {failures} 件", file=sys.stderr)
        return 1
    print(f"✅ self-test PASS: positive {len(positives)} / negative {len(negatives)} 件すべて期待通り")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ネイティブ /deep-research を engine 化（ハイブリッド）")
    ap.add_argument("research_id", nargs="?", help="リサーチ ID（任意の slug・例: my-topic）。--self-test 時は省略可")
    ap.add_argument("--engine-model", default=DEFAULT_ENGINE_MODEL)
    ap.add_argument("--normalize-model", default=DEFAULT_NORMALIZE_MODEL)
    ap.add_argument("--max-budget-usd", type=float, default=DEFAULT_MAX_BUDGET_USD,
                    help=f"検索サブプロセスの予算上限（既定: ${DEFAULT_MAX_BUDGET_USD}）。"
                         "検証で無制限にしたい場合は大きい値を明示指定する")
    ap.add_argument("--force", action="store_true",
                    help=f"当月累計が ${MONTHLY_BUDGET_GATE_USD} を超えていても強制実行する")
    ap.add_argument("--normalize-only", action="store_true",
                    help="検索をスキップし --report のレポートを正規化のみ行う")
    ap.add_argument("--report", help="--normalize-only 時の入力レポート md パス")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC,
                    help=f"検索サブプロセスの timeout 秒（既定 {DEFAULT_TIMEOUT_SEC}・"
                         "DEEP_RESEARCH_TIMEOUT_SEC で上書き可）")
    ap.add_argument("--out-dir", default=None,
                    help="出力先ディレクトリ（既定: content/research/）。検証時の上書き回避に使う")
    ap.add_argument("--extra-allowed-tools", default=None,
                    help="検索サブプロセスに追加許可するツール（既定の SEARCH_ALLOWED_TOOLS に追記）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="レート枠超過検出（_is_rate_limited）の単体テストを実行して終了する（#2814）")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    # research_id を nargs="?" にしたため（--self-test 単体実行を可能にする）、通常実行では
    # 未指定を明示的にエラーにする（後段で None が流れて壊れるのを防ぐ・Copilot レビュー #2815）
    if not args.research_id:
        ap.error("research_id は必須です（--self-test 実行時を除く）")
    out_dir = Path(args.out_dir) if args.out_dir else None
    allowed_tools = SEARCH_ALLOWED_TOOLS + (
        f" {args.extra_allowed_tools}" if args.extra_allowed_tools else "")

    vid = args.research_id
    prompt = read_prompt(vid) if not args.normalize_only else ""
    theme = _theme_from_prompt(prompt, vid) if prompt else f"{vid} research"

    if args.dry_run:
        # 実行時と同じビルダーで組み立てた実プロンプトの先頭をプレビューする（desync 防止・#4699）
        dr = f"{_build_dr_prompt(prompt)[:400]}..."
        print("[dry-run] 検索:", ["claude", "-p", dr, "--model", args.engine_model,
              "--output-format", "json", "--allowedTools", allowed_tools,
              *(["--max-budget-usd", str(args.max_budget_usd)]
                if args.max_budget_usd is not None and not USE_SUBSCRIPTION else [])])
        print("[dry-run] 正規化モデル:", args.normalize_model)
        return 0

    t0 = time.perf_counter()  # 単調増加クロックで経過時間を計測（システム時刻変更の影響を受けない）
    cost = 0.0
    raw_path: Path | None = None  # 喪失防止チェックポイント（検索成功時のみ設定）
    if args.normalize_only:
        if not args.report:
            print("ERROR: --normalize-only には --report が必要", file=sys.stderr)
            return 2
        report_md = Path(args.report).read_text(encoding="utf-8")
        if not theme or theme.endswith("research"):
            m = re.search(r"^#\s*(.+)", report_md, re.MULTILINE)
            if m:
                theme = m.group(1).strip()
    else:
        if USE_SUBSCRIPTION:
            # サブスク経路（#2562・#2563 対策C）: /deep-research は週次クォータの枠内で実行され
            # 追加 $ 課金が発生しないため、月次予算ゲートはバイパスする（サブスク実行は常に無料）。
            # 実課金（DIY フォールバック）に対するブレーカーは run_deep_research.py 側で維持。
            print("[0/3] サブスク経路（追加$ゼロ・週次クォータ枠内）のため月次予算ゲートをバイパス")
        else:
            # 月次予算チェック（#2394/#2411）。高コストエンジンの $50 枠圧迫を防ぐ（API 従量経路時）
            # ① プロジェクト全体（全エンジン）の月次上限 $50 をまず確認（run_deep_research.py と整合）
            total_mtd = month_to_date_cost(engine_prefix="")
            if total_mtd > MONTHLY_BUDGET_LIMIT_USD and not args.force:
                print(f"ERROR: 当月のリサーチ全体累計が ${total_mtd:.2f}（上限 ${MONTHLY_BUDGET_LIMIT_USD}）を超過。"
                      "--force で強制実行可。", file=sys.stderr)
                return 3
            # ② /deep-research 単体ゲート（超過→DIY フォールバック）
            mtd = month_to_date_cost()
            if mtd > MONTHLY_BUDGET_GATE_USD and not args.force:
                print(f"ERROR: 当月の /deep-research 累計が ${mtd:.2f}（ゲート ${MONTHLY_BUDGET_GATE_USD}）を超過。"
                      "DIY を使うか、--force で強制実行。", file=sys.stderr)
                return 3
            print(f"[0/3] 当月累計 /deep-research ${mtd:.2f}/ゲート${MONTHLY_BUDGET_GATE_USD} ・全体 ${total_mtd:.2f}/上限${MONTHLY_BUDGET_LIMIT_USD}")
        # サブスク/API 両経路で共通: ネイティブ /deep-research を実行する
        budget_note = "サブスク認証" if USE_SUBSCRIPTION else f"上限 ${args.max_budget_usd}"
        print(f"[1/3] ネイティブ /deep-research 実行中（model={args.engine_model}・{budget_note}）...")
        try:
            report_md, cost, meta = run_deep_research(
                prompt, args.engine_model, args.max_budget_usd, args.timeout,
                allowed_tools=allowed_tools)
        except RateLimitedError as exc:
            # レート枠超過（capacity）→ EXIT=6（#2814）。DIY へ即フォールバックせず、
            # research-runner が「スキップ→次スロットで claude -p 再試行」と解釈する終了コード。
            # 連続3回スキップで初めて DIY（SKILL.md Step 3.6 が skip カウンタで制御）。
            _log_engine_failure(vid, 6, f"RateLimited: {exc}")
            print(f"SKIP: claude -p レート枠超過のためスキップ（EXIT=6・DIY に落とさず次スロット再試行）: {exc}",
                  file=sys.stderr)
            return 6
        except ReportTooShortError as exc:
            # 本文回収失敗（出力欠落・長さガード）→ EXIT=4（#2704）。SKILL.md の終了コードと一致させる
            _log_engine_failure(vid, 4, f"ReportTooShort: {exc}")
            print(f"ERROR: deep-research 本文回収失敗（DIY フォールバックへ）: {exc}", file=sys.stderr)
            return 4
        except RuntimeError as exc:
            # 権限不足・サブプロセス実行エラー等 → EXIT=5（本文回収失敗=4 と区別して原因切り分け可能に）
            _log_engine_failure(vid, 5, f"RuntimeError: {exc}")
            print(f"ERROR: deep-research 実行失敗（DIY フォールバックへ）: {exc}", file=sys.stderr)
            return 5
        print(f"      取得: {len(report_md)} 文字 / cost=${cost:.3f} / source={meta.get('_report_source')}"
              f"（result={meta.get('_result_len')}字 / file={meta.get('_file_len')}字）")
        # ★喪失防止チェックポイント（#2704）: 高コストな normalize の前に生レポートを即ディスク保存する。
        # normalize 失敗やセッション切れが起きても 34分/$20 の検索成果を失わず、--normalize-only で再正規化できる。
        # lint（_deep_research*.md）・sync（_deep_research.*）の glob に当たらない _research_raw.md を使う。
        raw_path = (out_dir or RESEARCH_DIR) / f"{vid}_research_raw.md"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(report_md, encoding="utf-8")
        print(f"[1.5/3] 喪失防止: 生レポートを {raw_path} に保存（normalize 前・research-runner が即コミット）")

    print(f"[2/3] research_schema へ正規化中（model={args.normalize_model}）...")
    try:
        schema_obj = normalize_to_schema(
            report_md, vid, theme, args.normalize_model,
            engine_version=f"deep-research-workflow/{args.engine_model}")
    except RuntimeError as exc:
        # normalize（JSON 抽出）失敗。生レポートは raw_path に保存済みのため喪失しない（#2704・V183 で全損した事故の根治）
        # --normalize-only（手動再正規化）では検索サブプロセスが走っておらず、fallback ログに
        # 「主エンジン失敗」として記録すると反復検知（EXIT=4/5 2連続→type:bug 起票）を汚すため記録しない
        if not args.normalize_only:
            _log_engine_failure(vid, 1, f"NormalizeFailed: {exc}")
        print(f"⚠️ normalize 失敗: {exc}", file=sys.stderr)
        if raw_path is not None:
            print(f"   生レポートは {raw_path} に保存済み。"
                  f"`--normalize-only --report {raw_path}` で再検索なしに再正規化できる。", file=sys.stderr)
        return 1
    schema_obj.setdefault("metrics", {})
    duration = time.perf_counter() - t0
    schema_obj["metrics"]["cost_usd"] = round(cost, 6)
    schema_obj["metrics"]["duration_seconds"] = round(duration, 1)

    errors = validate_schema(schema_obj)
    # schema NG 時は正規ファイル名で書かず *_workflow_failed.* に退避（discover の誤起動防止）
    md_path, json_path = write_outputs(vid, report_md, schema_obj, out_dir,
                                       success=not errors)
    log_cost(vid, f"deep-research-workflow/{args.engine_model}", cost, duration)

    print(f"[3/3] 出力: {md_path}\n          {json_path}")
    if errors:
        # schema 検証 NG も EXIT=1 の失敗経路（「EXIT=1/4/5/6 は必ず記録」の抜け漏れ防止・レビュー指摘）。
        # --normalize-only（手動再正規化）は normalize 失敗時と同じ理由で記録しない
        if not args.normalize_only:
            _log_engine_failure(vid, 1, f"SchemaInvalid: {'; '.join(errors[:5])}")
        print("⚠️ schema 検証エラー（正規ファイルは生成せず *_workflow_failed.* に退避）:", file=sys.stderr)
        for e in errors[:20]:
            print(f"  - {e}", file=sys.stderr)
        print("→ tools/lint_research_files.py で再確認のこと", file=sys.stderr)
        return 1
    # 正本（_deep_research.md/.json）が揃ったので喪失防止用の生ファイルは破棄する（#2704）
    if raw_path is not None:
        raw_path.unlink(missing_ok=True)
    print("✅ schema 検証 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
