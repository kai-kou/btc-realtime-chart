#!/usr/bin/env python3
"""
通知トリアージ分類器 — 「ユーザー対応が必要」Slack メンションの厳選

`docs/rules/user-confirmation-minimization.md` の A/B/C/D 分類（A-1〜A-6 既約境界外）を
**通知レイヤーに機械適用** する。通知候補（テキスト + ラベル）を受け取り、本当にユーザーの
最終判断・操作が必要（A 区分）かどうかを決定論的に判定する。

設計原則（CP-6・L-077・本 PR の2大根本原因対策）:
  1. @mention するのは A-1〜A-6 に該当する項目だけ。B/C/D は自律処理 or 無 mention FYI。
  2. **障害（バグ・エラー・失敗）起因の通知は @mention しない**。L-077 の専門チーム調査
     プロトコルで Claude が自律修正すべき案件（type:bug 等）であり、ユーザーに丸投げしない。
     例外: 障害でもユーザーのアカウント操作が物理的に必要な場合（OAuth 再発行・課金）は A-6。
  3. **A 区分の通知には「ユーザーが取るべき具体的アクション」が必須**。状況ダンプだけの通知
     （ユーザーが何をすればいいか分からない通知）は放置の原因になるため A としては不適格。

使い方:
  python3 tools/triage_notification.py classify --text "..." --labels "type:bug,status:waiting-user"
  python3 tools/triage_notification.py classify --text "..." --json
  python3 tools/triage_notification.py --self-test
"""

import argparse
import json
import re
import sys

# ── 障害（バグ・エラー・失敗）シグナル ──
# これらが含まれる通知は「Claude が L-077 で自律修正すべき障害」であり、原則 @mention しない。
_FAILURE_LABELS = {"type:bug", "type:retro-try", "type:retro", "type:incident"}
_FAILURE_PAT = re.compile(
    r"エラー|失敗|停止|未実装で停止|バグ|例外|不具合|"
    r"Error|Exception|Traceback|ValueError|TypeError|KeyError|"
    r"クラッシュ|落ちる|動かない",
    re.IGNORECASE,
)

# ── A-6: アカウント・課金設定（ユーザーの権限が物理的に必要） ──
# 障害起因であっても、ユーザーのアカウント操作が必須なものはここで A に確定する。
_A6_PAT = re.compile(
    r"課金|請求(?:.{0,4}上限|超過|エラー|額)|クレジット購入|残高|チャージ|おやつ代|Billing|"
    r"OAuth|トークン再発行|リフレッシュトークン|refresh.?token|"
    r"API\s*有効化|アカウント設定|アカウント.?(BAN|凍結|停止)|"
    r"クレジット枯渇|クレカ|"
    r"支払(?:い)?.{0,3}(必要|遅延|エラー|失敗|できない|未完了|期限)|入金.{0,3}必要|"
    r"決済(?:[がのは]通らな|失敗|できない|エラー)|サブスク更新|2段階認証|"
    r"(?:API|アクセス).?キー.*(失効|無効|期限切れ|切れ)|"
    r"(?:API|アクセス)トークン.*(失効|無効|期限切れ|切れ)|"
    r"利用規約.*同意|無料枠.*上限|Actions.*クレジット",
    re.IGNORECASE,
)

# ── A-2: 動画の即時手動公開（publishAt 自動スケジュールは対象外） ──
# 「手動/緊急/即時」または「private→public（動画文脈）」のみを A-2 とする。
# 「急遽公開した（過去形 FYI）」「private リポジトリを public（リポジトリ設定）」等の
# 紛らわしい表現を A-2 と誤判定しない（過剰 @mention を防ぐ）。
_A2_PAT = re.compile(
    r"手動公開|手動で公開|緊急公開|即時公開|"
    r"private\s*(?:→|->|から|の動画を)\s*(?:public|公開)",
    re.IGNORECASE,
)
# EXCLUDE は「純粋な自動スケジュール完了」文脈のみに絞る（「公開スケジュール」単独は除外しない）
_A2_EXCLUDE_PAT = re.compile(
    r"publishAt\s*(?:自動|設定完了|スケジュール|で自動)|自動公開(?:設定完了|完了|済)|スケジュール公開(?:完了|設定済)"
)

