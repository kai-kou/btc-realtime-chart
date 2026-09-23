#!/usr/bin/env python3
"""self_review_check.py（汎用ベース）

PR 作成前のセルフレビュー機械チェック。pre-pr-create-check.sh フックから呼ばれ、
Error 検出時（exit 1）に PR 作成をブロックする「Lv3 ハードコンストレイント」。

汎用ベースでは誤ブロックを避けるため保守的に、明確な事故のみを Error にする:
  - Error: マージコンフリクト痕跡（<<<<<<< / ======= / >>>>>>>）
  - Error: 巨大ファイルの新規追加（既定 5MB 超・SELF_REVIEW_MAX_MB で調整）
  - Error: bash 構文エラー（`bash -n`。.sh / .bash 拡張子と bash / sh shebang が対象・Issue #627）
  - Error: Python 構文エラー（`compile()`。変更された .py が対象・Issue #627）
  - Error: 対応テスト失敗（変更ファイルに対応する tools/test_<name>.sh を自動実行・Issue #627。
    SELF_REVIEW_SELFTEST=warn で Warning に降格）
  - Warning: デバッグ痕跡（TODO/FIXME/console.log/print デバッグ等）※ブロックしない
  - Warning: shellcheck 指摘（shellcheck 導入済み環境のみ・-S warning・Issue #627）
  - Warning: ruff E9/F63/F7/F82（ruff 導入済み環境のみ・変更された .py が対象・Issue #627）

プロジェクト固有のチェックは docs/rules/self-review-checklist.md に追記し、
本スクリプトに検査関数を足して拡張する。

終了コード: 0=合格 or Warning のみ / 1=Error あり（ブロック） / 2=チェッカー異常
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_MB = float(os.environ.get("SELF_REVIEW_MAX_MB", "5"))
CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

# base-update-notes 追記リマインド（Issue #211）の検出スコープ。
# apply-to-repo.sh の同期（cp -a）はファイル削除・リネームを下流へ伝播しないため、
# 削除/リネーム（D/R）は下流に孤立ファイルを残す最高確度の「下流手動対応」シグナル。
# スコープは apply-to-repo.sh の SYNC_PATHS + docs/rules/ 全体（Warm 層含む）。
UPDATE_NOTES = "docs/base-update-notes.md"
DESTRUCTIVE_SCOPE = (
    "docs/rules/", ".claude/rules/", ".claude/hooks/", ".claude/skills/",
    ".claude/agents/", ".claude/output-styles/", ".claude/commands/",
    ".claude-plugin/", "tools/", "scripts/", "modules.yaml", ".mcp.json",
)
# 配線ファイル: 変更ステータスを問わず下流の手動判断（マージ・モジュール選択・
# フック登録）が要りやすいファイル。CLAUDE.md は PROTECT_PATHS（同期対象外）のため
# base 側の変更が下流へ自動伝播しない唯一級のファイルで、新規 Hot ルールの配線も
# ここに現れる（新規追加 A の代理シグナル）。
WIRING_FILES = ("modules.yaml", ".claude/settings.json", "CLAUDE.md")

# アップデート確認の基準点マーカー（apply-to-repo.sh が下流リポジトリに生成・Issue #205/#206）。
# コミット漏れは次回 apply-base 実行時に「初回適用」への無警告退行を招くため、
# PR 差分に含まれるかを問わず git status で直接検出する。
BASE_SYNC_STATE = ".claude/base-sync-state.json"

# CJK Markdown チェッカー（同ディレクトリの check_cjk_markdown.py）を再利用する。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from check_cjk_markdown import process_text as _cjk_process_text
except ImportError:
    # ツール自体が無い場合のみ黙って無効化（任意機能）
    _cjk_process_text = None
except Exception as _e:  # noqa: BLE001
    # ツールはあるが壊れている → 黙殺すると再発防止が機能しないので原因を出す
    print(f"[self-review] Warning: check_cjk_markdown の読み込みに失敗（CJK 検査を無効化）: {_e}",
          file=sys.stderr)
    _cjk_process_text = None

# Python 危険パターン検出（FAIR Layer 0 強化・#56）。
try:
    from scan_dangerous_patterns import scan_text as _scan_py
except ImportError:
    # ツール自体が無い場合のみ黙って無効化（任意機能）
    _scan_py = None
except Exception as _e:  # noqa: BLE001
    # ツールはあるが壊れている（SyntaxError 等）→ 黙殺するとセキュリティ検査が静かに無効化される
    print(f"[self-review] Warning: scan_dangerous_patterns の読み込みに失敗（危険パターン検査を無効化）: {_e}",
          file=sys.stderr)
    _scan_py = None

# フェーズ逆流検知ゲート（docs/rules/phase-boundary-rules-detail.md §4・Issue #6・tsukuru-studio 固有）。
try:
    from check_phase_reversal_gate import DOC_PATTERN as _PHASE_DOC_PATTERN, check_gate as _phase_gate_check
except ImportError:
    _phase_gate_check = None
    _PHASE_DOC_PATTERN = None
except Exception as _e:  # noqa: BLE001
    print(f"[self-review] Warning: check_phase_reversal_gate の読み込みに失敗（フェーズ逆流ゲートを無効化）: {_e}",
          file=sys.stderr)
    _phase_gate_check = None
    _PHASE_DOC_PATTERN = None

# 横断図の鮮度ゲート（D-25 M-1 段階・Q-5 → D-40・#123）。
try:
    from check_diagram_freshness import run as _diagram_freshness_run
except ImportError:
    _diagram_freshness_run = None
except Exception as _e:  # noqa: BLE001
    print(f"[self-review] Warning: check_diagram_freshness の読み込みに失敗（横断図鮮度ゲートを無効化）: {_e}",
          file=sys.stderr)
    _diagram_freshness_run = None

# 実装モデルの実行規律（TDD 側）の機械検査（docs/rules/implementation-discipline.md §3・Issue #84）。
try:
    from check_implementation_discipline import (
        RULES as _DISCIPLINE_RULES,
        Repo as _DisciplineRepo,
        changed_entries as _discipline_changed_entries,
        evaluate as _discipline_evaluate,
    )
except ImportError:
    _discipline_evaluate = None
except Exception as _e:  # noqa: BLE001
    # ツールはあるが壊れている → 黙殺すると D-12（空のゲートは無いゲートより悪い）を自ら踏む
    print(f"[self-review] Warning: check_implementation_discipline の読み込みに失敗（実装規律検査を無効化）: {_e}",
          file=sys.stderr)
    _discipline_evaluate = None


def cjk_violation_lines(text: str) -> list[int]:
    """CJK 半角スペース違反のある行番号一覧を返す（チェッカー不在時は空）。"""
    if _cjk_process_text is None:
        return []
    try:
        _, violations = _cjk_process_text(text, fix=False)
        return [ln for ln, _ in violations]
    except Exception as e:  # noqa: BLE001
        print(f"[self-review] Warning: CJK 検査でエラー: {e}", file=sys.stderr)
        return []


def sh(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def default_branch() -> str:
    r = sh(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    return "main"


def three_way_diff(extra_args: list[str]) -> list[subprocess.CompletedProcess]:
    """コミット済み・ステージ済み・作業ツリーの 3 経路で `git diff` を実行し結果一覧を返す。

    self-review 実行時点は未コミットの編集が大半のため、HEAD 比較だけでは見落とす
    （`changed_files()` と同じ理由）。呼び出し側は `returncode == 0` の `stdout` だけを使う。
    """
    base = f"origin/{default_branch()}"
    return [
        sh(["git", "diff", f"{base}...HEAD", *extra_args]),
        sh(["git", "diff", "--cached", *extra_args]),
        sh(["git", "diff", *extra_args]),
    ]


def changed_files() -> list[str]:
    base = f"origin/{default_branch()}"
    r = sh(["git", "diff", "--name-only", f"{base}...HEAD"])
    # split() ではなく splitlines()。スペースを含むパスを 1 件として扱う
    files = r.stdout.splitlines() if r.returncode == 0 else []
    # ステージ済み・作業ツリーの変更も含める
    for extra in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
        rr = sh(extra)
        if rr.returncode == 0:
            files += rr.stdout.splitlines()
    # 未追跡（git add 前の新規ファイル）も含める。git diff は untracked を出さないため、
    # これが無いと新規 .md が CJK 検査から漏れて AI レビュー指摘が再発する（#63）
    ru = sh(["git", "ls-files", "--others", "--exclude-standard"])
    if ru.returncode == 0:
        files += ru.stdout.splitlines()
    # 実在する追跡対象ファイルのみ、重複排除
    seen, out = set(), []
    for f in files:
        if f not in seen and Path(f).is_file():
            seen.add(f); out.append(f)
    return out


def update_notes_reminder(files: list[str]) -> str | None:
    """下流影響の破壊的シグナルがあるのに base-update-notes.md 追記が無ければ文言を返す。

    検出ロジック（開発リポジトリの議論記録・議題 ID: base-fork-review-211 の合意・Warning 一本）:
      - D/R（削除・リネーム）: DESTRUCTIVE_SCOPE 全域で拾う（range diff 1 本のみ）
      - 配線ファイル（WIRING_FILES）: ステータス不問の名前照合
      - .claude/rules/ への追加（新規 Hot 化 symlink）
    単純な内容修正（M）は自動同期で下流に届くため対象外（誤検知抑制の要）。
    base-update-notes.md を持たないリポジトリ（下流フォーク）ではスキップする。
    """
    if not Path(UPDATE_NOTES).is_file():
        return None
    if UPDATE_NOTES in files:
        return None
    impacted: list[str] = []
    r = sh(["git", "diff", "--name-status", f"origin/{default_branch()}...HEAD"])
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            st, old = parts[0][:1], parts[1]  # R100 → R / リネームは旧パスで判定
            if st in ("D", "R") and old.startswith(DESTRUCTIVE_SCOPE):
                impacted.append(f"{st}:{old}")
            elif st == "A" and old.startswith(".claude/rules/"):
                impacted.append(f"A:{old}")
    impacted += [f"変更:{f}" for f in files if f in WIRING_FILES]
    if not impacted:
        return None
    shown = ", ".join(impacted[:10]) + ("…" if len(impacted) > 10 else "")
    return (
        "下流影響シグナル（削除/リネーム・配線ファイル変更）を検出しましたが "
        f"{UPDATE_NOTES} に追記がありません: {shown}"
        " → 下流で手動対応（削除追従・settings/CLAUDE.md 配線・モジュール判断）が必要なら"
        "同一 PR でエントリを追記してください（不要な変更なら無視して構いません。"
        "特にファイル削除は同期が下流へ伝播しないため追記必須）"
    )


def base_sync_state_reminder() -> str | None:
    """base-sync-state.json が存在するのに未コミットならリマインドを返す（Issue #206）。

    PR 差分（changed_files）に載るとは限らない（別セッションで生成されたまま放置される
    ケースがある）ため、対象パス限定の git status で独立に検査する。
    """
    if not Path(BASE_SYNC_STATE).is_file():
        return None
    r = sh(["git", "status", "--porcelain", "--", BASE_SYNC_STATE])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return (
        f"{BASE_SYNC_STATE}（アップデート確認の基準点マーカー）が未コミットです"
        " → コミットに含めないと次回の apply-base 実行が基準点を見失い、"
        "無警告で初回適用扱いに退行します（UPDATE NOTES の手動手順確認もスキップされます）"
    )


def rule_deletion_citation_reminder(files: list[str]) -> str | None:
    """docs/rules/*.md の削除行を検出したら PR 本文への実ケース記載をリマインドする（Issue #469）。

    削減の品質バー（token-optimization-rules.md「削減の品質バーを先に固定する」）は、ルール文書の
    削除・降格・要約に「実際に適用されたはずの直近の実ケース」1 件以上を PR 本文へ記載することを
    求める。self-review 実行時点では PR 本文がまだ存在しないため、ここでは削除行の有無だけを
    機械検出してリマインドする（記載の検証ではなく Warning 一本のリマインド）。
    """
    rule_docs = [f for f in files if f.startswith("docs/rules/") and f.endswith(".md")]
    if not rule_docs:
        return None
    deleted_count: dict[str, int] = {}
    for r in three_way_diff(["--numstat", "--", *rule_docs]):
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            _added, removed, path = parts
            if removed.isdigit():
                deleted_count[path] = max(deleted_count.get(path, 0), int(removed))
    deleted = [f"{path}(-{n})" for path, n in deleted_count.items() if n > 0]
    if not deleted:
        return None
    shown = ", ".join(deleted[:10]) + ("…" if len(deleted) > 10 else "")
    return (
        f"docs/rules/*.md の削除行を検出しました: {shown}"
        " → PR 本文に実ケース（Issue コメント / PR diff / セッションの行動記録を1件以上）を記載してください"
        "（token-optimization-rules.md「削減の品質バーを先に固定する」）"
    )


def hot_budget_reminder(files: list[str]) -> str | None:
    """Hot 層（`.claude/rules/` 実体・`token-optimization-rules.md`）変更時に予算超過を機械検証する（Issue #469）。"""
    hot_dir = Path(".claude/rules")
    hot_names = {p.name for p in hot_dir.glob("*.md")} if hot_dir.is_dir() else set()
    touches_hot = any(
        f == "docs/rules/token-optimization-rules.md" or Path(f).name in hot_names
        for f in files
    )
    if not touches_hot:
        return None
    proc = sh([sys.executable, "tools/check_hot_budget.py"], timeout=20)
    if proc.returncode == 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    bullet_lines = [l.strip("- ").strip() for l in lines if l.strip().startswith("-")]
    # 通常は "- " 箇条書きの NG 理由行が本体。ツール異常（stdout 無し）等は stderr 全文を落とさず保持する
    detail = "; ".join(bullet_lines) or "; ".join(lines) or (proc.stderr or "").strip() or "unknown"
    return f"Hot 層予算チェック NG: {detail} → docs/rules/token-optimization-rules.md の増減ログを更新してください"


# Issue #627 対策 C（Layer 0 強化）: 決定論的に検出できる事故は LLM レビュー前に落とす。
# bash / Python の構文エラーとテスト未実行は、レビュアーの目視を待たず機械的に確定できる。

def _is_bash_shebang(first_line: str) -> bool:
    """shebang 行が bash / sh インタプリタを指しているかを判定する。

    `#!/bin/bash` `#!/bin/sh` のような直接指定と、`#!/usr/bin/env bash` のような
    env 経由の指定の両方に対応する。末尾のフラグ（`#!/bin/bash -e`）は無視する。
    """
    line = first_line.strip()
    if not line.startswith("#!"):
        return False
    parts = line[2:].split()
    if not parts:
        return False
    interpreter = parts[1] if Path(parts[0]).name == "env" and len(parts) > 1 else parts[0]
    return Path(interpreter).name in ("bash", "sh")


def _bash_syntax_targets(files: list[str]) -> list[str]:
    """bash 構文検査（bash -n）と shellcheck の対象ファイル一覧を返す。

    対象: .sh / .bash 拡張子のファイル、または shebang 行が bash / sh を指すファイル。
    """
    targets: list[str] = []
    for f in files:
        if Path(f).suffix in (".sh", ".bash"):
            targets.append(f)
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                first_line = fh.readline()
        except Exception:
            continue
        if _is_bash_shebang(first_line):
            targets.append(f)
    return targets


def bash_syntax_errors(files: list[str]) -> list[str]:
    """変更された bash/sh スクリプトを `bash -n` で構文検査する（Error・Issue #627）。"""
    errs: list[str] = []
    for f in _bash_syntax_targets(files):
        try:
            proc = sh(["bash", "-n", f])
        except subprocess.TimeoutExpired:
            errs.append(f"構文エラー (bash -n) タイムアウト: {f}")
            continue
        except Exception as e:  # noqa: BLE001
            errs.append(f"構文エラー (bash -n) 実行エラー: {f}: {e}")
            continue
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            first = lines[0] if lines else "(出力なし)"
            errs.append(f"構文エラー (bash -n): {f}: {first}")
    return errs


def python_syntax_errors(files: list[str]) -> list[str]:
    """変更された .py を `compile()` で構文検査する（Error・Issue #627）。

    py_compile は __pycache__ に .pyc を書き込むため使わず、compile() のみで
    検証する（バイトコードは破棄し SyntaxError の有無だけを見る）。
    """
    errs: list[str] = []
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            text = Path(f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        try:
            compile(text, f, "exec")
        except SyntaxError as e:
            lineno = e.lineno if e.lineno is not None else 0
            errs.append(f"構文エラー (python): {f}:{lineno}: {e.msg or e}")
        except Exception as e:  # noqa: BLE001 - NUL 文字等 compile() が投げうる他の異常も報告する
            errs.append(f"構文エラー (python): {f}: {e}")
    return errs


def shellcheck_warnings(files: list[str]) -> list[str]:
    """shellcheck 導入済みの環境でのみ、対象シェルファイルを検査する（Warning・Issue #627）。

    未導入環境（既定）では何もしない。あくまで補助チェックのためブロックはしない。
    """
    if shutil.which("shellcheck") is None:
        return []
    targets = _bash_syntax_targets(files)
    if not targets:
        return []
    try:
        proc = sh(["shellcheck", "-S", "warning", "-f", "gcc", *targets], timeout=15)
    except subprocess.TimeoutExpired:
        return [f"shellcheck タイムアウト（15秒超）: {', '.join(targets[:5])}"]
    except Exception as e:  # noqa: BLE001
        return [f"shellcheck 実行エラー: {e}"]
    return [f"shellcheck: {line.strip()}" for line in (proc.stdout or "").splitlines() if line.strip()]


def ruff_syntax_warnings(files: list[str]) -> list[str]:
    """ruff 導入済みの環境でのみ、変更 .py の構文/未定義名クラスを検査する（Warning・Issue #627）。

    E9（構文エラー）・F63（比較/型の誤用）・F7（構文関連）・F82（未定義名）に限定する。
    既存の SELF_REVIEW_RUFF=1 opt-in（S=bandit ルール）とは独立に、既定 ON で動く。
    """
    if shutil.which("ruff") is None:
        return []
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return []
    try:
        proc = sh(
            ["ruff", "check", "--select", "E9,F63,F7,F82", "--output-format", "concise", *py_files],
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return [f"ruff タイムアウト（15秒超）: {', '.join(py_files[:5])}"]
    except Exception as e:  # noqa: BLE001
        return [f"ruff 実行エラー: {e}"]
    warns: list[str] = []
    for line in (proc.stdout or "").splitlines():
        s = line.strip()
        if s and ".py:" in s and not s.lower().startswith(("found", "warning:", "error:")):
            warns.append(f"ruff: {s}")
    return warns


def _resolves_under_dir(path: Path, base_dir: Path) -> bool:
    """path の実体解決先が base_dir 配下かどうかを判定する（シンボリックリンク越境防止）。"""
    try:
        resolved = path.resolve()
    except Exception:
        return False
    try:
        return resolved.is_relative_to(base_dir)
    except AttributeError:  # pragma: no cover - Python 3.8 互換フォールバック
        try:
            resolved.relative_to(base_dir)
            return True
        except ValueError:
            return False


def _companion_test_script_targets(files: list[str]) -> list[str]:
    """変更ファイルに対応する tools/test_<name>.sh を収集する（Issue #627 対策 C）。

    対象スコープ: .claude/hooks/ 直下の .sh・.claude/hooks/lib/ 配下・tools/ 直下の
    .py / .sh。拡張子を除いた stem に対して tools/test_<stem>.sh が存在すれば対象に
    加える。テストスクリプト自身（tools/test_*.sh）が変更された場合は、stem 照合
    （tools/test_test_<stem>.sh は通常存在しない）ではなくそれ自身を直接対象に加える。
    同じテストが複数の変更ファイルから指されても重複排除して 1 回だけ実行する。
    解決後の実パスが tools/ 実体配下にないもの（シンボリックリンク越境）は除外する
    （self_test_errors() の _is_allowed と同じ方針）。
    """
    repo_root = Path(".").resolve()
    tools_dir = repo_root / "tools"
    hooks_dir = Path(".claude/hooks")
    tools_rel = Path("tools")
    # scripts/*.sh も対象に含める（Issue #59）。check_implementation_discipline.py の TDD-3 は
    # scripts/*.sh に tools/test_<name>.sh を要求するのに、ここが scripts/ を見ていなかったため、
    # 要求されて書いたテストが以降の PR で 1 度も起動されないまま腐る経路が空いていた。
    scripts_rel = Path("scripts")

    seen: set[str] = set()
    out: list[str] = []

    def _add(candidate: str) -> None:
        if candidate in seen:
            return
        cp = Path(candidate)
        if not cp.is_file() or not _resolves_under_dir(cp, tools_dir):
            return
        seen.add(candidate)
        out.append(candidate)

    for f in files:
        p = Path(f)
        in_scope = (
            (p.parent == hooks_dir and p.suffix == ".sh")
            or f.startswith(".claude/hooks/lib/")
            or (p.parent == tools_rel and p.suffix in (".py", ".sh"))
            or (p.parent == scripts_rel and p.suffix == ".sh")
        )
        if not in_scope:
            continue
        if p.parent == tools_rel and p.suffix == ".sh" and p.name.startswith("test_"):
            _add(f)
            continue
        # フック名はハイフン区切り（pre-pr-create-check.sh）、テスト名はアンダースコア区切り
        # （test_pre_pr_create_check.sh）が慣例のため、両方の綴りで照合する
        _add(f"tools/test_{p.stem}.sh")
        _add(f"tools/test_{p.stem.replace('-', '_')}.sh")
    return out


# pre-pr-create-check.sh は self_review_check.py プロセス全体を外側 timeout 90 秒で包む。
# ツール単体に近い秒数を許すと合計が外側予算を超え、timeout コマンドが exit=124 で
# プロセスごと強制終了する（フックは check_exit==1 のときしか hook_block しないため、
# 124 は「チェッカー自体の異常」扱いで PR 作成が無警告に通ってしまう）。
# 予算の起点はプロセス起動時（GATE_STARTED）。--self-test / 対応テストより前に走る lint 段
# （shellcheck / ruff・各 15 秒上限）の経過も同じ時計で数えるため、self-test 段は起動から
# 約 60 秒以内に終わり、外側の 90 秒を超えない（起点を self_test_errors() 内に置くと lint 段の
# 経過が予算に乗らず合計が 90 秒を超えうる・#627 Layer 2 指摘）。
SELF_TEST_PER_TOOL_TIMEOUT = 15
SELF_TEST_BUDGET_SECONDS = 60
GATE_STARTED = time.monotonic()

# 対象判定は基本 "--self-test" 文字列の有無で機械的に拾うが（新設ツールが自動で対象へ加わる）、
# チェッカー自身は .py に変更が無くても、監視対象の非 .py ファイル（.sh 等）が変更されたら
# 起動すべきものがある。例: tools/check_distribution_boundary.py は scripts/apply-to-repo.sh の
# SYNC_PATHS を検査するが、SYNC_PATHS だけを書き換える PR では check_distribution_boundary.py
# 自身が diff に含まれず self-test が起動しない（Issue #542 の Layer 1 セルフレビューで発覚）。
COMPANION_SELF_TESTS: dict[str, tuple[str, ...]] = {
    "scripts/apply-to-repo.sh": ("tools/check_distribution_boundary.py",),
    "scripts/publish-snapshot.sh": ("tools/check_distribution_boundary.py",),
    "scripts/bootstrap.sh": ("tools/check_distribution_boundary.py",),
    # docs/routines.md の cron 控えと週次ゲートの窓幅の整合（#106）。窓だけ / cron 控えだけを
    # 書き換える PR では check_routine_gate_window.py 自身が diff に含まれないため登録する。
    "docs/routines.md": ("tools/check_routine_gate_window.py",),
    # 横断図の鮮度ゲート対象登録（Q-5 → D-40・#123）。config だけを書き換える PR では
    # check_diagram_freshness.py 自身が diff に含まれず self-test が起動しないため登録する。
    "config/diagram_targets.yaml": ("tools/check_diagram_freshness.py",),
}


# AC-4（既定 1 本）の静的検査を起動する変更ファイルの接頭辞（#112）。
# document-catalog.md §3.3 の判定条件が見る対象と 1 対 1 で対応させる:
#   S-1 → docs/framework-templates/ の中身（雛形本体は Tier 0 の 1 本だけ）
#   S-1 → docs/autonomy-templates/（framework-templates へ戻す逆流を捕まえる）
#   S-2 / S-3 → document-catalog.md §2 の Tier 表
#   S-4 → upstream-flow スキルが保持する 2 つの規律
# 末尾のチェッカ自身は COMPANION_SELF_TESTS の裏返し（監視対象が変わったらチェッカを起動する /
# チェッカが変わったら実データ検査も起動する）。self_test_errors() 経由で走る --self-test は
# 合成フィクスチャしか読まないため、判定ロジックだけを変える PR が実データ未検査で通るのを防ぐ。
DEFAULT_DOC_COUNT_TRIGGERS: tuple[str, ...] = (
    "docs/framework-templates/",
    "docs/autonomy-templates/",
    "docs/02_requirements/document-catalog.md",
    ".claude/skills/upstream-flow/",
    "tools/check_default_doc_count.py",
)


def _run_within_budget(cmd: list[str], label: str, f: str, started: float,
                       errs: list[str], hint: str) -> None:
    """PR 前ゲートの実行時間予算内でコマンドを 1 つ実行し、未実行 / タイムアウト / 実行エラー / 失敗を
    errs に整形して積む（--self-test と対応テストの共用ヘルパー。予算計算と文言を 1 箇所に集約する）。
    """
    elapsed = time.monotonic() - started
    if elapsed > SELF_TEST_BUDGET_SECONDS:
        errs.append(
            f"{label}未実行: {f}（PR 前ゲートの実行時間予算 {SELF_TEST_BUDGET_SECONDS}秒を"
            f"超過。ローカルで `{hint}` を確認してから再度 PR 作成してください）"
        )
        return
    remaining = max(1, int(SELF_TEST_BUDGET_SECONDS - elapsed))
    per_call_timeout = min(SELF_TEST_PER_TOOL_TIMEOUT, remaining)
    try:
        proc = sh(cmd, timeout=per_call_timeout)
    except subprocess.TimeoutExpired:
        errs.append(f"{label}タイムアウト: {f}（{per_call_timeout}秒超）")
        return
    except Exception as e:  # noqa: BLE001 - サブプロセス起動失敗等もフェイルオープンさせない
        errs.append(f"{label}実行エラー: {f}: {e}")
        return
    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        detail = output[-200:] if output else "(出力なし)"
        errs.append(f"{label}失敗: {f}（exit={proc.returncode}）: {detail}")


def self_test_errors(files: list[str]) -> list[str]:
    """差分に含まれる --self-test 対応ツールを実行し、失敗を Error として返す（Issue #508）。

    対象判定はソース内の "--self-test" 文字列の有無で機械的に拾う（ハードコードの
    許可リストを持たないため、新設ツールが自動的に対象へ加わる）。差分に該当ツールが
    無ければ何も実行しない（既存 PR の所要時間を増やさない）。加えて、COMPANION_SELF_TESTS
    に登録された非 .py トリガーファイルが差分に含まれる場合は、対応する自己テストツールを
    diff への出現有無に関わらず対象に加える。

    本ファイル自身はこの docstring 内にも "--self-test" 文字列を含むため、対象判定に
    そのまま乗せると自分自身をサブプロセスとして再帰起動し無限にハングする。ファイル
    パスで自己除外する。

    対象はさらに、解決後の実パスが tools/ または scripts/ の実体配下にあるものに限定する
    （シンボリックリンク経由でリポジトリ外の任意ファイルを実行させない）。

    さらに、変更ファイルに対応する tools/test_<name>.sh（.claude/hooks/*.sh・
    .claude/hooks/lib/*・tools/*.py・tools/*.sh が対象）が存在すれば実行する
    （Issue #627 対策 C）。対象判定は _companion_test_script_targets() に分離し、
    上の --self-test ループと同じ started 基準の実行時間予算を共有する。
    """
    errs: list[str] = []
    repo_root = Path(".").resolve()
    self_path = Path(__file__).resolve()
    allowed_dirs = (repo_root / "tools", repo_root / "scripts")

    def _is_allowed(path: Path) -> bool:
        # 実体解決 + 配下判定は _resolves_under_dir() に一本化する（二重実装の解消・#627）
        return any(_resolves_under_dir(path, d) for d in allowed_dirs)

    targets = [
        f for f in files
        if (f.startswith("tools/") or f.startswith("scripts/")) and f.endswith(".py")
        and Path(f).resolve() != self_path
        and _is_allowed(Path(f).resolve())
    ]
    for trigger, companions in COMPANION_SELF_TESTS.items():
        if trigger not in files:
            continue
        for companion in companions:
            if companion in targets:
                continue
            # companion の欠落は「境界チェックが黙って無効化された」状態なので Error にする
            # （沈黙スキップだと、COMPANION_SELF_TESTS が防ぐはずの退行そのものを見逃す）。
            cpath = Path(companion)
            if not cpath.is_file():
                errs.append(
                    f"COMPANION_SELF_TESTS の {companion} が存在しません"
                    f"（{trigger} 変更時の自己テストが無効化されています）"
                )
                continue
            resolved = cpath.resolve()
            if resolved == self_path or not _is_allowed(resolved):
                errs.append(
                    f"COMPANION_SELF_TESTS の {companion} は実行対象外です"
                    "（自分自身、または解決後のパスが tools/ ・ scripts/ の実体配下にありません）"
                )
                continue
            if "--self-test" not in cpath.read_text(encoding="utf-8", errors="ignore"):
                errs.append(
                    f"COMPANION_SELF_TESTS の {companion} に --self-test がありません"
                    f"（{trigger} の companion 登録が空振りしています）"
                )
                continue
            targets.append(companion)

    started = GATE_STARTED  # 起点はプロセス起動時（lint 段の経過を含めて数える・#627 Layer 2）
    for f in targets:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "--self-test" not in text:
            continue
        _run_within_budget([sys.executable, f, "--self-test"], "--self-test ", f, started, errs,
                           f"python3 {f} --self-test")

    # 対応テストスクリプト（tools/test_<name>.sh）の自動実行（Issue #627 対策 C）。
    # 上の --self-test ループと同じ started 基準の予算（SELF_TEST_BUDGET_SECONDS /
    # SELF_TEST_PER_TOOL_TIMEOUT）を共有し、合計が pre-pr-create-check.sh の外側
    # timeout を超えないようにする。
    for f in _companion_test_script_targets(files):
        _run_within_budget(["bash", f], "対応テスト", f, started, errs, f"bash {f}")
    return errs


def phase_reversal_gate_violation(files: list[str]) -> str | None:
    """product-brief 形式の要件ドキュメントへの新規要件 ID 追加に、対応する決定ログ
    エントリが伴っているかを機械検証する（フェーズ逆流検知ゲート・Issue #6・tsukuru-studio 固有）。

    既知の限界（隠さない）: 形式的な ID 差分でしか発火しない。詳細は
    check_phase_reversal_gate.py のモジュール docstring と phase-boundary-rules-detail.md §4。
    """
    if _phase_gate_check is None:
        return None
    doc_files = [f for f in files if _PHASE_DOC_PATTERN.search(f)]
    if not doc_files:
        return None
    diff_text = "".join(r.stdout for r in three_way_diff(["--", *doc_files]) if r.returncode == 0)
    if not diff_text.strip():
        return None
    ok, message = _phase_gate_check(diff_text)
    if ok:
        return None
    return f"フェーズ逆流ゲート違反:\n{message}"


def diagram_freshness_violations() -> list[str]:
    """登録済みの横断図（document-catalog.md §5.2）がソースより古くないかを機械検知する
    （D-25 M-1 段階・Q-5 → D-40・tools/check_diagram_freshness.py）。

    config/diagram_targets.yaml への登録は opt-in（既定空）のため、未登録時は常に空リストを返す。
    """
    if _diagram_freshness_run is None:
        return []
    try:
        results = _diagram_freshness_run()
    except (RuntimeError, ValueError) as e:
        return [f"横断図の鮮度ゲート: config/diagram_targets.yaml の読み込みに失敗: {e}"]
    errs = []
    for r in results:
        if not r.stale:
            continue
        if r.error:
            errs.append(f"横断図の鮮度ゲート: {r.diagram}: {r.error}")
        else:
            shown = "、".join(r.stale_sources)
            errs.append(f"横断図の鮮度ゲート: {r.diagram} がソースより古い可能性があります（{shown}）")
    return errs


def implementation_discipline_findings() -> tuple[list[str], list[str]]:
    """実装モデルの実行規律（TDD 側）の機械検査を実行し (errors, warnings) を返す（Issue #84）。

    重大度の割り方は D-36 の段階適用そのもの: **新規追加**にテストの裏付けが無ければ Error、
    既存ファイルの変更なら Warning（遡及リファクタを強制しない）。規律本体は
    docs/rules/implementation-discipline.md §2、fail-closed の理由は同 §3。

    実行時間は GATE_STARTED を起点とする共有予算に参加する（本関数は git 呼び出しを 5 回行う。
    予算を超えていたら実行せず Warning を返す — ここでブロックすると外側 timeout 90 秒の
    exit=124 と同じ「チェッカー自体の異常で PR 作成が通る」経路を自分で作ることになる）。
    """
    if _discipline_evaluate is None:
        return [], []
    if not _DISCIPLINE_RULES:
        # fail-closed（D-12）: ルールが空のまま静かに通さない
        return ["実装規律検査: 登録ルールが 0 件です（D-12・docs/rules/implementation-discipline.md §3）"], []
    if time.monotonic() - GATE_STARTED > SELF_TEST_BUDGET_SECONDS:
        return [], [f"実装規律検査 未実行: PR 前ゲートの実行時間予算 {SELF_TEST_BUDGET_SECONDS}秒を超過"
                    f"（ローカルで `python3 tools/check_implementation_discipline.py --changed` を確認してください）"]
    try:
        entries = _discipline_changed_entries()
        findings = _discipline_evaluate(entries, _DisciplineRepo("."))
    except Exception as e:  # noqa: BLE001 - 検査自体の失敗を沈黙させない
        return [], [f"実装規律検査でエラー: {e}"]
    errs = [f"実装規律違反: {f.path}: {f.message}" for f in findings if f.severity == "error"]
    warns = [f"実装規律（推奨）: {f.path}: {f.message}" for f in findings if f.severity == "warning"]
    return errs, warns


# PR 本文チェック（Issue #628）: Session-Id / 検証証跡 / PR 前レビュー記録 / エッジケース表の
# 4 チェックをここに集約する（旧 pre-pr-create-check.sh の awk/grep 実装を移植）。フックは
# PR 本文の抽出だけを担い、SELF_REVIEW_PR_BODY 環境変数で本文を渡す。本文が渡されない
# （ローカルの PR 作成前チェック等、PR 本文がまだ確定していない）ときは、従来の無条件
# スプリントメタ・リマインドにフォールバックする。

_SESSION_ID_RE = re.compile(r"Session-Id:\s*`?[0-9A-Za-z][0-9A-Za-z_-]{7,}")
_TEST_SECTION_HEADING_RE = re.compile(r"^#+\s*テスト・確認内容")
_GENERIC_HEADING_RE = re.compile(r"^#+\s")
_EVIDENCE_RE = re.compile(
    r"^\s*([-*]\s+(\[[ x]\]\s+)?)?`?(python3|bash|sh|pytest|npm|node|git|make)\s.*"
    r"(→|->|=>|PASS|FAIL|OK|exit|passed|failed|結果|件)"
    r"|^\s*\$\s"
    r"|^\s*```"
)
_PRE_REVIEW_RE = re.compile(r"PR 前レビュー:\s*(検出 [0-9]+ 件|スキップ（.+）)")
_EDGE_HEADING_RE = re.compile(r"^#+\s.*エッジケース")
_EDGE_SEP_RE = re.compile(r"^\s*\|[\s:|-]*$")
_EDGE_PLACEHOLDER_RE = re.compile(r"^\{.*\}$")


def _extract_section(text: str, start_re: "re.Pattern[str]") -> str:
    """start_re にマッチする見出し配下（次の見出しまで）の本文を抜き出す（awk 実装の移植）。"""
    out: list[str] = []
    flag = False
    for line in text.splitlines():
        if start_re.match(line):
            flag = True
            continue
        if _GENERIC_HEADING_RE.match(line):
            flag = False
            continue
        if flag:
            out.append(line)
    return "\n".join(out)


def _edge_case_row_count(pr_body: str) -> int:
    """「エッジケース」見出し配下の表の実データ行数（プレースホルダ行・区切り行を除く）を返す。"""
    n = 0
    for line in _extract_section(pr_body, _EDGE_HEADING_RE).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if _EDGE_SEP_RE.match(line):
            continue
        real = False
        for cell in line.split("|"):
            cell = cell.strip()
            if cell and not _EDGE_PLACEHOLDER_RE.match(cell):
                real = True
                break
        if real:
            n += 1
    return n


def _line_search(regex: "re.Pattern[str]", text: str) -> bool:
    """regex が text のいずれかの行に単独でマッチするかを返す（grep -qE と同じ行単位の挙動）。

    `regex.search(text)` を複数行テキストへ直接使うと、`\\s` が改行も含むため
    「Session-Id:」と無関係な後続行の文字列を同一マッチとして誤認識する
    （bash の grep は行単位処理のため発生しない・Layer 1 正確性指摘）。
    """
    return any(regex.search(line) for line in text.splitlines())


def _run_detect_pr_diff_type() -> tuple[bool, bool]:
    """tools/detect_pr_diff_type.py を実行し (has_code, high_risk) を返す（失敗時は False, False）。

    探索先は cwd（消費先プロジェクトの repo_root）ではなく、自分自身（self_review_check.py）と
    同じディレクトリに固定する。旧 pre-pr-create-check.sh 実装は CLAUDE_PLUGIN_ROOT 優先の
    scripts_root 基準で探索しており（#539・tools/ を持たない第三者プロジェクトでの無効化防止）、
    cwd 相対に戻すと同じ退行が起きる（Layer 1 指摘）。
    """
    tool = Path(__file__).resolve().parent / "detect_pr_diff_type.py"
    if not tool.is_file():
        return False, False
    try:
        proc = sh([sys.executable, str(tool)], timeout=20)
    except Exception:  # noqa: BLE001
        return False, False
    if proc.returncode != 0:
        return False, False
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:  # noqa: BLE001
        return False, False
    return bool(data.get("has_code")), bool(data.get("high_risk"))


def _read_pr_body() -> str:
    """PR 本文を取得する。SELF_REVIEW_PR_BODY_FILE（本番経路）を優先し、無ければ
    SELF_REVIEW_PR_BODY（直接値・テスト用）を見る。

    フック側は本文をファイル経由で渡す（Issue #628 Layer 1 セキュリティ指摘）: execve(2) は
    単一の引数/環境変数文字列に MAX_ARG_STRLEN（既定 128KiB）の上限があり、本文を直接
    環境変数の値として渡すとこれを超えて E2BIG で起動自体が失敗しうる（その失敗は既存の
    fail-open 分岐に落ちて無警告で素通りする）。ファイルサイズはこの上限を受けない。
    """
    body_file = os.environ.get("SELF_REVIEW_PR_BODY_FILE", "").strip()
    if body_file:
        try:
            return Path(body_file).read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    return os.environ.get("SELF_REVIEW_PR_BODY", "")


def pr_body_reminders(files: list[str]) -> list[str]:
    """PR 本文チェック（非ブロッキング・Issue #628）。

    PR 本文が渡されていれば、Session-Id / 検証証跡 / PR 前レビュー記録 /
    （high_risk 時の）エッジケース表の 4 点を検査する。渡されていなければ（本文未確定の PR 作成前
    チェック等）、従来の無条件スプリントメタ・リマインドにフォールバックする。
    """
    pr_body = _read_pr_body()
    if not pr_body:
        if not files:
            return []
        br = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        cur = br.stdout.strip() if br.returncode == 0 else ""
        if cur in ("", "main", "master", "HEAD"):
            return []
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        sid_hint = f"Session-Id: {sid}" if sid else "Session-Id: $CLAUDE_CODE_SESSION_ID を PR 本文へ"
        return [
            "スプリントメタを PR 本文に記載してください（session-sprint-rules.md §2/§5）: "
            f"{sid_hint} ＋ sp:N ラベル（project-mission.md 工程別標準値 + Dynamic 補正）"
        ]

    warnings: list[str] = []
    if not _line_search(_SESSION_ID_RE, pr_body):
        warnings.append(
            "PR 本文に Session-Id: が無い（値未記入のテンプレートを含む・session-sprint-rules.md §2）"
        )

    test_section = _extract_section(pr_body, _TEST_SECTION_HEADING_RE)
    if not _line_search(_EVIDENCE_RE, test_section):
        warnings.append(
            "検証証跡なし: 「テスト・確認内容」に実行したコマンドと結果を書く（best-practices: show evidence）"
        )

    has_code, high_risk = _run_detect_pr_diff_type()
    if (has_code or high_risk) and not _line_search(_PRE_REVIEW_RE, pr_body):
        warnings.append(
            "PR 前フレッシュ文脈レビューの記録が無い（未記入のテンプレートを含む・self-reviewer Step 3.5・#627）"
        )
    if high_risk and _edge_case_row_count(pr_body) < 2:
        warnings.append(
            "高リスク差分（hooks / settings / permissions 等）なのにエッジケース表が無い"
            "（`## エッジケース…` 見出し配下に見出し行 + データ行 1 行以上・#627 対策 D）"
        )
    return warnings


def main() -> int:
    if not Path(".git").exists() and sh(["git", "rev-parse", "--git-dir"]).returncode != 0:
        return 2

    errors: list[str] = []
    warnings: list[str] = []
    files = changed_files()

    for f in files:
        p = Path(f)
        try:
            size_mb = p.stat().st_size / (1024 * 1024)
            if size_mb > MAX_MB:
                errors.append(f"巨大ファイル: {f}（{size_mb:.1f}MB > {MAX_MB}MB）。Git LFS か別管理を検討してください。")
                continue
            # バイナリは内容スキャンしない
            raw = p.read_bytes()
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(line.startswith(m) or line == m for m in CONFLICT_MARKERS):
                errors.append(f"マージコンフリクト痕跡: {f}:{i}")
            low = line.lower()
            if "console.log(" in low or "debugger;" in low or "import pdb" in low:
                warnings.append(f"デバッグ痕跡の可能性: {f}:{i}")

        # CJK Markdown 半角スペース（CLAUDE.md「Markdown 出力ルール」）
        # 目視では見落とすため機械化（AI レビュアーの同種指摘を未然に防ぐ）
        if f.endswith((".md", ".markdown")):
            cjk_lines = cjk_violation_lines(text)
            if cjk_lines:
                shown = ", ".join(str(n) for n in cjk_lines[:8])
                ellipsis = "…" if len(cjk_lines) > 8 else ""
                warnings.append(
                    f"CJK 半角スペース違反: {f}（{len(cjk_lines)} 行: {shown}{ellipsis}）"
                    f" → python3 tools/check_cjk_markdown.py --fix {f}"
                )

        # Python 危険パターン（FAIR Layer 0 強化・#56）
        # ERROR=コマンドインジェクション/eval/pickle 等の高危険（ブロック）、WARNING=資格情報ハードコード等。
        # SELF_REVIEW_SECURITY=warn で ERROR を非ブロック化する逃げ道を用意（保守的運用）。
        if f.endswith(".py") and _scan_py is not None:
            block_security = os.environ.get("SELF_REVIEW_SECURITY", "block").lower() != "warn"
            try:
                for lineno, sev, code, msg in _scan_py(text, f):
                    entry = f"危険パターン {code}: {f}:{lineno} {msg}"
                    if sev == "ERROR" and block_security:
                        errors.append(entry)
                    else:
                        warnings.append(entry)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"危険パターン検査でエラー: {f}: {e}")

    # 実装モデルの実行規律（TDD 側・Issue #84・docs/rules/implementation-discipline.md §2）
    # 新規追加のテスト裏付け欠落は Error（ブロック）、既存ファイルの変更は Warning（D-36 段階適用）。
    # lint / self-test 段より **前** に置く: git 呼び出しだけの軽い検査であり、共有予算
    # （GATE_STARTED 起点）を重い段が食い潰した後に回すと未実行になりやすいため。
    # SELF_REVIEW_DISCIPLINE=warn で Error を非ブロック化できる（SELF_REVIEW_SECURITY /
    # SELF_REVIEW_SELFTEST と同じ逃げ道。M-1 の KPI 直結タスクが本ゲートで止まったときの
    # 明示的な回避路であり、使ったことは差分に残らないので Issue へ 1 行記録すること）。
    discipline_errs, discipline_warns = implementation_discipline_findings()
    if os.environ.get("SELF_REVIEW_DISCIPLINE", "block").lower() == "warn":
        warnings.extend(discipline_errs)
    else:
        errors.extend(discipline_errs)
    warnings.extend(discipline_warns)

    # bash 構文エラー（Error・ブロック・Issue #627 対策 C）
    errors.extend(bash_syntax_errors(files))

    # Python 構文エラー（Error・ブロック・Issue #627 対策 C）
    errors.extend(python_syntax_errors(files))

    # shellcheck（Warning・shellcheck 導入済み環境のみ・Issue #627 対策 C）
    warnings.extend(shellcheck_warnings(files))

    # ruff E9/F63/F7/F82（Warning・ruff 導入済み環境のみ・Issue #627 対策 C）
    warnings.extend(ruff_syntax_warnings(files))

    # --self-test / 対応テスト（tools/test_<name>.sh）の自動実行（Issue #508・#627・PR 作成前ゲート）
    # SELF_REVIEW_SELFTEST=warn で Error を非ブロック化する逃げ道を用意
    # （SELF_REVIEW_SECURITY と同じパターン。フレークな self-test 調査中の一時回避用）。
    selftest_findings = self_test_errors(files)
    if os.environ.get("SELF_REVIEW_SELFTEST", "block").lower() == "warn":
        warnings.extend(selftest_findings)
    else:
        errors.extend(selftest_findings)

    # base-update-notes 追記リマインド（Issue #211・Warning 一本。Error 化は実測後に再検討）
    note_warn = update_notes_reminder(files)
    if note_warn:
        warnings.append(note_warn)

    # base-sync-state マーカー未コミット検出（Issue #206）
    state_warn = base_sync_state_reminder()
    if state_warn:
        warnings.append(state_warn)

    # docs/rules/*.md 削除行の実ケース記載リマインド（Issue #469・削減の品質バー）
    citation_warn = rule_deletion_citation_reminder(files)
    if citation_warn:
        warnings.append(citation_warn)

    # Hot 層予算チェック（Issue #469・実測とログの乖離・再棚卸しの合図を機械判定）
    budget_warn = hot_budget_reminder(files)
    if budget_warn:
        warnings.append(budget_warn)

    # フェーズ逆流検知ゲート（Issue #6・phase-boundary-rules-detail.md §4・ブロック・tsukuru-studio 固有）
    phase_gate_err = phase_reversal_gate_violation(files)
    if phase_gate_err:
        errors.append(phase_gate_err)

    # 横断図の鮮度ゲート（D-25 M-1 段階・Q-5 → D-40・Issue #123・ブロック）
    errors.extend(diagram_freshness_violations())

    # AC-4（既定 1 本）の静的検査（document-catalog.md §3.3 の S-1〜S-4・#112）。
    # チェッカ自体は #19 で用意されていたが PR 前ゲートへ配線されておらず、PR #111 の
    # S-1 違反（framework-templates への先行配置）が main まで素通りした。検査は
    # リポジトリ状態に対して走るので、判定材料を触る PR で起動する。
    if any(f.startswith(t) for f in files for t in DEFAULT_DOC_COUNT_TRIGGERS):
        proc = sh([sys.executable, "tools/check_default_doc_count.py"], timeout=20)
        if proc.returncode != 0:
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            errors.append(
                "AC-4（既定 1 本）の静的検査に違反があります（document-catalog.md §3.3）"
                f": {detail[-400:] or '(出力なし)'}"
            )

    # サブエージェント定義の `tools` がフィルタで全滅していないか（#367）
    # 全滅すると委譲が「空回答」になり、しかも Claude Code は削除をエラー報告しない。
    if any(f.startswith(".claude/agents/") for f in files):
        proc = sh([sys.executable, "tools/check_agent_definitions.py"], timeout=30)
        matched = False
        for line in (proc.stdout or "").splitlines():
            if line.startswith("❌"):
                errors.append(f"サブエージェント定義: {line[2:].strip()}")
                matched = True
            elif line.startswith("⚠️"):
                warnings.append(f"サブエージェント定義: {line[2:].strip()}")
                matched = True
        # 検査自体が壊れて無警告で素通りするのを防ぐ（他の補助ツールと同じ方針）
        if proc.returncode != 0 and not matched:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "(出力なし)"
            warnings.append(
                f"サブエージェント定義チェックが異常終了しました（exit={proc.returncode}）: {tail[:200]}"
            )

    # 月次コストテレメトリの feature PR 混入チェック（#106・#242 回帰検知）
    # cost_monthly は gitignore 対象で、telemetry/cost-data ブランチへのみ永続化する（#242）。
    # 回帰シグナルは 2 種（#243 レビュー）:
    #   a) ブランチのコミット済み差分に追加/変更(A/M/R/C)として現れた（WIP 除外の破れ）
    #   b) 未追跡かつ非 ignore で現れた（gitignore エントリの破れ。--flush が再生成する）
    # 追跡解除（削除差分）と、旧ブランチ上の未コミット worktree 変更では発火させない。
    tele_prefix = "content/analytics/cost_monthly/"
    if any(f.startswith(tele_prefix) for f in files):
        ns = sh(["git", "diff", "--name-status", f"origin/{default_branch()}...HEAD"]).stdout
        committed = set()
        for line in ns.splitlines():
            parts = line.split("\t")
            if parts and parts[0][:1] in "AMRC" and parts[-1].startswith(tele_prefix):
                committed.add(parts[-1])
        untracked = set(
            sh(["git", "ls-files", "--others", "--exclude-standard", "--", tele_prefix])
            .stdout.splitlines()
        )
        tele = sorted({f for f in files if f in committed or f in untracked})
        if tele:
            warnings.append(
                "月次コストテレメトリが feature 差分に混入しています（#106・#242 回帰）: "
                f"{', '.join(tele)} → gitignore と Stop hook の WIP add 除外を確認し、差分から外してください"
            )

    # ruff 補助セキュリティチェック（FAIR Layer 0 補完・#56・opt-in）
    # 既定 OFF（誤検知ノイズ回避）。SELF_REVIEW_RUFF=1 かつ ruff 在の時のみ S(=bandit) を Warning 表示。
    if os.environ.get("SELF_REVIEW_RUFF") == "1" and shutil.which("ruff"):
        py_files = [f for f in files if f.endswith(".py")]
        if py_files:
            rr = sh(["ruff", "check", "--select", "S", "--output-format", "concise", *py_files])
            for line in (rr.stdout or "").splitlines():
                s = line.strip()
                if s and ".py:" in s and not s.lower().startswith(("found", "warning:", "error:")):
                    warnings.append(f"ruff(S): {s}")

    # PR 本文チェック / スプリントメタのリマインド（session-sprint-rules.md §2/§5・Issue #628）
    # PR の Session-Id / 検証証跡等の記載漏れを未然に防ぐ（done_sp・セッション別ベロシティ計測のため）。
    # 本文が未確定（ローカルの PR 作成前チェック等）のときは無条件リマインドにフォールバックする。
    warnings.extend(pr_body_reminders(files))

    if warnings:
        print("[self-review] Warning:")
        for w in warnings[:20]:
            print(f"  - {w}")
    if errors:
        print("[self-review] Error（PR 作成をブロックします）:")
        for e in errors[:20]:
            print(f"  - {e}")
        return 1
    print("[self-review] OK（Error なし）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[self-review] checker error: {e}", file=sys.stderr)
        sys.exit(2)
