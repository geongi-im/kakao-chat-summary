"""FastAPI 웹 서버 진입점.

실행:
    uvicorn web.main:app --host 0.0.0.0 --port 8000 --reload
"""
import sys
from pathlib import Path

# src/ 모듈을 Python path에 추가 (라우터 임포트 전에 실행되어야 함)
_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.routers import rooms, upload, summaries
from web.routers import urls as urls_router

# ─────────────────────────────────────────
# FastAPI 앱
# ─────────────────────────────────────────
app = FastAPI(
    title="KakaoTalk Chat Summary",
    description="카카오톡 대화 요약 웹 서비스",
    version="1.0.0",
)

# 템플릿 / Static
_base = Path(__file__).parent
_static_dir = _base / "static"
_static_dir.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
templates = Jinja2Templates(directory=str(_base / "templates"))

# ─────────────────────────────────────────
# API 라우터 등록
# ─────────────────────────────────────────
app.include_router(rooms.router, prefix="/api/rooms", tags=["rooms"])
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(summaries.router, prefix="/api/rooms", tags=["summaries"])
app.include_router(urls_router.router, prefix="/api/rooms", tags=["urls"])


# ─────────────────────────────────────────
# 페이지 라우트
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from db.database import Database
    db = Database()
    all_rooms = db.get_all_rooms()
    room_stats = []
    for room in all_rooms:
        stats = db.get_room_stats(room.id)
        room_stats.append({"room": room, "stats": stats})
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "room_stats": room_stats},
    )


@app.get("/rooms/{room_id}", response_class=HTMLResponse)
async def room_detail(request: Request, room_id: int):
    from db.database import Database
    from file_storage import FileStorage

    db = Database()
    room = db.get_room_by_id(room_id)
    if not room:
        return HTMLResponse("<h1>채팅방을 찾을 수 없습니다.</h1>", status_code=404)

    storage = FileStorage()
    stats = db.get_room_stats(room_id)
    available_dates = sorted(storage.get_available_dates(room.name), reverse=True)
    summarized_set = set(storage.get_summarized_dates(room.name))
    dates_info = [
        {"date": d, "has_summary": d in summarized_set}
        for d in available_dates
    ]
    recent_summaries = db.get_summaries_by_room(room_id)[:5]

    return templates.TemplateResponse(
        "room.html",
        {
            "request": request,
            "room": room,
            "stats": stats,
            "dates_info": dates_info,
            "recent_summaries": recent_summaries,
            "summarized_set": summarized_set,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from full_config import LLM_PROVIDERS, config

    providers_info = [
        {
            "key": key,
            "name": p.name,
            "model": p.model,
            "env_key": p.env_key,
            "has_key": bool(config.get_api_key(key)),
        }
        for key, p in LLM_PROVIDERS.items()
    ]
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "providers": providers_info,
            "current_provider": config.current_provider,
        },
    )
