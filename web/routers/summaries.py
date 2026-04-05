"""요약 API (조회 + 비동기 LLM 생성)."""
import logging
import threading
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from db.database import get_db
from file_storage import get_storage

from web.task_state import (
    get_task, set_task, update_task, clear_task,
    is_running, get_cancel_flag,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SummarizeRequest(BaseModel):
    provider: str = "glm"
    # 날짜 옵션: "pending" | "today" | "yesterday" | "2days" | "all" | "custom"
    date_range: str = "pending"
    skip_existing: bool = True
    custom_dates: Optional[List[str]] = None  # 날짜 직접 선택 시 사용


# ────────────────────────────────────────────────────────────
# 조회
# ────────────────────────────────────────────────────────────

@router.get("/{room_id}/summaries")
async def list_summaries(room_id: int):
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    storage = get_storage()
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
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    storage = get_storage()
    content = storage.load_daily_summary(room.name, date_str)
    if content is None:
        raise HTTPException(status_code=404, detail="해당 날짜의 요약이 없습니다.")

    return {"date": date_str, "content": content}


# ────────────────────────────────────────────────────────────
# 생성 (비동기)
# ────────────────────────────────────────────────────────────

@router.post("/{room_id}/summarize")
async def start_summarize(room_id: int, req: SummarizeRequest):
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    if is_running(room_id):
        raise HTTPException(status_code=409, detail="이미 요약이 진행 중입니다.")

    # 대상 날짜 결정
    target_dates = _get_target_dates(room.name, req.date_range, req.skip_existing, req.custom_dates)
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

def _get_target_dates(room_name: str, date_range: str, skip_existing: bool, custom_dates: list = None):
    from file_storage import FileStorage
    from datetime import date, timedelta
    from typing import List, Optional

    storage = get_storage()
    today = date.today()

    if date_range == "pending":
        dates_dict = storage.get_dates_needing_summary(room_name)
        dates = sorted(dates_dict.keys())
    elif date_range == "custom" and custom_dates:
        dates = custom_dates
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

    if skip_existing and date_range != "pending" and date_range != "custom":
        summarized = set(storage.get_summarized_dates(room_name))
        dates = [d for d in dates if d not in summarized]

    # 원본 파일이 실제로 있는 날짜만
    return [d for d in dates if storage.load_daily_original(room_name, d)]


def _run_summarize(room_id: int, room_name: str, dates: list, provider: str):
    """백그라운드 스레드에서 LLM 요약 실행."""
    from db.database import Database
    from file_storage import FileStorage
    from chat_processor import ChatProcessor

    # Background thread needs its own DB instance to avoid thread-safety issues
    worker_db = Database()
    storage = get_storage()
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
                logger.warning("Failed to save summary to DB for room=%s date=%s", room_id, date_str, exc_info=True)

        update_task(room_id,
                    status="completed",
                    current=len(dates),
                    current_date=None)

    except Exception as e:
        logger.error("Summarize task failed for room=%s: %s", room_id, e, exc_info=True)
        update_task(room_id, status="failed", error=str(e))
    finally:
        try:
            worker_db.engine.dispose()
        except Exception:
            pass


@router.put("/{room_id}/summaries/{date_str}/read")
async def toggle_summary_read(room_id: int, date_str: str, request: Request):
    """요약 읽음 상태 토글"""
    # 요청 본문 파싱
    body = await request.json()
    is_read = body.get('is_read', True)
    
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    from datetime import datetime
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    # DB 업데이트
    success = db.update_summary_read_status(room_id, target_date, is_read)
    
    if not success:
        raise HTTPException(status_code=404, detail="요약을 찾을 수 없습니다.")
    
    return {"room_id": room_id, "date": date_str, "is_read": is_read}


@router.get("/{room_id}/summaries/{date_str}/read-status")
async def get_summary_read_status(room_id: int, date_str: str):
    """요약 읽음 상태 조회"""
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    from datetime import datetime
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    is_read = db.get_summary_read_status(room_id, target_date)
    
    return {"room_id": room_id, "date": date_str, "is_read": is_read}


@router.delete("/{room_id}/summaries/{date_str}")
async def delete_summary(room_id: int, date_str: str):
    """특정 날짜의 요약과 원본 메시지 모두 삭제"""
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    from datetime import datetime
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    # DB에서 요약 삭제
    summary_deleted = db.delete_summary(room_id, target_date)
    
    # DB에서 원본 메시지 삭제
    messages_deleted = db.delete_messages_by_date(room_id, target_date)
    
    # 파일도 삭제
    try:
        storage = get_storage()
        storage.delete_daily_summary(room.name, date_str)
        storage.delete_daily_original(room.name, date_str)
    except Exception:
        logger.warning("Failed to delete summary/original files for room=%s date=%s", room.name, date_str, exc_info=True)
    
    return {
        "room_id": room_id, 
        "date": date_str, 
        "deleted": True,
        "messages_deleted": messages_deleted
    }


@router.put("/{room_id}/dates/{date_str}/hidden")
async def toggle_date_hidden(room_id: int, date_str: str, request: Request):
    """특정 날짜 숨김/보이기 토글"""
    body = await request.json()
    hidden = body.get('hidden', True)
    
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    from datetime import datetime
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    updated = db.set_date_hidden(room_id, target_date, hidden)
    
    return {
        "room_id": room_id,
        "date": date_str,
        "is_hidden": hidden,
        "messages_updated": updated,
    }


@router.get("/{room_id}/dates/{date_str}/hidden")
async def get_date_hidden_status(room_id: int, date_str: str):
    """특정 날짜 숨김 상태 조회"""
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    from datetime import datetime
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    is_hidden = db.is_date_hidden(room_id, target_date)
    
    return {"room_id": room_id, "date": date_str, "is_hidden": is_hidden}


@router.get("/{room_id}/hidden-dates")
async def list_hidden_dates(room_id: int):
    """숨겨진 날짜 목록 조회"""
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    
    hidden = db.get_hidden_dates(room_id)
    return {"room_id": room_id, "hidden_dates": hidden}
