"""FastAPI 웹 서버 진입점.

실행:
    uvicorn web.main:app --host 127.0.0.1 --port 8000 --reload
"""
import os
import sys
import logging
from pathlib import Path

# src/ 모듈을 Python path에 추가 (라우터 임포트 전에 실행되어야 함)
_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# env.local/.env.local 로드를 full_config import에서 처리하므로,
# 다른 모듈(db 등)보다 먼저 import해야 환경변수가 세팅됨
import full_config  # noqa: F401

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from db.database import get_db
from file_storage import FileStorage, get_storage

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# FastAPI 앱
# ─────────────────────────────────────────
app = FastAPI(
    title="KakaoTalk Chat Summary",
    description="카카오톡 대화 요약 웹 서비스",
    version="1.0.0",
)

# ─────────────────────────────────────────
# CORS
# ─────────────────────────────────────────
_cors_origins = os.getenv("CORS_ORIGINS", "")
if _cors_origins:
    _origins_list = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    _origins_list = []

if _origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ─────────────────────────────────────────
# API Key middleware (optional)
# ─────────────────────────────────────────
_WEB_API_KEY = os.getenv("WEB_API_KEY")


class APIKeyMiddleware(BaseHTTPMiddleware):
    """X-API-Key header middleware. Skipped when WEB_API_KEY is not set."""

    async def dispatch(self, request: Request, call_next):
        if _WEB_API_KEY:
            api_key = request.headers.get("X-API-Key")
            if api_key != _WEB_API_KEY:
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)


app.add_middleware(APIKeyMiddleware)

# ─────────────────────────────────────────
# Upload size limit middleware (100 MB)
# ─────────────────────────────────────────
_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


class UploadSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than _MAX_UPLOAD_SIZE."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_UPLOAD_SIZE:
            return Response(status_code=413, content="Payload too large (max 100 MB)")
        return await call_next(request)


app.add_middleware(UploadSizeLimitMiddleware)

# ─────────────────────────────────────────
# Dependency injection helpers
# ─────────────────────────────────────────
def db_dep():
    return get_db()


def storage_dep():
    return get_storage()


# 템플릿 / Static
_base = Path(__file__).parent
_static_dir = _base / "static"
_static_dir.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
templates = Jinja2Templates(directory=str(_base / "templates"))

# ─────────────────────────────────────────
# API 라우터 등록
# ─────────────────────────────────────────
from web.routers import rooms, upload, summaries
from web.routers import urls as urls_router
from pydantic import BaseModel as PydanticModel

class UpdateModelRequest(PydanticModel):
    provider: str
    model: str = ""  # 빈 값이면 기본 모델로 복구


@app.put("/api/settings/model")
async def update_model(req: UpdateModelRequest):
    from full_config import LLM_PROVIDERS, config
    if req.provider not in LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 제공자: {req.provider}")
    try:
        config.set_custom_model(req.provider, req.model)
        effective = config.get_provider_info(req.provider).model
        return {
            "success": True,
            "provider": req.provider,
            "effective_model": effective,
            "message": f"모델이 '{effective}'(으)로 변경되었습니다.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

app.include_router(rooms.router, prefix="/api/rooms", tags=["rooms"])
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(summaries.router, prefix="/api/rooms", tags=["summaries"])
app.include_router(urls_router.router, prefix="/api/rooms", tags=["urls"])


# ─────────────────────────────────────────
# 페이지 라우트
# ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    db = get_db()

    rooms_with_stats = db.get_all_rooms_with_stats()

    room_stats = []
    for rws in rooms_with_stats:
        room = rws["room"]
        stats = rws["stats"]

        # 총 일수 계산 (메시지가 있는 날짜)
        available_dates = db.get_available_dates_for_room(room.id)
        total_days = len(available_dates)

        # 안읽은 일수 계산 (요약이 있고 is_read=False인 것)
        summaries = db.get_summaries_by_room(room.id)
        unread_days = sum(1 for s in summaries if not s.is_read)

        # 채팅방 생성일
        created_date = room.created_at.strftime("%Y-%m-%d") if room.created_at else None

        room_stats.append({
            "room": room,
            "stats": stats,
            "total_days": total_days,
            "unread_days": unread_days,
            "created_date": created_date,
        })

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "room_stats": room_stats},
    )


@app.get("/rooms/{room_id}", response_class=HTMLResponse)
async def room_detail(request: Request, room_id: int):
    db = get_db()
    room = db.get_room_by_id(room_id)
    if not room:
        return HTMLResponse("<h1>채팅방을 찾을 수 없습니다.</h1>", status_code=404)

    storage = get_storage()
    stats = db.get_room_stats(room_id)
    # DB에서 실제 메시지가 있는 날짜만 가져옴 (숨김 날짜 제외)
    available_dates = db.get_available_dates_for_room(room_id)
    hidden_dates = db.get_hidden_dates(room_id)
    summarized_set = set(storage.get_summarized_dates(room.name))
    # 요약된 날짜의 is_read 상태를 빠르게 조회하기 위해 딕셔너리 생성
    summaries = db.get_summaries_by_room(room_id)
    summaries_dict = {s.summary_date.isoformat(): s.is_read for s in summaries}
    
    dates_info = [
        {"date": d, "has_summary": d in summarized_set, "is_read": summaries_dict.get(d) if d in summarized_set else None}
        for d in available_dates
    ]
    recent_summaries = summaries[:5]
    unread_count = sum(1 for s in summaries if not s.is_read)

    return templates.TemplateResponse(
        "room.html",
        {
            "request": request,
            "room": room,
            "stats": stats,
            "dates_info": dates_info,
            "hidden_dates": hidden_dates,
            "recent_summaries": recent_summaries,
            "summarized_set": summarized_set,
            "unread_count": unread_count,
            "room_created": room.created_at.strftime("%Y-%m-%d") if room.created_at else None,
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
            "custom_model": config.get_custom_model(key),
            "effective_model": config.get_provider_info(key).model,
            "available_models": config.PROVIDER_MODELS.get(key, []),
            "env_key": p.env_key,
            "has_key": bool(config.get_api_key(key)),
        }
        for key, p in LLM_PROVIDERS.items()
    ]

    # 숨김 관리를 위한 채팅방 정보
    db = get_db()
    rooms = db.get_all_rooms()
    hidden_data = []
    for room in rooms:
        hidden_dates = db.get_hidden_dates(room.id)
        all_dates = db.get_all_dates_for_room(room.id)
        hidden_data.append({
            "room_id": room.id,
            "room_name": room.name,
            "hidden_dates": hidden_dates,
            "total_dates": len(all_dates),
            "hidden_count": len(hidden_dates),
        })

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "providers": providers_info,
            "current_provider": config.current_provider,
            "hidden_data": hidden_data,
        },
    )
