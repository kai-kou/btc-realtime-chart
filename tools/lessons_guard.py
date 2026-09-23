#!/usr/bin/env python3
"""
lessons_guard.py — lesson（学習記録）肥大化の根本対策ツール（統合版）

過去に乱立した prune_lessons.py / lessons_scorer.py / split_lessons.py を 1 本に統合し、
「出口（削除）の修復」と「機械強制（サイズ上限）」を提供する。

設計思想（docs/rules/lessons-management.md の SSOT に準拠）:
  - 入口（追加）と出口（削除）の非対称性こそが肥大化の根本原因。
  - 「昇格 = 物理削除」を徹底する（移動・アーカイブは総量を減らさない）。
  - Hot 層（lessons-core.md）には機械強制のサイズ上限を設ける。

層構造:
  Hot  : docs/rules/lessons-core.md   （全セッション常駐・サイズ上限を機械強制）
  Warm : docs/rules/lessons/*.md      （カテゴリ別・タスク依存 Read）
  Cold : git 履歴                      （昇格済みエントリは物理削除し履歴に委ねる）

サブコマンド:
    python3 tools/lessons_guard.py check          # Hot 層が上限内か検証（CI/フック用・超過で exit 1）
    python3 tools/lessons_guard.py stats          # 各層の行数・エントリ数・分類を表示
    python3 tools/lessons_guard.py prune          # 物理削除候補を表示（dry-run）
    python3 tools/lessons_guard.py prune --apply  # 昇格済みエントリを Hot 層から物理削除
    python3 tools/lessons_guard.py dedup          # タイトル類似の重複候補を検出（統合候補）

終了コード:
    check : 0 = 上限内 / 1 = 上限超過（CI・フックがブロック）
    その他: 0 = 正常
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- 設定（Hot 層のサイズ上限・SSOT は docs/rules/lessons-management.md）----
REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_FILE = REPO_ROOT / "docs/rules/lessons-core.md"
WARM_DIR = REPO_ROOT / "docs/rules/lessons"

# Hot 層の機械強制上限（超過したら Warm 層へ降格してから追加すること）
CORE_MAX_LINES = 350      # 目標 300・運用余裕を見て上限 350
CORE_MAX_ENTRIES = 15     # コア + 凍結を含む常駐エントリ数の上限

# 昇格済みエントリを物理削除対象とみなす経過日数（昇格日からの猶予）
PRUNE_THRESHOLD_DAYS = 30

# このマーカーを本文に含むエントリは分類に関わらず物理削除しない（誤削除の安全装置）。
# 「昇格済みだが全セッション横断で常駐が必須」な行動規範（例: L-077）に付与する。
KEEP_MARKER = "**保持理由**"

ENTRY_RE = re.compile(r"^#{2,3} (L-\d+):\s*(.+?)\s*$")
PROMOTION_RE = re.compile(r"\*\*昇格先\*\*\s*[:：]\s*(.+)")
DATE_RE = re.compile(r"昇格日[:：]\s*(\d{4})-(\d{2})-(\d{2})")


@dataclass
class Entry:
    id: str
    title: str
    start: int           # 0-based 行インデックス（### の行）
    end: int             # 次エントリ/セクション直前まで（排他）
    lines: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "".join(self.lines)

    @property
    def promotion_target(self) -> str | None:
        m = PROMOTION_RE.search(self.body)
        return m.group(1).strip() if m else None

    @property
    def promotion_date(self):
        m = DATE_RE.search(self.body)
        if not m:
            return None
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)

    def classify(self) -> str:
        """エントリを分類する。

        core               : 昇格先なし表記がなく昇格もされていない真のコア
        frozen             : 「昇格先: なし（…未完了）」= 凍結（昇格先未実装）
        self-ref           : 昇格先が lessons-core.md 自身（常駐維持の選択）
        promoted-issue     : 昇格先が Issue #N のみ（コード未実装・実装待ち）
        promoted-impl      : 昇格先が実コード/フック/ルールに実装済み → 物理削除対象
        """
        target = self.promotion_target
        if target is None:
            return "core"
        t = target
        # 「なし」を含む → 凍結
        if "なし" in t:
            return "frozen"
        # 自己参照（lessons-core.md 常駐維持）
        if "lessons-core.md" in t and self.promotion_date is None:
            return "self-ref"
        # Issue のみ（実コード言及なし）
        has_impl_ref = any(
            kw in t
            for kw in (".py", ".sh", ".yml", ".ts", "CLAUDE.md", "docs/rules/", ".claude/")
        )
        if not has_impl_ref and "Issue" in t:
            return "promoted-issue"
        # 自己参照を含むが昇格日もある場合は常駐維持を優先
        if "lessons-core.md" in t:
            return "self-ref"
        return "promoted-impl"


def parse_entries(path: Path) -> list[Entry]:
    """ファイルから ## / ### L-NNN エントリを抽出する。"""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").splitlines(keepends=True)
    entries: list[Entry] = []
    cur: Entry | None = None
    for i, line in enumerate(raw):
        m = ENTRY_RE.match(line)
        # 新セクション（## 始まり）でエントリを閉じる。ただしエントリ見出し
        # （## L-NNN: / ### L-NNN:）自体は区切りではなく次エントリの開始として扱う
        # （ENTRY_RE が H2/H3 両対応のため not m で除外する）。
        is_section = line.startswith("## ") and not m
        if m:
            if cur is not None:
                cur.end = i
                entries.append(cur)
            cur = Entry(id=m.group(1), title=m.group(2), start=i, end=i, lines=[line])
        elif cur is not None:
            if is_section:
                cur.end = i
                entries.append(cur)
                cur = None
            else:
                cur.lines.append(line)
    if cur is not None:
        cur.end = len(raw)
        entries.append(cur)
    return entries


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def find_duplicate_ids() -> dict[str, list[str]]:
    """Hot + Warm 層横断で同一 L-NNN 番号が複数ファイルに存在する箇所を検出する。"""
    by_id: dict[str, list[str]] = {}
    for f in [CORE_FILE] + (sorted(WARM_DIR.glob("*.md")) if WARM_DIR.exists() else []):
        for e in parse_entries(f):
            by_id.setdefault(e.id, []).append(f.name)
    return {lid: files for lid, files in by_id.items() if len(files) > 1}


