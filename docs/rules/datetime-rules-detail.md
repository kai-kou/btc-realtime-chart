# 日時表記ルール — 実装パターン詳細（Warm 層）

> **本ファイルは `datetime-rules.md`（Hot・SSOT）の実装コード例の補完版**。
> Hot 層予算の棚卸し（Issue #146）で §2 実装パターンを本ファイルへ移設した（サマリー＝ポインタ規約）。
> 原則・例外・完了条件の正本は引き続き Hot 版。

---

## 実装パターン（新規コードはこれに従う）

### Python

```python
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))  # 日本は DST がないため固定オフセットで正確

# 表示・記録用
now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

# 機械処理用（API・内部計算）は従来どおり UTC
now_utc = datetime.now(timezone.utc)
```

- `datetime.utcnow()` / `datetime.now()`（TZ なし）を **表示・記録用途で使わない**（コンテナのローカル TZ に依存して不定になる）。
- `self-review-checklist.md` は TZ 未指定の `datetime.now()` をコードの問題として検出する。

### シェル

```bash
# 表示・記録用（コミットメッセージ・ログ）。動的に生成するシェルのタイムスタンプは
# %Z（実行時の TZ 略称）を使う。リテラル "JST" を直書きすると PROJECT_TZ を
# 上書きしたとき「JST と書いてあるのに実体は別 TZ」の嘘表記になるため。
# 既定（PROJECT_TZ 未設定）では Asia/Tokyo なので %Z は "JST" を出す。
timestamp=$(TZ="${PROJECT_TZ:-Asia/Tokyo}" date '+%Y-%m-%d %H:%M %Z')
day=$(TZ="${PROJECT_TZ:-Asia/Tokyo}" date '+%Y-%m-%d')

# 機械処理用（GitHub API 等）は UTC のまま
rereview_ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
```

- ハーネス（hooks）の既定 TZ は `Asia/Tokyo`（`PROJECT_TZ` で上書き可能）。`TZ="${PROJECT_TZ:-Asia/Tokyo}"` の形を使う。
- **動的なシェル `date` のタイムスタンプはリテラル `JST` を直書きせず `%Z` を使う**（上記コメント参照・Issue #79）。Python は固定の `JST` 定数（`timezone(timedelta(hours=9))`）を使うためリテラル `JST` 表記で正しい（定数が +9 固定なので嘘にならない）。プロセス・ドキュメントのテンプレートで「このプロジェクトの既定＝JST」を説明する文中の `JST` 表記も可。
- `${PROJECT_TZ:-UTC}` のように **UTC を既定にしない**。

---

## 完了・成功の定義

> Hot 層予算の再棚卸し（#56）で `datetime-rules.md` §3 から移設した（§0〜§2 の再掲であり、各項目に機械チェックが実在するため）。

- [ ] ユーザーに伝える日時・表示/記録系コードの日時が JST 基準（API 用 UTC を除く）
- [ ] ハーネス（hooks）の既定 TZ が `Asia/Tokyo`、シェル `date` が `%Z`（リテラル直書きでない）
- [ ] 機械処理用 UTC（API・内部計算）は維持されている
- [ ] `python3 tools/check_datetime_tz.py` が PASS（表示・記録系の TZ 未指定 `datetime` 残存ゼロ・#80）
