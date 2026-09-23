# ユーザー通知トリアージ（@mention 厳選ワークフロー・SSOT）

> **このファイルは「ユーザーへの @mention 通知をどう厳選するか」の唯一の正本（SSOT）である。**
> `user-confirmation-minimization.md`（A/B/C/D 分類・A-1〜A-6）の **通知レイヤー実装**。
> 放置される通知の根本原因は 2 つ: **RC-1**（障害・バグ起因を @mention してしまう＝本来 Claude が自律修正すべき案件）と
> **RC-2**（「ユーザーが取るべき具体的アクション」がなく状況ダンプだけ）。経緯全文は `user-notification-triage-detail.md` §0。

---

## 1. トリアージの基本原則

```
「ユーザー対応が必要」と通知したくなった
  ↓
その項目は A-1〜A-6（user-confirmation-minimization.md §1 の既約境界外）に該当するか？
  ├─ 該当する（A 区分）→ @mention する。ただし「具体的ユーザーアクション」を必ず添える（§3）
  └─ 該当しない（B/C/D）→ @mention しない
       ├─ 障害起因（バグ・エラー）→ L-077 で自律修正（§5 の動線）
       ├─ B: ツール改修で自律化 → 実装 Issue として処理
       ├─ C: ルール整備済みで自律処理（定期レポート auto-close 等）
       └─ D: 外部要因 → フォールバックで継続（A-6 の課金設定のみ別途 @mention）
```

**鉄則**: A-1〜A-6 に一致しない `@mention` は原則 CP-6 違反。「判定に迷ったら B または C」（安易な A 化を禁止）。

---

## 2. 機械トリアージ（判定は `tools/triage_notification.py` が実装）

全 `@mention` 経路の前段に決定論的な分類器が組み込み済み。**分類ロジックの正本はコード**（本ファイルは方針のみ）。

```bash
python3 tools/triage_notification.py classify --text "{通知候補}" --labels "{ラベル}"
python3 tools/triage_notification.py --self-test    # CI / セルフレビューで実行
```

判定順の要点: ① **A-6（課金・OAuth・アカウント設定）は障害起因でも A** ② それ以外の障害シグナル（`type:bug` ラベル・エラー/失敗/停止等）は **B**（自律修正）③ A-1〜A-5 検出 → **A** ④ 既定は **B**。`mention = (action_class == "A")`。

**経路との合成（`slack_notify.py` の `_resolve_route_mention`・#64）**: 各経路は「`@mention` を意図するか（既定）」を宣言し、分類器はその両方向のゲートとして働く。優先順は ① `--no-mention`（`--force-mention` より強い）→ `--force-mention` の明示指定 ② **A-1〜A-6 検出 → 既定が FYI でも昇格** ③ 既定が FYI → 抑制 ④ **障害起因かつ A 非該当 → 降格して抑制**（RC-1）⑤ それ以外は既定どおり。

| 経路 | 既定 | この優先順の適用 |
|---|---|---|
| `approval` | FYI（PR 自律化済みでそれ自体は A 区分でない） | ①〜⑤ すべて |
| `publish` | `config/publish_events.yaml` の `fyi` フラグ（`fyi: false` = 取り消し困難な公開＝ A-2 相当の経路宣言として尊重し、降格は ④ のときだけ行う） | ①〜⑤ すべて |
| `waiting` | FYI（A 区分ゼロならメインチャンネルへ FYI 降格） | **`--force-mention` のみ**（`--no-mention` は未対応。抑制が既定なので上書きの必要がない） |

---

## 3. A 区分通知の必須要件（RC-2 対策）

`@mention` する通知は各項目に以下を **必ず** 含める。状況説明だけの通知は禁止。

| 必須要素 | 説明 | 悪い例 → 良い例 |
|---------|------|---------------|
| **具体的ユーザーアクション** | ユーザー *だけ* ができる操作を 1 文で | 「重複検出ロジックが原因」→「課金画面で上限を $X に引き上げてください（あなたのアカウント権限が必要）」 |
| **該当境界** | A-1〜A-6 のどれか | 「（A-6: 課金設定）」 |
| **取らない場合の結果** | 放置するとどうなるか | 「未対応だと次回スケジュール公開が止まります」 |
| **Claude 側の状態** | 自律でやれることは済ませたこと | 「代替手段で処理は継続中。課金復旧で本系統も再開します」 |

> **判定**: 「ユーザーが取るべき具体アクションを 1 文で書けない」なら、それは A 区分ではない（= `@mention` しない）。

---

## 4. 通知経路と日次レポート（要約）

`slack_notify.py`（waiting / approval / publish / daily-progress）の各経路に §2 の機械トリアージが組み込み済み。共通挙動: **A 区分ゼロなら `@mention` を抑制し FYI 降格**（`--force-mention` / `--no-mention` で上書き可）。唯一の例外は **`publish` の非 FYI イベント** で、§2 の表のとおり `config/publish_events.yaml` の宣言を既定として A 区分ゼロでも `@mention` を維持する（降格するのは障害起因のときだけ）。日次進捗は FYI が基本で、「要対応」欄には A 区分のみを載せる（B/C/D を混ぜない）。**真の要対応がゼロの日は `@mention` しない**（毎日 ping しない）。経路別の実装表・レポート構成は `user-notification-triage-detail.md` §4。

---

## 5. 障害（バグ・エラー）を検出したときの正しい動線（RC-1 対策）

ワークフロー実行中に障害（`ValueError`・API 失敗・ファイル不在・停止等）を検出したら、**絶対に `@mention` で丸投げしない**。

```
障害検出
  → STOP → 未コミットを保存（session-safety-rules.md）
  → problem-investigation-protocol.md の5ステップ
  → 自己解決可能 → 修正実装（type:bug Issue として処理）。ユーザー確認不要
  → 自己解決不可 かつ A-1〜A-6 該当（例: A-6 課金・OAuth）→ §3 の必須要件を満たして @mention
```

`type:bug` / `status:waiting-claude` の Issue は **Claude の作業** でありユーザーの To-Do ではない。これらを「要対応」に混ぜない。

判定が曖昧な新種通知のみ、Agent Teams で多角検証してから分類ルールを追加する（通常は機械トリアージで完結・詳細は detail §6）。

---

## 6. 完了・成功の定義

チェックリスト（§1・§3・§5 の再掲）は `user-notification-triage-detail.md`「完了・成功の定義」。
判定の実体は `tools/triage_notification.py`（分類器・`--self-test` 付き）が持つ。

---

## 7. 参照

| ドキュメント | 関係 |
|------------|------|
| `user-confirmation-minimization.md` | A/B/C/D 分類・A-1〜A-6 の SSOT（本ファイルは通知レイヤー実装） |
| `problem-investigation-protocol.md` | 障害起因の自己解決プロトコル（RC-1 の動線） |
| `slack-notification-rules.md` | Slack 通知のチャンネル分離・セットアップ |
| `tools/triage_notification.py` | 機械トリアージ分類器（分類ロジックの正本） |
| `user-notification-triage-detail.md` | §0 背景・§4 経路別実装表・§6 専門チーム詳細 |
