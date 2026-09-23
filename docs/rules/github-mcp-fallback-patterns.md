# クラウドでの GitHub 操作: 公式 MCP 一次経路パターン（SSOT）

> **このファイルは「クラウド実行環境で GitHub をどう操作するか」の唯一の正本（SSOT）である。**
> 旧版は「`gh` 不在時（FileNotFoundError）の代替」を前提にしていたが、実態は **egress プロキシが
> GitHub API 経路を 403 でブロックする** という別問題である（2026-06-30 実機検証・Issue #121）。
> 可否は短期間に変化し続けている:
> **06-30（#121）→ 07-02 でブロック拡大（#133）→ 07-13 文言変化（#227）→ 07-14 で repo スコープ REST が
> 403 から許可に転換（#254）→ 07-26 で repo スコープ REST が再び 403 へ回帰（#338）**。
> この変動性ゆえに、静的な「できる/できない」暗記ではなく
> **MCP 一次経路 + gh シム（`tools/gh_shim.py`・§1.5）の 403 検知ガイダンス** を防御の中核とする。

## 0. 結論（最重要・常駐）

クラウド実行環境（`CLAUDE_CODE_REMOTE=true`）の GitHub 操作経路は次の序列で使う（2026-07-26 実測・Issue #338）:

1. ✅ **公式 MCP（`mcp__github__*`）が一次経路**: Issue・PR・レビュー・マージ・ファイル・search・
   Actions read が安定動作する（Anthropic サーバ経由で egress プロキシを通らないため、
   プロキシポリシーの変動に影響されない）。フック・スクリプト内からは呼べない点に注意（§2.5）。
2. ✅ **git 操作は別系統で生存**: `git clone https://github.com/...`・`git fetch/pull/push`（origin）は
   **git プロキシ**（API プロキシとは別）経由で動作する。
3. ⚠️ **gh CLI は当てにしない**: **`gh` はクラウドにプリインストールされていない**（公式仕様。
   `apt-get install -y gh` で導入自体は可能・Ubuntu universe 2.45.0）。ただし **導入しても
   repo スコープ REST が 403 のセッションでは実益がない**（下記）。gh シム（`.claude/bin/gh`）は
   ローカル互換の維持と 403 時の MCP 代替ガイダンス発生器として残す（§1.5）。
4. ⚠️ **repo スコープ REST（`gh api repos/{o}/{r}/...`）はセッション依存**: 07-14 は許可、
   **07-26 は 403**。使う前提で設計せず、失敗を前提にフェイルファストする（§4）。

### 🔴 403 の切り分け（2026-07-26 実測・最重要）

`gh api user` は **200**（`login` が返る）、`gh api rate_limit` も **200** なのに
`gh api repos/{o}/{r}` は **403** になる。つまり **403 の原因は「認証」ではなく「リポジトリが
GitHub API アクセス付きでセッションに attach されていないこと」** である。

```
403: {"message":"GitHub access is not enabled for this session.
      An org admin must connect the Claude GitHub App for this organization."}
```

- 公式ドキュメントいわく「プロキシは GitHub API とリリースアセットのリクエストを、**セッションに
  attach されたリポジトリに限定** する（環境のネットワークアクセスレベルとは独立）」。
- スコープ外リポジトリの 403 は別文言で、`add_repo` を `access:"push"` で呼ぶよう案内される
  （`access:"read"` は git clone/fetch のみで **API アクセスは付かない**）。
- ただし `add_repo(access:"push")` は auto mode classifier にブロックされることがある（07-26 実測）。
  ブロックされたら回避せず MCP を使う。
- したがって **「403 = トークン権限不足」も「403 = gh 未導入」も誤診**。gh を入れても直らない。

依然 403 のまま（= シムがフェイルファスト + MCP 等へのガイダンスを付与する領域）:

