"""채팅방 CRUD API."""
from fastapi import APIRouter, HTTPException, Form

from db.database import get_db

router = APIRouter()


@router.get("")
async def list_rooms():
    db = get_db()
    rooms_with_stats = db.get_all_rooms_with_stats()
    result = []
    for rws in rooms_with_stats:
        room = rws["room"]
        stats = rws["stats"]
        result.append({
            "id": room.id,
            "name": room.name,
            "message_count": stats.get("total_messages", 0),
            "participant_count": stats.get("unique_senders", 0),
            "last_sync_at": room.last_sync_at.isoformat() if room.last_sync_at else None,
            "created_at": room.created_at.isoformat() if room.created_at else None,
        })
    return result


@router.post("", status_code=201)
async def create_room(name: str = Form(...)):
    db = get_db()
    existing = db.get_room_by_name(name)
    if existing:
        raise HTTPException(status_code=400, detail="같은 이름의 채팅방이 이미 존재합니다.")
    room = db.create_room(name)
    return {"id": room.id, "name": room.name}


@router.get("/{room_id}")
async def get_room(room_id: int):
    from file_storage import get_storage

    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    stats = db.get_room_stats(room_id)
    storage = get_storage()
    available_dates = storage.get_available_dates(room.name)
    summarized_dates = storage.get_summarized_dates(room.name)
    return {
        "id": room.id,
        "name": room.name,
        "total_messages": stats.get("total_messages", 0),
        "unique_senders": stats.get("unique_senders", 0),
        "first_date": stats.get("first_date").isoformat() if stats.get("first_date") else None,
        "last_date": stats.get("last_date").isoformat() if stats.get("last_date") else None,
        "last_sync_at": room.last_sync_at.isoformat() if room.last_sync_at else None,
        "total_days": len(available_dates),
        "summarized_days": len(summarized_dates),
    }


@router.delete("/{room_id}")
async def delete_room(room_id: int):
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    db.delete_room(room_id)
    return {"message": f"채팅방 '{room.name}'이 삭제되었습니다."}
