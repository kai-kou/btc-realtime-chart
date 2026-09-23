#!/usr/bin/env python3
"""作業領域の外へ書き込む Bash / ホーム配下の Claude 設定領域を触る Bash を、承認プロンプトになる前に差し戻すガード。

背景（実測・Issue #578）:
  クラウド実行環境には bwrap / sandbox-exec が存在せず（`command -v bwrap` = MISSING・`Seccomp: 0`）、
  `.claude/settings.json` の `sandbox.enabled: true` は起動できない。したがってクラウドでの Bash 承認可否は
  `permissions.*` の静的ルールと auto モードの classifier だけで決まる。公式仕様どおり classifier は
  「作業ディレクトリ・セッション一時ディレクトリの外への書き込み / 削除」を自動承認しないため、
  無人ルーティン（scheduled trigger）がそういうコマンドを出すと **誰も承認しないまま無限停止** する。

  実測（headless プローブ・`claude -p --permission-mode auto`）:
    - `mkdir -p /tmp/<作業ツリー外>/x && echo hi > /tmp/<作業ツリー外>/x/a.txt` → permission_denials に記録
    - `mkdir -p /tmp/<作業ツリー外>/y && rm -rf /tmp/<作業ツリー外>/y`         → permission_denials に記録
    - 作業ディレクトリ内・セッション scratchpad 配下の書き込み                 → 記録なし（通る）

  本ガードはプロンプトに落ちる前にブロックし、代替（セッション scratchpad / ネイティブ Read・Grep ツール）を
  案内する。ブロックは **ツール失敗として Claude に返る** ため、無人セッションでも停止せず自己修正できる。

追加の射程（#618・議論 routine-permission-prompts-20260910）:
  作業ツリーの **内側** でも、リポジトリ自身の `.claude/**`（`.claude/rules`・`.claude/worktrees` を除く）と
  `.git/**` への Bash 書き込み（`sed -i` / cp / mv / tee / リダイレクト等）を差し戻す。両者は Claude Code の
  ハードコード Protected paths で、`permissions.allow` では事前承認できず auto モードでも classifier の
  個別判定に回るため、無人ルーティンでは ask に倒れた時点で停止する（下流で実発生）。

射程と限界（過信しないこと）:
  - コマンド名の列挙型で、`python3 -c "open('/etc/x','w')"` のような任意コード経由は塞げない
    （`pre-tool-use-router.sh` の機密ファイルガードと同じ設計上の限界。残余リスクはコンテナ隔離が引き受ける）。
    保護パスについても同じで、`python3 -c` / `node -e` / `perl -i` / `awk -i inplace` 経由は素通りする
    （2 つの独立実装で確認済み）。塞いだのは sed / cp / mv / ln / tee / リダイレクトの直書きであって根絶ではない
  - 解決できない変数（外部 env 由来）を含むパスは判定不能として素通りさせる（誤ブロックを避ける。
    同一コマンド内の `NAME=value` 代入は解決する）
  - 目的は「無人セッションの停止防止」であって権限の代替ではない。`permissions.deny` の保護とは独立

トグル: `CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1` で無効化する。セッションの環境変数としても、
Bash コマンドの先頭に置く前置き代入としても効く（フックはプロセスの環境変数しか見ないため、
前置き代入はコマンド文字列から検出する）。heredoc 本文中の記述は無効（文書にこの語を書いただけで
ガードが外れるのを防ぐ）。承認レイヤーの代替ではないので、明示的に外す判断自体は設計意図どおり。

入出力:
  stdin  : PreToolUse フックの JSON（`tool_input.command` / `cwd` / `session_id` を読む）
  stdout : ブロックする場合のみ理由文（複数行）
  exit   : 0 = 問題なし / 1 = ブロック（呼び出し側の router が exit 2 に変換する）

自己テスト: `python3 .claude/hooks/lib/workspace_write_guard.py --self-test`
回帰テスト: `bash tools/test_workspace_write_guard.sh`
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# 非フラグ引数のすべてが書き込み / 削除対象になるコマンド
WRITE_ALL_ARGS = {
    "rm", "rmdir", "mkdir", "touch", "tee", "truncate", "shred", "unlink",
    "chmod", "chown", "chgrp",
}
# 最後の非フラグ引数が書き込み先になるコマンド
WRITE_LAST_ARG = {"cp", "mv", "ln", "install", "rsync"}
# フラグの値が書き込み先になるコマンド（GNU の `-t DIR` / ダウンロード先指定）
DEST_VALUE_FLAGS = {
    "cp": ("-t", "--target-directory"),
    "mv": ("-t", "--target-directory"),
    "install": ("-t", "--target-directory"),
    "rsync": ("--target-directory",),
    "curl": ("-o", "--output", "--output-dir"),
    "wget": ("-O", "--output-document", "-P", "--directory-prefix"),
}
# 実コマンドの前に置かれ、読み飛ばしてよいラッパー
COMMAND_WRAPPERS = {"sudo", "env", "nice", "ionice", "command", "exec", "builtin", "time", "timeout"}
# `-c` 引数のスクリプト文字列を再帰的に解析する対象（bash -c / sh -c 等・Issue #50）
# 既知の限界: perl -e / python3 -c / ruby -e / awk 等の非シェル言語経由の任意コードは対象外
#（モジュール docstring の「射程と限界」と同じ理由。列挙型である以上すべての言語処理系を
# 網羅できない。コンテナ隔離が最終防御という位置づけは変わらない）。
SHELL_C_NAMES = {"bash", "sh", "zsh", "dash", "ksh"}
# bash 系シェルは `-euc SCRIPT` のように単純フラグを1トークンへ結合でき、結合順に関係なく
# 'c' が含まれれば直後の引数をスクリプトとして消費する（実機確認済み）。ここに無いフラグ文字
# （値を取るもの・ロングオプション等）が混ざるトークンは対象外とし、過検知を避ける。
_BASH_SIMPLE_FLAG_CHARS = set("aBbCcDEefHhiklmnOoPprsTtuvx")
# find の式にこれらが現れたら削除系アクションとみなす（-exec/-execdir の直後のサブコマンド判定に使う）
FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}
# `-exec` の直後に来たら「書き込み / 削除を伴う」とみなすサブコマンド（削除系だけでは
# `find .claude/hooks -exec sed -i ... {} \;` が素通りする・PR #55 Layer 1 指摘）
FIND_EXEC_DESTRUCTIVE = WRITE_ALL_ARGS | WRITE_LAST_ARG | {"rm", "sed", "dd"}
# xargs 自身のフラグのうち値を1つ消費するもの（サブコマンド名の誤認防止・Issue #50 レビュー指摘）
XARGS_VALUE_FLAGS = {
    "-I", "-i", "-E", "-L", "-l", "-n", "-P", "-s", "-a", "-d",
    "--replace", "--max-args", "--max-lines", "--max-procs", "--max-chars",
    "--arg-file", "--delimiter", "--eof",
}
# xargs 経由で呼ばれると対象パスが標準入力由来になり静的判定できないため、
# これらのサブコマンドが来たら判定不能として fail-open にせず一律ブロックする
XARGS_DESTRUCTIVE = WRITE_ALL_ARGS | WRITE_LAST_ARG | {"dd", "sed"} | SHELL_C_NAMES
# コマンド置換 / bash -c の再帰評価が入れ子になりすぎたときの深度上限（RecursionError での
# クラッシュを防ぐ・Issue #50 レビュー指摘）。通常のコマンドはここまで深くネストしない。
MAX_RECURSION_DEPTH = 20
# セグメント区切りとして扱うトークン（`&>` はリダイレクトなので含めない）
SEGMENT_SEPARATORS = {";", "&", "&&", "|", "||"}
# リダイレクト演算子（`>` `>>` `2>` `&>` `>|` `1>>` 等）
_REDIRECT_OP = re.compile(r"^[0-9]*&?>{1,2}\|?$")
# heredoc の開始（`<<EOF` / `<<'EOF'` / `<<-"EOF"`）
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# 同一コマンド文字列内の変数代入（`WORK=/tmp/foo` 形式・値にスペースを含まないもののみ）
_ASSIGNMENT = re.compile(r"(?:^|[\s;&|(])([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|)]+)")
# 未解決の変数参照
_UNRESOLVED_VAR = re.compile(r"\$\{?[A-Za-z_(]")
# 脱出ハッチの環境変数名（セッション env / コマンド先頭の前置き代入のどちらでも効く）
_TOGGLE_NAME = "CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD"

# 作業ツリー（cwd）配下でも保護するリポジトリ内パス（#618・議論 routine-permission-prompts-20260910）。
#
# `.claude` と `.git` は Claude Code のハードコード Protected paths（公式 permission-modes「Protected
# paths」節）で、`permissions.allow` では事前承認できず、auto モードでも必ず classifier の個別判定に回る。
# 判定は文脈依存で、無人ルーティンが ask に倒れると応答者がいないまま停止する（下流で実発生:
# `sed -i` で `.claude/hooks/pre-tool-use-router.sh` を書き換え〔C1〕・`>> .git/info/exclude`〔C3〕）。
# 従来の is_safe() は「cwd の内側なら常に安全」と判定していたため、この 2 件はノーガードで通っていた。
#
# 範囲は `.claude` 全体と `.git` 全体に限る。公式 Protected paths に載る `.vscode` / `.husky` /
# `.devcontainer` / `.cargo` / `.mvn` / `.yarn` / `.config/git` は **含めない**（サードパーティ開発ツールの
# 設定で、下流の通常開発で Bash から日常的に編集される。実発生の証跡もない・YAGNI）。
#
# 判定は cwd からの相対パスの **どの深さ** でも `.claude` / `.git` セグメントを見る（`sub/.claude/hooks/x.sh` や
# `.claude/worktrees/wt-1/.claude/hooks/x.sh` も対象）。
#
# 除外:
#   - `.claude/worktrees/<name>/**`: 公式が「Claude 自身が git worktree を置く場所」として明示的に除外。
#     ただしその配下にさらに `.claude` / `.git` が現れたら再び保護する
#   - `.claude/rules/**` への **symlink 作成**（`ln -s ../../docs/rules/x.md .claude/rules/x.md`）は正規の直接
#     Bash 手順（`docs/rules/session-compression-rules-detail.md` / `tools/check_rules_sync.sh`）なので、
#     **リンク元が `<cwd>/docs/rules/` 配下のときだけ** 通す。symlink 以外の `.claude/rules/**` 書き込みは保護する
#   - セッション scratchpad / TMPDIR 配下は対象外（ラボ用の `.claude` `.git` フィクスチャを作る検証手順を止めない）
#   - `git` サブコマンド自体（`git config` / `git update-index` / `git worktree` 等）は `_write_targets()` の
#     抽出対象コマンドに `git` が無いため、この判定に渡る前に候補から外れる（誤ブロックしない）
#   - 読み取り（`cat .claude/settings.json`）と、cwd 外へコピーする際のコピー元は対象外
#
# 許容している副作用: `.git/hooks/*` へのローカル hook インストール・`rm .git/index.lock` 等の復旧操作も
# BLOCK になる。個別 allowlist は設けず、必要なら脱出ハッチ（`_TOGGLE_NAME`）で通す。
_REPO_PROTECTED_DIRS = (".claude", ".git")
_WORKTREES_DIRNAME = "worktrees"
_RULES_SYMLINK_SOURCE = os.path.join("docs", "rules")


def _strip_heredocs(command: str) -> str:
    """heredoc の本文を解析対象から除く。

    ドキュメントやスクリプトを heredoc で書き込むとき、本文中に例示として現れるパスは実際の
    ファイル操作ではない。除かないと文書を書くたびに誤ブロックする（導入直後に実発生）。

    2 つの安全策を持つ:
      - 同一行に複数の heredoc（`cat <<A <<B`）がある場合、宣言順に全ての本体を除く
      - **終端デリミタが見つからないまま行末に達したら、読み飛ばした行を解析対象へ戻す**
        （fail-open にすると、終端漏れした heredoc 以降の実コマンドが丸ごと不可視になる）
    """
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        pending = [m.group(2) for m in _HEREDOC_START.finditer(line)]
        index += 1
        if not pending:
            continue
        body_start = index
        while pending and index < len(lines):
            if lines[index].strip() == pending[0]:
                pending.pop(0)
            index += 1
        if pending:
            # 未終端: 読み飛ばした範囲を解析対象に戻す（安全側）
            kept.extend(lines[body_start:])
            index = len(lines)
    return "\n".join(kept)


def _expand_assignments(command: str) -> str:
    """同一コマンド文字列内の `NAME=value` 代入を、その後の `$NAME` / `${NAME}` へ展開する。

    `WORK=/tmp/demo; rm -rf $WORK` のような一時変数経由のパスを静的に解決するための最小実装。
    """
    values: dict[str, str] = {}
    for name, value in _ASSIGNMENT.findall(command):
        for known, known_value in values.items():
            value = value.replace(f"${{{known}}}", known_value).replace(f"${known}", known_value)
        values[name] = value
    if not values:
        return command
    expanded = command
    # 長い名前から置換する（`$WORK_DIR` を `$WORK` で壊さない）
    for name in sorted(values, key=len, reverse=True):
        expanded = expanded.replace(f"${{{name}}}", values[name]).replace(f"${name}", values[name])
    return expanded


def _toggle_in_segment(tokens: list[str]) -> bool:
    """このセグメントが `CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 cmd` 形式で外されているか。

    フックが見るのは Claude Code プロセスの環境変数なので、Bash コマンド文字列に置いた前置き代入は
    `os.environ` に現れない。ブロックメッセージが案内する外し方が実際には効かず、
    正当な作業（設計上リポジトリ外に置かれる成果物の後始末など）が進められなくなっていた（#582）。

    判定は **セグメント先頭から連続する代入トークン** に限る。コマンド文字列のどこにトグル名が
    現れても外れる実装だと、`echo "…TOGGLE=1…" && rm -rf /外` や、コミットメッセージ・説明文に
    この語が入っただけでガードが丸ごと無効化される（シェルの `VAR=1 cmd` の意味論とも食い違う）。
    適用範囲もそのセグメントに閉じる（`TOGGLE=1 rm -f /外/marker && rm -rf /外/other` の後段は検査する）。
    """
    for token in tokens:
        if "=" not in token or token.startswith("-") or token.startswith("/"):
            return False  # 代入以外が現れたら前置き部分は終わり
        name, _, value = token.partition("=")
        if name == _TOGGLE_NAME and value == "1":
            return True
    return False


def _tokenize_line(line: str) -> list[str]:
    """1 行をクォートを尊重してトークン化する。

    生文字列に正規表現でセグメント分割をかけると、`curl "https://x/a?b=1&c=2" -o out` のように
    クォート内へ区切り文字を含む引数でコマンドが分断され、以降の解析が丸ごと落ちる。
    `punctuation_chars=True` の shlex は `;` `&&` `||` `|` `>` `>>` を独立トークンにするため、
    クォート内の同じ文字と区別できる。
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # クォートが閉じていない等。判定不能として空を返す（素通り）
        return []


