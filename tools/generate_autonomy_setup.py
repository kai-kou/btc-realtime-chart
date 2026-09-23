#!/usr/bin/env python3
"""下流プロダクトを自律運転へ入れるための設定一式を生成する（`FR-23` / `AC-19`・Issue #110）。

`C-5`（下流プロダクトが自律運転に入れる状態を作る）の生成側の実体。上流工程（`C-1`〜`C-4`）が
要件ドキュメントと `sp:N` 付き Issue を引き渡した後、**そのリポジトリが定期ルーティンで回り始める
ために足りないもの** だけを配置する。

## 何を生成し、何を生成しないか（境界）

`scripts/apply-to-repo.sh` が既に配るもの（常駐ルール・スキル・フック・`tools/`）は **作り直さない**。
下流に届いていないのは次の 3 つだけで、本ツールの生成対象はそこに限る:

  1. ルーティン定義（母艦の `docs/routines.md` は配布境界の denylist にあり下流へ届かない）
  2. GitHub ラベルの実体（ルール群はラベルの存在を前提にするが、ラベル自体はファイルで配れない）
  3. Routines UI 側の設定手順（sources / 実行環境 / コネクターは agent から設定できない）

## 層構造について（`implementation-discipline.md` §1）

外部接点（ファイル読み書き・標準出力）は `main()` と `_read` / `_write` に閉じ、
テンプレート描画・プレースホルダ検査・cron の可読化・ラベルコマンド生成は純粋関数にしている。
`--self-test` はその純粋関数だけを検証するのでファイルシステムを触らない（L-3）。
層は増やしていない（L-4・YAGNI。実装が 1 つしかない抽象は作らない）。

fail-closed（`D-12`）: 雛形が 1 つでも欠けていたら exit 2、置換後にプレースホルダが残ったら
exit 1 で **1 ファイルも書かない**。「生成したが中身がプレースホルダのまま」を成功終了させない。

既知の限界（隠さない）:
  - cron の可読化は時フィールドの `*` と `*/N` だけを解釈する。それ以外（リスト・範囲）は
    可読文へ変換せず cron 式をそのまま埋める（誤った日本語を作らない安全側の退行）
  - ラベルの作成そのものは行わない。`gh label create` のコマンド列を出力するだけで、
    実行経路（gh / 画面）の選択は `routines-setup.md` §1 が案内する
  - 雛形ディレクトリ（`docs/autonomy-templates/`）は下流へ同期されないため、生成先リポジトリで
    ラベルコマンドを出し直すときは `--labels-file config/labels.yaml`（生成済みの写し）を渡す
  - 生成先に同名ファイルがあるときは上書きしない（`--force` で上書き）

Usage:
  python3 tools/generate_autonomy_setup.py --out ../my-product --name "My Product" --slug owner/my-product
  python3 tools/generate_autonomy_setup.py --out ../my-product --name "My Product" --slug owner/my-product --dry-run
  python3 tools/generate_autonomy_setup.py --print-label-commands --slug owner/my-product
  python3 tools/generate_autonomy_setup.py --print-label-commands --slug owner/my-product --labels-file config/labels.yaml
  python3 tools/generate_autonomy_setup.py --self-test
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from check_routine_gate_window import parse_cron_interval_hours

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "docs" / "autonomy-templates"

# 雛形 → 生成先（生成先は対象リポジトリのルートからの相対パス）。
# 🔴 ここが 0 件になったらツールは何も生成しない空のゲートになるため、main() で件数を検査する。
TEMPLATE_MAP: dict[str, str] = {
    "routines.md": "docs/routines.md",
    "routines-setup.md": "docs/setup/routines-setup.md",
    "labels.yaml": "config/labels.yaml",
}

# 置換するプレースホルダはこの 4 つだけ。bootstrap.sh の `{{...}}` 記法とは別綴りにして、
# 雛形が bootstrap のプレースホルダ置換に巻き込まれないようにしている。
PLACEHOLDER_RE = re.compile(r"\{(PROJECT_NAME|REPO_SLUG|CRON|CRON_HUMAN)\}")

DEFAULT_CRON = "0 */6 * * *"
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ── 純粋関数（外部接点を持たない）─────────────────────────────────


def describe_cron(cron: str) -> str:
    """cron 式を人が読める間隔表現へ変換する。解釈できない式はそのまま返す。

    時フィールドの解釈は `check_routine_gate_window.parse_cron_interval_hours` を再利用する
    （`*` は毎時・`*/N` は N 時間ごと・それ以外は解釈しない）。同じ解釈を 2 か所に持つと、
    対応範囲を片方だけ広げたときに週次ゲートの整合検査と本ツールの可読表現が食い違う。
    解釈できない式（リスト表記・範囲表記）は、誤った日本語を作るより cron 式をそのまま見せる。
    """
    interval = parse_cron_interval_hours(cron)
    if interval is None:
        return f"`{cron}`"
    if 24 % interval != 0:
        # 日付をまたぐところで間隔が不均等になる（例 */5 は 20 時 → 翌 0 時が 4 時間）。
        # 回数を断定せず間隔だけ述べる。
        return f"{interval} 時間ごと"
    return f"{interval} 時間ごと・1 日 {24 // interval} 回"


def render(text: str, values: dict[str, str]) -> str:
    """プレースホルダを置換する。未知のキーは置換しない（後段の検査で落とす）。"""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def unresolved_placeholders(text: str, values: dict[str, str] | None = None) -> list[str]:
    """`text` のうち `values` で解決できないプレースホルダ名を重複なく返す（出現順）。

    🔴 渡すのは **置換前** のテキストである。置換後のテキストを再スキャンする実装にすると、
    値そのものに `{CRON}` のような綴りが含まれていたとき（例 `--name '{CRON} Bot'`）に、
    正しく置換できているのに「未置換が残った」と誤判定して生成全体を失敗させる。
    """
    values = values or {}
    seen: list[str] = []
    for m in PLACEHOLDER_RE.finditer(text):
        name = m.group(1)
        if name not in values and name not in seen:
            seen.append(name)
    return seen


def parse_label_specs(yaml_text: str) -> list[dict[str, str]]:
    """labels.yaml の `labels:` を検証しつつ読み出す。

    name / color / description の 3 キーが揃っていない要素は握りつぶさず ValueError にする
    （壊れた定義から中途半端なラベルを作ると、あとで手作業の照合が必要になる）。
    YAML 自体が壊れている場合も同じ ValueError に包む — 呼び出し側へ生の `yaml.YAMLError` が
    抜けると、他のエラー（整形して exit 2）と違いトレースバックで落ちる。
    """
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"labels.yaml を YAML として読めません: {exc}") from exc
    raw = data.get("labels")
    if not isinstance(raw, list) or not raw:
        raise ValueError("labels.yaml に labels: のリストがありません")
    specs: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"labels[{i}] がマッピングではありません")
        missing = [k for k in ("name", "color", "description") if not item.get(k)]
        if missing:
            raise ValueError(f"labels[{i}] にキーがありません: {', '.join(missing)}")
        specs.append({k: str(item[k]) for k in ("name", "color", "description")})
    return specs


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def label_commands(specs: list[dict[str, str]], slug: str) -> list[str]:
    """`gh label create` のコマンド列を組み立てる（--force で再実行しても失敗しない形）。"""
    return [
        "gh label create {name} --color {color} --description {desc} -R {slug} --force".format(
            name=_shell_quote(s["name"]),
            color=_shell_quote(s["color"]),
            desc=_shell_quote(s["description"]),
            slug=_shell_quote(slug),
        )
        for s in specs
    ]


def build_values(project_name: str, slug: str, cron: str) -> dict[str, str]:
    return {
        "PROJECT_NAME": project_name,
        "REPO_SLUG": slug,
        "CRON": cron,
        "CRON_HUMAN": describe_cron(cron),
    }


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug))


# ── 外部接点（ファイル・標準出力）───────────────────────────────


def _read_templates(template_dir: Path) -> dict[str, str]:
    missing = [name for name in TEMPLATE_MAP if not (template_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"雛形が見つかりません（{template_dir}）: {', '.join(sorted(missing))}"
        )
    return {name: (template_dir / name).read_text(encoding="utf-8") for name in TEMPLATE_MAP}


def _write_outputs(out_root: Path, rendered: dict[str, str], force: bool) -> tuple[list[str], list[str]]:
    written: list[str] = []
    skipped: list[str] = []
    for name, dest_rel in TEMPLATE_MAP.items():
        dest = out_root / dest_rel
        if dest.exists() and not force:
            skipped.append(dest_rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered[name], encoding="utf-8")
        written.append(dest_rel)
    return written, skipped


def _run_self_test() -> int:
    cases: list[tuple[str, bool]] = []

    def check(label: str, ok: bool) -> None:
        cases.append((label, bool(ok)))

    # describe_cron: 解釈できる式 / できない式
    check("cron 6h", describe_cron("0 */6 * * *") == "6 時間ごと・1 日 4 回")
    check("cron hourly", describe_cron("0 * * * *") == "1 時間ごと・1 日 24 回")
    check("cron 5h は回数を断定しない", describe_cron("0 */5 * * *") == "5 時間ごと")
    check("cron リスト表記は変換しない", describe_cron("0 0,6,12 * * *") == "`0 0,6,12 * * *`")
    check("フィールド数が違う式は変換しない", describe_cron("0 */6") == "`0 */6`")

    # render / unresolved_placeholders
    values = build_values("Sample Product", "example-owner/sample-product", DEFAULT_CRON)
    rendered = render("{PROJECT_NAME} / {REPO_SLUG} / {CRON} / {CRON_HUMAN}", values)
    check(
        "render が 4 つとも置換する",
        rendered == "Sample Product / example-owner/sample-product / 0 */6 * * * / 6 時間ごと・1 日 4 回",
    )
    check("全キーが揃えば未解決なし", unresolved_placeholders("{PROJECT_NAME} {CRON}", values) == [])
    check("values に無いキーは未解決として返す", unresolved_placeholders("{CRON}", {}) == ["CRON"])
    check("重複は 1 回だけ報告する", unresolved_placeholders("{CRON} {CRON}", {}) == ["CRON"])
    check("似た綴りは拾わない", unresolved_placeholders("{PROJECT}", {}) == [])
    # 値そのものがプレースホルダの綴りを含んでも誤検知しない（置換後を再スキャンしない設計）
    tricky = build_values("{CRON} Bot", "example-owner/sample-product", DEFAULT_CRON)
    check("値に {CRON} を含む名前でも未解決ゼロ", unresolved_placeholders("{PROJECT_NAME}", tricky) == [])
    check("その置換結果はプレースホルダ綴りを保つ", render("{PROJECT_NAME}", tricky) == "{CRON} Bot")

    # parse_label_specs: 正常系と fail-closed
    ok_yaml = (
        "labels:\n"
        "  - name: 'status:in-progress'\n"
        "    color: 'fef2c0'\n"
        "    description: '着手中'\n"
    )
    specs = parse_label_specs(ok_yaml)
    check("labels を 1 件読める", len(specs) == 1 and specs[0]["name"] == "status:in-progress")
    for bad, why in (
        ("labels: []\n", "空リスト"),
        ("other: 1\n", "labels キー無し"),
        ("labels:\n  - name: 'x'\n    color: 'fff'\n", "description 欠落"),
        ("labels:\n  - 'x'\n", "マッピングでない"),
        ("labels: [foo: bar: baz\n", "YAML として壊れている"),
    ):
        raised = False
        try:
            parse_label_specs(bad)
        except ValueError:
            raised = True
        check(f"壊れた labels を拒否する（{why}）", raised)

    # label_commands: クォートと slug の埋め込み
    cmds = label_commands(specs, "example-owner/sample-product")
    check("コマンドは 1 件", len(cmds) == 1)
    check("ラベル名がクォートされる", "'status:in-progress'" in cmds[0])
    check("slug が入る", "'example-owner/sample-product'" in cmds[0])
    quoted = label_commands([{"name": "it's", "color": "fff", "description": "d"}], "o/r")
    check("シングルクォートを含む名前を壊さない", "'it'\\''s'" in quoted[0])

    # is_valid_slug
    check("slug 正常", is_valid_slug("example-owner/sample-product"))
    check("slug にスラッシュ 1 個が必要", not is_valid_slug("sample-product"))
    check("slug にスペースは入らない", not is_valid_slug("example owner/sample"))

    # 雛形側の不変条件: TEMPLATE_MAP が空だと空のゲートになる
    check("TEMPLATE_MAP が空でない", len(TEMPLATE_MAP) > 0)

    if not cases:
        print("ERROR: self-test のケースが 0 件です（fail-closed）", file=sys.stderr)
        return 2
    failed = [label for label, ok in cases if not ok]
    for label, ok in cases:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(cases) - len(failed)}/{len(cases)} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="下流プロダクトの自律運転設定一式を生成する")
    parser.add_argument("--out", help="生成先リポジトリの作業ツリー")
    parser.add_argument("--name", help="プロダクト名（雛形の {PROJECT_NAME}）")
    parser.add_argument("--slug", help="owner/repo 形式のリポジトリ slug（雛形の {REPO_SLUG}）")
    parser.add_argument("--cron", default=DEFAULT_CRON, help=f"ルーティンの cron 式（既定: {DEFAULT_CRON}）")
    parser.add_argument("--force", action="store_true", help="生成先の既存ファイルを上書きする")
    parser.add_argument("--dry-run", action="store_true", help="書き出さずに生成計画だけ表示する")
    parser.add_argument(
        "--print-label-commands",
        action="store_true",
        help="ラベル作成の gh コマンド列だけを出力する（--slug が必要）",
    )
    parser.add_argument(
        "--labels-file",
        help="ラベル定義の出典（既定: 雛形の labels.yaml。生成先リポジトリでは config/labels.yaml を渡す）",
    )
    parser.add_argument("--template-dir", default=str(TEMPLATE_DIR), help="雛形ディレクトリ")
    parser.add_argument("--self-test", action="store_true", help="純粋関数の自己テスト")
    args = parser.parse_args()

    if args.self_test:
        return _run_self_test()

    if not TEMPLATE_MAP:
        print("ERROR: TEMPLATE_MAP が空です（生成対象ゼロ・fail-closed）", file=sys.stderr)
        return 2

    template_dir = Path(args.template_dir)

    if args.print_label_commands:
        if not args.slug or not is_valid_slug(args.slug):
            print("ERROR: --print-label-commands には owner/repo 形式の --slug が必要です", file=sys.stderr)
            return 2
        # 出典は明示指定 > 雛形。生成先リポジトリには雛形が同期されないため、そちらでは
        # 生成済みの config/labels.yaml を --labels-file で渡して実行する。
        labels_path = Path(args.labels_file) if args.labels_file else template_dir / "labels.yaml"
        if not labels_path.is_file():
            print(f"ERROR: ラベル定義が見つかりません: {labels_path}", file=sys.stderr)
            return 2
        try:
            specs = parse_label_specs(labels_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for cmd in label_commands(specs, args.slug):
            print(cmd)
        return 0

    try:
        templates = _read_templates(template_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    missing_args = [flag for flag, val in (("--out", args.out), ("--name", args.name), ("--slug", args.slug)) if not val]
    if missing_args:
        print(f"ERROR: 生成には次の引数が要ります: {', '.join(missing_args)}", file=sys.stderr)
        return 2
    if not is_valid_slug(args.slug):
        print(f"ERROR: --slug は owner/repo 形式で指定してください（受け取った値: {args.slug}）", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    if not out_root.is_dir():
        print(f"ERROR: 生成先が存在しません: {out_root}", file=sys.stderr)
        return 2

    values = build_values(args.name, args.slug, args.cron)
    rendered: dict[str, str] = {}
    for name, text in templates.items():
        # 検査するのは置換前の雛形。置換後を見ると、値自身が `{CRON}` 等を含むときに誤検知する。
        left = unresolved_placeholders(text, values)
        if left:
            print(
                f"ERROR: {name} に解決できないプレースホルダがあります: {', '.join(left)}（何も書き出していません）",
                file=sys.stderr,
            )
            return 1
        rendered[name] = render(text, values)

    if args.dry_run:
        print(f"[dry-run] 生成先: {out_root}")
        for name, dest_rel in TEMPLATE_MAP.items():
            state = "上書き" if (out_root / dest_rel).exists() else "新規"
            print(f"[dry-run]   {name} → {dest_rel}（{state}）")
        return 0

    written, skipped = _write_outputs(out_root, rendered, args.force)
    for dest_rel in written:
        print(f"[generated] {dest_rel}")
    for dest_rel in skipped:
        print(f"[skipped] {dest_rel}（既存。上書きするなら --force）")
    if written:
        print(
            "\n次の操作は docs/setup/routines-setup.md を読む（ラベル作成 → ルーティン作成 →"
            " sources / 環境 / コネクターの設定 → 初回 run の確認）"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
