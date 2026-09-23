---
name: discussion-review
description: 議論型レビュー（敵対的相互レビュー）をネイティブ Agent Teams（name 付き Agent tool + SendMessage + 共有ホワイトボード）で実行する。「専門チームを組成して」「チームで議論して」「議論型レビューして」「/discussion-review」と依頼された時、および pr-review-watcher の Layer 2 自動起動（discussion_review_trigger.py が対象と判定した時）に使用する。claude -p サブプロセスは起動しない（失敗時のみ tools/run_discussion_review.py へフォールバック）。役割分担型 fan-out（独立評価の集計）で足りる軽微タスクには使わない。
effort: high
---

# discussion-review — ネイティブ議論型レビュー

> **位置付け**: 議論型レビューの **既定経路**（Phase 2 切替済み。移行検討の経緯は開発リポジトリの提案記録として保持）。
> 旧経路 `tools/run_discussion_review.py`（claude -p 駆動）は **フォールバック** として存置。
> 実行前に `docs/rules/discussion-whiteboard-rules.md` を必ず Read すること（ホワイトボード規約の SSOT）。

## 0. 実行モデル（オーケストレーター駆動ラウンド制）

メインセッション（あなた）が lead 進行役を兼ね、参加者を **name 付き background Agent** で起動する。
チーム作成手続きは不要（1 セッションに単一の暗黙チーム）。通信は次の 2 チャネルに限定する:

| チャネル | 用途 | 制約 |
|---------|------|------|
| **ホワイトボード**（`tools/discussion_whiteboard.py`） | 議論内容の SSOT（claim/rebuttal/consensus/verdict）。git 永続化 | 投稿は必ず `post`（直接 Write/Edit 禁止） |
| **SendMessage**（名前宛て） | ラウンド進行の合図（完了済み参加者の再開）のみ | **受信側がターン終了済みだとサブ発メッセージは消失する**（実測・V-5）。ラウンドを跨ぐ伝達に使わない。参加者同士の直接往復は同時稼働中のみ |

**禁止**: 共有タスクボード（TaskCreate 等）を参加者に使わせる設計（サブエージェント側から利用不可・V-6）。

## 1. 入力

- **spec JSON**（現行互換・`tools/discussion_specs/*.json`）: `topic` / `brief` / `participants[]`（`name`・`model`・`lens`）/ `synthesizer`（`name`・`instruction`）/ `verdict_schema`
- **targets**: レビュー対象パス（リポジトリ絶対パスに正規化して参加者へ渡す）
- **rounds**: 既定 2（round 1 = 独立分析、round 2 = 相互反論）
- **議題 ID**: 英数字 + `_.-`・先頭英数字・64 字以内（`content/discussions/<id>/` に保存）

## 2. 手順

### Step 0: 準備

1. `docs/rules/discussion-whiteboard-rules.md` を Read（未読なら）。
2. spec を Read し検証する（participants ≥ 2・name は英数字と `_-` 32 字以内・targets の存在確認）。
3. ホワイトボード初期化:
   ```bash
   python3 tools/discussion_whiteboard.py init <id> --topic "<topic>" --participants "<name1>,<name2>,..." --brief "<brief>"
   ```

### Step 1: Round 1（独立分析・並列）

各 participant を **1 つの Agent 呼び出しにつき 1 名**、同一メッセージ内で並列起動する:

- `name`: spec の participant name（そのまま。SendMessage の宛先になる）
- `model`: spec の `model`（`haiku`/`sonnet` 等。model mix は whiteboard-rules §4.5 準拠）
- `run_in_background`: true
- プロンプトに必ず含める: ① 自分の name とレンズ（lens） ② 対象の **絶対パス** ③ 投稿コマンド
  （`python3 <REPO>/tools/discussion_whiteboard.py post <id> --author <name> --round 1 --kind claim --body-file <一時ファイル>`。
  本文が複数行なら必ず `--body-file` か stdin） ④ 「whiteboard.md を直接編集しない」
  ⑤ 「最終出力は post 済みの旨 + 1 行サマリーのみ（分析全文を返さない）」
  ⑥ 「後続ラウンドはオーケストレーターからのメッセージで再開される。自分からポーリング・待機しない」

全参加者の完了通知を待つ。**待ち方は「そのターンを終えること」**（通知は後のターンで届く）。
`sleep` ループ・`ReadNotifications` 連打・`TaskGet` / `/tasks` の進捗確認など **手段を問わずポーリングしない**。
「完了待機中」の実況も出さない（`agent-team.md` §F-8「『待つ』の作法」・L-132）。
揃ったら `render` を実行し投稿を確認する（`list <id> --round 1 --json` で participant 全員分の post があること）。

