#!/usr/bin/env python3
"""detect_pr_diff_type.py — PR の差分が「コード変更」を含むかを判定する（#2880）。

制作系パイプラインの PR 作成前ステップで実行し、差分が自動生成データのみ
（例: 台本・素材・分析データ等）の場合は重い Layer 2（敵対的多観点議論）を
スキップして PR 所要時間を削減する。
外部 AI レビュアー（Copilot / Gemini）への依頼は廃止済みで、レビューは常に Claude 自身の
Layer 1 セルフレビュー（自前 code-review スキル・全 PR 必須）で完結する
（組み込み /code-review は同名 project スキルで置換済み・#275 → #280。SSOT: docs/rules/ai-reviewer-strategy.md）。

判定ロジック:
- コード拡張子（.py/.ts/.tsx/.js/.jsx/.sh/.yaml/.yml/.toml/.md）を含むか
- 拡張子マッチでは拾えない critical な設定・依存関係ファイル名（package.json / Dockerfile /
  requirements.txt / pyproject.toml / .gitignore / Makefile 等）も code 扱い（CRITICAL_FILENAMES）
- ただし `config/data_only_path_prefixes.txt` に列挙したパス配下は auto-gen データ扱いで
  コード変更とみなさない（既定値は `content/`。プロジェクト固有の生成データ置き場を追加したい
  場合はコード変更不要でこのファイルに追記する・#413）

使い方:
    python3 tools/detect_pr_diff_type.py                  # JSON 出力（既定）
    python3 tools/detect_pr_diff_type.py --base origin/main
    python3 tools/detect_pr_diff_type.py --head HEAD --base origin/main

出力例:
    {
      "has_code": false,
      "data_only": true,
      "review_strategy": "claude_only",
      "code_files": [],
      "data_files": ["content/scripts/episode_001.json"],
      "high_risk": false,
      "risk_reasons": [],
      "risk_files": []
    }

review_strategy（外部レビュアー依頼は廃止。Claude セルフレビュー前提）:
- "claude_only": Layer 0 機械ゲート + Layer 1 観点別フレッシュ文脈セルフレビューのみ（Layer 2 スキップ）
- "full": Layer 0 + Layer 1 観点別フレッシュ文脈セルフレビュー + 条件付き Layer 2（敵対的多観点議論）を起動

high_risk（#53・Layer 3 外部独立レビューの任意手動起動を検討する判断材料）:
- 認証/秘密情報関連パス・公開API/スキーマ/DB関連パス・フック/CI/権限境界の変更・
  差分行数/ファイル数の閾値超過のいずれかに該当すると true。Layer 3 は自動起動しない
  （`docs/rules/ai-reviewer-strategy.md` の通り任意・手動）ため、high_risk=true は
  「Layer 3 起動を検討すべき」というシグナルであり、マージをブロックしない。

終了コード: 常に 0（判定情報を JSON で返す。スキル側で分岐する）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

CODE_EXTENSIONS = re.compile(r"\.(py|ts|tsx|js|jsx|sh|ya?ml|toml|md)$")
# 自動生成データの配置先の組み込み既定値（config/data_only_path_prefixes.txt が無い/空のときの
# フォールバック）。プロジェクト固有の追加は config ファイル側に書く（コード変更不要・#413）。
DEFAULT_DATA_PATH_PREFIXES = ("content/",)
DATA_PATH_PREFIXES_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "data_only_path_prefixes.txt"
)


def load_data_path_prefixes(path: str = DATA_PATH_PREFIXES_CONFIG) -> tuple[str, ...]:
    """auto-gen データ扱いするパス接頭辞を config ファイルから読む（無ければ組み込み既定値）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.strip() for line in f]
    except OSError:
        return DEFAULT_DATA_PATH_PREFIXES
    prefixes = tuple(line for line in lines if line and not line.startswith("#"))
    return prefixes or DEFAULT_DATA_PATH_PREFIXES


DATA_PATH_PREFIXES = load_data_path_prefixes()
# 拡張子マッチでは拾えない critical な設定・依存関係ファイル名（コード変更と同等に扱う）
# package.json は .json 拡張子だがコード変更と同等の影響を持つため除外できない
CRITICAL_FILENAMES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
    ".gitignore", ".gitattributes",
    "Makefile",
})

# 高リスク差分判定（#53・FAIR Layer 3 の任意手動起動の判断材料）。
# しきい値は初期値であり、実運用の誤検知/取りこぼし実績に応じて較正する想定。
# 「auth」「token」単体は token-optimization-rules.md 等の無関係なファイルにも
# マッチする誤検知が多いため含めない（より具体的な複合語のみ対象にする）。
HIGH_RISK_PATH_PATTERN = re.compile(
    r"(secret|credential|password|\.env|security|permission"
    r"|api[-_]?key|access[-_]?token|auth[-_]?token|authentication|authorization)",
    re.IGNORECASE,
)
SCHEMA_PATH_PATTERN = re.compile(
    r"(schema|migrations?/|openapi|swagger|\.proto$|api/v\d+/)",
    re.IGNORECASE,
)
GOVERNANCE_PATH_PREFIXES = (".claude/hooks/", ".github/workflows/")
GOVERNANCE_FILENAMES = frozenset({".claude/settings.json", ".mcp.json"})
HIGH_RISK_DIFF_LINES = 500
HIGH_RISK_FILE_COUNT = 20