- ❌ **GraphQL 全般**: `gh api graphql` と GraphQL 依存コマンドのシム未変換形 → 403「This GraphQL query is not enabled for this session — only the pinned set of PR-review operations is served. Use REST via `gh api repos/{owner}/{repo}/...` instead.」（2026-07-14 文言。プロキシ自身が REST を案内する）
- ❌ **search 系**: `gh search ...`・`gh api search/...` → 403「sessions are bound to their configured repositories」→ MCP `search_issues` / `search_code` / `search_pull_requests` で代替
- ❌ **非 repo REST**: `gh api users/{u}`・`notifications`・`user/repos` → 同上 403（生存は `gh api user`・`gh api rate_limit` のみ）
- ❌ **Actions variables/secrets**: `gh variable/secret list/set`・`gh api repos/{o}/{r}/actions/variables` → 403「Access to this GitHub Actions path is not permitted through this proxy.」（MCP にも等価ツールなし・§2.4）
- ❌ **Actions runs/workflows の REST**: プロキシは通過するが GitHub App トークンの権限不足で「Resource not accessible by integration」→ MCP `actions_list` / `actions_get` / `get_job_logs` で代替
- ❌ **urllib 直叩きフォールバックは効かない**: `urllib.request` で `api.github.com` のブロック対象パスを呼んでも **同一プロキシを通るため同じ 403**。「urllib で代替」は誤り。

なお `gh auth status` はブロック（403）はされず exit 0 で完走するが、stderr に「GH_TOKEN invalid」の
失敗表示が出るため、**認証可否の判定には使わない**（2026-07-13 再確認）。

## 1. 実機検証マトリクス（2026-07-26・Issue #338。旧: 07-14 #254 / 07-13 #227 / 07-02 #133 / 06-30 #121）

| 操作 | 結果 | 備考 |
|------|------|------|
| `gh` のプリインストール | ❌ なし | **公式仕様**（"The `gh` CLI isn't pre-installed."）。PATH 上にあるのはシムだけの状態が既定 |
| `apt-get install -y gh` | ✅ 可能 | Ubuntu noble universe の 2.45.0。GitHub release asset（tarball）取得も到達可。**ただし下記のとおり導入しても repo API は 403 なので実益がない** |
| `gh auth status` | ⚠ exit 0 | stderr に「The token in GH_TOKEN is invalid」と失敗表示（GraphQL 依存）。**認証可否の判定に使わない**（`gh api user` / git ls-remote / MCP で実到達を確認する） |
| `gh api user`・`gh api rate_limit` | ✅ 200 | 非 repo REST で生存する 2 パス。**ここが 200 = プロキシの認証注入は効いている**（403 を認証問題と誤診しない根拠） |
| `gh api repos/{o}/{r}`（repo REST read 全般） | ❌ 403 | **07-14 の許可から回帰**。issues / pulls / labels とも 403「GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization.」= リポジトリが API アクセス付きで attach されていない（§0 の切り分け参照） |
| `gh api repos/{o}/{r}/...`（repo REST write） | ❌ 403 | read が 403 のため write も同様（07-14 は POST /issues 成功を実測していた） |
| `gh issue list/view`・`gh pr list/view`・`gh label list`・`gh repo view`（シム変換） | ❌ 403 | シムは REST へ変換するが、その REST 自体が 403。シムは stderr に `[gh-shim] repo スコープ REST がプロキシで遮断 → MCP へ切替` を付与（設計どおりのフェイルファスト） |
| `gh api graphql -f query=...`・GraphQL 依存コマンド（素の `gh issue/pr list`・`gh pr checks/diff`・`gh gist list`・`gh status` 等） | ❌ 403 | 「This GraphQL query is not enabled for this session — only the pinned set of PR-review operations is served. Use REST via `gh api repos/{owner}/{repo}/...` instead.」（MCP の PR レビュー系だけが pinned で通る） |
| `curl` / `urllib` で `api.github.com/repos/...` 直叩き | ❌ 403 | `Authorization` ヘッダ有無・`Bearer proxy-injected` 指定・実 `GH_TOKEN` のいずれでも同一 403。**直叩きはフォールバックにならない** |
| スコープ外リポジトリの API | ❌ 403 | 「Use `add_repo` to request access. … call add_repo again with access:"push"」。`access:"read"` は git clone/fetch のみで API は付かない |
| `add_repo(access:"push")` による API attach | ❌ | auto mode classifier にブロックされることがある（07-26 実測）。回避せず MCP を使う |
| `gh search repos/issues/code/prs`・`gh api search/...` | ❌ 403 | 「sessions are bound to their configured repositories」 |
| `gh api users/{u}`・`notifications`・`user/repos` | ❌ 403 | 同上 |
| `gh variable list`・`gh secret list`・`gh api repos/{o}/{r}/actions/variables` | ❌ 403 | 「Access to this GitHub Actions path is not permitted through this proxy」 |
| `gh run list`・`gh workflow list`・`gh api repos/{o}/{r}/actions/runs`・`/commits/{ref}/check-runs`・`/commits/{ref}/status` | ❌ | プロキシは通過するが GitHub App トークン権限不足「Resource not accessible by integration」→ MCP `actions_list` / `get_job_logs` / `get_check_run` |
| `gh repo clone {o}/{r}` | ❌ exit 1 | 内部で API 解決を伴うため失敗 → `git clone https://github.com/...` |
| `git clone/fetch/pull/push origin`・`git ls-remote` | ✅ | **git プロキシ経由（API プロキシとは別系統）**。07-26 も `git ls-remote origin` 成功を実測 |
| `git push origin --delete <branch>` / `git push origin :refs/heads/<branch>`（リモートブランチ削除） | ❌ 経路なし | 4 回とも `send-pack: unexpected disconnect while reading sideband packet` で切断（存在しない ref を対象にした対照実験でも同じ切断＝ブランチ固有ではなく **ref 削除という操作自体を git プロキシが拒否**）。通常 push は同一プロキシで成功するため生存自体はしている。MCP にも delete 系ツールがない（`create_branch`/`list_branches` はあるが delete なし）。**クラウドセッションからブランチ削除を完遂する経路が存在しない**（#399・オーナーが GitHub UI の Branches 画面かローカル `git push origin --delete <branch>` で削除する） |
| `mcp__github__*`（Issue・PR・レビュー・マージ・ファイル・search・Actions read） | ✅ | 従来どおり動作（07-26 に `list_pull_requests` / `issue_write` を実測確認）。**API プロキシを通らない** ため repo REST の 403 と無関係に生存する |
| `tools/check_pending_pr_reviews.py` 等の gh 依存スクリプト | ✅ 設計どおり | gh 失敗を `gh_unavailable` / `GH_UNAVAILABLE`（exit 3）で明示し、サイレント縮退しない（§4） |

