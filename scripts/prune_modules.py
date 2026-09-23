#!/usr/bin/env python3
"""prune_modules.py — modules.yaml で enabled:false のモジュールの資産を除去する。

bootstrap.sh --prune から呼ばれる。PyYAML が無ければ自動 pip install を試みる。
除去対象: rules（docs/rules + .claude/rules の symlink）/ hooks / skills / tools。
required:true のモジュールは無効化を拒否する。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    os.system(f"{sys.executable} -m pip install pyyaml --quiet")
    try:
        import yaml
    except ImportError:
        print("[prune] PyYAML が無く install も失敗。手動で無効モジュールのファイルを削除してください。")
        sys.exit(1)


def main(root: str):
    root = Path(root)
    data = yaml.safe_load((root / "modules.yaml").read_text(encoding="utf-8"))
    removed = []
    for name, mod in (data.get("modules") or {}).items():
        if mod.get("enabled", True):
            continue
        if mod.get("required"):
            print(f"[prune] '{name}' は required のため無効化をスキップします")
            continue
        for r in mod.get("rules", []) or []:
            (root / ".claude" / "rules" / r).unlink(missing_ok=True)
            removed.append(f"rules/{r}")
        for h in mod.get("hooks", []) or []:
            (root / ".claude" / "hooks" / h).unlink(missing_ok=True)
            removed.append(f"hooks/{h}")
        for s in mod.get("skills", []) or []:
            d = root / ".claude" / "skills" / s
            if d.exists():
                import shutil
                shutil.rmtree(d)
                removed.append(f"skills/{s}")
        for t in mod.get("tools", []) or []:
            (root / "tools" / t).unlink(missing_ok=True)
            removed.append(f"tools/{t}")
    print(f"[prune] removed {len(removed)} asset(s):")
    for r in removed:
        print(f"  - {r}")
    # symlink 整合を取り直す
    sync = root / "tools" / "check_rules_sync.sh"
    if sync.exists():
        os.system(f"bash {sync} --fix")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
