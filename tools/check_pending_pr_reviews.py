#!/usr/bin/env python3
"""レビュー待ちPRを検出し、セッション復帰時に対応を再開するためのスクリプト。

クラウド環境（Claude.ai Scheduled Tasks）ではセッションタイムアウトが頻発する。
PR作成後のAIレビュー待ち（sleep ポーリング）中にセッションが切れると、
レビュー対応が宙に浮く。本スクリプトはセッション開始時やスケジューラーから
呼び出され、対応が必要なPRを検出する。

レビュー構成（飼い主決定）:
  外部 AI レビュアー（Copilot / Gemini）への依頼は廃止。レビューは Claude 自身が
  Layer 1 セルフレビュー（自前 code-review スキル・Skill(code-review)）で完結する。組み込み
  /code-review は disable-model-invocation で自律起動不可のため、同名 project スキル
  .claude/skills/code-review/ が bundled を置換している（#275 → #280）。
  本スクリプトは Layer 1 を機械検出できないため、未解決スレッドの
  有無と経過時間でセッション復帰時の対応を決める。SSOT: docs/rules/ai-reviewer-strategy.md

検出条件:
  1. Open状態のPR（kai-kou/btc-realtime-chart）
  2. Claude 作業ブランチ（claude/ feat/ fix/ docs/ 等）**かつ著者が OWNER/MEMBER/COLLABORATOR**
     （authorAssociation・#379）の PR、または未解決スレッドのある PR
  3. 指摘対応 or Layer 1 セルフレビューが未完了

著者検証（#379・公開リスク監査 r03 critical）:
  _is_claude_branch() はブランチ名の前方一致だけでなく、gh --json authorAssociation を
  AND 条件に加える。fork PR は通常 CONTRIBUTOR 以下になるため、規約どおりの命名
  （feat/ fix/ docs/ 等）で fork から PR を出しても needs_prompt（自動マージ対象）にならない。
  authorAssociation が取得できない場合は安全側（対象外）に倒す。
  gh 経路（実 gh・クラウドの gh_shim.py 双方）は REST の author_association から取得できるが、
  gh が全面失敗して呼び出し元が mcp__github__list_pull_requests に直接切り替える場合、
  同ツールには authorAssociation 相当のフィールドが存在しない（2026-08 実機確認）。
  その場合は mcp__github__list_repository_collaborators で信頼済みログイン集合を作り
  PR 著者（user.login）と突合するフォールバックを使う（docs/rules/github-mcp-fallback-patterns.md）。

出力:
  - PENDING:<pr_number>:<status>:<summary>
  - status: needs_response（未解決スレッドあり = CI 失敗・人手コメント・履歴上のボット指摘 → 指摘対応）
            needs_prompt（Layer 1 セルフレビュー要実施 → 観点別フレッシュ文脈レビュー実行 → 即マージ）
            awaiting_review（PR 作成直後 = 作成セッションがセルフレビュー実行中 → 待機）
            blocked_waiting_user（status:waiting-user ラベル付き → 自動マージ対象外）
            no_action（Claude 以外の PR または手動 PR）
  ※ 外部レビュアーの 25 分応答待ち・催促・問題なし判定タイムアウトは廃止。

gh 取得失敗時（クラウドの 403 等・Issue #130）:
  PR 一覧取得自体（`gh pr list`）が失敗した場合は「0 件」と沈黙せず、stderr に
  `ERROR: gh_unavailable: ...`、stdout に `GH_UNAVAILABLE: ...` を出力して **exit code 3** で終了する。
  呼び出し元は exit code を確認し、3 の場合は `mcp__github__list_pull_requests` で直接代替すること
  （PR ごとの補助情報取得の失敗は従来どおり部分的な情報欠落として許容し、全体を失敗にはしない）。

アクティブセッション除外（CP-4・Issue #3007）:
  各 PR について「人間側（Claude セッション）の最終アクティビティ」
  （PR 作成・head ブランチへのコミット・非ボットコメント）からの経過分を
  last_activity_min として算出する。直近 ACTIVE_WINDOW_MIN 分以内に活動が
  ある PR は active_session=true となり、--actionable-only から除外される
  （作成セッションが現役で対応中の PR に他セッションが介入しない）。
  活動が途絶えた PR は従来どおり救済対象（CP-3 維持）。
  作成セッション自身のハートビート（--json + PR 番号フィルタ）は status を
  そのまま参照するため影響を受けない。

アイデンティティベース所有判定（CP-4・#47）:
  各 PR 本文の `Session-Id: {UUID}` トレーラー（session-sprint-rules.md §2 で必須化）を
  owner_session_id として解析し、現セッション（$CLAUDE_CODE_SESSION_ID）と一致するかを
  is_mine で返す。--mine を付けると「自セッションが作成した PR のみ」を決定論的に出力する
  （他セッションの PR は触れない）。自 PR は所有者本人なので active_session（時間ベース）の
  除外を適用しない＝10 分超アイドルやセッション再起動・圧縮後でも自 PR を見失わず責任継続できる。
  これが「自セッション作成 PR のみマージまで進める」積極的所有判定（時間ベースのレイヤー 5 を補完）。

Usage:
    python3 tools/check_pending_pr_reviews.py
    python3 tools/check_pending_pr_reviews.py --json
    python3 tools/check_pending_pr_reviews.py --actionable-only --include-active
    python3 tools/check_pending_pr_reviews.py --mine --json            # 自セッション所有 PR のみ
    python3 tools/check_pending_pr_reviews.py --mine --actionable-only # 自 PR で要対応のもの
    python3 tools/check_pending_pr_reviews.py --self-test              # Session-Id 解析テスト
    python3 tools/check_pending_pr_reviews.py --verify-layer1 <PR番号> # Layer 1 投稿済みか機械検証（#462）
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_slug import repo_from_git_remote as _repo_from_git_remote  # noqa: E402


class GhUnavailableError(RuntimeError):
    """gh CLI の repo スコープ操作が失敗した（クラウドの 403 等）ことを示す。

    「取得できたが 0 件」と「取得自体に失敗した」を区別するために使う（Issue #130・L-074/L-086）。
    このエラーを握りつぶして空リスト扱いすると、クラウドで常に「レビュー待ち PR 0 件」という
    誤判定が沈黙して発生する。
    """


# owner/repo 解決ロジックの正本は tools/repo_slug.py（#215）。
# 本リポジトリ自身（bootstrap 未適用）に対して運用ルーティンを回すケース（R-1・#213/#220）では
# プレースホルダが未置換のまま残るため、その場合のみ _repo_from_git_remote() で動的に補う。


_REPO_PLACEHOLDER = "kai-kou/btc-realtime-chart"
# owner / name を REPO から動的導出する（GraphQL クエリ等でハードコードしない）。
# bootstrap.sh が REPO の kai-kou/btc-realtime-chart を置換すれば OWNER/REPO_NAME も追従する。
if "__" in _REPO_PLACEHOLDER:
    # プレースホルダ未置換（本リポジトリの自己ホスト実行等） → git remote から動的補完
    REPO = _repo_from_git_remote() or _REPO_PLACEHOLDER
else:
    # bootstrap.sh 済み（下流リポジトリ） → git 呼び出し不要、既存の決定論的動作を維持
    REPO = _REPO_PLACEHOLDER
OWNER, _, REPO_NAME = REPO.partition("/")
# 形式不正（owner / name のどちらか欠落・bootstrap 未実行のプレースホルダ残存）のまま
# GitHub API を叩くと別リポジトリを参照したり取得失敗を 0 件扱いして誤判定するため、
# API を実際に使う前（main() 冒頭・ただし API 非依存の --self-test は除く）に明示的に失敗させる
# （Copilot review・誤 ready_to_merge 防止）。純粋関数（parse_session_id 等）の self-test は
# プレースホルダのままのテンプレートリポジトリでも実行できるよう、検証は関数化して遅延する。
def _validate_repo() -> None:
    # プレースホルダ検出は "__" の部分一致で行う（tools/repo_slug.py と同方式）。
    # "kai-kou" 等の完全一致リテラルを書くと bootstrap.sh の全域 sed がこの判定文字列
    # 自体を実値に置換してしまい、置換成功後もガードが常時発火する（exit 2）ため。
    if not OWNER or not REPO_NAME or "__" in REPO:
        print(
            f"ERROR: REPO の形式が不正です: '{REPO}'（owner/name 形式が必要。"
            "bootstrap.sh でプレースホルダを置換してください）",
            file=sys.stderr,
        )
        sys.exit(2)


GEMINI_BOT = "gemini-code-assist[bot]"
COPILOT_BOTS = {"copilot[bot]", "copilot-pull-request-reviewer[bot]"}
AI_REVIEWERS = {GEMINI_BOT} | COPILOT_BOTS

# Gemini Code Assist 消費者版は 2026-07-17 に code review activity 完全停止（#2485）。
# 同日以降は Gemini を必須から外し、Copilot 単独完了（+ 恒久構成 Claude /code-review）で
# 即時マージ可能にする。これがないと has_gemini_review が常に False になり全 PR が25分遅延する。
# 恒久構成の正本: docs/rules/ai-reviewer-strategy.md
GEMINI_SUNSET_DATE = datetime(2026, 7, 17, tzinfo=timezone.utc)

# 直近この分数以内に人間側アクティビティがある PR は「作成セッションが現役」と
# みなし、他セッション（--actionable-only 利用者）は介入しない（CP-4・Issue #3007）。
# 活動途絶後はこの分数の遅延だけで従来どおり救済される（CP-3 とのバランス点）。
ACTIVE_WINDOW_MIN = 10

# アイデンティティベース所有判定（CP-4・#47）。
# PR 本文の `Session-Id: {UUID}` トレーラー（session-sprint-rules.md §2 で必須化）を
# 所有権の権威ソースとして解析する。これにより「自セッションが作成した PR のみ」を
# 決定論的に識別でき、時間ベースの active_session（レイヤー 5）の穴
# （①自 PR でも 10 分超アイドルで奪われる ②セッション再起動・圧縮後に自 PR を見失う）を埋める。
# sprint_session_metrics.py の SESSION_ID_RE と同一パターン（UUID 形式・大文字小文字不問）。
SESSION_ID_RE = re.compile(
    r"Session-Id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)


def parse_session_id(body: str | None) -> str:
    """PR 本文から `Session-Id:` トレーラーの UUID を抽出する（小文字正規化）。

    トレーラー不在・形式不正の場合は空文字を返す（時間窓フォールバックに委ねる）。
    純粋関数（API 非依存）のため --self-test で検証する。
    """
    if not body:
        return ""
    m = SESSION_ID_RE.search(body)
    return m.group(1).lower() if m else ""


def current_session_id(explicit: str | None = None) -> str:
    """現セッションの ID を返す。--session-id 明示指定 > $CLAUDE_CODE_SESSION_ID。"""
    if explicit:
        return explicit.strip().lower()
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip().lower()


def _gemini_sunset() -> bool:
    """Gemini Code Assist 停止日（2026-07-17 UTC）以降なら True。"""
    return datetime.now(timezone.utc) >= GEMINI_SUNSET_DATE


def run_gh(args: list[str], critical: bool = False) -> str:
    """gh CLI コマンドを実行して stdout を返す。

    `critical=True` の呼び出しが失敗した場合は空文字列を返さず `GhUnavailableError` を送出する
    （クラウドで gh が 403 になる場合、呼び出し元が「0 件」と誤判定しないようにするため）。
    補助的な取得（PR ごとのレビュー/コメント等）は `critical=False`（既定）のまま、
    部分的な情報欠落として空リストにフォールバックしてよい。

    `gh` バイナリ自体が存在しない・実行できない環境（クラウドで `.claude/bin/gh` シムが未配置・
    実 gh 未導入・シムが破損している等）では `subprocess.run` が `returncode` を返す前に
    `OSError`（`FileNotFoundError` / `PermissionError` に加え、壊れた shebang 等による
    `Exec format error` も同族の `OSError`）を送出する。これを捕捉せず未処理例外のまま伝播させると、
    呼び出し元の `except GhUnavailableError` に届かず即座にトレースバックで落ちる（Issue #53）。
    `returncode != 0` と同じ経路に合流させる。`subprocess.TimeoutExpired` は `OSError` を継承しない
    ため本節では捕捉されず、従来どおり呼び出し元に伝播する（スコープ外）。
    """
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except OSError as e:
        print(f"WARNING: gh command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stderr: gh バイナリを実行できません: {e}", file=sys.stderr)
        if critical:
            raise GhUnavailableError(f"gh バイナリを実行できません: {e}") from e
        return ""
    if result.returncode != 0:
        stderr_msg = result.stderr.strip()
        print(f"WARNING: gh command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stderr: {stderr_msg}", file=sys.stderr)
        if critical:
            raise GhUnavailableError(stderr_msg or f"gh command failed: {' '.join(cmd)}")
        return ""
    return result.stdout.strip()


def get_open_prs() -> list[dict]:
    """Open状態のPR一覧を取得する。

    gh 呼び出し自体の失敗（クラウドの 403 等）は `GhUnavailableError` として呼び出し元に伝播する
    （「取得失敗」と「取得できたが 0 件」を混同しないため・Issue #130）。
    """
    output = run_gh([
        "pr", "list",
        "-R", REPO,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,createdAt,headRefName,author,authorAssociation,reviewRequests,labels,body",
    ], critical=True)
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        print("WARNING: Failed to parse PR list JSON", file=sys.stderr)
        return []


def get_pr_reviews(pr_number: int, critical: bool = False) -> list[dict]:
    """PRのレビュー一覧を取得する。

    `critical=True` は gh 到達不可（コマンド失敗・JSON 破損とも）を `GhUnavailableError` として
    呼び出し元に伝播する（`verify_layer1_review` が「レビュー0件」と「gh失敗による空リスト」を
    区別するために使う）。`commit_id` はレビュー投稿時点の PR head SHA（force-push 後の
    古いレビューを「最新コミットに対する実施」と誤認しないための判定材料・#462）。
    """
    output = run_gh([
        "api", f"repos/{REPO}/pulls/{pr_number}/reviews",
        "--jq", '[.[] | {user: .user.login, state, submitted_at, body_len: (.body | length), commit_id}]',
    ], critical=critical)
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        if critical:
            raise GhUnavailableError(f"reviews JSON 解析失敗: {e}") from e
        return []


def get_pr_comments(pr_number: int, critical: bool = False) -> list[dict]:
    """PRのインラインコメント一覧を取得する。

    `critical` / `commit_id` の意味は `get_pr_reviews` と同じ。
    """
    output = run_gh([
        "api", f"repos/{REPO}/pulls/{pr_number}/comments",
        "--jq", '[.[] | {user: .user.login, created_at, body_len: (.body | length), path, commit_id}]',
    ], critical=critical)
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        if critical:
            raise GhUnavailableError(f"comments JSON 解析失敗: {e}") from e
        return []


def get_pr_gemini_trigger_comments(pr_number: int) -> list[dict]:
    """/gemini review コマンドを含むコメントを取得する（投稿者種別不問）。"""
    output = run_gh([
        "api", f"repos/{REPO}/issues/{pr_number}/comments",
        "--jq", '[.[] | select(.body | test("/gemini review"; "i")) | {user: .user.login, created_at, body: (.body | .[0:200])}]',
    ])
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return []


def get_pr_issue_comments(pr_number: int) -> list[dict]:
    """PRの一般コメント（ボットのみ）を取得する。"""
    output = run_gh([
        "api", f"repos/{REPO}/issues/{pr_number}/comments",
        "--jq", '[.[] | select(.user.type == "Bot" or (.user.login | test("copilot|gemini"; "i"))) | {user: .user.login, created_at, body_len: (.body | length), body: (.body // "" | .[0:500])}]',
    ])
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return []


def get_branch_last_commit_time(branch: str) -> str:
    """head ブランチの最新コミット時刻（committer date・ISO8601）を返す。取得失敗時は空文字。

    注意: gh api は `-f` フィールド指定があるとデフォルトメソッドが POST に切り替わるため、
    `--method GET` の明示が必須（省略すると POST /commits → 404 で常に空文字となり、
    ブランチコミットによるアクティビティ検知が無効化される）。
    """
    if not branch:
        return ""
    output = run_gh([
        "api", "--method", "GET", f"repos/{REPO}/commits",
        "-f", f"sha={branch}",
        "-f", "per_page=1",
        "--jq", '.[0]?.commit.committer.date // ""',
    ])
    return output.strip()


def get_pr_human_comment_times(pr_number: int) -> list[str]:
    """PR の非ボット（人間 / Claude セッション）issue コメント時刻一覧を返す。"""
    output = run_gh([
        "api", f"repos/{REPO}/issues/{pr_number}/comments",
        "--jq", '[.[] | {login: .user.login, type: (.user.type // ""), created_at}]',
    ])
    if not output:
        return []
    try:
        comments = json.loads(output)
        times = []
        for c in comments:
            login = (c.get("login") or "").lower()
            user_type = c.get("type") or ""
            is_bot = (
                user_type == "Bot"
                or "copilot" in login
                or "gemini" in login
                or login.endswith("[bot]")
            )
            if not is_bot:
                times.append(c.get("created_at", ""))
        return times
    except json.JSONDecodeError:
        return []


def _parse_iso(ts: str) -> datetime | None:
    """ISO8601 文字列を datetime に変換する。失敗時は None。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def compute_last_activity_min(
    pr: dict,
    inline_comments: list[dict],
    human_comment_times: list[str] | None = None,
) -> int:
    """人間側（Claude セッション）の最終アクティビティからの経過分を算出する（Issue #3007）。

    アクティビティ源:
      - PR 作成時刻
      - head ブランチの最新コミット時刻（指摘対応中のコミットを検知）
      - 非ボットの issue コメント時刻
      - 非ボットのインラインレビューコメント時刻（スレッド返信を検知）

    human_comment_times: 事前取得済みの非ボットコメント時刻リスト。
      None の場合は内部で API を呼ぶ（後方互換）。
      analyze_pr() からは重複呼び出し削減のため事前取得値を渡すこと。
    """
    candidates: list[datetime] = []
    created = _parse_iso(pr.get("createdAt", ""))
    if created:
        candidates.append(created)
    branch_commit = _parse_iso(get_branch_last_commit_time(pr.get("headRefName", "")))
    if branch_commit:
        candidates.append(branch_commit)
    times = human_comment_times if human_comment_times is not None else get_pr_human_comment_times(pr["number"])
    for ts in times:
        parsed = _parse_iso(ts)
        if parsed:
            candidates.append(parsed)
    for c in inline_comments:
        login = (c.get("user", "") or "").lower()
        is_bot = login.endswith("[bot]") or "copilot" in login or "gemini" in login
        if not is_bot:
            parsed = _parse_iso(c.get("created_at", ""))
            if parsed:
                candidates.append(parsed)
    if not candidates:
        return 9999
    elapsed = datetime.now(timezone.utc) - max(candidates)
    return max(0, int(elapsed.total_seconds() / 60))


