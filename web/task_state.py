"""LLM 요약 백그라운드 작업 상태 관리."""
import threading
from datetime import datetime
from typing import Dict, Any, Optional

# room_id -> task info
_tasks: Dict[int, Dict[str, Any]] = {}
_lock = threading.Lock()


def get_task(room_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        return dict(_tasks[room_id]) if room_id in _tasks else None


def set_task(room_id: int, task_info: Dict[str, Any]):
    with _lock:
        _tasks[room_id] = task_info


def update_task(room_id: int, **kwargs):
    with _lock:
        if room_id in _tasks:
            _tasks[room_id].update(kwargs)


def clear_task(room_id: int):
    with _lock:
        _tasks.pop(room_id, None)


def is_running(room_id: int) -> bool:
    with _lock:
        task = _tasks.get(room_id)
        return task is not None and task.get("status") == "running"


def get_cancel_flag(room_id: int) -> bool:
    with _lock:
        task = _tasks.get(room_id)
        return task.get("cancel_requested", False) if task else False
