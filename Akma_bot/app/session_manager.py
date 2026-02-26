"""
Менеджер сессий для управления активными сессиями пользователей.
Обеспечивает:
- Отслеживание активных сессий
- Автоматическую очистку "зависших" сессий
- Circuit Breaker паттерн для проблемных пользователей
- Логирование всех операций
"""

import asyncio
import time
import logging
from typing import Dict, Optional, Tuple
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class SessionStatus(Enum):
    """Статусы сессии пользователя"""
    IDLE = "idle"                      # Неактивна
    ACTIVE = "active"                  # Активно прохождение теста
    WAITING_INPUT = "waiting_input"    # Ожидание ввода пользователя
    PROCESSING = "processing"          # Обработка ответа
    BLOCKED = "blocked"                # Заблокирована (Circuit Breaker)
    ERROR = "error"                    # Состояние ошибки
    TERMINATED = "terminated"          # Разорвана


class ErrorSeverity(Enum):
    """Уровни серьезности ошибок"""
    WARNING = "warning"        # Предупреждение
    ERROR = "error"            # Ошибка
    CRITICAL = "critical"      # Критическая ошибка


@dataclass
class UserError:
    """Информация об ошибке пользователя"""
    timestamp: datetime
    error_type: str
    message: str
    severity: ErrorSeverity
    retry_count: int = 0
    
    def __repr__(self) -> str:
        return (f"UserError(time={self.timestamp.strftime('%H:%M:%S')}, "
                f"type={self.error_type}, severity={self.severity.value}, "
                f"retries={self.retry_count})")


@dataclass
class UserSession:
    """Информация о сессии пользователя"""
    user_id: int
    start_time: datetime
    status: SessionStatus = SessionStatus.IDLE
    last_activity: datetime = field(default_factory=datetime.now)
    errors: list = field(default_factory=list)  # List[UserError]
    failure_count: int = 0
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    circuit_breaker_open: bool = False
    circuit_breaker_time: Optional[datetime] = None
    circuit_breaker_cooldown: int = 300  # 5 минут
    blocked_reason: Optional[str] = None
    message_ids: list = field(default_factory=list)  # Отправленные сообщения для отслеживания
    
    @property
    def is_active(self) -> bool:
        """Проверяет, активна ли сессия"""
        return self.status in [SessionStatus.ACTIVE, SessionStatus.WAITING_INPUT, SessionStatus.PROCESSING]
    
    @property
    def is_blocked(self) -> bool:
        """Проверяет, заблокирована ли сессия"""
        return self.circuit_breaker_open or self.status == SessionStatus.BLOCKED
    
    @property
    def idle_time_seconds(self) -> float:
        """Время неактивности в секундах"""
        return (datetime.now() - self.last_activity).total_seconds()
    
    @property
    def session_duration_seconds(self) -> float:
        """Время существования сессии в секундах"""
        return (datetime.now() - self.start_time).total_seconds()
    
    def add_error(self, error_type: str, message: str, severity: ErrorSeverity):
        """Добавить ошибку в историю"""
        error = UserError(
            timestamp=datetime.now(),
            error_type=error_type,
            message=message,
            severity=severity
        )
        self.errors.append(error)
        
        # Ограничиваем размер истории ошибок до 100
        if len(self.errors) > 100:
            self.errors.pop(0)
        
        return error
    
    def reset_consecutive_failures(self):
        """Сбросить счетчик последовательных ошибок"""
        self.consecutive_failures = 0
    
    def increment_consecutive_failures(self):
        """Увеличить счетчик последовательных ошибок"""
        self.consecutive_failures += 1
        self.failure_count += 1
    
    def activate_circuit_breaker(self, reason: str = "Множественные ошибки"):
        """Активировать Circuit Breaker"""
        self.circuit_breaker_open = True
        self.circuit_breaker_time = datetime.now()
        self.status = SessionStatus.BLOCKED
        self.blocked_reason = reason
    
    def check_circuit_breaker_recovery(self) -> bool:
        """Проверить возможность восстановления после Circuit Breaker"""
        if not self.circuit_breaker_open or self.circuit_breaker_time is None:
            return False
        
        elapsed = (datetime.now() - self.circuit_breaker_time).total_seconds()
        if elapsed >= self.circuit_breaker_cooldown:
            self.circuit_breaker_open = False
            self.circuit_breaker_time = None
            self.consecutive_failures = 0
            self.status = SessionStatus.IDLE
            return True
        
        return False


