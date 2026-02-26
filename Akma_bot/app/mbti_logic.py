import os
import re
import json
import time
import random
import datetime
import pytz
from pathlib import Path
import pandas as pd
import tiktoken

import textwrap
from openai import OpenAI
from typing import List, Union, Tuple, Dict, Optional, Any
from dotenv import load_dotenv
import logging
from aiogram import Bot
import asyncio
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import logging
from typing import Optional
import yaml
import edge_tts
import pygame
import io
from aiogram.types import BufferedInputFile 
import pyttsx3

import tempfile
from gtts import gTTS




# Импорт системы управления сессиями и безопасных функций отправки
from .session_manager import (
    get_session_manager, SessionStatus, ErrorSeverity,
    init_session_manager
)
from .safe_messages import (
    safe_send_message, safe_edit_message, safe_delete_message, safe_send_document,
    get_user_logger as get_safe_logger
)

# Глобальный словарь для хранения ожиданий ответов
pending_answers = {}

logger = logging.getLogger(__name__) 
def get_user_logger(tg_id):
    """Возвращает LoggerAdapter с полем tg_id (строка) или 'no-id' если отсутствует."""
    try:
        tg = str(tg_id) if tg_id is not None else "no-id"
    except Exception:
        tg = "no-id"
    return logging.LoggerAdapter(logger, {"tg_id": tg})


# Если запускается самостоятельно — убедимся, что формат логов содержит tg_id
class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'tg_id'):
            record.tg_id = 'system'
        return super().format(record)

_fmt = '%(asctime)s - %(levelname)s - [tg:%(tg_id)s] - %(message)s'
root_logger = logging.getLogger()
if not root_logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(SafeFormatter(_fmt))
    root_logger.addHandler(h)
else:
    for h in root_logger.handlers:
        h.setFormatter(SafeFormatter(_fmt))
root_logger.setLevel(logging.INFO)

# Module-level adapter used when chat_id is not provided (standalone run)
user_logger = get_user_logger(None)

# Промпты

def get_akma_system_prompt(size_query=150, name="Пётр", age=30, gender="мужской", post="директор", activity="согласно должностной  инструкции"):
    """
    Формирует нулевой вопрос для начала диалога и system_prompt Akma для беседы с LLM
    """    
    zero_question = f'''Здравствуйте, {name}! Я Акма — нейро-консультант. Расскажите, пожалуйста, как обычно проходит ваш рабочий день, чем вы занимаетесь и за что вы отвечаете?'''
    
    gender_clean = gender.strip().lower()
    if gender_clean in ["мужчина", "мужской", "male", "man"] or gender_clean.startswith('м'):
        style = "спокойна, дружелюбна, романтична, проявляешь лёгкий интеллектуальный флирт, с интересом к деталям его ответов"
    else:
        style = "спокойна, дружелюбна, внимательна к эмоциям и нюансам ответов"

    akma_system_prompt = f'''Ты — женщина Акма, ведущий психолог и акмеолог. Ты {style}. Ты лучше всех в связанной беседе задаешь вопросы для определения предпочтения по заданной оси MBTI.
Пользователь: {name}, {age} лет, пол -{gender}, занимает должность – {post} и выполняет работы {activity}
Задача: с Пользователь обращайся по имени. Веди связную беседу с Пользователь по его должности, обязанностям и выполняемой работе. 
Поддерживай и направляй Пользователь мягко, создавая комфортную и уверенную атмосферу. Обрати внимание на его эмоции. 
Задавай только новые вопросы, которые еще не задавала, для определения его предпочтения по заданной оси MBTI.
Правила:
    Запрещено повторять или переформулировать вопросы, которые уже были, есть в истории диалога или "role":"assistant".   
    Запрещено снова задавать тот же вопрос.
    Всегда коротко реагируй на последний ответ Пользователь - процитируй или похвали.
    На все вопросы Пользователь отвечай мягко, что это не относится к данной беседе и предложи продолжить диалог.   
    Если ответ Пользователь непонятен или не даёт точно определить его предпочтение, мягко, без давления попроси точнее формулировать ответ, 
        а если возникнут трудности с выбором, просто выбирать вариант, который ему ближе.   
    Всегда плавно переходи к следующему вопросу, связав его с предыдущими ответами пользователя для определения его предпочтения MBTI по указанной оси.
    В вопросе, не давай прямого выбора вариантов ответа, тем более где первый вариант это предпочтение по первой букве оси, а второй по второй букве.  
    Выдавай ответ всегда только на русском языке меньше {size_query} токенов.
    Запрещено в ответе выдавать </think>, тесты, смайлы, эмодзи, кавычки, ', ", *, термины MBTI («экстраверсия», «предпочтение», «ось», и т.д.) и какую-либо разметку.'''

    return zero_question, akma_system_prompt


def get_akma_local_prompt(axis, err_result_letter=0, max_err_result_letter=3):
    """
    Формирует вопрос akma по текущей оси в беседе с LLM
    """
    local_prompt ='''Обязательно кратко отреагируй(процетируй или поддержи или похвали или извинись, но только если есть за что) на последний Ответ Пользователь, чтобы показать, что ты его услышала.'''
    
    if err_result_letter > 0: # последний ответ не точный и не дал оценить       
        local_prompt +=''' Аккуратно и вежливо сообщи Пользователю, что его последний ответ был недостаточно точен, и попроси отвечать точнее. Объясни, что если Пользователь не знает, какой вариант выбрать, пусть выберет тот, который ему ближе. Напомни, что если ответы снова будут не точны, то ваша беседа, к сожалению, будет прервана и Пользователь сможет продолжить тест только в классическом формате.'''
        if err_result_letter >= max_err_result_letter-1:
            local_prompt +='''Вежливо сообщи Пользователю, что если и следующий ответ будет не точен, то беседа будет прервана и он сможет продолжить тест в привычном классическом формате, но уже без тебя.'''
    
    local_prompt +=f''' С новой спроки, после переноса строки, задай только один новый, не похожий на заданные ранее, вопрос направленный непосредственно на определение его предпочтения по оси "{axis}" по MBTI. Обязательно свяжи вопрос с предыдущими ответами Пользователь.'''        
    #print(f"local_prompt = {local_prompt}")
    return local_prompt


def get_analis_prompt(akma_question, user_resp, axis):
    """
    Формирует промпт для выбора предпочтения(буквы) MBTI по вопросу и ответу
    """
    system_content = "Ты профессиональный акмеолог-психолог по определению предпочтения по оси теста MBTI."
    user_content = (f"""На ВОПРОС: "{akma_question}".\n Пользователь дал ОТВЕТ: "{user_resp}".
Определи, если это возможно, по ОТВЕТ Пользователь на ВОПРОС его предпочтение "{axis[0]}" или "{axis[1]}" по оси "{axis}" MBTI. 
Если не можешь точно выбрать, определить предпочиение Пользователь, по оси "{axis}" по его ОТВЕТ верни "x".
Если ОТВЕТ не является ответом на ВОПРОС верни "x". 
Если ВОПРОС или ОТВЕТ не понятен, верни "x". 
Если не уверен в предпочтении Пользователь, верни "x".
Ответь только JSON-объектом с ключом "choice", где значение только 1 буква "{axis[0]}", "{axis[1]}" или "x", без дополнительного текста.
Пример правильного ответа: {{"choice": "x"}}""")
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]

def get_final_prompt(basic_type, size_query):
    """
    Формирует промпт для характеристики типа личности MBTI
    """
    system_content = "Ты профессиональный акмеолог-психолог и лучше всех характеризуешь тип личности по тесту MBTI."
    user_content = (f"""Ответь строго только на русском языке меньше {2*size_query} токенов. 
Запрещено в ответе выдавать </think>, тесты, смайлы, эмодзи, ', ", *, Markdown и какую-либо разметку.
По результатам теста MBTI выявлен тип личности: {basic_type}. Перечисли для типа {basic_type} не более трех:
cильные стороны -
cлабые стороны -
рекомендации для развития и обучения -
подходяшие профессии и должности -"""
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]


def get_demo_system_prompt(size_answer=80, name="Пётр", age=30, gender="мужской", post="директор", activity="согласно должностной  инструкции"):
    """
    Формирует промпт для ответов demo в беседе с LLM
    """
    return f'''Ты — {name}, {age} лет, пол-{gender} занимаешь должность – {post} и выполняешь работы {activity}.
Задача: Ответь на вопрос точно и четко как человек, согласно полученному предпочтению оси по тесту MBTI. Не называй свое предпочтение никогда.
Правила:
    Не задавай вопросы.
    Ответ — строго только на русском языке меньше {size_answer} токенов.
    Запрещено в ответе выдавать </think>, тесты, смайлы, эмодзи, кавычки, ', ", *, термины MBTI («экстраверсия», «интуиция» и т.д.)и какую-либо разметку.'''


def get_demo_user_prompt(akma_question, expected_letter, axis):
    """
    Формирует ответ demo по текущей оси в беседе с LLM
    """
    return f'''Ответь только на вопрос: "{akma_question}", как человек со 100% предпочтением "{expected_letter}" по оси "{axis}" по MBTI.'''



