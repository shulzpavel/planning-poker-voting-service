"""Audit logging for administrative actions."""

import json
from datetime import datetime
from typing import Any, Dict, Optional


def audit_log(
    action: str,
    user_id: int,
    user_name: str,
    chat_id: int,
    topic_id: Optional[int],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Log administrative action to stdout.
    
    Args:
        action: Action name (e.g., 'reset_queue', 'update_jira_sp', 'add_tasks')
        user_id: Product user ID
        user_name: User full name (can be from participant or callback.from_user)
        chat_id: Chat ID
        topic_id: Topic ID (if in topic)
        extra: Additional data (e.g., task_count, jira_keys, etc.)
    """
    timestamp = datetime.utcnow().isoformat()
    log_entry = {
        "timestamp": timestamp,
        "action": action,
        "user_id": user_id,
        "user_name": user_name,
        "chat_id": chat_id,
        "topic_id": topic_id,
    }
    
    if extra:
        log_entry["extra"] = extra
    
    # Форматируем для читаемости в stdout/journald
    log_line = f"[AUDIT] {timestamp} | {action} | user:{user_id} ({user_name}) | chat:{chat_id}"
    if topic_id:
        log_line += f" | topic:{topic_id}"
    
    if extra:
        extra_str = json.dumps(extra, ensure_ascii=False)
        log_line += f" | {extra_str}"
    
    print(log_line)

    # также пишем в stdout структурированный JSON для последующего парсинга логами / Grafana Loki
    structured = dict(log_entry)
    print(json.dumps({"audit": structured}, ensure_ascii=False))