# ---------------- check ----------------
def cmd_check(args) -> int:
    lines = count_lines(CORE_FILE)
    entries = parse_entries(CORE_FILE)
    n_entries = len(entries)
    over_lines = lines > CORE_MAX_LINES
    over_entries = n_entries > CORE_MAX_ENTRIES
    dup_ids = find_duplicate_ids()

    status = "✅ OK" if not (over_lines or over_entries or dup_ids) else "❌ 上限超過/重複あり"
    print(f"[lessons_guard check] {status}")
    print(f"  lessons-core.md: {lines} 行 / 上限 {CORE_MAX_LINES} 行")
    print(f"  エントリ数      : {n_entries} 件 / 上限 {CORE_MAX_ENTRIES} 件")
    print(f"  番号重複        : {len(dup_ids)} 件")

    if over_lines or over_entries:
        print()
        print("  Hot 層（lessons-core.md）が上限を超えています。以下のいずれかで解消してください:")
        print("   1. 昇格済みエントリを物理削除   : python3 tools/lessons_guard.py prune --apply")
        print("   2. 凍結/参照頻度の低いエントリを Warm 層（docs/rules/lessons/<category>.md）へ降格")
        print("   3. 重複を統合                   : python3 tools/lessons_guard.py dedup")
        print("  ※ 詳細は docs/rules/lessons-management.md を参照")

    if dup_ids:
        print()
        print("  同一番号が複数ファイルに存在します（採番の一意性違反・#340）:")
        for lid, files in sorted(dup_ids.items()):
            print(f"   - {lid}: {', '.join(files)}")
        print("  片方を未使用番号へ振り直し、参照元（grep）を追随更新してください。")

    return 1 if (over_lines or over_entries or dup_ids) else 0


# ---------------- stats ----------------
def cmd_stats(args) -> int:
    print("=== lessons 層別サマリー ===")
    core_entries = parse_entries(CORE_FILE)
    print(f"[Hot] lessons-core.md : {count_lines(CORE_FILE)} 行 / {len(core_entries)} エントリ")
    buckets: dict[str, list[str]] = {}
    for e in core_entries:
        buckets.setdefault(e.classify(), []).append(e.id)
    for cls in ("core", "self-ref", "promoted-issue", "frozen", "promoted-impl"):
        ids = buckets.get(cls, [])
        if ids:
            print(f"    - {cls:16s}: {len(ids):2d} 件  {', '.join(ids)}")

    print()
    warm_files = sorted(WARM_DIR.glob("*.md")) if WARM_DIR.exists() else []
    warm_total = 0
    for f in warm_files:
        n = len(parse_entries(f))
        warm_total += count_lines(f)
        print(f"[Warm] {f.name:20s}: {count_lines(f):4d} 行 / {n:2d} エントリ")
    print(f"    Warm 合計: {warm_total} 行")
    return 0


