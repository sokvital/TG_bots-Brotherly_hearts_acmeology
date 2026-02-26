"""
Безопасные функции для отправки, редактирования и удаления сообщений в Telegram.
Интегрированы с SessionManager для управления сессиями и обработки ошибок.

Все функции:
- Возвращают результат (успех/неудача) или объект сообщения
- Имеют ограниченное количество повторных попыток (по умолчанию 3)
- При критических ошибках записывают в SessionManager
- Логируют все ошибки с деталями
"""

import asyncio
import logging
from typing import Optional, Union, Dict, Any
from aiogram import Bot
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from .session_manager import get_session_manager, SessionStatus, ErrorSeverity


class SendMessageError(Exception):
    """Исключение при ошибке отправки сообщения"""
    pass


class EditMessageError(Exception):
    """Исключение при ошибке редактирования сообщения"""
    pass


class DeleteMessageError(Exception):
    """Исключение при ошибке удаления сообщения"""
    pass


def get_user_logger(user_id: int) -> logging.LoggerAdapter:
    """
    Получить логгер с привязкой к user_id.
    Должна совпадать с функцией в основном коде.
    """
    logger = logging.getLogger(__name__)
    try:
        tg = str(user_id) if user_id is not None else "no-id"
    except Exception:
        tg = "no-id"
    return logging.LoggerAdapter(logger, {"tg_id": tg})


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    retries: int = 3,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: Optional[bool] = None,
    **kwargs
) -> Optional[Message]:
    """
    Безопасная отправка сообщения с повторными попытками при ошибках.
    
    Особенности:
    - Обработка различных типов ошибок (сеть, таймауты, API)
    - Различие между критическими и временными ошибками
    - Интеграция с SessionManager
    - Подробное логирование
    
    Args:
        bot: Объект бота aiogram
        chat_id: ID чата для отправки
        text: Текст сообщения
        retries: Количество попыток отправки (по умолчанию 3)
        reply_markup: Клавиатура для сообщения
        parse_mode: Режим парсинга (HTML, Markdown, MarkdownV2)
        disable_web_page_preview: Отключить превью ссылок
        **kwargs: Дополнительные параметры для send_message
        
    Returns:
        Message: Объект отправленного сообщения если успешно, иначе None
    """
    user_logger = get_user_logger(chat_id)
    session_manager = get_session_manager()
    session = session_manager.get_or_create_session(chat_id)
    session_manager.set_status(chat_id, SessionStatus.PROCESSING)
    
    # Проверяем, не заблокирована ли сессия
    is_blocked, block_reason = session_manager.is_user_blocked(chat_id)
    if is_blocked:
        user_logger.warning(f"🔴 Сессия заблокирована: {block_reason}")
        return None
    
    last_error = None
    
    for attempt in range(1, retries + 1):
        try:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs
            )
            
            # Успех - сбросить счетчик ошибок
            session_manager.record_success(chat_id)
            session_manager.set_status(chat_id, SessionStatus.WAITING_INPUT)
            user_logger.debug(f"✅ Отправлено сообщение: {message.message_id}")
            
            # Отслеживать отправленные сообщения
            session.message_ids.append(message.message_id)
            
            return message
            
        except TelegramRetryAfter as e:
            # Обработка лимитов Telegram (429 слишком много запросов)
            wait_time = getattr(e, 'retry_after', 5)
            user_logger.warning(
                f"⏸️ TelegramRetryAfter: ждем {wait_time}с перед попыткой {attempt}/{retries}"
            )
            session_manager.record_send_error(
                chat_id,
                "TelegramRetryAfter",
                f"Лимит запросов, ожидание {wait_time}с",
                is_critical=False
            )
            await asyncio.sleep(min(wait_time, 30))  # Макс 30 сек
            
        except (asyncio.TimeoutError, asyncio.TimeoutError) as e:
            # Таймауты подключения/чтения
            last_error = e
            delay = 2 ** attempt  # Экспоненциальная задержка: 2, 4, 8
            user_logger.warning(
                f"⏳ Таймаут при попытке {attempt}/{retries}: {type(e).__name__}\n"
                f"   Задержка перед повтором: {delay}с"
            )
            session_manager.record_send_error(
                chat_id,
                "TimeoutError",
                f"Таймаут при отправке: {str(e)[:100]}",
                is_critical=False
            )
            await asyncio.sleep(delay)
            
        except TelegramAPIError as e:
            # Ошибки Telegram API
            error_msg = str(e).lower()
            last_error = e
            
            # Определяем критичность ошибки
            critical_phrases = [
                "user is deactivated",          # Пользователь удален
                "bot was blocked",              # Бот заблокирован
                "forbidden: bot can't send",    # Бот не может отправлять
                "chat not found",               # Чат не найден
                "user is bot",                  # Это бот
                "access denied"                 # Доступ запрещен
            ]
            
            is_critical = any(phrase in error_msg for phrase in critical_phrases)
            
            user_logger.warning(
                f"⚠️ TelegramAPIError при попытке {attempt}/{retries}: {e}\n"
                f"   Критично: {is_critical}"
            )
            
            session_manager.record_send_error(
                chat_id,
                "TelegramAPIError",
                str(e)[:200],
                is_critical=is_critical
            )
            
            # Если критическая ошибка - не повторяем
            if is_critical:
                user_logger.error(
                    f"🔴 Критическая ошибка при отправке сообщению {chat_id}: {e}"
                )
                return None
            
            # Иначе - экспоненциальная задержка
            delay = 2 ** attempt
            await asyncio.sleep(delay)
            
        except Exception as e:
            # Неожиданные ошибки
            last_error = e
            user_logger.error(
                f"❌ Неожиданная ошибка при отправке сообщения {chat_id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True
            )
            session_manager.record_send_error(
                chat_id,
                type(e).__name__,
                str(e)[:200],
                is_critical=True
            )
            return None
    
    # Все попытки исчерпаны
    user_logger.error(
        f"❌ Не удалось отправить сообщение пользователю {chat_id} "
        f"после {retries} попыток\n"
        f"   Последняя ошибка: {last_error}"
    )
    session_manager.record_send_error(
        chat_id,
        "AllRetriesFailed",
        f"Не удалось отправить после {retries} попыток",
        is_critical=True
    )
    
    return None


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    new_text: str,
    retries: int = 3,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: Optional[bool] = None,
    **kwargs
) -> bool:
    """
    Безопасное редактирование сообщения с повторными попытками.
    
    Args:
        bot: Объект бота aiogram
        chat_id: ID чата
        message_id: ID сообщения для редактирования
        new_text: Новый текст сообщения
        retries: Количество попыток
        reply_markup: Новая клавиатура
        parse_mode: Режим парсинга
        disable_web_page_preview: Отключить превью ссылок
        **kwargs: Дополнительные параметры
        
    Returns:
        bool: True если успешно, False иначе
    """
    user_logger = get_user_logger(chat_id)
    session_manager = get_session_manager()
    
    # Проверяем, не заблокирована ли сессия
    is_blocked, block_reason = session_manager.is_user_blocked(chat_id)
    if is_blocked:
        user_logger.warning(f"🔴 Сессия заблокирована: {block_reason}")
        return False
    
    last_error = None
    
    for attempt in range(1, retries + 1):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs
            )
            
            session_manager.record_success(chat_id)
            user_logger.debug(f"✅ Отредактировано сообщение: {message_id} ")
            return True
            
        except TelegramRetryAfter as e:
            wait_time = getattr(e, 'retry_after', 5)
            user_logger.warning(
                f"⏸️ TelegramRetryAfter при редактировании {message_id}: {wait_time}с "
                f"(попытка {attempt}/{retries})"
            )
            session_manager.record_send_error(
                chat_id,
                "TelegramRetryAfter",
                f"Лимит при редактировании, ожиданий {wait_time}с",
                is_critical=False
            )
            await asyncio.sleep(min(wait_time, 30))
            
        except (asyncio.TimeoutError,) as e:
            delay = 2 ** attempt
            user_logger.warning(
                f"⏳ Таймаут при редактировании {message_id} "
                f"(попытка {attempt}/{retries})"
            )
            session_manager.record_send_error(
                chat_id,
                "TimeoutError",
                f"Таймаут при редактировании",
                is_critical=False
            )
            await asyncio.sleep(delay)
            
        except TelegramAPIError as e:
            error_msg = str(e).lower()
            last_error = e
            
            # Сообщение не найдено/не может быть отредактировано - это нормально
            non_critical_phrases = [
                "message is not modified",
                "message to edit not found",
                "message can't be edited",
                "message not found",
                "message_id_invalid"
            ]
            
            if any(phrase in error_msg for phrase in non_critical_phrases):
                user_logger.info(
                    f"⚠️ Не найдено для редактирования сообщение: {message_id} "
                )
                return False  # Возвращаем True, т.к. это не критическая ошибка
            
            # Критические ошибки
            critical_phrases = [
                "bot was blocked",
                "user is deactivated",
                "forbidden: bot can't send"
            ]
            
            is_critical = any(phrase in error_msg for phrase in critical_phrases)
            
            user_logger.warning(
                f"⚠️ TelegramAPIError при редактировании {message_id} "
                f"(попытка {attempt}/{retries}): {e}"
            )
            session_manager.record_send_error(
                chat_id,
                "TelegramAPIError",
                str(e)[:200],
                is_critical=is_critical
            )
            
            if is_critical:
                return False
            
            delay = 2 ** attempt
            await asyncio.sleep(delay)
            
        except Exception as e:
            last_error = e
            user_logger.error(
                f"❌ Неожиданная ошибка при редактировании {message_id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True
            )
            session_manager.record_send_error(
                chat_id,
                type(e).__name__,
                str(e)[:200],
                is_critical=True
            )
            return False
    
    user_logger.error(
        f"❌ Не удалось отредактировать сообщение {message_id} "
        f"после {retries} попыток"
    )
    session_manager.record_send_error(
        chat_id,
        "AllRetriesFailed",
        f"Не удалось отредактировать после {retries} попыток",
        is_critical=True
    )
    
    return False


