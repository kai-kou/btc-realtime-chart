#!/usr/bin/env python3
"""Tier 1（document-catalog.md 9 本）のトリガー判定 + 3 ゲート（Issue #114 / D-39）。

正本は docs/02_requirements/document-catalog.md §3（トリガー判定の規律）。
判定は **決定ログ（`D-n`）の構造化タグ** で行い、自然文の語句マッチはしない（§3.1）。
タグの閉じた語彙は config/decision_tags.yaml（D-39・「本書では決めず M-2 に含める」の確定内容）。

例外は 2 つ（§3.1 が定義。D-n がまだ無い/現れにくいためタグ以外のシグナルを使う）:
  - T1-1（初期要件）: ユーザーの明示要求 or 複数の独立した要求（呼び出し側が構造化して渡す）
  - T1-3（インセプションデッキ）: タグ `stakeholder` に加えユーザー発言も判定材料にする

T1-4/T1-5（ストーリーマップ/ロードマップ）は D-n ではなく sp 見積もり合計で判定し、
同一トリガー・同時生成のため最初から 1 件の複合候補として扱う（§3.2 item 3）。

3 ゲート（§3.2）:
  1. 事前走査: docs_root 配下に類似ファイルがあれば「参照 or 新規」の文言に切り替える
  2. Yes/No 確認: 本ツールは確認用の文言を返すところまでを担い、実際の確認は呼び出し側（upstream-flow）が行う
  3. 1 セッション最大 1 件: pick_primary() が先頭 1 件を primary、残りを pending として返す
     （T1-7 ⊂ T1-8 の包含ペアは T1-7 を優先するだけで、T1-8 は pending へ残る＝発火自体は維持する）

Usage:
  python3 tools/tier1_triggers.py --brief docs/product-brief.md --docs-root docs [--fr-sp-total N]
  python3 tools/tier1_triggers.py --self-test
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

import yaml

CONFIG_PATH = "config/decision_tags.yaml"
DECISION_LOG_HEADING = "### 5.1"
TAG_COLUMN_KEYWORD = "タグ"
ID_COLUMN_KEYWORD = "ID"
DECISION_ID_PATTERN = re.compile(r"^D-\d+$")

# 各トリガーの提案文言と、事前走査で探すファイル名の手がかり（小文字・拡張子抜きの部分一致）。
TRIGGER_INFO: dict[str, dict[str, object]] = {
    "T1-1": {"doc": "初期要件", "hints": ["initial-requirements", "requirements-raw"]},
    "T1-2": {"doc": "リーンキャンバス", "hints": ["lean-canvas"]},
    "T1-3": {"doc": "インセプションデッキ", "hints": ["inception-deck"]},
    "T1-4+T1-5": {"doc": "ユーザーストーリーマップ + ロードマップ", "hints": ["user-story-map", "roadmap"]},
    "T1-6": {"doc": "アーキテクチャ / ドメインモデル定義", "hints": ["architecture", "domain-model"]},
    "T1-7": {"doc": "デザイン定義（ビジュアル）", "hints": ["design-tokens", "design-definition", "visual-design"]},
    "T1-8": {"doc": "UI/UX 定義", "hints": ["ui-ux", "ux-design", "screen-flow"]},
    "T1-9": {"doc": "汎用的インフラ構成", "hints": ["infra", "deployment", "infrastructure"]},
}
# 提示の優先順（先頭が primary の第一候補）は TRIGGER_INFO の定義順をそのまま使う
# （別リストで二重管理すると、片方だけ更新漏れで新規トリガーが静かに消える・#115 レビュー指摘）。
# TRIGGER_INFO は T1-7 が T1-8 より前にあり、包含ペア（T1-7 ⊂ T1-8）の「狭い方を優先」を満たす。
# 他ペア間の優先順は正本が定めていないためカタログの番号順に倣う。
CANONICAL_ORDER = list(TRIGGER_INFO.keys())


@dataclass(frozen=True)
class Decision:
    id: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    trigger_id: str
    reason: str
    matched_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GatedProposal:
    primary: Candidate | None
    pending: tuple[Candidate, ...]
    message: str | None  # primary 用の提案文言（事前走査ゲート適用済み）
    unknown_tags: tuple[str, ...] = field(default_factory=tuple)


def load_known_vocabulary(config_path: pathlib.Path) -> dict[str, list[str]]:
    """{tag_slug: [trigger_id, ...]} を返す。PyYAML は requirements.txt の必須依存（フォールバックしない）。"""
    text = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    tags = data.get("tags", {}) or {}
    vocab: dict[str, list[str]] = {}
    for slug, v in tags.items():
        triggers = (v or {}).get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]  # `triggers: T1-2`（角括弧忘れ）を単一要素として救済する
        if not isinstance(triggers, list):
            raise ValueError(
                f"{config_path}: タグ `{slug}` の triggers が list でも str でもない（{type(triggers).__name__}）"
            )
        vocab[slug] = [str(t) for t in triggers]
    return vocab


def _section_lines(text: str, heading_prefix: str) -> list[str] | None:
    """heading_prefix に完全一致する見出し（直後が非数字か行末）から次の見出しまでの本文行を返す。

    前方一致だけだと "### 5.1" が "### 5.10" / "### 5.1.1" にも誤マッチするため、
    見出し直後が空白または行末であること（数字・`.` 等の続きでないこと）まで確認する。
    """
    pattern = re.compile(rf"^{re.escape(heading_prefix)}(?!\S)")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if pattern.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if re.match(r"^#{1,6}\s", line):
            break
        body.append(line)
    return body


_SEPARATOR_ROW = re.compile(r"^\|?[\s:|-]+\|?$")


def _parse_table(body: list[str]) -> tuple[list[str], list[list[str]]]:
    """Markdown テーブルをヘッダ行とデータ行に分解する。

    区切り行（`|---|---|` 相当）は「2 行目は無条件に区切り行」と決め打たず、
    実際に区切り行のパターンに一致する行だけを取り除く（区切り行の欠落でデータ行を
    誤って捨てないため）。
    """
    rows = [line.strip() for line in body if line.strip().startswith("|")]
    if len(rows) < 2:
        return [], []
    header = [cell.strip() for cell in rows[0].strip("|").split("|")]
    data_rows = [r for r in rows[1:] if not _SEPARATOR_ROW.match(r)]
    data = [[cell.strip() for cell in row.strip("|").split("|")] for row in data_rows]
    return header, data


def parse_decision_log(markdown_text: str) -> list[Decision]:
    """product-brief.md §5.1 の決定ログ表から D-n とタグ列を抽出する。"""
    body = _section_lines(markdown_text, DECISION_LOG_HEADING)
    if body is None:
        return []
    header, rows = _parse_table(body)
    if not header:
        return []
    id_index = next((i for i, h in enumerate(header) if ID_COLUMN_KEYWORD in h), None)
    tag_index = next((i for i, h in enumerate(header) if TAG_COLUMN_KEYWORD in h), None)
    if id_index is None:
        return []
    decisions: list[Decision] = []
    for row in rows:
        if len(row) != len(header):
            continue  # 列ずれ行は無視（AC-4 静的検査と違い、ここは fail-safe で足りる）
        raw_id = row[id_index].strip("`* ")
        if not DECISION_ID_PATTERN.match(raw_id):
            # `D-n` 形式でない行（保留中の提案・語彙拡張候補等のメモ書き）は決定として拾わない。
            # SKILL.md はこれらを決定ログテーブルの外（箇条書き）へ書く前提だが、
            # 誤ってテーブル内に書かれても本物の決定と混同しない fail-safe。
            continue
        tags: tuple[str, ...] = ()
        if tag_index is not None and row[tag_index].strip():
            cell = row[tag_index].strip("`")
            parts = (p.strip("` ") for p in re.split(r"[、,]\s*", cell))
            tags = tuple(p for p in parts if p)
        decisions.append(Decision(id=raw_id, tags=tags))
    return decisions


def detect_candidates(
    decisions: list[Decision],
    known_vocab: dict[str, list[str]],
    *,
    fr_sp_total: int | None = None,
    initial_requests: list[str] | None = None,
    explicit_user_request: bool = False,
    stakeholder_mentioned: bool = False,
) -> tuple[list[Candidate], list[str]]:
    """(候補一覧, 未知タグ一覧) を返す。候補は CANONICAL_ORDER で安定ソート済み。"""
    tagset: set[str] = set()
    unknown: list[str] = []
    for d in decisions:
        for t in d.tags:
            if t in known_vocab:
                tagset.add(t)
            elif t not in unknown:
                unknown.append(t)

    candidates: dict[str, Candidate] = {}

    def fires(tag: str, trigger: str) -> bool:
        return tag in tagset and trigger in known_vocab.get(tag, [])

    if fires("payment", "T1-2"):
        candidates["T1-2"] = Candidate("T1-2", "決定ログに決済・課金の選定（`payment`）が記録された", ("payment",))
    if fires("stakeholder", "T1-3") or stakeholder_mentioned:
        matched = ("stakeholder",) if fires("stakeholder", "T1-3") else ()
        reason = "決定ログに自分以外の意思決定者への言及（`stakeholder`）が記録された" if matched else "ユーザー発言に自分以外の意思決定者への言及があった（D-n 例外・§3.1）"
        candidates["T1-3"] = Candidate("T1-3", reason, matched)
    if fires("architecture", "T1-6"):
        candidates["T1-6"] = Candidate("T1-6", "決定ログにアーキテクチャ様式/責務分割（`architecture`）が記録された", ("architecture",))
    frontend = "frontend" in tagset
    tone = "design_tone" in tagset
    if frontend and tone:
        candidates["T1-7"] = Candidate("T1-7", "決定ログにフロントエンド選定 + トンマナ言及（`frontend`+`design_tone`）が記録された", ("frontend", "design_tone"))
    if frontend:
        candidates["T1-8"] = Candidate("T1-8", "決定ログにフロントエンド技術の選定（`frontend`）が記録された", ("frontend",))
    if fires("deploy", "T1-9"):
        candidates["T1-9"] = Candidate("T1-9", "決定ログにデプロイ先/ホスティングの選定（`deploy`）が記録された", ("deploy",))
    if explicit_user_request or (initial_requests is not None and len(initial_requests) > 1):
        reason = "ユーザーの明示要求があった" if explicit_user_request else f"入力に独立した要求が {len(initial_requests)} 件含まれ 1 度で構造化しきれない（D-n 例外・§3.1）"
        candidates["T1-1"] = Candidate("T1-1", reason)
    if fr_sp_total is not None and fr_sp_total > 8:
        candidates["T1-4+T1-5"] = Candidate("T1-4+T1-5", f"FR 見積もり合計が sp:{fr_sp_total} で sp:8 を超え複数スプリントへの分割が必要")

    ordered = [candidates[k] for k in CANONICAL_ORDER if k in candidates]
    return ordered, unknown


def _prescan(trigger_id: str, docs_root: pathlib.Path | None) -> str | None:
    """既存正本の事前走査（§3.2 item 1）。ヒットしたファイルの相対パスを返す（無ければ None）。"""
    if docs_root is None or not docs_root.is_dir():
        return None
    hints = TRIGGER_INFO.get(trigger_id, {}).get("hints", [])
    for path in sorted(docs_root.rglob("*.md")):
        stem = path.stem.lower()
        if any(hint in stem for hint in hints):
            return path.as_posix()
    return None


def build_proposal_message(candidate: Candidate, docs_root: pathlib.Path | None) -> str:
    doc_name = TRIGGER_INFO.get(candidate.trigger_id, {}).get("doc", candidate.trigger_id)
    hit = _prescan(candidate.trigger_id, docs_root)
    if hit:
        return f"{candidate.reason}。既存の `{hit}` を正本として参照しますか、それとも{doc_name}を新規に作りますか？"
    return f"{candidate.reason}。{doc_name}を新規に作成しますか？"


def apply_gates(
    candidates: list[Candidate],
    unknown_tags: list[str],
    docs_root: pathlib.Path | None = None,
) -> GatedProposal:
    """§3.2 の 3 ゲートを適用する（事前走査 + 1 セッション最大 1 件。Yes/No は呼び出し側の責務）。"""
    if not candidates:
        return GatedProposal(primary=None, pending=(), message=None, unknown_tags=tuple(unknown_tags))
    primary, *rest = candidates
    message = build_proposal_message(primary, docs_root)
    return GatedProposal(primary=primary, pending=tuple(rest), message=message, unknown_tags=tuple(unknown_tags))


def evaluate(
    markdown_text: str,
    config_path: pathlib.Path,
    *,
    docs_root: pathlib.Path | None = None,
    fr_sp_total: int | None = None,
    initial_requests: list[str] | None = None,
    explicit_user_request: bool = False,
    stakeholder_mentioned: bool = False,
) -> GatedProposal:
    decisions = parse_decision_log(markdown_text)
    known_vocab = load_known_vocabulary(config_path)
    candidates, unknown = detect_candidates(
        decisions,
        known_vocab,
        fr_sp_total=fr_sp_total,
        initial_requests=initial_requests,
        explicit_user_request=explicit_user_request,
        stakeholder_mentioned=stakeholder_mentioned,
    )
    return apply_gates(candidates, unknown, docs_root)


def _self_test() -> int:
    failures: list[str] = []
    case_count = 0

    def expect(condition: bool, message: str) -> None:
        nonlocal case_count
        case_count += 1
        if not condition:
            failures.append(message)

    vocab = {
        "payment": ["T1-2"],
        "stakeholder": ["T1-3"],
        "architecture": ["T1-6"],
        "frontend": ["T1-7", "T1-8"],
        "design_tone": ["T1-7"],
        "deploy": ["T1-9"],
    }

    # --- parse_decision_log ---
    sample = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| ID | 決定事項 | 理由 | タグ | ADR 化 |\n"
        "|---|---|---|---|---|\n"
        "| `D-1` | 決済プロバイダを選ぶ | 理由1 | payment | いいえ |\n"
        "| `D-2` | 作業時間を **売る** 時間に変える運用にする | 比喩であり収益トリガーの対象ではない | | いいえ |\n"
        "| `D-3` | ターミナルの **画面** にログを **表示** する CLI にする | UI ではなくバッチ処理 | | いいえ |\n"
        "\n## 6. 未決事項\n"
    )
    decisions = parse_decision_log(sample)
    expect(len(decisions) == 3, f"3 件の D-n が抽出されるべき: {decisions}")
    expect(decisions[0].tags == ("payment",), f"D-1 のタグは payment のみのはず: {decisions[0]}")
    expect(decisions[1].tags == (), f"D-2 はタグ無しのはず: {decisions[1]}")
    expect(decisions[2].tags == (), f"D-3 はタグ無しのはず: {decisions[2]}")

    # --- _section_lines: 見出しの前方一致誤爆を防ぐ（"### 5.1" が "### 5.10" 等に誤マッチしない） ---
    heading_collision = (
        "### 5.10 別の話題\n"
        "ここは決定ログではない本文。\n"
        "## 6. 未決事項\n"
    )
    expect(
        parse_decision_log(heading_collision) == [],
        "見出し前方一致の誤爆（### 5.10 を ### 5.1 と誤認）で本文を拾ってはいけない",
    )
    sample_with_dotted_heading = sample.replace("### 5.1 決定ログ", "### 5.1.1 決定ログ")
    expect(
        parse_decision_log(sample_with_dotted_heading) == [],
        "### 5.1.1 のような枝番見出しを ### 5.1 と誤認してはいけない",
    )

    # --- _parse_table: 区切り行が無い/変則的でもデータ行を見失わない ---
    no_separator = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| ID | 決定事項 | 理由 | タグ | ADR 化 |\n"
        "| `D-9` | 決済プロバイダを選ぶ | 理由 | payment | いいえ |\n"
        "\n## 6. 未決事項\n"
    )
    expect(
        parse_decision_log(no_separator) == [Decision("D-9", ("payment",))],
        f"区切り行が無くてもデータ行を区切り行と誤認して捨ててはいけない: {parse_decision_log(no_separator)}",
    )

    # --- parse_decision_log のフォールバック分岐 ---
    no_id_column = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| 決定事項 | 理由 |\n"
        "|---|---|\n"
        "| 何か | 理由 |\n"
    )
    expect(parse_decision_log(no_id_column) == [], "ID 列が無ければ空リストを返すはず")

    no_tag_column = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| ID | 決定事項 | 理由 | ADR 化 |\n"
        "|---|---|---|---|\n"
        "| `D-1` | 決済プロバイダを選ぶ | 理由 | いいえ |\n"
    )
    expect(
        parse_decision_log(no_tag_column) == [Decision("D-1", ())],
        f"タグ列が無い決定ログでもタグ無しの決定として拾えるはず: {parse_decision_log(no_tag_column)}",
    )

    misaligned_row = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| ID | 決定事項 | 理由 | タグ | ADR 化 |\n"
        "|---|---|---|---|---|\n"
        "| `D-1` | 列がずれた行 | 理由 |\n"
        "| `D-2` | 正常な行 | 理由 | payment | いいえ |\n"
    )
    expect(
        parse_decision_log(misaligned_row) == [Decision("D-2", ("payment",))],
        f"列数がヘッダと不一致の行は無視し、正常な行だけ拾うはず: {parse_decision_log(misaligned_row)}",
    )

    non_decision_row = (
        "### 5.1 決定ログ（`D-n`）\n\n"
        "| ID | 決定事項 | 理由 | タグ | ADR 化 |\n"
        "|---|---|---|---|---|\n"
        "| 保留中の提案 | T1-8（UI/UX 定義） | 次回再提示 | | - |\n"
        "| `D-1` | 決済プロバイダを選ぶ | 理由 | payment | いいえ |\n"
    )
    expect(
        parse_decision_log(non_decision_row) == [Decision("D-1", ("payment",))],
        f"`D-n` 形式でない ID の行（保留中の提案の書き間違い等）は決定として拾ってはいけない: {parse_decision_log(non_decision_row)}",
    )

    # --- load_known_vocabulary: triggers の型が壊れていても静かに誤判定しない ---
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp_cfg:
        cfg_path = pathlib.Path(tmp_cfg) / "decision_tags.yaml"
        cfg_path.write_text("tags:\n  payment:\n    triggers: T1-2\n", encoding="utf-8")
        recovered = load_known_vocabulary(cfg_path)
        expect(
            recovered.get("payment") == ["T1-2"],
            f"`triggers: T1-2`（角括弧忘れ）はスカラを単一要素として救済するはず: {recovered}",
        )

        cfg_path.write_text("tags:\n  payment:\n    triggers: 123\n", encoding="utf-8")
        try:
            load_known_vocabulary(cfg_path)
            failures.append("triggers が int のとき ValueError を送出すべきだが送出しなかった")
        except ValueError:
            pass

    # --- 誤発火リグレッション（§3.1 の 2 実例）: タグが無ければ自然文があっても発火しない ---
    candidates, unknown = detect_candidates(decisions, vocab)
    expect(
        len(candidates) == 1 and candidates[0].trigger_id == "T1-2",
        f"D-2/D-3 の比喩・UI 語で誤発火してはいけない（payment 以外は 0 件のはず）: {candidates}",
    )
    expect(unknown == [], f"未知タグは無いはず: {unknown}")

    # --- T1-7/T1-8 の包含関係 ---
    d_frontend_tone = [Decision("D-1", ("frontend", "design_tone"))]
    c, _ = detect_candidates(d_frontend_tone, vocab)
    expect([x.trigger_id for x in c] == ["T1-7", "T1-8"], f"frontend+design_tone は T1-7 が先頭で両方発火: {c}")
    gated = apply_gates(c, [])
    expect(gated.primary is not None and gated.primary.trigger_id == "T1-7", "primary は狭い方の T1-7 のはず")
    expect([p.trigger_id for p in gated.pending] == ["T1-8"], "T1-8 は pending に残るはず（発火自体は維持）")

    d_frontend_only = [Decision("D-1", ("frontend",))]
    c2, _ = detect_candidates(d_frontend_only, vocab)
    expect([x.trigger_id for x in c2] == ["T1-8"], f"frontend のみは T1-8 だけ発火: {c2}")

    # --- 未知タグは fail-safe（判定に使わず記録だけ） ---
    d_unknown = [Decision("D-1", ("mystery_tag",))]
    c3, unknown3 = detect_candidates(d_unknown, vocab)
    expect(c3 == [], f"未知タグは何も発火させないはず: {c3}")
    expect(unknown3 == ["mystery_tag"], f"未知タグは記録されるはず: {unknown3}")

    # --- T1-1 例外（D-n 以外のシグナル） ---
    c4, _ = detect_candidates([], vocab, explicit_user_request=True)
    expect([x.trigger_id for x in c4] == ["T1-1"], "明示要求で T1-1 が発火するはず")
    c5, _ = detect_candidates([], vocab, initial_requests=["a", "b"])
    expect([x.trigger_id for x in c5] == ["T1-1"], "独立要求 2 件以上で T1-1 が発火するはず")
    c6, _ = detect_candidates([], vocab, initial_requests=["a"])
    expect(c6 == [], "独立要求 1 件では T1-1 は発火しないはず")

    # --- T1-3 例外（ユーザー発言でも発火） ---
    c7, _ = detect_candidates([], vocab, stakeholder_mentioned=True)
    expect([x.trigger_id for x in c7] == ["T1-3"], "ユーザー発言だけでも T1-3 が発火するはず")

    # --- タグ経由でしか判定材料の無いトリガー（architecture / deploy / stakeholder）が
    #     実際に Decision 経由で発火することを直接検証する（フラグでの代替検知に隠れさせない） ---
    c7b, _ = detect_candidates([Decision("D-1", ("architecture",))], vocab)
    expect([x.trigger_id for x in c7b] == ["T1-6"], f"architecture タグで T1-6 が発火するはず: {c7b}")
    c7c, _ = detect_candidates([Decision("D-1", ("deploy",))], vocab)
    expect([x.trigger_id for x in c7c] == ["T1-9"], f"deploy タグで T1-9 が発火するはず: {c7c}")
    c7d, _ = detect_candidates([Decision("D-1", ("stakeholder",))], vocab)
    expect(
        [x.trigger_id for x in c7d] == ["T1-3"] and c7d[0].matched_tags == ("stakeholder",),
        f"stakeholder タグ（flag 無し）で T1-3 が発火するはず: {c7d}",
    )

    # --- CANONICAL_ORDER の並び替えが、複数カテゴリ（D-n タグ由来 + 例外シグナル由来）の
    #     混在でも効いていることを検証する（T1-1 の判定材料は D-n ではなく explicit_user_request） ---
    c7e, _ = detect_candidates([Decision("D-1", ("payment",))], vocab, explicit_user_request=True)
    expect(
        [x.trigger_id for x in c7e] == ["T1-1", "T1-2"],
        f"T1-1 と T1-2 が同時発火したら CANONICAL_ORDER 順（T1-1 が先）になるはず: {c7e}",
    )
    gated_mixed = apply_gates(c7e, [])
    expect(gated_mixed.primary is not None and gated_mixed.primary.trigger_id == "T1-1", "混在時の primary は T1-1 のはず")

    # --- T1-4/T1-5 は複合 1 件 ---
    c8, _ = detect_candidates([], vocab, fr_sp_total=13)
    expect([x.trigger_id for x in c8] == ["T1-4+T1-5"], f"sp:8 超で複合 1 件が発火: {c8}")
    c9, _ = detect_candidates([], vocab, fr_sp_total=8)
    expect(c9 == [], "sp:8 ちょうどでは発火しないはず（超過が条件）")

    # --- 事前走査ゲート ---
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "lean-canvas-note.md").write_text("# note\n", encoding="utf-8")
        msg_hit = build_proposal_message(Candidate("T1-2", "理由"), root)
        expect("参照しますか" in msg_hit, f"既存ヒット時は参照モードの文言のはず: {msg_hit}")
        msg_miss = build_proposal_message(Candidate("T1-9", "理由"), root)
        expect("新規に作成しますか" in msg_miss, f"未ヒット時は新規モードの文言のはず: {msg_miss}")

    # --- 1 セッション最大 1 件ゲート ---
    many = [Candidate("T1-2", "r1"), Candidate("T1-6", "r2"), Candidate("T1-9", "r3")]
    gated2 = apply_gates(many, [])
    expect(gated2.primary is not None and gated2.primary.trigger_id == "T1-2", "先頭候補が primary")
    expect([p.trigger_id for p in gated2.pending] == ["T1-6", "T1-9"], "残りは pending")

    # --- 候補ゼロ ---
    gated3 = apply_gates([], [])
    expect(gated3.primary is None and gated3.pending == (), "候補ゼロなら primary も pending も空")

    # --- load_known_vocabulary（実ファイル） ---
    # is_file() が False のときに黙ってスキップすると検証が消える（fail-open）ため、
    # 不在自体を失敗として報告してから中身の検証に進む。
    real_config = pathlib.Path(__file__).resolve().parent.parent / CONFIG_PATH
    expect(real_config.is_file(), f"実 config が見つからない: {real_config}")
    if real_config.is_file():
        real_vocab = load_known_vocabulary(real_config)
        expect("payment" in real_vocab and real_vocab["payment"] == ["T1-2"], f"実 config の payment 読み取り: {real_vocab.get('payment')}")
        expect("frontend" in real_vocab and set(real_vocab["frontend"]) == {"T1-7", "T1-8"}, f"実 config の frontend 読み取り: {real_vocab.get('frontend')}")

    # --- CLI: --initial-requests-count が T1-1 判定へ実際に配線されていることを検証する ---
    import subprocess

    with tempfile.TemporaryDirectory() as tmp_cli:
        brief_path = pathlib.Path(tmp_cli) / "product-brief.md"
        brief_path.write_text("### 5.1 決定ログ（`D-n`）\n\n| ID | 決定事項 | 理由 | タグ | ADR 化 |\n|---|---|---|---|---|\n", encoding="utf-8")
        script = pathlib.Path(__file__).resolve()

        def run_cli(extra_args: list[str]) -> dict:
            proc = subprocess.run(
                [sys.executable, str(script), "--brief", str(brief_path), "--docs-root", tmp_cli, *extra_args],
                capture_output=True, text=True, timeout=30,
            )
            return json.loads(proc.stdout) if proc.returncode == 0 else {"_returncode": proc.returncode, "_stderr": proc.stderr}

        result_two = run_cli(["--initial-requests-count", "2"])
        expect(
            (result_two.get("primary") or {}).get("trigger_id") == "T1-1",
            f"CLI --initial-requests-count 2 で T1-1 が primary になるはず: {result_two}",
        )
        result_one = run_cli(["--initial-requests-count", "1"])
        expect(result_one.get("primary") is None, f"CLI --initial-requests-count 1 では発火しないはず: {result_one}")

    if failures:
        for f_ in failures:
            print(f"FAIL: {f_}")
        return 1
    print(f"PASS: tier1_triggers self-test ({case_count} cases)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 1 トリガー判定 + 3 ゲート（document-catalog.md §3）")
    parser.add_argument("--brief", help="product-brief.md のパス")
    parser.add_argument("--config", default=None, help=f"タグ語彙 YAML のパス（既定: {CONFIG_PATH}）")
    parser.add_argument("--docs-root", default=None, help="事前走査の対象ディレクトリ（既定: docs/）")
    parser.add_argument("--fr-sp-total", type=int, default=None, help="FR-n 見積もり合計（sp）")
    parser.add_argument("--initial-requests-count", type=int, default=None, help="入力に含まれる独立した要求の件数（2 件以上で T1-1 の判定材料になる）")
    parser.add_argument("--explicit-user-request", action="store_true")
    parser.add_argument("--stakeholder-mentioned", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if not args.brief:
        parser.error("--brief が必要です（--self-test を使わない場合）")

    root = pathlib.Path(__file__).resolve().parent.parent
    config_path = pathlib.Path(args.config) if args.config else root / CONFIG_PATH
    docs_root = pathlib.Path(args.docs_root) if args.docs_root else root / "docs"
    text = pathlib.Path(args.brief).read_text(encoding="utf-8")

    initial_requests = ["request"] * args.initial_requests_count if args.initial_requests_count else None
    gated = evaluate(
        text,
        config_path,
        docs_root=docs_root,
        fr_sp_total=args.fr_sp_total,
        initial_requests=initial_requests,
        explicit_user_request=args.explicit_user_request,
        stakeholder_mentioned=args.stakeholder_mentioned,
    )
    result = {
        "primary": None if gated.primary is None else {"trigger_id": gated.primary.trigger_id, "reason": gated.primary.reason},
        "message": gated.message,
        "pending": [{"trigger_id": p.trigger_id, "reason": p.reason} for p in gated.pending],
        "unknown_tags": list(gated.unknown_tags),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
