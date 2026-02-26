import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Dict
import re

import pandas as pd
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    Message, FSInputFile, InlineKeyboardMarkup, 
    InlineKeyboardButton, BotCommand
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import yaml

# Импорт системы управления сессиями и безопасных функций отправки
from .session_manager import (
    get_session_manager, SessionStatus, ErrorSeverity,
    init_session_manager
)
from .safe_messages import (
    safe_send_message, safe_edit_message, safe_delete_message, safe_send_document,
    get_user_logger as get_safe_logger
)

from .mbti_logic import testing, is_russian_text, pending_answers

# ======================
# ЗАГРУЗКА КОНФИГУРАЦИИ
# ======================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

#EXCEL_PATH = "data/users.xlsx"
#DB_PATH = "data/mbti_bot.db"
#REPORTS_DIR = Path("reports")

# Загрузка конфигурации из YAML файла
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
config_path = os.path.join(project_root, 'data', 'config.yaml')
with open(config_path, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Получаем абсолютный путь относительно корня проекта
EXCEL_PATH = os.path.join(project_root, config.get('bd_xlsx_path'))
DB_PATH = os.path.join(project_root, config.get('bd_path'))

# Настройка логирования
log_dir = os.path.join(project_root, 'logs')
# Создаем папку logs, если её нет
os.makedirs(log_dir, exist_ok=True)

# Путь к файлу лога
log_file = os.path.join(log_dir, 'telegram_bot.log')

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def get_user_logger(tg_id):
    """Возвращает LoggerAdapter с полем tg_id (строка) или 'no-id' если отсутствует."""
    try:
        tg = str(tg_id) if tg_id is not None else "no-id"
    except Exception:
        tg = "no-id"
    return logging.LoggerAdapter(logger, {"tg_id": tg})

# Безопасный форматтер, чтобы не падало при отсутствии tg_id в record
class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'tg_id'):
            record.tg_id = 'system'
        return super().format(record)

# Формат с полем tg_id
_fmt = '%(asctime)s - %(levelname)s - [tg:%(tg_id)s] - %(message)s'

# Применим SafeFormatter ко всем существующим обработчикам
root_logger = logging.getLogger()
for h in root_logger.handlers:
    h.setFormatter(SafeFormatter(_fmt))

# Инициализация бота и диспетчера
session = AiohttpSession(timeout=120.0)  # Общий таймаут 120 секунд

bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Глобальные хранилища данных
user_data_storage: Dict[int, dict] = {}      # Данные пользователей для тестирования
active_tests: Dict[int, bool] = {}           # Флаги активных тестов
temp_new_user_data: Dict[int, dict] = {}     # Временные данные новых пользователей



'''    
with open("data/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)
'''
# ======================
# СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЯ (FSM - Finite State Machine)
# ======================
class UserState(StatesGroup):
    """Класс состояний пользователя для управления диалогом"""
    awaiting_email = State()           # Ожидание ввода email
    verification = State()             # Подтверждение данных
    in_test = State()                  # В процессе тестирования
    new_user_email = State()           # Ввод email нового пользователя
    new_user_name = State()            # Ввод имени нового пользователя
    new_user_age = State()             # Ввод возраста нового пользователя
    new_user_gender = State()          # Ввод пола нового пользователя
    new_user_post = State()            # Ввод должности нового пользователя
    new_user_activity = State()        # Ввод обязанностей нового пользователя

