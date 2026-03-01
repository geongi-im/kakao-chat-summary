#!/usr/bin/env python3
"""SQLite → PostgreSQL 데이터 마이그레이션 스크립트"""

import sys
import os
from pathlib import Path

# 프로젝트 경로 추가
project_root = Path("/home/dryseason/apps/kakao-chat-summary")
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# SQLite DB 경로
sqlite_db_path = project_root / "data" / "db" / "chat_history.db"
sqlite_engine = create_engine(f"sqlite:///{sqlite_db_path}")

# PostgreSQL DB URL
postgres_url = "postgresql://dryseason:apfhd852@localhost/kakao_chat"
postgres_engine = create_engine(postgres_url)

# 세션 생성
SQLiteSession = sessionmaker(bind=sqlite_engine)
PostgresSession = sessionmaker(bind=postgres_engine)

print("🔄 마이그레이션 시작...")
print(f"📁 SQLite: {sqlite_db_path}")
print(f"🐘 PostgreSQL: {postgres_url}")

# SQLite 데이터 읽기
sqlite_session = SQLiteSession()
postgres_session = PostgresSession()

try:
    # 1. chat_rooms
    print("\n📋 chat_rooms 마이그레이션...")
    from db.models import ChatRoom
    
    rooms = sqlite_session.query(ChatRoom).all()
    print(f"   - 총 {len(rooms)}개 채팅방")
    
    for room in rooms:
        new_room = ChatRoom(
            id=room.id,
            name=room.name,
            file_path=room.file_path,
            participant_count=room.participant_count,
            last_sync_at=room.last_sync_at,
            created_at=room.created_at
        )
        postgres_session.merge(new_room)
    
    postgres_session.commit()
    print("   ✅ chat_rooms 완료")
    
    # 2. messages
    print("\n💬 messages 마이그레이션...")
    from db.models import Message
    
    messages = sqlite_session.query(Message).all()
    print(f"   - 총 {len(messages)}개 메시지")
    
    # 배치 처리 (1000개씩)
    batch_size = 1000
    for i in range(0, len(messages), batch_size):
        batch = messages[i:i + batch_size]
        
        for msg in batch:
            new_msg = Message(
                id=msg.id,
                room_id=msg.room_id,
                sender=msg.sender,
                content=msg.content,
                message_date=msg.message_date,
                message_time=msg.message_time,
                raw_line=msg.raw_line,
                created_at=msg.created_at
            )
            postgres_session.merge(new_msg)
        
        postgres_session.commit()
        print(f"   - {min(i + batch_size, len(messages))}/{len(messages)} 처리 완료")
    
    print("   ✅ messages 완료")
    
    # 3. summaries
    print("\n📝 summaries 마이그레이션...")
    from db.models import Summary
    
    summaries = sqlite_session.query(Summary).all()
    print(f"   - 총 {len(summaries)}개 요약")
    
    for summary in summaries:
        new_summary = Summary(
            id=summary.id,
            room_id=summary.room_id,
            summary_date=summary.summary_date,
            summary_type=summary.summary_type,
            content=summary.content,
            llm_provider=summary.llm_provider,
            token_count=summary.token_count,
            created_at=summary.created_at
        )
        postgres_session.merge(new_summary)
    
    postgres_session.commit()
    print("   ✅ summaries 완료")
    
    # 4. sync_logs
    print("\n📊 sync_logs 마이그레이션...")
    from db.models import SyncLog
    
    logs = sqlite_session.query(SyncLog).all()
    print(f"   - 총 {len(logs)}개 로그")
    
    for log in logs:
        new_log = SyncLog(
            id=log.id,
            room_id=log.room_id,
            status=log.status,
            message_count=log.message_count,
            new_message_count=log.new_message_count,
            error_message=log.error_message,
            synced_at=log.synced_at
        )
        postgres_session.merge(new_log)
    
    postgres_session.commit()
    print("   ✅ sync_logs 완료")
    
    # 5. urls
    print("\n🔗 urls 마이그레이션...")
    from db.models import URL
    
    urls = sqlite_session.query(URL).all()
    print(f"   - 총 {len(urls)}개 URL")
    
    for url_obj in urls:
        new_url = URL(
            id=url_obj.id,
            room_id=url_obj.room_id,
            url=url_obj.url,
            descriptions=url_obj.descriptions,
            source_date=url_obj.source_date,
            created_at=url_obj.created_at,
            updated_at=url_obj.updated_at
        )
        postgres_session.merge(new_url)
    
    postgres_session.commit()
    print("   ✅ urls 완료")
    
    print("\n✅ 마이그레이션 완료!")
    
    # 결과 확인
    print("\n📊 PostgreSQL 데이터 확인:")
    result = postgres_session.execute(text("SELECT 'chat_rooms' as table_name, COUNT(*) as count FROM chat_rooms UNION ALL SELECT 'messages', COUNT(*) FROM messages UNION ALL SELECT 'summaries', COUNT(*) FROM summaries UNION ALL SELECT 'sync_logs', COUNT(*) FROM sync_logs UNION ALL SELECT 'urls', COUNT(*) FROM urls"))
    
    for row in result:
        print(f"   - {row[0]}: {row[1]}개")
    
except Exception as e:
    print(f"\n❌ 오류 발생: {e}")
    import traceback
    traceback.print_exc()
    postgres_session.rollback()
    
finally:
    sqlite_session.close()
    postgres_session.close()
