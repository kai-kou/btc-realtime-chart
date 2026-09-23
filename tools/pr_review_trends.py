#!/usr/bin/env python3
"""pr_review_trends.py — PR レビュー週次トレンド蓄積 + グラフ + Slack 投稿（Issue #2905）

「過去 PR を分析してセルフレビューを強化する仕組みが機能しているか」を週次で可視化する。
`analyze_pr_review_comments.py`（全期間累積・チェックシート反映判断材料）とは役割分担:

  - analyze_pr_review_comments.py … 全期間累積スナップショット（docs/analysis/pr_review_stats_*.json）
  - pr_review_trends.py（本ツール） … 週次フローの時系列（content/analytics/pr_review_trends.jsonl）

severity / AI レビュアー判定ロジックは analyze_pr_review_comments.py を import 再利用（SSOT）。

## 指標設計（専門チーム @reviewer_a レビュー反映・Issue #2905）

セルフレビューの「効き」を正しく測るため、全指標を **PR のマージ週でバケット化** し、
分母は **週内マージ全 PR 数**（指摘ゼロ PR も含む）とする。コメントの created_at では
バケットしない（UTC/JST 週境界のズレ・後追いレビューの混入を避けるため）。

  - 主指標: 指摘ゼロ PR 率 = 指摘ゼロでマージされた PR 数 / 週内マージ全 PR 数（右肩上がりが善）
  - 補助: PR あたり AI 指摘数（reviewer 別: layer1 = Layer 1 セルフレビューのテンプレート本文で判定・
          #627 対策 E。廃止済みの Gemini / Copilot は --legacy-reviewers を付けたときだけ旧系列として保持）
  - 補助: 週次マージ PR 数（母数）/ severity 別 / 修正数(proxy=AI 指摘の付いた PR 数)
  - 直近週は provisional（レビューは後から増えるため確定は週末から 8 日以上経過後）

Usage:
    python3 tools/pr_review_trends.py --update            # JSONL を週次更新（冪等）
    python3 tools/pr_review_trends.py --render            # JSONL から PNG を生成（ローカル目視用）
    python3 tools/pr_review_trends.py --notify            # 既存 PNG を R2 へ上げ Slack 投稿
    python3 tools/pr_review_trends.py --all               # update → render → notify を一括
    python3 tools/pr_review_trends.py --weeks 16 --all    # 集計対象を直近 16 週に
    python3 tools/pr_review_trends.py --self-test         # 内蔵フィクスチャで検証
    python3 tools/pr_review_trends.py --update --legacy-reviewers  # 旧 Gemini / Copilot コメントも加算

実行タイミング: 毎週月曜 07:00 スロット ⑤.7（{プロジェクト定義: hourly-routing 相当}）
Exit code: 0 = 正常 / 1 = 失敗
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

JST = timezone(timedelta(hours=9))
REPO = resolve_repo_slug("kai-kou/btc-realtime-chart")
ROOT = Path(__file__).resolve().parent.parent
TRENDS_PATH = ROOT / "content" / "analytics" / "pr_review_trends.jsonl"
RENDER_PATH = ROOT / "content" / "analytics" / "pr_review_trends.png"
# matplotlib で日本語が豆腐化しないよう登録するフォント（既存 compose_channel_banner.py と同じ実績パス）
JP_FONT = "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"
# pull_request_url から PR 番号を抽出する正規表現（モジュールレベルで 1 度だけコンパイル）
_PR_RE = re.compile(r"/pulls/(\d+)")

# analyze_pr_review_comments.py のロジックを再利用（SSOT・同 tools/ ディレクトリ・sys.path は上で追加済み）
try:
    from analyze_pr_review_comments import (  # noqa: E402
        fetch_comments, parse_concatenated_json, severity_of, reviewer_key,
    )
except ImportError:  # 単体テスト・分離実行時のフォールバック（--self-test は import 不要で動く）
    fetch_comments = parse_concatenated_json = severity_of = reviewer_key = None

# reviewer 別系列のキー。layer1 が現行（#627 対策 E）、gemini / copilot は廃止済み外部レビュアーの旧系列
REVIEWER_KEYS = ("layer1", "gemini", "copilot")


# ─────────────────────────── 週境界ユーティリティ ───────────────────────────

def iso_week_label(d: date) -> str:
    """ISO 週ラベル（例: 2026-W24）を返す。"""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(d: date) -> "tuple[date, date]":
    """日付 d を含む ISO 週（月曜〜日曜）の (月曜, 日曜) を返す。"""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def recent_weeks(n: int, today: "date | None" = None) -> "list[tuple[str, date, date]]":
    """直近 n 週（古い順）の (iso_week, week_start, week_end) を返す。最新要素が当該週。"""
    today = today or datetime.now(JST).date()
    this_monday, _ = week_bounds(today)
    weeks = []
    for i in range(n - 1, -1, -1):
        monday = this_monday - timedelta(weeks=i)
        weeks.append((iso_week_label(monday), monday, monday + timedelta(days=6)))
    return weeks


def is_provisional(week_end: date, today: "date | None" = None) -> bool:
    """確定週か暫定週かを判定する。週末から 8 日未満なら暫定（レビューが後から増えるため）。"""
    today = today or datetime.now(JST).date()
    return (today - week_end).days < 8


# ─────────────────────────── GitHub データ取得 ───────────────────────────

def _segment_of(title: str) -> str:
    """PR タイトルからセグメントを判定（content=動画制作 / maintenance=保守）。

    自動生成・保守系 PR（[daily] state 更新・[wip] 圧縮・docs/fix 等）はレビュー指摘が
    ほぼ付かないため、混在させると指摘ゼロ率が水増しされる（@reviewer_a 交絡指摘）。
    """
    t = (title or "").lstrip()
    if t.startswith("[V"):
        return "content"
    return "maintenance"


def fetch_merged_prs(week_start: date, week_end: date) -> "list[dict] | None":
    """指定週にマージされた PR を gh search で取得する。失敗時は None（縮退記録用）。"""
    rng = f"{week_start.isoformat()}..{week_end.isoformat()}"
    cmd = [
        "gh", "search", "prs", "--repo", REPO, "--merged-at", rng,
        "--limit", "300", "--json", "number,title",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             check=True, timeout=120).stdout
        data = json.loads(out)
        return data if isinstance(data, list) else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError) as e:
        print(f"WARN: 週 {rng} のマージ PR 取得に失敗: {e}", file=sys.stderr)
        return None


def build_ai_index(comments: list, legacy: bool = False) -> dict:
    """全 inline コメントから PR 番号 → AI 指摘情報のインデックスを作る。

    判定は analyze_pr_review_comments.reviewer_key（既定: Layer 1 テンプレート本文。legacy=True で
    旧 Gemini / Copilot のログイン名も）に委ねる（SSOT・#627 対策 E）。
    返り値: {pr_number(int): {"count":N, "by_reviewer":{...}, "severity":{...}}}
    """
    index: dict = defaultdict(lambda: {"count": 0, "by_reviewer": Counter(), "severity": Counter()})
    for c in comments:
        if not isinstance(c, dict):
            continue
        login = (c.get("user") or {}).get("login", "")
        body = c.get("body") or ""
        rkey = reviewer_key(login, body, legacy)
        if rkey is None:
            continue
        m = _PR_RE.search(c.get("pull_request_url") or "")
        if not m:
            continue
        pr = int(m.group(1))
        entry = index[pr]
        entry["count"] += 1
        entry["by_reviewer"][rkey] += 1
        sev = severity_of(body)
        if sev:
            entry["severity"][sev] += 1
    return index


def aggregate_week(merged: "list[dict]", ai_index: dict) -> dict:
    """週内マージ PR 群と AI 指摘インデックスから週次メトリクスを計算する。"""
    prs_merged = len(merged)
    with_ai = 0
    ai_comments = 0
    by_reviewer = Counter()
    severity = Counter()
    seg_merged = Counter()
    seg_with_ai = Counter()
    for pr in merged:
        num = pr.get("number")
        seg = _segment_of(pr.get("title", ""))
        seg_merged[seg] += 1
        entry = ai_index.get(num)
        if entry and entry["count"] > 0:
            with_ai += 1
            seg_with_ai[seg] += 1
            ai_comments += entry["count"]
            by_reviewer.update(entry["by_reviewer"])
            severity.update(entry["severity"])
    zero = prs_merged - with_ai

    def _rate(num_zero: int, denom: int) -> "float | None":
        return round(num_zero / denom, 4) if denom else None

    content_merged = seg_merged.get("content", 0)
    content_zero = content_merged - seg_with_ai.get("content", 0)
    return {
        "prs_merged": prs_merged,
        "prs_with_ai_comments": with_ai,
        "zero_comment_prs": zero,
        "zero_comment_pr_rate": _rate(zero, prs_merged),
        "ai_comments": ai_comments,
        "ai_by_reviewer": dict(by_reviewer),
        "comments_per_pr": round(ai_comments / prs_merged, 3) if prs_merged else None,
        # layer1 は常に載せる。廃止済みの旧系列（gemini / copilot）は実データがある週だけ載せる
        # （常に 0.0 を保存すると描画側の「値がある週だけ旧系列を描く」判定が無効になる・#627 PR-2 レビュー指摘）
        "comments_per_pr_by_reviewer": {
            r: round(by_reviewer.get(r, 0) / prs_merged, 3)
            for r in REVIEWER_KEYS if r == "layer1" or by_reviewer.get(r)
        } if prs_merged else {},
        "severity": dict(severity),
        # 修正数 proxy: AI 指摘が付いて（=修正を要し）マージされた PR 数。
        # 厳密な resolved thread 数は GraphQL が必要なため後続フェーズ（@reviewer_a 助言）。
        "fixes_proxy": with_ai,
        "content": {
            "prs_merged": content_merged,
            "zero_comment_pr_rate": _rate(content_zero, content_merged),
        },
    }


# ─────────────────────────── JSONL 蓄積（冪等） ───────────────────────────

def load_trends() -> "dict[str, dict]":
    """既存 JSONL を {iso_week: row} で読み込む（冪等更新のため）。"""
    rows: dict = {}
    if not TRENDS_PATH.exists():
        return rows
    for line in TRENDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("iso_week"):
                rows[obj["iso_week"]] = obj
        except json.JSONDecodeError:
            continue
    return rows


def save_trends(rows: "dict[str, dict]") -> None:
    """{iso_week: row} を週キー昇順で JSONL に書き出す。"""
    TRENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = [rows[k] for k in sorted(rows.keys())]
    TRENDS_PATH.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered),
        encoding="utf-8",
    )


def update_trends(weeks: int, today: "date | None" = None, legacy: bool = False) -> "dict[str, dict]":
    """直近 weeks 週のメトリクスを取得し JSONL を冪等更新する（legacy=True で旧 Gemini / Copilot も加算）。"""
    if fetch_comments is None:
        print("ERROR: analyze_pr_review_comments の import に失敗（同 tools/ に存在するか確認）",
              file=sys.stderr)
        sys.exit(1)
    print("[trends] gh api --paginate で全レビューコメントを取得中（数分かかる）...",
          file=sys.stderr)
    comments = parse_concatenated_json(fetch_comments())
    ai_index = build_ai_index(comments, legacy)

    rows = load_trends()
    for iso, w_start, w_end in recent_weeks(weeks, today):
        merged = fetch_merged_prs(w_start, w_end)
        prov = is_provisional(w_end, today)
        if merged is None:
            # 取得失敗。既存の確定行は温存し、新規週のみ縮退記録する。
            if iso in rows and not rows[iso].get("provisional", True):
                continue
            rows[iso] = {
                "iso_week": iso, "week_start": w_start.isoformat(),
                "week_end": w_end.isoformat(), "provisional": prov,
                "prs_merged": None, "note": "gh search 失敗",
                "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            }
            continue
        metrics = aggregate_week(merged, ai_index)
        rows[iso] = {
            "iso_week": iso,
            "week_start": w_start.isoformat(),
            "week_end": w_end.isoformat(),
            "provisional": prov,
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            **metrics,
        }
        flag = "（暫定）" if prov else ""
        rate = metrics["zero_comment_pr_rate"]
        rate_str = f" / 指摘ゼロ率 {rate*100:.0f}%" if rate is not None else ""
        print(f"[trends] {iso}{flag}: PR {metrics['prs_merged']}{rate_str}", file=sys.stderr)
    save_trends(rows)
    print(f"[trends] JSONL 更新: {TRENDS_PATH}", file=sys.stderr)
    return rows


# ─────────────────────────── グラフ描画 ───────────────────────────

def _moving_avg(values: "list[float | None]", window: int = 4) -> "list[float | None]":
    out = []
    for i in range(len(values)):
        win = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(round(sum(win) / len(win), 4) if win else None)
    return out


def _rows_sorted(rows: "dict[str, dict]") -> list:
    return [rows[k] for k in sorted(rows.keys())]


def _reviewer_series(data: list) -> "dict[str, list]":
    """描画する reviewer 別系列。layer1 は常に描き、廃止済みの旧系列（gemini / copilot）は
    非ゼロの値がある週が 1 つでもあるときだけ描く（0.0 だけのフラットラインを出さない）。
    matplotlib 不在環境でも検証できるよう描画本体から分離してある（#627 PR-2 レビュー指摘）。"""
    series = {}
    for key in REVIEWER_KEYS:
        vals = [(r.get("comments_per_pr_by_reviewer") or {}).get(key) for r in data]
        if key == "layer1" or any(vals):
            series[key] = vals
    return series