def get_demo_t_prompt(q_text, opt_a, opt_b, expected_letter, axis):
    """
    Формирует промпт для ответов demo на классический тест
    """
    system_content = "Ты — система прохождения теста MBTI."
    user_content = (f"""Вопрос: {q_text}
Вариант a: {opt_a}
Вариант b: {opt_b}
Какой вариант соответствует предпочтению "{expected_letter}" по оси "{axis}" по тесту MBTI? Если ни один из вариантов не подходит верни 'x'.
Ответь только JSON-объектом с ключом "choice", где значение только 1 буква "a", "b" или "x", без дополнительного текста.
Пример правильного ответа: {{"choice": "x"}}"""
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]


# Вспомогательные функции

def get_demo_user(user_id="1"):
    """
    Возвращает данные пользователя по его ID.
    Если пользователя с таким ID нет — возвращает первого пользователя.
    Параметры:
        user_id (str или int): идентификатор пользователя.
    Возвращает:
        dict: словарь с данными пользователя.
    """
    users_data = {
        "1": {
            "id": 1,
            "e-mail": "0",
            "name": "Пётр",
            "age": 30,
            "gender": "мужской",
            "post": "директор",
            "activity": "согласно должностной инструкции",
            "demo_MBTI": "ENTJ"
        },
        "2": {
            "id": 2,
            "name": "Анна",
            "age": 25,
            "gender": "женский",
            "post": "менеджер",
            "activity": "ведение проектов",
            "demo_MBTI": "ENFP"
        },
        "3": {
            "id": 3,
            "name": "Иван",
            "age": 28,
            "gender": "мужской",
            "post": "бухгалтер",
            "activity": "согласно должностной инструкции",
            "demo_MBTI": "ISTJ"
        }
    }
    # Приведение к строке на случай, если передан int
    user_id = str(user_id)
    # Если ID существует — возвращаем его, иначе — первого пользователя
    if user_id in users_data:
        return users_data[user_id]
    else:
        first_user_id = next(iter(users_data))  # Получаем первый ключ
        return users_data[first_user_id]


def collect_user_data(config: Dict) -> Dict:
    """
    Собирает данные о пользователе.
    В текущей версии используется только через Telegram, консольный ввод отключен.
    Возвращает данные демо-пользователя или загруженные данные.
    """
    user_id = "1"
    user = {}
    
    # Используем демо-данные по умолчанию
    user = get_demo_user(user_id)        
    return user


def normalize_user_choice(choice: str) -> str:
    """Преобразует ввод пользователя к 'a' или 'b' (или 'q')"""
    choice = choice.strip().lower()
    if choice in ['a', 'а', '1', 'аа']: return 'a'
    if choice in ['b', 'б', 'в', '2', 'бб', 'вв']: return 'b'
    if choice in ['q', 'quit', 'exit', 'выход']: return 'q'
    return None

def is_russian_text(text: str, min_length: int = 3) -> bool:
    """
    Проверяет, содержит ли строка хотя бы min_length русских букв.
    Считаем, что текст "русский", если в нём есть хотя бы несколько кириллических символов.
    """
    if not isinstance(text, str) or len(text) < min_length:
        return False
    # Ищем кириллические символы (включая ё/Ё)
    cyrillic_chars = re.findall(r'[а-яА-ЯёЁ]', text)
    return len(cyrillic_chars) >= min_length

# Удаляет разметки и ссылки
def remove_markdown_keep_content(text):
    """
    Удаляет разметку Markdown, сохраняя текстовое содержимое.    
    Args:
        text (str): Текст с Markdown разметкой        
    Returns:
        str: Текст без разметки
    """
    if not text:
        return text
    
    # Удаление заголовков (### Текст -> Текст)
    text = re.sub(r'#+\s+', '', text)
    
    # Удаление жирного текста (**Текст** -> Текст)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    
    # Удаление курсива (*Текст* -> Текст)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    
    # Удаление кода (`Текст` -> Текст)
    text = re.sub(r'`(.*?)`', r'\1', text)
    
    # Удаление зачеркнутого текста (~~Текст~~ -> Текст)
    text = re.sub(r'~~(.*?)~~', r'\1', text)
    
    # Удаление ссылок [Текст](URL) -> Текст
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # Удаление изображений ![Alt](URL) -> Alt
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
    
    # Удаление символа цитирования в начале строк (> Текст -> Текст)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)

    # Удаляем эмодзи
    text = re.sub(r'['
                r'U0001F600-U0001F64F'  # эмоции смайлы
                r'U0001F300-U0001F5FF'  # символы и пиктограммы
                r'U0001F680-U0001F6FF'  # транспорт и карты
                r'U0001F1E0-U0001F1FF'  # флаги
                r'U00002700-U000027BF'  # дополнительные символы
                r'U000024C2-U0001F251'
                r']+', '', text, flags=re.UNICODE)
    
    # Удаление горизонтальных линий (только отдельные строки с ---, ***, ___)
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        # Проверяем, является ли строка горизонтальной линией
        stripped_line = line.strip()
        if re.fullmatch(r'[-*_]{3,}', stripped_line):
            continue  # Пропускаем эту строку
        cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)
    
    # Удаление HTML-тегов (сохраняя текст внутри)
    text = re.sub(r'<[^>]+>', '', text)
    
    # Очистка от одиночных символов разметки, которые могли остаться
    # Важно: не удаляем символы, если они являются частью слова
    text = re.sub(r'\s([*_`])\s', ' ', text)  # Одиночные символы с пробелами вокруг
    text = re.sub(r'\s([*_`])$', '', text)    # В конце строки
    text = re.sub(r'^([*_`])\s', '', text)    # В начале строки
    
    # Удаление лишних пробелов и пустых строк
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()

async def speak_text(
    text: str,
    voice: str = "ru-RU-SvetlanaNeural",
    rate: str = "+8%",
    volume: float = 0.8,
    bot: Optional[object] = None,
    chat_id: Optional[int] = None,
    send_as_voice: bool = True
) -> bool:
    """
    Озвучивает текст с fallback по приоритету:
    1. Edge TTS в OGG (основной, но может не работать из-за 403)
    2. pyttsx3 (локальный, оффлайн)
    3. gTTS (облачный Google)
    Если всё упало - возвращает False, ничего не озвучивая.
    """    
    
    user_logger = get_user_logger(chat_id)
    
    # === 1. Подготовка текста ===
    clean_text = remove_markdown_keep_content(text)
    if not clean_text:
        user_logger.warning("Текст пустой после очистки")
        return False
    
    audio_data = None
    audio_format = None
    
    # === 2. УРОВЕНЬ 1: Edge TTS в OGG ===
    try:
        #logger.info("Уровень 1: Пробуем Edge TTS...")
        params = {"rate": rate} if rate else {}
        communicate = edge_tts.Communicate(clean_text, voice, **params)
        
        audio_chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.extend(chunk["data"])
        
        if audio_chunks:
            audio_data = bytes(audio_chunks)
            audio_format = "ogg"
            user_logger.info("Успешно: Edge TTS в OGG")
        else:
            raise Exception("Пустые данные от Edge TTS")
            
    except Exception as e:
        user_logger.warning(f"Edge TTS не сработал: {e}")
        
        # === 3. УРОВЕНЬ 2: pyttsx3 ===
        try:
            #logger.info("Уровень 2: Пробуем pyttsx3...")            
            engine = pyttsx3.init()
            
            # Настройка голоса (пробуем найти русский)
            voices = engine.getProperty('voices')
            for v in voices:
                if 'russian' in v.name.lower() or 'ru' in v.id.lower():
                    engine.setProperty('voice', v.id)
                    break
            
            # Сохраняем во временный файл
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                tmp_path = tmp.name
            
            try:
                engine.save_to_file(clean_text, tmp_path)
                engine.runAndWait()  # Блокирующий вызов
                
                # Читаем файл в память
                with open(tmp_path, 'rb') as f:
                    audio_data = f.read()
                audio_format = "mp3"
                
                user_logger.info("Успешно: pyttsx3")
            finally:
                # Удаляем временный файл
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                    
        except Exception as e:
            user_logger.warning(f"pyttsx3 не сработал: {e}")
            
            # === 4. УРОВЕНЬ 3: gTTS ===
            try:
                #logger.info("Уровень 3: Пробуем gTTS...")              
                
                tts = gTTS(text=clean_text, lang='ru')
                audio_io = io.BytesIO()
                tts.write_to_fp(audio_io)
                audio_data = audio_io.getvalue()
                audio_format = "mp3"
                
                user_logger.info("Успешно: gTTS")
                
            except Exception as e:
                user_logger.error(f"❌ gTTS не сработал: {e}")
                user_logger.error("❌ Все методы TTS не сработали, озвучка отменена.")
                return False
    
    # Если ни один метод не дал аудиоданных
    if not audio_data:
        return False
    
    # === 5. Отправка в Telegram или локальное воспроизведение ===
    if bot is not None and chat_id is not None:
        try:
            filename = f"speech.{audio_format}"
            audio_file = BufferedInputFile(file=audio_data, filename=filename)
            
            if send_as_voice:
                await bot.send_voice(chat_id=chat_id, voice=audio_file)
            else:
                await bot.send_audio(chat_id=chat_id, audio=audio_file)
            
            user_logger.info("Звуковой файл оправлен в Telegram")            
            return True
            
        except Exception as e:
            user_logger.error(f"❌ Ошибка отправки звук файла в Telegram: {e}")
            return False
    
    else:
        # Локальное воспроизведение через pygame
        try:
            pygame.mixer.init()
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))
            
            audio_stream = io.BytesIO(audio_data)
            pygame.mixer.music.load(audio_stream)
            pygame.mixer.music.play()
            
            # Ожидание окончания
            while pygame.mixer.music.get_busy():
                await asyncio.sleep(0.05)
            
            pygame.mixer.quit()
            return True
            
        except Exception as e:
            user_logger.error(f"❌ Ошибка локального воспроизведения: {e}")
            return False

