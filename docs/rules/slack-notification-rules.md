# Slack 通知連携セットアップガイド

Claude Code のフックから Slack チャンネルへ通知を送る仕組みのセットアップ手順と運用ルール。

> ⚠️ **`@mention`（ユーザー対応が必要）通知の厳選は `docs/rules/user-notification-triage.md`（SSOT）に従う。**
> `waiting` / `daily-progress` 等の `@mention` は **A-1〜A-6 該当時のみ** 発火する（`tools/triage_notification.py` でトリアージ）。
> 障害（バグ・エラー）起因や B/C/D 区分は `@mention` せず Claude が自律処理する（L-077）。

## 概要

| 項目 | 内容 |
|------|------|
| メイン通知先 | `#all-yourproject-updates` チャンネル（情報提供系） |
| 承認依頼通知先 | **承認依頼専用チャンネル**（要アクション系。`@mention` は機械トリアージが A-1〜A-6 該当と判定したときだけ発火する・`user-notification-triage.md` §2） |
| 公開通知先 | **公開・マーケティング専用チャンネル**（公開・配信完了イベント・メンション付き。既定は `published` のみ。動画公開・SNS配信等のプロジェクト固有イベントは `config/publish_events.yaml` に追記する） |
| 通知トリガー | 半日アウトカムサマリー（07:00/19:00 自動）、PR 作成・待機・パイプライン完了・公開イベント（スキル内から明示呼び出し）。※セッション開始/終了の自動通知は **#2597 で廃止**（通知氾濫の解消） |
| 実装 | `tools/slack_notify.py`（Slack Web API / Block Kit） |
| フック | `.claude/hooks/stop-slack-notify.sh`（WIP 自動コミット・cost_log 追記のみ。Slack 通知は #2597 で廃止）。半日サマリーは `tools/half_day_summary.py` をスケジュールスロットから実行 |
| 設定方法 | Claude.ai 環境変数（`SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` / `SLACK_APPROVAL_CHANNEL_ID` / `SLACK_PUBLISH_CHANNEL_ID`）※ローカル例外用途として `.claude/settings.local.json`（Git 管理外）も利用可 | <!-- refcheck:ignore -->

## チャンネル分離設計

通知をタイプ別に **3 チャンネル** へ振り分け、ユーザーが承認依頼・公開通知を見落とさないようにする。

| 通知タイプ | チャンネル | 環境変数 | 目的 |
|-----------|-----------|----------|------|
| ~~`session-start` / `session-stop`~~ | — | — | **廃止（#2597）**: セッション単位通知が氾濫の主因（約64通/日・全体の75〜85%）。半日アウトカムサマリーに集約した |
| `half-day-summary` | **公開・マーケティング専用チャンネル** | `SLACK_PUBLISH_CHANNEL_ID` | **半日アウトカムサマリー**（朝07:00/夜19:00・PR消化/進捗/稼働/制作中/要対応A区分・要対応ゼロでもハートビート1通） |
| `pr` / `pipeline` / `message` / `progress` | メインチャンネル | `SLACK_CHANNEL_ID` | 完了報告・情報提供 |
| `approval` / `waiting` | **承認依頼専用チャンネル** | `SLACK_APPROVAL_CHANNEL_ID` | **要アクション**（`@mention` は A-1〜A-6 該当時のみ・§2 のトリアージ） |
| `publish` | **公開・マーケティング専用チャンネル** | `SLACK_PUBLISH_CHANNEL_ID` | **公開通知**（`@mention` は `fyi: false` イベントと A 区分のみ） |

フォールバックチェーン:
- `SLACK_APPROVAL_CHANNEL_ID` 未設定 → `SLACK_CHANNEL_ID`
- `SLACK_PUBLISH_CHANNEL_ID` 未設定 → `SLACK_APPROVAL_CHANNEL_ID` → `SLACK_CHANNEL_ID`

### publish の --event-type（config-driven・#358）