# ======================
# ФУНКЦИИ ДЛЯ СОЗДАНИЯ КЛАВИАТУР
# ======================
def get_verification_keyboard():
    """Клавиатура для подтверждения данных пользователя"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, верно", callback_data="confirm_data"),
                InlineKeyboardButton(text="❌ Не верно", callback_data="not_correct")
            ]
        ]
    )

# ======================
# ОБРАБОТЧИКИ КОМАНД
# ======================
@dp.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    """
    Обработчик команды /start
    Выводит приветственное сообщение и запрашивает email
    """
    user_id = message.from_user.id
    user_logger = get_user_logger(user_id)
    
    # Проверяем, не идет ли уже тест
    if user_id in active_tests and active_tests[user_id]:
        #await message.answer("Пожалуйста, продолжайте тестирование!")
        msg = await message.answer("❌ Пожалуйста, продолжайте тестирование!")
        await asyncio.sleep(5)  # подождать 5 секунд
        await msg.delete()    
        return
    
    # Очищаем состояние и данные
    await state.clear()
    user_data_storage.pop(user_id, None)
    temp_new_user_data.pop(user_id, None)
    active_tests.pop(user_id, None)
    
    # Устанавливаем состояние ожидания email
    await state.set_state(UserState.awaiting_email)
    await state.update_data(email_attempts=0)
    
    # Приветственное сообщение
    await message.answer(
        "👋 Добро пожаловать в бот для диагностики по MBTI!\n\n"
        "Для начала работы введите ваш e-mail или код:\n"       
    )

@dp.message(Command("help"))
async def help_command(message: Message):
    """Обработчик команды /help - выводит справку"""
    user_id = message.from_user.id
    
    # Проверяем, не идет ли уже тест
    if user_id in active_tests and active_tests[user_id]:
        #await message.answer("Пожалуйста, продолжайте тестирование!")
        msg = await message.answer("❌ Пожалуйста, продолжайте тестирование!")
        await asyncio.sleep(5)  # подождать 5 секунд
        await msg.delete()    
        return
    
    help_text = (
        "📖 Справка по боту:\n\n"
        "1. Введите /start для начала работы\n"
        "2. Введите ваш email для идентификации или код для нового пользователя\n"
        "3. Подтвердите ваши данные\n"
        "4. Пройдите тестирование MBTI\n"
        "5. Получите результаты в виде файла\n\n"
        "Для перезапуска введите /start"
    )
    await message.answer(help_text)

# ======================
# ОБРАБОТКА ВВОДА EMAIL И ПРОВЕРКА ДАННЫХ
# ======================
@dp.message(UserState.awaiting_email)
async def handle_email_input(message: Message, state: FSMContext):
    """
    Обработка введенного email пользователем
    Проверяет наличие пользователя в базе данных
    """
    MAX_EMAIL_ATTEMPTS = 10 # Макс попыток ввода email, после чего сброс состояния
    user_id = message.from_user.id
    
    # Обновляем активность сессии
    session_manager = get_session_manager()
    session_manager.update_activity(user_id)
    session_manager.set_status(user_id, SessionStatus.PROCESSING)
    
    # Проверяем, не идет ли уже тест
    if user_id in active_tests and active_tests[user_id]:
        #await message.answer("Пожалуйста, продолжайте тестирование!")
        msg = await message.answer("❌ Пожалуйста, продолжайте тестирование!")
        await asyncio.sleep(5)  # подождать 5 секунд
        await msg.delete()    
        return
    
    email = message.text.strip()
    
    # Получаем данные из состояния и считаем попытки
    data = await state.get_data()
    attempts = data.get('email_attempts', 0) + 1
    await state.update_data(email_attempts=attempts)
    
    # Проверка на превышение попыток
    if attempts > MAX_EMAIL_ATTEMPTS:
        await state.clear()
        await message.answer(
            "❌ Слишком много неудачных попыток ввода email.\n"
            "Введите /start чтобы начать заново."
        )
        return
    
    # РЕЖИМ ПРОФ ТЕСТИРОВАНИЯ (999)
    if email == "999":
        await handle_prof_mode(user_id, state)
        return
    
    # РЕЖИМ ДОБАВЛЕНИЯ НОВОГО ПОЛЬЗОВАТЕЛЯ (000)
    if email == "0":
        await state.set_state(UserState.new_user_email)
        await message.answer(
            "🔧 Режим добавления нового пользователя.\n\n"
            "Введите email нового пользователя:"
        )
        return
    
    # ПОИСК ПОЛЬЗОВАТЕЛЯ В БАЗЕ ДАННЫХ
    user = get_user_by_email(email)
    if not user:
        await message.answer(
            f"❌ Email '{email}' не найден в базе данных.\n"
            f"Попытка {attempts}/{MAX_EMAIL_ATTEMPTS}\n\n"
            "Проверьте правильность email или обратитесь к администратору."
        )
        return
    
    # ПРОВЕРКА - ЕСЛИ ТЕСТ УЖЕ ПРОЙДЕН
    if "mbti" in user and user["mbti"] and user["mbti"] != "" and user["mbti"].lower() != "nan":
        await message.answer(
            f"ℹ️ Тест уже пройден для этого email.\n\n"
            "Введите /start чтобы начать заново."
        )
        await state.clear()
        return
    
    # СОХРАНЕНИЕ ДАННЫХ ПОЛЬЗОВАТЕЛЯ И ПОДТВЕРЖДЕНИЕ
    user_data = {
        "id": user["id"],
        "e-mail": user["email"],
        "name": user["name"],
        "age": user["age"],
        "gender": user["gender"],
        "post": user["post"],
        "activity": user["activity"],
        "MBTI": user["mbti"] or "",
        "report": user["report_path"] or ""
    }
    
    user_data_storage[user_id] = user_data
    
    # Выводим данные для подтверждения с кнопками
    verification_text = (
        f"✅ Пользователь найден!\n\n"
        f"📋 Проверьте ваши данные:\n\n"
        f"• Имя: {user_data['name']}\n"
        f"• Возраст: {user_data['age']}\n"
        f"• Пол: {user_data['gender']}\n"
        f"• Должность: {user_data['post']}\n"
        f"• Обязанности: {user_data['activity'][:100]}...\n\n"
        f"Верны ли данные?"
    )
    
    await message.answer(verification_text, reply_markup=get_verification_keyboard())
    await state.set_state(UserState.verification)

async def handle_prof_mode(user_id: int, state: FSMContext):
    """Обработка проф режима тестирования. Видны все тех коментарии"""
    user_data = {
        "id": 0,
        "e-mail": "guest@example.com",
        "name": "Пётр",
        "age": 30,
        "gender": "м",
        "post": "Админ",
        "activity": "Тестирование системы",
        "MBTI": "ENTJ",
        "report": ""
    }
    
    user_data_storage[user_id] = user_data
    
    # Запускаем тестирование сразу
    await state.set_state(UserState.in_test)
    active_tests[user_id] = True
    
    await safe_send_message(
        bot=bot,
        chat_id=user_id,
        text=f"✅ Режим тестирования системы.\n⏳ Запускаем тестирование MBTI..."
    )
    
    # Запускаем тестирование асинхронно
    asyncio.create_task(run_test_async(user_id))

# ======================
# ОБРАБОТЧИКИ ДЛЯ ВВОДА ДАННЫХ НОВОГО ПОЛЬЗОВАТЕЛЯ
# ======================
@dp.message(UserState.new_user_email)
async def handle_new_user_email(message: Message, state: FSMContext):
    """Обработка ввода email нового пользователя"""
    email = message.text.strip()
    
    if not email:
        await message.answer("❌ Некорректный email. Пожалуйста, введите корректный email:")
        return
    
    # Проверяем, не существует ли уже пользователь с таким email
    existing_user = get_user_by_email(email)
    if existing_user:
        await message.answer(
            f"❌ Пользователь с email '{email}' уже существует в базе данных.\n\n"
            f"Введите /start чтобы начать заново."
        )
        await state.clear()
        return
    
    # Сохраняем email во временное хранилище
    user_id = message.from_user.id
    temp_new_user_data[user_id] = {"e-mail": email}
    
    await state.set_state(UserState.new_user_name)
    await message.answer("✅ Email сохранен.\n\nВведите имя нового пользователя:")

@dp.message(UserState.new_user_name)
async def handle_new_user_name(message: Message, state: FSMContext):
    """Обработка ввода имени нового пользователя"""
    user_id = message.from_user.id
    name = message.text.strip()
    
    if not name:
        await message.answer("❌ Имя не может быть пустым. Введите имя:")
        return
    
    # Сохраняем имя
    if user_id in temp_new_user_data:
        temp_new_user_data[user_id]["name"] = name
    
    await state.set_state(UserState.new_user_age)
    await message.answer("✅ Имя сохранено.\n\nВведите возраст нового пользователя (число):")

@dp.message(UserState.new_user_age)
async def handle_new_user_age(message: Message, state: FSMContext):
    """Обработка ввода возраста нового пользователя"""
    user_id = message.from_user.id
    age_text = message.text.strip()
    
    try:
        age = int(age_text)
        if age < 1 or age > 120:
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректный возраст. Введите число от 1 до 120:")
        return
    
    # Сохраняем возраст
    if user_id in temp_new_user_data:
        temp_new_user_data[user_id]["age"] = age
    
    await state.set_state(UserState.new_user_gender)
    await message.answer("✅ Возраст сохранен.\n\nВведите пол нового пользователя (м/ж):")

@dp.message(UserState.new_user_gender)
async def handle_new_user_gender(message: Message, state: FSMContext):
    """Обработка ввода пола нового пользователя"""
    user_id = message.from_user.id
    gender = message.text.strip().lower()
    
    if gender not in ["м", "ж", "m", "f"]:
        await message.answer("❌ Некорректный пол. Введите 'м' или 'ж':")
        return
    
    # Приводим к русскому формату
    if gender == "m":
        gender = "м"
    elif gender == "f":
        gender = "ж"
    
    # Сохраняем пол
    if user_id in temp_new_user_data:
        temp_new_user_data[user_id]["gender"] = gender
    
    await state.set_state(UserState.new_user_post)
    await message.answer("✅ Пол сохранен.\n\nВведите должность нового пользователя:")

@dp.message(UserState.new_user_post)
async def handle_new_user_post(message: Message, state: FSMContext):
    """Обработка ввода должности нового пользователя"""
    user_id = message.from_user.id
    post = message.text.strip()
    
    if not post:
        await message.answer("❌ Должность не может быть пустой. Введите должность:")
        return
    
    # Сохраняем должность
    if user_id in temp_new_user_data:
        temp_new_user_data[user_id]["post"] = post
    
    await state.set_state(UserState.new_user_activity)
    await message.answer("✅ Должность сохранена.\n\nВведите обязанности нового пользователя:")

@dp.message(UserState.new_user_activity)
async def handle_new_user_activity(message: Message, state: FSMContext):
    """Обработка ввода обязанностей нового пользователя и завершение регистрации"""
    user_id = message.from_user.id
    activity = message.text.strip()
    
    if not activity:
        await message.answer("❌ Обязанности не могут быть пустыми. Введите обязанности:")
        return
    
    # Сохраняем обязанности и завершаем регистрацию
    if user_id in temp_new_user_data:
        temp_new_user_data[user_id]["activity"] = activity
        
        # Формируем полные данные нового пользователя
        new_user_data = temp_new_user_data[user_id]
        
        # Проверяем, что все поля заполнены
        required_fields = ["e-mail", "name", "age", "gender", "post", "activity"]
        if all(field in new_user_data for field in required_fields):
            # Добавляем нового пользователя в базу данных и Excel
            user_id_db = add_new_user_to_db(
                email=new_user_data["e-mail"],
                name=new_user_data["name"],
                age=new_user_data["age"],
                gender=new_user_data["gender"],
                post=new_user_data["post"],
                activity=new_user_data["activity"]
            )
            
            if user_id_db:
                # Формируем данные пользователя для тестирования
                user_data = {
                    "id": user_id_db,
                    "e-mail": new_user_data["e-mail"],
                    "name": new_user_data["name"],
                    "age": new_user_data["age"],
                    "gender": new_user_data["gender"],
                    "post": new_user_data["post"],
                    "activity": new_user_data["activity"],
                    "MBTI": "",
                    "report": ""
                }
                
                # Сохраняем в глобальное хранилище
                user_data_storage[user_id] = user_data
                
                # Показываем данные для подтверждения с кнопками
                verification_text = (
                    f"✅ Новый пользователь добавлен!\n\n"
                    f"📋 Проверьте данные:\n\n"
                    f"• Имя: {user_data['name']}\n"
                    f"• Возраст: {user_data['age']}\n"
                    f"• Пол: {user_data['gender']}\n"
                    f"• Должность: {user_data['post']}\n"
                    f"• Обязанности: {user_data['activity'][:100]}...\n\n"
                    f"Верны ли данные?"
                )
                
                await message.answer(verification_text, reply_markup=get_verification_keyboard())
                await state.set_state(UserState.verification)
            else:
                await message.answer("❌ Ошибка при добавлении пользователя. Введите /start чтобы начать заново.")
                await state.clear()
        
        # Очищаем временные данные
        del temp_new_user_data[user_id]

# ======================
# ОБРАБОТЧИКИ КНОПОК ПОДТВЕРЖДЕНИЯ ДАННЫХ
# ======================
@dp.callback_query(F.data == "confirm_data")
async def handle_confirm_data(callback: types.CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки подтверждения данных
    Запускает тестирование после подтверждения
    """
    user_id = callback.from_user.id
    
    # Проверяем, не идет ли уже тест
    if user_id in active_tests and active_tests[user_id]:
        await callback.answer("❌ Тестирование уже запущено!")
        return
    
    # Проверяем, что пользователь в состоянии verification
    current_state = await state.get_state()
    if current_state != UserState.verification.state:
        await callback.answer("❌ Некорректное состояние")
        return
    
    #await callback.answer("✅ Данные подтверждены!")
    
    # Устанавливаем флаг активного теста и запускаем
    active_tests[user_id] = True
    await state.set_state(UserState.in_test)
    
    await callback.message.edit_text(
        "✅ Данные подтверждены!\n⏳ Запускаем тестирование MBTI...",
        reply_markup=None
    )
    
    # Запускаем тестирование асинхронно
    asyncio.create_task(run_test_async(user_id))