async def safe_delete_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    retries: int = 3
) -> bool:
    """
    Безопасное удаление сообщения с повторными попытками.
    
    Args:
        bot: Объект бота aiogram
        chat_id: ID чата
        message_id: ID сообщения для удаления
        retries: Количество попыток
        
    Returns:
        bool: True если удалено успешно, False иначе
    """
    user_logger = get_user_logger(chat_id)
    session_manager = get_session_manager()
    
    # Проверяем, не заблокирована ли сессия
    is_blocked, _ = session_manager.is_user_blocked(chat_id)
    if is_blocked:
        user_logger.debug(f"Попытка удаления при заблокированной сессии")
        return False
    
    last_error = None
    
    for attempt in range(1, retries + 1):
        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id
            )
            
            session_manager.record_success(chat_id)
            user_logger.debug(f"✅ Удалено сообщение: {message_id} ")
            
            # Удалить из отслеживания
            session = session_manager.get_session(chat_id)
            if session and message_id in session.message_ids:
                session.message_ids.remove(message_id)
            
            return True
            
        except TelegramRetryAfter as e:
            wait_time = getattr(e, 'retry_after', 5)
            user_logger.debug(
                f"⏸️ TelegramRetryAfter при удалении {message_id}: {wait_time}с"
            )
            await asyncio.sleep(min(wait_time, 30))
            
        except asyncio.TimeoutError as e:
            delay = 2 ** attempt
            user_logger.warning(
                f"⏳ Таймаут при удалении {message_id} (попытка {attempt}/{retries})"
            )
            await asyncio.sleep(delay)
            
        except TelegramAPIError as e:
            error_msg = str(e).lower()
            last_error = e
            
            # Сообщение уже удалено - это нормально
            if any(phrase in error_msg for phrase in [
                "message to delete not found",
                "message can't be deleted",
                "message not found"
            ]):
                user_logger.debug(
                    f"ℹ️ Сообщение уже удалено ранее: {message_id}"
                )
                return True
            
            # Критические ошибки
            critical_phrases = [
                "bot was blocked",
                "user is deactivated",
                "forbidden"
            ]
            
            is_critical = any(phrase in error_msg for phrase in critical_phrases)
            
            user_logger.warning(
                f"⚠️ TelegramAPIError при удалении {message_id}: {e} (критично: {is_critical})"
            )
            
            if is_critical:
                session_manager.record_send_error(
                    chat_id,
                    "TelegramAPIError",
                    str(e)[:200],
                    is_critical=True
                )
                return False
            
            delay = 2 ** attempt
            await asyncio.sleep(delay)
            
        except Exception as e:
            last_error = e
            user_logger.error(
                f"❌ Неожиданная ошибка при удалении {message_id}: {type(e).__name__}: {e}"
            )
            return False
    
    user_logger.warning(
        f"⚠️ Не удалось удалить сообщение {message_id} после {retries} попыток"
    )
    
    return False


