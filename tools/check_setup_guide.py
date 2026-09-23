#!/usr/bin/env python3
"""設定手順書（C-5 の設定案内）を機械検査する（`FR-15`・Issue #13）。

`setup-guide` スキルが生成する手順書と、`docs/autonomy-templates/routines-setup.md`（#110）が
共通の規律を満たしているかを判定する。人が読んで確認する運用だと、手順が 10 を超えたあたりで
「完了条件の書き忘れ」「実在ドメインの混入」を必ず見落とすため、ここで fail-closed にする。

## 検査する規律

1. **各工程に確認可能な完了条件がある**: 工程見出し（`## 1. 〜` / `### 3. 〜` /
   `## 工程 2/6: 〜`）の配下に `**完了条件**` 行が 1 つ以上ある
2. **入力値は架空値だけ**（`FR-15`）: コードブロック・インラインコード（＝ユーザーが貼り付ける値）
   に現れる URL のホスト・メールアドレスのドメイン・IPv4 アドレス・12 桁のアカウント ID が、
   予約済みの例示値（RFC 2606 / RFC 6761 のドメイン・RFC 5737 の文書用アドレス等）に限られる。
   Markdown リンク記法の URL（公式ドキュメントへの案内）は入力値ではないため対象外
3. **クレデンシャル形状の混入が無い**: 本文全体を、公開レーン第 4 層
   （`scripts/publish-snapshot.sh` の `CREDENTIAL_SHAPE_PATTERNS`）と **同じパターン** で照合する。
   母艦では同ファイルから読み込み、本ファイルの写し（`EMBEDDED_SHAPES`）と食い違えば exit 2
   （ドリフト）にする。配布物・下流には `publish-snapshot.sh` が配られない（`PUBLISH_DENYLIST`）ため、
   そこでは写しだけで検査する（配布先で常に判定不能になり規律が機能しない事態を避ける）

## 層構造について（`implementation-discipline.md` §1）

外部接点（ファイル読み込み・標準出力）は `main()` と `_read` に閉じ、見出し解析・コード抽出・
値の判定は純粋関数にしている。`--self-test` は純粋関数だけを検証するのでファイルを触らない。

## 既知の限界（隠さない）

- 架空値判定の対象は URL・スキーム無しドメイン（末尾が一般的な TLD のもの）・メール・IPv4・12 桁数字だけ。組織名・人名のような固有名詞は
  形で判定できないため、`REVIEW.md` のセルフレビュー項目（著者規律）が担う
- コードブロック外の地の文に書かれた実在ドメインは検査しない（公式ページの説明で固有名が
  出るのは正当なため）。入力させる値は必ずコードブロックで渡す書式（`interactive-guide` 2-3）が前提
- `{PLACEHOLDER}` / `<placeholder>` を含む値は未確定の差し込み口として判定から外す

終了コード: 0 = 違反なし / 1 = 違反あり（工程見出しが 0 件の手順書を含む）/
2 = 判定不能（形状定義が読めない・原本と写しが食い違う・対象のいずれかが見つからない）

Usage:
  python3 tools/check_setup_guide.py docs/setup/
  python3 tools/check_setup_guide.py docs/autonomy-templates/routines-setup.md .claude/skills/setup-guide/guide-template.md
  python3 tools/check_setup_guide.py --self-test
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_SCRIPT = REPO_ROOT / "scripts" / "publish-snapshot.sh"

# 工程見出し: `## 1. 名前` / `### 12. 名前` / `## 工程 2/6: 名前`（interactive-guide の書式）。
# `## 2〜5. まとめ見出し` のような範囲見出しは工程ではない（配下の各工程が完了条件を持つ）。
# `### 1.1 詳細` のような小節番号は工程ではない（区切りの後に空白を要求して除外する）。
STEP_HEADING = re.compile(r"^(#{2,4})\s+(?:工程\s*)?\d+(?:/\d+)?\s*(?:[.:]\s+|：\s*)\S")
ANY_HEADING = re.compile(r"^(#{1,6})\s")
COMPLETION_MARK = "**完了条件**"

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
INDENTED_CODE = re.compile(r"^(?: {4}|\t)")

URL_FULL = re.compile(r"https?://\S+", re.IGNORECASE)
URL_HOST = re.compile(r"https?://([^/\s:?#'\"`)>\]]+)", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
# スキーム無しのドメイン（DNS レコード・ホスト名入力）。`tools/x.py` のようなパスの一部は前置の `/` で除外し、
# `os.path` のような識別子を拾わないよう、末尾ラベルが一般的な TLD か予約 TLD のものだけを対象にする。
BARE_DOMAIN = re.compile(r"(?<![\w@/.-])((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,}))\.?(?![\w-])")
COMMON_TLDS = frozenset(
    "com net org edu gov io co jp dev app ai cloud info biz me us uk de fr cn kr tw xyz tech site online "
    "page run sh zz".split()
)
ACCOUNT_ID = re.compile(r"(?<!\d)(\d{12})(?!\d)")

# 公開レーン第 4 層（scripts/publish-snapshot.sh の CREDENTIAL_SHAPE_NAMES / _PATTERNS）の写し。
# publish-snapshot.sh は配布物へ配られないため写しを持つ。母艦では main() が原本と突き合わせ、
# 食い違えば判定不能（exit 2）にする＝片側だけの更新を機械的に捕まえる。
EMBEDDED_SHAPES: tuple[tuple[str, str], ...] = (
    ("AWS Access Key ID", r"AKIA[0-9A-Z]{16}"),
    ("GitHub Personal Access Token（classic）", r"ghp_[A-Za-z0-9]{36}"),
    ("GitHub OAuth Token", r"gho_[A-Za-z0-9]{36}"),
    ("GitHub App Token", r"gh[us]_[A-Za-z0-9]{36}"),
    ("GitHub Fine-grained PAT", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("Slack Token", r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{20,32}"),
    ("Slack Incoming Webhook URL", r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"),
    ("Private Key Block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Google API Key", r"AIza[0-9A-Za-z_-]{35}"),
    ("Stripe API Key", r"[sp]k_(live|test)_[A-Za-z0-9]{24,}"),
    ("OpenAI API Key", r"sk-[A-Za-z0-9_-]{32,}"),
    ("Anthropic API Key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("npm Access Token", r"npm_[A-Za-z0-9]{36}"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)

# RFC 2606 / RFC 6761 の予約ドメイン。
# example.edu は RFC 2606 外だが IANA 保有の例示ドメインで、autonomy-templates/README.md の著者規律と揃える。
RESERVED_SECOND_LEVEL = ("example.com", "example.net", "example.org", "example.edu")
RESERVED_TLDS = ("example", "test", "invalid", "localhost")
# RFC 5737（文書用）・ループバック・未指定アドレス。
FICTITIOUS_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8", "0.0.0.0/32")
)
# 12 桁のアカウント ID として許す例示値（AWS 公式ドキュメントの例示 ID と全桁ゼロ）。
FICTITIOUS_ACCOUNT_IDS = ("123456789012", "000000000000")


def is_placeholder(value: str) -> bool:
    return "{" in value or "<" in value or "}" in value or ">" in value


def is_reserved_domain(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h == "localhost":
        return True
    if h.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return True
    return any(h == d or h.endswith("." + d) for d in RESERVED_SECOND_LEVEL)


def is_fictitious_ipv4(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # 999.1.1.1 のような非アドレス（バージョン番号等）は判定対象外
    return any(ip in net for net in FICTITIOUS_NETWORKS)


def fenced_lines(text: str) -> list[tuple[str, bool]]:
    """各行を (行, コードブロック内か) で返す。フェンス行自体は内側扱い（地の文として解析しない）。
    閉じは開きと同じ記号で長さが開き以上の行だけ（CommonMark）。入れ子の別種・短いフェンスで
    開閉が反転し、内側の値が検査から漏れる fail-open を防ぐ。"""
    out: list[tuple[str, bool]] = []
    opener: str | None = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if opener is None:
            if m:
                opener = m.group(1)
                out.append((line, True))
            else:
                out.append((line, False))
            continue
        is_close = bool(m) and m.group(1)[0] == opener[0] and len(m.group(1)) >= len(opener) \
            and line.strip() == m.group(1)
        out.append((line, True))
        if is_close:
            opener = None
    return out


def extract_code_spans(text: str) -> list[tuple[int, str]]:
    """コードブロックの各行とインラインコードを (行番号, 文字列) で返す（行番号は 1 始まり）。"""
    spans: list[tuple[int, str]] = []
    prev_blank = True
    in_indented = False
    for no, (line, in_fence) in enumerate(fenced_lines(text), start=1):
        if in_fence:
            if not FENCE.match(line):
                spans.append((no, line))
            prev_blank = in_indented = False
            continue
        # 字下げコードブロック（空行の直後から 4 スペース / タブで始まる行の連なり・CommonMark）。
        # リストの継続行を拾う誤検知は起こりうるが、値を検査する側（fail-closed）に倒す。
        if line.strip() and INDENTED_CODE.match(line) and (prev_blank or in_indented):
            in_indented = True
            spans.append((no, line))
        else:
            in_indented = in_indented and not line.strip()
            spans.extend((no, m.group(1)) for m in INLINE_CODE.finditer(line))
        prev_blank = not line.strip()
    return spans


def find_steps(text: str) -> list[tuple[int, str, str]]:
    """工程見出しごとに (行番号, 見出し, 配下本文) を返す。コードブロック内の `#` は見出しとみなさない。"""
    lines = text.splitlines()
    heads: list[tuple[int, int, bool]] = []  # (index, level, is_step)
    for i, (line, in_fence) in enumerate(fenced_lines(text)):
        if in_fence:
            continue
        m = ANY_HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), bool(STEP_HEADING.match(line))))
    steps: list[tuple[int, str, str]] = []
    for k, (i, level, is_step) in enumerate(heads):
        if not is_step:
            continue
        end = len(lines)
        for j, lv, nxt_is_step in heads[k + 1:]:
            # 下位レベルでも工程見出しなら別工程（その完了条件を親工程に数えない）。
            if lv <= level or nxt_is_step:
                end = j
                break
        steps.append((i + 1, lines[i].strip(), "\n".join(lines[i + 1:end])))
    return steps


def check_completion_conditions(text: str) -> list[str]:
    if not find_steps(text):
        # 見出し書式のずれ（`## ステップ1` 等）で工程を 1 つも拾えないまま合格にしない（fail-closed）。
        return ["L1: 工程見出しが 1 つも無い（`## 1. 工程名` か `## 工程 1/N: 工程名` の書式で書く）"]
    return [
        f"L{no}: 工程「{head}」に `{COMPLETION_MARK}` 行が無い（ユーザーが自分で確かめられる観測を 1 行書く）"
        for no, head, body in find_steps(text)
        if COMPLETION_MARK not in body
    ]


def check_fictitious_values(text: str) -> list[str]:
    errs: list[str] = []
    for no, span in extract_code_spans(text):
        for host in URL_HOST.findall(span):
            if not is_placeholder(host) and not is_reserved_domain(host):
                errs.append(f"L{no}: 入力値の URL ホスト `{host}` が予約済みの例示ドメインではない（example.com 等を使う）")
        rest = EMAIL.sub(" ", URL_FULL.sub(" ", span))
        for domain, tld in BARE_DOMAIN.findall(rest):
            if tld.lower() in COMMON_TLDS | set(RESERVED_TLDS) and not is_reserved_domain(domain):
                errs.append(f"L{no}: 入力値のドメイン `{domain}` が予約済みの例示ドメインではない")
        for domain in EMAIL.findall(span):
            if not is_placeholder(domain) and not is_reserved_domain(domain):
                errs.append(f"L{no}: 入力値のメールドメイン `{domain}` が予約済みの例示ドメインではない")
        for addr in IPV4.findall(span):
            if not is_fictitious_ipv4(addr):
                errs.append(f"L{no}: 入力値の IPv4 `{addr}` が文書用アドレス（192.0.2.0/24 等）ではない")
        for acct in ACCOUNT_ID.findall(span):
            if acct not in FICTITIOUS_ACCOUNT_IDS:
                errs.append(f"L{no}: 12 桁の ID `{acct}` は例示値（123456789012）ではない")
    return errs


def _strip_comment(line: str, quote: str) -> str:
    """クォート外の最初の `#` 以降（行末コメント）を切り落とす。コメント中のクォート文字列を
    配列要素として誤抽出しないため（check_distribution_boundary.py の #542 と同じ対策。
    あちらは二重引用符専用なので、単一引用符の CREDENTIAL_SHAPE_PATTERNS にも使えるよう引用符を引数にする）。"""
    inside = False
    for i, ch in enumerate(line):
        if ch == quote:
            inside = not inside
        elif ch == "#" and not inside:
            return line[:i]
    return line


def parse_shape_patterns(script_text: str) -> list[tuple[str, re.Pattern[str]]] | None:
    """publish-snapshot.sh の CREDENTIAL_SHAPE_NAMES（二重引用符）と CREDENTIAL_SHAPE_PATTERNS
    （単一引用符）を組にして返す。書式が崩れて読めない・件数が合わないときは None（判定不能）。"""

    def block(name: str, quote: str) -> list[str] | None:
        m = re.search(rf"^{name}=\(\n(.*?)^\)", script_text, re.MULTILINE | re.DOTALL)
        if not m:
            return None
        return [
            v
            for line in m.group(1).splitlines()
            if not line.strip().startswith("#")
            for v in re.findall(rf"{quote}([^{quote}]+){quote}", _strip_comment(line, quote))
        ]

    names = block("CREDENTIAL_SHAPE_NAMES", '"')
    patterns = block("CREDENTIAL_SHAPE_PATTERNS", "'")
    if not names or not patterns or len(names) != len(patterns):
        return None
    try:
        return [(n, re.compile(p)) for n, p in zip(names, patterns)]
    except re.error:
        return None


def resolve_shapes(script_text: str | None) -> tuple[list[tuple[str, re.Pattern[str]]] | None, str]:
    """検査に使う形状パターンを決める。原本（publish-snapshot.sh の中身）が無ければ写しを使い、
    あれば原本と写しの一致を要求する。(パターン, 理由) を返し、判定不能のときパターンは None。"""
    embedded = [(n, re.compile(p)) for n, p in EMBEDDED_SHAPES]
    if script_text is None:
        return embedded, "写し（publish-snapshot.sh 不在の配布先）"
    parsed = parse_shape_patterns(script_text)
    if parsed is None:
        return None, "CREDENTIAL_SHAPE_NAMES / CREDENTIAL_SHAPE_PATTERNS を読めない（書式変更の疑い）"
    if [(n, p.pattern) for n, p in parsed] != list(EMBEDDED_SHAPES):
        return None, "publish-snapshot.sh の形状定義と EMBEDDED_SHAPES が食い違う（片側だけ更新された）"
    return parsed, "原本と写しが一致"


def check_credential_shapes(text: str, shapes: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    errs: list[str] = []
    for no, line in enumerate(text.splitlines(), start=1):
        errs.extend(f"L{no}: クレデンシャル形状（{name}）に一致する" for name, pat in shapes if pat.search(line))
    return errs


def check_guide(text: str, shapes: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    return check_completion_conditions(text) + check_fictitious_values(text) + check_credential_shapes(text, shapes)


# ── 外部接点 ───────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _collect(targets: list[str]) -> tuple[list[Path], list[str]]:
    """(検査するファイル, 解決できなかった対象) を返す。1 つでも解決できなければ呼び出し側で判定不能にする。"""
    files: list[Path] = []
    missing: list[str] = []
    for t in targets:
        p = Path(t)
        found = sorted(p.rglob("*.md")) if p.is_dir() else ([p] if p.is_file() else [])
        if found:
            files.extend(found)
        else:
            missing.append(t)
    return files, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="設定手順書（C-5）の規律を機械検査する")
    parser.add_argument("targets", nargs="*", help="検査する .md ファイルまたはディレクトリ")
    parser.add_argument("--self-test", action="store_true", help="判定ロジックの自己テストを実行する")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.targets:
        parser.error("検査対象を 1 つ以上指定してください")

    shapes, reason = resolve_shapes(_read(SNAPSHOT_SCRIPT) if SNAPSHOT_SCRIPT.is_file() else None)
    if shapes is None:
        print(f"判定不能: {reason}", file=sys.stderr)
        return 2

    files, missing = _collect(args.targets)
    if missing:
        print(f"判定不能: 検査対象の .md が見つからない（{' '.join(missing)}）", file=sys.stderr)
        return 2

    total = 0
    for f in files:
        errs = check_guide(_read(f), shapes)
        total += len(errs)
        for e in errs:
            print(f"{f}: {e}")
    if total:
        print(f"[check_setup_guide] NG: {total} 件（{len(files)} ファイル）")
        return 1
    print(f"[check_setup_guide] OK: {len(files)} ファイル")
    return 0


def self_test() -> int:
    failures: list[str] = []

    def expect(label: str, cond: bool) -> None:
        if not cond:
            failures.append(label)

    shapes = [("GitHub PAT", re.compile(r"ghp_[A-Za-z0-9]{36}"))]

    good = "\n".join([
        "# 手順書",
        "## 1. アカウントを作る",
        "説明",
        "**完了条件**: ダッシュボードが表示される",
        "## 2〜5. まとめ",
        "### 2. リダイレクト URL を登録する",
        "```",
        "https://app.example.com/callback",
        "```",
        "**完了条件**: 一覧に 1 行増える",
        "## 工程 3/3: 通知先を登録する",
        "`ops@example.com` と `192.0.2.10` と `123456789012` を入れる",
        "[公式ヘルプ](https://docs.fictional-vendor.zz/setup) を開く",
        "**完了条件**: テスト送信が届く",
    ])
    expect("適合する手順書は違反ゼロ", check_guide(good, shapes) == [])
    expect("範囲見出し（2〜5.）は工程とみなさない", len(find_steps(good)) == 3)

    missing = "## 1. 作る\n説明だけ\n## 2. 次\n**完了条件**: 見える"
    errs = check_completion_conditions(missing)
    expect("完了条件の欠落を検出する", len(errs) == 1 and "1. 作る" in errs[0])

    nested = "## 1. 親\n### 補足\n**完了条件**: 見える\n## 2. 次\n**完了条件**: 見える"
    expect("下位見出しの中の完了条件も工程に属する", check_completion_conditions(nested) == [])

    nested = "## 1. A\n**完了条件**: x\n```markdown\n~~~\nhttps://api.fictional-corp.zz/x\n~~~\n```\n"
    expect("入れ子の別種フェンス内の値も検査する", len(check_fictitious_values(nested)) == 1)
    longer = "````markdown\n```text\nhttps://api.fictional-corp.zz/cb\n```\n````\n"
    expect("長いフェンス内の短いフェンスで閉じない", len(check_fictitious_values(longer)) == 1)
    expect("工程見出しが 0 件なら NG", len(check_completion_conditions("## ステップ1: 作る\n説明")) == 1)
    expect("小節番号（1.1）は工程ではない", len(find_steps("## 1. 親\n**完了条件**: x\n### 1.1 詳細\n")) == 1)
    expect("全角コロンの工程見出しを拾う", len(find_steps("## 工程 1/2：作る\n")) == 1)
    expect("クエリ・フラグメントをホストに含めない",
           len(check_fictitious_values("`https://console.fictional-corp.zz?d=app.example.com` `https://a.fictional-corp.zz#x.example`")) == 2)

    expect("下位レベルの工程見出しの完了条件を親に数えない",
           len(check_completion_conditions("## 1. A\n説明\n### 2. B\n**完了条件**: x\n")) == 1)
    expect("スキーム無しの実在風ドメインを検出する",
           len(check_fictitious_values("```\nCNAME  www.fictional-corp.zz\n```")) == 1)
    expect("スキーム無しの例示ドメインは許す", check_fictitious_values("```\nCNAME  www.example.com.\n```") == [])
    expect("ファイル名・パス・識別子はドメインとみなさない",
           check_fictitious_values("`config/labels.yaml` `routines-setup.md` `tools/x.py` `os.path` `v2.1.0`") == [])
    expect("URL のホストを二重に数えない", len(check_fictitious_values("`https://api.fictional-corp.zz/x`")) == 1)
    expect("字下げコードブロックの値も検査する",
           len(check_fictitious_values("説明\n\n    https://api.fictional-corp.zz/x\n")) == 1)

    fenced = "```\n## 1. これはコード\n```\n"
    expect("コードブロック内の # は工程見出しではない", find_steps(fenced) == [])

    real = "```\nhttps://api.fictional-vendor.zz/v1\nadmin@fictional-corp.zz\n100.64.0.1\n987654321098\n```"
    errs = check_fictitious_values(real)
    expect("実在風のホスト・メール・IP・ID を 4 件検出する", len(errs) == 4)

    link_only = "[公式](https://console.fictional-vendor.zz/) を開く"
    expect("Markdown リンクの URL は入力値ではない", check_fictitious_values(link_only) == [])

    placeholder = "`https://{YOUR_DOMAIN}/callback` と `<id>@{DOMAIN}`"
    expect("プレースホルダを含む値は判定しない", check_fictitious_values(placeholder) == [])

    reserved = "`https://a.b.test/x` `https://localhost:8080` `u@svc.example` `https://example.org`"
    expect("予約 TLD・localhost・example.org を許す", check_fictitious_values(reserved) == [])

    expect("サブドメイン偽装（example.com.evil.io）は許さない", not is_reserved_domain("example.com.evil.io"))
    expect("バージョン番号風の非アドレスは判定対象外", check_fictitious_values("`999.1.2.3`") == [])

    leaked = "トークン: ghp_" + "a" * 36
    expect("クレデンシャル形状を本文全体から検出する", len(check_credential_shapes(leaked, shapes)) == 1)

    script = "\n".join([
        "CREDENTIAL_SHAPE_NAMES=(",
        '  "AWS Access Key ID"',
        "  # コメント行",
        '  "JWT"',
        ")",
        "CREDENTIAL_SHAPE_PATTERNS=(",
        "  'AKIA[0-9A-Z]{16}'",
        "  'eyJ[A-Za-z0-9_-]{10,}'",
        ")",
    ])
    parsed = parse_shape_patterns(script)
    expect("publish-snapshot.sh の形状定義を組で読む", parsed is not None and [n for n, _ in parsed] == ["AWS Access Key ID", "JWT"])
    commented = script.replace("  'AKIA[0-9A-Z]{16}'", "  'AKIA[0-9A-Z]{16}'  # 'AKIA' 始まり")
    expect("行末コメント内のクォート文字列を要素にしない", parse_shape_patterns(commented) is not None)
    expect("件数不一致は判定不能（None）", parse_shape_patterns(script.replace('  "JWT"\n', "")) is None)
    expect("配列が無ければ判定不能（None）", parse_shape_patterns("FOO=(\n)") is None)

    shapes_only, _ = resolve_shapes(None)
    expect("原本が無ければ写しで検査する", shapes_only is not None and len(shapes_only) == len(EMBEDDED_SHAPES))
    original = "\n".join(
        ["CREDENTIAL_SHAPE_NAMES=("] + [f'  "{n}"' for n, _ in EMBEDDED_SHAPES] + [")"]
        + ["CREDENTIAL_SHAPE_PATTERNS=("] + [f"  '{p}'" for _, p in EMBEDDED_SHAPES] + [")"]
    )
    expect("原本と写しが一致すれば原本を使う", resolve_shapes(original)[0] is not None)
    drifted = original.replace("'AKIA[0-9A-Z]{16}'", "'AKIA[0-9A-Z]{20}'")
    expect("原本と写しの食い違いは判定不能", resolve_shapes(drifted)[0] is None)

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"[check_setup_guide --self-test] FAIL: {len(failures)} 件")
        return 1
    print("[check_setup_guide --self-test] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