`--event-type` の値は **`config/publish_events.yaml` のキー** で決まる（本体コードにはドメイン固有語を持たない）。汎用ベースの既定は `published`（公開完了・汎用）のみ。

| event-type | 説明 |
|-----------|------|
| `published` | 公開完了（汎用・既定） |

各イベントの定義項目（`emoji` / `header` / `message` / `action_label` / `action_url` / `fyi`）は `config/publish_events.yaml` 冒頭のコメントを参照。`message` は `{mention}` を、`action_url` は `{url}` を含めると `--url` の値で展開される。`fyi: true` にするとユーザー操作不要の完了報告として `@mention` を抑制する。

#### project example（動画公開プロジェクトの場合）

動画公開・SNS配信のようなドメイン固有イベントを使うプロジェクトは、`config/publish_events.yaml` に以下のようなキーを追記する（本体コードの変更は不要）:

```yaml
events:
  unlisted:
    emoji: "📤"
    header: "動画 限定公開アップロード完了"
    message: "{mention}動画を限定公開でアップロードしました。YouTube Studio で内容を確認してください。"
    action_label: "YouTube Studio で確認"
    action_url: "https://studio.youtube.com/"
    fyi: false
  scheduled:
    emoji: "📅"
    header: "動画 公開スケジュール設定完了"
    message: "{mention}動画の公開スケジュールを設定しました。"
    action_label: null
    action_url: null
    fyi: false
  pre-publish:
    emoji: "⏰"
    header: "動画 公開前日リマインダー"
    message: "{mention}明日公開予定の動画があります。メタデータ・サムネイルを最終確認してください。"
    action_label: "YouTube Studio で確認"
    action_url: "https://studio.youtube.com/"
    fyi: false
  public:
    emoji: "🎉"
    header: "動画 公開完了！"
    message: "{mention}動画が公開されました！"
    action_label: "YouTube で視聴"
    action_url: "{url}"
    fyi: false
  shorts-public:
    emoji: "🎬"
    header: "Shorts 限定公開アップロード完了"
    message: "{mention}Shorts 動画を限定公開でアップロードしました。YouTube Studio で内容を確認してください。"
    action_label: "YouTube Studio で確認"
    action_url: "https://studio.youtube.com/"
    fyi: false
  sns-complete:
    emoji: "📣"
    header: "SNS・BLOG 配信完了"
    message: "{mention}SNS・BLOG への配信が完了しました。"
    action_label: null
    action_url: null
    fyi: true
  marketing-review:
    emoji: "📊"
    header: "週次マーケティングレポート 生成完了"
    message: "{mention}週次マーケティングレポートを生成しました。GitHub Issues でレビューしてください。"
    action_label: null
    action_url: null
    fyi: true
```

`fyi: true`（`sns-complete` / `marketing-review`）はユーザー操作不要の完了報告のため `@mention` を抑制する。それ以外は確認・節目アクション（A-2 相当）のため `@mention` を維持する（`user-notification-triage.md`）。

### 推奨チャンネル設定

| チャンネル | 名前例 | 通知設定（推奨） |
|-----------|--------|----------------|
| メインチャンネル | `#all-yourproject-updates` | **mute** 推奨（大量に流れるため） |
| 承認依頼専用チャンネル | `#approval-yourproject` | **通常通知**（見落とし防止） |
| 公開・マーケティング専用チャンネル | `#publish-yourproject` | **通常通知**（見落とし防止） |

---

## 1. Slack App の作成

### 1.1 新規 App を作成