async def safe_send_document(
    bot: Bot,
    chat_id: int,
    document: Union[FSInputFile, str],
    caption: Optional[str] = None,
    retries: int = 3,
    parse_mode: Optional[str] = None,
    **kwargs
) -> Optional[Message]:
    """
    Безопасная отправка файла с повторными попытками.
    
    Args:
        bot: Объект бота aiogram
        chat_id: ID чата
        document: Файл (FSInputFile или путь)
        caption: Подпись к файлу
        retries: Количество попыток
        parse_mode: Режим парсинга
        **kwargs: Дополнительные параметры
        
    Returns:
        Message: Объект отправленного сообщения если успешно, None иначе
    """
    user_logger = get_user_logger(chat_id)
    session_manager = get_session_manager()
    session = session_manager.get_or_create_session(chat_id)
    session_manager.set_status(chat_id, SessionStatus.PROCESSING)
    
    # Проверяем, не заблокирована ли сессия
    is_blocked, block_reason = session_manager.is_user_blocked(chat_id)
    if is_blocked:
        user_logger.warning(f"🔴 Сессия заблокирована: {block_reason}")
        return None
    
    last_error = None
    
    for attempt in range(1, retries + 1):
        try:
            message = await bot.send_document(
                chat_id=chat_id,
                document=document,
                caption=caption,
                parse_mode=parse_mode,
                **kwargs
            )
            
            session_manager.record_success(chat_id)
            session_manager.set_status(chat_id, SessionStatus.WAITING_INPUT)
            user_logger.info(f"✅ Файл отправлен (ID: {message.message_id})")
            
            session.message_ids.append(message.message_id)
            return message
            
        except TelegramRetryAfter as e:
            wait_time = getattr(e, 'retry_after', 5)
            user_logger.warning(
                f"⏸️ TelegramRetryAfter при отправке файла: {wait_time}с "
                f"(попытка {attempt}/{retries})"
            )
            session_manager.record_send_error(
                chat_id,
                "TelegramRetryAfter",
                f"Лимит при отправке файла, ожидание {wait_time}с",
                is_critical=False
            )
            await asyncio.sleep(min(wait_time, 30))
            
        except asyncio.TimeoutError as e:
            delay = 2 ** attempt
            user_logger.warning(
                f"⏳ Таймаут при отправке файла (попытка {attempt}/{retries})"
            )
            session_manager.record_send_error(
                chat_id,
                "TimeoutError",
                "Таймаут при отправке файла",
                is_critical=False
            )
            await asyncio.sleep(delay)
            
        except TelegramAPIError as e:
            error_msg = str(e).lower()
            last_error = e
            
            critical_phrases = [
                "user is deactivated",
                "bot was blocked",
                "forbidden: bot can't send",
                "chat not found"
            ]
            
            is_critical = any(phrase in error_msg for phrase in critical_phrases)
            
            user_logger.warning(
                f"⚠️ TelegramAPIError при отправке файла (попытка {attempt}/{retries}): {e}"
            )
            session_manager.record_send_error(
                chat_id,
                "TelegramAPIError",
                str(e)[:200],
                is_critical=is_critical
            )
            
            if is_critical:
                return None
            
            delay = 2 ** attempt
            await asyncio.sleep(delay)
            
        except Exception as e:
            last_error = e
            user_logger.error(
                f"❌ Неожиданная ошибка при отправке файла: {type(e).__name__}: {e}",
                exc_info=True
            )
            session_manager.record_send_error(
                chat_id,
                type(e).__name__,
                str(e)[:200],
                is_critical=True
            )
            return None
    
    user_logger.error(
        f"❌ Не удалось отправить файл пользователю {chat_id} после {retries} попыток"
    )
    session_manager.record_send_error(
        chat_id,
        "AllRetriesFailed",
        "Не удалось отправить файл после всех попыток",
        is_critical=True
    )
    
    return None