def send_to_llm_with_validation(
    client,
    messages: List[dict],
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 256,
    expected_format: str = "any",
    valid_choices: Union[str, List[str]] = "a,b",
    max_retries: int = 2,
    chat_id: Optional[int] = None
) -> Tuple[str, int, int, float]:
    """
    Отправляет запрос к LLM и ждёт ответ в заданном формате.
    Особенность для expected_format='json_choice':
      - LLM может вернуть "Choice": "A", "a", "B", "b" — регистр НЕ важен.
      - Но функция вернёт значение **в том виде, как оно указано в valid_choices**.
        Пример:
          valid_choices = "A,B" → разрешены "a"/"A" и "b"/"B", но возвращается "A" или "B"
          valid_choices = ["Да", "Нет"] → возвращается "Да", даже если LLM прислал "да"
    Параметры:
        client — OpenAI-совместимый клиент
        messages — список сообщений
        model — название модели
        temperature, max_tokens — параметры генерации
        expected_format — "any", "russian", "json_choice"
        valid_choices — строка "A,B" или список ["A", "B"] (регистр важен для возврата!)
        max_retries — сколько раз повторять запрос при невалидном ответе
        chat_id — ID чата для логирования (опционально)
    Возвращает: (content, input_tokens, output_tokens, elapsed_sec)
        content = "err", если все попытки провалились
    """
    user_logger = get_user_logger(chat_id)
    
    # === Подготовка для режима json_choice ===
    choice_mapping: Dict[str, str] = {}  # lowercase -> оригинальный вариант
    if expected_format == "json_choice":
        # Приводим valid_choices к списку
        if isinstance(valid_choices, str):
            raw_list = [c.strip() for c in valid_choices.split(",") if c.strip()]
        else:
            raw_list = [str(c).strip() for c in valid_choices if str(c).strip()]

        if not raw_list:
            raise ValueError("valid_choices не может быть пустым при expected_format='json_choice'")

        # Создаём маппинг: нижний регистр → оригинальный вариант
        choice_mapping = {}
        for orig in raw_list:
            key = orig.lower()
            if key in choice_mapping:
                user_logger.warning(f"⚠️ Дубликат при нормализации: '{orig}' и '{choice_mapping[key]}' → будет использоваться первый")  
            else:
                choice_mapping[key] = orig

    def _count_tokens(text: str) -> int:
        """Подсчёт токенов с fallback-оценкой."""
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return max(0, len(text) // 3)

    # Переменные для fallback-ответа
    last_raw_content = ""
    last_input_tokens = 0
    start_time_total = time.time()

    for attempt in range(max_retries + 1):
        start_time = time.time()
        raw_content = ""
        input_tokens = 0
        output_tokens = 0
        usage = None

        try:
            # === Запрос к модели ===
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            elapsed = time.time() - start_time
            raw_content = (response.choices[0].message.content or "").strip()
            usage = response.usage
            last_raw_content = raw_content

            # === Подсчёт токенов ===
            if usage:
                input_tokens = usage.prompt_tokens
                output_tokens = usage.completion_tokens
                user_logger.debug(f"input_tokens usage: {input_tokens}")  
                user_logger.debug(f"output_tokens usage {output_tokens}") 
               
            else:
                input_text = " ".join([msg["content"] for msg in messages if msg.get("content")])
                input_tokens = _count_tokens(input_text)
                output_tokens = _count_tokens(raw_content)
                user_logger.debug(f"input_tokens tiktoken(cl100k_base): {input_tokens}")  
                user_logger.debug(f"output_tokens tiktoken(cl100k_base): {output_tokens}")  
            

            last_input_tokens = input_tokens

            
            # === Валидация ответа ===
            is_valid = False
            final_content = raw_content  # по умолчанию — как есть

            if expected_format == "any":
                is_valid = len(raw_content) > 0

            elif expected_format == "russian":
                is_valid = is_russian_text(raw_content)

            elif expected_format == "json_choice":
                try:
                    # Извлекаем JSON из markdown-блока (если есть)
                    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_content, re.DOTALL)
                    json_str = match.group(1).strip() if match else raw_content.strip()
                    data = json.loads(json_str)
                    choice_from_llm = str(data.get("choice", "")).strip()

                    if not choice_from_llm:
                        is_valid = False
                    else:
                        # Приводим к нижнему регистру для сравнения
                        choice_lower = choice_from_llm.lower()
                        if choice_lower in choice_mapping:
                            # Возвращаем вариант В ТОМ ВИДЕ, как он задан в valid_choices!
                            final_content = choice_mapping[choice_lower]
                            is_valid = True
                        else:
                            is_valid = False

                except (json.JSONDecodeError, AttributeError, TypeError, KeyError, ValueError):
                    is_valid = False

            # === Если ответ подходит — возвращаем ===
            if is_valid:
                return final_content, input_tokens, output_tokens, elapsed

            # === Логирование невалидного ответа ===
            user_logger.warning(f"Попытка {attempt + 1}/{max_retries + 1}: ответ не соответствует формату '{expected_format}'") 
            user_logger.debug(f"   Получено: {repr(raw_content[:120])}")  
            
            if attempt < max_retries:
                time.sleep(1.0 + attempt * 0.5)

        except Exception as e:
            elapsed = time.time() - start_time
            user_logger.error(f"❌ Ошибка LLM (попытка {attempt + 1}): {str(e)}")  
           
            if hasattr(e, 'response') and e.response is not None:
                err_text = getattr(e.response, 'text', '')
                user_logger.debug(f"   Ответ API: {err_text[:300]}...")  
               
            if attempt < max_retries:
                time.sleep(1.5 + attempt * 1.0)

    # === Все попытки исчерпаны — возвращаем "error_LLM" с реальными токенами ===
    total_elapsed = time.time() - start_time_total

    if last_input_tokens == 0:
        input_text = " ".join([msg["content"] for msg in messages if msg.get("content")])
        last_input_tokens = _count_tokens(input_text)

    output_tokens = _count_tokens(last_raw_content)  # именно то, что модель вернула (даже если неверно)

    return "error_LLM", last_input_tokens, output_tokens, total_elapsed


def load_mbti_questions(ques_path):
    """
    Загружает вопросы для MBTI-теста из Excel-файла и группирует их по дихотомиям.
    Ожидаемая структура файла:
        Колонки: Dichotomy, Question, OptionA, KeyA, OptionB, KeyB
    Для каждого вопроса с вероятностью 50% меняет местами варианты A и B (вместе с ключами).
    Параметры:
        ques_path (str): путь к Excel-файлу с вопросами (.xlsx).
    Возвращает:
        dict или None: словарь вида {dichotomy: [вопросы]}, где каждый вопрос — словарь.
                       Возвращает None в случае ошибки.
    """
    required_cols = ["Dichotomy", "Question", "OptionA", "KeyA", "OptionB", "KeyB"]

    try:
        df = pd.read_excel(ques_path)

        # Проверка структуры
        if not all(col in df.columns for col in required_cols):
            missing = [col for col in required_cols if col not in df.columns]
            user_logger.error(f"❌ В файле {ques_path} отсутствуют обязательные колонки: {missing}")
            return None

        mbti_questions = df.to_dict(orient="records")
        user_logger.debug(f"Загружено {len(mbti_questions)} вопросов из файла.")       

        questions_by_axis = {}

        for q in mbti_questions:
            axis = q["Dichotomy"]
            if axis not in questions_by_axis:
                questions_by_axis[axis] = []

            # Случайно меняем местами OptionA/OptionB и KeyA/KeyB
            if random.random() > 0.5:
                q["OptionA"], q["OptionB"] = q["OptionB"], q["OptionA"]
                q["KeyA"], q["KeyB"] = q["KeyB"], q["KeyA"]

            questions_by_axis[axis].append(q)

        return questions_by_axis

    except Exception as e:
        user_logger.error(f"❌ Ошибка загрузки XLSX: {e}")     
        return None

def print_pretty(text: str, width: int = 80, indent: str = " ") -> None:
    """Красиво выводит текст в консоль, сохраняя абзацы (по \\n)."""
    paragraphs = text.split('\n')
    for paragraph in paragraphs:
        clean_paragraph = ' '.join(paragraph.split())
        if not clean_paragraph:
            print()  # просто пустая строка (без отступа)
            continue
        wrapped = textwrap.fill(
            clean_paragraph,
            width=width,
            initial_indent="",
            subsequent_indent=indent
        )
        print(wrapped)