1. [api.slack.com/apps](https://api.slack.com/apps) → **「Create New App」** → **「From scratch」**
2. App Name: `YourProjectBot`（任意。日本語可）
3. Workspace を選択 → **「Create App」**

### 1.2 Bot User を追加する（必須）

> **ハマりポイント**: Bot User が未設定のままインストールしようとすると
> 「ボットユーザーがありません」エラーが出てインストールできない。
> 必ず最初に Bot User を有効化すること。

1. 左メニュー → **「App Home」**
2. **「Your App's Presence in Slack」** セクション → **「Edit」** ボタンをクリック
3. **「Add App Display Name」** ダイアログが開く:
   - **Display Name（Bot Name）**: `YourProjectBot`（日本語 OK）
   - **Default username**: `yourproject-bot`（**半角英数字・ハイフン・アンダースコアのみ**）
     > **ハマりポイント**: `YourProjectBot` をそのまま入力すると
     > 「Usernames can't contain special characters.」エラーになる。
     > `yourproject-bot` のように ASCII のみで入力する。
4. **「Add」** ボタンをクリック

### 1.3 Bot Token Scopes を設定

左メニュー → **「OAuth & Permissions」** → **「Scopes」** → **「Bot Token Scopes」** に追加:

| Scope | 用途 |
|-------|------|
| `chat:write` | メッセージ送信（招待済みチャンネル） |
| `chat:write.public` | パブリックチャンネルへの送信（招待不要） |

### 1.4 ワークスペースにインストール

1. 左メニュー → **「Install App」** → **「Install to Workspace」**
2. 権限確認画面 → **「許可する」**
3. **「Bot User OAuth Token」**（`xoxb-...`）をコピーして保存

---

## 2. チャンネルの作成と ID の取得

### 2.1 専用チャンネルを作成する

Slack で **2 つの専用チャンネル** を作成する。

**承認依頼専用チャンネル** (`SLACK_APPROVAL_CHANNEL_ID`):
1. Slack サイドバー「チャンネルを追加」→「チャンネルを作成」
2. チャンネル名: `approval-yourproject`（または任意）
3. プライベートチャンネル推奨（自分だけが参加）
4. 作成後、Bot を招待: `/invite @yourproject-bot`

**公開・マーケティング専用チャンネル** (`SLACK_PUBLISH_CHANNEL_ID`):
1. 同様にチャンネルを作成
2. チャンネル名: `publish-yourproject`（または任意）
3. プライベートチャンネル推奨
4. 作成後、Bot を招待: `/invite @yourproject-bot`

> **なぜ専用チャンネルが必要か**: メインチャンネルには多くの通知が流れ、要アクション通知が埋もれる。承認依頼・公開通知それぞれに専用チャンネルを設けることで見落としを防ぐ。

### 2.2 チャンネル ID の取得

1. Slack デスクトップアプリでチャンネルを右クリック → **「チャンネルの詳細を表示」**
2. 下部に表示される **チャンネル ID**（`C` で始まる文字列）をコピー

または URL から確認: `https://app.slack.com/client/{workspace_id}/{channel_id}`

**3 チャンネルの ID を取得する**:
- メインチャンネル（例: `#all-yourproject-updates`）→ `SLACK_CHANNEL_ID`
- 承認依頼専用チャンネル（例: `#approval-yourproject`）→ `SLACK_APPROVAL_CHANNEL_ID`
- 公開・マーケティング専用チャンネル（例: `#publish-yourproject`）→ `SLACK_PUBLISH_CHANNEL_ID`

---

## 3. 環境変数の設定

> ⚠️ **クラウド環境では `.claude/settings.local.json` を使わない**。セッション終了時に消えるため無意味。 <!-- refcheck:ignore -->
> 全環境変数は **Claude.ai スケジュールタスクの環境変数設定** で管理する（`docs/rules/env-vars.md` 参照）。

`claude.ai` → スケジュールタスク設定 → 環境変数セクションに以下を `.env` 形式で追加:

```
SLACK_BOT_TOKEN=xoxb-xxxxx-xxxxx-xxxxx
SLACK_CHANNEL_ID=C0XXXXXXXXX
SLACK_APPROVAL_CHANNEL_ID=C0YYYYYYYYY
SLACK_PUBLISH_CHANNEL_ID=C0ZZZZZZZZZ
SLACK_MENTION_USER_ID=U0XXXXXXXXX
```

| 変数名 | 取得方法 | 必須 |
|--------|---------|------|
| `SLACK_BOT_TOKEN` | Slack App の「Install App」→「Bot User OAuth Token」（`xoxb-...`） | ✓ |
| `SLACK_CHANNEL_ID` | メインチャンネルを右クリック → 「チャンネルの詳細を表示」→ 下部に表示される ID（`C...`） | ✓ |
| `SLACK_APPROVAL_CHANNEL_ID` | 承認依頼専用チャンネルの ID（`C...`）。**未設定時は `SLACK_CHANNEL_ID` にフォールバック** | 推奨 |
| `SLACK_PUBLISH_CHANNEL_ID` | 公開・マーケティング専用チャンネルの ID（`C...`）。**未設定時は `SLACK_APPROVAL_CHANNEL_ID` にフォールバック** | 推奨 |
| `SLACK_MENTION_USER_ID` | Slack で自分のプロフィールを開き「メンバーID をコピー」（`U...`）。未設定ではメンションなし | 推奨 |

---

## 4. チャンネルに Bot を招待

**3 つのチャンネル全て** で以下を入力:

```
/invite @yourproject-bot
```

> `chat:write.public` を付与した場合は招待不要だが、招待しておくとメッセージスレッドで
> Bot の存在が明示的になりわかりやすい。

---

## 5. 動作確認

```bash
# メインチャンネルへの通知テスト
python3 tools/slack_notify.py message --text "テスト通知 🐹"

# 承認依頼専用チャンネルへの通知テスト（SLACK_APPROVAL_CHANNEL_ID が設定されている場合）
python3 tools/slack_notify.py approval \
  --summary "テスト承認依頼" \
  --branch "test/branch"

# 公開・マーケティング専用チャンネルへの通知テスト（SLACK_PUBLISH_CHANNEL_ID が設定されている場合）
python3 tools/slack_notify.py publish \
  --event-type published \
  --title "テストタイトル" \
  --url "https://example.com/test"
```

`OK: message sent (ts=...)` と表示されれば成功。

承認依頼が `SLACK_APPROVAL_CHANNEL_ID` のチャンネルに届いていることを確認する。

---

## 6. 通知タイプ一覧

`tools/slack_notify.py` が提供する通知タイプ:

| タイプ | 用途 | 送信先チャンネル | フック／スキルから呼び出す方法 |
|--------|------|----------------|-------------------------------|
| ~~`session-start`~~ | **廃止（#2597）** — セッション開始通知は通知氾濫の主因のため廃止。半日サマリーに集約 | — | — |
| ~~`session-stop`~~ | **廃止（#2597）** — セッション終了通知は廃止。`stop-slack-notify.sh` は WIP 自動コミット・cost_log 追記のみ担当 | — | — |
| `half-day-summary` | **半日アウトカムサマリー**（PR消化・進捗・稼働・制作中・要対応A区分・ハートビート） | **`SLACK_PUBLISH_CHANNEL_ID`** | `tools/half_day_summary.py`（07:00/19:00 スロットで自動） |
| `pr` | PR 作成通知 + 「PR を開く」ボタン | `SLACK_CHANNEL_ID` | スキル内で明示呼び出し |
| `pipeline` | パイプライン完了／失敗通知 | `SLACK_CHANNEL_ID` | スキル内で明示呼び出し |
| `message` | 任意テキスト通知 | `SLACK_CHANNEL_ID` | スキル内・デバッグ用 |
| `progress` | 動画制作進捗レポート | `SLACK_CHANNEL_ID` | スキル内で明示呼び出し |
| `approval` | **PR作成前の承認依頼**（PR 自律化済みのため既定は FYI。`@mention` は A-1〜A-6 該当時のみ・#64） | **`SLACK_APPROVAL_CHANNEL_ID`** | スキル内で明示呼び出し（**非推奨**。`user-notification-triage-detail.md` §4） |
| `waiting` | **ユーザーアクション待ち**（`@mention` は A 区分のみ。ゼロならメインチャンネルへ FYI 降格） | **`SLACK_APPROVAL_CHANNEL_ID`** | スキル内で明示呼び出し |
| `publish` | **公開・配信完了**（`config/publish_events.yaml` 駆動。`@mention` は `fyi: false` イベントと A 区分のみ・障害起因は降格） | **`SLACK_PUBLISH_CHANNEL_ID`** | スキル内で明示呼び出し（`--event-type` 必須。値は同ファイルのキー） |
| `routine-idle` | **ルーティンのアイドル通知**（消化対象ゼロ＝バックログ空。維持/停止の判断支援 FYI・@mention なし） | `SLACK_CHANNEL_ID` | ルーティンが完全 no-op 時に呼び出し（R-1 手順の該当ステップは下流プロジェクトの運用メモが定義）。JST スロット（既定 08:00〜10:00）で 1 日 1 回に自己抑制・`--force` でバイパス |

> `SLACK_APPROVAL_CHANNEL_ID` が未設定の場合、`approval` / `waiting` も `SLACK_CHANNEL_ID` に送信される（後方互換）。
> `SLACK_PUBLISH_CHANNEL_ID` が未設定の場合、`publish` は `SLACK_APPROVAL_CHANNEL_ID` → `SLACK_CHANNEL_ID` にフォールバック。

### スキルからの呼び出し例

```bash
# PR作成前の承認依頼（実装完了後・PR作成前に必ず実行）
# 戻り値の ts（タイムスタンプ）を保存しておき、承認確認のポーリングに使用する
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" approval \
  --summary "台本v1生成完了（15分32秒）\nセルフレビュー: Error 0件" \
  --branch "content/V007-xxx" \
  --issue-url "https://github.com/kai-kou/tsukuru-studio/issues/123" \
  --issue-title "[V007] Phase 3: 台本生成"

# PR 作成通知
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pr \
  --pr-url "https://github.com/kai-kou/tsukuru-studio/pull/123" \
  --pr-title "[V007] script: 台本v1生成" \
  --branch "content/V007-xxx"

# ユーザーアクション待ち
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" waiting \
  --issues "Deep Research実行依頼" "PR #123 のレビュー" \
  --branch "content/V007-xxx"

# パイプライン完了
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" pipeline \
  --pipeline "音声" \
  --video-id "V007" \
  --result "完了（15分32秒）" \
  --duration "3分21秒"

# 公開完了（SLACK_PUBLISH_CHANNEL_ID に送信・@mention付き）
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" publish \
  --event-type published \
  --title "コンテンツタイトル" \
  --url "https://example.com/xxxxxxxxxxxxx" \
  --detail "内容を確認してください。"

# プロジェクト固有イベント（config/publish_events.yaml に追記したキーを使う例。
# 「project example」節参照）
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" publish \
  --event-type sns-complete \
  --video-id "V007" \
  --title "動画タイトル" \
  --url "https://youtu.be/xxxxxxxxxxxxx" \
  --detail "配信先: Zenn / Qiita / X / Bluesky"
```

---

## 7. サンドボックス外実行の保証（必須設定）

### 背景と問題

`tools/slack_notify.py` は `urllib.request` で `https://slack.com/api/...` に HTTPS リクエストを送る。
Claude Code が Bash ツール経由でこのスクリプトを呼び出すとき、サンドボックスのネットワーク制御が
子プロセスの通信を遮断する場合がある。

### 対策: `sandbox.network.allowedDomains` への登録（2026-08-01 見直し後・#379）

> 🔴 旧設定は `sandbox.excludedCommands` に `tools/` 配下の全 Python スクリプトを一括バイパスする
> ブロードパターンが登録されており、`slack_notify.py` もその副産物としてバイパス対象になっていた。
> このブロード exclusion は配布テンプレートとして危険な既定値だったため廃止した
> （`scripts/apply-to-repo.sh` 経由で下流リポジトリへそのまま配布されるため・詳細は
> `docs/rules/sandbox-rules.md`）。**`slack_notify.py` は接続先が `slack.com` / `api.slack.com` の
> 静的ドメインのみなので、`allowedDomains` 登録だけで足り、`excludedCommands` は不要になった。**

```json
"sandbox": {
  "network": {
    "allowedDomains": ["slack.com", "api.slack.com"]
  }
}
```

`excludedCommands` は現在、接続先ドメインが実行時まで決まらない一部のスクリプト
（secrets-broker 移行ツール等）だけに narrow exclusion として残る。`slack_notify.py` はそこに含まれない。

> サンドボックス設定の詳細と全対象スクリプトの一覧は `docs/rules/sandbox-rules.md` を参照。

### フックからの呼び出し（hooks は既にサンドボックス外）

`SessionStart` / `Stop` フックとして登録されたシェルスクリプトは、
Claude Code のサンドボックスとは独立したプロセスで実行されるため、
`allowedDomains` の制限を受けない。フック内の `slack_notify.py` 呼び出しは問題なし。

### スキルからの直接呼び出し（Bash ツール経由）

スキル SKILL.md からの呼び出しは、接続先が `allowedDomains` に登録済みの `slack.com` /
`api.slack.com` である限りそのまま動く（`excludedCommands` への一致は不要）。

```bash
python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" approval ...
timeout 15s python3 "${CLAUDE_PROJECT_DIR}/tools/slack_notify.py" session-start ...
```

---

## 8. settings.json の関連設定

以下は `settings.json`（Git 管理対象）に追加済み:

```json
{
  "sandbox": {
    "network": {
      "allowedDomains": [
        "slack.com",
        "api.slack.com"
      ]
    }
  },
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/stop-router.sh" }] }
    ]
  }
}
```

> `excludedCommands` は `slack_notify.py` には不要（`allowedDomains` で足りる）。現行の
> `excludedCommands` 全文は `docs/rules/sandbox-rules.md`（narrow exclusion のみ）を参照。

`SLACK_BOT_TOKEN` または `SLACK_CHANNEL_ID` が未設定の場合、フックは **無音でスキップ**（`exit 0`）するため、設定前はエラーにならない。

---

## 9. トラブルシューティング

### 「ボットユーザーがありません」エラー

→ **「App Home」** で Bot Name を設定する（[1.2 Bot User を追加する](#12-bot-user-を追加する必須) 参照）

### 「Usernames can't contain special characters.」エラー

→ Default username を `yourproject-bot` など **ASCII のみ** に変更する

### `channel_not_found` エラー

→ チャンネル ID が正しいか確認。チャンネルに Bot が招待されているか確認（`/invite @yourproject-bot`）

### `not_in_channel` エラー

→ `chat:write.public` スコープを追加するか、チャンネルに Bot を招待する

### `invalid_auth` エラー

→ `settings.local.json` のトークンが `xoxb-...` 形式か確認。コピーミスに注意 <!-- refcheck:ignore -->

### スキルから呼び出した通知が届かない（サンドボックスエラーの可能性）

スキル内の `python3 tools/slack_notify.py` がサンドボックスに遮断されている可能性がある。

1. `settings.json` の `sandbox.excludedCommands` に以下が登録されているか確認:
   ```json
   "excludedCommands": [
     "python3 *slack_notify*",
     "timeout *python3 *slack_notify*"
   ]
   ```
2. 未登録の場合は本ドキュメントの「7. サンドボックス外実行の保証」セクションを参照して追加する
3. 追加後、スキルから再呼び出しして動作確認する

### フックが動いていない（通知が届かない）

1. `echo $SLACK_BOT_TOKEN` で環境変数が読み込まれているか確認
2. `settings.local.json` の JSON 構文が正しいか確認（`jq . .claude/settings.local.json`） <!-- refcheck:ignore -->
3. `bash -x .claude/hooks/session-start-slack.sh` でフックを手動実行してデバッグ
