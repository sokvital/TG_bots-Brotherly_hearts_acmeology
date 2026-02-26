"""
Akma Bot - MBTI Diagnostic Telegram Bot
Version 1.0.0
"""

__version__ = "1.0.0"
__author__ = "AI Assistant"

from .session_manager import (
    SessionManager,
    SessionStatus,
    ErrorSeverity,
    get_session_manager,
    init_session_manager
)
from .safe_messages import (
    safe_send_message,
    safe_edit_message,
    safe_delete_message,
    safe_send_document,
    get_user_logger as get_safe_logger
)

__all__ = [
    "SessionManager",
    "SessionStatus", 
    "ErrorSeverity",
    "get_session_manager",
    "init_session_manager",
    "safe_send_message",
    "safe_edit_message",
    "safe_delete_message",
    "safe_send_document",
    "get_safe_logger"
]
