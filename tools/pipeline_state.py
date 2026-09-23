#!/usr/bin/env python3
"""
パイプライン実行状態管理ユーティリティ

content/pipeline-state/{動画ID}.json にチェックポイントを保存し、
セッション中断後の再開をサポートする。

使い方:
  # ステップ完了を記録
  python3 tools/pipeline_state.py --video-id V006 --pipeline script --complete-step 3

  # 現在の状態を確認
  python3 tools/pipeline_state.py --video-id V006 --status

  # 再開ポイントを取得（何ステップ目から再開するか）
  python3 tools/pipeline_state.py --video-id V006 --pipeline script --resume-from

  # パイプライン完了を記録
  python3 tools/pipeline_state.py --video-id V006 --pipeline script --finish

  # パイプラインの初期化（新規開始）
  python3 tools/pipeline_state.py --video-id V006 --pipeline script --init --branch content/V006-script --issue-number 123

  # stale 状態（status=in_progress + finished_at=null + 4時間以上未更新）の検出
  python3 tools/pipeline_state.py --list-stale [--stale-hours 4] [--json]

  # stale 状態の清掃（status=stale_cleaned に遷移 + finished_at 設定）
  python3 tools/pipeline_state.py --video-id V155 --cleanup-stale
  python3 tools/pipeline_state.py --cleanup-all-stale [--stale-hours 4]
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


STATE_DIR = Path(__file__).parent.parent / "content" / "pipeline-state"

# 各パイプラインのステップ定義（再開可能なステップ番号）
PIPELINE_STEPS = {
    "script": ["0", "0.5", "1", "2", "3", "4", "5", "5.5", "5.6", "5.7", "6", "6.5", "7", "8", "9"],
    "audio":  ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"],
    "image":  ["0", "1", "2", "3", "4", "5", "6", "7", "8"],
    "video":  ["0", "1", "1.5", "2", "3"],
}


def _state_path(video_id: str) -> Path:
    return STATE_DIR / f"{video_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(video_id: str) -> dict:
    """状態ファイルを読み込む。存在しない場合は空の dict を返す。"""
    path = _state_path(video_id)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(video_id: str, state: dict) -> None:
    """状態ファイルを保存する。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(video_id)
    state["updated_at"] = _now()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def init_pipeline(video_id: str, pipeline: str, branch: str = "", issue_number: int = 0) -> dict:
    """パイプラインの新規実行を開始する。既存の状態は上書きする。"""
    state = {
        "video_id": video_id,
        "pipeline": pipeline,
        "status": "in_progress",
        "branch": branch,
        "issue_number": issue_number,
        "started_at": _now(),
        "updated_at": _now(),
        "last_completed_step": None,
        "completed_steps": [],
        "outputs": {},
    }
    save_state(video_id, state)
    return state


def complete_step(video_id: str, pipeline: str, step: str, outputs: Optional[dict] = None) -> dict:
    """ステップ完了を記録する。"""
    state = load_state(video_id)
    if not state:
        # 状態ファイルがない場合は自動初期化
        state = init_pipeline(video_id, pipeline)

    # パイプライン種別が変わった場合は completed_steps / finished_at をリセット
    if state.get("pipeline") and state["pipeline"] != pipeline:
        print(f"[INFO] pipeline 変更検知: {state['pipeline']} → {pipeline}. completed_steps をリセット")
        state["completed_steps"] = []
        state["last_completed_step"] = None
        state["finished_at"] = None
        state["started_at"] = _now()

    state["pipeline"] = pipeline
    state["status"] = "in_progress"
    state["last_completed_step"] = step

    if "completed_steps" not in state:
        state["completed_steps"] = []
    if step not in state["completed_steps"]:
        state["completed_steps"].append(step)

    if outputs:
        if "outputs" not in state:
            state["outputs"] = {}
        state["outputs"].update(outputs)

    save_state(video_id, state)
    return state


def finish_pipeline(video_id: str, pipeline: str, outputs: Optional[dict] = None) -> dict:
    """パイプライン完了を記録する。"""
    state = load_state(video_id)
    if not state:
        state = {"video_id": video_id, "pipeline": pipeline}

    # finish 時も pipeline 種別を明示更新する。状態ファイルに古い pipeline が
    # 残っていると pipeline と status が矛盾するため（retro #1784）
    state["pipeline"] = pipeline
    state["status"] = "done"
    state["finished_at"] = _now()
    if outputs:
        if "outputs" not in state:
            state["outputs"] = {}
        state["outputs"].update(outputs)

    save_state(video_id, state)
    return state