def get_unresolved_threads(pr_number: int) -> int:
    """未解決のレビュースレッド数を取得する。"""
    query = """
    query {
      repository(owner: "%s", name: "%s") {
        pullRequest(number: %d) {
          reviewThreads(first: 100) {
            nodes { isResolved }
          }
        }
      }
    }
    """ % (OWNER, REPO_NAME, pr_number)
    output = run_gh(["api", "graphql", "-f", f"query={query}"])
    if not output:
        return 0
    try:
        data = json.loads(output)
        threads = data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
        return sum(1 for t in threads if not t.get("isResolved", True))
    except (json.JSONDecodeError, KeyError, TypeError):
        return 0


def analyze_pr(pr: dict) -> dict:
    """PRのレビュー状態を分析する。"""
    pr_number = pr["number"]
    title = pr["title"]
    branch = pr.get("headRefName", "")
    created_at = pr.get("createdAt", "")
    pr_labels = {lbl.get("name", "") for lbl in pr.get("labels", [])}
    # アイデンティティベース所有判定（#47）: PR 本文の Session-Id トレーラーを抽出
    owner_session_id = parse_session_id(pr.get("body", ""))
    # 著者検証（#379・公開リスク監査 r03 critical）: ブランチ名だけでなく著者関係も見る
    author_association = pr.get("authorAssociation", "")

    # status:waiting-user ラベル付き PR は自動マージ対象から除外（#2173）
    if "status:waiting-user" in pr_labels:
        return {
            "pr_number": pr_number,
            "title": title,
            "branch": branch,
            "status": "blocked_waiting_user",
            "summary": "status:waiting-user ラベル付き（ユーザー判断必須・自動マージ対象外）",
            "elapsed_min": 0,
            "ai_reviews_count": 0,
            "ai_inline_count": 0,
            "unresolved_threads": 0,
            "bot_comments_count": 0,
            "has_gemini_review": False,
            "has_copilot_review": False,
            "gemini_quota_exceeded": False,
            "last_activity_min": 9999,
            "active_session": False,
            "owner_session_id": owner_session_id,
            "author_association": author_association,
        }

    # レビューリクエスト（requested_reviewers）を確認
    review_requests = pr.get("reviewRequests", [])
    has_ai_reviewer_requested = False
    for rr in review_requests:
        login = rr.get("login", "") or rr.get("name", "")
        if any(bot_name.replace("[bot]", "") in login.lower() for bot_name in ["copilot", "gemini-code-assist"]):
            has_ai_reviewer_requested = True
            break

    # レビュー取得
    reviews = get_pr_reviews(pr_number)
    ai_reviews = [r for r in reviews if r.get("user", "") in AI_REVIEWERS]

    # インラインコメント取得
    inline_comments = get_pr_comments(pr_number)
    ai_inline = [c for c in inline_comments if c.get("user", "") in AI_REVIEWERS]

    # ボットのIssueコメント取得
    issue_comments = get_pr_issue_comments(pr_number)

    # /gemini review コマンドコメント取得（投稿者種別不問・L-051 対策）
    gemini_trigger_comments = get_pr_gemini_trigger_comments(pr_number)

    # 未解決スレッド数
    unresolved = get_unresolved_threads(pr_number)

    # PR作成からの経過時間（ステータス判定より前に計算する）
    elapsed_min = 0
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - created
            elapsed_min = int(elapsed.total_seconds() / 60)
        except (ValueError, TypeError):
            pass

    # ステータス判定
    has_ai_review = len(ai_reviews) > 0 or len(ai_inline) > 0
    has_unresolved = unresolved > 0

    # Gemini / Copilot を個別に判定（L-038 対策: Copilot 無応答 → 片方完了でマージ可）
    ai_reviewer_logins = {r.get("user", "") for r in ai_reviews} | {c.get("user", "") for c in ai_inline}
    # Gemini はフォーマルレビュー・インラインコメントに加え、issue comment でも応答する場合がある
    gemini_issue_comments = [c for c in issue_comments if c.get("user", "") == GEMINI_BOT]
    has_gemini_review = (GEMINI_BOT in ai_reviewer_logins) or bool(gemini_issue_comments)
    has_copilot_review = bool(ai_reviewer_logins & COPILOT_BOTS)

    # Gemini クォータ超過検出（#1079 対策）
    # Gemini が「quota」「rate limit」を含むコメントを投稿している場合、当日クォータ超過と判定する
    gemini_quota_exceeded = any(
        ("quota" in c.get("body", "").lower() or "rate limit" in c.get("body", "").lower()
         or "クォータ" in c.get("body", ""))
        for c in issue_comments
        if c.get("user", "").startswith("gemini")
    )

    # Gemini レビュー依頼済み判定（L-051 対策）
    # Gemini は /gemini review コメントで依頼されるため reviewRequests に現れない。
    # gemini_issue_comments（Gemini Bot 応答）または gemini_trigger_comments（/gemini review コマンド・投稿者不問）があれば依頼済みと判定する。
    has_gemini_review_requested = bool(gemini_issue_comments) or bool(gemini_trigger_comments)

    # Copilot レビュー依頼済み判定
    has_copilot_review_requested = any(
        rr.get("login", "").replace("[bot]", "").lower() in {"copilot", "copilot-pull-request-reviewer"}
        for rr in review_requests
    )

    # AIレビュー依頼済みの最終判定（Gemini か Copilot のどちらかに依頼済みなら True）
    has_ai_reviewer_requested_combined = (
        has_ai_reviewer_requested
        or has_gemini_review_requested
        or has_copilot_review_requested
    )

    # 外部 AI レビュアー（Copilot/Gemini）への依頼は廃止（飼い主決定）。レビューは Claude 自身の
    # Layer 1 セルフレビュー（自前 code-review スキル・組み込みを同名 project スキルで置換済み・
    # #275 → #280）で完結する。本スクリプトは Layer 1 の実施を機械検出できないため、未解決スレッドの有無と
    # 経過時間でセッション復帰時の対応を決める。外部レビュアーの 25 分応答待ち・催促は廃止した。
    # has_gemini_review / has_copilot_review / gemini_quota_exceeded は履歴 PR の互換情報として
    # 返り値に残すが、マージ判定には用いない。
    #
    # 🔴 著者検証は「分岐に入る前」に一度だけ行う（#379）。
    #
    # 経緯: 当初は _is_claude_branch() の内部にだけ authorAssociation を AND した。しかしその後の
    # レビューで、判定に至る経路が 3 本あり、うち 2 本が検証を迂回できることが実測で判明した:
    #   ① _is_claude_branch() 経由 …… ブランチ名の前方一致。命名規則は CLAUDE.md で公開済み
    #   ② has_ai_reviewer_requested_combined / has_ai_review 経由 …… OR で並んでいたため、
    #      fork PR の作成者が自分の PR に `/gemini review` と書く・bot レビューを呼ぶだけで到達できた
    #   ③ has_unresolved 経由 …… ブランチ名すら不要。fork PR の作成者が自分の PR に
    #      レビューコメントを 1 件投稿すれば未解決スレッドが立ち、needs_response に到達できた
    #      （GitHub は自分の PR への approve は拒否するが comment は拒否しない）
    # ①だけを塞ぐと穴が②③へ移動するだけなので、**すべての分岐の手前で信頼境界を 1 回引く**。
    # 取得できないときは fail-closed（対象外）。外部 AI レビュアーは廃止済み（ai-reviewer-strategy.md）で
    # ②の 2 経路は履歴 PR 互換のための残骸だが、残す以上は同じ信頼境界の内側に置く。
    if not _is_trusted_author_association(author_association):
        # 信頼できない著者（fork PR・外部コントリビューター・authorAssociation 取得失敗）は
        # どのステータスにも乗せない。pr-review-watcher は status だけを見てマージまで進めるため、
        # ここを通した時点で無人マージの対象になる（下流へも同じ設定が配布される）。
        status = "no_action"
        summary = "信頼境界外の著者による PR（自律レビュー・自動マージの対象外）"
    elif has_unresolved:
        # 未解決スレッド（CI 失敗・人手コメント・履歴上のボット指摘等）→ 指摘対応が必要
        status = "needs_response"
        summary = f"未解決スレッド{unresolved}件（指摘対応が必要）"
    elif (
        _is_claude_branch(branch, author_association)
        or has_ai_reviewer_requested_combined
        or has_ai_review
    ):
        # Claude 作業ブランチの PR。Layer 0（機械ゲート）+ Layer 1（観点別フレッシュ文脈セルフレビュー）で
        # 完結する。復帰セッションはセルフレビューを実行し指摘を解消してから即マージする
        # （外部レビュアーの応答待ちは存在しない）。active_session（直近10分の活動）除外により
        # --actionable-only では作成セッションが現役対応中の PR は出力されないため、ここに残るのは
        # アイドル化した自 PR or 孤児 PR で、復帰セッションが Layer 1 を実行してマージすべきもの。
        if elapsed_min >= ACTIVE_WINDOW_MIN:
            status = "needs_prompt"  # = Layer 1 セルフレビュー要実施 → 観点別フレッシュ文脈レビュー実行 → マージ
            summary = (
                f"Layer 1 セルフレビュー要実施・{elapsed_min}分経過"
                "（観点別フレッシュ文脈レビュー実行 → 指摘解消 → 即マージ。外部レビュアー依頼なし）"
            )
        else:
            status = "awaiting_review"
            summary = f"PR 作成直後・{elapsed_min}分（作成セッションがセルフレビュー実行中）"
    else:
        status = "no_action"
        summary = "Claude 以外の PR または手動 PR（自律レビュー対象外）"

    # アクティブセッション判定（Issue #3007・CP-4）
    # 介入対象ステータスの PR のみ追加 API 呼び出しでアクティビティを算出する
    last_activity_min = 9999
    active_session = False
    if status in ("awaiting_review", "needs_prompt", "needs_response", "ready_to_merge"):
        human_comment_times = get_pr_human_comment_times(pr_number)
        last_activity_min = compute_last_activity_min(pr, inline_comments, human_comment_times)
        active_session = last_activity_min < ACTIVE_WINDOW_MIN
        if active_session:
            summary += f"｜⚠️ 作成セッション活動中（最終活動{last_activity_min}分前）→ 他セッションは介入禁止"

    return {
        "pr_number": pr_number,
        "title": title,
        "branch": branch,
        "status": status,
        "summary": summary,
        "elapsed_min": elapsed_min,
        "ai_reviews_count": len(ai_reviews),
        "ai_inline_count": len(ai_inline),
        "unresolved_threads": unresolved,
        "bot_comments_count": len(issue_comments),
        "has_gemini_review": has_gemini_review,
        "has_copilot_review": has_copilot_review,
        "gemini_quota_exceeded": gemini_quota_exceeded,
        "last_activity_min": last_activity_min,
        "active_session": active_session,
        "owner_session_id": owner_session_id,
        "author_association": author_association,
    }


