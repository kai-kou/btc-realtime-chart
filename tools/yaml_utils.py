"""
yaml_utils.py — meta.yaml 書き込み共通ユーティリティ

L-093 対策: yaml.dump() の multiline 文字列が block scalar (`|`) ではなく
単一引用符形式 (`'...\n...'`) になる問題を共通の Dumper で解消する。

複数行を含む文字列のみ block scalar (`|`) に変換し、1行文字列は通常の
plain/single-quoted で出力する。
"""
from typing import IO, Any

import yaml


class BlockScalarDumper(yaml.SafeDumper):
    """multiline 文字列を block scalar (`|`) として出力する Dumper。"""
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


BlockScalarDumper.add_representer(str, _str_representer)


def dump_meta_yaml(data: dict[str, Any], stream: IO[str]) -> None:
    """meta.yaml データを block scalar 保持で書き込む。

    Args:
        data: meta dict（description 等の multiline 文字列を含みうる）
        stream: 書き込み先のテキストモードファイルオブジェクト
    """
    yaml.dump(
        data,
        stream,
        Dumper=BlockScalarDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )
