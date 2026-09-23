---
name: apply-base
description: kai-kou/claude-code-repository-base（公開ベース）の汎用ルール・スキル定義・ハーネス一式を現在のリポジトリへ反映（適用・同期）する。「claude-code-base の内容を反映して」「claude-code-base を適用して」「ベースを反映して」「ベース設定を取り込んで」「claude-code-base で初期化して」「claude-code-base のアップデートを確認して適用して」「ベースのアップデート内容を確認して」等と依頼された時に使用する。前回適用時点からの更新内容（コミット一覧・手動手順が必要な更新）の確認も本スキルが担う。公開リポジトリのベースを git clone（クラウドでは gh の repo スコープ操作が 403 でブロックされるため git/MCP 経路）で取得して適用するため、ユーザーがコマンドを打つ必要はない。
effort: medium
---

> 🔴 **GitHub 操作の経路（必読・L-114）**: クラウド実行環境では `gh` がプリインストールされず、
> 導入しても repo スコープ REST が 403 になる。**本ファイル内の `gh ...` コマンドはローカル実行専用** で、
> クラウドでは `mcp__github__*` に読み替える（対応表: `docs/rules/github-mcp-fallback-patterns.md` §2。
> ラベル一覧/作成・マイルストーン・release 作成・variables は MCP に等価が無く **クラウドでは実行不可**・同 §2.5）。


# claude-code-base 適用スキル（apply-base）

現在のリポジトリ（カレントの作業リポジトリ）に、`kai-kou/claude-code-repository-base`（公開ベース）の
**ルール・スキル定義・ハーネス・ツール一式** を反映する。ユーザーは
「claude-code-base の内容を本リポジトリに反映して」程度の自然文を伝えるだけでよく、
コマンド実行・ファイル名の把握は不要。本スキルが gh 経由で適用機構を取得して実行する。

## 0. 前提と方針

> 🔶 **取得元**: `apply-base` の取得元は **公開リポジトリ `kai-kou/claude-code-repository-base` を正とする**（§1・§2 参照）。公開リポジトリからの取得になるため、アクセス権を持たない第三者も本スキルをそのまま使える。
- ベースは **public**（`kai-kou/claude-code-repository-base`）の想定。取得は **git clone（`https://github.com/...`）を一次経路** にする（public のため認証不要。プロキシ/トークンが付与されていても支障はない）。
- 🔴 **クラウド実行環境（`CLAUDE_CODE_REMOTE=true`）では `gh api repos/.../contents`・`gh repo clone` が egress プロキシに 403 でブロックされる**（L-114・`github-mcp-fallback-patterns.md`）。よってベース取得に `gh api contents` を使わない。クラウドで生存するのは `git clone https://...` と公式 MCP（`mcp__github__get_file_contents`）のみ。
- 適用は **冪等**。初回適用にも、ベース更新後の再同期にも同じ手順を使う。
- 既存の `CLAUDE.md` / `docs/project-mission.md` は **既定で保護**（上書きしない）。
- 🔴 **SYNC_PATHS 配下（`docs/rules` / `.claude/*` / `tools` / `scripts` / `.mcp.json` / `.claude/settings.json` 等）も無条件上書きしない**。
  前回適用したベースの SHA（`.claude/base-sync-state.json`）を祖先に、ファイルごとに <!-- refcheck:ignore -->
  「ベース側が無変更なら触らない / 下流が祖先のままなら更新 / 両側変更なら 3 方向マージ /
  衝突・検証失敗なら下流を温存して `<path>.base-latest` を併置」と振り分ける。**衝突マーカーは
  ワークツリーに書かれない**（`tools/merge_three_way.py` が JSON 構文・重複キー・`hooks.*` など
  順序依存配列の相対順序まで検証し、通らなければ採用しない）。ベースに存在しないファイル
  （下流独自のルール・スキル）は一切触られない。
  `modules.yaml` も同じ 4 分岐に乗る（専用の意味マージャは廃止済み・Issue #509 で実測検証: 3 方向
  マージの方が `enabled` / `project:` 以外の下流カスタマイズも保護でき、衝突時も安全に倒れる）。
  未解決の `.base-latest` が残っているファイルは、祖先がずれるためマージせず
  毎回「要確認」として再報告される（取り込んで `.base-latest` を消すまで消えない）。
- これらの保護・置換・symlink 同期はベース側の適用機構が内部で行う。本スキルはそれを起動するだけ。

## 1. 事前チェック（自律実行）

```bash
# カレントが対象リポジトリのルートであること（サブディレクトリ実行を厳密に拒否）
[ -z "$(git rev-parse --show-prefix 2>/dev/null)" ] || { echo "エラー: 対象リポジトリのルートで実行してください" >&2; exit 1; }
# git でベースへ到達できること（クラウドでは git プロキシが認証付与・gh auth status は当てにしない）
git ls-remote https://github.com/kai-kou/claude-code-repository-base.git HEAD >/dev/null 2>&1 \
  || { echo "ベースに到達できません: GH_TOKEN/ネットワーク設定を確認してください" >&2; exit 1; }
```

- ベースに到達できない場合は `docs/rules/problem-investigation-protocol.md` の手順で自己解決を試みる（GH_TOKEN の有無確認等）。それでも不可ならユーザーへ認証を依頼する（A-6 該当時のみ）。
- ⚠️ `gh auth status` はクラウドで stderr に「token invalid」と出すが終了コードは 0 で、かつ repo 操作は別途 403 になる。**認証可否の判定に `gh auth status` を使わない**（git ls-remote / MCP で実到達を確認する）。
- 🔴 **タスク実行モードによっては `git ls-remote` 自体が 403 になる（L-117）**: GitHub Issue/PR 対応のリモートタスク（システムプロンプトに「Repository Scope」が単一リポジトリで明示される形態）では、`mcp__Claude_Code_Remote__add_repo` が提供されず、クロスリポ git 操作自体が遮断される（GH_TOKEN/ネットワーク設定の問題ではない）。この場合は GH_TOKEN 依頼に進まず、「このタスク実行モードでは claude-code-base に到達できない。`claude-code-base を反映して` を通常の claude.ai/code セッション（チャット形式）で実行してほしい」とユーザーへ案内し、本タスクでの適用は断念する。

## 2. 適用の実行（コア手順）

ベース本体を **git clone** で取得し、同梱の `scripts/apply-to-repo.sh` を実行する。スクリプトが
設定一式を展開・プレースホルダ置換・symlink 同期まで行う。**対象リポジトリのルートで実行すること。**

```bash
# クラウド・ローカル共通: git clone でベースを取得して適用スクリプトを実行
tmp="$(mktemp -d)"
git clone --depth 1 https://github.com/kai-kou/claude-code-repository-base.git "$tmp/base" >/dev/null 2>&1 \
  || { echo "ベースの clone に失敗しました" >&2; exit 1; }
bash "$tmp/base/scripts/apply-to-repo.sh"            # オプションは末尾に付与（例: --prune --tz Asia/Tokyo）
rm -rf "$tmp"
```

- `apply-to-repo.sh` 内部の `fetch_base()` も `git clone https://...` フォールバックを持つため、`--base`/`--ref` で別リポ・別 ref を指定しても取得できる（`gh repo clone` はクラウドで失敗するが git フォールバックが効く）。
- 単一ファイルだけ確認したい等で MCP を使う場合は `mcp__github__get_file_contents(owner="kai-kou", repo="claude-code-repository-base", path="scripts/apply-to-repo.sh")` で取得できる（クラウドの 403 を回避）。`gh api repos/.../contents` は使わない。
- 主なオプション: `--prune`（`modules.yaml` で無効化したモジュール資産を除去）/ `--tz` / `--overwrite-project`（CLAUDE.md 等も上書き）/ `--check-updates`（アップデート内容の表示のみ・適用なし）/ `--dry-run`（適用対象と 4 分岐の判定結果だけ表示）/ `--no-merge`（3 方向マージを行わず旧来の無条件上書きに戻す退避経路）。

## 2.5 アップデート確認（「アップデートを確認して適用して」の場合）

`apply-to-repo.sh` は実行の冒頭で **前回適用マーカー**（対象リポジトリの `.claude/base-sync-state.json`・ <!-- refcheck:ignore -->
適用済みベースの SHA と日時）を読み、以下を自動表示する。追加の手作業は不要:

1. **前回適用以降の更新コミット一覧**（ベースの `git log <前回SHA>..HEAD --oneline`）
2. **手動手順が必要な更新**（ベース側の移行ノートから前回適用日以降のエントリを抜粋。
   このノートはベースのクローンから読み取って画面表示されるだけで、適用先には配置されない）

このため「アップデートを確認して適用して」も §2 と同じコマンドでよい。運用手順:

- 表示された **更新コミット一覧** を 3〜5 行に要約してユーザー報告（§4）に含める。
- 表示された **手動手順（base-update-notes のエントリ）** は、§3 の `*.base` 取り込みと同様に
  **本スキルが自律実施** する（対象ファイル・コマンドがエントリに明記されている）。
  A-1〜A-6 相当の不可逆操作を含む手順のみユーザー確認に回す。
- **内容の確認だけ** を求められた場合（「アップデート内容を教えて」等）は `--check-updates` を付けて
  実行する（表示のみで適用しない）。
- マーカーが無い（初回適用 or 旧版からの移行）場合は一覧表示をスキップして通常適用し、
  適用完了時にマーカーが作成される。**マーカーは次回確認の基準点なので必ずコミットに含める**。

## 2.6 自己検査（必ず実施・コミット前・Issue #3）

同期直後、コミットする前に `tools/check_base_sync_integrity.py` を実行する。適用は成功しても
「正常系のログ 1 行」に紛れて気づけない壊れ方をすることがある（子固有資産の消失・`modules.yaml`
の `project.name` が非公開母艦名へ化ける、など）。

```bash
python3 tools/check_base_sync_integrity.py
```

- `[NG]` が 1 件でもあれば **コミットせずに原因を修正する**（fail-closed。「たぶん大丈夫」で通さない）。
  子固有資産の欠落は取り込み漏れ、`project.*` の食い違いは `modules.yaml` の該当行を修正、
  プレースホルダ保護の欠落は `scripts/bootstrap.sh` の該当箇所をベースの原本と突き合わせて復元する
  （旧・modules.yaml 専用マージスクリプトが未同期のスナップショット等にまだ残っている場合は、
  `tools/check_base_sync_integrity.py` がその難読化構造の欠落も検査する）。
- 全件 `[OK]` を確認してから §3 以降・コミットへ進む。

## 3. 反映後の取り込み（同期サマリーとプロジェクト固有ファイル）

### 3.0 同期サマリーの処理（必ず実施）

適用の最後に「触れず / 配置・更新 / マージ / 要確認」の件数と該当ファイルが表示される。

- **マージ** されたファイルは、コミット前に `git diff` を確認する（特に `hooks` や `permissions` のような
  **並び順に意味がありうる配列** を含む変更は目視する。行としては非衝突でも意図しない順序になりうる）。
- **要確認**（`<path>.base-latest` 併置）は `diff <path> <path>.base-latest` で取り込み、
  済んだら `.base-latest` を削除する。**放置してコミットしない**。
- 「祖先: 未使用（上書き同期）」と出た場合は下流の変更が保護されていない。初回適用なら正常だが、
  2 回目以降なら `.claude/base-sync-state.json` がコミットされているかを確認する。 <!-- refcheck:ignore -->

### 3.1 下流独自ルールの置き場所（`docs/rules/local/`）

