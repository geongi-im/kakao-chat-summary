"""URL 관련 API."""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/{room_id}/urls")
async def get_urls(room_id: int, list_type: str = "all"):
    """
    채팅방 URL 목록 조회.
    list_type: "recent" | "weekly" | "all"
    """
    from db.database import Database
    from file_storage import FileStorage

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    if list_type not in ("recent", "weekly", "all"):
        raise HTTPException(status_code=400, detail="list_type은 recent/weekly/all 중 하나여야 합니다.")

    storage = FileStorage()
    urls = storage.load_url_list(room.name, list_type)

    return {
        "room_id": room_id,
        "list_type": list_type,
        "count": len(urls),
        "urls": [
            {"url": url, "descriptions": descs}
            for url, descs in urls.items()
        ],
    }


@router.post("/{room_id}/urls/sync")
async def sync_urls(room_id: int):
    """요약 파일에서 URL을 추출하여 DB와 파일에 저장."""
    from db.database import Database
    from file_storage import FileStorage
    from url_extractor import extract_urls_from_text, deduplicate_urls
    from datetime import date, timedelta

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    storage = FileStorage()
    today = date.today()
    three_days_ago = today - timedelta(days=3)
    seven_days_ago = today - timedelta(days=7)

    all_urls: dict = {}
    weekly_urls: dict = {}
    recent_urls: dict = {}

    # 요약된 모든 날짜의 URL 추출
    summarized_dates = storage.get_summarized_dates(room.name)
    for date_str in summarized_dates:
        content = storage.load_daily_summary(room.name, date_str)
        if not content:
            continue
        extracted = extract_urls_from_text(content)
        for url, descs in extracted.items():
            if url not in all_urls:
                all_urls[url] = []
            all_urls[url].extend(descs)

        # 날짜별 분류
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d >= three_days_ago:
            for url, descs in extracted.items():
                if url not in recent_urls:
                    recent_urls[url] = []
                recent_urls[url].extend(descs)
        if d >= seven_days_ago:
            for url, descs in extracted.items():
                if url not in weekly_urls:
                    weekly_urls[url] = []
                weekly_urls[url].extend(descs)

    # 중복 제거 (최근 3일은 최대 50개)
    all_urls = deduplicate_urls(all_urls)
    weekly_urls = deduplicate_urls(weekly_urls)
    recent_urls_dedup = deduplicate_urls(recent_urls)
    # 최근 3일 50개 제한
    recent_items = list(recent_urls_dedup.items())[:50]
    recent_urls_limited = dict(recent_items)

    # 파일 저장
    storage.save_url_lists(room.name, recent_urls_limited, weekly_urls, all_urls)

    # DB 저장
    try:
        db.clear_urls_by_room(room_id)
        db.add_urls_batch(room_id, all_urls)
    except Exception:
        pass

    return {
        "success": True,
        "total": len(all_urls),
        "weekly": len(weekly_urls),
        "recent": len(recent_urls_limited),
    }
