#!/usr/bin/env python3
"""
Ozon Price Tracker Bot
Телеграм-бот для отслеживания цен на Ozon
"""

import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import run_bot

if __name__ == '__main__':
    print("=" * 50)
    print("  Ozon Price Tracker Bot")
    print("=" * 50)
    print()
    print("Запуск бота...")
    print("Для остановки нажмите Ctrl+C")
    print()
    
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n\nБот остановлен пользователем")
    except Exception as e:
        print(f"\n\nКритическая ошибка: {e}")
        sys.exit(1)