def _segments(command: str) -> list[list[str]]:
    """コマンド文字列を「1 コマンド = 1 トークン列」のセグメントへ分ける（行またぎも区切る）。"""
    result: list[list[str]] = []
    for line in command.split("\n"):
        current: list[str] = []
        for token in _tokenize_line(line):
            if token in SEGMENT_SEPARATORS:
                if current:
                    result.append(current)
                current = []
            else:
                current.append(token)
        if current:
            result.append(current)
    return result


def _resolve(path: str, cwd: str | None, home: str) -> str | None:
    """パス様トークンを絶対パスへ正規化する。解決できないものは None を返す。

    シンボリックリンクも解決する（作業ディレクトリ内のリンクが外部を指すケースを取りこぼさない）。
    """
    if not path or _UNRESOLVED_VAR.search(path):
        return None
    if path.startswith("~"):
        path = home + path[1:]
    if not path.startswith("/"):
        if cwd is None:
            return None  # `cd` 先が解決できないセグメント。判定不能として素通りさせる
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _is_device_sink(path: str) -> bool:
    """`/dev/null` 等の擬似デバイスは「作業領域外への書き込み」に数えない。

    `cmd 2>/dev/null` は日常的に使われ、承認プロンプトにもならない（実ファイルを作らない）。
    除外しないと本ガードが通常運用を止める（導入直後に実発生）。`realpath` 後に
    `/proc/self/fd/…` へ解決される `/dev/stdout` 等も拾えるようプレフィックスで判定する。
    """
    return path.startswith("/dev/") or re.match(r"^/proc/(self|[0-9]+)/fd/", path) is not None