> 🔴 **可否は変動する（5 回/1 か月で変化）**: 上表は 2026-07-26 時点の実測。
> **「07-14 に許可されたから今も使える」と暗記しない**。
> 再検証は `curl -o /dev/null -w '%{http_code}' https://api.github.com/repos/{o}/{r}` の HTTP コードを見るのが最短
> （`gh --shim-doctor` は実 gh の導入が前提のため、既定のクラウドセッションでは使えない）。
> 挙動変化を検知したら本表と L-114 を更新すること（CP-2）。

## 1.5 gh シム（`tools/gh_shim.py`・Issue #254）— ローカル互換 + MCP 誘導シグナル

**クラウドの gh 403 を「事前変換 + 事後ガイダンス」で排除する PATH ラッパー**。SessionStart フック
（`session-start.sh`）が `.claude/bin` を PATH 先頭に注入し、`gh` 呼び出しをシムが受ける。

> 🔴 **クラウドでは実 gh が無いのが既定**（公式仕様・§1）。その場合シムは変換もできず
> `[gh-shim] 実 gh が見つかりません` + MCP 代替案内を出して終わる。**gh を導入しても repo REST が
> 403 なら状況は変わらない**ため、`apt install gh` を解決策として試さない（#318 / #338）。
> シムの現在の主価値は ① ローカル実行での挙動不変（即 exec）② クラウドで gh を呼んだ既存スクリプトに
> 「MCP へ切り替えよ」という機械可読なシグナルを与えること、の 2 点。

| レイヤー | 動作 |
|---------|------|
| ローカル（`CLAUDE_CODE_REMOTE` ≠ true） | 実 gh へ即 `exec`（挙動不変・オーバーヘッドゼロ） |
| 実 gh 不在（クラウド既定） | 変換不能。`実 gh が見つかりません` + MCP 代替ガイダンスを出力 |
| 変換（クラウド） | GraphQL 依存コマンド（`issue list/view/create/comment/edit/close`・`pr list/view/create/merge/comment`・`label list`・`repo view`・`release list`）を repo スコープ REST へ透過変換。`--json`（gh 互換フィールド名: `headRefName`・`reviewRequests` 等）・`--jq`・`--label` AND・`--head`・`--paginate` 相当を再現 |
| パススルー + アノテート（クラウド） | 変換対象外・未対応フラグは実 gh へパススルーし、403 系エラーを検知したら **エラーカテゴリ別の MCP 代替ガイダンスを stderr に付与**（exit code は gh のまま）。repo スコープコマンドには `-R {slug}` を自動注入（クラウドの origin は git プロキシ URL のため gh が repo を推定できない） |
| 診断 | `gh --shim-doctor`（ライブ疎通マトリクス）/ `gh --shim-self-test`（オフライン自己テスト） |
| 脱出ハッチ | `GH_SHIM=off gh ...` で完全パススルー / `GH_SHIM=force` でローカルでも変換を強制 |

- プロキシ挙動が再変化しても壊れない設計: 変換に使う REST 自体が 403 化したらアノテート経路に落ち、
  ガイダンス付きでエラーが返る（サイレント破壊なし）。
- フック・`tools/*.py` の `subprocess` からも、PATH に `.claude/bin` が入っていればシム経由になる
  （SessionStart が `CLAUDE_ENV_FILE` に PATH を書き出す）。入っていない場合も `gh api repos/...`
  直書きなら実 gh のままで動作する。
  > 🔴 **`CLAUDE_ENV_FILE` はセッション種別によって未設定になる**（scheduled trigger セッションで実測・
  > L-133）。未設定だと SessionStart の PATH 注入はフック終了と同時に失われ、後続の Bash 呼び出しでは
  > シムどころか `gh` 自体が `command not found` になる（403 ではない）。「PATH に `.claude/bin` が
  > 入っていれば」は無条件の既定ではなく `CLAUDE_ENV_FILE` 提供時のみの条件付き事実として扱う。

## 2. コマンド別 代替パターン（gh → MCP）

gh シム（§1.5）と repo スコープ REST で大半の gh 操作はクラウドでもそのまま動く（2026-07-14〜）。
以下の MCP 対応表は、**REST が 403 に回帰した場合の受け皿**、および **シム/REST に等価がない操作
（search・Actions read・resolve_review_thread 等）の一次経路** として維持する。

| やりたいこと（旧 gh コマンド） | クラウド一次経路（MCP） |
|----------------|----------------|
| `gh pr list --state open` | `mcp__github__list_pull_requests(owner, repo, state="open")` |
| `gh pr view {N}` | `mcp__github__pull_request_read(method="get", pullNumber=N)` |
| `gh pr view {N} --json reviews` | `mcp__github__pull_request_read(method="get_reviews", pullNumber=N)` |
| `gh pr view {N} --json comments` | `mcp__github__pull_request_read(method="get_comments", pullNumber=N)` |
| `gh pr view {N} --json files` | `mcp__github__pull_request_read(method="get_files", pullNumber=N)` |
| `gh pr diff {N}` | `mcp__github__pull_request_read(method="get_diff", pullNumber=N)` |
| `gh pr create` | `mcp__github__create_pull_request(owner, repo, title, head, base, body)` |
| `gh pr merge {N} --squash` | `mcp__github__merge_pull_request(owner, repo, pullNumber=N, merge_method="squash")` |
| `gh pr list --head {ブランチ}` | `mcp__github__list_pull_requests(owner, repo, head="{owner}:{ブランチ}", state="open")` |
| `gh issue list --label "X"` | `mcp__github__list_issues(owner, repo, labels=["X"], state="OPEN")` |
| `gh issue view {N}` | `mcp__github__issue_read(method="get", issue_number=N)` |
| `gh issue create` | `mcp__github__issue_write(method="create", title, body, labels)` |
| `gh issue comment {N} --body "..."` | `mcp__github__add_issue_comment(owner, repo, issue_number=N, body="...")` |
| `gh issue edit {N} --add-label "..."` | `mcp__github__issue_write(method="update", issue_number=N, labels=[...])` |
| `gh api repos/.../contents/{path}` | `mcp__github__get_file_contents(owner, repo, path)` |
| ファイル commit/push（CLI 失敗時） | `mcp__github__create_or_update_file` / `mcp__github__push_files` |
| `gh api graphql`（resolveReviewThread 等） | `mcp__github__resolve_review_thread` / `mcp__github__unresolve_review_thread` |
| `gh repo view` / repo メタデータ | `mcp__github__search_repositories` または `mcp__github__list_branches` 等の個別 MCP |
| `gh search issues "repo:{o}/{r} ..."` | `mcp__github__search_issues(query, owner, repo)` |
| `gh search prs "repo:{o}/{r} ..."` | `mcp__github__search_pull_requests(query, owner, repo)` |
| `gh search code "... repo:{o}/{r}"` | `mcp__github__search_code(query="... repo:{o}/{r}")` |
| `gh search repos` | `mcp__github__search_repositories(query)` |
| `gh run list` | `mcp__github__actions_list(method="list_workflow_runs", owner, repo)` |
| `gh run view {id}` / `gh run view --log` | `mcp__github__actions_get(method="get_workflow_run", resource_id)` / `mcp__github__get_job_logs` |
| `gh workflow list` | `mcp__github__actions_list(method="list_workflows", owner, repo)` |
| `gh workflow run {wf}` | `mcp__github__actions_run_trigger(owner, repo, workflow_id, ref)` |
| `gh release list` | `mcp__github__list_releases(owner, repo)` |
| `gh label list` | 一覧の等価 MCP なし。ラベル名が既知なら `mcp__github__get_label(owner, repo, name)`、網羅が必要なら `list_issues` 応答の `labels` から収集する |
| `gh api users/{u}` | `mcp__github__search_users(query)`（自分自身は `mcp__github__get_me`） |
| `gh variable list/set`・`gh secret list` | ❌ **MCP 等価ツールなし**（§2.4 参照。クラウドから GitHub Variables は読み書き不能） |

> **search 系の注意（Repository Scope）**: `search_issues` / `search_code` / `search_pull_requests` は
> repo 引数・`repo:` 修飾を省くとセッションスコープ外のリポジトリまで検索できてしまう。
> 必ず対象リポジトリを指定してスコープ内に限定すること。

### 2.3.1 `authorAssociation`（PR 著者の信頼判定）は MCP に無い（2026-08-01 実機確認・#379）

🔴 **`mcp__github__list_pull_requests` の `fields` にも `mcp__github__pull_request_read(method="get")` の
応答にも `authorAssociation`（`author_association`）は含まれない**（本リポジトリの実 PR で実測確認済み。
gh CLI の `--json authorAssociation` / REST `GET /repos/{o}/{r}/pulls` の `author_association` には存在する）。

`tools/check_pending_pr_reviews.py` の `_is_claude_branch()` は自動マージ対象判定に
`authorAssociation`（OWNER/MEMBER/COLLABORATOR のみ信頼）を使う（#379・公開リスク監査 r03
critical: ブランチ名の前方一致だけで判定すると fork PR が規約どおりの命名で対象になってしまう）。
この判定はスクリプト内部（gh・クラウドでは `gh_shim.py` が REST の `author_association` を
`authorAssociation` にマップする）で完結するため、通常の gh 経路では問題にならない。

**gh 自体が全面失敗し（`GhUnavailableError`・exit 3）、呼び出し元の Claude が §4 のフォールバックで
`mcp__github__list_pull_requests` を直接使う場合は、著者の信頼判定にこのフィールドを使えない。**
その場合は以下のいずれかで代替する（安全側＝判定不能なら「信頼しない」がデフォルト）:

1. `mcp__github__list_repository_collaborators(owner, repo, affiliation="all")` で信頼済みログイン
   集合を作り、PR の `user.login`（`list_pull_requests` / `pull_request_read` のどちらにも含まれる）が
   その集合に含まれるかを突合する
2. 該当しない・取得自体に失敗した場合は自動マージ対象と判定しない（A-1〜A-6 相当のリスクを取らない）

### 2.4 GitHub Actions Variables / Secrets は MCP に等価ツールがない（2026-07-02）

🔴 **`gh variable list/set`・`gh secret list` と `repos/{o}/{r}/actions/variables` への urllib 直叩きは
2026-07-02 からクラウドで 403 ブロックされ、公式 GitHub MCP にも variables/secrets の等価ツールが
存在しない。** つまり **クラウドセッションから GitHub Variables を読むことも設定することも不能** になった。