@dp.callback_query(F.data == "not_correct")
async def handle_not_correct(callback: types.CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки "Не верно"
    Возвращает к вводу email
    """
    # Проверяем, что пользователь в состоянии verification
    current_state = await state.get_state()
    if current_state != UserState.verification.state:
        #await callback.answer("❌ Некорректное состояние")
        return
    
    #await callback.answer("❌ Данные не верны")
    
    # Очищаем данные пользователя
    user_id = callback.from_user.id
    user_data_storage.pop(user_id, None)
    temp_new_user_data.pop(user_id, None)
    
    # Возвращаемся к состоянию ввода email
    await state.set_state(UserState.awaiting_email)
    await state.update_data(email_attempts=0)  # Сбрасываем счетчик попыток
    
    # Отправляем новое сообщение с запросом email
    await callback.message.answer(
        "📧 Пожалуйста, введите ваш e-mail или код заново:",
        reply_markup=None
    )

# ======================
# ОБРАБОТЧИКИ КНОПОК ВЫБОРА 
# ======================

@dp.callback_query(F.data.startswith("choice_"))
async def process_choice_callback(callback_query: types.CallbackQuery):
    """
    Обработчик нажатий на кнопки выбора (a, b, q) во время тестирования
    """
    # отвечаем на callback 
    try:
        await callback_query.answer()
    except:
        pass  # Если уже умер - игнорируем
    
    # остальная логика
    chat_id = callback_query.message.chat.id
    user_logger = get_user_logger(chat_id)
    
    if chat_id in pending_answers:
        choice = callback_query.data.split("_")[1]
        user_logger.debug(f"Найден future для чата {chat_id}, выбор: {choice}")
        
        future = pending_answers[chat_id]
        if not future.done():
            future.set_result(choice)
            user_logger.info(f"✅ Выбрана кнопка: {choice}")           
            pending_answers.pop(chat_id)
            user_logger.debug(f"Удален future для чата {chat_id}")
        else:
            user_logger.warning(f"Future уже завершён для чата {chat_id}")
            if chat_id in pending_answers:
                pending_answers.pop(chat_id)                       
    else:
        user_logger.warning(f"НЕТ pending_answers для чата {chat_id}")
        try:
            await callback_query.answer("Ответ не принят (таймаут или ошибка)", show_alert=True)
        except:
            pass

    # Убираем кнопки после выбора        
    if callback_query.message and callback_query.message.reply_markup:
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
            user_logger.debug("✅ Кнопки 'choice_' удалены")
        except Exception as e:
            user_logger.debug(f"❌ Ошибка при удалении кнопок 'choice_': {e}") 

@dp.callback_query(F.data == "text_choice_q")
async def handle_text_q_button(callback_query: types.CallbackQuery):
    """
    Обработчик кнопки q для текстового режима
    Используется когда пользователь выбирает выход из текстового вопроса
    """
    # отвечаем на callback
    try:
        await callback_query.answer()  # Пустой ответ, быстро
    except:
        pass  # Если callback устарел - игнорируем
    
    # остальная логика
    chat_id = callback_query.message.chat.id
    user_logger = get_user_logger(chat_id)
    user_logger.info(f"✅ В беседе нажата кнопка 'Выход'.")
    
    # Проверяем, в режиме ли мы текстового ввода
    if hasattr(bot, '_waiting_text_responses') and chat_id in bot._waiting_text_responses:
        data = bot._waiting_text_responses[chat_id]
        future = data['future']
        
        if not future.done():
            future.set_result("q")
            user_logger.debug(f"✅ Future установлен в 'q' ")
        '''
        # Убираем кнопки
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
            #await callback_query.message.edit_text(f"{callback_query.message.text}\n\n✅ Выбран выход.")
            user_logger.debug("✅ Кнопки удалены")
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
        '''
    else:
        user_logger.warning(f"Нет ожидаемого текстового ответа для чата {chat_id}")
        try:
            await callback_query.answer("Нет активного вопроса", show_alert=True)
        except:
            pass

    # Убираем кнопки после выбора        
    if callback_query.message and callback_query.message.reply_markup:
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
            user_logger.debug("✅ Кнопка 'text_choice_q' удалена")
        except Exception as e:
            user_logger.debug(f"❌ Ошибка при удалении кнопки 'text_choice_q': {e}")   

# ======================
# ОБРАБОТЧИК ТЕКСТОВЫХ ОТВЕТОВ ВО ВРЕМЯ ТЕСТИРОВАНИЯ
# ======================
@dp.message()
async def handle_text_message(message: Message, state: FSMContext):
    """
    Обработчик текстовых сообщений
    Используется для приема текстовых ответов во время тестирования
    """
    user_id = message.from_user.id
    user_logger = get_user_logger(user_id)
    
    # Проверяем, ожидаем ли мы текстовый ответ от этого пользователя
    if hasattr(bot, '_waiting_text_responses') and user_id in bot._waiting_text_responses:
        if message.text is None or message.text == "":
            #await message.answer(f"❌ Ответ минимум 3 русских буквы.")
            msg = await message.answer("❌ Ответ минимум 3 русских буквы.")
            await asyncio.sleep(5)  # подождать 5 секунд
            await msg.delete()
            return
        user_resp = message.text.strip()
        config_data = bot._waiting_text_responses[user_id]        
        user_logger.debug(f"✅ Получен текстовый ответ: '{user_resp[:50]}...'")
        
        # Проверяем если пользователь ввел "q" для выхода
        if user_resp.lower() == 'q':
            user_logger.debug(f"Пользователь {user_id} ввёл 'q' в тексте")
            future = config_data['future']
            if not future.done():
                future.set_result("q")
                await message.answer("✅ Выбран выход (q)")
            return
        
        # Проверяем длину ответа
        if len(user_resp) > config_data['max_length']:
            #await message.answer(f"❌ Ответ максимум {config_data['max_length']} символов.")
            msg = await message.answer(f"❌ Ответ максимум {config_data['max_length']} символов.")
            await asyncio.sleep(5)  # подождать 5 секунд
            await msg.delete()
            return
        
        # Проверяем русский текст
        if is_russian_text(user_resp, config_data['min_russian_chars']):
            # Успешный ответ - отправляем в Future
            future = config_data['future']
            if not future.done():
                future.set_result(user_resp)
        else:
            #await message.answer(f"❌ Ответ минимум {config_data['min_russian_chars']} русских буквы."          )
            msg = await message.answer("❌ Ответ минимум 3 русских буквы.")
            await asyncio.sleep(5)  # подождать 5 секунд
            await msg.delete()
        return
    
    # Если сообщение не является ответом на текстовый вопрос теста
    current_state = await state.get_state()
    
    # Если идет тестирование, но это не ответ на текстовый вопрос
    if user_id in active_tests and active_tests[user_id]:
        #await message.answer("Пожалуйста, продолжайте тестирование!")
        msg = await message.answer("❌ Пожалуйста, продолжайте тестирование!")
        await asyncio.sleep(5)  # подождать 5 секунд
        await msg.delete()        
        return
    
    # Если пользователь не в процессе тестирования и не в состоянии ввода данных
    if current_state is None:
        await message.answer("Для начала тестирования введите команду /start")
        return
    
    # Если пользователь в каком-то состоянии, но это не обработка теста
    # (этот код не должен выполняться, т.к. все состояния обрабатываются выше)
    await message.answer(
        "Пожалуйста, следуйте инструкциям или введите /start для начала заново."
    )

# ======================
# ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ
# ======================
def init_db():
    """Инициализация базы данных SQLite, создание таблицы если не существует"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            age INTEGER,
            gender TEXT,
            post TEXT,
            activity TEXT,
            mbti TEXT,
            report_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def sync_excel_to_db():
    """
    Синхронизация данных из Excel файла в базу данных SQLite
    Обновляет существующие записи по email
    """
    if not Path(EXCEL_PATH).exists():
        logger.warning(f"❌ Файл {EXCEL_PATH} не найден")
        return

    logger.info(f"✅ Синхронизация данных из Excel в БД с обновлением записей")
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_excel(EXCEL_PATH, dtype=str)

    # Приводим email к нижнему регистру для единообразия
    df['e-mail'] = df['e-mail'].astype(str).str.strip().str.lower()

    for _, row in df.iterrows():
        email = row.get('e-mail', '').strip()
        if not email or email.lower() in ('nan', ''):
            continue

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users 
            (email, name, age, gender, post, activity, mbti, report_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name=excluded.name,
                age=excluded.age,
                gender=excluded.gender,
                post=excluded.post,
                activity=excluded.activity,
                mbti=excluded.mbti,
                report_path=excluded.report_path
        """, (
            email,
            str(row.get('name', '')).strip(),
            int(row.get('age')) if pd.notna(row.get('age')) else None,
            str(row.get('gender', '')).strip(),
            str(row.get('post', '')).strip(),
            str(row.get('activity', '')).strip(),
            str(row.get('MBTI', '')).strip() or None,
            str(row.get('report', '')).strip() or None
        ))
    conn.commit()
    conn.close()
    logger.info("✅ Данные из Excel синхронизированы с обновлением")

def get_user_by_email(email: str) -> Optional[dict]:
    """Получение пользователя из базы данных по email"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    # Преобразуем результат запроса в словарь
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))

