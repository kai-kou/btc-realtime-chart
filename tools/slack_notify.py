#!/usr/bin/env python3
"""
Slack 通知ユーティリティ（汎用ベース）

環境変数:
  SLACK_BOT_TOKEN            - Slack Bot User OAuth Token (xoxb-...)
  SLACK_CHANNEL_ID           - メイン通知チャンネルID (C...) — セッション開始/終了・PR・パイプライン完了など
  SLACK_APPROVAL_CHANNEL_ID  - 承認依頼専用チャンネルID (C...) — approval/waiting 通知の送信先
                               未設定時は SLACK_CHANNEL_ID にフォールバック
  SLACK_PUBLISH_CHANNEL_ID   - 公開・マーケティング専用チャンネルID (C...) — publish 通知の送信先
                               未設定時は SLACK_APPROVAL_CHANNEL_ID → SLACK_CHANNEL_ID にフォールバック
  SLACK_MENTION_USER_ID      - approval / waiting / publish 通知でメンションするユーザーID (U...)
                               @mention の発火可否は全経路で機械トリアージ（tools/triage_notification.py）
                               が決める（A-1〜A-6 のみ発火・user-notification-triage.md §2）

通知タイプとチャンネルの対応:
  SLACK_CHANNEL_ID           → session-start / session-stop / pr / pipeline / message / progress / routine-idle
  SLACK_APPROVAL_CHANNEL_ID  → approval / waiting（ユーザーメンション付き・要アクション）
  SLACK_PUBLISH_CHANNEL_ID   → publish（公開・配信完了、@mention付き）

publish の --event-type: config/publish_events.yaml のキー駆動（既定は `published` のみ）。
  プロジェクト固有のイベント（動画公開・SNS配信等）は同ファイルに追記する
  （例は docs/rules/slack-notification-rules.md「project example」参照）。

使い方:
  python3 tools/slack_notify.py session-start --branch main
  python3 tools/slack_notify.py session-start --branch main --issue-title "[V007] Phase 3: 台本生成" --issue-url https://github.com/...
  python3 tools/slack_notify.py session-stop --branch main --summary "台本生成完了"
  python3 tools/slack_notify.py pr --pr-url https://... --pr-title "title" --branch feat/xxx
  python3 tools/slack_notify.py waiting --issues "Deep Research実行依頼" --branch main
  python3 tools/slack_notify.py pipeline --pipeline "音声" --video-id V006 --result "完了（15分32秒）"
  python3 tools/slack_notify.py message --text "任意のテキスト"
  python3 tools/slack_notify.py approval --summary "台本v1生成完了" --branch content/V007-xxx --issue-url https://github.com/...
  python3 tools/slack_notify.py publish --event-type published --title "コンテンツタイトル" --url https://example.com/xxx
  python3 tools/slack_notify.py daily-progress --summary "進捗サマリー" --action-items "要対応項目"
  python3 tools/slack_notify.py routine-idle --routine-name "改善Issue消化スプリント"
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import resolve_repo_slug  # noqa: E402

SLACK_API_BASE = "https://slack.com/api"
# Slack 通知内の Issue リンク生成用（正本: tools/repo_slug.py・#215）。
_REPO_SLUG = resolve_repo_slug("kai-kou/btc-realtime-chart")

# publish のイベント定義は config/publish_events.yaml で管理する（#358・config-driven）。
# プロジェクト固有のイベント（動画公開・SNS配信等）は config ファイル側に追記し、
# 本体コードにドメイン固有語を持ち込まない。
_PUBLISH_EVENTS_CONFIG = Path(__file__).resolve().parent.parent / "config" / "publish_events.yaml"

_DEFAULT_PUBLISH_EVENTS = {
    "published": {
        "emoji": "🎉",
        "header": "公開完了",
        "message": "{mention}公開が完了しました。",
        "action_label": None,
        "action_url": None,
        "fyi": False,
    },
}


def _load_publish_events() -> dict:
    """publish イベント定義を config/publish_events.yaml から読み込む。

    ファイル未配置・読み込み失敗時は汎用の既定イベント（`published` のみ）で動作する。
    """
    if not _PUBLISH_EVENTS_CONFIG.exists():
        return dict(_DEFAULT_PUBLISH_EVENTS)
    try:
        with open(_PUBLISH_EVENTS_CONFIG, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        events = data.get("events") or {}
        if not isinstance(events, dict):
            raise ValueError(f"events は dict である必要があります（実際: {type(events).__name__}）")
    except (yaml.YAMLError, OSError, AttributeError, ValueError) as e:
        print(f"WARNING: config/publish_events.yaml の読み込みに失敗（既定イベントで続行）: {e}", file=sys.stderr)
        return dict(_DEFAULT_PUBLISH_EVENTS)
    return events if events else dict(_DEFAULT_PUBLISH_EVENTS)


_PUBLISH_EVENTS = _load_publish_events()
# fyi: true のイベント（ユーザー操作不要の完了報告）→ @mention しない。
# それ以外は確認・節目アクション（A-2 相当）のため @mention を維持する。
_PUBLISH_FYI_EVENTS = {name for name, cfg in _PUBLISH_EVENTS.items() if cfg.get("fyi")}


def _get_mention_text() -> str:
    """SLACK_MENTION_USER_ID からメンションテキストを生成する"""
    mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
    return f"<@{mention_user_id}> " if mention_user_id else ""


def _api_call(method: str, payload: dict) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("Error: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return {"ok": False, "error": "token_missing"}

    url = f"{SLACK_API_BASE}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    import time
    last_err = None
    for attempt in range(3):  # 一時障害向けに最大3回・指数バックオフ（2s, 4s）
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as e:
            last_err = e
            # 4xx クライアントエラーはリトライしても回復しないため即座に中断する
            if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500:
                break
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
    print(f"Error: Slack API call failed after retries: {last_err}", file=sys.stderr)
    return {"ok": False, "error": str(last_err)}


def post_message(channel: str, text: str, blocks: list = None) -> dict:
    payload = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    return _api_call("chat.postMessage", payload)


# --- ヘルパー ---

def _now() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M") + " JST"


def _jst_hour() -> int:
    """現在の JST 時（0〜23）を返す。routine-idle のスロット判定用。"""
    return datetime.now(timezone(timedelta(hours=9))).hour


def in_idle_window(hour: int, window_start: int, window_end: int) -> bool:
    """hour が [window_start, window_end) の JST スロットに入るか判定する（日跨ぎ対応）。

    routine-idle を「1 日 1 回のナッジ」に抑えるためのゲート。R-1 は 4 時間ごと発火するため、
    朝枠（既定 08:00〜10:00 JST）にだけ通知することで、fresh session でも状態ファイルなしに
    実質日次のアイドル通知を実現する（±30 分ジッターがあっても該当スロットは 1 つだけ）。
    window_start == window_end は「常時許可」とみなす（--force 相当の全時間帯送信）。
    """
    if window_start == window_end:
        return True
    if window_start < window_end:
        return window_start <= hour < window_end
    # 日跨ぎ（例: 22 → 2）
    return hour >= window_start or hour < window_end


def _branch_to_label(branch: str) -> str:
    """ブランチ名から人間が読みやすいラベルを生成する"""
    label = re.sub(r"^(claude|content|feat|fix|docs)/", "", branch)
    # 末尾のセッションIDらしき英数字列（5文字以上）を除去
    label = re.sub(r"-[A-Za-z0-9]{5,}$", "", label)
    label = label.replace("-", " ").strip()
    return label or branch


def _build_context_links(
    issue_title: str, issue_url: str, pr_title: str, pr_url: str
) -> list:
    """Issue / PR リンクブロックを生成（どちらか一方でも存在すれば返す）"""
    blocks = []
    if not (issue_title or issue_url or pr_title or pr_url):
        return blocks

    fields = []
    if issue_title:
        label = issue_title if len(issue_title) <= 60 else issue_title[:57] + "…"
        value = f"<{issue_url}|{label}>" if issue_url else label
        fields.append({"type": "mrkdwn", "text": f"*関連Issue:*\n{value}"})
    if pr_title:
        label = pr_title if len(pr_title) <= 60 else pr_title[:57] + "…"
        value = f"<{pr_url}|{label}>" if pr_url else label
        fields.append({"type": "mrkdwn", "text": f"*関連PR:*\n{value}"})

    if fields:
        blocks.append({"type": "section", "fields": fields})

    buttons = []
    if issue_url:
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Issue を開く 🔗", "emoji": True},
            "url": issue_url,
        })
    if pr_url:
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "PR を開く 🔗", "emoji": True},
            "url": pr_url,
            "style": "primary",
        })
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})

    return blocks


# --- Block Kit builders ---

def build_session_start_blocks(
    branch: str,
    issue_title: str = "",
    issue_url: str = "",
    pr_title: str = "",
    pr_url: str = "",
) -> list:
    task_label = issue_title or _branch_to_label(branch)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🐹 Claude Code セッション開始", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*タスク:* {task_label}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ブランチ:*\n`{branch}`"},
                {"type": "mrkdwn", "text": f"*開始時刻:*\n{_now()}"},
            ],
        },
    ]

    blocks.extend(_build_context_links(issue_title, issue_url, pr_title, pr_url))

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": "このスレッドに返信するとClaude Codeが読み取ります 💬"},
        ],
    })
    return blocks


def build_session_stop_blocks(
    branch: str,
    summary: str,
    issue_title: str = "",
    issue_url: str = "",
    pr_title: str = "",
    pr_url: str = "",
    cost_summary: str = "",
) -> list:
    task_label = issue_title or _branch_to_label(branch)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "✅ Claude Code セッション終了", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*タスク:* {task_label}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ブランチ:*\n`{branch}`"},
                {"type": "mrkdwn", "text": f"*終了時刻:*\n{_now()}"},
            ],
        },
    ]

    blocks.extend(_build_context_links(issue_title, issue_url, pr_title, pr_url))

    if summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*作業サマリー（直近のコミット）:*\n{summary}"},
        })

    # 日次コストサマリー（#1213）
    if cost_summary:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": cost_summary}],
        })

    blocks.append({"type": "divider"})
    return blocks


# 完了報告アウトカムのプロセス的文言（completion-report-rules.md / L-052・L-076・L-102）。
# これらが [完了] 通知の --outcome に含まれていると「何をしたか（プロセス）」報告になり、
# 「初回指示で何ができるようになったか（結果）」が欠落する。
_PROCESS_OUTCOME_PATTERNS = re.compile(
    r"マージ(し|済|完了)|merged?|squash|rebase|PR\s*#?\d|指摘\s*\d|レビュー往復|"
    r"修正サイクル|を更新した|を修正した|を追加した|を変更した|コミット\s*\d",
    re.IGNORECASE,
)


def check_completion_outcome(pr_title: str, outcome: str) -> str:
    """[完了] 通知のアウトカムを検査し、問題があれば警告文字列を返す（問題なければ ""）。

    ガードレール（completion-report-rules.md の機械実装）:
      - [完了] なのに --outcome が空 → 初回指示アウトカム欠落
      - --outcome がプロセス的文言（マージ手順・レビュー往復・指摘件数等）を含む → 結果でなく過程の報告
    非致命（通知自体は送る）。stderr に出して Claude / 実行ログに気付かせる。
    """
    if not pr_title.startswith("[完了]"):
        return ""
    stripped = (outcome or "").strip()
    if not stripped:
        return (
            "⚠️ [完了] 通知に --outcome がありません。completion-report-rules.md に従い、"
            "「初回指示で何ができるようになったか」を1文で指定してください。"
        )
    if _PROCESS_OUTCOME_PATTERNS.search(stripped):
        return (
            "⚠️ --outcome がプロセス的文言（マージ手順・レビュー往復・指摘件数等）を含みます。"
            "completion-report-rules.md（L-052/L-076/L-102）に従い、"
            "「何をしたか」ではなく「初回指示で何ができるようになったか（結果）」を書いてください。"
        )
    return ""


def build_pr_blocks(pr_url: str, pr_title: str, branch: str, outcome: str = "") -> list:
    is_complete = pr_title.startswith("[完了]")
    header_text = "✅ マージ完了" if is_complete else "📋 PR作成 — レビュー & Approveをお願いします"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{pr_title}*"},
        },
    ]
    if outcome:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*アウトカム:* {outcome}"},
        })
    blocks.extend([
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ブランチ:*\n`{branch}`"},
                {"type": "mrkdwn", "text": f"*PR URL:*\n{pr_url}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "PRを開く 🔗", "emoji": True},
                    "url": pr_url,
                    "style": "primary",
                }
            ],
        },
    ])
    return blocks


def _load_triage():
    """tools/triage_notification.py を import する（同ディレクトリ・sys.path はモジュール読み込み時に追加済み）"""
    from triage_notification import triage_items, classify_item  # type: ignore
    return triage_items, classify_item


def _resolve_route_mention(
    texts: list,
    *,
    force_mention: bool = False,
    no_mention: bool = False,
    baseline_mention: bool = True,
) -> tuple:
    """通知経路の `@mention` 可否を機械トリアージで決める（`user-notification-triage.md` §2・#64）。

    `waiting` 経路と同じ判定器（`triage_notification.classify_item`）を approval / publish 経路にも
    通すための共通ヘルパー。判定は次の優先順で決まる（上が強い）。

    1. `--no-mention` / `--force-mention` の明示指定
    2. トリアージが A-1〜A-6 を検出 → **経路の既定が FYI でも昇格して `@mention`**
       （例: fyi イベントの detail に課金枯渇が書かれていれば A-6 として通知する）
    3. 経路の既定が FYI（`baseline_mention=False`）→ 抑制
    4. 障害（バグ・エラー・失敗）起因かつ A 非該当 → 抑制（RC-1・L-077 で Claude が自律修正する）
    5. それ以外は経路の既定に従う

    Args:
        texts: 判定対象テキストの断片（None・空文字は無視して結合する）
        force_mention: True なら判定結果より優先して `@mention` する
        no_mention: True なら判定結果より優先して `@mention` しない（force より強い）
        baseline_mention: 経路側の既定。publish の FYI イベント・approval（PR 自律化済みで
            それ自体は A 区分ではない）は False を渡す

    Returns:
        (mention_text, reason): `mention_text` は空文字なら `@mention` なし
    """
    joined = "\n".join(t for t in texts if t and t.strip())
    triage_fn, _ = _load_triage()
    triage = triage_fn([{"text": joined, "labels": []}] if joined else [])

    if no_mention:
        allow, reason = False, "--no-mention による明示的な抑制"
    elif force_mention:
        allow, reason = True, "--force-mention による明示的な上書き"
    elif triage["mention"]:
        boundaries = ", ".join(r["boundary"] for r in triage["a_items"] if r.get("boundary"))
        allow, reason = True, f"トリアージが A 区分と判定（{boundaries or 'A'}）"
    elif not baseline_mention:
        allow, reason = False, "経路の既定が @mention なし（FYI 相当・ユーザー操作不要）のため抑制"
    elif any(r.get("is_failure") for r in triage["results"]):
        allow, reason = False, "障害（バグ・エラー・失敗）起因かつ A 非該当のため抑制（L-077 で自律修正）"
    else:
        allow, reason = True, "経路の既定（要確認・節目アクション）に従い @mention"

    return (_get_mention_text() if allow else ""), reason


def build_waiting_user_blocks(issues: list, branch: str, triage: dict = None) -> list:
    """ユーザー対応待ち通知ブロック。triage 指定時は A 区分（A-1〜A-6）の境界も併記する。"""
    if triage and triage.get("a_items"):
        lines = []
        for r in triage["a_items"]:
            bd = f"（{r['boundary']}）" if r.get("boundary") else ""
            lines.append(f"• {r['text']} {bd}")
        issues_text = "\n".join(lines)
    else:
        issues_text = "\n".join(f"• {t}" for t in issues) if issues else "確認が必要なタスクがあります"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⏳ ユーザーのアクションが必要です",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": issues_text},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ブランチ:*\n`{branch}`"},
                {"type": "mrkdwn", "text": f"*時刻:*\n{_now()}"},
            ],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "ℹ️ この通知は A-1〜A-6（ユーザーの最終判断・操作が物理的に必要な項目）に絞り込まれています。"
                        "各項目には「あなたが取るべき具体的アクション」が含まれているはずです。"
                        "技術的な障害・バグは Claude が自律修正するため、ここには表示されません。",
            }],
        },
    ]
    return blocks


def build_approval_request_blocks(
    summary: str,
    branch: str,
    issue_url: str = "",
    issue_title: str = "",
    *,
    mention_text: str,
) -> list:
    """PR作成前のユーザー承認依頼通知

    Args:
        mention_text: 呼び出し側がトリアージで解決した `@mention` 文字列（`_resolve_route_mention`）。
            `@mention` 判定を本関数に持たせない（判定源を 1 つにする・#64）ため必須引数にしている。
    """

    summary_lines = summary.splitlines() if summary else ["実装が完了しました"]
    summary_text = "\n".join(f"• {line.strip()}" for line in summary_lines if line.strip())
    if not summary_text:
        summary_text = "• 実装が完了しました"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔔 実装完了 — PR作成前の承認をお願いします",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{mention_text}実装が完了しました。内容を確認してPR作成の承認をお願いします。",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*実装内容:*\n{summary_text}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ブランチ:*\n`{branch}`"},
                {"type": "mrkdwn", "text": f"*時刻:*\n{_now()}"},
            ],
        },
    ]

    if issue_url:
        label = issue_title if issue_title else "関連Issue"
        label = label if len(label) <= 60 else label[:57] + "…"
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"{label} を開く 🔗", "emoji": True},
                    "url": issue_url,
                }
            ],
        })

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "✅ *承認方法*: このスレッドに `OK` / `LGTM` / `承認` などのキーワードで返信してください（Claude Codeが自動検出します）",
        },
    })
    return blocks


def build_progress_report_blocks(summary: str, adjustments: str = "", mention_text: str = "") -> list:
    """日次進捗レポート通知（オーナーメンション付き）"""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📊 日次進捗レポート",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{mention_text}本日の進捗をお知らせします。",
            },
        },
    ]

    if summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📹 各動画の進捗:*\n{summary}"},
        })

    if adjustments:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🔧 Issue調整内容:*\n{adjustments}"},
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"実行時刻: {_now()}"}],
    })

    return blocks


def build_pipeline_blocks(
    pipeline: str, video_id: str, result: str, success: bool, duration: str = ""
) -> list:
    emoji = "✅" if success else "❌"
    fields = [{"type": "mrkdwn", "text": f"*動画ID:*\n`{video_id}`"}]
    if duration:
        fields.append({"type": "mrkdwn", "text": f"*所要時間:*\n{duration}"})
    fields.append({"type": "mrkdwn", "text": f"*時刻:*\n{_now()}"})
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {pipeline}パイプライン",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": result},
        },
        {
            "type": "section",
            "fields": fields,
        },
    ]


def _resolve_action_url(template, url: str):
    """action_url テンプレートを解決する。`{url}` を含む場合は --url の値を埋め込む。"""
    if not template:
        return None
    if "{url}" in template:
        if not url:
            return None
        try:
            return template.format(url=url)
        except (KeyError, IndexError):
            # {url} 以外の未知プレースホルダが混在していてもクラッシュせず未展開のまま返す。
            return template
    return template


def build_publish_blocks(
    event_type: str,
    video_id: str = "",
    title: str = "",
    url: str = "",
    detail: str = "",
    *,
    mention_text: str,
) -> list:
    """公開・配信完了通知（config/publish_events.yaml 駆動・#358）。

    Args:
        mention_text: 呼び出し側がトリアージで解決した `@mention` 文字列（`_resolve_route_mention`）。
            `fyi: true` のイベントを抑制する判定も呼び出し側（`baseline_mention`）が持つので、
            本関数は `@mention` の有無を判定しない（判定源を 1 つにする・#64）。
    """
    now_str = _now()

    raw_cfg = _PUBLISH_EVENTS.get(event_type)
    if raw_cfg is None:
        cfg = {
            "emoji": "🔔",
            "header": f"公開通知: {event_type}",
            "message": f"{mention_text}公開通知: {event_type}",
            "action_label": None,
            "action_url": url or None,
        }
    else:
        # `raw_cfg.get(k, default)` は config 側で明示的に `null` が書かれた場合は
        # default を素通りしてしまう（get の default はキー欠落時のみ効く）ため `or` で補う。
        message_tmpl = raw_cfg.get("message") or ("{mention}公開通知: " + event_type)
        try:
            message = message_tmpl.format(mention=mention_text)
        except (KeyError, IndexError):
            # config 側が {mention} 以外の未知プレースホルダを書いた場合はクラッシュせず
            # 未展開のまま表示する（通知欠落よりは崩れた表示の方が実害が小さい）。
            message = message_tmpl
        cfg = {
            "emoji": raw_cfg.get("emoji") or "🔔",
            "header": raw_cfg.get("header") or event_type,
            "message": message,
            "action_label": raw_cfg.get("action_label"),
            "action_url": _resolve_action_url(raw_cfg.get("action_url"), url),
        }

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{cfg['emoji']} {cfg['header']}", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": cfg["message"]},
        },
    ]

    fields = []
    if video_id:
        fields.append({"type": "mrkdwn", "text": f"*ID:* `{video_id}`"})
    if title:
        fields.append({"type": "mrkdwn", "text": f"*タイトル:* {title}"})
    if url and url != cfg.get("action_url"):
        fields.append({"type": "mrkdwn", "text": f"*URL:* {url}"})
    fields.append({"type": "mrkdwn", "text": f"*実行時刻:* {now_str}"})
    if fields:
        blocks.append({"type": "section", "fields": fields})

    if detail:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"_{detail}_"},
        })

    action_url = cfg.get("action_url")
    action_label = cfg.get("action_label")
    if action_url and action_label:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": action_label, "emoji": True},
                    "url": action_url,
                    "style": "primary",
                }
            ],
        })

    return blocks


def build_daily_progress_blocks(summary: str, action_items: str = "", analytics: str = "", youtube_status: str = "", no_mention: bool = False) -> list:
    """日次進捗報告通知（SLACK_PUBLISH_CHANNEL_ID に送信）

    Args:
        summary: 制作中の動画一覧テキスト
        action_items: 要対応項目テキスト（A-1〜A-6 該当の真の要対応のみ）
        analytics: KPI サマリーテキスト（省略可・プロジェクト定義）
        youtube_status: meta.yaml 由来の YouTube 公開状況テキスト（省略可）
        no_mention: True の場合 @mention を付けない（真の要対応がゼロのとき・情報提供のみ）
    """
    mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
    if no_mention:
        lead = "本日の進捗をお知らせします。（要対応はありません・情報提供のみ）"
    else:
        mention_text = f"<@{mention_user_id}> " if mention_user_id else ""
        lead = f"{mention_text}本日の進捗です。下記に *要対応* があります。"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📊 日次進捗レポート", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": lead},
        },
    ]

    if analytics:
        analytics_text = analytics[:2900] + "..." if len(analytics) > 2900 else analytics
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📈 KPI:*\n{analytics_text}"},
        })

    # YouTube 公開状況（meta.yaml より取得・--sync-published 後の正確な状態）
    if youtube_status:
        yt_text = youtube_status[:2900] + "..." if len(youtube_status) > 2900 else youtube_status
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📺 YouTube公開状況:*\n{yt_text}"},
        })

    if summary:
        # Slack のブロックサイズ制限対策: 長すぎる場合は切り詰め
        summary_text = summary[:2900] + "..." if len(summary) > 2900 else summary
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📹 パイプライン進捗:*\n{summary_text}"},
        })

    if action_items:
        action_text = action_items[:2900] + "..." if len(action_items) > 2900 else action_items
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚠️ 要対応:*\n{action_text}"},
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"実行時刻: {_now()}"}],
    })

    return blocks


def build_half_day_summary_blocks(summary: str, action_items: str = "", period_label: str = "", no_mention: bool = False) -> list:
    """半日アウトカムサマリー通知（SLACK_PUBLISH_CHANNEL_ID に送信）

    ユーザーが「半日〜1日に1回」で自律稼働とPR消化を把握するための集約通知。
    セッション単位の session-start/session-stop 通知を廃止した代替（Issue #2597）。
    要対応がゼロでも必ず1通送る（生存確認＝ハートビート）。

    Args:
        summary: 直近約12時間のアウトカム本文（PR消化・パイプライン進捗・SNS・コスト等・呼び出し側で組み立て済み）
        action_items: 要対応項目テキスト（A-1〜A-6 該当の真の要対応のみ）
        period_label: 集計期間ラベル（例: "前日19:00〜本日07:00"）
        no_mention: True の場合 @mention を付けない（真の要対応がゼロのとき・情報提供のみ）
    """
    mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
    if no_mention or not action_items:
        lead = "半日ぶんの活動サマリーです。（要対応はありません・自律稼働中）"
    else:
        mention_text = f"<@{mention_user_id}> " if mention_user_id else ""
        lead = f"{mention_text}半日ぶんの活動サマリーです。下記に *要対応* があります。"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🐾 半日アウトカムサマリー", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": lead},
        },
    ]

    if summary:
        summary_text = summary[:2900] + "..." if len(summary) > 2900 else summary
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        })

    if action_items:
        action_text = action_items[:2900] + "..." if len(action_items) > 2900 else action_items
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚠️ 要対応:*\n{action_text}"},
        })

    ctx = f"期間: {period_label} | 実行時刻: {_now()}" if period_label else f"実行時刻: {_now()}"
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": ctx}],
    })

    return blocks


def build_routine_idle_blocks(routine_name: str = "", detail: str = "") -> list:
    """ルーティンのアイドル通知（消化対象ゼロ = バックログ空）。

    ルーティン（例: R-1 改善Issue消化スプリント）が「消化すべき Issue がない」状態を検知したときに、
    メインチャンネル（SLACK_CHANNEL_ID）へ FYI として送る。ユーザーがルーティンを維持するか停止するかを
    判断するための情報通知であり、A-1〜A-6（不可逆リスク）ではないため @mention しない
    （user-notification-triage.md）。

    Args:
        routine_name: ルーティン名（例: "改善Issue消化スプリント"）。表示用。
        detail: バックログ内訳などの補足テキスト（省略可）。
    """
    label = routine_name or "定期ルーティン"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🌙 ルーティン: 消化対象なし（アイドル）", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{label}* が起動しましたが、消化すべき Issue がありませんでした"
                    "（バックログが空・オープン PR も対応不要）。\n"
                    "ルーティンを *このまま維持* するか *停止* するかをご判断ください。"
                ),
            },
        },
    ]

    if detail:
        detail_text = detail[:2900] + "..." if len(detail) > 2900 else detail
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*バックログ状況:*\n{detail_text}"},
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                "ℹ️ これは要対応（@mention）ではなく判断支援の FYI です。"
                "維持しても no-op 1 回分のコストのみ。停止・頻度変更は `docs/routines.md` の運用メモを参照。"
                f" | 実行時刻: {_now()}"
            ),
        }],
    })
    return blocks


# ──────────────────────────────────────────────
# セルフテスト（#64: 経路別 @mention 判定の回帰ガード）
# ──────────────────────────────────────────────

# (説明, texts, _resolve_route_mention への kwargs, 期待する @mention 有無)
_MENTION_SELF_TEST_CASES = [
    # approval 経路（PR 自律化済みのため経路の既定は FYI）
    ("approval: 通常の実装完了は A 非該当で抑制",
     ["台本v1生成完了", "feat: 音声生成を実装"], {"baseline_mention": False}, False),
    ("approval: 課金枯渇は A-6 として昇格",
     ["API クレジットが枯渇したのでチャージをお願いします"], {"baseline_mention": False}, True),
    ("approval: --force-mention は判定を上書きする",
     ["台本v1生成完了"], {"baseline_mention": False, "force_mention": True}, True),
    ("approval: --no-mention は --force-mention より強い",
     ["クレジット枯渇"], {"baseline_mention": False, "force_mention": True, "no_mention": True}, False),
    ("approval: --no-mention 単独でも A 区分を抑制する",
     ["クレジット枯渇"], {"baseline_mention": False, "no_mention": True}, False),
    # publish 経路（config の fyi フラグが経路の既定を決める）
    ("publish 非 FYI: 経路の既定（確認・節目アクション）に従い @mention",
     ["公開前確認", "コンテンツタイトル"], {"baseline_mention": True}, True),
    ("publish 非 FYI: 障害起因は降格して抑制",
     ["公開前確認", "公開に失敗しました（ValueError）"], {"baseline_mention": True}, False),
    ("publish FYI: 完了報告は抑制",
     ["公開完了", "コンテンツタイトル"], {"baseline_mention": False}, False),
    ("publish FYI: A-6 を検出したら昇格",
     ["公開完了", "請求上限に達したため以降の公開を停止しました"], {"baseline_mention": False}, True),
    # A 検出（②）は障害降格（④）より強い = 障害語を含む A-6 でも昇格する（順序の回帰ガード）
    ("publish FYI: 障害語を含む A-6 でも昇格（A 判定が障害降格より優先）",
     ["公開完了", "公開が停止しました。請求上限に達しています"], {"baseline_mention": False}, True),
    ("判定材料が無いときは経路の既定に従う",
     [], {"baseline_mention": True}, True),
    ("判定材料が無く経路の既定が FYI なら抑制",
     [], {"baseline_mention": False}, False),
]


def run_self_test() -> int:
    """経路別 `@mention` 判定のセルフテスト（Slack API へは一切アクセスしない・#64）。"""
    passed, failed = 0, 0

    def report(ok: bool, name: str, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")

    prev_user_id = os.environ.get("SLACK_MENTION_USER_ID")
    os.environ["SLACK_MENTION_USER_ID"] = "UTESTUSER"
    try:
        for name, texts, kwargs, expect in _MENTION_SELF_TEST_CASES:
            mention, reason = _resolve_route_mention(texts, **kwargs)
            report(bool(mention) == expect, name,
                   f"expected mention={expect} got={bool(mention)}（{reason}）")

        # ブロックビルダーは呼び出し側が解決した mention_text をそのまま使う（二重判定を持たない）
        approval_blocks = json.dumps(
            build_approval_request_blocks("実装完了", "feat/x", mention_text=""),
            ensure_ascii=False,
        )
        report("<@" not in approval_blocks, "approval ブロックは mention_text='' を尊重する")

        publish_blocks = json.dumps(
            build_publish_blocks("published", title="t", mention_text="<@UTESTUSER> "),
            ensure_ascii=False,
        )
        report("<@UTESTUSER>" in publish_blocks, "publish ブロックは渡された mention_text を使う")

        # SLACK_MENTION_USER_ID 未設定なら A 区分でも @mention 文字列は空になる
        del os.environ["SLACK_MENTION_USER_ID"]
        mention, _ = _resolve_route_mention(["クレジットが枯渇したのでチャージをお願いします"])
        report(mention == "", "SLACK_MENTION_USER_ID 未設定なら A 区分でも空文字")
    finally:
        if prev_user_id is None:
            os.environ.pop("SLACK_MENTION_USER_ID", None)
        else:
            os.environ["SLACK_MENTION_USER_ID"] = prev_user_id

    total = passed + failed
    print(f"\nセルフテスト: {passed} passed, {failed} failed / {total} cases")
    return 0 if failed == 0 else 1


# --- CLI ---

def main():
    # `type` は必須の位置引数なので、セルフテストは parser を組む前に引き取る
    # （`python3 tools/slack_notify.py --self-test` だけで実行できるようにする）。
    # 判定は **第 1 引数のみ** に限定する。argv 全体のメンバーシップで見ると
    # `--summary "--self-test"` のように値として渡った場合に通知送信が黙ってテスト実行へ
    # 迂回し、呼び出し元が exit 0 を「送信成功」と誤認する（通知の欠落・#64 Layer 1 指摘）。
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(run_self_test())

    parser = argparse.ArgumentParser(description="Slack通知ユーティリティ")
    parser.add_argument(
        "type",
        choices=["session-start", "session-stop", "pr", "waiting", "pipeline", "message", "approval", "progress", "publish", "daily-progress", "half-day-summary", "comment-approval", "chart", "routine-idle"],
    )
    parser.add_argument("--channel", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--summary", default="")
    # PR 情報（pr タイプおよび session-start/stop の関連PR表示に共用）
    parser.add_argument("--pr-url", default="")
    parser.add_argument("--pr-title", default="")
    parser.add_argument("--outcome", default="", help="pr タイプ用: ユーザー視点のアウトカム1文（何ができるようになったか）")
    # Issue 情報（session-start/stop 用）
    parser.add_argument("--issue-url", default="")
    parser.add_argument("--issue-title", default="")
    # コスト情報（session-stop タイプ用: calc_daily_cost.py のサマリー文字列）
    parser.add_argument("--cost-summary", default="", help="session-stop タイプ用: 日次コストサマリー文字列（calc_daily_cost.py --summary-only の出力）")
    # その他
    parser.add_argument("--issues", nargs="*", default=[])
    parser.add_argument(
        "--force-mention",
        action="store_true",
        help="waiting / approval / publish タイプ用: トリアージで A 区分なしでも強制的に @mention する（既定はトリアージ判定に従う）",
    )
    parser.add_argument("--pipeline", default="")
    parser.add_argument("--video-id", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--duration", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--adjustments", default="")  # progress タイプ用: Issue調整内容
    parser.add_argument(
        "--event-type",
        dest="event_type",
        help="publish タイプ用: 公開イベント種別（config/publish_events.yaml のキー。未設定時は 'published' のみ利用可）",
    )
    parser.add_argument("--title", default="")   # publish タイプ用: 動画タイトル
    parser.add_argument("--url", default="")     # publish タイプ用: 公開URL
    parser.add_argument("--detail", default="")  # publish タイプ用: 補足情報
    parser.add_argument("--action-items", default="")        # daily-progress タイプ用: 要対応項目
    parser.add_argument("--analytics", default="")           # daily-progress タイプ用: YouTube/SNS KPI サマリー
    parser.add_argument("--youtube-status", default="")      # daily-progress タイプ用: YouTube公開状況（meta.yaml由来）
    parser.add_argument("--no-mention", action="store_true", help="daily-progress / half-day-summary / approval / publish タイプ用: 真の要対応がゼロのとき @mention を付けない（トリアージ判定より優先）")
    parser.add_argument("--period-label", default="", help="half-day-summary タイプ用: 集計期間ラベル（例: 前日19:00〜本日07:00）")
    # comment-approval タイプ用
    parser.add_argument("--platform-name", default="")  # comment-approval: プラットフォーム名
    parser.add_argument("--priority", default="low", help="comment-approval: 優先度 (critical/high/medium/low)")
    parser.add_argument("--category-name", default="", help="comment-approval: カテゴリ名（表示用）")
    parser.add_argument("--comment-text", default="")  # comment-approval: 元コメントテキスト
    parser.add_argument("--reply-text", default="")  # comment-approval: 返信案テキスト
    parser.add_argument("--issue-number", default="")  # comment-approval: Issue 番号
    parser.add_argument("--batch-summary", default="")  # comment-approval: バッチ承認サマリー
    # chart タイプ用: 公開アクセス可能な画像 URL（R2 公開ドメイン等）を image block で表示する。
    # Slack の files:write スコープが無くても chat:write だけで画像を表示できる経路。
    parser.add_argument("--image-url", default="", help="chart タイプ用: 公開画像 URL（R2 公開ドメイン等）")
    parser.add_argument("--alt-text", default="グラフ", help="chart タイプ用: 画像 alt テキスト")
    # routine-idle タイプ用
    parser.add_argument("--routine-name", default="", help="routine-idle タイプ用: ルーティン名（表示用）")
    parser.add_argument("--idle-window-start", type=int, default=8, choices=range(24), help="routine-idle タイプ用: 送信を許可する JST 開始時（0〜23・既定 8 = 08:00）")
    parser.add_argument("--idle-window-end", type=int, default=10, choices=range(24), help="routine-idle タイプ用: 送信を許可する JST 終了時（0〜23・排他・既定 10 = 10:00 未満）")
    parser.add_argument("--force", action="store_true", help="routine-idle タイプ用: JST スロット判定を無視して強制送信する")
    args = parser.parse_args()

    # publish タイプでは --event-type を必須とする（値は config/publish_events.yaml のキー駆動）
    if args.type == "publish" and not args.event_type:
        parser.error("--event-type is required when type is 'publish'")
    if args.type == "publish" and args.event_type not in _PUBLISH_EVENTS:
        available = ", ".join(sorted(_PUBLISH_EVENTS)) or "(なし)"
        parser.error(
            f"--event-type '{args.event_type}' は config/publish_events.yaml に未定義です。"
            f"利用可能: {available}"
        )

    # --channel 未指定の場合、通知タイプに応じてチャンネルを自動選択
    # approval / waiting は SLACK_APPROVAL_CHANNEL_ID を優先（未設定時は SLACK_CHANNEL_ID にフォールバック）
    # publish は SLACK_PUBLISH_CHANNEL_ID を優先（未設定時は SLACK_APPROVAL_CHANNEL_ID → SLACK_CHANNEL_ID にフォールバック）
    if not args.channel:
        main_channel_id = os.environ.get("SLACK_CHANNEL_ID", "")
        approval_channel_id = os.environ.get("SLACK_APPROVAL_CHANNEL_ID", main_channel_id)
        if args.type in ("approval", "waiting", "comment-approval"):
            args.channel = approval_channel_id
        elif args.type in ("publish", "daily-progress", "half-day-summary"):
            args.channel = os.environ.get("SLACK_PUBLISH_CHANNEL_ID", approval_channel_id)
        else:
            args.channel = main_channel_id

    if not args.channel:
        print("エラー: 送信先チャンネルを特定できませんでした。環境変数 SLACK_CHANNEL_ID (および approval/waiting 通知の場合は SLACK_APPROVAL_CHANNEL_ID) が設定されているか、--channel オプションで指定されているか確認してください。", file=sys.stderr)
        sys.exit(1)

    blocks = None
    text = ""

    if args.type == "session-start":
        blocks = build_session_start_blocks(
            args.branch,
            issue_title=args.issue_title,
            issue_url=args.issue_url,
            pr_title=args.pr_title,
            pr_url=args.pr_url,
        )
        task = args.issue_title or _branch_to_label(args.branch)
        text = f"🐹 Claude Code セッション開始 — {task}"

    elif args.type == "session-stop":
        blocks = build_session_stop_blocks(
            args.branch,
            args.summary,
            issue_title=args.issue_title,
            issue_url=args.issue_url,
            pr_title=args.pr_title,
            pr_url=args.pr_url,
            cost_summary=args.cost_summary,
        )
        task = args.issue_title or _branch_to_label(args.branch)
        text = f"✅ Claude Code セッション終了 — {task}"

    elif args.type == "pr":
        _outcome_warning = check_completion_outcome(args.pr_title, args.outcome)
        if _outcome_warning:
            print(_outcome_warning, file=sys.stderr)
        blocks = build_pr_blocks(args.pr_url, args.pr_title, args.branch, args.outcome)
        is_complete = args.pr_title.startswith("[完了]")
        text = f"✅ マージ完了: {args.pr_title}" if is_complete else f"📋 PR作成: {args.pr_title}"

    elif args.type == "waiting":
        # トリアージゲート: A-1〜A-6（既約境界外）に該当する項目だけを @mention する。
        # 障害（バグ・エラー）起因や B/C/D 区分は自律処理対象のため @mention しない（CP-6・L-077）。
        triage_items, _ = _load_triage()
        triage = triage_items([{"text": t, "labels": []} for t in args.issues])

        if not triage["mention"] and not args.force_mention:
            # A 区分なし → @mention を抑制。情報はメインチャンネルへ FYI 降格（記録は残すが ping しない）。
            main_ch = os.environ.get("SLACK_CHANNEL_ID", "")
            non_a = triage["non_a_items"]
            summary = "\n".join(
                f"• {r['text']}  → *{r['action_class']}区分*（自律処理）" for r in non_a
            ) or "（項目なし）"
            if main_ch and non_a:
                fyi_blocks = [
                    {"type": "header", "text": {"type": "plain_text", "text": "🤖 自律処理項目（ユーザー対応は不要）", "emoji": True}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
                    {"type": "context", "elements": [{
                        "type": "mrkdwn",
                        "text": "トリアージ: A-1〜A-6 に非該当のため @mention を抑制。Claude が自律処理します（要対応ではありません）。",
                    }]},
                ]
                post_message(main_ch, "🤖 自律処理項目（ユーザー対応不要）", fyi_blocks)
            print(
                "INFO: A区分（A-1〜A-6）該当なし。@mention を抑制し、自律処理対象としてメインチャンネルへ FYI 降格しました。"
                "（強制 @mention するには --force-mention）",
                file=sys.stderr,
            )
            sys.exit(0)

        blocks = build_waiting_user_blocks(args.issues, args.branch, triage=triage)
        mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
        mention = f"<@{mention_user_id}> " if mention_user_id else ""
        text = f"{mention}⏳ ユーザーのアクションが必要です"

    elif args.type == "pipeline":
        pipeline_success = (
            "完了" in args.result or "成功" in args.result or "OK" in args.result
        )
        blocks = build_pipeline_blocks(
            args.pipeline, args.video_id, args.result, pipeline_success, args.duration
        )
        text = f"{args.pipeline}パイプライン: {args.result}"

    elif args.type == "message":
        text = args.text

    elif args.type == "approval":
        # トリアージゲート（#64）: approval も waiting と同じ判定器を通す。
        # PR 作成・マージは本プロジェクトでは自律対象（恒久委任・CLAUDE.md）なので経路の既定は
        # FYI（baseline_mention=False）とし、本文が A-1〜A-6 に該当するときだけ @mention へ昇格する。
        # waiting 経路と違いチャンネルは切り替えない（承認スレッドでの OK/LGTM 検出動線を壊さないため、
        # 通知自体は承認チャンネルに残して @mention だけを抑制する）。
        mention, _mention_reason = _resolve_route_mention(
            [args.summary, args.issue_title],
            force_mention=args.force_mention,
            no_mention=args.no_mention,
            baseline_mention=False,
        )
        if not mention:
            print(
                f"INFO: approval 通知の @mention を抑制しました（{_mention_reason}）。"
                "強制するには --force-mention。",
                file=sys.stderr,
            )
        blocks = build_approval_request_blocks(
            args.summary,
            args.branch,
            issue_url=args.issue_url,
            issue_title=args.issue_title,
            mention_text=mention,
        )
        text = f"{mention}🔔 実装完了 — PR作成前の承認をお願いします（ブランチ: {args.branch}）"

    elif args.type == "progress":
        mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
        mention = f"<@{mention_user_id}> " if mention_user_id else ""
        blocks = build_progress_report_blocks(args.summary, args.adjustments, mention_text=mention)
        text = f"{mention}📊 日次進捗レポート"

    elif args.type == "publish":
        # トリアージゲート（#64）: FYI イベントは従来どおり無 @mention。非 FYI イベントは
        # config の宣言（確認・節目アクション = A-2 相当）を経路の既定として尊重しつつ、
        # 障害起因（公開失敗等）なら降格し、A-1〜A-6 を検出したら FYI イベントでも昇格する。
        label = _PUBLISH_EVENTS.get(args.event_type, {}).get("header") \
            or f"公開通知: {args.event_type}"
        mention, _mention_reason = _resolve_route_mention(
            [label, args.title, args.detail, args.event_type],
            force_mention=args.force_mention,
            no_mention=args.no_mention,
            baseline_mention=args.event_type not in _PUBLISH_FYI_EVENTS,
        )
        if not mention and args.event_type not in _PUBLISH_FYI_EVENTS:
            print(
                f"INFO: publish 通知の @mention を抑制しました（{_mention_reason}）。"
                "強制するには --force-mention。",
                file=sys.stderr,
            )
        blocks = build_publish_blocks(
            args.event_type,
            video_id=args.video_id,
            title=args.title,
            url=args.url,
            detail=args.detail,
            mention_text=mention,
        )
        id_suffix = f" ({args.video_id})" if args.video_id else ""
        text = f"{mention}🔔 {label}{id_suffix}"

    elif args.type == "comment-approval":
        mention_user_id = os.environ.get("SLACK_MENTION_USER_ID", "")
        platform_name = args.platform_name or "unknown"
        # 大文字小文字の表記揺れ（HIGH / Critical 等）で @mention 判定や emoji が漏れないよう正規化
        priority = args.priority.lower()
        category_name = args.category_name or priority
        # トリアージ（user-notification-triage.md §4）: 炎上・ブランド毀損リスク
        # （critical/high = fact_error / criticism 等）の個別通知のみ @mention する。
        # medium/low（request/positive/faq）・バッチダイジェストは FYI（ドラフトは Issue に残り、
        # 自動投稿はしない。ユーザーは都合のよいときに Issue で確認できる）。
        _should_mention = (not args.batch_summary) and priority in ("critical", "high")
        mention = (f"<@{mention_user_id}> " if mention_user_id else "") if _should_mention else ""
        # Slack mrkdwn サニタイズ: <, >, & をエスケープし、バッククォートを無害化
        def _sanitize_mrkdwn(text: str) -> str:
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")
        comment_text = _sanitize_mrkdwn(args.comment_text[:100]) if args.comment_text else ""
        reply_text = _sanitize_mrkdwn(args.reply_text[:200]) if args.reply_text else ""
        issue_num = args.issue_number

        # バッチモード（サマリーのみ）
        if args.batch_summary:
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": "📬 コメント返信承認依頼（バッチ）"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": args.batch_summary}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "→ 承認: Slack スレッドで `OK` / GitHub Issue で個別確認"}},
            ]
            text = f"{mention}📬 コメント返信承認依頼（バッチ）"
        else:
            # 個別モード
            priority_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(priority, "🟡")
            issue_link = f" (<https://github.com/{_REPO_SLUG}/issues/{issue_num}|#{issue_num}>)" if issue_num else ""
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"📬 コメント返信承認依頼（{platform_name}）"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"{priority_emoji} *カテゴリ*: {category_name}{issue_link}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*元コメント*:\n> {comment_text}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*返信案*:\n```{reply_text}```"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "→ このスレッドで `OK` で承認 / `修正: {テキスト}` で修正投稿 / `スキップ` で却下"}},
            ]
            text = f"{mention}📬 [{platform_name}] {category_name} コメント返信承認依頼{issue_link}"

    elif args.type == "daily-progress":
        blocks = build_daily_progress_blocks(
            args.summary, args.action_items, args.analytics, args.youtube_status,
            no_mention=args.no_mention,
        )
        mention = "" if args.no_mention else _get_mention_text()
        text = f"{mention}📊 日次進捗レポート"

    elif args.type == "half-day-summary":
        blocks = build_half_day_summary_blocks(
            args.summary, args.action_items, period_label=args.period_label,
            no_mention=args.no_mention,
        )
        mention = "" if (args.no_mention or not args.action_items) else _get_mention_text()
        text = f"{mention}🐾 半日アウトカムサマリー"

    elif args.type == "chart":
        # 週次トレンドグラフ等を公開画像 URL（R2 公開ドメイン）から image block で表示する。
        # FYI 扱い（週次レポート・要対応ではない）のため @mention しない（CP-6・user-notification-triage）。
        title = args.title or "週次レポート"
        summary = args.text or ""
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}}]
        if summary:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary[:3000]}})
        if args.image_url:
            blocks.append({
                "type": "image",
                "image_url": args.image_url,
                "alt_text": (args.alt_text or "グラフ")[:1000],
            })
        else:
            # 画像 URL が無い場合（描画フォールバックでテキスト縮退したケース）はサマリーのみ投稿する。
            blocks.append({"type": "context", "elements": [{
                "type": "mrkdwn", "text": "（グラフ画像なし・テキストサマリーのみ）",
            }]})
        text = title

    elif args.type == "routine-idle":
        # スロットゲート: fresh session でも状態ファイルなしに「1 日 1 回」に抑えるため、
        # JST の指定スロット（既定 08:00〜10:00）だけ送信する。スロット外は自己抑制（exit 0）。
        if not args.force and not in_idle_window(_jst_hour(), args.idle_window_start, args.idle_window_end):
            print(
                f"INFO: routine-idle はスロット外（現在 {_jst_hour():02d}:xx JST・許可枠 "
                f"{args.idle_window_start:02d}:00〜{args.idle_window_end:02d}:00 JST）のため送信を抑制しました。"
                "（強制送信は --force）",
                file=sys.stderr,
            )
            sys.exit(0)
        blocks = build_routine_idle_blocks(args.routine_name, args.detail)
        label = args.routine_name or "定期ルーティン"
        text = f"🌙 {label}: 消化対象なし（アイドル）"

    result = post_message(args.channel, text, blocks)
    if result.get("ok"):
        ts = result.get("ts", "")
        print(f"OK: message sent (ts={ts})")
    else:
        print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