- **env の供給**: クラウドセッションの環境変数は ① Claude.ai の環境設定（environment variables）
  ② secrets-broker（`SECRETS_BROKER_URL`/`SECRETS_BROKER_TOKEN` 設定時・`infra/secrets-broker/`）で供給する。
  `session-start.sh` の GitHub Variables 自動ロード（gh / `tools/gh_vars.py` の 2 系統）はクラウドでは両方 403 になる
  （フックは 403 を検知してその旨をログに出す。ローカル実行では引き続き動作する）。
- **env の新規設定**: 旧ルールの「`gh variable set` で Claude が自律設定」は **クラウドでは実行不能**。
  ユーザーがローカル端末で `gh variable set` を実行するか、Claude.ai 環境設定 / broker に登録する
  （= A-6 相当のユーザー作業。依頼時は具体的なコマンド・設定名を添える）。
- 詳細な env 運用は `docs/rules/env-vars.md` を参照。

> **GraphQL 専用操作**: `gh api graphql` の独自 mutation/query はクラウドで実行不能（urllib も不可）。
> review thread の resolve/unresolve は MCP に専用ツール（`resolve_review_thread` / `unresolve_review_thread`）が
> あるためそれを使う。MCP に等価が無い GraphQL 専用処理は、**ローカル実行に切り出す** か、必要なら
> ツール改修 Issue（B カテゴリ・`user-confirmation-minimization.md`）として起票する。

### 2.1 `mcp__github__list_issues` の `labels` は OR（gh CLI の `--label A --label B` は AND）

🔴 **gh CLI の `--label A --label B` は「A かつ B」（AND）だが、`mcp__github__list_issues(labels=[A,B])` は
GitHub GraphQL の `issues(labels:)` 引数に渡るため「A または B」（OR）で返る。** 単純な gh→MCP 置換では
意図しない Issue が混入する（例: `labels=["type:retro-try","status:waiting-claude"]` は
`type:retro-try`（status 不問）と `status:waiting-claude`（type 不問）の和集合を返す）。

**対策**: 複数ラベルで絞り込みたい場合は、`list_issues` を **最も絞り込み効果が高い単一ラベル** で呼び、
応答の `labels` 配列を見て **Claude が client-side で残りのラベル条件を AND 判定** する（該当しない Issue は除外する）。

### 2.2 `mcp__github__issue_write` の `labels` は全置換（gh CLI の `--add-label`/`--remove-label` は差分指定）

🔴 **`issue_write` の `labels` パラメータは Issue のラベルを完全に置き換える**（gh CLI の
`--add-label`/`--remove-label` のような追加/削除の差分指定ではない）。ラベルを 1 つ追加/削除したいだけでも、
**まず現在のラベル一覧を取得**（`list_issues` の応答 or `mcp__github__issue_read(method="get_labels")`）し、
そこから対象ラベルを足し引きした **フルリスト** を `labels` に渡す必要がある。

```
❌ mcp__github__issue_write(method="update", issue_number=N, labels=["status:in-progress"])
   → 他の既存ラベル（type:bug 等）が全て消える
✅ 現在のラベル ["type:bug","status:waiting-claude"] を取得
   → "status:waiting-claude" を除き "status:in-progress" を加えたフルリスト
   → mcp__github__issue_write(method="update", issue_number=N, labels=["type:bug","status:in-progress"])
```

### 2.3 ページングの既定値（gh CLI の `--limit 1000` に相当する指定はない）

`mcp__github__list_issues` / `list_pull_requests` の `perPage` は最大 100（既定はツール依存でそれ以下の場合あり）。
gh CLI の `--limit 1000` のような大きな上限指定はできないため、対象が 100 件を超えうる場合は
`perPage=100` を明示し、応答の `pageInfo.hasNextPage`/`endCursor` を見て `after` で追加ページを取得する
（本リポジトリ規模では通常 1 ページで足りるが、件数が多いプロジェクトでは省略しないこと）。

### 2.5 gh→MCP 全面移行の残ギャップ（2026-07-13 調査・Issue #227）