def get_pr_head_sha(pr_number: int, critical: bool = False) -> str:
    """PRの現在のheadコミットSHAを取得する（`verify_layer1_review` の最新コミット判定用）。"""
    output = run_gh([
        "api", f"repos/{REPO}/pulls/{pr_number}",
        "--jq", ".head.sha",
    ], critical=critical)
    return output.strip()


def _layer1_verdict(review_count: int, inline_count: int) -> int:
    """レビュー件数・インラインコメント件数から Layer 1 実施の終了コードを決める純粋関数。

    I/O から分離することで `_run_self_test()` が gh 非依存で判定ロジックを検証できる（#462）。
    返り値は 0（LAYER1_VERIFIED）/ 1（LAYER1_MISSING）のいずれか。
    """
    return 0 if (review_count > 0 or inline_count > 0) else 1


def verify_layer1_review(pr_number: int) -> int:
    """マージ直前に Layer 1 セルフレビューが投稿済みかを機械検証する（#462）。

    Layer 0（`self_review_check.py`）は PR 作成前の静的チェックで、Layer 1（観点別フレッシュ文脈
    セルフレビュー・`Skill(code-review)`）の実施有無までは検出しない。#461 により指摘ゼロでも
    `event="COMMENT"` のレビューを 1 件投稿する運用のため、対象 PR にフォーマルレビュー
    （`get_pr_reviews`）または行単位インラインコメント（`get_pr_comments`）が 1 件でもあれば
    Layer 1 実施済みとみなす（投稿者の識別はしない — 本プロジェクトは自律運用のため、
    レビューの存在自体が Layer 1 実施の代理指標として十分機能する）。

    ただし件数を数える対象は **現在の PR head コミットに対するレビュー/コメントのみ**
    （`commit_id == head_sha`）に絞る。force-push や追加コミット後も古いレビューが
    残り続けるため、絞り込まないと「最新差分は未レビューなのに件数だけで PASS する」
    誤判定が起こる（レビューレビュー指摘・#462）。

    返り値は終了コード:
      0 = LAYER1_VERIFIED（現行コミットへのレビューまたはインラインコメントが1件以上・マージ続行可）
      1 = LAYER1_MISSING（0 件・Layer 1 未実施とみなしマージをブロックする）
      2 = LAYER1_UNKNOWN（gh 到達不可 or JSON 破損で判定不能・誤ブロックを避け mcp フォールバックへ委ねる）
    """
    try:
        head_sha = get_pr_head_sha(pr_number, critical=True)
        reviews = get_pr_reviews(pr_number, critical=True)
        inline_comments = get_pr_comments(pr_number, critical=True)
    except GhUnavailableError as e:
        print(f"ERROR: gh_unavailable: {e}", file=sys.stderr)
        print(
            "LAYER1_UNKNOWN: gh 経由の取得に失敗しました（クラウドの 403 等）。"
            "mcp__github__pull_request_read(method=\"get_reviews\") で件数を直接確認してから"
            "マージ判断してください（誤ブロックを避けるためこの終了コードだけではマージを止めない）。",
        )
        return 2

    current_reviews = [r for r in reviews if r.get("commit_id") == head_sha]
    current_inline = [c for c in inline_comments if c.get("commit_id") == head_sha]
    review_count = len(current_reviews)
    inline_count = len(current_inline)

    if _layer1_verdict(review_count, inline_count) == 0:
        print(f"LAYER1_VERIFIED:{pr_number}:reviews={review_count}:inline={inline_count}")
        return 0

    stale_reviews = len(reviews) - review_count
    stale_inline = len(inline_comments) - inline_count
    stale_note = (
        f"（うち現行コミット以前の古いレビュー/コメント: reviews={stale_reviews} inline={stale_inline}）"
        if (stale_reviews or stale_inline) else ""
    )
    print(
        f"LAYER1_MISSING:{pr_number}:reviews=0:inline=0 "
        f"(Layer 1 セルフレビュー未投稿の可能性 — Skill(code-review) を実行してから再検証すること){stale_note}",
    )
    return 1


