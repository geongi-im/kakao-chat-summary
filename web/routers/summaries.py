"""요약 API (조회 + 비동기 LLM 생성)."""
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.task_state import (
    get_task, set_task, update_task, clear_task,
    is_running, get_cancel_flag,
)

router = APIRouter()


class SummarizeRequest(BaseModel):
    provider: str = "glm"
    # 날짜 옵션: "pending" | "today" | "yesterday" | "2days" | "all"
    date_range: str = "pending"
    skip_existing: bool = True


# ────────────────────────────────────────────────────────────
# 조회
# ────────────────────────────────────────────────────────────

@router.get("/{room_id}/summaries")
async def list_summaries(room_id: int):
    from db.database import Database
    from file_storage import FileStorage

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    storage = FileStorage()
    available = storage.get_available_dates(room.name)
    summarized = set(storage.get_summarized_dates(room.name))

    return {
        "room_id": room_id,
        "room_name": room.name,
        "dates": [
            {"date": d, "has_summary": d in summarized}
            for d in sorted(available, reverse=True)
        ],
    }


@router.get("/{room_id}/summaries/{date_str}")
async def get_summary(room_id: int, date_str: str):
    from db.database import Database
    from file_storage import FileStorage

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    storage = FileStorage()
    content = storage.load_daily_summary(room.name, date_str)
    if content is None:
        raise HTTPException(status_code=404, detail="해당 날짜의 요약이 없습니다.")

    return {"date": date_str, "content": content}


# ────────────────────────────────────────────────────────────
# 생성 (비동기)
# ────────────────────────────────────────────────────────────

@router.post("/{room_id}/summarize")
async def start_summarize(room_id: int, req: SummarizeRequest):
    from db.database import Database

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    if is_running(room_id):
        raise HTTPException(status_code=409, detail="이미 요약이 진행 중입니다.")

    # 대상 날짜 결정
    target_dates = _get_target_dates(room.name, req.date_range, req.skip_existing)
    if not target_dates:
        return {"status": "skipped", "message": "요약할 날짜가 없습니다.", "dates": []}

    # 작업 등록
    set_task(room_id, {
        "status": "running",
        "current": 0,
        "total": len(target_dates),
        "current_date": None,
        "error": None,
        "started_at": datetime.now().isoformat(),
        "cancel_requested": False,
    })

    # 백그라운드 스레드 시작
    thread = threading.Thread(
        target=_run_summarize,
        args=(room_id, room.name, target_dates, req.provider),
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "total": len(target_dates),
        "dates": target_dates,
    }


@router.get("/{room_id}/summarize/status")
async def get_summarize_status(room_id: int):
    task = get_task(room_id)
    if task is None:
        return {"status": "idle"}
    return task


@router.delete("/{room_id}/summarize")
async def cancel_summarize(room_id: int):
    if not is_running(room_id):
        raise HTTPException(status_code=404, detail="진행 중인 요약 작업이 없습니다.")
    update_task(room_id, cancel_requested=True)
    return {"message": "취소 요청이 전달되었습니다."}


# ────────────────────────────────────────────────────────────
# 내부 로직
# ────────────────────────────────────────────────────────────

def _get_target_dates(room_name: str, date_range: str, skip_existing: bool):
    from file_storage import FileStorage
    from datetime import date, timedelta

    storage = FileStorage()
    today = date.today()

    if date_range == "pending":
        dates_dict = storage.get_dates_needing_summary(room_name)
        dates = sorted(dates_dict.keys())
    elif date_range == "today":
        dates = [today.isoformat()]
    elif date_range == "yesterday":
        dates = [(today - timedelta(days=1)).isoformat(), today.isoformat()]
    elif date_range == "2days":
        dates = [
            (today - timedelta(days=2)).isoformat(),
            (today - timedelta(days=1)).isoformat(),
            today.isoformat(),
        ]
    else:  # "all"
        dates = sorted(storage.get_available_dates(room_name))

    if skip_existing and date_range != "pending":
        summarized = set(storage.get_summarized_dates(room_name))
        dates = [d for d in dates if d not in summarized]

    # 원본 파일이 실제로 있는 날짜만
    return [d for d in dates if storage.load_daily_original(room_name, d)]


def _run_summarize(room_id: int, room_name: str, dates: list, provider: str):
    """백그라운드 스레드에서 LLM 요약 실행."""
    from db.database import Database
    from file_storage import FileStorage
    from chat_processor import ChatProcessor

    worker_db = Database()
    storage = FileStorage()
    processor = ChatProcessor(provider)

    try:
        for i, date_str in enumerate(dates):
            if get_cancel_flag(room_id):
                update_task(room_id, status="cancelled", current=i)
                return

            update_task(room_id, current=i, current_date=date_str)

            # 원본 로드
            lines = storage.load_daily_original(room_name, date_str)
            if not lines:
                continue
            text = "\n".join(lines)

            # LLM 요약
            summary_content = processor.process_summary(text)
            if summary_content.startswith("[ERROR]"):
                update_task(room_id,
                            status="failed",
                            error=f"{date_str}: {summary_content}",
                            current=i)
                return

            # 파일 저장
            storage.save_daily_summary(room_name, date_str, summary_content, provider)

            # DB 저장 (기존 삭제 후 재저장)
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                worker_db.delete_summary(room_id, target_date)
                worker_db.add_summary(
                    room_id, target_date, "daily", summary_content, provider
                )
            except Exception:
                pass

        update_task(room_id,
                    status="completed",
                    current=len(dates),
                    current_date=None)

    except Exception as e:
        update_task(room_id, status="failed", error=str(e))
    finally:
        try:
            worker_db.engine.dispose()
        except Exception:
            pass