def add_new_user_to_db(email: str, name: str, age: int, gender: str, post: str, activity: str) -> Optional[int]:
    """Добавление нового пользователя в базу данных и Excel"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Вставляем нового пользователя в базу данных
        cursor.execute("""
            INSERT INTO users 
            (email, name, age, gender, post, activity, mbti, report_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email.lower(),
            name,
            age,
            gender,
            post,
            activity,
            None,  # mbti будет заполнен после тестирования
            None   # report_path будет заполнен после тестирования
        ))
        
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Новый пользователь добавлен в БД: {email}, ID: {user_id}")
        
        # Добавляем в Excel файл
        add_new_user_to_excel(email, name, age, gender, post, activity)
        
        return user_id
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении нового пользователя в БД: {e}")
        return None

def add_new_user_to_excel(email: str, name: str, age: int, gender: str, post: str, activity: str):
    """Добавление нового пользователя в Excel файл"""
    try:
        if not Path(EXCEL_PATH).exists():
            # Создаем новый Excel файл с заголовками
            df = pd.DataFrame(columns=['e-mail', 'name', 'age', 'gender', 'post', 'activity', 'MBTI', 'report'])
            df.to_excel(EXCEL_PATH, index=False)
        
        df = pd.read_excel(EXCEL_PATH, dtype=str)
        
        # Создаем новую строку
        new_row = {
            'e-mail': email,
            'name': name,
            'age': str(age),
            'gender': gender,
            'post': post,
            'activity': activity,
            'MBTI': '',
            'report': ''
        }
        
        # Добавляем новую строку в DataFrame
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        
        # Сохраняем обратно в Excel
        df.to_excel(EXCEL_PATH, index=False)
        
        logger.info(f"✅ Новый пользователь добавлен в Excel: {email}")
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении нового пользователя в Excel: {e}")

