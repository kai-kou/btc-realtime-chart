#!/usr/bin/env python3
"""discussion_whiteboard.py — マルチエージェント議論用「ホワイトボード」基盤（Blackboard パターン）。

専門チームの議論（敵対的相互レビュー等）を、各エージェントが自由に書き込める
共有ドキュメントに集約し、議論の整理 + 履歴を git 管理できるようにする基盤ツール。

## 設計（同時書き込み破損の構造的排除）

公式の落とし穴: 複数エージェントが「同一ファイル」へ同時書き込みすると破損しうる
（claude-code Issue #29217）。本ツールは Anthropic のマルチエージェント研究システムが
採る **artifact パターン**（各エージェントは個別ファイルに findings を書き、orchestrator が
集約する）を踏襲し、構造的に競合を排除する:

  content/discussions/<id>/
    meta.json            … 議題・参加者・ラウンド情報
    entries/             … 1 post = 1 ユニークファイル（同時 post でも衝突しない）
      r01_<ns>_<pid>_<rand>_<author>_<kind>.md
    whiteboard.md        … orchestrator が render で集約する人間可読ビュー（git 履歴 = 議論履歴）

- `post`: 各エージェントが呼ぶ。**ユニーク名 + atomic write** なので並列でも壊れない。
- `render`: orchestrator（単一書き手）だけが呼ぶ。entries を (round, ts) 順に whiteboard.md へ。
  entries が空のとき既存の非空 whiteboard.md（= lead が直接記載した等）は **上書きしない**（内容消失防止）。

詳細規約は docs/rules/discussion-whiteboard-rules.md を参照。

## 使い方

  python3 tools/discussion_whiteboard.py init V188-thumb \
      --topic "V188 サムネ案" --participants "seo,visual,reviewer_a" --brief "どれがCTRを取れるか"
  python3 tools/discussion_whiteboard.py post V188-thumb \
      --author seo --round 1 --kind claim --body "案Aはキーワード前置で強い..."
  python3 tools/discussion_whiteboard.py render V188-thumb
  python3 tools/discussion_whiteboard.py list V188-thumb --round 1
  python3 tools/discussion_whiteboard.py show V188-thumb
  python3 tools/discussion_whiteboard.py --self-test
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCUSSIONS_DIR = REPO_ROOT / "content" / "discussions"
JST = _dt.timezone(_dt.timedelta(hours=9))

# render 出力に埋める標識（クロバー防止: この標識が無い非空 whiteboard.md は外部記載とみなす）
SENTINEL = "<!-- discussion_whiteboard:auto -->"

KINDS = ("claim", "evidence", "rebuttal", "question", "concession", "consensus", "verdict", "note")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# refs はエントリヘッダ（HTML コメント）に書かれるため、改行や `-->` `<` `>` 等を含む値を拒否する
# （含むと _HEADER_RE が途中で閉じ _parse_entry が誤動作する）。entry ファイル名・短い ID 用途を想定。
_REF_RE = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9_.:#/-]{0,127}$")


def _now() -> _dt.datetime:
    return _dt.datetime.now(JST)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _validate_id(value: str, label: str, pattern: re.Pattern) -> str:
    if not pattern.match(value or ""):
        raise ValueError(f"{label} が不正です: {value!r}（許可: 英数字と一部記号のみ）")
    return value


def _board_dir(discussion_id: str) -> Path:
    _validate_id(discussion_id, "discussion_id", _ID_RE)
    return DISCUSSIONS_DIR / discussion_id


def _atomic_write(path: Path, text: str) -> None:
    """同一ディレクトリの一時ファイルへ書いてから rename（部分書き込み/競合破損を防ぐ）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_meta(board: Path) -> dict:
    p = board / "meta.json"
    if not p.exists():
        raise FileNotFoundError(f"議題が未作成です: {board.name}（先に init を実行）")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_init(args: argparse.Namespace) -> int:
    board = _board_dir(args.id)
    (board / "entries").mkdir(parents=True, exist_ok=True)
    meta_path = board / "meta.json"
    participants = [p.strip() for p in (args.participants or "").split(",") if p.strip()]
    for p in participants:
        _validate_id(p, "participant", _AUTHOR_RE)
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["topic"] = args.topic or meta.get("topic", "")
        if args.brief:
            meta["brief"] = args.brief
        meta["participants"] = list(dict.fromkeys(meta.get("participants", []) + participants))
        meta["updated_at"] = _now_iso()
    else:
        meta = {
            "id": args.id, "topic": args.topic or "", "brief": args.brief or "",
            "participants": participants, "created_at": _now_iso(), "updated_at": _now_iso(),
        }
    _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    cmd_render(argparse.Namespace(id=args.id, quiet=True))
    print(f"✅ init: {board}")
    print(f"   whiteboard: {board / 'whiteboard.md'}")
    return 0


def _entry_filename(round_no: int, author: str, kind: str) -> str:
    token = f"{_now().timestamp():.0f}_{os.getpid()}_{secrets.token_hex(3)}"
    return f"r{int(round_no):02d}_{token}_{author}_{kind}.md"


