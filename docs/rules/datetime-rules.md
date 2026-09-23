# 日時表記ルール（Datetime Rules・JST 統一 SSOT）

> **このファイルは「日時を表示・記録するときのタイムゾーン基準」の唯一の正本（SSOT）である。**（飼い主の明示決定・Issue #75）

## 0. 大原則: 日時は JST で統一する

**ユーザーに見える、または記録（コミットメッセージ・Issue / PR コメント・ログ・通知・スナップショット）に残る日時は、すべて JST（Asia/Tokyo・UTC+9）で表記する。**

- チャットでユーザーに日時を伝えるときも **必ず JST**（UTC で答えない）。システム注入の時刻が UTC 由来でも **JST に換算** して示す（UTC = JST − 9 時間）。
- フォーマットは `YYYY-MM-DD HH:MM JST`（日付のみで足りる場合は `YYYY-MM-DD`）。

## 1. 唯一の例外: 機械処理用の UTC は維持する

以下は **JST 化してはならない**（人間が読む日時ではなく、機械が解釈する値・内部計算用）。

- 外部 API に渡す ISO 8601（GitHub API の `after_timestamp` 等・`date -u +"%Y-%m-%dT%H:%M:%SZ"`）— API 仕様が UTC `Z` を要求。JST 化すると壊れる
- 内部の経過時間・stale 判定（`datetime.now(timezone.utc)` の差分）— 表示しないため基準が一貫していれば正しい
- エポック秒・mtime 差分（`date +%s`）— TZ 非依存
- UTC↔JST↔PT 換算表（`token-optimization-rules.md`）— 換算が目的なので UTC 併記が正しい

**判定基準**: 「その日時を **人間が読む / 記録として残す** か？」→ YES なら JST。「機械が解釈する / 内部計算にのみ使う（表示しない）」→ UTC 維持で可。

## 2. 実装パターン

Python は表示・記録用に `datetime.now(timezone(timedelta(hours=9)))`、機械処理用は `datetime.now(timezone.utc)`。`datetime.utcnow()` / TZ 未指定の `datetime.now()` は表示・記録用途で使わない。シェルは表示・記録用に `TZ="${PROJECT_TZ:-Asia/Tokyo}" date '+%Y-%m-%d %H:%M %Z'`（リテラル `JST` の直書きは禁止・`%Z` を使う・#79）、機械処理用は `date -u +"%Y-%m-%dT%H:%M:%SZ"`。コード例全文は `datetime-rules-detail.md`。

日時テンプレートに時刻を含めるときは必ず ` JST` を付ける（`{YYYY-MM-DD HH:MM JST}`）。日付のみのテンプレートは `$(TZ=Asia/Tokyo date +%Y-%m-%d)` で生成し、コンテナ TZ に依存させない。

## 3. 完了・成功の定義

チェックリスト（§0〜§2 の再掲）は `datetime-rules-detail.md`「完了・成功の定義」。判定の実体は
`python3 tools/check_datetime_tz.py`（表示・記録系の TZ 未指定 `datetime` 残存ゼロ・#80）が持ち、
`tools/self_review_check.py` から PR 前チェックで自動実行される。

> 関連: `tools/check_datetime_tz.py`（機械チェック）/ `tools/generate_project_context.py`（スナップショット時刻）/ `datetime-rules-detail.md`（実装パターン全文）/ `session-safety-rules.md`（JST 明示の模範テンプレート）
