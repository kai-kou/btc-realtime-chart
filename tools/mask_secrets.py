#!/usr/bin/env python3
"""
mask_secrets.py — 秘匿情報マスクユーティリティ（P-12）

print / log 出力時に環境変数の値を印字側でマスクする。
Claude Code のコンテキストやターミナルへの秘匿情報流出を防ぐ。

NOTE: GitHub Variables の一覧表示には必ず以下のいずれかを使うこと:
  - python3 tools/setup_github_variables.py --list  （マスク表示）
  - python3 tools/gh_vars.py --json                 （キー一覧のみ・値は *** ）
  ❌ gh variable list を直接実行すると全ての値が平文でターミナルに流れる
"""

import re

# 秘匿性の高い変数名に含まれるキーワードパターン（大文字小文字不問）
_SENSITIVE_PATTERNS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "KEY",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "PRIVATE",
    "ACCESS",
)

_SENSITIVE_RE = re.compile(
    "|".join(_SENSITIVE_PATTERNS),
    re.IGNORECASE,
)


def is_sensitive_var(name: str) -> bool:
    """変数名が機密パターンに一致するか判定する。"""
    return bool(_SENSITIVE_RE.search(name))


def mask_value(value: str, keep_start: int = 4, keep_end: int = 4) -> str:
    """値をマスク表示する（先頭/末尾を数文字残して中間を **** に置換）。

    Args:
        value: マスク対象の値（None・空文字も安全に処理する）
        keep_start: 先頭に残す文字数
        keep_end: 末尾に残す文字数

    Returns:
        マスクされた文字列。None・空文字・短い値は "****" のみ返す。

    Examples:
        mask_value("xoxb-abc123def456")    -> "xoxb****f456"
        mask_value("short")               -> "****"
        mask_value("")                    -> "****"
        mask_value(None)                  -> "****"
    """
    if value is None:
        return "****"
    value = str(value)
    if not value:
        return "****"
    if len(value) <= keep_start + keep_end:
        return "****"
    return value[:keep_start] + "****" + value[-keep_end:]


def mask_if_sensitive(name: str, value: str) -> str:
    """変数名が機密パターンに一致する場合のみマスクして返す。

    Args:
        name: 環境変数名
        value: 環境変数の値（None・空文字も安全に処理する）

    Returns:
        機密パターン一致 → mask_value(value) の結果
        非一致 → value をそのまま返す
        None / 空文字 → "" を返す
    """
    if not value:
        return ""
    if is_sensitive_var(name):
        return mask_value(value)
    return value