MCP は Issue・PR・レビュー・マージ・ファイル・search・Actions（read + workflow_dispatch）を実用上カバーするが、
以下は **セッション提供の MCP に等価ツールが存在しない**（= gh からの単純移行が不可能。ローカル gh・別経路・機能断念のいずれか）。

| 領域 | gh（ローカル）ではできる | セッション提供 MCP の状況 |
|------|----------------------|------------------------|
| Actions Variables / Secrets | `gh variable/secret list/set` | ❌ なし（§2.4。クラウドでは読み書きとも不能） |
| ラベル管理（作成・編集・削除・一覧） | `gh label create/edit/delete/list` | ❌ 書き込み系なし。read も `get_label`（単体取得）のみで一覧不可（§2 の代替手順参照） |
| マイルストーン | `gh api repos/{o}/{r}/milestones` | ❌ 作成・一覧ツールなし（`issue_write` の `milestone` 番号指定のみ可） |
| Release の作成・編集 | `gh release create/edit` | ❌ read のみ（`list_releases` / `get_latest_release` / `get_release_by_tag`） |
| リモートブランチ / ref の削除 | `gh api -X DELETE repos/{o}/{r}/git/refs/heads/{branch}`・`git push origin --delete` | ❌ MCP に delete 系ツールなし（`create_branch`/`list_branches` のみ）。**git プロキシも ref 削除自体を拒否**（§1 実機検証マトリクス）ため git 経路の代替も不能。オーナーが GitHub UI か ローカル git で削除する（#399） |
| Gist / Notifications / Discussions / Projects V2 | `gh gist` / `gh api notifications` 等 | ❌ セッション版に未提供。上流 github-mcp-server には gists / notifications / discussions / projects（`projects_list/get/write`・2026-01-28 changelog）の各 toolset が実装済みだが、クラウドセッションに配備される公式 MCP はそのサブセット |
| 任意 API 呼び出し | `gh api {path}` / `gh api graphql` | ❌ 生 REST / GraphQL ツールなし。定義済みツールの範囲のみ |

**構造的制約（全面移行が不可能な理由）**: MCP ツールを呼べるのは **Claude のメインセッション（とサブエージェント）だけ**。
フック（`session-start.sh`・`stop-pr-check.sh` 等）・`tools/*.py`・シェルスクリプトの **内部からは MCP を呼べない**。
したがって「gh 主体のスクリプトを全て MCP に移行」は構造的に成立せず、現行の二段構え
（スクリプトは gh 失敗を `gh_unavailable` で明示 → 呼び出し元の Claude が MCP で直接操作・§4）が正しい終着形。
ローカル実行では gh が全機能動作するため、**gh 経路の削除ではなく「クラウド = MCP 一次経路 / ローカル = gh」の併存を維持する**。

## 3. git 操作（クラウドで生存）

`git` は API プロキシとは別の git プロキシを通るため、以下は **そのまま使える**:

```bash
git clone --depth 1 https://github.com/owner/repo.git                 # ✅ gh repo clone の代わり
git fetch origin <branch>                                             # ✅
git pull origin <branch>                                              # ✅
git push -u origin <branch>                                           # ✅（push が 403/413/502 のときは L-079 のフォールバック）
```

`gh repo clone` は内部で API を叩くため **クラウドでは失敗する**。リポジトリ取得は
`git clone https://github.com/...`（認証はプロキシが付与）を使う。

## 4. Python スクリプト・フックからの GitHub アクセス

フック・`tools/*.py`・シェルスクリプトの内部からは MCP を呼べない（§2.5）。したがって
**この層はクラウドで GitHub API に到達する手段を持たない**（実 gh は不在、repo スコープ REST は 403・
2026-07-26 実測）。この層の設計は **「取りに行く」ではなく「取れなかったことを正確に伝える」** が正解であり、
以下の失敗シグナリング原則が実際の一次動作になる（呼び出し元の Claude が MCP で引き取る）。