def _entry_header(author: str, round_no: int, kind: str, refs: list[str]) -> str:
    lines = ["<!--entry", f"author: {author}", f"round: {int(round_no)}",
             f"kind: {kind}", f"ts: {_now_iso()}"]
    if refs:
        lines.append("refs: " + ", ".join(refs))
    lines.append("-->")
    return "\n".join(lines)


def cmd_post(args: argparse.Namespace) -> int:
    board = _board_dir(args.id)
    _read_meta(board)
    author = _validate_id(args.author, "author", _AUTHOR_RE)
    if int(args.round) < 1:
        raise ValueError(f"round は 1 以上が必要です: {args.round}（ファイル名・並び順が崩れるため）")
    kind = args.kind
    if kind not in KINDS:
        raise ValueError(f"kind が不正です: {kind}（許可: {', '.join(KINDS)}）")
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8").strip()
    elif args.body is not None:
        body = args.body.strip()
    else:
        body = sys.stdin.read().strip()
    if not body:
        raise ValueError("本文が空です（--body / --body-file / stdin のいずれかで渡す）")
    refs = [r.strip() for r in (args.refs or "").split(",") if r.strip()]
    for r in refs:
        if not _REF_RE.match(r):
            raise ValueError(
                f"ref が不正です: {r!r}（許可: 英数字と _ . : # / -・先頭は英数字・128字以内。"
                "改行や `-->` `<` `>` 等を含む値はエントリヘッダを壊すため不可）")
    entries = board / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    fname = _entry_filename(args.round, author, kind)
    content = _entry_header(author, args.round, kind, refs) + "\n\n" + body + "\n"
    _atomic_write(entries / fname, content)
    print(str(entries / fname))
    return 0


_HEADER_RE = re.compile(r"<!--entry\s*(.*?)-->\s*(.*)", re.DOTALL)