# ── A-3: 品質ゲート致命的 NG（ファクトチェック等。具体例はプロジェクト定義）──
# 汎用ベースでは機械的指標（fact_check_flags / ランク C 等）を主に検出する。
_A3_PAT = re.compile(
    r"ファクトチェック.*(致命的|虚偽|出典皆無|裏付けなし)|致命的.*(誤情報|虚偽)|虚偽断定|"
    r"ハルシネーション.*(残|含|疑い|思われる|記述)|根拠のない.*断定|"
    r"一次ソース.*確認できない.*断定|誤った数字.*断定|"
    r"fact_check.*ランク\s*C|fact_check_flags.*(致命的|ランク\s*C)",
    re.IGNORECASE,
)

# ── A-4: サーキットブレーカー ──
_A4_PAT = re.compile(
    r"サーキットブレーカー|修正サイクル.*(超|2回|2サイクル|3回)|無限ループ|2サイクル超|"
    r"[3-9]回.*(修正|試みて?も?|繰り返して?も?|まだ直).*(?:エラー|直らない|改善しない|収束|失敗)|"
    r"同じ.*(修正|エラー|失敗|問題).*(何度|繰り返|収束しない|直らない)|"
    r"ループ.*(抜けられない|から出られない)|"
    r"[2-9]\s*サイクル.*(以上|超|続|経過)"
)

# ── A-5: 新規マイルストーン ──
_A5_PAT = re.compile(
    r"新規マイルストーン|マイルストーン.?(追加|新設|作成)|新しいマイルストーン|新しいマイル|新規マイル|"
    r"マイル.?(新設|追加|作成)"
)

# ── A-1: main 直接 push ──
_A1_PAT = re.compile(
    r"main\s*(ブランチ)?\s*(へ|に|への|に対する)?\s*直接\s*(push|プッシュ|commit|コミット)|"
    r"main\s*(ブランチ)?\s*(への|に対する)\s*(push|プッシュ)|"
    r"main\s*(ブランチ)?\s*に\s*(誤って|誤)\s*(push|プッシュ|commit)",
    re.IGNORECASE,
)

# ── C: 自律処理で解消（ユーザー不要） ──
_C_LABELS = {"type:marketing-report", "type:weekly-report", "phase:1-neta",
             "phase:2-research", "type:comment-response"}
_C_PAT = re.compile(
    r"週次レポート|マーケティングレポート|週次マーケティング|"
    r"ネタ候補|リファインメント|"
    r"Phase\s*2|リサーチ依頼|Deep\s*Research|research-runner|"
    r"コメント対応|コメント返信|技術質問|コメント監視",
    re.IGNORECASE,
)

# ── B: ツール改修・実装で自律化可能 ──
_B_PAT = re.compile(
    r"ローカル実行|ローカルで|note\s*公開|note\s*記事|Shorts.*レンダリング|"
    r"ツール改修|実装|スクリプト|パイプライン|フック|desync",
    re.IGNORECASE,
)


