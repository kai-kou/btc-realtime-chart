# サンドボックス設定ルール

Claude Code のサンドボックス制限に関する設定方針と運用ルール。

## サンドボックスの仕組み

Claude Code が Bash ツールでコマンドを実行する際、サンドボックスがネットワーク通信を制御する。

| 設定 | 効果 |
|------|------|
| `sandbox.enabled` | **サンドボックス機能全体の起動スイッチ。これが `true` でない限り、以下のサブ設定はすべて適用されない**（#383） |
| `sandbox.network.allowedDomains` | サンドボックス内プロセスがアクセスできるドメインを制限 |
| `sandbox.excludedCommands` | このパターンにマッチするコマンドはサンドボックスのネットワーク制限を **完全バイパス** して実行される |

### 🔴 `enabled` は前提条件（省略すると全設定が無効になる・#383）

`allowedDomains` や `excludedCommands` を書いただけではサンドボックスは有効にならない。公式は
「To enable the sandbox across all of your projects, set `sandbox.enabled` to `true`」と明記しており
（[公式](https://code.claude.com/docs/en/sandboxing)）、公式サンプル `examples/settings/settings-bash-sandbox.json`
も例外なく `enabled: true` を先頭に置いている。

本リポジトリは 2026-08-02 まで `enabled` を欠いており、許可リスト外ドメイン（`example.com`）へ
Bash から HTTP 200 で到達できる状態だった（実機検証で確認）。つまり #379 のネットワーク許可リスト厳格化は
**制限が効いている前提で行われたが、その前提自体が成立していなかった**。設定を追加・変更したら、
必ず許可リスト外ドメインへの到達可否を実機で確認する（設定ファイルの記述だけを根拠にしない）。

### 🔴 クラウド実行環境ではサンドボックスは動作しない（実機確認・#383）

Claude Code on the web のコンテナ（`CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE=cloud_default`）には
Linux 側のサンドボックス実装である **`bwrap`（bubblewrap）が存在しない**（`command -v bwrap` で確認済み）。
そのため `enabled: true` を設定してもクラウドでは許可リストは適用されず、`example.com` への到達は
引き続き成功する（実機確認済み）。

- **クラウドでの実効的な防御は 3 層**: ① セッションコンテナ自体の隔離（`IS_SANDBOX=yes`・破棄前提の
  ephemeral コンテナ）② `permissions.allow/deny` のツール単位 ACL ③ `pre-tool-use-router.sh` 等の
  PreToolUse フック。`allowedDomains` / `excludedCommands` はここに含まれない。
- **`enabled: true` を設定する意義はローカル実行と配布先にある**。本リポジトリは `apply-base` で下流へ
  配布されるベースであり、`bwrap` / Seatbelt が使えるローカル環境では設定が実際に効く。
  #379 が問題視した「ブロード exclusion の危険な既定値」も、その効き先はローカル・下流環境である。
- **したがって「クラウドで到達できた」ことを根拠に許可リストを緩めない**。検証はローカル環境で行う。

### `failIfUnavailable` は意図的に採用しない

`sandbox.failIfUnavailable: true` はサンドボックスが利用不可な環境でセッションを **起動失敗させる** 設定。
本リポジトリは R-1 ルーティン等のクラウド無人セッションで稼働するため、実行環境側のサンドボックス依存が
欠けた瞬間に全セッションが沈黙し、誰も気づけないまま停止する。CP-6（持続可能な自律運用）と衝突するため
**採用しない**（議論型レビュー `content/discussions/audit-prompt-findings-2026-08-02/` で 4 名合意・#383）。

### フックスクリプトはサンドボックス外

`SessionStart` / `Stop` / `PreToolUse` 等のフックとして登録されたシェルスクリプトは、
Claude Code のサンドボックスとは独立したプロセスで実行されるため、`allowedDomains` の制限を受けない。

---

## `excludedCommands` のパターン（現行設定・2026-08-01 見直し後・#379）

> 🔴 **2026-08-01 に既定値を変更した（公開リスク監査 r03・critical 判定の一部）**。
> 旧既定値は `python3 *tools/*.py` の **ブロード exclusion**（`tools/` 配下の全 Python スクリプトが
> サンドボックスのネットワーク制限を無条件バイパス）で、`scripts/apply-to-repo.sh` がこの設定一式を
> 下流リポジトリへそのまま配布していたため、**配布テンプレートとして危険な既定値** になっていた
> （任意の `tools/*.py` を追加・改変するだけで allowlist を回避できてしまう）。
> 以下は変更後の設定と、その根拠。

### 変更内容

```json
"allowedDomains": [
  "github.com", "api.github.com", "raw.githubusercontent.com",
  "slack.com", "api.slack.com", "api.anthropic.com",
  "mcp.context7.com", "context7.com"
],
"excludedCommands": [
  "python3 *tools/verify_broker_migration.py*",
  "python3 *tools/finalize_broker_migration.py*",
  "python *tools/verify_broker_migration.py*",
  "python *tools/finalize_broker_migration.py*",
  "timeout * python3 *tools/verify_broker_migration.py*",
  "timeout * python3 *tools/finalize_broker_migration.py*",
  "timeout * python *tools/verify_broker_migration.py*",
  "timeout * python *tools/finalize_broker_migration.py*"
]
```

### 判断の根拠（何を調べてこの結論に至ったか）

1. `tools/*.py` を全数調査し、実際に外部 HTTP 通信するスクリプトを洗い出した
   （`requests` / `urllib.request` 直接呼び出し + `gh` サブプロセス経由の 2 系統）。
2. 接続先はほぼ全て静的ドメインで、変更前から `allowedDomains` に登録済みだった
   （`api.github.com` / `slack.com`）か、未登録の 1 件（`raw.githubusercontent.com`・
   `tools/check_claude_code_updates.py` の release atom 403 時フォールバック先）だけだった
   → **未登録分を `allowedDomains` に追加すれば、ブロード exclusion なしで全て動く**。
3. 唯一の例外が `tools/verify_broker_migration.py` / `tools/finalize_broker_migration.py`
   （opt-in の secrets-broker 移行ツール・`docs/rules/env-vars.md`）。接続先が
   `SECRETS_BROKER_URL` 環境変数で **プロジェクトごとに異なる Cloudflare Workers サブドメイン等**
   になるため、静的な `allowedDomains` に事前登録できない。この 2 本だけ、**ワイルドカードを
   スクリプト名まで絞った exclusion** を残す（本リポジトリ自体は `SECRETS_BROKER_URL` 未設定で
   このツールを使わないが、opt-in する下流プロジェクトのために残す）。
4. `tools/scan_dangerous_patterns.py` 等、コード中に `requests`/`urllib` の **文字列** が現れるが
   実際には静的パターンスキャナで通信しないスクリプトは対象から除外（誤検出に注意）。

### 変更後に何が変わるか

- `tools/` 配下の Python スクリプトは、**`verify_broker_migration.py` / `finalize_broker_migration.py`
  以外すべて** `allowedDomains` の制限を受ける設計になった（従来は無条件バイパス）。
  上記調査の結果、実際に必要なドメインは全て `allowedDomains` でカバーされているため、
  通常運用への影響はない（`raw.githubusercontent.com` 追加のみが実質的な差分）。
  > ⚠️ **ただしこの設計は 2026-08-02 まで発効していなかった**: `sandbox.enabled` が無いため
  > サンドボックス自体が起動しておらず、制限は一切かかっていなかった（上記「`enabled` は前提条件」参照・#383）。
  > 実際に制限がかかり始めるのは #383 で `enabled: true` を入れた後、かつ `bwrap` / Seatbelt が
  > 使えるローカル環境に限られる。
- 新しい `tools/*.py` を追加する場合、**既定では exclusion にヒットしない**。接続先ドメインを
  `allowedDomains` に追加すること（次節「新しい tools スクリプトを追加するとき」参照）。
- 接続先が実行時まで決まらない（環境変数・ユーザー設定依存の）ツールを新規追加する場合のみ、
  `verify_broker_migration.py` / `finalize_broker_migration.py` と同様に **スクリプト名を明示した
  narrow exclusion** を追加する。`*tools/*.py` のようなブロードパターンを復活させない。

### パターンの設計意図（残る 2 パターンについて）

- `*tools/verify_broker_migration.py*` — 末尾 `*` はスクリプト後方の引数（`--gate` 等）を許容するため
  （`excludedCommands` はコマンド全体との前方一致で、後方に引数があっても pattern 側にワイルドカードが
  なければマッチしないため明示している）
  - **⚠️ セキュリティ上の注意**: 先頭 `*` はプロジェクトディレクトリ外のパス（例:
    `/tmp/evil/tools/verify_broker_migration.py`）にもマッチしうる。ただし Claude Code が実行する
    コマンドはエージェント自身が生成するため、外部からの任意コマンド実行には直結しない
    （旧パターンから引き継いだ既知のトレードオフ）。
  - **先頭 `*` が必要な理由**: フックスクリプトは `${CLAUDE_PROJECT_DIR}/tools/xxx.py` という
    **絶対パス** で Python スクリプトを呼び出す。絶対パスにマッチするには先頭の `*` が必須。
- `python3` と `python` の両方に対応（SKILL.md で両方使われるため）
- `timeout *` プレフィックス対応（長時間処理のタイムアウトラッパーに対応）

---

## `allowedDomains` の登録ドメイン一覧

`excludedCommands` でバイパスされる場合でも、`allowedDomains` は MCP サーバー通信や
`excludedCommands` に該当しないコマンド（`gh` CLI 等）のために必要。

> ⚠️ 以下の許可リストは **出自プロジェクト（動画制作）の実例**。自プロジェクトで実際に使う API ドメインに読み替えること（`<your-account>` は自分の Cloudflare Workers サブドメイン等に置換）。

| ドメイン | 用途 |
|---------|------|
| `github.com` / `api.github.com` | GitHub API（gh CLI、MCP サーバー） |
| `slack.com` / `api.slack.com` | Slack Web API（slack_notify.py） |
| `generativelanguage.googleapis.com` | Gemini API（画像生成） |
| `gemini-image-mcp-server.<your-account>.workers.dev` | Gemini Image MCP サーバー |
| `oauth2.googleapis.com` | Google OAuth トークン更新 |
| `www.googleapis.com` | Google API 汎用 |
| `youtube.googleapis.com` / `youtubeanalytics.googleapis.com` | YouTube Data API v3 |
| `youtube-api-proxy.<your-account>.workers.dev` | YouTube API プロキシ（Cloudflare Workers） |
| `api.anthropic.com` | Anthropic API（`claude -p` サブプロセス経由のみ。`tools/*.py` から直接呼び出しは不使用） |
| `qiita.com` | Qiita API（post_qiita_article.py） |
| `api.twitter.com` | X（Twitter）API v2（post_x_announcement.py） |
| `bsky.social` | Bluesky PDS（post_bluesky.py） |
| `us-central1-aiplatform.googleapis.com` | Vertex AI BGM 生成（generate_bgm.py、GCP_LOCATION=us-central1 の場合） |
| `r2.cloudflarestorage.com` | Cloudflare R2 S3 互換 API（backup_video_r2.py） |
| `r2.dev` | Cloudflare R2 パブリックバケットドメイン（動画公開 URL） |

> **注意**: `generate_bgm.py` の接続先は `GCP_LOCATION` 環境変数で変わる（例: `asia-northeast1-aiplatform.googleapis.com`）。
> `us-central1` 以外のリージョンを使う場合は `allowedDomains` に追加すること
> （2026-08-01 以降 `excludedCommands` はブロード exclusion ではなくなったため、これが唯一の到達経路）。

---

## 新しい tools スクリプトを追加するとき（2026-08-01 見直し後・#379）

> 🔴 旧来「`tools/*.py` に置けば `excludedCommands` に自動一致して追加作業不要」だったが、
> ブロード exclusion は配布テンプレートとして危険な既定値だったため廃止した（上記参照）。
> **既定は allowlist 経由**（`excludedCommands` への追加が既定の解決策ではない）。

### ネットワーク通信がある場合

1. 接続先ドメインを `allowedDomains` に追加する（**これが既定の解決策**）
2. `docs/rules/env-vars.md` に必要な環境変数を記載する
3. **`allowedDomains` に事前登録できない場合のみ**（接続先が環境変数・ユーザー設定で実行時に決まる、
   本質的に動的なドメインの場合 = `verify_broker_migration.py` / `finalize_broker_migration.py` と
   同じ事情）、`excludedCommands` に **スクリプト名まで絞った** narrow exclusion を追加する
   （`*tools/{スクリプト名}.py*` の形。`*tools/*.py` のような全 `tools/` 一致パターンは復活させない）

### `tools/` 以外の場所に配置する場合

`excludedCommands` の narrow exclusion（3 の場合のみ必要）はスクリプト名まで絞っているため、
配置場所を変えてもパターン側のパスを合わせれば動く。可能な限り `tools/` に配置することを推奨する。

### ラッパースクリプトを追加する場合

```bash
# NG: excludedCommands パターン "python3 *tools/verify_broker_migration.py*" にはマッチしない
bash tools/run_broker_check.sh  # → シェルラッパー経由の場合は python3 が直接呼ばれない

# OK: python3 を直接呼び出す
python3 tools/verify_broker_migration.py ...
```

bash / sh ラッパースクリプト経由でしか呼べない場合は、ラッパーを介さず python3 を直接呼び出す方式に
変更するか、そのラッパー用の narrow exclusion（`bash *tools/xxx.sh*` 等）を個別に追加する。

---

## 対象スクリプト一覧（主要なもの）

> ⚠️ 下表は **汎用ベースに実在するもの（`slack_notify.py` / `github_push_helper.py` /
> `check_pending_pr_reviews.py`）と、出自プロジェクト（動画制作）の実例が混在** している。
> 後者は汎用ベースには存在しないため自分のプロジェクトのスクリプト名に読み替えること
> （「外部接続するスクリプトを洗い出して許可リストに載せる」という運用が本体）。

| スクリプト | 接続先 | 備考 |
|-----------|--------|------|
| `tools/slack_notify.py` | `api.slack.com` | セッション通知 |
| `tools/github_push_helper.py` | `api.github.com` | git push 403 時の Contents API フォールバック（L-079） |
| `tools/youtube_scheduler.py` | YouTube API / OAuth | YouTube スケジュール管理 <!-- refcheck:ignore --> |
| `tools/youtube_comment_monitor.py` | YouTube API / OAuth | コメント監視 <!-- refcheck:ignore --> |
| `tools/youtube_delete_video.py` | YouTube API / OAuth | 動画削除 <!-- refcheck:ignore --> |
| `tools/generate_audio.py` | `localhost:50021`（VOICEVOX） | 音声生成 <!-- refcheck:ignore --> |
| `tools/generate_images_gemini.py` | `gemini-image-mcp-server.*` | 画像生成 <!-- refcheck:ignore --> |
| `tools/generate_bgm.py` | `{region}-aiplatform.googleapis.com` | BGM 生成 <!-- refcheck:ignore --> |
| `tools/post_qiita_article.py` | `qiita.com` | Qiita 投稿 <!-- refcheck:ignore --> |
| `tools/fetch_x_posts.py` | `api.twitter.com` | X 投稿取得（theme-discovery の Step 1.5） <!-- refcheck:ignore --> |
| `tools/post_x_announcement.py` | `api.twitter.com` | X（Twitter）投稿 <!-- refcheck:ignore --> |
| `tools/post_bluesky.py` | `bsky.social` | Bluesky 投稿 <!-- refcheck:ignore --> |
| `tools/backup_video_r2.py` | `r2.cloudflarestorage.com` | Cloudflare R2 動画バックアップ <!-- refcheck:ignore --> |
| `tools/generate_comment_reply.py` | `claude -p` サブプロセス（Anthropic API 直接呼び出しなし） | コメント返信生成 <!-- refcheck:ignore --> |
| `tools/adjust_subtitle_lines.py` | `claude -p` サブプロセス（Anthropic API 直接呼び出しなし） | 字幕調整 <!-- refcheck:ignore --> |
| `tools/check_pending_pr_reviews.py` | `api.github.com`（gh 経由） | PR レビュー確認 |
| `tools/discover_pending_audio.py` | `api.github.com`（gh 経由） | 音声生成対象検出 <!-- refcheck:ignore --> |
| `tools/discover_pending_phase.py` | `api.github.com`（gh 経由） | フェーズ対象検出 <!-- refcheck:ignore --> |

---

## トラブルシューティング

### `tools/*.py` スクリプトの通信が失敗する（2026-08-02 の `enabled: true` 適用後・#379 / #383）

> この症状が起こりうるのは **`enabled: true` が入った 2026-08-02 以降** かつ **`bwrap` / Seatbelt が
> 使えるローカル環境** に限られる。2026-08-01 の #379 時点では `enabled` が無く制限は発効していなかった。
> クラウド実行環境では現在も発生しない（サンドボックス自体が動作しないため）。


1. 接続先ドメインが `settings.json` の `allowedDomains` に登録されているか確認（**既定の到達経路**）
2. 未登録なら `allowedDomains` に追加する（多層防御ではなく、これが唯一の到達経路になった）
3. 接続先が環境変数で実行時に決まる等どうしても事前登録できない場合のみ、
   `excludedCommands` に **スクリプト名まで絞った** narrow exclusion（`*tools/{スクリプト名}.py*`）を
   追加する。`*tools/*.py` のような全 `tools/` 一致パターンを復活させない
4. `bash tools/wrapper.sh` のようにシェルラッパーを経由している場合は python3 直接呼び出しに変更するか、
   ラッパー用の narrow exclusion を個別に追加する

### VOICEVOX（localhost）への接続が失敗する

VOICEVOX のような localhost 常駐サービスへの接続は `allowedDomains`（ドメイン単位の許可リスト）では
表現しにくいため、`generate_audio.py` のように localhost 依存のスクリプトは
`excludedCommands` に **そのスクリプト名まで絞った** narrow exclusion を追加する対象になる
（`*tools/*.py` のブロード exclusion 経由でカバーする方式は 2026-08-01 に廃止した）。
VOICEVOX が起動しているか確認: `curl -s http://localhost:50021/speakers | head -c 50`

### `generate_bgm.py` の接続先ドメインエラー

`GCP_LOCATION` が `us-central1` 以外の場合、`allowedDomains` に未登録ドメインになる可能性がある。
`allowedDomains` に `{リージョン}-aiplatform.googleapis.com` を追加すること（ブロード exclusion は
廃止したため、これが唯一の到達経路）。