def _parse_entry(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    m = _HEADER_RE.search(raw)
    meta: dict = {"author": "?", "round": 0, "kind": "note", "ts": "", "refs": []}
    body = raw
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "round":
                try:
                    meta["round"] = int(v)
                except ValueError:
                    meta["round"] = 0
            elif k == "refs":
                meta["refs"] = [x.strip() for x in v.split(",") if x.strip()]
            elif k in ("author", "kind", "ts"):
                meta[k] = v
        body = m.group(2).strip()
    meta["body"] = body
    meta["_file"] = path.name
    return meta


_KIND_LABEL = {
    "claim": "主張", "evidence": "根拠", "rebuttal": "反論", "question": "問い",
    "concession": "譲歩", "consensus": "合意", "verdict": "判定", "note": "メモ",
}


def cmd_render(args: argparse.Namespace) -> int:
    board = _board_dir(args.id)
    meta = _read_meta(board)
    entries_dir = board / "entries"
    entries = [_parse_entry(p) for p in sorted(entries_dir.glob("*.md"))] if entries_dir.exists() else []
    entries.sort(key=lambda e: (e.get("round", 0), e.get("ts", ""), e.get("_file", "")))

    # クロバー防止: entries が空かつ既存 whiteboard.md が「外部記載（標識なし）の非空」なら上書きしない
    wb_path = board / "whiteboard.md"
    if not entries and wb_path.exists():
        existing = wb_path.read_text(encoding="utf-8")
        if existing.strip() and SENTINEL not in existing:
            if not getattr(args, "quiet", False):
                print(f"⚠️ entries が空で、外部記載の whiteboard.md が存在するため上書きをスキップ: {wb_path}",
                      file=sys.stderr)
            return 0

    out: list[str] = [SENTINEL,
                      f"# 🧑‍🏫 議論ホワイトボード: {meta.get('topic') or meta.get('id')}", ""]
    out.append(f"- 議題ID: `{meta.get('id')}`")
    if meta.get("brief"):
        out.append(f"- 論点: {meta['brief']}")
    if meta.get("participants"):
        out.append("- 参加者: " + ", ".join(f"`{p}`" for p in meta["participants"]))
    out.append(f"- 投稿数: {len(entries)}")
    out.append(f"- 更新: {_now_iso()}")
    out.append("")
    out.append("> このファイルは `tools/discussion_whiteboard.py render` が自動生成する。"
               "直接編集せず `post` で追記すること（同時書き込み破損防止）。")
    out.append("")

    if not entries:
        out.append("_（まだ投稿がありません）_")
    else:
        for rnd in sorted({e.get("round", 0) for e in entries}):
            out.append(f"## ラウンド {rnd}")
            out.append("")
            for e in [x for x in entries if x.get("round", 0) == rnd]:
                label = _KIND_LABEL.get(e["kind"], e["kind"])
                out.append(f"### `{e['author']}` — {label}")
                meta_bits = []
                if e.get("ts"):
                    meta_bits.append(e["ts"])
                if e.get("refs"):
                    meta_bits.append("refs: " + ", ".join(e["refs"]))
                if meta_bits:
                    out.append(f"<sub>{' ・ '.join(meta_bits)}</sub>")
                out.append("")
                out.append(e["body"])
                out.append("")
    _atomic_write(wb_path, "\n".join(out).rstrip() + "\n")
    if not getattr(args, "quiet", False):
        print(str(wb_path))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    board = _board_dir(args.id)
    _read_meta(board)
    entries_dir = board / "entries"
    entries = [_parse_entry(p) for p in sorted(entries_dir.glob("*.md"))] if entries_dir.exists() else []
    if args.round is not None:
        entries = [e for e in entries if e.get("round", 0) == args.round]
    entries.sort(key=lambda e: (e.get("round", 0), e.get("ts", "")))
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    for e in entries:
        preview = e["body"].replace("\n", " ")[:80]
        print(f"[R{e['round']}] {e['author']:<10} {e['kind']:<10} {preview}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    board = _board_dir(args.id)
    wb = board / "whiteboard.md"
    if not wb.exists():
        cmd_render(argparse.Namespace(id=args.id, quiet=True))
    print(wb.read_text(encoding="utf-8"))
    return 0


def _self_test() -> int:
    import multiprocessing
    global DISCUSSIONS_DIR
    orig = DISCUSSIONS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        DISCUSSIONS_DIR = Path(tmp)
        did = "selftest"
        cmd_init(argparse.Namespace(id=did, topic="セルフテスト", participants="a,b,c", brief="並列post整合性"))

        def _worker(i: int) -> None:
            global DISCUSSIONS_DIR
            DISCUSSIONS_DIR = Path(tmp)
            author = ["a", "b", "c"][i % 3]
            cmd_post(argparse.Namespace(id=did, author=author, round=(i % 2) + 1, kind="claim",
                                        body=f"並列投稿 #{i} from {author}", body_file=None, refs=None))

        N = 24
        procs = [multiprocessing.Process(target=_worker, args=(i,)) for i in range(N)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        board = DISCUSSIONS_DIR / did
        n_entries = len(list((board / "entries").glob("*.md")))
        cmd_render(argparse.Namespace(id=did, quiet=True))
        wb = (board / "whiteboard.md").read_text(encoding="utf-8")
        ok = n_entries == N
        if not ok:
            print(f"❌ entries 数が不一致: {n_entries} != {N}", file=sys.stderr)
        for i in range(N):
            if f"並列投稿 #{i} " not in wb:
                print(f"❌ whiteboard に投稿 #{i} が欠落", file=sys.stderr)
                ok = False
        if "## ラウンド 1" not in wb or "## ラウンド 2" not in wb:
            print("❌ ラウンド見出しが欠落", file=sys.stderr)
            ok = False

        # クロバー防止ガードの検証: 外部記載を render が消さないこと
        ext_id = "external"
        cmd_init(argparse.Namespace(id=ext_id, topic="ext", participants="", brief=""))
        ext_wb = DISCUSSIONS_DIR / ext_id / "whiteboard.md"
        ext_wb.write_text("# 外部が直接書いた議論\n本文あり\n", encoding="utf-8")  # 標識なし
        cmd_render(argparse.Namespace(id=ext_id, quiet=True))
        if "外部が直接書いた議論" not in ext_wb.read_text(encoding="utf-8"):
            print("❌ クロバー防止ガードが外部記載を消した", file=sys.stderr)
            ok = False

        DISCUSSIONS_DIR = orig
        if ok:
            print(f"✅ self-test PASS（{N} 並列 post→render 整合・破損なし・クロバー防止OK）")
            return 0
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="マルチエージェント議論ホワイトボード基盤")
    ap.add_argument("--self-test", action="store_true", help="並列post→render とクロバー防止の検証")
    sub = ap.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="議題を作成（冪等）")
    p_init.add_argument("id")
    p_init.add_argument("--topic", default="")
    p_init.add_argument("--participants", default="")
    p_init.add_argument("--brief", default="")
    p_init.set_defaults(func=cmd_init)

    p_post = sub.add_parser("post", help="意見を投稿（並列安全）")
    p_post.add_argument("id")
    p_post.add_argument("--author", required=True)
    p_post.add_argument("--round", type=int, default=1)
    p_post.add_argument("--kind", default="claim")
    p_post.add_argument("--body", default=None)
    p_post.add_argument("--body-file", default=None)
    p_post.add_argument("--refs", default=None)
    p_post.set_defaults(func=cmd_post)

    p_render = sub.add_parser("render", help="whiteboard.md を集約再生成（orchestrator 専用）")
    p_render.add_argument("id")
    p_render.add_argument("--quiet", action="store_true")
    p_render.set_defaults(func=cmd_render)

    p_list = sub.add_parser("list", help="投稿一覧")
    p_list.add_argument("id")
    p_list.add_argument("--round", type=int, default=None)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="whiteboard.md を表示")
    p_show.add_argument("id")
    p_show.set_defaults(func=cmd_show)

    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