def classify_item(text: str, labels: list | None = None) -> dict:
    """通知1項目を A/B/C/D に分類する。

    Returns:
        {
          "action_class": "A"|"B"|"C"|"D",
          "boundary": "A-1".."A-6" or None,
          "mention": bool,            # ユーザーに @mention すべきか（A 区分のみ True）
          "requires_user_action": bool,  # A 区分は具体アクション文面が必須
          "is_failure": bool,         # 障害起因か（L-077 自律修正対象）
          "reason": str,
        }
    """
    text = text or ""
    labels = set(labels or [])
    is_failure = bool(_FAILURE_LABELS & labels) or bool(_FAILURE_PAT.search(text))

    def A(boundary, reason):
        return {
            "action_class": "A", "boundary": boundary, "mention": True,
            "requires_user_action": True, "is_failure": is_failure, "reason": reason,
        }

    def non_A(cls, reason):
        return {
            "action_class": cls, "boundary": None, "mention": False,
            "requires_user_action": False, "is_failure": is_failure, "reason": reason,
        }

    # 1) A 区分（A-1〜A-6）を最優先で判定する。
    #    A パターンは固有名詞的に具体的（サーキットブレーカー・課金・ファクト致命的 NG 等）なため、
    #    障害キーワード（停止・ValueError 等）と共起しても A を優先する。
    #    例:「サーキットブレーカー発動で停止」は障害語「停止」を含むが A-4（要ユーザー判断）であり、
    #    is_failure を先に評価すると誤って B（@mention 抑制）に落ちてしまうため、A 判定を先に置く。
    if _A6_PAT.search(text):
        return A("A-6", "アカウント・課金設定の変更はユーザー権限が物理的に必要（A-6）")
    if _A2_PAT.search(text) and not _A2_EXCLUDE_PAT.search(text):
        return A("A-2", "動画の即時手動公開は収益・ブランドに直結し取消困難（A-2）")
    if _A3_PAT.search(text):
        return A("A-3", "ファクトチェック致命的 NG。誤情報公開リスク（A-3）")
    if _A4_PAT.search(text):
        return A("A-4", "サーキットブレーカー発動。無限ループ・予算浪費防止の続行判断（A-4）")
    if _A5_PAT.search(text):
        return A("A-5", "新規マイルストーンはプロジェクト計画の骨格に影響（A-5）")
    if _A1_PAT.search(text):
        return A("A-1", "main ブランチへの直接 push は保護ブランチ操作（A-1）")

    # 2) A 非該当の障害起因は B（L-077 で Claude が自律調査・修正。@mention しない）
    if is_failure:
        return non_A("B", "障害（バグ・エラー・失敗）起因。L-077 専門チーム調査プロトコルで自律修正すべき案件のため @mention しない")

    # 4) C 区分（自律処理で解消・ユーザー不要）
    if (_C_LABELS & labels) or _C_PAT.search(text):
        return non_A("C", "ルール整備済みで自律処理可能（週次レポート auto-close / ネタ候補 / Phase2 research 等）。@mention しない")

    # 5) B 区分（ツール改修・実装で自律化可能）
    if _B_PAT.search(text):
        return non_A("B", "ツール改修・実装で自律化可能。実装 Issue として処理し @mention しない")

    # 6) デフォルト: B（user-confirmation-minimization.md §2「迷ったら B または C」）
    return non_A("B", "A-1〜A-6 に一致しないため自律処理対象（既定 B）。@mention しない")


def triage_items(items: list) -> dict:
    """複数項目をトリアージし、@mention 可否を集約する。

    Args:
        items: [{"text": str, "labels": [..]}], または [str]
    Returns:
        {"mention": bool, "a_items": [...], "non_a_items": [...], "results": [...]}
    """
    results = []
    a_items, non_a_items = [], []
    for it in items:
        if isinstance(it, str):
            text, labels = it, []
        else:
            text, labels = it.get("text", ""), it.get("labels", [])
        r = classify_item(text, labels)
        r["text"] = text
        results.append(r)
        (a_items if r["mention"] else non_a_items).append(r)
    return {
        "mention": len(a_items) > 0,
        "a_items": a_items,
        "non_a_items": non_a_items,
        "results": results,
    }


# ──────────────────────────────────────────────
# セルフテスト
# ──────────────────────────────────────────────