### Step 2: Round 2..N（敵対的相互反論）

各参加者へ **SendMessage（名前宛て）** で再開指示を送る（完了済みエージェントはトランスクリプトから再開される・V-4）:

> round k を開始する。`python3 <REPO>/tools/discussion_whiteboard.py show <id>` で他者の投稿を読み、
> **相手の具体的な指摘への rebuttal / concession** を `--round k --kind rebuttal`（譲歩は `concession`）で post せよ。
> 対象ファイルは再読しない（round 1 の自分の分析とホワイトボードのみで反論する・再読トークン削減）。
> post 後はターンを終えてよい。

全員の完了通知を待ち（Step 1 と同じ作法。ポーリング・待機実況をしない）、`render` する。

### Step 3: 合意・verdict（lead = メインセッション）

`show` で全投稿を読み、spec の `synthesizer.instruction` に従って対立点・合意点を整理し、

```bash
python3 tools/discussion_whiteboard.py post <id> --author lead --round <N+1> --kind consensus --body-file <ファイル>
python3 tools/discussion_whiteboard.py post <id> --author lead --round <N+1> --kind verdict --body-file <ファイル>
```

verdict 本文は spec の `verdict_schema` に従う JSON **のみ** を書く（後続の機械処理のため）。
**critical は「議論を経ても残った真の問題」だけ** に絞る（相互検証で否定された指摘は除外）。

### Step 4: 締め

```bash
python3 tools/discussion_whiteboard.py render <id>
python3 tools/discussion_whiteboard.py list <id> --json   # verdict の存在確認
git add content/discussions/<id>/ && git commit -m "discussion: <id> 議論記録" && git push
```

verdict JSON を呼び出し元（スキル・ユーザー報告）へ返す。

## 3. ガードレール

1. **参加者無応答**: 他の参加者の通知が揃ったあとも 1 名分だけ届かない状態が続く場合（目安 10 分。
   **経過時間を見るためにポーリングしない** — 他の作業を進めて自然にターンが進んだ時点で判定する）、
   SendMessage で 1 回だけ状況確認を送る。それでも応答がなければその参加者を欠席扱いにして進行し、
   verdict にその旨を記録する。
2. **サーキットブレーカー**: 進行不能（起動失敗・投稿ゼロ）が 2 回連続したら STOP → フォールバックへ。
3. **フォールバック（サイレント禁止）**: ネイティブ実行が成立しない場合（Agent/SendMessage 不可・
   参加者全滅等）は旧経路へ退避し、退避理由を 1 行ログ（Issue/PR コメント）に残す:
   ```bash
   python3 tools/run_discussion_review.py --id <id> --spec <spec> --targets "<t1,t2>" --rounds 2
   ```
   旧経路も失敗したら fan-out（Agent 並列の独立評価）へ最終退避する。
   退避の標準形（ランタイム判定・退避ログ・撤去判断）の SSOT は `docs/rules/native-fallback-rules.md`。
4. **予算**: ネイティブ経路はメインセッションのサブスク/セッション課金に一本化される（claude -p の
   API キー除去ハックは不要）。参加者の最終出力は 1 行サマリーに限定しコンテキスト消費を抑える（R-4）。

## 4. 完了・成功の定義

- [ ] `content/discussions/<id>/whiteboard.md` に claim → rebuttal → consensus/verdict が揃っている
- [ ] verdict JSON（spec の verdict_schema 準拠）が取得できた
- [ ] 議論記録がコミットされている
- [ ] フォールバックした場合、退避理由が記録されている

## 5. 参照

| ドキュメント | 関係 |
|------------|------|
| `docs/rules/discussion-whiteboard-rules.md` | ホワイトボード規約・model mix・ガードレールの SSOT |
| `docs/rules/agent-team-summary.md` | 協調モード振り分け（議論型 vs fan-out）の SSOT |
| `tools/discussion_whiteboard.py` | ホワイトボード基盤（init/post/render/list/show） |
| `tools/run_discussion_review.py` | 旧経路（claude -p）・フォールバック |
| `tools/discussion_review_trigger.py` | Layer 2 自動起動の判定（pr-review-watcher から呼ばれる） |

> 移行の経緯・実機検証（V-1〜V-6）・制約の根拠は開発リポジトリの提案記録として保持している。