- 取得系（read）: スクリプトが `gh` で失敗（403/非 0）したら、メインセッションの `mcp__github__*` ツールで直接操作する。
- GraphQL 系: **urllib で `api.github.com/graphql` を直叩きしない**（同一プロキシで 403）。MCP の等価ツールへ置換する。
- 🔴 **サイレント縮退の禁止（Issue #133 で一斉修正）**: gh 失敗時に「空リスト・0 件・False」へ静かに縮退する
  実装は、403 を「対象なし」と誤認させる（スナップショットが空になる・重複防止が無効化する等）。
  gh を呼ぶ `tools/*` は失敗時に **stderr へ `gh_unavailable` を明示し、専用 exit code / センチネル値で
  「取得失敗」を呼び出し元へ伝える**（`check_pending_pr_reviews.py` の `GhUnavailableError` → exit 3 が参考モデル）。
  呼び出し元（Claude・フック）は失敗シグナルを受けたら MCP で直接操作する。
- 🔴 **「取得失敗」と「0 件」を混同しない（L-074/L-086・Issue #130）**: `tools/check_pending_pr_reviews.py` は
  PR 一覧取得（`gh pr list`）自体が失敗した場合、**`NO_PENDING_PRS`（exit 0）を返さず** stderr に
  `ERROR: gh_unavailable`、stdout に `GH_UNAVAILABLE` を出力して **exit code 3** で終了する。
  呼び出し元は exit code を確認し、3 の場合は「0 件」と解釈せず下記の代替フローで直接取得すること。
  他の `tools/*.py` を新規に書く場合も同じ原則（取得失敗を沈黙して空リスト化しない）に従う。
- `check_pending_pr_reviews.py` 等が `FileNotFoundError`（gh 不在）や 403 を返した場合の代替フロー:

```
1. mcp__github__list_pull_requests(state="open") でオープン PR を取得
2. 各 PR について:
   a. mcp__github__pull_request_read(method="get_reviews") でレビュー取得
   b. mcp__github__pull_request_read(method="get_review_comments") でスレッド確認
   c. mcp__github__pull_request_read(method="get") で作成日時確認
3. needs_response / ready_to_merge をメインセッション側で判定する
```

## 5. ローカル実行との違い

`gh` が GitHub に直接到達できるローカル環境では、repo スコープ操作も `gh` で動く。その場合は従来どおり:

- repo 指定に `-R kai-kou/tsukuru-studio` を付与する
- `gh pr create` に `--head {現在のブランチ}` `--base main` を付与する

クラウドかどうかは `CLAUDE_CODE_REMOTE` で判定できる（`true` ならクラウド = MCP 一次経路）。

### 5.1 `GH_TOKEN` / `GITHUB_TOKEN` の 2 モード（公式仕様・誤読しやすい）

| モード | 条件 | 挙動 |
|--------|------|------|
| パススルー | 環境設定で自分のトークンを設定した | 値がそのままコンテナに渡る。`gh` とスクリプトはそれを直接使う |
| プロキシ注入 | どちらも未設定 | コンテナが両変数を **プレースホルダ文字列 `proxy-injected`** にし、プロキシが送信時に実認証情報へ差し替える。`gh` はトークン無しで動くが、**`GITHUB_TOKEN` を直読みするスクリプトはプレースホルダを掴む** |

判定は `echo $GH_TOKEN`（`proxy-injected` なら注入モード）。

- **どちらのモードでも repo スコープ REST の 403 は解消しない**（403 は attach の問題・§0）。
  実測でも `Bearer proxy-injected` / 実トークン / ヘッダ無しの 3 パターンすべて同一 403 だった。
- **`GH_TOKEN` の値を解決策として触らない**。トークンを差し替えても 403 は変わらず、
  セッション環境変数の書き換えはユーザーのアカウント設定（A-6）に属する。

## 6. 参照

| ドキュメント / ツール | 関係 |
|------------------------|------|
| `CLAUDE.md`「gh CLI / GitHub 操作」節 | 要約（本ファイルが SSOT） |
| `docs/rules/lessons-core.md` L-114 | クラウド gh ブロックの Hot 層 lesson |
| `docs/rules/lessons-core.md` L-079 | git push が 403/413/502 のときのフォールバック |
| `docs/rules/env-vars.md` | GitHub Variables がクラウド 403 化した後の env 供給・設定経路（§2.4 の詳細） |
| `.claude/skills/apply-base/SKILL.md` | ベース取得を git clone / MCP 経路で行う（gh api contents 非依存） |