async def report_and_print(
    message: str,
    report_file: Path,
    width: int = 120,
    indent: str = "  ",
    _print: bool = True,
    bot: Bot = None,
    chat_id: int = None
) -> None:
    """
    Выводит сообщение красиво в консоль, дописывает в файл отчета и при необходимости
    отправляет в Telegram.

    Параметры:
    - bot: экземпляр aiogram.Bot для отправки сообщений (если None, отправка в Telegram пропускается)
    - chat_id: id чата, куда отправлять сообщение (обязателен, если bot передан)
    """
    # Вывод в консоль
    if _print:
        # Отправка в Telegram (если переданы bot и chat_id)
        if bot and chat_id:
            # Отправляем сообщение, учитывая ограничение по длине (4096 символов)
            #message = message.replace("n", "\n")
            max_len = 4000  # с запасом
            parts = [message[i:i+max_len] for i in range(0, len(message), max_len)]
            for part in parts:
                #await bot.send_message(chat_id, part)#, parse_mode="Markdown")
                message_bot = await safe_send_message(
                    bot=bot,
                    chat_id=chat_id,
                    text=part
                )

        else: # если нет бота, просто печатаем в консоль
            print_pretty(message, width=width, indent=indent)

    # Запись в файл
    with open(report_file, "a", encoding="utf-8") as f:
        if not message.endswith('\n'):
            message += '\n'
        f.write(message)   
        entry_logger = get_user_logger(chat_id)
        entry_logger.info(message)


def setup_report_file(user: Dict, config: Dict, chat_id: int = None) -> Path:
    """
    Создаёт файл отчет и записывает в него заголовок с метаданными.
    Возвращает путь к файлу отчета.
    """
    user_logger = get_user_logger(chat_id)

    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.datetime.now(moscow_tz).strftime("%Y-%m-%d") #_%H-%M")
  
    # Определяем корень проекта (папка на уровень выше текущего файла)
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    # Создаем папку reports внутри корня проекта
    reports_path = Path(project_root) / 'reports'
    reports_path.mkdir(parents=True, exist_ok=True)

    # Формируем имя файла отчёта    
    user_name = user["name"]  # предполагается, что user — словарь с ключом "name"
    report_filename = f"{now}_{user_name}.txt"

    # Полный путь к файлу отчёта
    report_filename = reports_path / report_filename

    # Заголовок — сразу пишем в файл и выводим
    header_lines = [
        f"Файл отчета: {report_filename}",
        f"Дата: {now}",
    ]
    if config["test"]:
        header_lines.append(f"Режим: тест max {config['max_qty']} вопроса с вариантами ответов из файла {config['ques_xlsx_path']}")
    else:
        header_lines.append(f"Режим: беседа max {config['max_qty']} вопроса с LLM {config['model_Akma']} температура {config['temperature_Akma']}")
        header_lines.append(f"Анализ ответов: LLM {config['model_analis']} температура {config['temperature_analis']}")
    if config["demo"]:
        header_lines.append(f"Выбран demo режим с LLM {config['model_demo']} температура {config['temperature_demo']}")

    header_lines.extend([f"{key}: {user[key]}" for key in ["id", "name", "age", "gender", "post", "activity"]])

    if config["demo"]:
        header_lines.append(f"\nТип demo_MBTI: {user.get('demo_MBTI', 'N/A')}")

    header_text = " | ".join(header_lines)
    user_logger.info(f"📝 {header_text}")

    header_lines.append("\n--- НАЧАЛО ТЕСТА ---\n")

    header_text = "\n".join(header_lines)

    # Записываем и выводим заголовок
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(header_text + "\n")
    
    user_logger.debug(f"Файл отчета создан: {report_filename}") 

    return report_filename

# ==================== ОСНОВНЫЕ РЕЖИМЫ РАБОТЫ ====================

async def run_test_mode(client, config: Dict, user: Dict, report_file: Path, counters: Dict[str, int], percent_axes: List[str], bot=None, chat_id=None) -> Tuple[Dict[str, int], int, int]:
    """
    Режим "тест": задаёт фиксированные вопросы из Excel-файла.
    Возвращает:
        - counters: словарь с счетчиками по осям (EI: +2, SN: -1, ...)
        - total_input_tokens, total_output_tokens: для расчёта стоимости
    """
    user_logger = get_user_logger(chat_id)
    user_logger.info(f"✅ Запуск режима Тест для {user['name']} (user_id: {user['id']})")
    
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    ques_path = os.path.join(project_root, config["ques_xlsx_path"])
    questions_by_axis = load_mbti_questions(ques_path)

    total_questions = sum(len(questions) for questions in questions_by_axis.values())   
    if not questions_by_axis or total_questions < config["max_qty"]:
        await report_and_print(f"❌ Извините, тест не возможен.\nОбратитесь к администратору.", report_file, bot=bot, chat_id=chat_id)
        user_logger.info(f"❌ Не удалось загрузить {config['max_qty']} вопросов из {config['ques_xlsx_path']} \n Тестирование невозможно. Проверьте файл и его путь.")
        return counters, 0, 0, 'q'  
      
    user_logger.info(f"Загружено {total_questions} вопросов из файла.")
    
    # Инициализация счётчиков и указателей
    #counters = {"EI": 0, "SN": 0, "TF": 0, "JP": 0}
    axis_pointers = {axis: 0 for axis in ["EI", "SN", "TF", "JP"]}    
    demo_input_tokens, demo_output_tokens = 0.0, 0.0
    axis_index = 0
    AXES = ["EI", "SN", "TF", "JP"]
    _print=False  
    if user["id"] == 0:  #вывод технич инфо на экран только для id=0 те админ тестировщика
        _print=True   

    while config["num"] <= config["max_qty"]:
        axis_index = (config["num"] - 1) % len(AXES) 
        axis = AXES[axis_index]        

        # Пропускаем ось, если по ней уже достаточно данных (но только если она не в percent_axes)
        if axis not in percent_axes:
            threshold = config["max_qty"] / 4 / 2
            if abs(counters[axis]) > threshold:               
                await report_and_print(f"\n***{config["num"]}/{config["max_qty"]}. Вопрос({axis}): Пропускаем т.к. {axis} не надо считать в %, а счетчик {axis} = {counters[axis]}, что < -{threshold} или > {threshold}", report_file, _print=_print, bot=bot, chat_id=chat_id)
                config["num"] += 1                
                continue

        # Берём следующий вопрос по текущей оси
        await report_and_print(f"\n*** {config["num"]}/{config["max_qty"]}. Вопрос по оси ({axis})", report_file, _print=_print, bot=bot, chat_id=chat_id)         
        axis_qs = questions_by_axis.get(axis, [])
        if axis_pointers[axis] >= len(axis_qs):
            break  # вопросы по этой оси закончились
        q = axis_qs[axis_pointers[axis]]
        axis_pointers[axis] += 1

        q_text = q["Question"]
        opt_a, opt_b = q["OptionA"], q["OptionB"]
        key_a, key_b = q["KeyA"], q["KeyB"]  # какая буква соответствует каждому варианту
        user_choice = None  # значение по умолчанию

        # Выводим вопрос        
        question_display = f"{q_text}\na. {opt_a}\nb. {opt_b}"
        
        if config["voice"]:
            await speak_text(question_display, bot=bot, chat_id=chat_id)         

        next_question = f"{config['num']}/{config['max_qty']}. Вопрос: {question_display}"       
        user_choice = None

        # Ответ 
        if config["demo"]:
            await report_and_print(next_question, report_file, bot=bot, chat_id=chat_id)
            # В демо-режиме LLM сам выбирает ответ, соответствующий demo_MBTI
            expected_letter = user["demo_MBTI"][AXES.index(axis)]
            prompt_demo_t = get_demo_t_prompt(q_text, opt_a, opt_b, expected_letter, axis)
            valid_choices = ("a", "b") #верный json_choice            

            user_choice, demo_in_t, demo_out_t, _ = send_to_llm_with_validation(
            client=client,
            messages=prompt_demo_t,
            model=config["model_demo"],
            temperature=0.0,  # для детерминированности
            max_tokens=32,
            expected_format="json_choice",
            valid_choices=valid_choices,
            chat_id=chat_id
            )
            demo_input_tokens += demo_in_t
            demo_output_tokens += demo_out_t

            result_letter = "x"
            if user_choice in valid_choices:
                if user_choice == "a":
                    result_letter = key_a
                elif user_choice == "b":
                    result_letter = key_b
                await report_and_print(f"{user['name']}({expected_letter}): вариант {user_choice}({result_letter})", report_file, bot=bot, chat_id=chat_id)            

            else:
                await report_and_print(f"***❌ Извините, LLM_demo не доступна, demo режим продолжить не возможна.\nПродолжите тест в ручном режиме.", report_file, bot=bot, chat_id=chat_id)
                config["demo"] = False            

        if not config["demo"]: # Реальный пользователь вводит a/b            
            if bot and chat_id:
                # Если есть bot и chat_id — спрашиваем выбор через кнопки Telegram
                await report_and_print(next_question, report_file, _print=False, bot=bot, chat_id=chat_id)
                       
                user_choice = await ask_user_choice_tg(                    
                    bot=bot, 
                    chat_id=chat_id,
                    question=next_question,                    
                    default="Timeout"
                ) 
            else:
                # Консольный ввод не поддерживается
                await report_and_print("Ошибка: консольный режим не поддерживается. Используйте Telegram.", report_file, bot=bot, chat_id=chat_id)
                return counters, demo_input_tokens, demo_output_tokens, 'q'

            if user_choice == "a":
                result_letter = key_a
            elif user_choice == "b":
                result_letter = key_b
            else:        
                await report_and_print(f"{user['name']}: {user_choice}", report_file, bot=bot, chat_id=chat_id)
                await report_and_print(f"Тест прерван на вопросе {config["num"]}.", report_file, bot=bot, chat_id=chat_id)                    
                return counters, demo_input_tokens, demo_output_tokens, 'q'
                       
            await report_and_print(f"{user['name']}: вариант {user_choice}", report_file, bot=bot, chat_id=chat_id)           

        # Обновляем счётчик по оси
        if result_letter == axis[0]:
            counters[axis] += 1
        elif result_letter == axis[1]:
            counters[axis] -= 1

        await report_and_print(f"*** вариант {user_choice} --> {result_letter} --> счетчик {axis} = {counters[axis]}", report_file, _print=_print, bot=bot, chat_id=chat_id)        
        config["num"] += 1
        config["actual_questions"]  += 1

    goodbye = f"Спасибо за прохождение теста! До свидания {user['name']}!"
    
    if config["voice"]:
        await speak_text(goodbye, bot=bot, chat_id=chat_id)
    await report_and_print(f"\n{goodbye} 👋😊", report_file, bot=bot, chat_id=chat_id)

    return counters, demo_input_tokens, demo_output_tokens, 'ok'