def save_test_result(email: str, final_type: str, report_path: str):
    """Сохранение результата теста в базу данных"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET mbti = ?, report_path = ?
        WHERE email = ?
    """, (final_type, report_path, email.strip().lower()))
    conn.commit()
    conn.close()
    logger.info(f"✅ Результат сохранен для {email}: {final_type}")

def update_excel_with_result(email: str, final_type: str, report_path: str):
    """Обновление Excel файла с результатами теста"""
    try:
        if not Path(EXCEL_PATH).exists():
            return

        df = pd.read_excel(EXCEL_PATH, dtype=str)
        email_clean = email.strip().lower()
        
        # Ищем строку с нужным email
        mask = df['e-mail'].astype(str).str.strip().str.lower() == email_clean

        if mask.any():
            # Обновляем данные
            df.loc[mask, 'MBTI'] = final_type
            df.loc[mask, 'report'] = report_path
            # Сохраняем обратно в Excel
            df.to_excel(EXCEL_PATH, index=False)
            logger.info(f"✅ Excel обновлен: {email} → {final_type}")
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении Excel: {e}")

def convert_path_to_string(path_obj):
    """Безопасное преобразование Path объекта в строку"""
    if isinstance(path_obj, Path):
        return str(path_obj)
    elif hasattr(path_obj, '__str__'):
        return str(path_obj)
    else:
        return path_obj