_SELF_TEST_CASES = [
    # (text, labels, expected_class, expected_mention)
    # ユーザーが実際に放置した実例（save_meta ValueError 停止）→ 障害起因 B・@mention しない
    ("[19:00 hourly-routing] --activate-schedule が Issue #2483 の根本対策2未実装で停止（save_meta ValueError）",
     ["type:bug", "status:waiting-claude"], "B", False),
    # 現在の実 waiting-user 3件
    ("[コメント対応] 🟡 技術質問 — Copilot一強時代は終わった？", ["priority:medium", "status:waiting-user", "type:content"], "C", False),
    ("[週次レポート] マーケティング状況 — 2026-06-01", ["status:waiting-user", "type:marketing-report"], "C", False),
    # A 区分（本当にユーザー対応が必要）
    ("X API クレジットが枯渇。おやつ代のチャージをお願いします", ["priority:critical"], "A", True),
    ("YouTube OAuth リフレッシュトークンの再発行が必要", ["type:bug"], "A", True),  # 障害でも A-6
    ("ファクトチェックで致命的な虚偽断定を検出。公開前に確認をお願いします", [], "A", True),
    ("サーキットブレーカー発動（修正サイクル2回超）。続行判断をお願いします", [], "A", True),
    ("新規マイルストーン M10 の追加可否を判断してください", [], "A", True),
    ("動画 V120 を private→public で緊急手動公開してよいか", [], "A", True),
    # publishAt 自動スケジュールは A-2 ではない（除外語）
    ("動画 V120 の公開スケジュール設定完了（publishAt 自動）", [], "B", False),
    # 障害系（自律修正対象・@mention しない）
    ("画像パイプラインが ValueError で失敗", ["type:bug"], "B", False),
    ("Phase 2 リサーチ依頼（research-runner 自律実行）", ["phase:2-research"], "C", False),
    # A 区分 × 障害キーワード共起（A を優先・誤 B 化を防ぐ・回帰防止）
    ("サーキットブレーカー発動で停止（2サイクル超）。続行判断をお願いします", ["type:bug"], "A", True),
    ("ファクトチェック致命的 NG で公開を停止。確認をお願いします", ["type:bug"], "A", True),
    ("新規マイルストーン M10 追加可否（関連 Issue を再オープン済み）", [], "A", True),
    # 専門チーム検証で発見した false-negative（言い回し漏れ・取りこぼし防止）
    ("公開スケジュールが遅れたので今すぐ手動公開をお願いします", [], "A", True),  # A-2 過剰除外の修正
    ("クレカの上限に達したので追加入金が必要です", [], "A", True),
    ("OpenAI 請求ハード上限到達", [], "A", True),
    ("アクセストークンの有効期限が切れました", [], "A", True),
    ("ハルシネーションと思われる記述が成果物に残っています", [], "A", True),
    ("3回修正してもエラーが直りません", ["type:bug"], "A", True),  # A-4 を障害より優先
    ("ループから抜けられない状態です", [], "A", True),
    ("main に直接コミットしてしまいました", [], "A", True),
    # 専門チーム検証で発見した false-positive（過剰 @mention 防止・最重要回帰ガード）
    ("private リポジトリを public にする設定変更を実装", ["type:improvement"], "B", False),
    ("急遽公開した動画の反応が良い", [], "B", False),
    ("支払い処理のテストコードを実装", ["type:feature"], "B", False),
    ("ビルドが1回失敗したのでリトライします", ["type:bug"], "B", False),
    ("3回再生されたショート動画", [], "B", False),
]


def run_self_test() -> int:
    passed, failed = 0, 0
    for text, labels, exp_cls, exp_mention in _SELF_TEST_CASES:
        r = classify_item(text, labels)
        ok = (r["action_class"] == exp_cls) and (r["mention"] == exp_mention)
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {text[:50]!r}\n  expected class={exp_cls} mention={exp_mention}\n"
                  f"  got      class={r['action_class']} mention={r['mention']} ({r['reason']})")
    print(f"\nセルフテスト: {passed} passed, {failed} failed / {len(_SELF_TEST_CASES)} cases")
    return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="通知トリアージ分類器（A/B/C/D・A区分のみ @mention）")
    sub = parser.add_subparsers(dest="cmd")

    p_cls = sub.add_parser("classify", help="1項目を分類する")
    p_cls.add_argument("--text", required=True)
    p_cls.add_argument("--labels", default="", help="カンマ区切りラベル")
    p_cls.add_argument("--json", action="store_true")

    parser.add_argument("--self-test", action="store_true", help="セルフテストを実行")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    if args.cmd == "classify":
        labels = [s.strip() for s in args.labels.split(",") if s.strip()]
        r = classify_item(args.text, labels)
        if args.json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            mark = "🔔 @mention 必要（A区分）" if r["mention"] else "🤖 自律処理（@mention 不要）"
            print(f"{mark}")
            print(f"  action_class: {r['action_class']}" + (f" ({r['boundary']})" if r['boundary'] else ""))
            print(f"  is_failure: {r['is_failure']}")
            print(f"  reason: {r['reason']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