async def run_conversation_mode(client, config: Dict, user: Dict, report_file: Path, percent_axes: List[str], bot=None, chat_id=None) -> Tuple[Dict[str, int], int, int]:
    """
    Режим "беседа": LLM (Akma) генерирует вопросы, основываясь на предыдущих ответах.
    Анализ ответов тоже делает LLM.
    """
    user_logger = get_user_logger(chat_id)
    user_logger.info(f"✅ Запуск режима Беседа для {user['name']} (user_id: {user['id']})")

    total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens = 0,0,0,0
    counters = {"EI": 0, "SN": 0, "TF": 0, "JP": 0}  

    _print=False  
    if user["id"] == 0:  #вывод технич инфо на экран только для id=0 те админ тестировщика
        _print=True        
  
    # История диалога с Akma (нейро-психологом)
    zero_question, akma_system_prompt = get_akma_system_prompt(
        size_query=config["size_query"],
        name=user["name"], age=user["age"], gender=user["gender"],
        post=user["post"], activity=user["activity"])
    hist_Akma = [{"role": "system", "content": akma_system_prompt}]

    # История для генерации демо-ответов (если в демо-режиме)
    hist_demo = []
    if config["demo"]:
        hist_demo = [{"role": "system", "content": get_demo_system_prompt(
            size_answer=config["size_answer"],
            name=user["name"], age=user["age"], gender=user["gender"],
            post=user["post"], activity=user["activity"]
        )}]
    # Нулевой вводный вопрос   
    if config["voice"]:
        await speak_text(zero_question, bot=bot, chat_id=chat_id)   

    next_question = f"Акма: {zero_question}"
    user_resp = ""

    if config["demo"]:
        await report_and_print(next_question, report_file, bot=bot, chat_id=chat_id)
        # Генерируем нулевой демо-ответ
        hist_demo.append({"role": "user", "content": zero_question})    

        user_resp, demo_in_t, demo_out_t, _  = send_to_llm_with_validation(
        client=client,
        messages=hist_demo,
        model=config["model_demo"],
        temperature=config["temperature_demo"],
        max_tokens=int(config["size_answer"]*3),
        expected_format="russian",
        chat_id=chat_id
        )
        demo_input_tokens += demo_in_t
        demo_output_tokens += demo_out_t

        if not is_russian_text(user_resp): # не релевантный ответ LLM_demo           
            await report_and_print(f"***❌ Извините, LLM_demo не доступна, demo режим продолжить не возможна.\nПродолжите беседу с LLM в ручном режиме.", report_file, bot=bot, chat_id=chat_id)
            config["demo"] = False
            #return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'error_LLM_demo'
        else:            
            if config["voice"]:
                await speak_text(user_resp, bot=bot, chat_id=chat_id, voice="ru-RU-DmitryNeural", rate="+5%", volume=0.8)
            await report_and_print(f"{user['name']}({user['demo_MBTI']}): {user_resp}", report_file, bot=bot, chat_id=chat_id)
            hist_demo.append({"role": "assistant", "content": user_resp})

    # Ввод ответа пользователем     
    if not config["demo"]:        
        if bot and chat_id:                     
            # Если есть bot и chat_id — спрашиваем выбор через кнопки Telegram     
            await report_and_print(next_question, report_file, _print=False, bot=bot, chat_id=chat_id)       
            user_resp = await ask_user_text_tg(                
                bot=bot, 
                chat_id=chat_id,
                question=next_question,
                max_length=200,           # Максимальная длина
                min_russian_chars=3,      # Минимум русских букв              
                default="Timeout"
                 )  
        else:
            # Консольный ввод не поддерживается
            await report_and_print("Ошибка: консольный режим не поддерживается. Используйте Telegram.", report_file, bot=bot, chat_id=chat_id)
            return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'q'
        
        if user_resp.lower() == 'q' or user_resp == "Timeout": #
            await report_and_print(f"{user['name']}: {user_resp}", report_file, _print=False, bot=bot, chat_id=chat_id)
            await report_and_print(f"Беседа с ИИ прервана на вопросе {config["num"]}.\nПройдите тест.", report_file, bot=bot, chat_id=chat_id)               
            return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'q' 

        await report_and_print(f"{user['name']}: {user_resp}", report_file, _print=False, bot=bot, chat_id=chat_id)          

    # Обновляем историю Akma
    hist_Akma.extend([
        {"role": "assistant", "content": zero_question},
        {"role": "user", "content": f"Ответ Пользователь: {user_resp}"}
    ])

    # Основной цикл: задаём до max_qty вопросов    
    axis_index = 0
    AXES = ["EI", "SN", "TF", "JP"]

    err_result_letter = 0 # не релевантные ответы пользователя в одном вопросе
    max_err_result_letter = 5 # max не релевантные ответы и завершение

    while config["num"] <= config["max_qty"]:
        axis_index = (config["num"] - 1) % len(AXES) 
        axis = AXES[axis_index]  
        akma_question = None  # Инициализируем здесь      

        # Пропускаем ось, если данных достаточно (но не для percent_axes)
        if axis not in percent_axes:
            threshold = config["max_qty"] / 4 / 2
            if abs(counters[axis]) > threshold:                
                await report_and_print(f"\n*** {config["num"]}/{config["max_qty"]}. Вопрос по оси ({axis})\n***Пропускаем т.к. {axis} не надо считать в %, а счетчик  счетчик {axis} = {counters[axis]}, что < -{threshold} или > {threshold}", report_file, _print=_print, bot=bot, chat_id=chat_id)
                #axis_index = (axis_index + 1) % len(AXES)                
                config["num"] += 1               
                continue

        # Akma генерирует новый вопрос, связанный с предыдущими ответами
        await report_and_print(f"\n*** {config["num"]}/{config["max_qty"]}. Вопрос по оси ({axis})", report_file, _print=_print, bot=bot, chat_id=chat_id)         
        akma_local_prompt = get_akma_local_prompt(axis, err_result_letter, max_err_result_letter)        
        hist_local_akma = hist_Akma + [{"role": "user", "content": akma_local_prompt}]
        
        if bot and chat_id:
            #thinking_message = await bot.send_message(chat_id, "🤔 Акма думает...")
            thinking_message = await safe_send_message(
                    bot=bot,
                    chat_id=chat_id,
                    text="🤔 Акма думает..."
                )

        akma_question, in_t, out_t, _  = send_to_llm_with_validation(
        client=client,
        messages=hist_local_akma,
        model=config["model_Akma"],
        temperature=config["temperature_Akma"],
        max_tokens=int(config["size_query"]*3),
        expected_format="russian",
        )

        if bot and chat_id:
            await bot.delete_message(chat_id, thinking_message.message_id)
    
        total_input_tokens += in_t
        total_output_tokens += out_t

        if akma_question is None or not is_russian_text(akma_question):
            await report_and_print(f"***❌ Ошибка на вопросе {config["num"]}. Вопрос LLM_Akma: {user_resp}.", report_file, _print=_print, bot=bot, chat_id=chat_id)
            await report_and_print(f"❌ Извините, LLM не доступна, беседу продолжить не возможна.\nПройдите тест.", report_file, bot=bot, chat_id=chat_id)
            return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'error_LLM_Akma'

        if config["voice"]:
            await speak_text(akma_question, bot=bot, chat_id=chat_id)

        # Получаем ответ
        next_question = f"{config["num"]}/{config["max_qty"]}. Акма: {akma_question}"        
        user_resp = ""
        if config["demo"]: # Генерируем демо-ответ в беседе с LLM
            await report_and_print(next_question, report_file, bot=bot, chat_id=chat_id)
            expected_letter = user["demo_MBTI"][AXES.index(axis)]
            demo_user_prompt = get_demo_user_prompt(akma_question, expected_letter, axis)
            hist_demo.append({"role": "user", "content": demo_user_prompt})            

            user_resp, demo_in_t, demo_out_t, _  = send_to_llm_with_validation(
            client=client,
            messages=hist_demo,
            model=config["model_demo"],
            temperature=config["temperature_demo"],
            max_tokens=int(config["size_answer"]*3),
            expected_format="russian",
            chat_id=chat_id
            )
            demo_input_tokens += demo_in_t
            demo_output_tokens += demo_out_t

            if not is_russian_text(user_resp): # не релевантный ответ LLM_demo                
                await report_and_print(f"***❌Извините, LLM_demo не доступна, demo режим продолжить не возможно.\nПродолжите беседу с LLM в ручном режиме.", report_file, bot=bot, chat_id=chat_id)
                config["demo"] = False
                #return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'error_LLM_demo'
            else:                
                if config["voice"]:
                    await speak_text(user_resp, bot=bot, chat_id=chat_id, voice="ru-RU-DmitryNeural", rate="+5%", volume=0.8)
                await report_and_print(f"{user['name']}({expected_letter}): {user_resp}", report_file, bot=bot, chat_id=chat_id)            
                hist_demo.append({"role": "assistant", "content": user_resp})

        if not config["demo"]: # Ответ пользователя в беседе с LLM                   
            if bot and chat_id:                     
                # Если есть bot и chat_id — спрашиваем выбор через кнопки Telegram     
                await report_and_print(next_question, report_file, _print=False, bot=bot, chat_id=chat_id)       
                user_resp = await ask_user_text_tg(                
                    bot=bot, 
                    chat_id=chat_id,
                    question=next_question,
                    max_length=200,           # Максимальная длина
                    min_russian_chars=3,      # Минимум русских букв              
                    default="Timeout"
                    )  
            else:
                # Консольный ввод не поддерживается
                await report_and_print("Ошибка: консольный режим не поддерживается. Используйте Telegram.", report_file, bot=bot, chat_id=chat_id)
                return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'q'

            if user_resp.lower() == 'q' or user_resp == "Timeout": #
                await report_and_print(f"{user['name']}: {user_resp}", report_file, _print=False, bot=bot, chat_id=chat_id)
                await report_and_print(f"Беседа с ИИ прервана на вопросе {config["num"]}.\nПройдите тест.", report_file, bot=bot, chat_id=chat_id)               
                return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'q' 

            await report_and_print(f"{user['name']}: {user_resp}", report_file, _print=False, bot=bot, chat_id=chat_id)          

        # Обновляем историю Akma 
        hist_Akma.extend([
            {"role": "assistant", "content": akma_question},
            {"role": "user", "content": f"Ответ Пользователь: {user_resp}"}
        ])
        
        # Анализируем ответ через LLM-аналитика
        analis_prompt = get_analis_prompt(akma_question, user_resp, axis)
        valid_choices = (axis[0], axis[1], "x") #верный json_choice
        result_letter = None

        result_letter, in_t, out_t, _ = send_to_llm_with_validation(
        client=client,
        messages=analis_prompt,
        model=config["model_analis"],
        temperature=0.0,  # для детерминированности
        max_tokens=32,
        expected_format="json_choice",
        valid_choices=valid_choices,
        chat_id=chat_id
        )
        total_input_tokens += in_t
        total_output_tokens += out_t

        #print(hist_Akma)
        if result_letter not in valid_choices:            
            await report_and_print(f"***❌ Ошибка LLM_angalisis на вопросе {config["num"]} ответ LLM_angalisis: {result_letter}.", report_file, _print=_print, bot=bot, chat_id=chat_id)  
            await report_and_print(f"❌ Извините, LLM не доступна, беседу продолжить не возможна.\nПройдите тест.", report_file, bot=bot, chat_id=chat_id)       
            return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'error_LLM_angalisis'

        # Обновляем счётчики
        if result_letter == axis[0]:
            counters[axis] += 1
        elif result_letter == axis[1]:
            counters[axis] -= 1
        else:            
            # Если анализ не удался — переформулируем вопрос по той же оси (можно улучшить: повторить)
            await report_and_print(f"***❌ Не получилось классифицировать ответ.\nПереформулируем вопрос по той же оси.", report_file, _print=_print, bot=bot, chat_id=chat_id)             
            err_result_letter +=1

            if err_result_letter >= max_err_result_letter:
                await report_and_print(f"❌ Беседа прервана на {config["num"]} вопросе. Получено {err_result_letter} подряд неточных ответа.\nПройдите тест", report_file, bot=bot, chat_id=chat_id)                    
                return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'incorrect'

            continue

        await report_and_print(f"*** Результат анализа: {result_letter} --> счетчик {axis} = {counters[axis]}", report_file, _print=_print, bot=bot, chat_id=chat_id)
        err_result_letter = 0             
        config["num"] += 1        
        config["actual_questions"]  += 1

    goodbye = f"Спасибо за приятную беседу! До свидания, {user['name']}!"
    
    if config["voice"]:
        await speak_text(goodbye, bot=bot, chat_id=chat_id)
    await report_and_print(f"\nАкма: {goodbye} 👋😊", report_file, bot=bot, chat_id=chat_id)

    #print(hist_Akma)
    return counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, 'ok'