# ======================
# ФУНКЦИЯ ЗАПУСКА ТЕСТИРОВАНИЯ
# ======================
async def run_test_async(user_id: int):
    """
    Асинхронный запуск тестирования MBTI
    Основная функция для проведения тестирования
    """
    user_logger = get_user_logger(user_id)
    try:
        # Получаем менеджер сессий и обновляем статус
        session_manager = get_session_manager()
        session_manager.get_or_create_session(user_id)
        session_manager.set_status(user_id, SessionStatus.ACTIVE)
        session_manager.update_activity(user_id)
        
        # Получаем данные пользователя из хранилища
        user_data = user_data_storage.get(user_id)
        if not user_data:
            await safe_send_message(bot=bot, chat_id=user_id, text="❌ Данные пользователя не найдены. Введите /start чтобы начать снова.")
            return
        
        user_config = config.copy()  # Копируем конфигурацию
      
        # В приватных чатах chat_id совпадает с user_id
        chat_id = user_id
        
        user_logger.info(f"✅ Запуск тестирования для ({user_data['name']})")
        

        # Запускаем основной цикл тестирования  
        final_type_perc, final_type, report_file = await testing(user_config, user_data, bot, chat_id, logger)

        # Проверяем результат тестирования
        if final_type_perc and final_type_perc != "" and report_file:
            # Сохраняем результат в базу данных (исключая тестовые аккаунты)
            if user_data["e-mail"] not in ("guest@example.com", "1"):
                report_file_str = convert_path_to_string(report_file)
                save_test_result(user_data["e-mail"], final_type_perc, report_file_str)
                update_excel_with_result(user_data["e-mail"], final_type_perc, report_file_str)

            # Информируем пользователя об окончании тестирования
            await safe_send_message(
                bot=bot,
                chat_id=user_id,
                text=f"✅ Тестирование успешно завершено!\n\n🎯 Ваш тип MBTI: *{final_type}*",
                parse_mode="Markdown"
            )           
            
            # Отправляем файл с описанием психотипа, если он существует
            if final_type and final_type != "":
                #psychotypes_dir = Path("data/Decoding_psychotypes")

                psychotypes_dir = Path(project_root) / config.get('decoding_path')
                pdf_file = psychotypes_dir / f"{final_type}.pdf"

                if pdf_file.exists():
                    psychotype_doc = FSInputFile(pdf_file)
                    await safe_send_document(
                        bot=bot,
                        chat_id=user_id,
                        document=psychotype_doc,
                        caption=f"📖 *Описание вашего психотипа {final_type}*",
                        parse_mode="Markdown"
                    )
                    user_logger.info(f"✅ Файл {final_type}.pdf отправлен ({user_data['name']})")
                else:
                    user_logger.warning(f"❌ Файл {final_type}.pdf не найден в {psychotypes_dir}")

            user_logger.info(f"✅ Тестирование завершено для ({user_data['name']}), тип: {final_type}")
        else:
            # Если тестирование не удалось
            await safe_send_message(
                bot=bot,
                chat_id=user_id,
                text="❌ Тестирование не удалось завершить.\n\nВведите /start чтобы попробовать снова"
            )
            user_logger.info(f"❌ Тестирование не удалось для ({user_data['name']})")
       
    except Exception as e:
        # Обработка ошибок при тестировании
        try:
            user_logger = get_user_logger(user_id)
        except Exception as log_exc:
            logger.warning(f"❌ Не удалось получить пользовательский логгер для user_id={user_id}: {log_exc}")
            user_logger = logger

        user_logger.error(f"❌ Ошибка при тестировании ({user_data['name']}): {e}", exc_info=True)
        await safe_send_message(
            bot=bot,
            chat_id=user_id,
            text=f"❌ Ошибка при тестировании: {str(e)}\n\nВведите /start чтобы попробовать снова"
        )
    finally:
        # Снимаем флаг активного теста и очищаем состояние
        active_tests.pop(user_id, None)
        pending_answers.pop(user_id, None)
        
        # Завершаем сессию в менеджере
        try:
            session_manager = get_session_manager()
            session_manager.terminate_session(user_id, "Завершено тестирование")
            user_logger.debug(f"✅ Сессия пользователя ({user_data['name']}) завершена")
        except Exception as e:
            user_logger.warning(f"❌ Ошибка при завершении сессии ({user_data['name']}): {e}")

