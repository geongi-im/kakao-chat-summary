"""파일 업로드 API."""
import re
import tempfile
from datetime import datetime, date as date_type, time as time_type
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

router = APIRouter()


# MessageParser (main_window.py에서 복사, GUI 의존성 없음)
class _MessageParser:
    MSG_PATTERN = re.compile(
        r'\[(.*?)\]\s*\[(오전|오후)\s*(\d{1,2}):(\d{2})\]\s*(.*)', re.DOTALL
    )

    @classmethod
    def parse(cls, line: str, msg_date: date_type):
        match = cls.MSG_PATTERN.match(line)
        if not match:
            return None
        sender = match.group(1)
        am_pm = match.group(2)
        hour = int(match.group(3))
        minute = int(match.group(4))
        content = match.group(5)

        if am_pm == "오후" and hour != 12:
            hour += 12
        elif am_pm == "오전" and hour == 12:
            hour = 0

        return {
            "sender": sender,
            "content": content,
            "date": msg_date,
            "time": time_type(hour, minute),
            "raw_line": line,
        }


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    room_name: str = Form(...),
    create_room: bool = Form(default=True),
):
    from db.database import Database
    from file_storage import FileStorage
    from parser import KakaoLogParser

    if not file.filename:
        raise HTTPException(status_code=400, detail="파일이 없습니다.")

    db = Database()
    storage = FileStorage()
    parser = KakaoLogParser()

    # 채팅방 조회 또는 생성
    room = db.get_room_by_name(room_name)
    if not room:
        if create_room:
            room = db.create_room(room_name)
        else:
            raise HTTPException(
                status_code=404,
                detail="채팅방을 찾을 수 없습니다. 먼저 채팅방을 만드세요.",
            )

    # 임시 파일 저장 후 파싱
    content_bytes = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp.write(content_bytes)
        tmp_path = Path(tmp.name)

    try:
        parse_result = parser.parse(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not parse_result.messages_by_date:
        raise HTTPException(
            status_code=400,
            detail="파싱된 메시지가 없습니다. 카카오톡 내보내기 파일인지 확인해주세요.",
        )

    # 기존 파일 크기 기록
    old_sizes = {
        date_str: storage.get_original_file_size(room_name, date_str)
        for date_str in parse_result.messages_by_date
    }

    # 원본 파일 저장
    storage.save_all_daily_originals(room_name, parse_result.messages_by_date)

    # 요약 무효화 체크 + DB 저장
    total_messages = 0
    new_messages = 0
    invalidated = []

    for date_str, lines in parse_result.messages_by_date.items():
        msg_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # 파일 크기 변경 시 요약 무효화
        new_size = storage.get_original_file_size(room_name, date_str)
        if storage.invalidate_summary_if_file_changed(
            room_name, date_str, old_sizes.get(date_str, 0), new_size
        ):
            invalidated.append(date_str)

        # 메시지 파싱 & DB 저장
        msg_dicts = [
            parsed
            for line in lines
            if (parsed := _MessageParser.parse(line, msg_date)) is not None
        ]
        if msg_dicts:
            total_messages += len(msg_dicts)
            try:
                new_messages += db.add_messages(room.id, msg_dicts)
            except Exception:
                pass

    # 동기화 로그
    try:
        db.update_room_sync_time(room.id)
        db.add_sync_log(
            room.id,
            "success",
            message_count=total_messages,
            new_message_count=new_messages,
        )
    except Exception:
        pass

    return {
        "success": True,
        "room_id": room.id,
        "room_name": room_name,
        "dates_processed": len(parse_result.messages_by_date),
        "total_messages": total_messages,
        "new_messages": new_messages,
        "invalidated_summaries": len(invalidated),
    }
