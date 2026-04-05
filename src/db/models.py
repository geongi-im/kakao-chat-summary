"""SQLAlchemy models for chat data storage."""
from datetime import datetime, date, time
from typing import Optional, List
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date, Time, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship, Session

Base = declarative_base()


class ChatRoom(Base):
    """채팅방 테이블."""
    __tablename__ = 'chat_rooms'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    file_path = Column(String(512))
    participant_count = Column(Integer, default=0)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now())
    
    # Relationships
    messages = relationship("Message", back_populates="room", cascade="all, delete-orphan")
    summaries = relationship("Summary", back_populates="room", cascade="all, delete-orphan")
    sync_logs = relationship("SyncLog", back_populates="room", cascade="all, delete-orphan")
    urls = relationship("URL", back_populates="room", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<ChatRoom(id={self.id}, name='{self.name}')>"


class Message(Base):
    """메시지 테이블."""
    __tablename__ = 'messages'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey('chat_rooms.id'), nullable=False)
    sender = Column(String(255), nullable=False)
    content = Column(Text)
    message_date = Column(Date, nullable=False)
    message_time = Column(Time)
    raw_line = Column(Text)  # 원본 라인 저장
    is_hidden = Column(Integer, default=0)  # 0: 표시, 1: 숨김
    created_at = Column(DateTime, default=lambda: datetime.now())
    
    # Unique constraint to prevent duplicates
    __table_args__ = (
        UniqueConstraint('room_id', 'sender', 'message_date', 'message_time', 'content', 
                        name='uq_message_unique'),
    )
    
    # Relationships
    room = relationship("ChatRoom", back_populates="messages")
    
    def __repr__(self):
        return f"<Message(id={self.id}, sender='{self.sender}', date={self.message_date})>"


class Summary(Base):
    """요약 테이블."""
    __tablename__ = 'summaries'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey('chat_rooms.id'), nullable=False)
    summary_date = Column(Date, nullable=False)
    summary_type = Column(String(50), nullable=False)  # 'daily', '2days', 'weekly'
    content = Column(Text)
    llm_provider = Column(String(100))
    token_count = Column(Integer)
    created_at = Column(DateTime, default=lambda: datetime.now())
    is_read = Column(Integer, default=0)  # 0: 안읽음, 1: 읽음
    
    # Relationships
    room = relationship("ChatRoom", back_populates="summaries")
    
    def __repr__(self):
        return f"<Summary(id={self.id}, type='{self.summary_type}', date={self.summary_date})>"


class SyncLog(Base):
    """동기화 로그 테이블."""
    __tablename__ = 'sync_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey('chat_rooms.id'), nullable=False)
    status = Column(String(50), nullable=False)  # 'success', 'failed', 'partial'
    message_count = Column(Integer, default=0)
    new_message_count = Column(Integer, default=0)
    error_message = Column(Text)
    synced_at = Column(DateTime, default=lambda: datetime.now())
    
    # Relationships
    room = relationship("ChatRoom", back_populates="sync_logs")
    
    def __repr__(self):
        return f"<SyncLog(id={self.id}, status='{self.status}', synced_at={self.synced_at})>"


class URL(Base):
    """URL 테이블."""
    __tablename__ = 'urls'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey('chat_rooms.id'), nullable=False)
    url = Column(Text, nullable=False)
    descriptions = Column(Text)  # JSON 또는 " / " 구분 문자열
    source_date = Column(Date)  # URL이 발견된 요약 날짜
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())
    
    # Unique constraint
    __table_args__ = (
        UniqueConstraint('room_id', 'url', name='uq_url_unique'),
    )
    
    # Relationships
    room = relationship("ChatRoom", back_populates="urls")
    
    def __repr__(self):
        return f"<URL(id={self.id}, url='{self.url[:50]}...')>"