# 自動マージ対象と認めるレビュー著者関係（GitHub の authorAssociation 値）。
# CONTRIBUTOR / FIRST_TIME_CONTRIBUTOR / FIRST_TIMER / NONE は信頼しない
# （fork PR は通常 CONTRIBUTOR 以下になる）。
TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _is_trusted_author_association(author_association: str | None) -> bool:
    """authorAssociation が信頼できる（OWNER/MEMBER/COLLABORATOR）かどうかを判定する。

    取得できなかった場合（None・空文字・想定外の値）は **安全側（False）に倒す**
    （公開リスク監査 r03・critical: ブランチ名の前方一致だけで自動マージ対象と
    判定すると、CLAUDE.md が公開しているブランチ命名規則どおりの名前で fork から
    PR を出すだけで対象になってしまう。取得失敗を「信頼できる」と誤判定しない）。
    """
    if not author_association:
        return False
    return author_association.strip().upper() in TRUSTED_AUTHOR_ASSOCIATIONS


def _is_claude_branch(branch: str, author_association: str | None) -> bool:
    """Claude Code が作成したブランチ、かつ著者が信頼できるか（AND 条件・#379）。

    ブランチ名の前方一致だけを見ていた旧実装は、規約どおりの命名（feat/ fix/ docs/ 等）で
    fork から PR を出すだけで自動マージ対象（needs_prompt）になってしまう critical な穴だった。
    authorAssociation（gh --json authorAssociation・REST の author_association）を AND 条件に
    加えることで、ブランチ名が一致していても著者が OWNER/MEMBER/COLLABORATOR でなければ
    対象外にする。
    """
    prefixes = ("claude/", "content/", "feat/", "fix/", "docs/")
    branch_matches = any(branch.startswith(p) for p in prefixes)
    return branch_matches and _is_trusted_author_association(author_association)