class SessionManager:
    """Менеджер для управления всеми сессиями пользователей"""
    
    def __init__(
        self,
        session_timeout: int = 1800,  # 30 минут неактивности
        cleanup_interval: int = 300,  # Проверка каждые 5 минут
        max_consecutive_failures: int = 3,
        admin_user_id: Optional[int] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Args:
            session_timeout: Время неактивности перед удалением сессии (сек)
            cleanup_interval: Интервал проверки неактивных сессий (сек)
            max_consecutive_failures: Критическое количество последовательных ошибок
            admin_user_id: ID администратора для уведомлений
            logger: Logger для логирования
        """
        self.sessions: Dict[int, UserSession] = {}
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval
        self.max_consecutive_failures = max_consecutive_failures
        self.admin_user_id = admin_user_id
        self.logger = logger or logging.getLogger(__name__)
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def create_session(self, user_id: int) -> UserSession:
        """Создать новую сессию пользователя"""
        session = UserSession(
            user_id=user_id,
            start_time=datetime.now(),
            max_consecutive_failures=self.max_consecutive_failures
        )
        self.sessions[user_id] = session
        self.logger.info(f"[tg:{user_id}] ✅ Сессия создана")
        return session
    
    def get_session(self, user_id: int) -> Optional[UserSession]:
        """Получить сессию пользователя"""
        return self.sessions.get(user_id)
    
    def get_or_create_session(self, user_id: int) -> UserSession:
        """Получить или создать сессию"""
        session = self.get_session(user_id)
        if not session:
            session = self.create_session(user_id)
        return session
    
    def update_activity(self, user_id: int):
        """Обновить время последней активности"""
        session = self.get_session(user_id)
        if session:
            session.last_activity = datetime.now()
    
    def set_status(self, user_id: int, status: SessionStatus):
        """Установить статус сессии"""
        session = self.get_session(user_id)
        if session:
            old_status = session.status
            session.status = status
            self.logger.debug(f"[tg:{user_id}] Статус: {old_status.value} → {status.value}")
    
    def record_send_error(
        self,
        user_id: int,
        error_type: str,
        message: str,
        is_critical: bool = False
    ) -> Tuple[bool, str]:
        """
        Записать ошибку отправки и проверить критичность.
        
        Returns:
            (should_continue, reason): продолжать ли операцию и причина
        """
        session = self.get_or_create_session(user_id)
        
        severity = ErrorSeverity.CRITICAL if is_critical else ErrorSeverity.ERROR
        session.add_error(error_type, message, severity)
        session.increment_consecutive_failures()
        
        # Проверяем Circuit Breaker
        if session.consecutive_failures >= self.max_consecutive_failures:
            session.activate_circuit_breaker(
                reason=f"Критическое количество ошибок: {session.consecutive_failures}"
            )
            msg = f"[tg:{user_id}] 🔴 Circuit Breaker активирован после {session.consecutive_failures} ошибок"
            self.logger.warning(msg)
            return False, msg
        
        msg = (f"[tg:{user_id}] ⚠️ Ошибка отправки ({session.consecutive_failures}/"
               f"{self.max_consecutive_failures}): {error_type}")
        self.logger.warning(msg)
        return True, msg
    
    def record_success(self, user_id: int):
        """Записать успешную операцию"""
        session = self.get_session(user_id)
        if session:
            session.reset_consecutive_failures()
            session.update_activity = datetime.now()
            self.logger.debug(f"[tg:{user_id}] ✅ Операция успешна")
    
    def terminate_session(self, user_id: int, reason: str = ""):
        """Завершить сессию"""
        session = self.get_session(user_id)
        if session:
            session.status = SessionStatus.TERMINATED
            reason_msg = f" ({reason})" if reason else ""
            self.logger.info(f"[tg:{user_id}] 🛑 Сессия завершена{reason_msg}")
    
    def cleanup_session(self, user_id: int, reason: str = ""):
        """Удалить сессию (полная очистка)"""
        if user_id in self.sessions:
            reason_msg = f" ({reason})" if reason else ""
            self.logger.info(f"[tg:{user_id}] 🗑️ Сессия удалена{reason_msg}")
            del self.sessions[user_id]
    
    async def cleanup_inactive_sessions(self) -> int:
        """
        Очистить неактивные сессии.
        
        Returns:
            Количество удаленных сессий
        """
        now = datetime.now()
        inactive_users = []
        
        for user_id, session in list(self.sessions.items()):
            # Проверяем Circuit Breaker recovery
            if session.is_blocked:
                session.check_circuit_breaker_recovery()
            
            # Удаляем неактивные сессии
            if session.idle_time_seconds > self.session_timeout:
                if session.is_active:
                    self.logger.warning(
                        f"[tg:{user_id}] ⚠️ Активная сессия неактивна "
                        f"{int(session.idle_time_seconds)}сек, удаляем"
                    )
                inactive_users.append(user_id)
        
        for user_id in inactive_users:
            self.cleanup_session(user_id, f"неактивность ({self.session_timeout}сек)")
        
        return len(inactive_users)
    
    async def start_cleanup_task(self):
        """Запустить фоновую задачу очистки сессий"""
        if self._cleanup_task and not self._cleanup_task.done():
            return
        
        async def cleanup_loop():
            self.logger.info("🧹 Задача очистки сессий запущена")
            try:
                while True:
                    await asyncio.sleep(self.cleanup_interval)
                    removed = await self.cleanup_inactive_sessions()
                    if removed > 0:
                        active_count = len([s for s in self.sessions.values() if s.is_active])
                        self.logger.info(
                            f"🧹 Очистка: удалено {removed} сессий, "
                            f"активных осталось: {active_count}"
                        )
            except asyncio.CancelledError:
                self.logger.info("🧹 Задача очистки остановлена")
        
        self._cleanup_task = asyncio.create_task(cleanup_loop())
    
    async def stop_cleanup_task(self):
        """Остановить фоновую задачу очистки"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    def get_session_stats(self) -> Dict:
        """Получить статистику сессий"""
        total = len(self.sessions)
        active = sum(1 for s in self.sessions.values() if s.is_active)
        blocked = sum(1 for s in self.sessions.values() if s.is_blocked)
        errors = sum(len(s.errors) for s in self.sessions.values())
        
        return {
            "total_sessions": total,
            "active_sessions": active,
            "blocked_sessions": blocked,
            "total_errors": errors,
            "sessions_by_status": {
                status.value: sum(1 for s in self.sessions.values() if s.status == status)
                for status in SessionStatus
            }
        }
    
    def is_user_blocked(self, user_id: int) -> Tuple[bool, str]:
        """
        Проверить, заблокирован ли пользователь.
        
        Returns:
            (is_blocked, reason)
        """
        session = self.get_session(user_id)
        if not session:
            return False, ""
        
        if session.circuit_breaker_open:
            if session.check_circuit_breaker_recovery():
                return False, f"Circuit Breaker восстановлен после {session.circuit_breaker_cooldown}сек"
            
            elapsed = (datetime.now() - session.circuit_breaker_time).total_seconds()
            remaining = int(session.circuit_breaker_cooldown - elapsed)
            return True, f"Заблокирован на {remaining}сек ({session.blocked_reason})"
        
        return False, ""


# Глобальный экземпляр менеджера сессий (инициализируется в main боте)
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Получить глобальный менеджер сессий"""
    global _session_manager
    if _session_manager is None:
        raise RuntimeError("SessionManager не инициализирован. Вызовите init_session_manager().")
    return _session_manager


def init_session_manager(
    session_timeout: int = 1800,
    cleanup_interval: int = 300,
    max_consecutive_failures: int = 3,
    admin_user_id: Optional[int] = None,
    logger: Optional[logging.Logger] = None
) -> SessionManager:
    """Инициализировать глобальный менеджер сессий"""
    global _session_manager
    _session_manager = SessionManager(
        session_timeout=session_timeout,
        cleanup_interval=cleanup_interval,
        max_consecutive_failures=max_consecutive_failures,
        admin_user_id=admin_user_id,
        logger=logger
    )
    return _session_manager