# ---------------- prune ----------------
def _git_is_clean_for(path: Path) -> bool:
    """対象ファイルに未コミット変更がないか（物理削除前の安全確認）。"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out == ""
    except Exception:
        return False


def cmd_prune(args) -> int:
    entries = parse_entries(CORE_FILE)
    now = datetime.now(timezone.utc)
    targets: list[Entry] = []
    for e in entries:
        if KEEP_MARKER in e.body:
            continue  # 保持マーカー付きは常駐必須のため削除しない
        if e.classify() != "promoted-impl":
            continue
        pd = e.promotion_date
        if pd is None:
            continue
        if (now - pd).days < PRUNE_THRESHOLD_DAYS:
            continue
        targets.append(e)

    if not targets:
        print("[lessons_guard prune] 物理削除候補なし（昇格済み・実装済み・30日経過のエントリは現在ありません）")
        return 0

    print(f"[lessons_guard prune] 物理削除候補: {len(targets)} 件（昇格先に実装済み・git 履歴に残る）")
    for e in targets:
        days = (now - e.promotion_date).days
        print(f"  - {e.id}: {e.title[:40]}  （昇格 {days} 日経過）")
        print(f"      昇格先: {e.promotion_target[:80]}")

    if not args.apply:
        print()
        print("  ※ dry-run です。物理削除するには --apply を付けてください。")
        print("    削除内容は git 履歴に保存されるため復元可能です。")
        return 0

    if not _git_is_clean_for(CORE_FILE):
        print()
        print("  ❌ lessons-core.md に未コミット変更があります。先にコミットしてから --apply してください")
        print("     （誤削除時に git で復元できる状態を保証するため）")
        return 1

    # 後ろから削除して行インデックスのズレを防ぐ
    raw = CORE_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    for e in sorted(targets, key=lambda x: x.start, reverse=True):
        del raw[e.start:e.end]
    CORE_FILE.write_text("".join(raw), encoding="utf-8")
    print()
    print(f"  ✅ {len(targets)} 件を lessons-core.md から物理削除しました（git 履歴に保存済み）")
    print(f"  削除後: {count_lines(CORE_FILE)} 行")
    return 0


# ---------------- dedup ----------------
def _normalize(title: str) -> set[str]:
    t = re.sub(r"（.*?）|\(.*?\)", "", title)
    t = re.sub(r"[^\wぁ-んァ-ヶ一-龠ー]", " ", t)
    return {w for w in t.split() if len(w) >= 2}


def cmd_dedup(args) -> int:
    all_entries: list[tuple[str, Entry]] = []
    for f in [CORE_FILE] + (sorted(WARM_DIR.glob("*.md")) if WARM_DIR.exists() else []):
        for e in parse_entries(f):
            all_entries.append((f.name, e))

    print(f"[lessons_guard dedup] {len(all_entries)} エントリ横断でタイトル類似を検査")
    found = 0
    for i in range(len(all_entries)):
        fi, ei = all_entries[i]
        si = _normalize(ei.title)
        if not si:
            continue
        for j in range(i + 1, len(all_entries)):
            fj, ej = all_entries[j]
            sj = _normalize(ej.title)
            if not sj:
                continue
            jac = len(si & sj) / len(si | sj)
            if jac >= 0.5:
                found += 1
                print(f"  類似 {jac:.2f}: [{fi}] {ei.id} {ei.title[:30]}")
                print(f"             [{fj}] {ej.id} {ej.title[:30]}")
    if found == 0:
        print("  重複候補なし（タイトル Jaccard 類似度 >= 0.5）")
    else:
        print(f"\n  {found} 組の統合候補を検出。最も一般的な 1 件に統合し残りを削除してください。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="lesson 肥大化の根本対策ツール")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Hot 層が上限内か検証（超過で exit 1）")
    sub.add_parser("stats", help="各層の行数・エントリ数・分類を表示")
    pp = sub.add_parser("prune", help="昇格済みエントリを物理削除")
    pp.add_argument("--apply", action="store_true", help="実際に削除する（デフォルトは dry-run）")
    sub.add_parser("dedup", help="タイトル類似の重複候補を検出")

    args = p.parse_args()
    return {
        "check": cmd_check,
        "stats": cmd_stats,
        "prune": cmd_prune,
        "dedup": cmd_dedup,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