def _run_self_test() -> None:
    """Session-Id 解析（純粋関数）の決定論テスト。CI / セルフレビューで実行する。"""
    uid = "ec373723-01dc-54c9-a204-9ebb221b2295"
    cases: list[tuple[str | None, str]] = [
        (f"Sprint Goal: x\nSession-Id: {uid}\nsp:3", uid),
        (f"session-id: {uid.upper()}", uid),  # 大文字小文字・正規化
        (f"前置き\n\nSession-Id:   {uid}   \n後置き", uid),  # 余白許容
        ("Session-Id: not-a-uuid", ""),  # 形式不正 → 空
        ("Sprint Goal のみ・トレーラーなし", ""),
        ("", ""),
        (None, ""),
    ]
    failures = []
    for body, expected in cases:
        got = parse_session_id(body)
        if got != expected:
            failures.append(f"  parse_session_id({body!r}) = {got!r} (expected {expected!r})")
    # current_session_id: 明示指定が env より優先・小文字正規化
    if current_session_id("ABC-123") != "abc-123":
        failures.append("  current_session_id explicit override failed")

    # 著者検証（#379・公開リスク監査 r03 critical）: ブランチ名一致 AND 信頼できる著者
    branch_cases: list[tuple[str, str | None, bool]] = [
        # (branch, authorAssociation, expected) — オーナー/メンバー/コラボレーターの正規 PR
        ("feat/x", "OWNER", True),
        ("fix/y", "MEMBER", True),
        ("docs/z", "COLLABORATOR", True),
        ("claude/session-123", "owner", True),  # 小文字応答も許容
        ("content/V001-x", "Member", True),
        # fork PR を模した入力（ブランチ名は規約どおりだが著者は信頼できない） → 対象外
        ("feat/evil", "CONTRIBUTOR", False),
        ("fix/evil", "FIRST_TIME_CONTRIBUTOR", False),
        ("docs/evil", "NONE", False),
        # authorAssociation 取得不能（gh 応答欠落等） → 安全側（対象外）
        ("feat/unknown", None, False),
        ("feat/unknown", "", False),
        # ブランチ名がそもそも規約に一致しない → 著者が OWNER でも対象外
        ("random-branch", "OWNER", False),
    ]
    for branch, assoc, expected in branch_cases:
        got = _is_claude_branch(branch, assoc)
        if got != expected:
            failures.append(
                f"  _is_claude_branch({branch!r}, {assoc!r}) = {got!r} (expected {expected!r})"
            )

    # 信頼境界そのもののテスト（#379）。_is_claude_branch() 単体のテストだけでは、
    # 判定に至る 3 経路（ブランチ名一致 / AI レビュー痕跡 / 未解決スレッド）のうち
    # 後ろ 2 つが検証を迂回していたことを検出できなかった。ここで境界関数を直接固定する。
    assoc_cases: list[tuple[str | None, bool]] = [
        ("OWNER", True),
        ("MEMBER", True),
        ("COLLABORATOR", True),
        ("owner", True),          # 小文字応答
        ("  MEMBER  ", True),     # 余白
        ("CONTRIBUTOR", False),   # 過去にマージ実績があるだけの外部貢献者
        ("FIRST_TIME_CONTRIBUTOR", False),
        ("FIRST_TIMER", False),
        ("NONE", False),
        ("MANNEQUIN", False),
        ("", False),              # 取得失敗 → fail-closed
        (None, False),
    ]
    for assoc, expected in assoc_cases:
        got = _is_trusted_author_association(assoc)
        if got != expected:
            failures.append(
                f"  _is_trusted_author_association({assoc!r}) = {got!r} (expected {expected!r})"
            )

    # Layer 1 検証の判定ロジック（#462）。I/O から分離した純粋関数なので gh 非依存でテストできる。
    verdict_cases: list[tuple[int, int, int]] = [
        (0, 0, 1),   # 両方0件 → LAYER1_MISSING
        (1, 0, 0),   # レビューのみ1件 → LAYER1_VERIFIED
        (0, 1, 0),   # インラインのみ1件 → LAYER1_VERIFIED
        (3, 5, 0),   # 複数件 → LAYER1_VERIFIED
    ]
    for review_count, inline_count, expected in verdict_cases:
        got = _layer1_verdict(review_count, inline_count)
        if got != expected:
            failures.append(
                f"  _layer1_verdict({review_count!r}, {inline_count!r}) = {got!r} (expected {expected!r})"
            )

    # run_gh() の OSError 捕捉（gh バイナリ不在・実行不能。Issue #53）。subprocess.run を
    # モックして未処理例外にならないこと・critical フラグどおりの挙動になることを検証する。
    run_gh_cases = 0
    with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file or directory")):
        run_gh_cases += 1
        got = run_gh(["pr", "list"], critical=False)
        if got != "":
            failures.append(f"  run_gh(critical=False) with FileNotFoundError = {got!r} (expected '')")
        run_gh_cases += 1
        try:
            run_gh(["pr", "list"], critical=True)
            failures.append("  run_gh(critical=True) with FileNotFoundError did not raise GhUnavailableError")
        except GhUnavailableError:
            pass
        except Exception as e:  # noqa: BLE001 — 未処理例外の再発（#53）を明示的に不合格にする
            failures.append(
                f"  run_gh(critical=True) with FileNotFoundError raised {type(e).__name__} "
                "instead of GhUnavailableError"
            )

    total_cases = (
        len(cases) + 1 + len(branch_cases) + len(assoc_cases) + len(verdict_cases) + run_gh_cases
    )
    if failures:
        print("FAIL: check_pending_pr_reviews self-test", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        sys.exit(1)
    print(f"PASS: check_pending_pr_reviews self-test ({total_cases} cases)")


def main():
    parser = argparse.ArgumentParser(
        description="レビュー待ちPRを検出する",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON形式で出力する",
    )
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help="対応が必要なPRのみ出力する（no_action と active_session=true を除外）",
    )
    parser.add_argument(
        "--include-active",
        action="store_true",
        help="作成セッション活動中（active_session=true）の PR も actionable に含める（デバッグ・強制救済用）",
    )
    parser.add_argument(
        "--mine",
        action="store_true",
        help=(
            "自セッションが作成した PR のみ出力する（PR 本文の Session-Id トレーラーが "
            "$CLAUDE_CODE_SESSION_ID と一致するもの・#47）。自 PR は所有者が常に対応可能なため "
            "active_session 除外を適用しない（時間ベースの穴を埋める積極的所有判定）。"
        ),
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="--mine の照合に使うセッション ID を明示指定する（既定は $CLAUDE_CODE_SESSION_ID）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Session-Id 解析の純粋関数テストを実行して終了する（API 非依存）",
    )
    parser.add_argument(
        "--verify-layer1",
        type=int,
        metavar="PR_NUMBER",
        default=None,
        help=(
            "指定 PR に Layer 1 セルフレビュー（レビュー本体 or 行単位インラインコメント）が "
            "1 件以上投稿されているかをマージ直前に機械検証する（#462）。"
            "終了コード: 0=LAYER1_VERIFIED / 1=LAYER1_MISSING(ブロック) / 2=LAYER1_UNKNOWN(判定不能)"
        ),
    )
    args = parser.parse_args()

    if args.self_test:
        _run_self_test()
        return

    # API を実際に使うパスに入るためここで REPO 形式を検証する（--self-test は API 非依存で除外済み）
    _validate_repo()

    if args.verify_layer1 is not None:
        sys.exit(verify_layer1_review(args.verify_layer1))

    # --mine 利用時はセッション ID が必須（誤って全 PR を自 PR 扱いしないため）
    session_id = current_session_id(args.session_id)
    if args.mine and not session_id:
        print(
            "ERROR: --mine には $CLAUDE_CODE_SESSION_ID または --session-id が必要です "
            "（クラウドセッション外では --session-id <id> を明示してください）",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        prs = get_open_prs()
    except GhUnavailableError as e:
        # クラウドで repo スコープ gh が 403 になる場合など（L-114・Issue #130）。
        # 「0 件」と誤判定させないため exit 0 以外で終了し、専用の機械可読行を出す。
        print(f"ERROR: gh_unavailable: {e}", file=sys.stderr)
        print(
            "GH_UNAVAILABLE: repo スコープの gh が失敗しました。"
            "mcp__github__list_pull_requests(owner, repo, state=\"open\") で直接オープン PR を確認してください。",
        )
        sys.exit(3)

    if not prs:
        if args.json:
            print(json.dumps([], indent=2))
        else:
            print("NO_PENDING_PRS")
        return

    results = []
    for pr in prs:
        result = analyze_pr(pr)
        # is_mine: PR の Session-Id が現セッションと一致するか（#47）
        is_mine = bool(session_id) and result.get("owner_session_id", "") == session_id
        result["is_mine"] = is_mine
        # --mine: 自セッション所有 PR 以外を除外する（積極的所有判定）
        if args.mine and not is_mine:
            continue
        if args.actionable_only and result["status"] in ("no_action", "blocked_waiting_user"):
            continue
        # 作成セッションが現役で対応中の PR には他セッションが介入しない（CP-4・Issue #3007）。
        # ただし自 PR（is_mine）は所有者本人なので active_session 除外を適用しない
        # （自 PR でも 10 分超アイドルで奪われる穴を埋める・#47）。
        if (
            args.actionable_only
            and result["active_session"]
            and not args.include_active
            and not is_mine
        ):
            continue
        results.append(result)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        if not results:
            print("NO_PENDING_PRS")
            return
        for r in results:
            print(f"PENDING:{r['pr_number']}:{r['status']}:{r['summary']} (#{r['pr_number']} {r['title']}, {r['elapsed_min']}分経過)")


if __name__ == "__main__":
    main()