# ==================== ГЕНЕРАЦИЯ ИТОГОВОГО ОТЧЁТА ====================

def build_final_type(counters: Dict[str, int], config: Dict, percent_axes: List[str]) -> Tuple[str, str]:
    """
    Формирует два варианта итоговой строки типа MBTI.
    Возвращает кортеж из двух строк:
    1. С процентами для осей из percent_axes
    2. Только доминирующие буквы без процентов
    """
    final_parts_with_percent = []
    final_parts_letters_only = []
    max_per_axis = max(1, config["max_qty"] / 4)  # максимум вопросов по одной оси но мин 1   

    for axis in ["EI", "SN", "TF", "JP"]:
        score = counters[axis]
        
        if axis in percent_axes:
            # Вычисляем процент доминирования
            percent = int((score + max_per_axis) / (2 * max_per_axis) * 100)
            dominant = axis[0] if percent >= 50 else axis[1]
            p = percent if percent >= 50 else 100 - percent
            final_parts_with_percent.append(f"{dominant}({p}%)")
            final_parts_letters_only.append(dominant)
        else:
            # Просто определяем доминирующую букву
            dominant = axis[0] if score >= 0 else axis[1]
            final_parts_with_percent.append(dominant)
            final_parts_letters_only.append(dominant)
            
    return " ".join(final_parts_with_percent), "".join(final_parts_letters_only)

async def final_report(
    client,
    config: Dict,
    counters: Dict[str, int],
    total_input_tokens: int,
    total_output_tokens: int,
    demo_input_tokens: int,
    demo_output_tokens: int,    
    start_time: float,
    report_file: Path,  
    percent_axes: List[str] = None,
    bot=None, 
    chat_id=None
):
    """
    Выводит на экран и сохраняет в файл подробный отчёт по результатам диагностики.
    """    
    final_type_perc, final_type = "", ""
    if config.get('num', 0) > config.get('max_qty', 0): # Ответили на все вопросы
        
        final_type_perc, final_type = build_final_type(counters, config, percent_axes)
        await report_and_print("\n======    ✅Тестирование завершено!    ======", report_file, _print = False, bot=bot, chat_id=chat_id)
        await report_and_print(f"\nОпределен тип личности по MBTI: \n{final_type_perc}", report_file, _print = False, bot=bot, chat_id=chat_id)
        
    else:
        await report_and_print("\n======  ❌❌❌Тестирование не пройдено!!!   ======", report_file, bot=bot, chat_id=chat_id)
        await report_and_print(f"\n❌Тестирование прервано на {config['num']}/{config['max_qty']} вопросе.\n", report_file, _print=False, bot=bot, chat_id=chat_id)

    elapsed_time = time.time() - start_time
    cost_in = total_input_tokens / 1_000_000 * config['price_query']
    cost_out = total_output_tokens / 1_000_000 * config['price_answer']
    report_lines = [
        "\n=========   Технический отчет   =========",
        f"Модель Akma: {config['model_Akma']} с температурой {config['temperature_Akma']}",
        f"Модель Analis: {config['model_analis']} с температурой 0.0",
        f"Время прохождения: {elapsed_time:.1f} сек",
        f"Задано вопросов: {config["actual_questions"]} из max: {config['max_qty']}",
        f"Отправлено токенов: {total_input_tokens} → стоимость: {cost_in:.5f} $",
        f"Получено токенов: {total_output_tokens} → стоимость: {cost_out:.5f} $"
    ]

    if config.get("demo"):
        demo_cost_in = demo_input_tokens / 1_000_000 * config['price_query']
        demo_cost_out = demo_output_tokens / 1_000_000 * config['price_answer']
        report_lines.append(f"demo - Отправлено токенов: {demo_input_tokens} → стоимость: {demo_cost_in:.5f} $")
        report_lines.append(f"demo - Получено токенов: {demo_output_tokens} → стоимость: {demo_cost_out:.5f} $")

    report_lines.append(f"\nПолный отчет сохранён: {report_file}")
    report_lines.append("✅ Система MBTI-диагностики завершила работу.")

    full_report = "\n".join(report_lines)

    # Отправляем одним сообщением   
    await report_and_print(full_report, report_file, _print = False, bot=bot, chat_id=chat_id)

    return final_type_perc, final_type