下流固有のルールを `docs/rules/*.md`（ベース管理下）に直接書き足すと、ベース側が同じファイルを
更新した回にマージ確認の手間が残る。**独自ルールは `docs/rules/local/{同名}-local.md` に分離する**
（ベースに存在しないファイルは同期対象外なので常に無傷）。常駐させたいものは下流側で
`.claude/rules/` へ symlink する。既存ルール本文そのものの書き換えだけは分離できないので、
3 方向マージの衝突検出に委ねる。

### 3.2 プロジェクト固有ファイルの取り込み

適用機構は既存のプロジェクト固有ファイルを保護し、ベース版を `*.base` として横に置く。

- `CLAUDE.md.base` が生成されていれば、対象リポジトリの `CLAUDE.md` に
  「応答スタイル / 必読ルール表 / PR 自律化方針」などの必要な節を取り込む（既存の内容は尊重）。
- `docs/project-mission.md.base` があれば、ミッション・KPI を対象プロジェクト向けに記入する。
- これらの取り込みは、対象リポジトリの既存内容を壊さない範囲で自律的に提案・実施する。
- **取り込み判断の基準**: プロジェクト固有ファイルは保護し、汎用ハーネスのみ取り込む。取り込むべきか迷う差分は、無理に一括反映せず `*.base` を残したまま Issue 化して後で判断する。

## 4. コミットと報告

- 反映差分はその場でコミットする（対象リポジトリの運用に従う。作業ブランチ運用なら
  `claude/apply-base-<日付>` 等のブランチを切って PR 経由。単純取り込みならブランチへ直接コミット）。
- 報告は「何ができるようになったか（ルール・スキル・ハーネスが適用された）」を中心に簡潔に。
  適用された区分（ルール / スキル / ハーネス / ツール / コマンド）と、`*.base` として
  手動取り込みが必要なファイルがあればそれを 1〜2 行で示す。
- **セッション途中の適用ではフック（`.claude/hooks/`）・`settings.json` の変更は再読込されない**
  （起動時スナップショット・公式仕様）。完了報告に「フック・settings の変更は新セッションから
  有効になる」旨を 1 行添える（`scripts/apply-to-repo.sh` の完了サマリーにも同じ注意が出力される）。
- 再同期（アップデート適用）の場合は、§2.5 で表示された更新内容の要約（3〜5 行）と、
  実施した手動手順（base-update-notes 該当分）を報告に含める。

## 5. 再同期（2 回目以降）

初回適用後は本スキル（`.claude/skills/apply-base/`）も対象リポジトリに入るため、
以降は同じ自然文（「claude-code-base を反映して」「アップデートを確認して適用して」等）で
再同期が起動する。手順は §2 と同一（冪等）で、§2.5 のアップデート確認が自動で先行する。

## 完了・成功の定義

- [ ] ユーザーがコマンドを打たずに、自然文の指示だけで適用が実行された
- [ ] `python3 tools/check_base_sync_integrity.py` が全件 `[OK]` を返した（§2.6・Issue #3）
- [ ] ルール（`docs/rules/` + `.claude/rules/` symlink）・スキル・ハーネス・ツールが対象リポジトリに展開された
- [ ] 既存の `CLAUDE.md` / `docs/project-mission.md` が破壊されていない（保護 or `*.base` 併置）
- [ ] **SYNC_PATHS 配下の下流カスタマイズが保持されている**（同期サマリーの「要確認」がゼロ、
      または `.base-latest` を全て取り込んで削除済み。衝突マーカーがワークツリーに残っていない）
- [ ] 再同期の場合、前回適用以降の更新内容と手動手順（base-update-notes 該当分）が確認・実施された
- [ ] `.claude/base-sync-state.json`（同期マーカー）が更新され、コミットに含まれている <!-- refcheck:ignore -->
- [ ] 反映差分がコミットされ、アウトカム中心の報告が出された
