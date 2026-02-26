"""
MBTI Diagnostic Telegram Bot - Main Entry Point

Использует конфигурацию из config.yaml и переменные окружения из .env
"""

import asyncio
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# Добавляем папку приложения в path
sys.path.insert(0, str(Path(__file__).parent))

# Загружаем переменные окружения
load_dotenv()

# Проверяем наличие необходимых переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения. Проверьте файл .env")

# Создаем папку для логов если её нет
Path('logs').mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/telegram_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


async def main():
    """
    Главная функция приложения.
    Импортирует и запускает telegram-бот.
    """
    logger.info("=" * 50)
    logger.info("🚀 Запуск MBTI Diagnostic Telegram Bot")
    logger.info("=" * 50)
    
    try:
        # Импортируем основное приложение бота
        from app.bot import main as bot_main
        
        # Запускаем бот
        await bot_main()
        
    except ImportError as e:
        logger.error(f"❌ Ошибка импорта: {e}")
        logger.error("Убедитесь, что файл находится в папке app/bot.py")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("⛔ Бот остановлен пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.debug("🛑 Бот полностью остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Приложение остановлено")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка приложения: {e}", exc_info=True)
        sys.exit(1)
