"""Database connection and session management."""
import os
from pathlib import Path
from datetime import datetime, date, time
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, Session

from .models import Base, ChatRoom, Message, Summary, SyncLog, URL


class Database:
    """데이터베이스 관리 클래스 (SQLite/PostgreSQL 지원)."""

    def __init__(self, db_path: Optional[str] = None):
        # 환경 변수에서 DATABASE_URL 확인 (PostgreSQL 우선)
        database_url = os.getenv('DATABASE_URL')

        if database_url:
            # PostgreSQL 사용
            self.engine = create_engine(
                database_url,
                echo=False,
                pool_pre_ping=True,  # 연결 확인 후 사용
                pool_size=5,
                max_overflow=10
            )
            self.db_path = None  # PostgreSQL은 파일 경로 없음
            self.db_type = "postgresql"
        else:
            # SQLite 사용 (기존 방식)
            if db_path is None:
                # 기본 경로: 프로젝트 루트의 data/db/chat_history.db
                project_root = Path(__file__).parent.parent.parent
                db_dir = project_root / "data" / "db"
                db_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(db_dir / "chat_history.db")

            self.db_path = db_path
            self.db_type = "sqlite"

            # SQLite 최적화 설정
            self.engine = create_engine(
                f"sqlite:///{db_path}",
                echo=False,
                connect_args={
                    "check_same_thread": False,
                    "timeout": 30
                }
            )

            # WAL 모드 및 성능 최적화
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA cache_size=10000")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        # 테이블 생성
        Base.metadata.create_all(self.engine)
    
    @contextmanager
    def get_session(self):
        """세션 컨텍스트 매니저."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    
    # ==================== ChatRoom 관련 ====================
    
    def create_room(self, name: str, file_path: Optional[str] = None) -> ChatRoom:
        """새 채팅방 생성."""
        with self.get_session() as session:
            room = ChatRoom(name=name, file_path=file_path)
            session.add(room)
            session.flush()
            # detached 객체 반환을 위해 속성 복사
            return ChatRoom(
                id=room.id, name=room.name, file_path=room.file_path,
                last_sync_at=room.last_sync_at, created_at=room.created_at
            )
    
    def get_room_by_id(self, room_id: int) -> Optional[ChatRoom]:
        """ID로 채팅방 조회."""
        with self.get_session() as session:
            room = session.query(ChatRoom).filter(ChatRoom.id == room_id).first()
            if room:
                return ChatRoom(
                    id=room.id, name=room.name, file_path=room.file_path,
                    last_sync_at=room.last_sync_at, created_at=room.created_at
                )
            return None
    
    def get_room_by_name(self, name: str) -> Optional[ChatRoom]:
        """이름으로 채팅방 조회."""
        with self.get_session() as session:
            room = session.query(ChatRoom).filter(ChatRoom.name == name).first()
            if room:
                return ChatRoom(
                    id=room.id, name=room.name, file_path=room.file_path,
                    last_sync_at=room.last_sync_at, created_at=room.created_at
                )
            return None
    
    def get_all_rooms(self) -> List[ChatRoom]:
        """모든 채팅방 조회 (메시지 개수 내림차순 정렬)."""
        with self.get_session() as session:
            # 메시지 개수로 정렬하기 위한 서브쿼리
            from sqlalchemy import select
            msg_count_subq = (
                select(Message.room_id, func.count(Message.id).label('msg_count'))
                .group_by(Message.room_id)
                .subquery()
            )
            
            # 채팅방과 메시지 개수를 조인하여 정렬
            rooms = (
                session.query(ChatRoom)
                .outerjoin(msg_count_subq, ChatRoom.id == msg_count_subq.c.room_id)
                .order_by(func.coalesce(msg_count_subq.c.msg_count, 0).desc())
                .all()
            )
            
            return [
                ChatRoom(
                    id=r.id, name=r.name, file_path=r.file_path,
                    last_sync_at=r.last_sync_at, created_at=r.created_at
                ) for r in rooms
            ]
    
    def update_room_sync_time(self, room_id: int):
        """채팅방 동기화 시간 업데이트."""
        with self.get_session() as session:
            room = session.query(ChatRoom).filter(ChatRoom.id == room_id).first()
            if room:
                room.last_sync_at = datetime.now()
    
    def delete_room(self, room_id: int):
        """채팅방 삭제 (연관 데이터 포함)."""
        with self.get_session() as session:
            room = session.query(ChatRoom).filter(ChatRoom.id == room_id).first()
            if room:
                session.delete(room)
    
    # ==================== Message 관련 ====================
    
    def add_messages(self, room_id: int, messages: List[Dict[str, Any]], batch_size: int = 500) -> int:
        """메시지 일괄 추가 (중복 무시, 배치 처리). INSERT OR IGNORE 방식."""
        added_count = 0

        # 배치 단위로 처리
        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]

            with self.get_session() as session:
                if self.db_type == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                    for msg_data in batch:
                        try:
                            stmt = sqlite_insert(Message).values(
                                room_id=room_id,
                                sender=msg_data['sender'],
                                content=msg_data.get('content'),
                                message_date=msg_data['date'],
                                message_time=msg_data.get('time'),
                                raw_line=msg_data.get('raw_line'),
                            )
                            stmt = stmt.on_conflict_do_nothing(
                                constraint='uq_message_unique'
                            )
                            result = session.execute(stmt)
                            if result.rowcount > 0:
                                added_count += 1
                        except Exception:
                            continue
                else:
                    # PostgreSQL fallback: per-message SELECT check
                    for msg_data in batch:
                        try:
                            existing = session.query(Message).filter(
                                Message.room_id == room_id,
                                Message.sender == msg_data['sender'],
                                Message.message_date == msg_data['date'],
                                Message.message_time == msg_data.get('time'),
                                Message.content == msg_data.get('content')
                            ).first()

                            if existing is None:
                                msg = Message(
                                    room_id=room_id,
                                    sender=msg_data['sender'],
                                    content=msg_data.get('content'),
                                    message_date=msg_data['date'],
                                    message_time=msg_data.get('time'),
                                    raw_line=msg_data.get('raw_line')
                                )
                                session.add(msg)
                                added_count += 1
                        except Exception:
                            continue

        return added_count
    
    def get_messages_by_room(self, room_id: int, 
                             start_date: Optional[date] = None,
                             end_date: Optional[date] = None) -> List[Message]:
        """채팅방의 메시지 조회."""
        with self.get_session() as session:
            query = session.query(Message).filter(Message.room_id == room_id)
            if start_date:
                query = query.filter(Message.message_date >= start_date)
            if end_date:
                query = query.filter(Message.message_date <= end_date)
            return query.order_by(Message.message_date, Message.message_time).all()
    
    def get_message_count_by_room(self, room_id: int) -> int:
        """채팅방의 메시지 수 조회."""
        with self.get_session() as session:
            return session.query(func.count(Message.id)).filter(
                Message.room_id == room_id
            ).scalar()
    
    def get_message_count_by_date(self, room_id: int, target_date: date) -> int:
        """특정 날짜의 메시지 수 조회."""
        with self.get_session() as session:
            return session.query(func.count(Message.id)).filter(
                Message.room_id == room_id,
                Message.message_date == target_date
            ).scalar()
    

    def delete_messages_by_date(self, room_id: int, target_date: date) -> int:
        """특정 날짜의 모든 메시지 삭제."""
        with self.get_session() as session:
            deleted = session.query(Message).filter(
                Message.room_id == room_id,
                Message.message_date == target_date
            ).delete()
            return deleted

    def get_unique_senders(self, room_id: int) -> List[str]:
        """채팅방의 참여자 목록 조회."""
        with self.get_session() as session:
            results = session.query(Message.sender).filter(
                Message.room_id == room_id
            ).distinct().all()
            return [r[0] for r in results]

    def get_available_dates_for_room(self, room_id: int) -> List[str]:
        """채팅방의 실제 메시지가 있는 날짜 목록 (DB 기반, 숨김 날짜 제외)."""
        with self.get_session() as session:
            dates = session.query(Message.message_date).filter(
                Message.room_id == room_id,
                Message.is_hidden == 0
            ).distinct().order_by(Message.message_date.desc()).all()
            # SQLAlchemy Row 객체에서 date 객체 추출
            return [row[0].isoformat() for row in dates]

    def get_all_dates_for_room(self, room_id: int) -> List[str]:
        """숨김 여부 상관없이 모든 날짜 목록."""
        with self.get_session() as session:
            dates = session.query(Message.message_date).filter(
                Message.room_id == room_id
            ).distinct().order_by(Message.message_date.desc()).all()
            return [row[0].isoformat() for row in dates]

    def get_hidden_dates(self, room_id: int) -> List[str]:
        """숨겨진 날짜 목록 조회."""
        with self.get_session() as session:
            dates = session.query(Message.message_date).filter(
                Message.room_id == room_id,
                Message.is_hidden == 1
            ).distinct().order_by(Message.message_date.desc()).all()
            return [row[0].isoformat() for row in dates]

    def set_date_hidden(self, room_id: int, target_date: date, hidden: bool) -> int:
        """특정 날짜의 메시지 숨김/보이기 토글."""
        with self.get_session() as session:
            updated = session.query(Message).filter(
                Message.room_id == room_id,
                Message.message_date == target_date
            ).update({'is_hidden': 1 if hidden else 0})
            return updated

    def is_date_hidden(self, room_id: int, target_date: date) -> bool:
        """특정 날짜가 숨겨져 있는지 확인."""
        with self.get_session() as session:
            count = session.query(func.count(Message.id)).filter(
                Message.room_id == room_id,
                Message.message_date == target_date,
                Message.is_hidden == 1
            ).scalar()
            return count > 0
    
    # ==================== Summary 관련 ====================
    
    def add_summary(self, room_id: int, summary_date: date, 
                    summary_type: str, content: str,
                    llm_provider: Optional[str] = None) -> Summary:
        """요약 추가."""
        with self.get_session() as session:
            summary = Summary(
                room_id=room_id,
                summary_date=summary_date,
                summary_type=summary_type,
                content=content,
                llm_provider=llm_provider
            )
            session.add(summary)
            session.flush()
            summary_id = summary.id
        return self.get_summary_by_id(summary_id)
    
    def get_summary_by_id(self, summary_id: int) -> Optional[Summary]:
        """ID로 요약 조회."""
        with self.get_session() as session:
            return session.query(Summary).filter(Summary.id == summary_id).first()
    
    def get_summaries_by_room(self, room_id: int, 
                              summary_type: Optional[str] = None) -> List[Summary]:
        """채팅방의 요약 목록 조회."""
        with self.get_session() as session:
            query = session.query(Summary).filter(Summary.room_id == room_id)
            if summary_type:
                query = query.filter(Summary.summary_type == summary_type)
            return query.order_by(Summary.summary_date.desc()).all()
    
    def delete_summary(self, room_id: int, summary_date: date) -> bool:
        """특정 날짜의 요약 삭제."""
        with self.get_session() as session:
            deleted = session.query(Summary).filter(
                Summary.room_id == room_id,
                Summary.summary_date == summary_date
            ).delete()
            return deleted > 0

    # ==================== SyncLog 관련 ====================
    
    def add_sync_log(self, room_id: int, status: str, 
                     message_count: int = 0, new_message_count: int = 0,
                     error_message: Optional[str] = None) -> SyncLog:
        """동기화 로그 추가."""
        try:
            with self.get_session() as session:
                log = SyncLog(
                    room_id=room_id,
                    status=status,
                    message_count=message_count,
                    new_message_count=new_message_count,
                    error_message=error_message
                )
                session.add(log)
                session.flush()
                log_id = log.id
            return log_id
        except Exception as e:
            # 로깅 실패는 치명적이지 않으므로 무시 (DB 손상 방지)
            print(f"⚠️ [DB Warning] Failed to add sync log: {e}")
            return -1
    
    def get_sync_logs_by_room(self, room_id: int, limit: int = 10) -> List[SyncLog]:
        """채팅방의 동기화 로그 조회."""
        with self.get_session() as session:
            return session.query(SyncLog).filter(
                SyncLog.room_id == room_id
            ).order_by(SyncLog.synced_at.desc()).limit(limit).all()
    
    # ==================== URL 관련 ====================
    
    def add_url(self, room_id: int, url: str, descriptions: List[str] = None,
                source_date: Optional[date] = None) -> URL:
        """URL 추가 또는 업데이트."""
        desc_str = " / ".join(descriptions) if descriptions else ""
        
        with self.get_session() as session:
            existing = session.query(URL).filter(
                URL.room_id == room_id,
                URL.url == url
            ).first()
            
            if existing:
                # 기존 설명에 새 설명 추가
                existing_descs = set(existing.descriptions.split(" / ")) if existing.descriptions else set()
                new_descs = set(descriptions) if descriptions else set()
                merged = existing_descs | new_descs
                merged.discard("")
                existing.descriptions = " / ".join(sorted(merged))
                existing.updated_at = datetime.now()
                url_id = existing.id
            else:
                url_obj = URL(
                    room_id=room_id,
                    url=url,
                    descriptions=desc_str,
                    source_date=source_date
                )
                session.add(url_obj)
                session.flush()
                url_id = url_obj.id
        
        return url_id
    
    def add_urls_batch(self, room_id: int, urls: Dict[str, List[str]]) -> int:
        """URL 일괄 추가."""
        added = 0
        for url, descriptions in urls.items():
            self.add_url(room_id, url, descriptions)
            added += 1
        return added
    
    def get_urls_by_room(self, room_id: int) -> Dict[str, List[str]]:
        """채팅방의 URL 목록 조회."""
        with self.get_session() as session:
            urls_list = session.query(URL).filter(URL.room_id == room_id).all()
            result = {}
            for u in urls_list:
                descs = u.descriptions.split(" / ") if u.descriptions else []
                descs = [d for d in descs if d]  # 빈 문자열 제거
                result[u.url] = descs
            return result
    
    def get_url_count_by_room(self, room_id: int) -> int:
        """채팅방의 URL 수 조회."""
        with self.get_session() as session:
            return session.query(func.count(URL.id)).filter(
                URL.room_id == room_id
            ).scalar()
    
    def clear_urls_by_room(self, room_id: int) -> int:
        """채팅방의 모든 URL 삭제."""
        with self.get_session() as session:
            count = session.query(URL).filter(URL.room_id == room_id).delete()
            return count
    
    # ==================== 통계 관련 ====================

    def get_all_rooms_with_stats(self) -> List[Dict[str, Any]]:
        """모든 채팅방을 통계와 함께 한 번의 쿼리로 조회."""
        with self.get_session() as session:
            from sqlalchemy import select, literal_column

            # 단일 JOIN 쿼리로 모든 방의 통계 가져오기
            msg_count_subq = (
                select(
                    Message.room_id,
                    func.count(Message.id).label('total_messages'),
                    func.count(func.distinct(Message.sender)).label('unique_senders'),
                    func.min(Message.message_date).label('first_date'),
                    func.max(Message.message_date).label('last_date'),
                )
                .group_by(Message.room_id)
                .subquery()
            )

            rows = (
                session.query(
                    ChatRoom,
                    func.coalesce(msg_count_subq.c.total_messages, 0).label('total_messages'),
                    func.coalesce(msg_count_subq.c.unique_senders, 0).label('unique_senders'),
                    msg_count_subq.c.first_date,
                    msg_count_subq.c.last_date,
                )
                .outerjoin(msg_count_subq, ChatRoom.id == msg_count_subq.c.room_id)
                .order_by(func.coalesce(msg_count_subq.c.total_messages, 0).desc())
                .all()
            )

            result = []
            for row in rows:
                room = row[0]
                detached_room = ChatRoom(
                    id=room.id, name=room.name, file_path=room.file_path,
                    last_sync_at=room.last_sync_at, created_at=room.created_at
                )
                result.append({
                    "room": detached_room,
                    "stats": {
                        'room_name': room.name,
                        'total_messages': row[1],
                        'unique_senders': row[2],
                        'first_date': row[3],
                        'last_date': row[4],
                        'last_sync': room.last_sync_at,
                    },
                })
            return result

    def get_room_stats(self, room_id: int) -> Dict[str, Any]:
        """채팅방 통계 조회."""
        with self.get_session() as session:
            room = session.query(ChatRoom).filter(ChatRoom.id == room_id).first()
            if not room:
                return {}
            
            total_messages = session.query(func.count(Message.id)).filter(
                Message.room_id == room_id
            ).scalar()
            
            unique_senders = session.query(func.count(func.distinct(Message.sender))).filter(
                Message.room_id == room_id
            ).scalar()
            
            date_range = session.query(
                func.min(Message.message_date),
                func.max(Message.message_date)
            ).filter(Message.room_id == room_id).first()
            
            return {
                'room_name': room.name,
                'total_messages': total_messages,
                'unique_senders': unique_senders,
                'first_date': date_range[0] if date_range else None,
                'last_date': date_range[1] if date_range else None,
                'last_sync': room.last_sync_at
            }



    def update_summary_read_status(self, room_id: int, summary_date: date, is_read: bool) -> bool:
        """요약 읽음 상태 업데이트."""
        with self.get_session() as session:
            summary = session.query(Summary).filter(
                Summary.room_id == room_id,
                Summary.summary_date == summary_date
            ).first()
            if summary:
                summary.is_read = 1 if is_read else 0
                return True
            return False

    def get_summary_read_status(self, room_id: int, summary_date: date) -> bool:
        """요약 읽음 상태 조회."""
        with self.get_session() as session:
            summary = session.query(Summary).filter(
                Summary.room_id == room_id,
                Summary.summary_date == summary_date
            ).first()
            return summary.is_read == 1 if summary else False

# 싱글톤 인스턴스
_db_instance: Optional[Database] = None
_db_path: Optional[str] = None


def get_db(db_path: Optional[str] = None, force_new: bool = False) -> Database:
    """데이터베이스 인스턴스 반환."""
    global _db_instance, _db_path
    
    # 새 인스턴스 강제 생성
    if force_new:
        _db_instance = Database(db_path)
        _db_path = db_path
        return _db_instance
    
    # 경로가 변경되었으면 새 인스턴스 생성
    if db_path is not None and db_path != _db_path:
        _db_instance = Database(db_path)
        _db_path = db_path
        return _db_instance
    
    # 기존 인스턴스 재사용
    if _db_instance is None:
        _db_instance = Database(db_path)
        _db_path = db_path
    
    return _db_instance


def reset_db():
    """데이터베이스 인스턴스 리셋."""
    global _db_instance, _db_path
    if _db_instance is not None:
        _db_instance.engine.dispose()
    _db_instance = None
    _db_path = None
