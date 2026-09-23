# 環境変数管理ルール

> 🔴 **クラウドの GitHub Variables 自動ロードは 2026-07-02 から 403 でブロックされている（Issue #133）。**
> egress プロキシが GitHub Actions パス（`repos/{o}/{r}/actions/variables`）を遮断するため、
> `gh variable list/get/set` も `tools/gh_vars.py`（urllib）も **クラウドセッションからは一切動作しない**
> （公式 GitHub MCP にも variables/secrets の等価ツールはない）。
> **クラウドセッションの env は ① Claude.ai 環境設定（environment variables）② secrets-broker
> （`infra/secrets-broker/`）で供給する。** 本ファイルの GitHub Variables 運用（自動ロード・
> `gh variable set` による自律設定）は **ローカル実行時のみ有効**。
> 実測マトリクスは `docs/rules/github-mcp-fallback-patterns.md` §2.4 を参照。

## 0. 大原則: 標準構成は PAT 不要・GH_TOKEN は「自動化層」の任意設定

**標準構成（最小構成のユーザー）は GitHub PAT を必要としない。** Claude Code on the web の
GitHub 接続（**Claude GitHub App** または `/web-setup`）が clone/push の認証を担うため、
日常利用（ファイル編集・コミット）は **トークン設定なしで完結** する（[公式](https://code.claude.com/docs/en/claude-code-on-the-web)）。

`GH_TOKEN`（PAT）は、以下の **自動化層を使うときだけ必要な任意のアップグレード**:

| 機能 | なぜ PAT が要るか |
|------|------------------|
| Issue/PR の完全自動化（`mcp__github__*`） | Claude Code の **リモート GitHub MCP は PAT 認証**（`Bearer ${GH_TOKEN}`）。Claude GitHub App（git 接続用）とは **別物** で、App では MCP を認証しない（[GitHub 公式](https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-claude.md)） |
| GitHub Repository Variables 経由の secrets 一元管理 | 変数値の読み取りに `repo`/`actions` 権限のトークンが要る（`gh_vars.py`）。App も MCP もこれをカバーしない |
| クロスリポ操作（別リポジトリの Issue/PR 操作・Variables 読み取り） | App トークンは接続リポ中心で、別リポ操作は GitHub MCP（PAT 認証）が要る。**ただしクラウド環境では別リポへの git push 自体は PAT 直叩き（埋め込みトークンでの git push/gh REST/urllib REST）が 403 で拒否される** ため、GH_TOKEN があっても解決しない。`add_repo` でスコープ追加した上で埋め込みトークンを使わないプレーン git push（プロキシが App 認証を注入）か MCP `push_files` を使う（`lessons-core.md` L-079・L-117） |

### Claude GitHub App と GH_TOKEN(PAT) の役割分担（混同しやすい）

| | Claude GitHub App / `/web-setup` | GH_TOKEN（PAT） |
|---|---|---|
| 認証する対象 | クラウドセッションの **git 接続**（clone/push） | `.mcp.json` の **GitHub MCP**（`mcp__github__*`）+ Variables 読み取り |
| ユーザー作業 | claude.ai の Web 接続（承認するだけ） | 任意。自動化層を使うときだけ Environment Variables に登録 |
| 範囲 | 接続アカウントが見える全リポ（App はアクセス制御ではない） | トークンのスコープ次第（`repo`） |
| 標準構成で必要？ | **必要**（git 接続の土台） | **不要**（任意のアップグレード） |

> **重要**: 「Claude GitHub App を入れれば PAT 不要」は **git 操作（clone/push）と PR の作成（open）については正しい** が、
> **GitHub MCP（Issue 化・PR 自動マージ・PR レビュー操作）と Variables 読み取りには PAT が要る**。両者は別レイヤー。

以降のセクションは、自動化層（GH_TOKEN 設定済み）を使う場合の運用ルール。

## 大原則: GitHub Repository Variables で一元管理する（クラウドでは 403・冒頭注記参照）

環境変数は **GitHub Repository Variables** に保存し、セッション開始時に `session-start.sh` が
`gh variable list`（または `gh` 不在時は `tools/gh_vars.py`）で自動取得して環境変数に設定する。
これは §0 の「自動化層」を使う場合の運用であり、`GH_TOKEN` 自体は §0 のとおり任意設定。

> ⚠️ **クラウドでは両経路（gh / gh_vars.py）とも 2026-07-02 から 403 ブロック**（冒頭注記）。
> クラウド専用運用のプロジェクトは Claude.ai 環境設定 / secrets-broker を一次経路にすること。
> 本セクション以降の自動ロード・`gh variable` 運用はローカル実行（gh が GitHub に直接到達できる環境）
> でのみ機能する。

**唯一の例外**: 自動化層を使う場合、`GH_TOKEN` のみ **Claude.ai スケジュールタスクの環境変数設定** に直接設定する（GitHub Variables 自体を読み取るブートストラップに必要なため。他の変数は GH_TOKEN 経由で GitHub Variables から自動取得できる）。

### なぜ GitHub Variables を使うのか

| 方式 | 変数の追加・変更 | 問題点 |
|------|----------------|--------|
| ~~Claude.ai 環境変数設定~~ | ユーザーが Web UI で手動編集 | 変更のたびに手動操作が必要 |
| **GitHub Repository Variables** | `gh variable set` で CLI/スクリプトから設定 | `GH_TOKEN` 1つだけ手動管理 |

### なぜ GitHub Secrets ではなく Variables なのか

GitHub Secrets は **値を読み取る API が存在しない**（設計上の制限）。

| 項目 | Secrets | Variables |
|------|---------|-----------|
| 暗号化 | LibSodium 公開鍵暗号 | なし（プレーンテキスト） |
| API で値を読める | ❌ 不可能（名前・日時のみ） | ✅ `gh variable get` で取得可能 |
| CLI で値を読める | ❌ `gh secret get` は存在しない | ✅ `gh variable get` で取得可能 |
| 値の参照可能範囲 | Actions ランナー内のみ | API/CLI でリポジトリ権限保有者が読取可 |
| セキュリティ | 最高 | 中（プライベートリポジトリで保護） |

**セキュリティの現実的判断**: Variables はプレーンテキスト保存だが、プライベートリポジトリのためアクセスはリポジトリ権限保有者のみ。Claude.ai の環境変数設定も同等のセキュリティレベル。

## セットアップ手順

### 1. Claude.ai 環境変数に `GH_TOKEN` を設定（1回のみ）

Claude.ai → スケジュールタスク設定 → 環境変数セクションに以下を設定:

```
GH_TOKEN=ghp_...
```

### 2. GitHub Variables に全環境変数を登録

以下のいずれかの方法で登録する。

#### 方法 A: 現在の環境変数から一括登録（推奨）

Claude.ai の環境変数に全変数が設定された状態でセッション内から実行:

```bash
python3 tools/setup_github_variables.py
```

#### 方法 B: .env ファイルから一括登録

```bash
python3 tools/setup_github_variables.py --from-env-file .env
```

#### 方法 C: 個別に設定

```bash
gh variable set SLACK_BOT_TOKEN -R kai-kou/tsukuru-studio --body "xoxb-..."
gh variable set SLACK_CHANNEL_ID -R kai-kou/tsukuru-studio --body "C0XXXXXXXXX"
# ... 残りの変数
```

### 3. Claude.ai 環境変数から GH_TOKEN 以外を削除

GitHub Variables への登録が完了したら、Claude.ai の環境変数設定から `GH_TOKEN` 以外の変数を削除できる。

### 4. 動作確認

次回セッション開始時に以下のログが出力されることを確認:

```
GitHub Variables: loaded 28 var(s), skipped 0 (already set in env)
```

## 新しい環境変数が必要になった時のフロー

1. Claude Code が「新しい環境変数 `XXX` が必要です」と判断する
2. **ローカル実行時のみ**: Claude Code が自律的に GitHub Variables に設定する:

```bash
gh variable set XXX -R kai-kou/tsukuru-studio --body "値"
```

3. 次回セッションから自動的に利用可能になる
4. 現セッションで即時利用が必要な場合は `export XXX=YYY` で一時設定する

**クラウドセッションでは `gh variable set` が 403 で実行不能（2026-07-02）** のため、上記 2 は使えない。
現セッションは `export XXX=YYY` で継続しつつ、恒久化はユーザーへ **設定名・値の取得手順・設定先
（Claude.ai 環境設定 or ローカルでの `gh variable set` or broker 登録）を添えて A-6 として依頼** する
（`user-confirmation-minimization.md` §1）。それ以外の手動設定依頼は不要（自動化層のブートストラップである `GH_TOKEN` 自体の初回登録を除く。§0 のとおり `GH_TOKEN` 自体が任意設定）。

## 環境変数の管理コマンド

```bash
# 登録済み変数の一覧表示（値はマスク表示）
python3 tools/setup_github_variables.py --list

# 変数を設定
python3 tools/setup_github_variables.py --set SLACK_BOT_TOKEN=xoxb-...

# 変数を削除
python3 tools/setup_github_variables.py --delete SLACK_BOT_TOKEN

# gh CLI で直接操作
gh variable get SLACK_BOT_TOKEN -R kai-kou/tsukuru-studio
gh variable set SLACK_BOT_TOKEN -R kai-kou/tsukuru-studio --body "xoxb-..."
gh variable delete SLACK_BOT_TOKEN -R kai-kou/tsukuru-studio
```

## 環境変数一覧

### ブートストラップ変数（Claude.ai 環境変数に直接設定）

| 変数名 | 用途 | 必須 | 値の例 |
|--------|------|------|--------|
| `GH_TOKEN` | GitHub パーソナルアクセストークン（GitHub MCP 認証 + GitHub Variables 読み取り + クロスリポ操作） | △ 任意（自動化層を使うときのみ・§0） | `ghp_...` |

### GitHub Variables で管理する変数

| 変数名 | 用途 | 必須 | 値の例 |
|--------|------|------|--------|
| `SLACK_BOT_TOKEN` | Slack Bot トークン | ○ | `xoxb-...` |
| `SLACK_CHANNEL_ID` | メイン通知チャンネルID（session-start/stop・PR・pipeline 等） | ○ | `C0XXXXXXXXX` |
| `SLACK_APPROVAL_CHANNEL_ID` | 承認依頼専用チャンネルID（approval/waiting 通知の送信先。未設定時は `SLACK_CHANNEL_ID` にフォールバック） | 推奨 | `C0YYYYYYYYY` |
| `SLACK_PUBLISH_CHANNEL_ID` | 公開・マーケティング専用チャンネルID（publish 通知の送信先。未設定時は `SLACK_APPROVAL_CHANNEL_ID` にフォールバック） | 推奨 | `C0ZZZZZZZZZ` |
| `SLACK_CODE_CHANNEL_ID` | コード関連通知専用チャンネルID（将来予約。現時点では `slack_notify.py` に実装なし。未設定でも動作に影響なし） | 任意 | `C0WWWWWWWWW` |
| `SLACK_MENTION_USER_ID` | `approval` / `waiting` / `publish` 通知でメンションするユーザーID（未設定時はメンションなし） | △ | `U0XXXXXXXXX` |

本ベースが標準で使うのは上記の `SLACK_*` だけ。**外部 API のトークン等、プロジェクト固有の変数は
各プロジェクトが本表に追記する**（ベース側には一切ハードコードしない）。`setup_github_variables.py` も
プロジェクト固有変数を持たず、`MANAGED_GITHUB_VARS` 環境変数（カンマ区切り）で管理対象を注入する。

```bash
export MANAGED_GITHUB_VARS="YOUR_API_KEY,YOUR_SERVICE_TOKEN"
python3 tools/setup_github_variables.py --list
```

追記するときは同じ 4 列（変数名 / 用途 / 必須 / 値の例）を守り、**値そのものは書かない**（例は形式だけ示す）。

### Claude Code 最適化変数

| 変数名 | 用途 | 必須 | 値の例 |
|--------|------|------|--------|
| `ENABLE_PROMPT_CACHING_1H` | プロンプトキャッシュの TTL を 1 時間に維持する。既定はサブスクなら 1 時間だが、**Usage Credits 利用中・API キー / クラウドプロバイダ経由では 5 分に落ちる**（[公式](https://code.claude.com/docs/en/costs)）ため、本 env でその条件下でも 1 時間を保つ。毎時パイプラインで CLAUDE.md・常駐ルール群を繰り返しロードするため、キャッシュ読込の90%割引でコスト削減（5分超のターン間隔が常態のため確実に元が取れる）。Claude Code BP差分 P-9（#2672・専門チームレビュー GO） | 推奨 | `1` |

## 自動ロードの仕組み（ローカル実行時のみ機能・クラウドは 403）

### session-start.sh の処理フロー

```
セッション開始
  ↓
GH_TOKEN で gh CLI 認証（Claude.ai 環境変数から取得）
  ↓
gh variable list -R kai-kou/tsukuru-studio --json name,value
  ↓
各変数について:
  ├─ 既にプロセス環境変数に存在する → スキップ（Claude.ai 側の値を優先）
  └─ 未設定 → export + CLAUDE_ENV_FILE に書き出し（セッション全体に伝搬）
  ↓
ログ出力: "GitHub Variables: loaded N var(s), skipped M (already set in env)"
```

### 優先順位

1. **Claude.ai 環境変数設定**（プロセス環境変数として既に存在） → 最優先
2. **secrets-broker**（`SECRETS_BROKER_URL`/`SECRETS_BROKER_TOKEN` 設定時） → クラウドの実働フォールバック
3. **GitHub Repository Variables**（session-start.sh で取得） → ローカル実行時のみ（クラウドは 403）

Claude.ai 環境変数に同名の変数が設定されている場合、GitHub Variables の値は上書きされない。

### secrets-broker 移行パイプラインの実行順序（オプトイン・非該当プロジェクトはスキップ）

`infra/secrets-broker/` は **Cloudflare Worker 経由で秘密情報を配布したいプロジェクトだけが使う opt-in
機能**。本ベースリポジトリ自体は Cloudflare broker を稼働させていない（`SECRETS_BROKER_URL` /
`SECRETS_BROKER_TOKEN` 未設定）ため、以下のパイプラインを自動実行するスキル・スケジュールは
**意図的に持たない**。導入するプロジェクトは手動 or 自プロジェクトのスケジュールタスクで下記順に呼び出す。

```
tools/setup_secrets_broker.sh      # Worker デプロイ + bundle 投入（Phase 0-1）
  → tools/verify_broker_migration.py --gate   # parity ゲート確認（Phase 2・READY 必須）
  → tools/finalize_broker_migration.py        # 生キー削除・カナリア→ドレイン（Phase 3）
  → tools/sync_broker_drift.sh                 # 以後の定期実行（broker 未同期キーの回収）
```

各ツールの詳細・Phase 表・ゲート条件は `infra/secrets-broker/README.md`（正本）を参照。

## 秘匿情報の取り扱いルール（P-12・2026-06-06）

GitHub Repository Variables にはトークン・API キー・Cookie など機密性の高い値が多数含まれる。
Claude Code のコンテキストやターミナルへの **平文流出** を防ぐため、以下のルールを厳守する。

### 一覧表示の方法（必ずマスク表示を使う）

| ✅ 正しい（マスク済み） | ❌ 禁止（平文出力） |
|---|---|
| `python3 tools/setup_github_variables.py --list` | `gh variable list` を直接実行 |
| `python3 tools/gh_vars.py --json` | `gh variable list --json name,value` |
| `python3 tools/gh_vars.py` （名前のみ） | `gh variable get TOKEN_NAME` を目視確認目的で使用 |

**理由**: `gh variable list` を Bash ツールで実行すると全変数の値が平文で Claude のコンテキストに流れ込む（トークン・Cookie などの長大な機密値も含む）。実地確認済み（P-12）。

> **例外**: `session-start.sh` のように `gh variable list ... --json name,value` の出力を **対話的な stdout 表示なしで変数に取り込む** 用途は許容する（値はログに出力せず `export` と `CLAUDE_ENV_FILE` への書き出しのみ）。禁止しているのは「値を対話的に stdout 表示する用途」であり、値をログに出さずに処理するパイプ・変数取り込みの用途は含まない。

### print / log 出力時のマスク

スクリプトが環境変数の値を出力する場合、 `tools/mask_secrets.py` の `mask_value()` を使う。

```python
from tools.mask_secrets import mask_value, mask_if_sensitive

# 全変数を一律マスク
print(f"  {name} = {mask_value(value)}")

# 変数名でマスク要否を自動判定（TOKEN / KEY / SECRET 等のパターン）
print(f"  {name} = {mask_if_sensitive(name, value)}")
```

### `gh_vars.py --key` の扱い

`python3 tools/gh_vars.py --key VAR_NAME` は値をそのまま出力する（スクリプトへのパイプ用途）。
ターミナルで目視確認のために使うと平文が表示されるため、目視確認には使わないこと。

## 禁止事項

- `.claude/settings.local.json` に環境変数を書き込む（セッション間で消える） <!-- refcheck:ignore -->
- ユーザーに `.claude/settings.local.json` の設定を依頼する <!-- refcheck:ignore -->
- `.env` ファイルを作成して環境変数を管理する
- 環境変数の値をコードやコミットメッセージに埋め込む
- `GH_TOKEN` を GitHub Variables に保存する（ブートストラップ問題: 読み取りに認証が必要）
- `gh variable list` を実行して変数値を **対話的に stdout 表示** する（平文流出）。変数取り込みのみの用途（session-start.sh 等）は例外（なおクラウドでは 403 でそもそも実行不可・2026-07-02）

## 複数ページに分かれた Variables の取得パターン（Issue #1485）

GitHub Actions Variables API は `per_page` の最大値が実質 30 件程度のため、
変数が多いと 2 ページ目以降に分断される。個別スクリプトで直接 API を呼ぶ場合は
**必ず `tools/gh_vars.py` の `load_github_variables()` または `get_all_variables()` を使う**。

```python
# ✅ 正しい（複数ページ自動統合）
from tools.gh_vars import load_github_variables
load_github_variables()  # os.environ に全変数を反映

# または辞書として取得
from tools.gh_vars import get_all_variables
variables = get_all_variables()  # {"VAR_NAME": "value", ...}

# ❌ 禁止（ページ 1 の 30 件しか取得できない）
urllib.request.urlopen("...actions/variables?per_page=30&page=1")
```

`tools/gh_vars.py` は `per_page=100` × 自動ページングで全変数を取得する。
`session-start.sh` の `gh variable list` は `gh` CLI 依存のため別途維持するが、
Python スクリプト内での変数取得は `gh_vars.py` に統一すること
（いずれもクラウドでは 403・冒頭注記。ローカル実行時のみ機能する）。

## ローカル開発環境（例外）

ユーザーがローカルで Claude Code CLI を実行する場合のみ、`.env` または `.claude/settings.local.json` が有効。 <!-- refcheck:ignore -->
ただし本プロジェクトの主な実行環境はクラウドのため、基本的に不要。