def _session_tmp_ok(path: str, session_id: str) -> bool:
    """ハーネスが払い出す **このセッションの** scratchpad 領域かどうか。

    `/tmp/claude-<N>/<project>/<session-id>/...` という構造なので、session_id まで一致させる。
    緩いプレフィックス一致（`/tmp/claude-` で始まれば何でも可）にすると、他セッションの
    scratchpad の削除や、実在しない自作パスまで安全扱いになる。
    """
    if not path.startswith("/tmp/claude-"):
        return False
    if not session_id:
        # session_id が渡らない環境では、セッション領域を安全基点に加えない（安全側）
        return False
    return re.match(rf"^/tmp/claude-[^/]*/[^/]+/{re.escape(session_id)}(/|$)", path) is not None


def _command_index(tokens: list[str]) -> int | None:
    """先頭の環境変数代入とラッパー（sudo / env / timeout 等）を読み飛ばしてコマンド名の位置を返す。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-") and not token.startswith("/"):
            index += 1
            continue
        if os.path.basename(token) in COMMAND_WRAPPERS:
            index += 1
            # ラッパーのフラグと数値引数（`timeout 300 cmd` の 300）を読み飛ばす
            while index < len(tokens) and (tokens[index].startswith("-") or tokens[index].isdigit()):
                index += 1
            continue
        return index
    return None


def _is_shell_c_flag(token: str) -> bool:
    """`-c` そのもの、または `-euc` のように 'c' を含む単純フラグの結合トークンか。"""
    if token == "-c":
        return True
    if len(token) > 1 and token[0] == "-" and token[1] != "-":
        chars = set(token[1:])
        return "c" in chars and chars <= _BASH_SIMPLE_FLAG_CHARS
    return False


def _shell_c_script(tokens: list[str], index: int) -> str | None:
    """`bash -c '...'` / `bash -euc "..."` 等、`-c` 相当のフラグ直後にあるスクリプト文字列を返す。

    最初の非フラグトークンに達したら走査を止める（bash 自身のオプション解析は最初の
    非フラグ引数以降を位置引数として扱うため、それ以降を `-c` 探索の対象にしない）。
    """
    i = index + 1
    while i < len(tokens):
        token = tokens[i]
        if _is_shell_c_flag(token):
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if not token.startswith("-"):
            break
        i += 1
    return None


def _leading_command_name(tokens: list[str]) -> str | None:
    """先頭のフラグ（値を取るものは値ごと）を読み飛ばした最初の非フラグトークンを返す。

    `xargs -0 rm -rf` の `rm` や `xargs -I {} bash -c ...` の `bash` のように、対象コマンドの
    前に xargs 自身のフラグが挟まる形を拾う。値を取るフラグ（`XARGS_VALUE_FLAGS`）はその値
    トークンも読み飛ばさないと、値（`{}` 等）をコマンド名と誤認してブロックをすり抜ける
    （Issue #50 レビュー指摘・CRITICAL）。
    """
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            if token in XARGS_VALUE_FLAGS and i + 1 < len(tokens):
                i += 2
            else:
                i += 1
            continue
        return os.path.basename(token)
    return None


def _find_has_destructive_action(args: list[str]) -> bool:
    """`find` の式に `-delete` か、`-exec` / `-execdir` 経由の書き込み系コマンドが含まれるか。

    判定対象は削除系（`rm` 等）だけでなく `sed -i` / `cp` / `mv` / `tee` / `ln` まで含める。
    `find .claude/hooks -type f -exec sed -i "s/a/b/" {} \\;` は C1（フックの書き換え）と同義で、
    削除系に限定していると保護パスガードごと素通りする（PR #55 Layer 1 指摘）。
    """
    for i, arg in enumerate(args):
        if arg == "-delete":
            return True
        if arg in FIND_EXEC_FLAGS and i + 1 < len(args):
            sub = os.path.basename(args[i + 1])
            if sub in FIND_EXEC_DESTRUCTIVE:
                return True
    return False


def _extract_command_substitutions(text: str) -> list[str]:
    """`$( ... )` の内側コマンド文字列を、ネストを保ったまま独立の文字列として抽出する。

    `shlex(punctuation_chars=True)` は `$(` `)` を境界として扱わずトークンを平坦化するため、
    `echo $(rm -rf /outside)` のような置換の中身が実コマンドとして認識されない（Issue #50）。
    ここでテキストレベルで括弧の対応を取って切り出し、`analyze()` に独立コマンドとして
    再帰的に渡す（バッククォート形式 `` `cmd` `` は対象外）。
    """
    results: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 1
            j = i + 2
            start = j
            while j < n and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = text[start:j]
            if inner.strip():
                results.append(inner)
            i = j + 1
            continue
        i += 1
    return results


def _strip_redirection_args(tokens: list[str]) -> list[str]:
    """リダイレクト演算子とその直後の宛先をトークン列から除く（宛先は別途 targets へ入れる）。"""
    cleaned: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_OP.match(token):
            skip_next = True
            continue
        cleaned.append(token)
    return cleaned


def _write_targets(tokens: list[str]) -> list[str]:
    """セグメントのトークン列から、書き込み / 削除の対象になるパス様トークンを抽出する。"""
    if not tokens:
        return []
    targets: list[str] = []

    # リダイレクト先（`>` `>>` `2>` `&>` `>|` の直後）
    for i, token in enumerate(tokens):
        if _REDIRECT_OP.match(token) and i + 1 < len(tokens):
            targets.append(tokens[i + 1])

    index = _command_index(tokens)
    if index is None:
        return targets
    name = os.path.basename(tokens[index])
    args = _strip_redirection_args(tokens[index + 1:])

    # 値が書き込み先になるフラグ（`-t DIR` / `--target-directory=DIR` / `curl -o FILE`）
    dest_flags = DEST_VALUE_FLAGS.get(name, ())
    flag_dests: list[str] = []
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and arg != "-":
            matched_value = None
            consumed = 1
            for flag in dest_flags:
                if arg == flag and i + 1 < len(args):
                    matched_value, consumed = args[i + 1], 2
                    break
                if arg.startswith(flag + "="):
                    matched_value = arg[len(flag) + 1:]
                    break
            if matched_value is not None:
                flag_dests.append(matched_value)
            i += consumed
            continue
        operands.append(arg)
        i += 1

    targets.extend(flag_dests)

    if name == "dd":
        targets.extend(a[len("of="):] for a in args if a.startswith("of="))
    elif name == "sed" and any(a.startswith("-i") and not a.startswith("--") for a in args):
        # `sed -i 's/a/b/' file...` — 最初の非フラグ引数はスクリプト、残りが書き換え対象
        targets.extend(operands[1:])
    elif name in WRITE_ALL_ARGS:
        targets.extend(operands)
    elif name == "mv":
        # 移動元も消える（= 書き込み扱い）。`mv .claude/hooks/x.sh /tmp/out` はフックの除去（#618 Layer 1 指摘）
        targets.extend(operands)
    elif name in WRITE_LAST_ARG and operands and not flag_dests:
        targets.append(operands[-1])
    elif name == "find" and _find_has_destructive_action(args):
        # -delete / -exec ... 書き込み系があれば、検索対象パスと -exec 以降の実引数（いずれも
        # 非フラグ引数として operands に入る）を書き込み対象とみなす
        targets.extend(operands)
    return targets


def _repo_root_of(cwd: str) -> str:
    """`cwd` から上へ辿って `.git` を持つディレクトリ（リポジトリルート）を返す。見つからなければ `cwd`。

    保護パス判定の基点を payload の `cwd` にすると、セッションが以前のターンで
    `cd .claude/hooks` していた場合（Bash ツールの cwd はコマンド間で永続する）、
    保護対象の `.claude` / `.git` セグメントが基点側に吸収されて判定できなくなる。
    ルートへ正規化してから相対パスを取ることでこの取りこぼしを塞ぐ（PR #55 Layer 1 指摘）。
    """
    current = cwd
    while True:
        if os.path.exists(os.path.join(current, ".git")):  # worktree では .git がファイル
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return cwd  # リポジトリ外（テスト用の仮想 cwd 等）は従来どおり cwd を基点にする
        current = parent


def _repo_protected(path: str, cwd: str) -> str | None:
    """`path`（正規化済み絶対パス）が cwd 配下の `.claude/**`・`.git/**` に当たるか。

    cwd からの相対パスをセグメント単位で走査し、どの深さでも `.claude` / `.git` が現れたら
    その名前（ログ用）を返す。`.claude/worktrees/<name>/` は読み飛ばし、その配下でさらに
    `.claude` / `.git` が現れたら再び保護する。一致しなければ None。
    """
    if not _under(path, cwd) or path == cwd:
        return None
    parts = os.path.relpath(path, cwd).split(os.sep)
    i = 0
    while i < len(parts):
        part = parts[i]
        # 除外するのは `.claude/worktrees/<name>/**` であって、コンテナである `.claude/worktrees`
        # 自体ではない（`i + 2 < len(parts)` = worktree 名セグメントが実在するときだけ読み飛ばす。
        # 無いまま読み飛ばすと `rm -rf .claude/worktrees` が保護判定に到達しない・PR #55 Layer 1 指摘）
        if (part == ".claude" and i + 2 < len(parts) and parts[i + 1] == _WORKTREES_DIRNAME):
            i += 3  # `.claude/worktrees/<name>` を読み飛ばして続きを走査
            continue
        if part in _REPO_PROTECTED_DIRS:
            return part
        i += 1
    return None


def _legit_rules_symlink(tokens: list[str], dest_resolved: str, cwd: str, home: str) -> bool:
    """`ln [-s] <src> <cwd>/.claude/rules/<name>` で、リンク元が `<cwd>/docs/rules/` 配下なら正規手順。"""
    index = _command_index(tokens)
    if index is None or os.path.basename(tokens[index]) != "ln":
        return False
    if os.path.dirname(dest_resolved) != os.path.join(cwd, ".claude", "rules"):
        return False
    operands = [a for a in _strip_redirection_args(tokens[index + 1:]) if not a.startswith("-")]
    if len(operands) < 2:
        return False
    src = operands[0]
    if src.startswith("~"):
        src = home + src[1:]
    # 相対リンク元はリンクを置くディレクトリ基準で解決される（`../../docs/rules/x.md` はこの形）
    src_resolved = os.path.realpath(src if src.startswith("/") else os.path.join(os.path.dirname(dest_resolved), src))
    return _under(src_resolved, os.path.join(cwd, _RULES_SYMLINK_SOURCE))


def _repo_protected_hint(protected: str) -> str:
    """ブロック理由に添える代替手段（保護対象ごとに出し分ける）。

    一律に「ネイティブ Edit を使え」と案内すると、symlink 作成のように Edit で代替できない操作で
    回避経路（python3 -c 等）へ流れやすい。代替が存在しない理由を減らすための出し分け。
    """
    if protected == ".git":
        return (
            "  → .git 配下を Bash で直接書き換えない。git コマンド（git config / git update-index / "
            "git stash 等）で操作すること。一時ファイルはリポジトリ直下に作らずセッション scratchpad に置く"
            "（.git/info/exclude で除外する必要自体をなくす）。"
        )
    return (
        "  → .claude 配下（.claude/worktrees を除く）はネイティブ Edit / Write ツールで"
        "書き換えること（PermissionRequest フックが自動承認する。settings.json / settings.local.json だけは"
        "設計どおりユーザー確認に残る・#238）。.claude/rules への symlink 作成は "
        "`bash tools/check_rules_sync.sh --fix` か `ln -s ../../docs/rules/<name>.md .claude/rules/<name>.md`"
        "（リンク元が docs/rules/ 配下のときだけ通る。Edit / Write は symlink を作れない）。"
    )


def _path_like(tokens: list[str]) -> list[str]:
    """`/` か `~` で始まるトークン（パス様トークン）。読み取りも対象にする判定用。"""
    return [t for t in tokens if t.startswith("/") or t.startswith("~")]


def analyze(command: str, cwd: str, home: str, session_id: str = "", _depth: int = 0,
            _repo_root: str | None = None) -> list[str]:
    """ブロック理由のリストを返す（空なら問題なし）。"""
    if _depth > MAX_RECURSION_DEPTH:
        # コマンド置換 / bash -c の入れ子が深すぎて Python の再帰上限に達する前に安全側で打ち切る
        # （fail-closed。Issue #50 レビュー指摘: 未対策だと RecursionError で無整形にクラッシュする）
        return [
            f"コマンド置換 / bash -c の入れ子が深すぎるため安全性を判定できません（上限 {MAX_RECURSION_DEPTH}）\n"
            "  → コマンドを単純化すること。"
        ]
    reasons: list[str] = []
    expanded = _expand_assignments(_strip_heredocs(command))

    home_claude = os.path.realpath(os.path.join(home, ".claude"))
    cwd = os.path.realpath(cwd)
    # 保護パス判定の基点はリポジトリルート（最初の呼び出しの cwd）に固定する。`cd .claude/hooks &&
    # bash -c "sed -i ... x.sh"` のように再帰評価へ入る前に cwd が保護ディレクトリの内側へ移ると、
    # 相対パスから `.claude` / `.git` セグメントが基点側に吸収されて判定できなくなるため
    # （パス解決の基点 cwd と保護判定の基点 repo_root を分ける）
    repo_root = _repo_root_of(cwd) if _repo_root is None else _repo_root

    # コマンド置換 `$(...)` の内側は、セグメント走査を終えてから再帰評価する（`cd` 追跡の結果を
    # 基点候補に含めるため。抽出はテキストベースで位置情報を持たないので、走査中に観測した
    # `cd` 先すべてを基点候補として評価し、いずれかで危険と判定されたらブロックする＝安全側に倒す）
    cd_bases: list[str] = []
    tmpdir = os.environ.get("TMPDIR", "").strip()
    safe_bases = [cwd] + ([os.path.realpath(tmpdir)] if tmpdir else [])
    # `cd` でカレントディレクトリが変わったら以降のセグメントの基点も変える（None = 解決不能）
    current_cwd: str | None = cwd

    def is_safe(path: str) -> bool:
        return (
            any(_under(path, base) for base in safe_bases)
            or _session_tmp_ok(path, session_id)
            or _is_device_sink(path)
        )

    for tokens in _segments(expanded):
        if _toggle_in_segment(tokens):
            continue  # このセグメントだけ明示的に外されている（#582・#583）

        index = _command_index(tokens)
        name = os.path.basename(tokens[index]) if index is not None else None

        # (0) `bash -c` / `sh -c` 等: -c 引数のスクリプト文字列を独立コマンドとして再帰評価する
        if name in SHELL_C_NAMES and current_cwd is not None:
            inner = _shell_c_script(tokens, index)
            if inner:
                reasons.extend(analyze(inner, current_cwd, home, session_id, _depth + 1, repo_root))

        # (0.5) xargs 経由の破壊的コマンドは対象パスが標準入力由来で静的判定できないため一律ブロックする
        if name == "xargs":
            sub = _leading_command_name(tokens[index + 1:])
            if sub in XARGS_DESTRUCTIVE:
                reasons.append(
                    f"xargs 経由の破壊的コマンド（{sub}）: 対象パスが標準入力由来のため安全性を判定できません\n"
                    "  → 対象を明示した個別コマンドに書き換えること。"
                )

        # (1) ホーム配下の Claude 領域への Bash アクセス（読み書き問わず）
        for token in _path_like(tokens):
            resolved = _resolve(token, current_cwd, home)
            if resolved is None:
                continue
            if _under(resolved, home_claude) and not is_safe(resolved):
                reasons.append(
                    f"ホーム配下の Claude 領域への Bash アクセス: {resolved}\n"
                    "  → ネイティブの Read / Grep / Edit ツールを使うこと"
                    "（PermissionRequest フックが自動承認するため無人でも止まらない）。"
                )

        # (2) 作業ディレクトリ・セッション一時領域の外への書き込み / 削除
        # (2') cwd の内側でも、リポジトリ自身の .claude/** と .git/** への Bash 書き込みは保護する
        #      （#618・C1/C3）。is_safe() は cwd 内を無条件に True にするため、必ずその前に判定する。
        for token in _write_targets(tokens):
            if current_cwd is None and not token.startswith(("/", "~")) and not _UNRESOLVED_VAR.search(token):
                # cd 先が解決できず、かつ変数参照でもない相対パス＝安全性を判定する基点が無い（fail-closed・Issue #50）
                reasons.append(
                    f"cd 先が解決できないため相対パスの書き込み / 削除先を判定できません: {token}\n"
                    "  → 絶対パスを使うか、cd 先を静的に解決できる形にすること。"
                )
                continue
            resolved = _resolve(token, current_cwd, home)
            if resolved is None:
                continue
            in_tmp = _session_tmp_ok(resolved, session_id) or any(_under(resolved, b) for b in safe_bases[1:])
            protected = None if in_tmp else _repo_protected(resolved, repo_root)
            if protected is not None:
                if _legit_rules_symlink(tokens, resolved, repo_root, home):
                    continue
                reasons.append(
                    f"リポジトリ内の保護パス（{protected}）への Bash 書き込み: {resolved}\n"
                    + _repo_protected_hint(protected)
                )
                continue
            if is_safe(resolved) or _under(resolved, home_claude):
                continue  # 後者は (1) で報告済み
            reasons.append(
                f"作業領域の外への書き込み / 削除: {resolved}\n"
                "  → 一時作業はセッション scratchpad（システムプロンプトが提示するパス）か"
                "リポジトリ内の作業ディレクトリで行うこと。"
            )

        # (3) `cd` の効果を次のセグメントへ引き継ぐ
        if index is not None and name == "cd":
            raw = tokens[index + 1:]
            if raw and raw[0] == "-":
                # `cd -`（直前のディレクトリへ戻る）は追跡していないので判定不能にする。`-` をフラグ扱いで
                # 落として home へ移動したと誤断定すると、以降の相対パスが実在しない場所へ解決され
                # 保護パスへの書き込みを見逃す（#618 Layer 1 指摘）
                current_cwd = None
            else:
                operands = [t for t in raw if not t.startswith("-")]
                current_cwd = _resolve(operands[0], current_cwd, home) if operands else home
            if current_cwd is not None and current_cwd not in cd_bases:
                cd_bases.append(current_cwd)

    for substitution in _extract_command_substitutions(expanded):
        for base in [cwd, *cd_bases]:
            reasons.extend(analyze(substitution, base, home, session_id, _depth + 1, repo_root))

    # 同一理由の重複を除く（順序は維持）
    return list(dict.fromkeys(reasons))


def _self_test() -> int:
    cwd, home, sid = "/home/user/demo-repo", "/root", "sess-1"
    cases: list[tuple[bool, str]] = [
        # (ブロックされるべきか, コマンド)
        (True, 'WORK=/tmp/demo-out; rm -rf $WORK; mkdir -p $WORK/x'),
        (True, 'cp /root/.claude/projects/a/tool-results/r.txt ./page1.json'),
        (True, 'cp -t /tmp/demo-out file1.txt file2.txt'),
        (True, 'cp --target-directory=/tmp/demo-out file1.txt'),
        (True, 'curl -o /tmp/demo-out/report.json https://example.com/r.json'),
        (True, 'wget -O /tmp/demo-out/a.bin https://example.com/a.bin'),
        (True, 'sed -i "s/a/b/" /etc/demo.conf'),
        (True, 'dd if=/dev/zero of=/tmp/demo-out/blob bs=1M count=1'),
        (True, 'somecmd 2> /tmp/demo-out/err.log'),
        (True, 'echo hi >| /tmp/demo-out/a.txt'),
        (True, 'cd /tmp/other-place && rm -rf temp_output'),
        (True, 'sudo rm -rf /tmp/demo-out/x'),
        (True, 'rm -rf /tmp/claude-0/proj/other-session/scratchpad'),
        (True, 'curl "https://x.example/a?b=1&c=2" -o /tmp/demo-out/report.json'),
        (True, 'cat <<EOF\nintro\nrm -rf /tmp/demo-out\n'),  # 未終端 heredoc は解析対象へ戻す
        # --- リポジトリ内保護パス（#618・C1 / C3 の実例と派生） ---
        (True, 'sed -i "s/a/b/" .claude/hooks/pre-tool-use-router.sh'),  # C1 相当
        (True, 'echo "tmp-content-issues.json" >> .git/info/exclude'),  # C3 相当
        (True, 'cp .claude/hooks/a.sh .claude/hooks/b.sh'),
        (True, 'rm -rf .git/hooks/pre-commit'),
        (True, 'sed -i "s/a/b/" .claude/settings.json'),
        (True, 'sed -i "s/a/b/" .claude/skills/foo/SKILL.md'),  # hooks 以外の .claude/** も保護
        (True, 'echo x >> .claude/agents/reviewer.md'),
        (True, 'echo x | tee -a .claude/hooks/x.sh'),
        (True, 'cd .claude/hooks && sed -i "s/a/b/" dummy.sh'),  # cd 追従
        (True, 'ln -sf ../../docs/x.md .claude/skills/foo/SKILL.md'),  # rules 以外への symlink は除外しない
        (False, 'ln -sf ../../docs/rules/x.md .claude/rules/x.md'),  # 正規手順
        (False, 'echo x > .claude/worktrees/wt-1/marker'),  # 公式の明示除外
        (False, 'git config core.excludesfile .git/info/exclude'),  # git サブコマンド自体は対象外
        (False, 'git update-index --assume-unchanged path/to/file'),
        (False, 'git worktree add ../wt-x'),
        (False, 'bash scripts/apply-to-repo.sh'),  # 内部の cp -a はこの層から不可視（字面ベースの限界）
        (False, 'cat .claude/hooks/pre-tool-use-router.sh'),  # 読み取りは対象外
        (False, 'echo x > docs/a.md'),
        (False, f'cp -r .claude/skills /tmp/claude-0/proj/{sid}/scratchpad/skills'),  # コピー元は対象外
        (True, 'sed -i "s/a/b/" sub/.claude/hooks/x.sh'),  # 深い位置の .claude も保護
        (True, 'sed -i "s/a/b/" .claude/worktrees/wt-1/.claude/hooks/x.sh'),  # worktree 内のフックは再び保護
        (True, 'mv .claude/hooks/pre-tool-use-router.sh /tmp/demo-out/x'),  # 移動元の除去
        (True, 'mv .git/hooks/pre-commit ./saved-hook.sh'),
        (True, 'ln -sf /etc/passwd .claude/rules/evil.md'),  # リンク元が docs/rules 外
        (True, 'ln -sf ../../tools/x.py .claude/rules/x.md'),
        (True, 'echo x > .claude/rules/new.md'),  # symlink 以外の .claude/rules 書き込みは保護
        (True, 'cd .claude/hooks && cd .. && sed -i "s/a/b/" hooks/x.sh'),  # cd .. 追従
        (True, 'ln -sf ../../docs/rules/x.md .claude/rules/x.md && sed -i "s/a/b/" .claude/hooks/x.sh'),
        # ベースは `cd -` を素通り（False）としているが、本リポジトリは Issue #50 の fail-closed
        # （基点不明の相対書き込みは判定不能としてブロック）を併存させているため True になる
        (True, 'cd .claude/hooks && cd - && sed -i "s/a/b/" .claude/hooks/x.sh'),
        (False, f'sed -i "s/a/b/" /tmp/claude-0/proj/{sid}/scratchpad/lab/.claude/hooks/dummy.sh'),  # scratchpad のラボは対象外
        (False, f'cd /tmp/claude-0/proj/{sid}/scratchpad/lab && echo x >> .git/info/exclude'),
        (False, 'ln -s ../../docs/rules/new-rule.md .claude/rules/new-rule.md'),
        (False, 'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 cp local-hook.sh .git/hooks/pre-commit'),  # 脱出ハッチ
        # --- 新機能（保護パス）× 既存の下流パッチ（再帰評価・find）の合成（PR #55 Layer 1 指摘） ---
        (True, 'bash -c "sed -i \'s/a/b/\' .claude/hooks/x.sh"'),
        (True, 'sh -c "echo x >> .git/info/exclude"'),
        (True, 'echo $(sed -i "s/a/b/" .claude/rules/new.md)'),
        (True, 'cd .claude/hooks && bash -c "sed -i \'s/a/b/\' dummy.sh"'),  # 基点が保護配下でも判定できる
        (True, 'cd .git/hooks && bash -c "rm -f pre-commit"'),
        (True, 'cd .claude/hooks && echo $(sed -i "s/a/b/" pre-tool-use-router.sh)'),
        (True, 'find .claude/hooks -type f -exec sed -i "s/a/b/" {} \\;'),  # find 経由の書き換え
        (True, 'find .claude/hooks -type f -execdir sed -i "s/a/b/" {} \\;'),
        (True, 'find . -type f -exec sed -i "s/a/b/" /tmp/demo-out/x \\;'),  # 直書きの書き込み先
        (True, 'rm -rf .claude/worktrees'),  # コンテナ自体は除外しない
        (False, 'find .claude/hooks -type f -exec grep -l ERROR {} \\;'),  # 非破壊なら通す
        (False, f'mkdir -p /tmp/claude-0/proj/{sid}/scratchpad && echo hi > /tmp/claude-0/proj/{sid}/scratchpad/a.txt'),
        (False, 'echo hi > ./notes.md'),
        (False, 'rm -rf node_modules'),
        (False, 'cp /usr/share/doc/readme ./readme'),
        (False, 'grep -rn foo /usr/share/doc'),
        (False, 'cat .claude/settings.json'),
        (False, 'cd docs && rm -rf build'),
        (False, 'curl "https://x.example/a?b=1&c=2" -o ./report.json'),
        (False, 'cat > docs/note.md <<EOF\n例: rm -rf /tmp/demo-out は承認プロンプトになる\nEOF'),
        (False, 'mkdir -p "$EXTERNAL_BASE/x"'),
        (False, 'some-check 2>/dev/null | head -3'),
        (False, 'echo hi > /dev/null'),
        (False, 'dd if=/dev/zero of=/dev/null bs=1M count=1'),
        (False, 'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 rm -f /tmp/demo-out/marker'),
        (True, 'rm -rf /tmp/demo-out; CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 echo done'),
        (True, 'echo "CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1" && rm -rf /tmp/demo-out'),
        (True, 'CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 rm -f /tmp/demo-out/marker && rm -rf /tmp/demo-out/other'),
        (True, 'rm -rf /tmp/demo-out # CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1'),
        (True, 'rm -rf /tmp/demo-out\necho x\nCLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 true'),
        (True, 'cat > docs/n.md <<EOF\nCLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1 と書く\nEOF\nrm -rf /tmp/demo-out'),
        # Issue #50: PR #49 の Layer 1 セルフレビューで見つかった 4 件の検知漏れ
        (True, 'bash -c "rm -rf /tmp/demo-out"'),
        (True, 'sh -c "rm -rf /tmp/demo-out"'),
        (True, 'find /tmp/demo-out -type f -delete'),
        (True, 'find /tmp/demo-out -exec rm -rf {} \\;'),
        (True, 'echo $(rm -rf /tmp/demo-out)'),
        (True, 'cd "$(mktemp -d)"; rm -rf newfile'),
        (True, 'find /tmp/demo-out -type f | xargs rm -f'),
        (False, 'bash -c "echo hello"'),
        (False, 'find . -name "*.pyc"'),
        (False, 'find . -name "*.log" | xargs grep -l ERROR'),
        (False, 'echo $(pwd)'),
        # Layer 1 セルフレビュー指摘（CONFIRMED・PR #51）: xargs の値取りフラグ誤認・bash 複合フラグ・再帰爆弾
        (True, 'find /tmp/demo-out -type f | xargs -I {} bash -c "rm -rf {}"'),
        (True, 'bash -euc "rm -rf /tmp/demo-out"'),
        (True, 'bash -lc "rm -rf /tmp/demo-out"'),
        (True, 'echo ' + '$(' * 30 + 'pwd' + ')' * 30),
        (False, 'find . -name "*.txt" | xargs -I {} grep -l ERROR {}'),
        (True, 'bash -c "bash -c \'rm -rf /tmp/demo-out\'"'),
        (True, 'echo $(echo $(rm -rf /tmp/demo-out))'),
        # --- publish-sync のマーカー削除（Issue #650） ---
        # 修正前: マーカーが $CHECKOUT の兄弟パスにあり、Step 4 が $CHECKOUT へ cd した後の
        # 削除は cwd（$CHECKOUT）の外への書き込みとして本ガードにブロックされていた。
        (True, 'cd /workspace/claude-code-repository-base && rm -f /workspace/claude-code-repository-base.publish-verified'),
        # 修正後: マーカーを $SRC（本リポジトリ）配下の gitignore 済みパスへ移し、$CHECKOUT へは
        # cd せず git -C で操作するため、cwd は常に $SRC のまま = マーカー削除は cwd の内側で通る。
        # SKILL.md は相対パス（`content/pipeline-state/.publish-verified`）で参照する（絶対パスの
        # ハードコード重複を避けるため・#657 セルフレビュー指摘）。
        (False, 'rm -f /home/user/demo-repo/content/pipeline-state/.publish-verified'),
        (False, 'MARKER="content/pipeline-state/.publish-verified"\ntest -f "$MARKER" || exit 1\nrm -f "$MARKER"'),
    ]
    failures = 0
    for expect_block, command in cases:
        actual = bool(analyze(command, cwd, home, sid))
        if actual != expect_block:
            failures += 1
            print(f"  NG 期待={'BLOCK' if expect_block else 'ALLOW'} 実際={'BLOCK' if actual else 'ALLOW'}: {command!r}")
    print(f"[workspace_write_guard --self-test] PASS={len(cases) - failures} FAIL={failures}")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    if os.environ.get("CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # 入力が読めないときは素通り（フックで作業を止めない）
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0
    cwd = os.path.normpath(payload.get("cwd") or os.getcwd())
    home = os.path.normpath(os.path.expanduser("~"))
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    reasons = analyze(command, cwd, home, session_id)
    if not reasons:
        return 0

    print("BLOCK: 承認プロンプトになるコマンドです（無人ルーティンが停止するため事前に差し戻します）")
    for reason in reasons:
        print(f"- {reason}")
    print(
        "背景: クラウドでは Bash サンドボックスが起動できず、作業ディレクトリ外への書き込みは "
        "auto モードの classifier が自動承認しない（Issue #578・実測）。"
    )
    print(
        "どうしても外部パスが必要な場合のみ "
        "`CLAUDE_BASE_DISABLE_WORKSPACE_WRITE_GUARD=1` を付けて実行する"
        "（対話セッションでは承認プロンプトが出る前提で使うこと）。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
