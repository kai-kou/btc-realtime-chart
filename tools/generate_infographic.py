#!/usr/bin/env python3
"""グラレコ風インフォグラフィック生成パイプライン（D-24 / D-25・M-2 オプトイン機能・Issue #121）。

`open-questions.md` D-24 の決定により、co-location できる図は Mermaid を既定 ON にする一方、
一枚絵として訴求力が要る「グラレコ風」画像は生成 AI 画像 API（OPENAI_API_KEY）を要求するため
**既定 OFF のオプトイン機能** として実装する。gem-hunter の 4 層パイプライン
（md → specs/*.json → prompts/*.txt → .webp）はそのまま移植せず（specs/*.json が第 2 の正本に
なるため D-24 で却下済み）、md から直接プロンプトを組み立てて生成し、元 md の内容ハッシュを
画像の sidecar spec（<output>.spec.json）に埋めて fail-closed で鮮度判定する（D-25 の M-2 段階）。

opt-in の判定は config/infographic.yaml の enabled フラグと targets への明示登録の 2 段構え。
どちらか片方が欠けても generate は拒否する（黙って動かない・fail-closed）。

Usage:
  python3 tools/generate_infographic.py check --target docs/foo.md   # 鮮度ゲート（fail-closed・単体）
  python3 tools/generate_infographic.py check --all                  # config 登録済み全対象を検査
  python3 tools/generate_infographic.py generate --target docs/foo.md  # 生成（要 opt-in + OPENAI_API_KEY）
  python3 tools/generate_infographic.py --self-test                  # 単体テスト（実 API 呼び出しなし）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - requirements.txt が PyYAML を必須にしている
    yaml = None

JST = timezone(timedelta(hours=9))
DEFAULT_CONFIG_PATH = "config/infographic.yaml"
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
OPENAI_MODEL = "gpt-image-1"
PROMPT_EXCERPT_LIMIT = 2000


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".spec.json")


_FALSY_STRINGS = {"false", "no", "0", "off", ""}


def _coerce_enabled(raw: object) -> bool:
    """YAML の `enabled: "false"`（クォート付き文字列）を誤って True にしない。

    唯一の opt-in ゲートを bool() の素通し（文字列は空文字以外すべて truthy）に
    任せると、クォートしただけで無効化のつもりが有効化されてしまう（fail-open）。
    """
    if isinstance(raw, str):
        return raw.strip().lower() not in _FALSY_STRINGS
    return bool(raw)


def _valid_target(t: dict) -> bool:
    return isinstance(t, dict) and isinstance(t.get("source"), str) and isinstance(t.get("output"), str)


@dataclass
class InfographicConfig:
    enabled: bool = False
    targets: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "InfographicConfig":
        if not path.exists():
            return cls(enabled=False, targets=[])
        if yaml is None:
            raise RuntimeError("PyYAML が未インストールです（requirements.txt を確認してください）")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            enabled=_coerce_enabled(data.get("enabled", False)),
            targets=list(data.get("targets") or []),
        )

    def find_target(self, source: str) -> dict | None:
        for t in self.targets:
            if _valid_target(t) and t.get("source") == source:
                return t
        return None


def check_freshness(source: Path, output: Path) -> tuple[bool, str]:
    """鮮度を fail-closed で判定する。判定に必要な情報が欠けていたら stale 扱いにする（D-25）。"""
    if not source.exists():
        return False, f"元 md が存在しません: {source}"
    spec_file = spec_path_for(output)
    if not spec_file.exists():
        return False, f"spec ファイルが存在しません（未生成 or 破損）: {spec_file}"
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"spec の解析に失敗しました（fail-closed）: {e}"
    stored_hash = spec.get("source_sha256")
    if not stored_hash:
        return False, "spec に source_sha256 が記録されていません（fail-closed）"
    current_hash = content_hash(source)
    if stored_hash != current_hash:
        return False, f"元 md の内容ハッシュが変わっています（stale）: {stored_hash[:12]} != {current_hash[:12]}"
    if not output.exists():
        return False, f"画像ファイルが存在しません: {output}"
    return True, "鮮度 OK"


def build_prompt(source_text: str) -> str:
    """md 本文からグラレコ風一枚絵生成用のプロンプトを組み立てる（長文は先頭で打ち切る）。"""
    excerpt = source_text.strip()
    if len(excerpt) > PROMPT_EXCERPT_LIMIT:
        excerpt = excerpt[:PROMPT_EXCERPT_LIMIT] + "…"
    return (
        "手描き風グラフィックレコーディング（グラレコ）のスタイルで、"
        "以下のドキュメントの要点を 1 枚のイラストにまとめてください。"
        "アイコン・矢印・手書き文字風の装飾を使い、訴求力のある一枚絵にしてください。\n\n"
        f"---\n{excerpt}\n---"
    )


def _extract_image_bytes(body: dict) -> bytes:
    """API レスポンス JSON から画像バイト列を取り出す（ネットワーク I/O を含まない・単体テスト可能）。"""
    b64 = body["data"][0]["b64_json"]
    if not b64:
        raise ValueError("API レスポンスの b64_json が空です")
    return base64.b64decode(b64)


def call_openai_image_api(prompt: str, api_key: str) -> bytes:
    """OpenAI Images API を呼び出し、生成画像のバイト列を返す。"""
    payload = json.dumps({
        "model": OPENAI_MODEL,
        "prompt": prompt,
        "size": "1024x1024",
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_IMAGES_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return _extract_image_bytes(body)


def generate(source: Path, output: Path, config: InfographicConfig, api_key: str | None) -> int:
    if not config.enabled:
        print(
            "[NG] グラレコ風インフォグラフィック生成は既定 OFF のオプトイン機能です。"
            f"{DEFAULT_CONFIG_PATH} の enabled を true にし、targets へ対象を登録してから実行してください（D-24）。",
            file=sys.stderr,
        )
        return 1
    if not api_key:
        print(
            "[NG] OPENAI_API_KEY が未設定です。生成 AI 画像 API のアカウント設定はユーザー操作が必要です"
            "（A-6）。API キーを取得しセッション環境変数へ設定してから再実行してください。",
            file=sys.stderr,
        )
        return 1
    if not source.exists():
        print(f"[NG] 元 md が見つかりません: {source}", file=sys.stderr)
        return 1

    prompt = build_prompt(source.read_text(encoding="utf-8"))
    try:
        image_bytes = call_openai_image_api(prompt, api_key)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, ValueError) as e:
        print(f"[NG] 画像生成 API 呼び出しに失敗しました: {e}", file=sys.stderr)
        return 1
    if not image_bytes:
        print("[NG] 画像生成 API から空の画像データが返されました", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image_bytes)
    spec_path_for(output).write_text(
        json.dumps({
            "source": str(source),
            "source_sha256": content_hash(source),
            "model": OPENAI_MODEL,
            "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M %Z"),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] 生成しました: {output}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="実 API 呼び出しなしの単体テストを実行する")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="opt-in 設定ファイルのパス")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="鮮度ゲート（fail-closed）")
    p_check.add_argument("--target", help="対象 md のパス（省略時は --all と併用）")
    p_check.add_argument("--all", action="store_true", help="config 登録済み全対象を検査する")

    p_gen = sub.add_parser("generate", help="グラレコ風画像を生成する（要 opt-in + OPENAI_API_KEY）")
    p_gen.add_argument("--target", required=True, help="対象 md のパス（config の targets に登録済みであること）")

    args = parser.parse_args()

    if args.self_test:
        sys.exit(_run_self_test())

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    config = InfographicConfig.load(Path(args.config))

    if args.command == "check":
        if not args.target and not args.all:
            parser.error("check には --target か --all のどちらかを指定してください")
        targets = config.targets
        if args.target:
            t = config.find_target(args.target)
            if t is None:
                print(f"[NG] config に未登録の対象です（fail-closed）: {args.target}", file=sys.stderr)
                sys.exit(1)
            targets = [t]
        if not targets:
            print("[OK] 検査対象がありません（config 未登録）")
            sys.exit(0)
        ok_all = True
        for t in targets:
            if not _valid_target(t):
                print(f"[NG] source/output を欠く不正な target です（fail-closed）: {t}", file=sys.stderr)
                ok_all = False
                continue
            ok, msg = check_freshness(Path(t["source"]), Path(t["output"]))
            print(f"{'[OK]' if ok else '[NG]'} {t['source']}: {msg}")
            ok_all = ok_all and ok
        sys.exit(0 if ok_all else 1)

    if args.command == "generate":
        t = config.find_target(args.target)
        if t is None:
            print(
                f"[NG] config の targets に未登録です。先に {args.config} へ source/output を登録してください: {args.target}",
                file=sys.stderr,
            )
            sys.exit(1)
        api_key = os.environ.get("OPENAI_API_KEY")
        sys.exit(generate(Path(t["source"]), Path(t["output"]), config, api_key))


# --- 自己テスト（実 API 呼び出しなし） ---

def _run_self_test() -> int:
    cases = [
        _case_hash_stable,
        _case_freshness_missing_spec_fail_closed,
        _case_freshness_hash_mismatch_stale,
        _case_freshness_fresh,
        _case_generate_refuses_when_disabled,
        _case_generate_refuses_without_api_key,
        _case_config_load_default_disabled,
        _case_config_load_parses_enabled_targets,
        _case_coerce_enabled_quoted_string_false,
        _case_build_prompt_truncates_long_text,
        _case_extract_image_bytes_rejects_empty_data,
        _case_extract_image_bytes_rejects_empty_b64,
        _case_find_target_skips_malformed_entry,
    ]
    failed = 0
    for case_fn in cases:
        desc, passed = case_fn()
        print(f"{'PASS' if passed else 'FAIL'}: {desc}")
        if not passed:
            failed += 1
    if failed:
        print(f"\n{failed} 件失敗", file=sys.stderr)
        return 1
    print("\n全件 PASS")
    return 0


def _case_hash_stable() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "a.md"
        p.write_text("同じ内容", encoding="utf-8")
        h1 = content_hash(p)
        h2 = content_hash(p)
        p.write_text("違う内容", encoding="utf-8")
        h3 = content_hash(p)
        ok = h1 == h2 and h1 != h3
        return "同一内容は同一ハッシュ・内容変更でハッシュも変わる", ok


def _case_freshness_missing_spec_fail_closed() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        source = Path(t) / "a.md"
        source.write_text("本文", encoding="utf-8")
        output = Path(t) / "a.webp"
        output.write_bytes(b"dummy")
        ok, _ = check_freshness(source, output)
        return "spec 未生成は fail-closed（stale 扱い）", ok is False


def _case_freshness_hash_mismatch_stale() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        source = Path(t) / "a.md"
        source.write_text("本文 v1", encoding="utf-8")
        output = Path(t) / "a.webp"
        output.write_bytes(b"dummy")
        spec_path_for(output).write_text(json.dumps({"source_sha256": "0" * 64}), encoding="utf-8")
        source.write_text("本文 v2（変更済み）", encoding="utf-8")
        ok, _ = check_freshness(source, output)
        return "元 md 変更後はハッシュ不一致で stale", ok is False


def _case_freshness_fresh() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        source = Path(t) / "a.md"
        source.write_text("本文", encoding="utf-8")
        output = Path(t) / "a.webp"
        output.write_bytes(b"dummy")
        spec_path_for(output).write_text(
            json.dumps({"source_sha256": content_hash(source)}), encoding="utf-8"
        )
        ok, _ = check_freshness(source, output)
        return "ハッシュ一致・画像存在で fresh", ok is True


def _case_generate_refuses_when_disabled() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        source = Path(t) / "a.md"
        source.write_text("本文", encoding="utf-8")
        output = Path(t) / "a.webp"
        config = InfographicConfig(enabled=False, targets=[])
        rc = generate(source, output, config, api_key="dummy")
        return "opt-in 無効時は生成を拒否する（既定 OFF・D-24）", rc == 1 and not output.exists()


def _case_generate_refuses_without_api_key() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        source = Path(t) / "a.md"
        source.write_text("本文", encoding="utf-8")
        output = Path(t) / "a.webp"
        config = InfographicConfig(enabled=True, targets=[])
        rc = generate(source, output, config, api_key=None)
        return "OPENAI_API_KEY 未設定時は生成を拒否する（A-6）", rc == 1 and not output.exists()


def _case_config_load_default_disabled() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        cfg = InfographicConfig.load(Path(t) / "nonexistent.yaml")
        return "config 未配置時は enabled=False を既定にする（fail-closed）", cfg.enabled is False


def _case_config_load_parses_enabled_targets() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        cfg_path = Path(t) / "infographic.yaml"
        cfg_path.write_text(
            "enabled: true\ntargets:\n  - source: docs/x.md\n    output: docs/x.webp\n",
            encoding="utf-8",
        )
        cfg = InfographicConfig.load(cfg_path)
        found = cfg.find_target("docs/x.md")
        ok = cfg.enabled is True and found is not None and found["output"] == "docs/x.webp"
        return "実際の yaml から enabled/targets を正しく読める", ok


def _case_coerce_enabled_quoted_string_false() -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as t:
        cfg_path = Path(t) / "infographic.yaml"
        # YAML でクォートすると文字列になる。bool("false") は True になるため素通しでは fail-open する。
        cfg_path.write_text('enabled: "false"\ntargets: []\n', encoding="utf-8")
        cfg = InfographicConfig.load(cfg_path)
        return 'enabled: "false"（クォート付き文字列）は無効化として扱う（fail-open 防止）', cfg.enabled is False


def _case_build_prompt_truncates_long_text() -> tuple[str, bool]:
    long_text = "あ" * (PROMPT_EXCERPT_LIMIT + 100)
    prompt = build_prompt(long_text)
    ok = "あ" * PROMPT_EXCERPT_LIMIT in prompt and "…" in prompt and long_text not in prompt
    return f"{PROMPT_EXCERPT_LIMIT} 文字超のテキストは打ち切られる", ok


def _case_extract_image_bytes_rejects_empty_data() -> tuple[str, bool]:
    try:
        _extract_image_bytes({"data": []})
        return "data が空配列なら IndexError で拒否する（生の未処理例外にしない）", False
    except IndexError:
        return "data が空配列なら IndexError で拒否する（生の未処理例外にしない）", True


def _case_extract_image_bytes_rejects_empty_b64() -> tuple[str, bool]:
    try:
        _extract_image_bytes({"data": [{"b64_json": ""}]})
        return "b64_json が空文字なら ValueError で拒否する（0 バイト画像を書き込まない）", False
    except ValueError:
        return "b64_json が空文字なら ValueError で拒否する（0 バイト画像を書き込まない）", True


def _case_find_target_skips_malformed_entry() -> tuple[str, bool]:
    config = InfographicConfig(enabled=True, targets=[{"source": "docs/x.md"}])  # output キー欠落
    return "source/output のいずれか欠落した target は fail-closed で未登録扱いにする", config.find_target("docs/x.md") is None


if __name__ == "__main__":
    main()
