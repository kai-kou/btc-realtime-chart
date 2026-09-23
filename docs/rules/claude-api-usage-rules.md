# Claude API 利用ルール

## 大原則: `anthropic` Python ライブラリを直接使わない

このプロジェクトは **Claude.ai クラウド環境（Scheduled Tasks）** で実行される。
クラウド環境では Claude Code 自身が認証済みセッションとして動作するため、
`ANTHROPIC_API_KEY` は不要であり、設定してはいけない。

### 禁止パターン

```python
# ❌ 禁止: anthropic ライブラリの直接インポート・使用
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(model="claude-haiku-4-5-20251001", ...)

# ❌ 禁止: ANTHROPIC_API_KEY を参照するコード
import os
api_key = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=api_key)
```

### 正しいパターン: `claude -p` サブプロセス

```python
# ✅ 正解: claude -p サブプロセスを使う
import os
import subprocess

def _call_claude(prompt: str, timeout: int = 60) -> str:
    """claude -p サブプロセスで AI 生成を行う（ANTHROPIC_API_KEY 不要）"""
    cmd = ["claude", "-p", "--output-format", "text"]
    # CLAUDECODE 環境変数を除去（サブプロセス起動時の競合防止）
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p が終了コード {result.returncode} で失敗しました\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip()
```

## なぜ `claude -p` サブプロセスを使うのか

| 項目 | `anthropic` ライブラリ直接 | `claude -p` サブプロセス |
|------|--------------------------|------------------------|
| 認証方法 | `ANTHROPIC_API_KEY` 必須 | Claude Code セッション認証を共有（不要） |
| クラウド環境での動作 | キーが消えると動作不能 | 常に動作する |
| コスト管理 | 別途 API 使用量が発生する可能性 | Claude Code のセッション内に統合 |
| 依存関係 | `anthropic` パッケージが必要 | 不要（`claude` CLI のみ） |

## system プロンプトが必要な場合

```python
def _call_claude_with_system(system: str, user_prompt: str, timeout: int = 60) -> str:
    """system プロンプト付きで claude -p を呼び出す"""
    # system と user を1つのプロンプトに結合して渡す
    full_prompt = f"System: {system}\n\n{user_prompt}"
    return _call_claude(full_prompt, timeout=timeout)
```

## JSON 出力が必要な場合

```python
import json

def _call_claude_json(prompt: str, timeout: int = 60) -> dict | list:
    """JSON 形式で返す claude -p 呼び出し"""
    result = _call_claude(prompt, timeout=timeout)
    # コードブロックがある場合は除去
    content = result.strip()
    if content.startswith("```"):
        import re
        content = re.sub(r"^```(?:json)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    return json.loads(content)
```

## `CLAUDECODE` 環境変数の除去が必要な理由

Claude Code セッション内で `claude -p` をサブプロセスとして起動する際、
`CLAUDECODE` 環境変数が引き継がれると子プロセスが「すでにセッション内で実行中」と判断して
予期しない挙動をすることがある。環境変数を除去することで問題を回避する。

```python
env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
```

## 既存スクリプトの変換方法

`tools/*.py` で `import anthropic` が含まれている場合:

1. `import anthropic` と関連インポート（`os` のみの用途など）を削除
2. `import subprocess` を追加
3. `_call_claude()` ヘルパー関数を追加
4. `client.messages.create(...)` 呼び出しを `_call_claude(prompt)` に置き換え
5. `ANTHROPIC_API_KEY` チェックのコードを削除

## 参照実装

> ⚠️ 以下の `tools/*.py` は **出自プロジェクト（動画制作）の実例**。汎用ベースには存在しないため、
> 自分のプロジェクトで `import anthropic` している箇所に読み替えること（`_call_claude()` ヘルパーに
> 集約する原則が本体）。

以下のファイルに正しい実装例がある:

- `tools/generate_comment_reply.py` — `_call_claude()` ヘルパー + システムプロンプト統合 <!-- refcheck:ignore -->
- `tools/adjust_subtitle_lines.py` — バッチ処理での `_call_claude()` 使用例 <!-- refcheck:ignore -->
- `.claude/skills/skill-creator/scripts/improve_description.py` — 最初期の実装例

## 禁止事項

- `tools/*.py` に `import anthropic` を書く
- `ANTHROPIC_API_KEY` を `docs/rules/env-vars.md` の必須変数として記載する
- ユーザーに `ANTHROPIC_API_KEY` の設定を依頼する
- `anthropic.Anthropic()` や `anthropic.AsyncAnthropic()` をインスタンス化する