DEFAULT_STALE_HOURS = 4


def _parse_iso(ts) -> Optional[datetime]:
    """ISO 文字列を tz-aware datetime にパース。tzinfo なしの場合は UTC を付与する。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_stale_states(stale_hours: int = DEFAULT_STALE_HOURS) -> list[dict]:
    """
    status=in_progress + finished_at=null + 最終更新が stale_hours 時間以上前の
    動画パイプライン状態（V*.json）を返す（#2746・CP-3 衛生）。

    project-sync の 11:00 スロットから呼び、ゾンビ状態を自動検出する。
    動画 ID 以外の log/cache JSON は対象外（V*.json 限定）。
    """
    if not STATE_DIR.exists():
        return []

    threshold = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    stale: list[dict] = []

    for path in sorted(STATE_DIR.glob("V*.json")):
        state = load_state(path.stem)
        if not isinstance(state, dict) or not state:
            continue
        if state.get("status") != "in_progress":
            continue
        if state.get("finished_at"):
            continue

        updated_at = _parse_iso(state.get("updated_at", ""))
        if updated_at is None:
            continue
        if updated_at > threshold:
            continue

        age_hours = (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600
        stale.append({
            "video_id": state.get("video_id", path.stem),
            "pipeline": state.get("pipeline"),
            "branch": state.get("branch", ""),
            "issue_number": state.get("issue_number", 0),
            "last_completed_step": state.get("last_completed_step"),
            "updated_at": state.get("updated_at"),
            "age_hours": round(age_hours, 1),
        })

    return stale


def cleanup_stale_state(video_id: str) -> dict:
    """
    1 件の stale 状態を清掃する。
    既存の状態を破壊せず status="stale_cleaned" + finished_at を設定して終端遷移させる。

    安全側: 真に stale な状態（status=in_progress かつ finished_at が空）のみ清掃する。
    冪等性: 既に done / stale_cleaned のレコードはスキップする。
    """
    state = load_state(video_id)
    if not state:
        return {"video_id": video_id, "cleaned": False, "reason": "state file not found"}
    if not isinstance(state, dict):
        return {"video_id": video_id, "cleaned": False, "reason": "state is not a dict"}

    status = state.get("status")
    if status in ("done", "finished", "stale_cleaned"):
        return {"video_id": video_id, "cleaned": False, "reason": f"already {status}"}
    if status != "in_progress":
        return {"video_id": video_id, "cleaned": False, "reason": f"status={status} (expected in_progress)"}
    if state.get("finished_at"):
        return {"video_id": video_id, "cleaned": False, "reason": "finished_at already set"}

    state["status"] = "stale_cleaned"
    state["finished_at"] = _now()
    save_state(video_id, state)
    return {"video_id": video_id, "cleaned": True, "pipeline": state.get("pipeline")}


def get_resume_step(video_id: str, pipeline: str) -> Optional[str]:
    """
    再開すべきステップを返す。
    - 状態ファイルがない / 完了済み: None（最初から開始）
    - 中断あり: last_completed_step の次のステップ番号
    """
    state = load_state(video_id)
    if not state:
        return None
    if state.get("status") == "done":
        return None
    if state.get("pipeline") != pipeline:
        return None

    last_step = state.get("last_completed_step")
    if last_step is None:
        return "0"  # Step 0 から再開

    steps = PIPELINE_STEPS.get(pipeline, [])
    if last_step not in steps:
        return None  # 不明なステップ → 最初から

    idx = steps.index(last_step)
    if idx + 1 < len(steps):
        return steps[idx + 1]
    return None  # 最終ステップ完了済み


def main():
    parser = argparse.ArgumentParser(description="パイプライン状態管理")
    parser.add_argument("--item-id", "--video-id", dest="video_id", help="処理対象の ID（汎用は --item-id。--video-id は後方互換の別名）。--list-stale / --cleanup-all-stale 以外で必須")
    parser.add_argument("--pipeline", help="パイプライン名（プロジェクトで定義。例: build/test/deploy）")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--init", action="store_true", help="パイプラインを新規開始")
    action.add_argument("--complete-step", metavar="STEP", help="ステップ完了を記録（例: 3）")
    action.add_argument("--finish", action="store_true", help="パイプライン完了を記録")
    action.add_argument("--status", action="store_true", help="現在の状態を表示")
    action.add_argument("--resume-from", action="store_true", help="再開ポイントを表示")
    action.add_argument("--list-stale", action="store_true", help="stale 状態のレコードを一覧表示（#2746）")
    action.add_argument("--cleanup-stale", action="store_true", help="指定 video-id の stale 状態を清掃")
    action.add_argument("--cleanup-all-stale", action="store_true", help="全ての stale 状態を清掃")

    parser.add_argument("--branch", default="", help="作業ブランチ名（--init 時）")
    parser.add_argument("--issue-number", type=int, default=0, help="Issue番号（--init 時）")
    parser.add_argument("--outputs", default="{}", help="追加出力情報（JSON文字列）")
    parser.add_argument("--stale-hours", type=int, default=DEFAULT_STALE_HOURS,
                        help=f"stale と判定する経過時間（時間・デフォルト {DEFAULT_STALE_HOURS}）")
    parser.add_argument("--json", action="store_true", help="JSON 形式で出力（--list-stale 時）")

    args = parser.parse_args()

    if not args.video_id and not (args.list_stale or args.cleanup_all_stale):
        parser.error("--item-id（別名 --video-id）が必要です（--list-stale / --cleanup-all-stale 以外）")

    try:
        outputs = json.loads(args.outputs)
    except json.JSONDecodeError as e:
        print(f"エラー: --outputs に指定された文字列は有効な JSON ではありません: {args.outputs}", file=sys.stderr)
        print(f"詳細: {e}", file=sys.stderr)
        sys.exit(1)

    if args.status:
        state = load_state(args.video_id)
        if state:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(f"状態ファイルなし: {args.video_id}")
        sys.exit(0)

    if args.resume_from:
        if not args.pipeline:
            print("エラー: --resume-from には --pipeline が必要です", file=sys.stderr)
            sys.exit(1)
        step = get_resume_step(args.video_id, args.pipeline)
        if step is None:
            print("NONE")  # 最初から開始
        else:
            print(step)
        sys.exit(0)

    if args.init:
        if not args.pipeline:
            print("エラー: --init には --pipeline が必要です", file=sys.stderr)
            sys.exit(1)
        state = init_pipeline(args.video_id, args.pipeline, args.branch, args.issue_number)
        print(f"初期化完了: {args.video_id} / {args.pipeline}")
        sys.exit(0)

    if args.complete_step:
        if not args.pipeline:
            print("エラー: --complete-step には --pipeline が必要です", file=sys.stderr)
            sys.exit(1)
        state = complete_step(args.video_id, args.pipeline, args.complete_step, outputs)
        print(f"Step {args.complete_step} 完了を記録: {args.video_id}")
        sys.exit(0)

    if args.finish:
        if not args.pipeline:
            print("エラー: --finish には --pipeline が必要です", file=sys.stderr)
            sys.exit(1)
        state = finish_pipeline(args.video_id, args.pipeline, outputs)
        print(f"パイプライン完了を記録: {args.video_id} / {args.pipeline}")
        sys.exit(0)

    if args.list_stale:
        stale = find_stale_states(args.stale_hours)
        if args.json:
            print(json.dumps(stale, ensure_ascii=False, indent=2))
        else:
            if not stale:
                print(f"stale 状態なし（閾値 {args.stale_hours}h）")
            else:
                print(f"stale 状態 {len(stale)} 件（閾値 {args.stale_hours}h）:")
                for s in stale:
                    print(f"  - {s['video_id']} / {s['pipeline']} / "
                          f"last_step={s['last_completed_step']} / "
                          f"age={s['age_hours']}h / branch='{s['branch']}'")
        sys.exit(0)

    if args.cleanup_stale:
        result = cleanup_stale_state(args.video_id)
        if result["cleaned"]:
            print(f"清掃完了: {args.video_id} / {result.get('pipeline')} → stale_cleaned")
            sys.exit(0)
        else:
            print(f"清掃スキップ: {args.video_id}（{result['reason']}）", file=sys.stderr)
            sys.exit(1)

    if args.cleanup_all_stale:
        stale = find_stale_states(args.stale_hours)
        if not stale:
            print(f"stale 状態なし（閾値 {args.stale_hours}h）")
            sys.exit(0)
        cleaned = 0
        for s in stale:
            result = cleanup_stale_state(s["video_id"])
            if result["cleaned"]:
                cleaned += 1
                print(f"清掃: {s['video_id']} / {s['pipeline']} (age={s['age_hours']}h)")
            else:
                print(f"清掃スキップ: {s['video_id']}（理由: {result['reason']}）", file=sys.stderr)
        print(f"合計 {cleaned}/{len(stale)} 件清掃完了")
        sys.exit(0)


if __name__ == "__main__":
    main()