def render_matplotlib(rows: "dict[str, dict]", out_path: Path) -> bool:
    """matplotlib で週次トレンドグラフ（3 パネル）を描画する。成功で True。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return False
    try:
        if os.path.exists(JP_FONT):
            font_manager.fontManager.addfont(JP_FONT)
            matplotlib.rcParams["font.family"] = font_manager.FontProperties(
                fname=JP_FONT).get_name()
        matplotlib.rcParams["axes.unicode_minus"] = False

        data = _rows_sorted(rows)
        if not data:
            return False
        labels = [r["iso_week"].replace("-W", "\nW") for r in data]
        zero_rate = [(r.get("zero_comment_pr_rate") or 0) * 100 if r.get("zero_comment_pr_rate") is not None else None for r in data]
        series = _reviewer_series(data)
        prs = [r.get("prs_merged") or 0 for r in data]
        ai = [r.get("ai_comments") or 0 for r in data]
        x = list(range(len(data)))

        fig, axes = plt.subplots(3, 1, figsize=(11, 12))
        fig.suptitle("PR レビュー週次トレンド（セルフレビュー有効性）", fontsize=15, fontweight="bold")

        # パネル1: 指摘ゼロ PR 率（主指標・0-100% 固定・4週移動平均）
        ax = axes[0]
        ax.plot(x, [v if v is not None else float("nan") for v in zero_rate],
                marker="o", color="#2e7d32", label="指摘ゼロPR率")
        ma = _moving_avg(zero_rate)
        ax.plot(x, [v if v is not None else float("nan") for v in ma],
                linestyle="--", color="#81c784", label="4週移動平均")
        ax.set_ylim(0, 100)
        ax.set_ylabel("指摘ゼロPR率 (%)")
        ax.set_title("① 指摘ゼロでマージされたPRの割合（右肩上がり = セルフレビューが効いている）")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

        # パネル2: PR あたり指摘数（Layer 1 が主系列。旧 Gemini / Copilot は _reviewer_series が選んだときだけ描く）
        ax = axes[1]
        styles = {"layer1": ("o", "#6a1b9a", "Layer 1 /PR"), "gemini": ("s", "#1565c0", "Gemini /PR（旧）"),
                  "copilot": ("^", "#ef6c00", "Copilot /PR（旧）")}
        for key, vals in series.items():
            marker, color, label = styles[key]
            ax.plot(x, [v if v is not None else float("nan") for v in vals], marker=marker, color=color, label=label)
        ax.set_ylabel("PRあたり指摘数")
        ax.set_title("② PRあたりAI指摘数（Layer 1 セルフレビュー・低いほど良い／Gemini・Copilot は廃止済みの旧系列）")
        ax.legend(loc="upper right", fontsize=9)
        ax.grid(True, alpha=0.3)

        # パネル3: 週次マージ PR 数（母数）+ AI 指摘総数
        ax = axes[2]
        ax.bar(x, prs, color="#b0bec5", label="週次マージPR数（母数）")
        ax2 = ax.twinx()
        ax2.plot(x, ai, marker="o", color="#c62828", label="AI指摘総数")
        ax.set_ylabel("マージPR数")
        ax2.set_ylabel("AI指摘総数")
        ax.set_title("③ 母数（週次マージPR数）と AI指摘総数")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=8)
        lines1, lab1 = ax.get_legend_handles_labels()
        lines2, lab2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, lab1 + lab2, loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

        for a in axes[:2]:
            a.set_xticks(x)
            a.set_xticklabels(labels, fontsize=8)

        fig.tight_layout(rect=(0, 0, 1, 0.97))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=110, bbox_inches="tight")
        plt.close(fig)
        return out_path.exists()
    except Exception as e:  # 描画失敗は致命にしない（縮退）
        print(f"WARN: matplotlib 描画失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def render_pillow(rows: "dict[str, dict]", out_path: Path) -> bool:
    """Pillow フォールバック: 指摘ゼロ PR 率の簡易バーチャートを矩形描画する。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        data = _rows_sorted(rows)
        if not data:
            return False
        try:
            font = ImageFont.truetype(JP_FONT, 14)
            tfont = ImageFont.truetype(JP_FONT, 18)
        except Exception:
            font = tfont = ImageFont.load_default()
        W, H = 980, 420
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        d.text((20, 14), "PR レビュー週次トレンド — 指摘ゼロPR率（右肩上がり=改善）", fill="black", font=tfont)
        base_y, max_h, bw, gap = 360, 280, 46, 14
        x0 = 40
        for i, r in enumerate(data):
            rate = r.get("zero_comment_pr_rate")
            h = int((rate or 0) * max_h)
            x = x0 + i * (bw + gap)
            color = "#bdbdbd" if rate is None else "#2e7d32"
            d.rectangle([x, base_y - h, x + bw, base_y], fill=color)
            pct = "-" if rate is None else f"{rate*100:.0f}%"
            d.text((x, base_y - h - 18), pct, fill="black", font=font)
            d.text((x, base_y + 6), r["iso_week"][-3:], fill="black", font=font)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return out_path.exists()
    except Exception as e:
        print(f"WARN: Pillow 描画失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def render_chart(rows: "dict[str, dict]", out_path: Path = RENDER_PATH) -> "Path | None":
    """matplotlib → Pillow の順でグラフを生成する。両方失敗なら None（テキスト縮退）。"""
    if render_matplotlib(rows, out_path):
        print(f"[trends] グラフ生成（matplotlib）: {out_path}", file=sys.stderr)
        return out_path
    if render_pillow(rows, out_path):
        print(f"[trends] グラフ生成（Pillow フォールバック）: {out_path}", file=sys.stderr)
        return out_path
    print("WARN: matplotlib / Pillow とも利用不可。グラフ画像なしでテキスト縮退します。",
          file=sys.stderr)
    return None


# ─────────────────────────── サマリーテキスト ───────────────────────────

def build_summary(rows: "dict[str, dict]") -> str:
    """Slack 投稿用のサマリーテキスト（最新確定週と前週比）を作る。"""
    data = [r for r in _rows_sorted(rows) if r.get("prs_merged") is not None]
    if not data:
        return "週次データがまだありません。"
    latest = data[-1]
    prev = data[-2] if len(data) >= 2 else None

    def _pct(r):
        v = r.get("zero_comment_pr_rate")
        return f"{v*100:.0f}%" if v is not None else "-"

    lines = [f"*最新週 {latest['iso_week']}*" + ("（暫定）" if latest.get("provisional") else "")]
    lines.append(f"• マージPR数: {latest.get('prs_merged')} 件")
    by_rev = latest.get("ai_by_reviewer", {}) or {}
    legacy_note = (f" / 旧 Gemini {by_rev.get('gemini', 0)} / 旧 Copilot {by_rev.get('copilot', 0)}"
                   if by_rev.get("gemini") or by_rev.get("copilot") else "")
    lines.append(f"• AIレビュー指摘数: {latest.get('ai_comments')} 件"
                 f"（Layer 1 {by_rev.get('layer1', 0)}{legacy_note}）")
    lines.append(f"• 修正を要したPR数(修正数proxy): {latest.get('fixes_proxy')} 件")
    cur_rate = _pct(latest)
    if prev:
        delta = None
        if latest.get("zero_comment_pr_rate") is not None and prev.get("zero_comment_pr_rate") is not None:
            delta = (latest["zero_comment_pr_rate"] - prev["zero_comment_pr_rate"]) * 100
        arrow = "" if delta is None else (f"（前週比 {'+' if delta>=0 else ''}{delta:.0f}pt）")
        lines.append(f"• *指摘ゼロPR率*: {_pct(prev)} → *{cur_rate}* {arrow}")
    else:
        lines.append(f"• *指摘ゼロPR率*: {cur_rate}")
    lines.append("")
    lines.append("_指摘ゼロPR率が右肩上がりなら、セルフレビューが事前に問題を潰せている。_")
    return "\n".join(lines)


# ─────────────────────────── R2 アップロード + Slack ───────────────────────────

def upload_to_r2(png_path: Path) -> "str | None":
    """PNG を R2 画像バケットへアップロードし公開 URL を返す（boto3・r2_media 再利用）。"""
    public_domain = os.environ.get("R2_PUBLIC_DOMAIN_IMAGES", "")
    if not public_domain:
        print("WARN: R2_PUBLIC_DOMAIN_IMAGES 未設定。Slack 画像表示をスキップ（テキストのみ）。",
              file=sys.stderr)
        return None
    try:
        from r2_media import _get_r2_client, _get_images_bucket_name
    except ImportError as e:
        print(f"WARN: r2_media import 失敗: {e}", file=sys.stderr)
        return None
    try:
        client = _get_r2_client()
        bucket = _get_images_bucket_name()
        stamp = datetime.now(JST).strftime("%Y-%m-%d")
        key = f"reports/pr_review_trends_{stamp}.png"
        client.upload_file(str(png_path), bucket, key,
                           ExtraArgs={"ContentType": "image/png"})
        url = f"https://{public_domain}/{key}"
        print(f"[trends] R2 アップロード完了: {url}", file=sys.stderr)
        return url
    except SystemExit:
        # _get_r2_client は環境変数不足時 sys.exit(1) する。トレンド全体を落とさず縮退。
        print("WARN: R2 環境変数不足。Slack 画像表示をスキップ（テキストのみ）。", file=sys.stderr)
        return None
    except Exception as e:
        print(f"WARN: R2 アップロード失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def notify_slack(image_url: "str | None", summary: str) -> bool:
    """slack_notify.py chart で Slack へ投稿する（FYI・@mention なし）。"""
    title = "📊 PRレビュー週次トレンド（セルフレビュー有効性）"
    cmd = ["python3", str(ROOT / "tools" / "slack_notify.py"), "chart",
           "--title", title, "--text", summary, "--alt-text", "PRレビュー週次トレンドグラフ"]
    if image_url:
        cmd += ["--image-url", image_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        if r.returncode == 0:
            print("[trends] Slack 投稿完了", file=sys.stderr)
            return True
        print(f"WARN: Slack 投稿失敗: {r.stderr.strip()}", file=sys.stderr)
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARN: Slack 投稿エラー: {e}", file=sys.stderr)
        return False


# ─────────────────────────── 悪化検知 → 改善 Issue 自動起票 ───────────────────────────

ISSUE_MARKER = "[auto] PRレビュー週次トレンド悪化検知"


def check_regression_and_file_issue(rows: "dict[str, dict]") -> "str | None":
    """確定週のトレンド悪化を検知したら type:improvement Issue を起票する（@owner 助言）。

    起票条件（いずれか）: 指摘ゼロPR率が前確定週比 -10pt 超 / PRあたり指摘数が前週比 +30% 超。
    既に同マーカーのオープン Issue があれば二重起票しない。
    """
    confirmed = [r for r in _rows_sorted(rows)
                 if not r.get("provisional") and r.get("prs_merged")]
    if len(confirmed) < 2:
        return None
    latest, prev = confirmed[-1], confirmed[-2]
    reasons = []
    lz, pz = latest.get("zero_comment_pr_rate"), prev.get("zero_comment_pr_rate")
    if lz is not None and pz is not None and (lz - pz) * 100 <= -10:
        reasons.append(f"指摘ゼロPR率が {pz*100:.0f}% → {lz*100:.0f}%（{(lz-pz)*100:.0f}pt）に低下")
    lc, pc = latest.get("comments_per_pr"), prev.get("comments_per_pr")
    if lc is not None and pc not in (None, 0) and lc > pc * 1.3:
        reasons.append(f"PRあたり指摘数が {pc} → {lc}（+{(lc/pc-1)*100:.0f}%）に増加")
    if not reasons:
        return None

    # 二重起票防止
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "-R", REPO, "--state", "open",
             "--search", ISSUE_MARKER, "--json", "number", "--limit", "1000"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            check=True).stdout
        if json.loads(out or "[]"):
            print("[trends] 既存の悪化検知 Issue があるため起票をスキップ", file=sys.stderr)
            return None
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        # 重複チェックに失敗しても起票自体は続行する（二重起票リスクは許容）。握りつぶさず記録する。
        print(f"WARN: 既存 Issue 確認に失敗（起票は続行）: {e}", file=sys.stderr)

    body = (
        f"## {ISSUE_MARKER}\n\n"
        f"週次トレンド（`content/analytics/pr_review_trends.jsonl`）でセルフレビュー有効性の"
        f"悪化を検知しました。\n\n### 検知内容\n"
        + "".join(f"- {r}\n" for r in reasons)
        + f"\n### 確定週\n- 最新確定週: {latest['iso_week']} / 前確定週: {prev['iso_week']}\n\n"
        "### 対応方針\n"
        "- `python3 tools/analyze_pr_review_comments.py --report` で増加カテゴリを特定\n"
        "- `docs/rules/self-review-checklist.md` に新パターンを追加（機械化可能なら "
        "`tools/self_review_check.py` も同一 PR で・L-094）\n"
        "- 同種指摘 3 回以上なら Lv3 フック昇格を検討（`docs/rules/harness-escalation.md`）\n"
    )
    try:
        out = subprocess.run(
            ["gh", "issue", "create", "-R", REPO,
             "--title", f"improvement: {ISSUE_MARKER}（{latest['iso_week']}）",
             "--body", body, "--label", "type:improvement,status:waiting-claude"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if out.returncode == 0:
            url = out.stdout.strip()
            print(f"[trends] 悪化検知 Issue 起票: {url}", file=sys.stderr)
            return url
        print(f"WARN: Issue 起票失敗: {out.stderr.strip()}", file=sys.stderr)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"WARN: Issue 起票エラー: {e}", file=sys.stderr)
    return None


# ─────────────────────────── self-test ───────────────────────────

def self_test() -> int:
    print("=== pr_review_trends.py self-test ===")
    ok = True

    # 1. 週境界・ISO週
    monday, sunday = week_bounds(date(2026, 6, 11))  # 木曜
    if monday != date(2026, 6, 8) or sunday != date(2026, 6, 14):
        print(f"FAIL: week_bounds 誤り {monday}..{sunday}"); ok = False
    else:
        print("PASS: week_bounds（月曜〜日曜）")

    # 2. provisional 判定
    if not is_provisional(date(2026, 6, 14), today=date(2026, 6, 15)):
        print("FAIL: 直近週が確定扱いになっている"); ok = False
    elif is_provisional(date(2026, 5, 31), today=date(2026, 6, 15)):
        print("FAIL: 8 日以上前の週が暫定扱い"); ok = False
    else:
        print("PASS: provisional 判定（週末から 8 日未満は暫定）")

    # 3. 週次集計（分母 = 週内マージ全 PR・指摘ゼロ PR も分母に入る）
    merged = [{"number": 1, "title": "[V001] x"}, {"number": 2, "title": "fix: y"},
              {"number": 3, "title": "[V002] z"}, {"number": 4, "title": "docs: w"}]
    ai_index = {
        1: {"count": 3, "by_reviewer": Counter({"layer1": 2, "copilot": 1}),
            "severity": Counter({"high": 1})},
        3: {"count": 1, "by_reviewer": Counter({"layer1": 1}), "severity": Counter()},
    }
    m = aggregate_week(merged, ai_index)
    if reviewer_key is not None:
        # Layer 1 テンプレート本文で判定し、旧ログイン名は legacy=True のときだけ数える（#627 対策 E）
        l1_body = "**🟡 WARNING** ・ **CONFIRMED** ・ 観点: 正確性\n\n欠陥"
        comments = [
            {"user": {"login": "kai-kou"}, "body": l1_body, "pull_request_url": "https://x/pulls/7"},
            {"user": {"login": "kai-kou"}, "body": "人手コメント", "pull_request_url": "https://x/pulls/7"},
            {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "body": "旧", "pull_request_url": "https://x/pulls/8"},
        ]
        idx = build_ai_index(comments)
        idx_legacy = build_ai_index(comments, legacy=True)
        if dict(idx[7]["by_reviewer"]) == {"layer1": 1} and 8 not in idx and dict(idx[7]["severity"]) == {"warning": 1} \
                and dict(idx_legacy[8]["by_reviewer"]) == {"copilot": 1}:
            print("PASS: build_ai_index（Layer 1 テンプレート判定・旧レビュアーは legacy 時のみ）")
        else:
            print(f"FAIL: build_ai_index: {dict(idx)} / legacy {dict(idx_legacy)}"); ok = False
    if m["prs_merged"] != 4 or m["prs_with_ai_comments"] != 2 or m["zero_comment_prs"] != 2:
        print(f"FAIL: 集計件数誤り {m}"); ok = False
    elif m["comments_per_pr_by_reviewer"].get("layer1") != 0.75 or m["comments_per_pr_by_reviewer"].get("copilot") != 0.25 \
            or "gemini" in m["comments_per_pr_by_reviewer"]:
        print(f"FAIL: reviewer 別 PR あたり指摘数（layer1 常設・旧系列は実データがある週だけ）誤り {m['comments_per_pr_by_reviewer']}"); ok = False
    elif abs(m["zero_comment_pr_rate"] - 0.5) > 1e-9:
        print(f"FAIL: 指摘ゼロPR率誤り {m['zero_comment_pr_rate']}（期待 0.5）"); ok = False
    elif m["ai_comments"] != 4 or m["comments_per_pr"] != 1.0:
        print(f"FAIL: 指摘数/PRあたり誤り {m}"); ok = False
    elif m["fixes_proxy"] != 2:
        print(f"FAIL: 修正数proxy 誤り {m['fixes_proxy']}"); ok = False
    else:
        print("PASS: 週次集計（分母=週内マージ全PR・指摘ゼロPR率 0.5・PRあたり 1.0）")

    # 4. JSONL 冪等性（同週キー上書き）
    import tempfile
    global TRENDS_PATH
    orig = TRENDS_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            TRENDS_PATH = Path(td) / "t.jsonl"
            save_trends({"2026-W24": {"iso_week": "2026-W24", "prs_merged": 10}})
            r = load_trends()
            r["2026-W24"] = {"iso_week": "2026-W24", "prs_merged": 20}  # 上書き
            r["2026-W23"] = {"iso_week": "2026-W23", "prs_merged": 5}
            save_trends(r)
            r2 = load_trends()
            lines = TRENDS_PATH.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) != 2:
                print(f"FAIL: JSONL 行数 {len(lines)}（期待 2・二重追記なし）"); ok = False
            elif r2["2026-W24"]["prs_merged"] != 20:
                print("FAIL: 同週キーが上書きされていない"); ok = False
            elif lines[0].find("W23") < 0:
                print("FAIL: 週キー昇順になっていない"); ok = False
            else:
                print("PASS: JSONL 冪等更新（同週上書き・昇順・二重追記なし）")
    finally:
        TRENDS_PATH = orig

    # 5. 描画フォールバック連鎖（matplotlib or Pillow or テキスト縮退）
    sample = {"2026-W23": {"iso_week": "2026-W23", "prs_merged": 8, "zero_comment_pr_rate": 0.4,
                           "comments_per_pr_by_reviewer": {"layer1": 1.0, "copilot": 0.5},
                           "ai_comments": 12},
              "2026-W24": {"iso_week": "2026-W24", "prs_merged": 10, "zero_comment_pr_rate": 0.6,
                           "comments_per_pr_by_reviewer": {"layer1": 0.8, "copilot": 0.4},
                           "ai_comments": 12}}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "c.png"
        result = render_chart(sample, out)
        if result is None:
            print("PASS: 描画は両エンジン不在でテキスト縮退（None）— 縮退経路 OK")
        elif result.exists() and out.stat().st_size > 0:
            print("PASS: 描画フォールバック連鎖（PNG 生成成功）")
        else:
            print("FAIL: 描画結果が不正"); ok = False

    # 5b. 描画する reviewer 系列の選択（matplotlib 不在でも検証できる純粋関数）
    data_zero = [{"comments_per_pr_by_reviewer": {"layer1": 1.0, "gemini": 0.0}},
                 {"comments_per_pr_by_reviewer": {"layer1": 0.0}}]
    data_legacy = [{"comments_per_pr_by_reviewer": {"layer1": 1.0, "gemini": 0.5}}]
    if set(_reviewer_series(data_zero)) == {"layer1"} and set(_reviewer_series(data_legacy)) == {"layer1", "gemini"} \
            and _reviewer_series(data_zero)["layer1"] == [1.0, 0.0]:
        print("PASS: 旧系列は非ゼロの値がある週があるときだけ描画対象にする（layer1 は常に描く）")
    else:
        print(f"FAIL: _reviewer_series: {_reviewer_series(data_zero)} / {_reviewer_series(data_legacy)}"); ok = False

    # 6. サマリー生成
    s = build_summary(sample)
    if "指摘ゼロPR率" in s and "最新週" in s:
        print("PASS: サマリーテキスト生成")
    else:
        print(f"FAIL: サマリー不正:\n{s}"); ok = False

    print("=== self-test:", "PASS ===" if ok else "FAIL ===")
    return 0 if ok else 1


# ─────────────────────────── main ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="JSONL を週次更新")
    ap.add_argument("--render", action="store_true", help="JSONL から PNG を生成")
    ap.add_argument("--notify", action="store_true", help="PNG を R2 へ上げ Slack 投稿")
    ap.add_argument("--all", action="store_true", help="update → render → notify を一括")
    ap.add_argument("--weeks", type=int, default=12, help="集計対象週数（既定 12）")
    ap.add_argument("--no-issue", action="store_true", help="悪化検知 Issue の自動起票を抑制")
    ap.add_argument("--legacy-reviewers", action="store_true",
                    help="廃止済みの Gemini / Copilot のログイン名判定も AI 指摘に含める（過去分析用）")
    ap.add_argument("--self-test", action="store_true", help="内蔵フィクスチャで検証")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    do_update = args.update or args.all
    do_render = args.render or args.all
    do_notify = args.notify or args.all
    if not (do_update or do_render or do_notify):
        ap.print_help()
        return 0

    rows = update_trends(args.weeks, legacy=args.legacy_reviewers) if do_update else load_trends()
    if not rows:
        print("ERROR: トレンドデータがありません（先に --update を実行してください）", file=sys.stderr)
        return 1

    if do_update and not args.no_issue:
        check_regression_and_file_issue(rows)

    image_url = None
    if do_render or do_notify:
        png = render_chart(rows)
        if do_notify and png:
            image_url = upload_to_r2(png)

    if do_notify:
        notify_slack(image_url, build_summary(rows))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[trends] 内部エラー: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