def get_diff_files(base: str, head: str) -> list[str] | None:
    """base...head の差分ファイル一覧を取得する。

    エラー時は None を返し、呼び出し側で安全側（full レビュー）にフォールバックさせる。
    空リスト [] は「正常に取得できたが差分が 0 件」を示し、None と意味が異なる。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        )
    except Exception as e:
        # subprocess.CalledProcessError（base 不在等）/ FileNotFoundError（git 未インストール）等を広く捕捉
        stderr_msg = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"⚠️  git diff failed: {stderr_msg}", file=sys.stderr)
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def get_diff_line_count(base: str, head: str) -> int | None:
    """base...head の追加+削除行数を返す（取得失敗時は None）。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        )
    except Exception:
        return None
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, deleted = parts[0], parts[1]
        # バイナリファイルは "-" が入るため数値変換できる列だけ加算する
        for v in (added, deleted):
            if v.isdigit():
                total += int(v)
    return total


def assess_risk(files: list[str], diff_lines: int | None) -> dict:
    """高リスク差分判定（#53）。Layer 3（外部独立レビュー）の任意手動起動の判断材料を返す。

    しきい値（HIGH_RISK_DIFF_LINES・HIGH_RISK_FILE_COUNT）は初期値であり、
    実運用の誤検知/取りこぼし実績に応じて較正する想定。
    """
    reasons: list[str] = []

    security_files = [f for f in files if HIGH_RISK_PATH_PATTERN.search(f)]
    if security_files:
        reasons.append(f"認証/秘密情報関連パス {len(security_files)} 件")

    schema_files = [f for f in files if SCHEMA_PATH_PATTERN.search(f)]
    if schema_files:
        reasons.append(f"公開API/スキーマ/DB関連パス {len(schema_files)} 件")

    governance_files = [
        f for f in files
        if f in GOVERNANCE_FILENAMES or any(f.startswith(p) for p in GOVERNANCE_PATH_PREFIXES)
    ]
    if governance_files:
        reasons.append(f"フック/CI/権限境界の変更 {len(governance_files)} 件")

    if diff_lines is not None and diff_lines >= HIGH_RISK_DIFF_LINES:
        reasons.append(f"差分 {diff_lines} 行（閾値 {HIGH_RISK_DIFF_LINES} 行）")

    if len(files) >= HIGH_RISK_FILE_COUNT:
        reasons.append(f"変更ファイル数 {len(files)} 件（閾値 {HIGH_RISK_FILE_COUNT} 件）")

    return {
        "high_risk": len(reasons) > 0,
        "risk_reasons": reasons,
        "risk_files": sorted(set(security_files) | set(schema_files) | set(governance_files)),
    }


def classify(files: list[str], diff_lines: int | None = None) -> dict:
    """ファイルリストを「コード変更」と「データのみ変更」に分類し、高リスク判定を付与する。"""
    code_files: list[str] = []
    data_files: list[str] = []
    for path in files:
        filename = path.rsplit("/", 1)[-1]
        is_code = bool(CODE_EXTENSIONS.search(path)) or filename in CRITICAL_FILENAMES
        is_data_path = any(path.startswith(p) for p in DATA_PATH_PREFIXES)
        if is_code and not is_data_path:
            code_files.append(path)
        else:
            data_files.append(path)

    has_code = len(code_files) > 0
    result = {
        "has_code": has_code,
        "data_only": not has_code and len(data_files) > 0,
        "review_strategy": "full" if has_code else "claude_only",
        "code_files": code_files,
        "data_files": data_files,
        "total_files": len(files),
    }
    result.update(assess_risk(files, diff_lines))
    return result


def _full_review_fallback() -> dict:
    """get_diff_files が None を返した時の安全側フォールバック（full レビュー + 高リスク扱い）。"""
    return {
        "has_code": True,
        "data_only": False,
        "review_strategy": "full",
        "code_files": [],
        "data_files": [],
        "total_files": 0,
        "high_risk": True,
        "risk_reasons": ["差分取得失敗のため安全側で高リスク扱い"],
        "risk_files": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PR 差分タイプ判定（#2880・高リスク判定は #53）")
    ap.add_argument("--base", default="origin/main", help="比較元ブランチ（既定: origin/main）")
    ap.add_argument("--head", default="HEAD", help="比較先（既定: HEAD）")
    ap.add_argument("--strategy-only", action="store_true",
                    help="review_strategy のみ出力（シェルスクリプトでの利用向け）")
    ap.add_argument("--risk-only", action="store_true",
                    help="high_risk（true/false）のみ出力（シェルスクリプトでの利用向け）")
    ap.add_argument("--diff-lines", type=int, default=None,
                    help="追加+削除行数を明示指定（省略時は git diff --numstat で取得を試みる。"
                         "mcp__github__pull_request_read 等で既に additions/deletions を"
                         "取得済みのクラウド環境での再計算コスト削減に使う）")
    args = ap.parse_args()

    files = get_diff_files(args.base, args.head)
    if files is None:
        # エラー時は安全側で full レビュー（Layer 2 をスキップしない）
        result = _full_review_fallback()
    else:
        diff_lines = args.diff_lines if args.diff_lines is not None else get_diff_line_count(args.base, args.head)
        result = classify(files, diff_lines)

    if args.strategy_only:
        print(result["review_strategy"])
    elif args.risk_only:
        print("true" if result["high_risk"] else "false")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