"""
async def wait_resp(
    bot,
    chat_id,
    message,
    question: str,
    keyboard,    
    timeout_: int,       # таймаут в секундах
    future: asyncio.Future,
):
    '''
    Обновляет сообщение, показывая, сколько минут осталось до окончания таймера.    
    ''' 
    remaining_minutes = timeout_ // 60

    while remaining_minutes > 0 and not future.done():       
        # Обновляем сообщение с текущим временем
        try:
            text = f"{question}\n\n⏳ На ответ осталось {remaining_minutes} мин"
            if remaining_minutes == 1:
                text = f"{question}\n\n⏳ <b>Внимание!!! Осталась 1 мин</b> 🔔"          

            result = await safe_edit_message(
                bot = bot,
                chat_id = chat_id,
                message_id = message.message_id,
                new_text = text,
                reply_markup = keyboard,
                parse_mode = "HTML"
            )  

            if not result: # Не получилось изменить сообщение--> удалим и повторим сообщение
                result = await safe_delete_message(
                    bot = bot,
                    chat_id = chat_id,
                    message_id = message.message_id
                )  
                message = await safe_send_message(
                    bot = bot,
                    chat_id = chat_id,
                    text = text,
                    reply_markup=keyboard,
                    parse_mode = "HTML"
                ) 

        except Exception:
            pass
        
        # Ждем 1 секунду и проверяем future
        for _ in range(60):
            if future.done():
                if message.reply_markup is not None:
                    # ПОЛЬЗОВАТЕЛЬ НАЖАЛ КНОПКУ - УБИРАЕМ КНОПКИ И ВЫХОДИМ
                    try:
                        await message.edit_reply_markup(reply_markup=None)
                    except:
                        pass
                    await asyncio.sleep(1) 
                
                return  #  ВЫХОД ИЗ ФУНКЦИИ
            await asyncio.sleep(1)
        '''
        try:
            await asyncio.wait_for(future, timeout=60)
            # Пользователь нажал кнопку — убираем кнопки
            try:
                await message.edit_reply_markup(reply_markup=None)
            except Exception as e:
                print(f"Ошибка при удалении кнопок: {e}")
        except asyncio.TimeoutError:
            # Таймаут — пользователь не нажал кнопку
            pass
        '''
        remaining_minutes -= 1
    
    # Время вышло а кнопу не нажали
    if not future.done():           
        # Обновляем сообщение и тайаут
        try:
            text = f"{question}"

            result = await safe_edit_message(
                bot = bot,
                chat_id = chat_id,
                message_id = message.message_id,
                new_text = text,
                reply_markup = None,
                parse_mode = "HTML"
            )  

            if not result: # Не получилось изменить сообщение--> удалим и повторим сообщение
                result = await safe_delete_message(
                    bot = bot,
                    chat_id = chat_id,
                    message_id = message.message_id
                )  
                message = await safe_send_message(
                    bot = bot,
                    chat_id = chat_id,
                    text = text,
                    reply_markup=None,
                    parse_mode = "HTML"
                )

        except Exception as e:
            print(f"Ошибка в wait_resp: {e}")
"""

class WaitResponseTimeoutError(Exception):
    pass

async def wait_resp(bot, chat_id, message, question, keyboard, timeout_: int, future: asyncio.Future) -> Any:
    """
    Ожидает ответ пользователя с обновлением таймера.
    
    Returns:
        Результат future при успехе
        
    Raises:
        WaitResponseTimeoutError: при таймауте
    """
    remaining_minutes = max(1, timeout_ // 60)
    original_text = message.text if hasattr(message, 'text') else question
    
    try:
        while remaining_minutes > 0:
            # Формируем текст с таймером
            text = f"{question}\n\n⏳ На ответ осталось {remaining_minutes} мин"
            if remaining_minutes == 1:
                text = f"{question}\n\n⏳ <b>Внимание!!! Осталась 1 мин</b> 🔔"
            
            # Обновляем сообщение
            res = await safe_edit_message(
                bot = bot,
                chat_id = chat_id,
                message_id = message.message_id,
                new_text = text,
                reply_markup = keyboard,
                parse_mode = "HTML"
            )  

            if not res: # Не получилось изменить сообщение--> удалим и повторим сообщение
                res = await safe_delete_message(
                    bot = bot,
                    chat_id = chat_id,
                    message_id = message.message_id
                )  
                message = await safe_send_message(
                    bot = bot,
                    chat_id = chat_id,
                    text = text,
                    reply_markup=keyboard,
                    parse_mode = "HTML"
                ) 
            
            # Ждем ответ с таймаутом 60 секунд
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=60)
                
                # Убираем клавиатуру при успехе
                try:
                    await safe_edit_message(bot, chat_id, message.message_id, text, None, "HTML")
                except:
                    pass
                return future.result()
                
            except asyncio.TimeoutError:
                remaining_minutes -= 1
                continue
                
    except Exception as e:
        # Очистка при ошибке
        try:
            await safe_edit_message(bot, chat_id, message.message_id, original_text, None, "HTML")
        except:
            pass
        if not isinstance(e, WaitResponseTimeoutError):
            raise e
    
    # Таймаут
    try:
        await safe_edit_message(bot, chat_id, message.message_id, original_text, None, "HTML")
    except:
        pass
    raise WaitResponseTimeoutError("Время ожидания истекло")


async def ask_user_choice_tg(
    bot,
    chat_id,
    question=None,    
    options=["a", "b", "Выход"],
    timeout_=600,
    default="q"
):
    """Отправляет сообщение с кнопками и ждёт ответа пользователя."""
    
    user_logger = get_user_logger(chat_id) 

    # Создаем клавиатуру
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[]])
    keyboard.inline_keyboard[0] = [
        InlineKeyboardButton(text=opt, callback_data=f"choice_{opt}")
        for opt in options
    ]

    # Создаем future
    future = asyncio.get_event_loop().create_future()
    pending_answers[chat_id] = future
    
    message = None
    
    try:
        # Отправляем сообщение
        message = await safe_send_message(
            bot=bot,
            chat_id=chat_id,
            text=question,
            reply_markup=keyboard
        )

        if timeout_ is not None and timeout_ > 0:
            # Ждем результат от wait_resp (она сама все контролирует)
            try:
                user_choice = await wait_resp(
                    bot, chat_id, message, question, keyboard, timeout_, future
                )
                # wait_resp уже удалила кнопки
                await _safe_remove_keyboard(message, user_logger)
                user_logger.debug(f"✅ Выбор: '{user_choice}'")
                return user_choice
            except WaitResponseTimeoutError:
                user_logger.debug("⏰ Таймаут")
                # wait_resp уже удалила кнопки
                await _safe_remove_keyboard(message, user_logger)
                await safe_send_message(
                    bot, chat_id,
                    "⏰ Время на выбор ответа истекло."
                )
                return default
        else:
            # Без таймаута
            user_choice = await future
            await _safe_remove_keyboard(message, user_logger)
            user_logger.debug(f"✅ Выбор: '{user_choice}'")
            return user_choice

    finally:
        pending_answers.pop(chat_id, None)


async def ask_user_text_tg(    
    bot,
    chat_id,
    question="Ваш ответ:", 
    max_length=200,
    min_russian_chars=3,
    timeout_=600,
    default="q"
):
    """Запрашивает у пользователя текстовый ответ."""
    
    user_logger = get_user_logger(chat_id)

    # Создаем клавиатуру с кнопкой выхода
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выход", callback_data="text_choice_q")]
    ])

    # Отправляем сообщение с клавиатурой
    message = await safe_send_message(
        bot=bot,
        chat_id=chat_id,
        text=question,
        reply_markup=keyboard
    )

    # Создаем Future для ожидания ответа
    future = asyncio.Future()

    # Сохраняем состояние ожидания ответа
    if not hasattr(bot, '_waiting_text_responses'):
        bot._waiting_text_responses = {}

    bot._waiting_text_responses[chat_id] = {
        'future': future,
        'min_russian_chars': min_russian_chars,
        'max_length': max_length,
        'question_message_id': message.message_id
    }
    
    try:
        # Ждем ответ
        if timeout_ is not None and timeout_ > 0:
            try:
                # Ждем результат от wait_resp (она сама все контролирует)
                user_response = await wait_resp(
                    bot=bot,
                    chat_id=chat_id,
                    message=message,
                    question=question,
                    keyboard=keyboard,
                    timeout_=timeout_,
                    future=future
                )
                
                # Если дошли сюда - wait_resp вернула результат (пользователь ответил)
                # wait_resp САМА удалила кнопки при успехе
                await _safe_remove_keyboard(message, user_logger)
                user_logger.debug(f"✅ Получен ответ: '{user_response}'")
                return user_response
                
            except WaitResponseTimeoutError:
                # wait_resp выбросила исключение - время истекло
                user_logger.debug(f"⏰ Время на ответ истекло.")
                
                # wait_resp САМА удалила кнопки при таймауте
                await _safe_remove_keyboard(message, user_logger)
                # Отправляем сообщение о таймауте
                await safe_send_message(
                    bot=bot,
                    chat_id=chat_id,
                    text=f"⏰ Время на ответ истекло.\nТестирование прервано по времени."
                )
                return default
        
        else:
            # Таймаут не задан - ждем бесконечно
            user_response = await future
            
            # Убираем кнопки после ответа (только для случая без таймаута)
            await _safe_remove_keyboard(message, user_logger)
            
            user_logger.debug(f"✅ Получен ответ: '{user_response}'")
            return user_response

    except asyncio.CancelledError:
        user_logger.debug("Задача ask_user_text_tg отменена")
        # При отмене пытаемся удалить кнопку (только если без таймаута)
        if timeout_ is None and message:
            await _safe_remove_keyboard(message, user_logger)
        raise
        
    except Exception as e:
        user_logger.error(f"❌ Ошибка в ask_user_text_tg: {e}")
        # При любой ошибке пытаемся удалить кнопку
        if message:
            await _safe_remove_keyboard(message, user_logger)
        raise
        
    finally:
        # Очистка
        if chat_id in bot._waiting_text_responses:
            del bot._waiting_text_responses[chat_id]