# ======================
# НАСТРОЙКА КОМАНД БОТА
# ======================
async def set_commands():
    """Установка команд бота для меню"""
    commands = [
        BotCommand(command="start", description="Начать тестирование"),
        BotCommand(command="help", description="Показать справку"),
    ]
    await bot.set_my_commands(commands)

# ======================
# ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА БОТА
# ======================
async def main():
    """Главная функция запуска бота"""
    # Инициализация системы управления сессиями
    session_manager = init_session_manager(
        session_timeout=1800,       # 30 минут неактивности
        cleanup_interval=660,       # Проверка каждые 11 минут
        max_consecutive_failures=3,  # 3 критические ошибки
        admin_user_id=None,         # Будет использован при необходимости
        logger=logger
    )
    logger.info("✅ SessionManager инициализирован")
    
    # Инициализация базы данных
    init_db()
    
    # Синхронизация с Excel
    sync_excel_to_db()
    
    # Установка команд бота
    await set_commands()
    
    logger.info("🚀 Бот запущен и готов к работе")
    
    # Запуск фоновой задачи очистки сессий
    await session_manager.start_cleanup_task()
    logger.debug("🧹 Фоновая задача очистки сессий запущена")
    
    # Запуск polling для получения обновлений
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")
    finally:
        # Остановить задачу очистки
        await session_manager.stop_cleanup_task()
        await bot.session.close()
        logger.info("🛑 Бот остановлен")

# ======================
# ТОЧКА ВХОДА В ПРОГРАММУ
# ======================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Бот остановлен адинистратором!")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")