# Вспомогательная функция для надежного удаления кнопки
async def _safe_remove_keyboard(message, logger=None):
    """Надежно удаляет клавиатуру с сообщения, если она есть.
    
    Returns:
        bool: True если клавиатура удалена или её не было, False при ошибке
    """
    
    # Проверка наличия сообщения
    if not message:
        if logger:
            logger.debug("❌ Нет сообщения для удаления клавиатуры")
        return False
    
    # Проверка наличия клавиатуры
    if not message.reply_markup:
        if logger:
            logger.debug("ℹ️ У сообщения нет клавиатуры")
        return True  # Выходим, так как задачи нет
    
    # Если дошли сюда - у сообщения есть клавиатура, пробуем удалить
    try:
        # Пробуем через edit_reply_markup
        await message.edit_reply_markup(reply_markup=None)
        if logger:
            logger.debug("✅ Клавиатура удалена")
        return True
        
    except Exception as e:
        # Если первый способ не сработал, пробуем через edit_text
        try:
            await message.edit_text(
                text=message.text,
                reply_markup=None,
                parse_mode="HTML"
            )
            if logger:
                logger.debug("✅ Клавиатура удалена через edit_text")
            return True
            
        except Exception as e2:
            if logger:
                logger.debug(f"❌ Не удалось удалить клавиатуру: {e2}")
            return False
    
#=======================Основная функия тестирования================================
async def testing(config, user, bot=None, chat_id=None, logging=None):    
    
    client = None    
    counters = {"EI": 0, "SN": 0, "TF": 0, "JP": 0}
    total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, fial_input_tokens, fial_output_tokens  = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0    
    
    if user["id"] != 0:  # demo  доступно только для админ
        config["demo"] = False 

    # Настраиваем файл отчета
    report_file = setup_report_file(user, config, chat_id)

    # Определяем, по каким осям нужно показывать проценты
    AXES = ["EI", "SN", "TF", "JP"]
    percent_axes = [ax.strip() for ax in config["percent"].split(",") if ax.strip() in AXES] 

    '''
    # Приветствие
    if config["entry"]:
        intro = f"💬 Здравствуйте, {user['name']}! Пройдите, пожалуйста, небольшой тест."  
        await report_and_print(intro, report_file, bot=bot, chat_id=chat_id)        
        if config["voice"]:
            speak_text(intro)
    '''  

    LLM_ready = False      
    if not config["test"] or config["demo"]: # только для беседа c LLM иля для demo подключаем LLM
        # Загрузка переменных окружения
        load_dotenv()        
        if config.get("api_base") is None:             
            key ="OPENAI_API_KEY"   
        else:
            key ="OPENROUTER_API_KEY_W"           

        API_KEY = os.getenv(key)

        # Логируем с привязкой к chat_id, если он есть
        try:
            user_logger = get_user_logger(chat_id)
        except Exception:
            user_logger = logging.getLogger(__name__)

        if API_KEY:
            user_logger.debug(f"🔑 API-ключ получен: {key}")

            # Инициализируем клиент OpenAI для работы с OpenRouter            
            try:
                #client = OpenAI(base_url=config["api_base"], api_key=API_KEY)
                client = OpenAI(
                    base_url=config["api_base"],
                    api_key=API_KEY,
                    timeout=60.0,      # 60 секунд общий таймаут
                    max_retries=2      # 2 попытки по умолчанию
                )
                user_logger.debug("✅ Клиент успешно создан")

                try:
                    response = client.chat.completions.create(
                        model=config["model_Akma"],
                        messages=[{"role": "user", "content": "ok"}],
                        max_tokens=1
                    )
                    #print(response.choices[0].message.content)
                    user_logger.info(f"✅ LLL_Akma: {config["model_Akma"]} готова к работе!")
                    LLM_ready = True
                    '''
                    try:
                        response = client.chat.completions.create(
                            model=config["model_analis"],
                            messages=[{"role": "user", "content": "ok"}],
                            max_tokens=1
                        )
                        print(response)
                        user_logger.info(f"✅ LLL_analis: {config["model_analis"]} готова к работе!")
                        LLM_ready = True 
                        
                    except Exception as e:
                        user_logger.info(f"❌ LLL_analis: {config["model_analis"]} недоступна: {str(e)}")                    
                    '''
                except Exception as e:
                    user_logger.info(f"❌ LLL_analis: {config["model_Akma"]} недоступна: {str(e)}")


            except Exception as e:
                user_logger.error(f"❌ Ошибка при создании клиента: {str(e)}")               

        else:
            user_logger.error("❌ API-ключ не задан")
            
    
    question=" "
    if not config["test"]: # беседа c LLM 
        if LLM_ready:
            question = (
                f"Приветствуем вас, {user['name']}! 😊\n"
                f"🤖 Начните беседу с ИИ — всего {config['max_qty']} вопросов, чтобы определить ваш тип личности (MBTI). Здесь нет правильных и неправильных ответов! 🧩\n"
                "\n"
                "Советы для лучшего результата:\n"
                "✅ Отвечайте честно и точно (макс. 200 символов)\n"
                "⏳ У вас есть 10 минут на каждый ответ\n"
                "🔄 Если ИИ не сможет понять 5 ответов подряд или закончится время на ответ или нажмете «Выход» — тестирование продолжится классическим тестом.\n"                
                "\n"
                "Когда будете готовы, нажмите «Начать»! ▶️✨"
                )  
            '''
                "Хотите надиктовывать(не запись), а не печатать?\n"
                "📱 На телефоне — на клавиатуре(если настроено) нажмите 🎤 \n"
                "💻 На Windows — Win + H\n"
                "🍎 На Mac — включите 'Диктовку' (Fn Fn)\n"
                "\n"
            '''
                      
        else:
            await report_and_print(f"❌ Извините, LLM не доступна, беседа не возможна.\nПройдите тест.", report_file, bot=bot, chat_id=chat_id)       
            config["test"]  = True
            config["demo"] = False    
      
    config["num"] = 1
    config["actual_questions"] = 0
    start_time = time.time()
    err_mess = ''
    final_type_perc, final_type ="", ""
  
    if not config["test"]: # беседа c LLM

        if bot and chat_id: # в телеграмм                
            user_choice = await ask_user_choice_tg(                    
                bot=bot, 
                chat_id=chat_id,
                question=question,
                options = ["Начать", "Выход"],
                default="Timeout"
            ) 

        if user_choice != "Начать": # Прерываем тестирование  
            user_logger.info(f"✅ На приглашение начать беседу, получен ответ: {user_choice}")        
            return final_type_perc, final_type, report_file
        
        user_logger.debug("✅ Запуск режима беседы")  
        counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens, err_mess = await run_conversation_mode(
            client, config, user, report_file, percent_axes, bot, chat_id) 
        
    if err_mess == 'error_LLM_demo': # LLM_demo не отвечает в режиме demo
        return report_file 
    
    if config.get('test', False) or config.get('num', 0) <= config.get('max_qty', 0):
        #выбран тест или прервалась беседа и продолжаем тестом

        question = (
            f"Приветствуем вас, {user['name']}! 😊\n"
            f"📝 Пройдите тест — всего {config['max_qty']-config['num']+1} вопросов — чтобы определить ваш тип личности (MBTI). Здесь нет правильных и неправильных ответов! 🧩\n"
            "\n" 
            "Советы для лучшего результата:\n"
            "✅ Отвечайте честно, выбирайте a или b\n"
            "⏳ У вас есть 10 минут на каждый ответ\n"
            "🛑 Если закончится время на ответ или нажмете «Выход» — тестирование прервется.\n"
            "\n"                 
            "Когда будете готовы, нажмите «Начать»! ▶️✨"
        )

        if bot and chat_id: # в иелеграмм                
            user_choice = await ask_user_choice_tg(                    
                bot=bot, 
                chat_id=chat_id,
                question=question,
                options = ["Начать", "Выход"],
                default="Timeout"
            )  

        if user_choice != "Начать": # Прерываем тестирование  
            user_logger.info(f"✅ На приглашение начать тест, получен ответ: {user_choice}")        
            return final_type_perc, final_type, report_file
       
        user_logger.debug("✅ Запуск режима тестирования")  
        counters, demo_input_tokens, demo_output_tokens, err_mess = await run_test_mode(
            client, config, user, report_file, counters, percent_axes, bot, chat_id)         
    
    user_logger.info(f"✅ Получено ответов: {config.get('num', 0)-1}, из макс вопросов: {config.get('max_qty', 0)}")
    
    # Финальный отчёт
    final_type_perc, final_type = await final_report(client, config, counters, total_input_tokens, total_output_tokens, demo_input_tokens, demo_output_tokens,
                          start_time, report_file, percent_axes, bot, chat_id)

    return final_type_perc, final_type, report_file 


# ==================== ГЛАВНАЯ ТОЧКА ВХОДА ====================

async def main():
    """
    Основная функция программы.
    """
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/mbti_bot.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    # Загружаем конфигурацию из YAML-файла    
    with open("data/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    user_logger.info("✅ Конфигурация загружена из data/config.yaml")  # Добавлено/изменено для логирования    
    
    # Собираем данные пользователя
    user = collect_user_data(config)   
    
    # Запускаем основной цикл тестирования
    _,_,_ = await testing(config, user, logging=logging)

   
# Запуск программы
if __name__ == "__main__":
    asyncio.run(main())