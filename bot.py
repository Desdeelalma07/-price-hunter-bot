import logging
import asyncio
import socket
from datetime import datetime, timezone, timedelta
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.txt')
TOKEN_PATH = os.path.join(BASE_DIR, 'token.txt')

# Proxy configuration (only for Telegram bot)
def _get_proxy_config():
    """Get proxy config for Telegram bot."""
    proxy_type = 'none'
    proxy_url = ''
    proxy_user = ''
    proxy_pass = ''

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('PROXY_TYPE='):
                    proxy_type = line.split('=', 1)[1].strip()
                elif line.startswith('PROXY_URL='):
                    proxy_url = line.split('=', 1)[1].strip()
                elif line.startswith('PROXY_USER='):
                    proxy_user = line.split('=', 1)[1].strip()
                elif line.startswith('PROXY_PASS='):
                    proxy_pass = line.split('=', 1)[1].strip()
    except FileNotFoundError:
        pass

    if proxy_type != 'none' and proxy_url:
        # If user is set, add auth to URL
        if proxy_user:
            # Parse URL: protocol://host:port
            if '://' in proxy_url:
                proto, rest = proxy_url.split('://', 1)
                auth = f"{proxy_user}:{proxy_pass}@" if proxy_pass else f"{proxy_user}@"
                proxy_url = f"{proto}://{auth}{rest}"
        return proxy_url
    return None


def _mask_proxy_url(proxy_url: str) -> str:
    """Mask proxy credentials before logging."""
    if not proxy_url or "://" not in proxy_url or "@" not in proxy_url:
        return proxy_url

    proto, rest = proxy_url.split("://", 1)
    if "@" not in rest:
        return proxy_url

    _, host_part = rest.rsplit("@", 1)
    return f"{proto}://***@{host_part}"

# Московское время (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))


def to_msk(dt_str: str) -> str:
    """Конвертировать ISO-строку в московское время"""
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        msk = dt.astimezone(MSK_TZ)
        return msk.strftime('%d.%m %H:%M')
    except Exception:
        return dt_str
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    JobQueue,
    CallbackQueryHandler,
)
from telegram.request import HTTPXRequest
from telegram.error import Forbidden, BadRequest
from httpx import AsyncHTTPTransport

import database as db
from ozon_client import get_ozon_price
from price_checker import extract_product_id, is_valid_sku, sku_to_url, resolve_short_url
from cart_parser import get_cart_products, extract_share_code
from list_handler import add_list_handlers, list_products_paginated, show_product_detail, STATE_THRESHOLD_ASK

# Настройка логирования
import logging.handlers
from price_chart import build_price_history_chart

# Создаём кастомный handler с UTF-8 кодировкой
file_handler = logging.FileHandler('bot.log', encoding='utf-8', mode='a')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        file_handler,
        logging.StreamHandler(),  # Консольный вывод
    ],
)
logger = logging.getLogger(__name__)

# Rate limit для /checkall (user_id -> время последнего вызова)
_checkall_cooldowns: dict[int, datetime] = {}
CHECKALL_COOLDOWN_SECONDS = 300  # 5 минут

# Константы состояний (FSM) — только строки, данные хранятся в БД
STATE_REMOVE_ASK_NUMBER = "remove_ask_number"
STATE_REMOVE_CONFIRM = "remove_confirm"
STATE_HISTORY_ASK_NUMBER = "history_ask_number"
STATE_CHECK_ASK_NUMBER = "check_ask_number"
STATE_THRESHOLD_ASK = "threshold_ask"

# Telegram max message length
MAX_MESSAGE_LENGTH = 4000

# Retry settings for free proxy
MAX_SEND_RETRIES = 3
RETRY_DELAY = 1  # seconds

# Bot start time for uptime calculation
BOT_START_TIME = datetime.now(timezone.utc)

# Monitoring statistics
monitoring_stats = {
    'total_checks': 0,
    'successful_checks': 0,
    'failed_checks': 0,
    'notifications_sent': 0,
    'last_check_time': None
}


def build_history_price_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 История цен", callback_data=f"detail_hist:{product_id}")]
    ])


async def notify_price_change_subscribers(
    bot,
    product: dict,
    product_name: Optional[str],
    old_price: Optional[float],
    new_regular: Optional[float],
    new_bank: Optional[float],
    *,
    skip_user_id: Optional[int] = None,
) -> int:
    """Отправить уведомления всем подписчикам товара с учётом их порогов."""
    new_price = new_bank or new_regular
    if not old_price or new_price is None:
        return 0

    diff = new_price - old_price
    if old_price == 0:
        return 0

    percent = (diff / old_price) * 100
    changed = abs(percent) >= config.price_change_threshold
    if not changed:
        return 0

    should_notify = False
    if config.notification_direction == 'both':
        should_notify = True
    elif config.notification_direction == 'down' and diff < 0:
        should_notify = True
    elif config.notification_direction == 'up' and diff > 0:
        should_notify = True

    if not should_notify:
        return 0

    arrow = "📉" if diff < 0 else "📈"
    name = product_name or product['product_name'] or f"Товар #{product['product_id']}"
    price_detail = f"{new_price:,.0f} ₽"
    if new_bank and new_regular and new_bank < new_regular:
        price_detail += f" (обычная: {new_regular:,.0f} ₽)"

    message = (
        f"{arrow} *Цена изменилась!*\n\n"
        f"*{name}*\n"
        f"Было: {old_price:,.0f} ₽\n"
        f"Стало: {price_detail}\n"
        f"Изменение: {'+' if diff > 0 else ''}{diff:,.0f} ₽ ({'+' if percent > 0 else ''}{percent:.1f}%)\n\n"
        f"🔗 [Открыть товар]({product['ozon_url']})"
    )
    reply_markup = build_history_price_keyboard(product['product_id'])

    notifications = 0
    user_entries = db.get_user_product_for_notification(product['product_id'])
    for entry in user_entries:
        if skip_user_id is not None and entry['user_id'] == skip_user_id:
            continue

        user_threshold = entry['price_threshold'] if 'price_threshold' in entry.keys() else None
        effective_threshold = user_threshold if user_threshold is not None else config.price_change_threshold
        if abs(percent) < effective_threshold:
            continue

        try:
            await send_message_with_retry(
                bot,
                chat_id=entry['user_id'],
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            notifications += 1
            logger.info(
                f"Уведомление отправлено пользователю {entry['user_id']} "
                f"о товаре #{product['product_id']}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")

    return notifications


async def send_message_with_retry(bot, chat_id: int, text: str, **kwargs) -> Optional[object]:
    """Send message with retry on failure (for free proxy reliability)"""
    for attempt in range(MAX_SEND_RETRIES):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except Forbidden as e:
            db.set_user_active(chat_id, False)
            logger.warning(f"Пользователь {chat_id} помечен неактивным: бот заблокирован или доступ запрещён ({e})")
            raise
        except BadRequest as e:
            error_text = str(e).lower()
            if "chat not found" in error_text or "user is deactivated" in error_text:
                db.set_user_active(chat_id, False)
                logger.warning(f"Пользователь {chat_id} помечен неактивным: чат недоступен ({e})")
                raise
            logger.warning(f"Send attempt {attempt + 1}/{MAX_SEND_RETRIES} failed: {e}")
            if attempt == MAX_SEND_RETRIES - 1:
                logger.error(f"Failed to send message after {MAX_SEND_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            logger.warning(f"Send attempt {attempt + 1}/{MAX_SEND_RETRIES} failed: {e}")
            if attempt == MAX_SEND_RETRIES - 1:
                logger.error(f"Failed to send message after {MAX_SEND_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def reply_text_with_retry(message, text: str, **kwargs) -> Optional[object]:
    """Reply to message with retry on failure"""
    for attempt in range(MAX_SEND_RETRIES):
        try:
            return await message.reply_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"Reply attempt {attempt + 1}/{MAX_SEND_RETRIES} failed: {e}")
            if attempt == MAX_SEND_RETRIES - 1:
                logger.error(f"Failed to reply after {MAX_SEND_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def reply_photo_with_retry(message, photo, **kwargs) -> Optional[object]:
    """Reply with photo and retry on failure"""
    for attempt in range(MAX_SEND_RETRIES):
        try:
            if hasattr(photo, "seek"):
                photo.seek(0)
            return await message.reply_photo(photo=photo, **kwargs)
        except Exception as e:
            logger.warning(f"Photo reply attempt {attempt + 1}/{MAX_SEND_RETRIES} failed: {e}")
            if attempt == MAX_SEND_RETRIES - 1:
                logger.error(f"Failed to reply with photo after {MAX_SEND_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def edit_message_with_retry(message_or_query, text: str, **kwargs) -> Optional[object]:
    """Edit message with retry on failure (works with both Message and CallbackQuery)"""
    for attempt in range(MAX_SEND_RETRIES):
        try:
            if hasattr(message_or_query, 'edit_message_text'):
                # CallbackQuery
                return await message_or_query.edit_message_text(text, **kwargs)
            else:
                # Message
                return await message_or_query.edit_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"Edit attempt {attempt + 1}/{MAX_SEND_RETRIES} failed: {e}")
            if attempt == MAX_SEND_RETRIES - 1:
                logger.error(f"Failed to edit message after {MAX_SEND_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def send_long_message(message: str, send_func, parse_mode='Markdown'):
    """
    Отправить сообщение, разбивая на части по товарам.
    send_func — функция отправки с retry
    """
    if not message:
        return

    if len(message) <= MAX_MESSAGE_LENGTH:
        await send_func(message, parse_mode=parse_mode)
        return

    # Разбиваем на части по товарам
    lines = message.split('\n\n')
    chunks = []
    current = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        test = (current + '\n\n' + line).strip() if current else line
        if len(test) > MAX_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
                current = line
            else:
                chunks.append(line)
                current = ""
        else:
            current = test

    if current:
        chunks.append(current)

    for chunk in chunks:
        await send_func(chunk.strip(), parse_mode=parse_mode)
        await asyncio.sleep(0.3)


class BotConfig:
    """Класс для загрузки конфигурации"""
    
    def __init__(self):
        self.check_interval = 3600
        self.price_change_threshold = 1.0
        self.notification_direction = 'both'  # both, down, up
        self.use_test_cookie_method = True
        self.bot_token = ''
        self.proxy_type = 'none'  # none, socks5, https
        self.proxy_url = ''
        self.admin_id = ''  # admin user ID

        self._load_config()
    
    def _load_config(self):
        """Загрузить конфигурацию из файла"""
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if key == 'CHECK_INTERVAL':
                            self.check_interval = int(value)
                        elif key == 'PRICE_CHANGE_THRESHOLD':
                            self.price_change_threshold = float(value)
                        elif key == 'NOTIFICATION_DIRECTION':
                            self.notification_direction = value
                        elif key == 'USE_TEST_COOKIE_METHOD':
                            self.use_test_cookie_method = value.lower() in ('1', 'true', 'yes', 'on')
                        elif key == 'BOT_TOKEN':
                            self.bot_token = value
                        elif key == 'PROXY_TYPE':
                            self.proxy_type = value
                        elif key == 'PROXY_URL':
                            self.proxy_url = value
                        elif key == 'ADMIN_ID':
                            self.admin_id = value
            
            logger.info(f"Конфигурация загружена: interval={self.check_interval}s, "
                       f"threshold={self.price_change_threshold}%, "
                       f"direction={self.notification_direction}, "
                       f"use_test_cookie_method={self.use_test_cookie_method}, "
                       f"proxy={self.proxy_type}")
                       
        except FileNotFoundError:
            logger.warning("Файл config.txt не найден, используются значения по умолчанию")
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")


config = BotConfig()


def load_bot_token() -> str:
    """Загрузить токен Telegram из переменной окружения."""
    token = os.environ.get("BOT_TOKEN")

    if token:
        logger.info("BOT_TOKEN успешно загружен из переменных окружения")
        return token.strip()

    # Локальный запуск: пробуем config.txt
    if config.bot_token:
        logger.info("BOT_TOKEN загружен из config.txt")
        return config.bot_token.strip()

    raise RuntimeError(
        "BOT_TOKEN не найден. Добавь переменную BOT_TOKEN в Railway Variables."
    )


# === Обработчики команд ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    
    # Сохраняем пользователя в БД
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    
    welcome_text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для отслеживания цен на Ozon.\n"
        "Отправь мне ссылку на товар, и я буду следить за изменением цены!\n\n"
        "📋 *Доступные команды:*\n"
        "/add - Добавить товар для отслеживания\n"
        "/cart - Добавить все товары из общей корзины Ozon\n"
        "/remove - Удалить товар из отслеживания\n"
        "/list - Показать мои товары\n"
        "/history - История цен товара\n"
        "/check - Принудительно проверить цену\n"
        "/checkall - Проверить все товары сразу\n"
        "/settings - Настройки бота\n"
        "/help - Показать это сообщение"
    )

    # Add admin commands if accessible
    if not config.admin_id or str(user.id) == config.admin_id:
        welcome_text += "\n/status - Статус бота\n/monitoring - Мониторинг"
    
    await reply_text_with_retry(update.message, welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = (
        "📖 *Как пользоваться ботом:*\n\n"
        "1️⃣ *Добавить товар:* отправь ссылку на Ozon или /add <ссылка>\n"
        "2️⃣ *Просмотреть товары:* /list\n"
        "3️⃣ *Удалить товар:* /remove <номер>\n"
        "4️⃣ *История цен:* /history <номер>\n"
        "5️⃣ *Проверить цену:* /check <номер>\n"
        "6️⃣ *Проверить все:* /checkall\n\n"
        "🔔 Бот проверяет цены автоматически с случайным интервалом "
        f"(в среднем каждые {config.check_interval // 60} мин) и уведомит "
        f"об изменении цены на {config.price_change_threshold}% или более.\n\n"
        "⚙️ *Настройки:* /settings"
    )

    # Add admin commands if accessible
    if not config.admin_id or str(update.effective_user.id) == config.admin_id:
        help_text += "\n\n👑 *Админ команды:*\n/status - Статус бота\n/monitoring - Мониторинг"
    
    await reply_text_with_retry(update.message, help_text, parse_mode='Markdown')


async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /add"""
    user_id = update.effective_user.id
    
    # Проверяем, есть ли аргументы (ссылка)
    if context.args:
        url = ' '.join(context.args)
        await _process_add(update, user_id, url)
    else:
        await reply_text_with_retry(update.message,
            "📝 Отправь ссылку на товар Ozon для добавления.\n\n"
            "Пример: /add https://www.ozon.ru/product/tovar-123456789/"
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик: ссылки Ozon, короткие ссылки ozon.ru/t/..., артикулы, корзины"""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    url = None

    # 1. Проверяем, это артикул (только цифры)?
    if is_valid_sku(text):
        url = sku_to_url(text)
        logger.info(f"Артикул распозна: {text} → {url}")

    # 2. Ищем URL в тексте (может быть "Название\nhttps://...")
    if not url:
        import re as re_mod
        url_match = re_mod.search(r'(https?://[^\s]+)', text)
        if url_match:
            url = url_match.group(1)

    if not url:
        return

    # Проверяем, что это Ozon
    if 'ozon.ru' not in url:
        return

    # Проверяем, это ссылка на корзину?
    share_code = extract_share_code(url)
    if share_code and ('cart' in url or 'share=' in url):
        # Автоматически добавляем товары из корзины
        msg = await reply_text_with_retry(update.message, "🛒 Распознана ссылка на корзину. Загружаю товары...")
        await _add_cart_products(update, user_id, share_code, msg)
        return

    # Поддерживаем полные, короткие ссылки на товары
    is_product_link = 'product' in url or '/t/' in url
    if not is_product_link:
        return

    await _process_add(update, user_id, url)


async def _process_add(update: Update, user_id: int, url: str):
    """Общая логика добавления товара"""
    # Убедимся что пользователь есть в БД
    user = update.effective_user
    db.add_user(
        user_id=user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    # Проверяем URL (поддерживаем и полные, и короткие ссылки)
    is_ozon = 'ozon.ru' in url
    is_product_link = 'product' in url or '/t/' in url

    if not is_ozon or not is_product_link:
        await reply_text_with_retry(update.message,
            "❌ Это не похоже на ссылку Ozon. Отправь корректную ссылку на товар."
        )
        return

    # Разрешаем короткую ссылку в полную (до добавления в БД)
    url = await resolve_short_url(url)
    logger.info(f"Ссылка разрешена: {url}")

    product_id = extract_product_id(url)
    if not product_id:
        await reply_text_with_retry(update.message,
            "❌ Не удалось распознать товар в ссылке. Попробуй другую ссылку."
        )
        return

    # Отправляем сообщение о начале проверки
    status_msg = await reply_text_with_retry(update.message, "⏳ Проверяю товар...")

    # Получаем информацию о товаре
    product_name, regular_price, ozon_bank_price, request_ok = await get_ozon_price(url)

    # Используем цену Ozon Банка если есть, иначе обычную
    display_price = ozon_bank_price or regular_price

    if not request_ok:
        await edit_message_with_retry(status_msg,
            "❌ Не удалось связаться с Ozon. Попробуй позже."
        )
        return

    if display_price is None:
        await edit_message_with_retry(status_msg,
            "❌ Не удалось получить цену товара. Возможно, товар недоступен или ссылка неверная."
        )
        return

    # Добавляем в БД (проверяем дубликаты)
    db_product_id, is_new = db.add_product(user_id, url)

    # Обновляем цену в БД (используем результат первого запроса)
    db.update_product_price(db_product_id, regular_price, ozon_bank_price, product_name, config.check_interval)

    # Если товар уже отслеживался — сообщаем об этом, но показываем актуальную цену
    if not is_new:
        name_text = f"📦 *{product_name}*\n" if product_name else ""
        price_text = f"💰 Текущая цена: *{display_price:,.0f} ₽*"
        if ozon_bank_price and regular_price and ozon_bank_price < regular_price:
            price_text += f" (обычная: {regular_price:,.0f} ₽)"

        await edit_message_with_retry(status_msg,
            f"ℹ️ *Этот товар уже отслеживается!*\n\n"
            f"{name_text}"
            f"{price_text}\n\n"
            f"Я обновил цену. Ты получишь уведомление, если она изменится.",
            parse_mode='Markdown',
        )
        return

    # Формируем сообщение для нового товара
    name_text = f"📦 *{product_name}*\n" if product_name else ""
    price_text = f"💰 Цена: *{display_price:,.0f} ₽*"
    if ozon_bank_price and regular_price and ozon_bank_price < regular_price:
        price_text += f" (обычная: {regular_price:,.0f} ₽)"
    elif regular_price:
        price_text += f" (обычная: {regular_price:,.0f} ₽)"

    # Кнопка порога уведомления
    threshold_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔔 Порог: {config.price_change_threshold}%",
            callback_data=f"detail_thresh:{db_product_id}"
        )]
    ])

    await edit_message_with_retry(status_msg,
        f"✅ *Товар добавлен!*\n\n"
        f"{name_text}"
        f"{price_text}\n\n"
        f"🔗 [Открыть товар]({url})\n\n"
        f"Я буду проверять цену каждые {config.check_interval // 60} минут "
        f"и уведомлю об изменении на {config.price_change_threshold}% или более.",
        parse_mode='Markdown',
        reply_markup=threshold_kb,
    )


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /list"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message,
            "📭 У тебя пока нет отслеживаемых товаров.\n"
            "Отправь ссылку на товар Ozon, чтобы начать!"
        )
        return
    
    message = "📋 *Твои товары:*\n\n"

    for idx, product in enumerate(products, 1):
        regular_price = product['current_price']
        bank_price = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None

        status = "✅" if (regular_price or bank_price) else "⏳"

        if bank_price:
            price = f"{bank_price:,.0f} ₽"
            if regular_price and bank_price < regular_price:
                price += f" ~~{regular_price:,.0f}~~"
        elif regular_price:
            price = f"{regular_price:,.0f} ₽"
        else:
            price = "Не определена"

        last_check = to_msk(product['last_checked']) if product['last_checked'] else "—"

        name = product['product_name'] or f"Товар #{product['product_id']}"
        if len(name) > 50:
            name = name[:47] + "..."

        ozon_url = product['ozon_url']

        message += (
            f"*{idx}.* {status} {name}\n"
            f"   💰 {price}\n"
            f"   🕐 {last_check}  [🔗 товар]({ozon_url})\n\n"
        )

    message += f"Всего товаров: *{len(products)}*"

    await send_long_message(message, lambda t, **k: reply_text_with_retry(update.message, t, **k))


async def remove_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /remove — первый шаг: запрос номера товара"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message,
            "📭 У тебя пока нет отслеживаемых товаров."
        )
        return

    # Формируем список товаров для показа
    items_text = ""
    for idx, p in enumerate(products, 1):
        name = (p['product_name'] or f"Товар #{p['product_id']}")[:40]
        bp = p['current_ozon_bank_price'] if 'current_ozon_bank_price' in p.keys() else None
        price = bp or p['current_price']
        price_str = f"{price:,.0f} ₽" if price else "—"
        items_text += f"  {idx}. {name} — {price_str}\n"

    await reply_text_with_retry(update.message,
        f"🗑 *Какой товар удалить?*\n\n"
        f"{items_text}\n"
        f"Отправь номер товара (1-{len(products)}) или /cancel для отмены.",
        parse_mode='Markdown',
    )
    db.db.set_user_state(user_id, STATE_REMOVE_ASK_NUMBER)


async def handle_remove_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Обработка шагов удаления"""
    state = db.get_user_state(user_id)

    if state["state"] == STATE_REMOVE_ASK_NUMBER:
        # Пользователь ввёл номер товара
        try:
            product_number = int(text)
        except ValueError:
            await reply_text_with_retry(update.message, "❌ Номер должен быть числом. Или отправь /cancel.")
            return

        products = db.get_user_products(user_id)
        if product_number < 1 or product_number > len(products):
            await reply_text_with_retry(update.message,
                f"❌ Неверный номер. Доступные номера: 1-{len(products)}\n"
                f"Или отправь /cancel для отмены."
            )
            return

        product = products[product_number - 1]
        name = product['product_name'] or f"Товар #{product['product_id']}"
        if len(name) > 50:
            name = name[:47] + "..."

        await reply_text_with_retry(update.message,
            f"🗑 *Удалить этот товар?*\n\n"
            f"{name}\n"
            f"🔗 [Открыть товар]({product['ozon_url']})",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([

                [
                    InlineKeyboardButton("✅ Да", callback_data=f"del_yes:{product['product_id']}"),
                    InlineKeyboardButton("❌ Нет", callback_data=f"del_no:{product['product_id']}"),
                ]
            ]),
        )
        db.set_user_state(user_id, STATE_REMOVE_CONFIRM, {"product_id": product['product_id']})


async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок Да/Нет при удалении"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    data = query.data  # "del_yes:123" или "del_no:123"
    logger.info(f"handle_delete_callback: data={data}, user={user_id}")

    action, pid_str = data.split(":", 1)
    product_id = int(pid_str)

    # Проверяем, что товар принадлежит этому пользователю
    products = db.get_user_products(user_id)
    product_ids = [p['product_id'] for p in products]
    if product_id not in product_ids:
        await edit_message_with_retry(query, "❌ Этот товар не найден.")
        db.clear_user_state(user_id)
        return

    product = next((p for p in products if p['product_id'] == product_id), None)

    if action == "del_yes":
        success = db.delete_product(product_id, user_id)
        if success:
            name = (product['product_name'] if product else f"Товар #{product_id}")[:50]
            # Кнопка "Назад к списку"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К списку", callback_data=f"list_back:{product_id}")]])
            await edit_message_with_retry(query, f"✅ Товар удалён: {name}", reply_markup=kb)
        else:
            await edit_message_with_retry(query, "❌ Ошибка при удалении.")
        db.clear_user_state(user_id)
    else:
        # Возвращаемся к детальному просмотру товара
        await show_product_detail(update, user_id, product_id)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /history — первый шаг: запрос номера товара"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message, "📭 У тебя пока нет отслеживаемых товаров.")
        return

    items_text = ""
    for idx, p in enumerate(products, 1):
        name = (p['product_name'] or f"Товар #{p['product_id']}")[:40]
        items_text += f"  {idx}. {name}\n"

    await reply_text_with_retry(update.message,
        f"📈 *История цен какого товара показать?*\n\n"
        f"{items_text}\n"
        f"Отправь номер товара (1-{len(products)}) или /cancel для отмены.",
        parse_mode='Markdown',
    )
    db.set_user_state(user_id, STATE_HISTORY_ASK_NUMBER)


async def handle_history_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Обработка шагов истории цен"""
    try:
        product_number = int(text)
    except ValueError:
        await reply_text_with_retry(update.message, "❌ Номер должен быть числом. Или отправь /cancel.")
        return

    products = db.get_user_products(user_id)
    if product_number < 1 or product_number > len(products):
        await reply_text_with_retry(update.message, f"❌ Неверный номер. Доступные: 1-{len(products)}")
        return

    product = products[product_number - 1]
    price_history = db.get_price_history(product['product_id'], limit=10)
    full_history = db.get_price_history(product['product_id'], limit=None, ascending=True)

    if not price_history:
        await reply_text_with_retry(update.message, "📭 История цен пока пуста.")
        db.clear_user_state(user_id)
        return

    name = product['product_name'] or f"Товар #{product['product_id']}"
    min_price, max_price = db.get_price_stats(product['product_id'])
    latest_price = price_history[0]['price'] if price_history else None
    caption = f"📈 *График цены: {name}*\n🔗 [Открыть товар]({product['ozon_url']})"
    if latest_price is not None:
        caption += f"\n💰 *Текущая:* {latest_price:,.0f} ₽"
    if min_price is not None and max_price is not None:
        caption += f"\n📊 *Мин:* {min_price:,.0f} ₽  *Макс:* {max_price:,.0f} ₽"

    chart = build_price_history_chart(name, full_history)
    if chart is not None:
        await reply_photo_with_retry(
            update.message,
            chart,
            caption=caption,
            parse_mode='Markdown',
        )

    message = f"📈 *Последние изменения: {name}*\n🔗 [Открыть товар]({product['ozon_url']})\n\n"

    for idx, record in enumerate(price_history[:10], 1):
        price = f"{record['price']:,.2f} ₽"
        change = ""
        if record['old_price']:
            diff = record['price'] - record['old_price']
            arrow = "📈" if diff > 0 else "📉"
            change = f" {arrow} ({'+' if diff > 0 else ''}{diff:,.2f} ₽)"

        try:
            time_str = to_msk(record['checked_at'])
        except:
            time_str = record['checked_at']

        message += f"*{idx}.* {time_str} - {price}{change}\n"

    db.clear_user_state(user_id)
    await send_long_message(message, lambda t, **k: reply_text_with_retry(update.message, t, **k))


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /check — первый шаг: запрос номера товара"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message, "📭 У тебя пока нет отслеживаемых товаров.")
        return

    items_text = ""
    for idx, p in enumerate(products, 1):
        name = (p['product_name'] or f"Товар #{p['product_id']}")[:40]
        bp = p['current_ozon_bank_price'] if 'current_ozon_bank_price' in p.keys() else None
        price = bp or p['current_price']
        price_str = f"{price:,.0f} ₽" if price else "—"
        items_text += f"  {idx}. {name} — {price_str}\n"

    await reply_text_with_retry(update.message,
        f"🔍 *Цену какого товара проверить?*\n\n"
        f"{items_text}\n"
        f"Отправь номер товара (1-{len(products)}) или /cancel для отмены.",
        parse_mode='Markdown',
    )
    db.set_user_state(user_id, STATE_CHECK_ASK_NUMBER)


async def handle_check_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Обработка шагов проверки цены"""
    try:
        product_number = int(text)
    except ValueError:
        await reply_text_with_retry(update.message, "❌ Номер должен быть числом. Или отправь /cancel.")
        return

    products = db.get_user_products(user_id)
    if product_number < 1 or product_number > len(products):
        await reply_text_with_retry(update.message, f"❌ Неверный номер. Доступные: 1-{len(products)}")
        return

    product = products[product_number - 1]
    status_msg = await reply_text_with_retry(update.message, "⏳ Проверяю цену...")

    old_regular = product['current_price']
    old_bank = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None
    old_price = old_bank or old_regular

    product_name, new_regular, new_bank, request_ok = await get_ozon_price(product['ozon_url'])

    if not request_ok:
        await edit_message_with_retry(status_msg, "❌ Ошибка сети. Не удалось связаться с Ozon, попробуй позже.")
        db.clear_user_state(user_id)
        return

    new_price = new_bank or new_regular

    if new_price is None:
        await edit_message_with_retry(status_msg, "❌ Не удалось получить цену товара (товар может быть снят с продажи).")
        db.clear_user_state(user_id)
        return

    db.update_product_price(product['product_id'], new_regular, new_bank, product_name, config.check_interval)

    name = product_name or product['product_name'] or f"Товар #{product['product_id']}"
    sent_notifications = 0

    if old_price and new_price:
        diff = new_price - old_price
        percent = (diff / old_price) * 100
        changed = abs(percent) >= config.price_change_threshold

        if changed:
            arrow = "📉" if diff < 0 else "📈"
            price_details = f"*{new_price:,.0f} ₽*"
            if new_bank and new_regular and new_bank < new_regular:
                price_details += f" (обычная: {new_regular:,.0f} ₽)"
            elif new_regular:
                price_details += f" (обычная: {new_regular:,.0f} ₽)"

            message = (
                f"{arrow} *Цена изменилась!*\n\n"
                f"*{name}*\n"
                f"Было: {old_price:,.0f} ₽\n"
                f"Стало: {price_details}\n"
                f"Изменение: {'+' if diff > 0 else ''}{diff:,.0f} ₽ ({'+' if percent > 0 else ''}{percent:.1f}%)\n\n"
                f"🔗 [Открыть товар]({product['ozon_url']})"
            )
            reply_markup = build_history_price_keyboard(product['product_id'])
            sent_notifications = await notify_price_change_subscribers(
                context.bot,
                product,
                product_name,
                old_price,
                new_regular,
                new_bank,
                skip_user_id=user_id,
            )
        else:
            price_details = f"*{new_price:,.0f} ₽*"
            if new_bank and new_regular and new_bank < new_regular:
                price_details += f" (обычная: {new_regular:,.0f} ₽)"

            message = (
                f"✅ *Цена не изменилась*\n\n"
                f"*{name}*\n"
                f"Цена: {price_details}\n\n"
                f"🔗 [Открыть товар]({product['ozon_url']})"
            )
            reply_markup = None
    else:
        price_details = f"*{new_price:,.0f} ₽*"
        if new_bank and new_regular and new_bank < new_regular:
            price_details += f" (обычная: {new_regular:,.0f} ₽)"

        message = (
            f"✅ *Цена получена*\n\n"
            f"*{name}*\n"
            f"Цена: {price_details}\n\n"
            f"🔗 [Открыть товар]({product['ozon_url']})"
        )
        reply_markup = None

    db.clear_user_state(user_id)
    if sent_notifications > 0:
        message += f"\n\n🔔 Другим подписчикам отправлено уведомлений: {sent_notifications}"
    await edit_message_with_retry(status_msg, message, parse_mode='Markdown', reply_markup=reply_markup)


async def handle_threshold_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Обработка ввода порога уведомления"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        threshold = float(text.replace('%', '').strip())
    except ValueError:
        await reply_text_with_retry(update.message, "❌ Введи число от 0 до 100. Или /cancel.")
        return

    if threshold < 0 or threshold > 100:
        await reply_text_with_retry(update.message, "❌ Порог должен быть от 0 до 100. Или /cancel.")
        return

    state = db.get_user_state(user_id)
    product_id = state.get("data", {}).get("product_id")

    if not product_id:
        await reply_text_with_retry(update.message, "❌ Ошибка. Попробуй снова через 🔔 Порог.")
        db.clear_user_state(user_id)
        return

    db.set_user_product_threshold(user_id, product_id, threshold)
    db.clear_user_state(user_id)

    thresh_str = f"{threshold}%" if threshold > 0 else "любое изменение"

    # Кнопка "Назад к товару"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К товару", callback_data=f"list_prod:{product_id}")]])

    # Если это callback_query (нажали кнопку из страницы товара)
    if update.callback_query:
        await edit_message_with_retry(update.callback_query,
            f"✅ Порог уведомления установлен: *{thresh_str}*",
            parse_mode='Markdown',
            reply_markup=kb,
        )
    else:
        await reply_text_with_retry(update.message,
            f"✅ Порог уведомления установлен: *{thresh_str}*",
            parse_mode='Markdown',
            reply_markup=kb,
        )


async def check_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /checkall — проверить все товары пользователя"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message,
            "📭 У тебя пока нет отслеживаемых товаров.\n"
            "Отправь ссылку на товар Ozon, чтобы начать!"
        )
        return

    # Проверка cooldown
    now = datetime.now()
    last_checkall = _checkall_cooldowns.get(user_id)
    if last_checkall:
        elapsed = (now - last_checkall).total_seconds()
        remaining = CHECKALL_COOLDOWN_SECONDS - elapsed
        if remaining > 0:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await reply_text_with_retry(update.message,
                f"⏳ Слишком часто! Следующая проверка доступна через *{minutes} мин {seconds} сек*.",
                parse_mode='Markdown',
            )
            return

    # Устанавливаем cooldown
    _checkall_cooldowns[user_id] = now

    status_msg = await reply_text_with_retry(update.message,
        f"⏳ Проверяю {len(products)} товар(ов)..."
    )

    # Перемешиваем порядок проверки
    import random
    products_shuffled = list(products)
    random.shuffle(products_shuffled)

    results = []
    for product in products_shuffled:
        try:
            old_regular = product['current_price']
            old_bank = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None
            old_price = old_bank or old_regular

            product_name, new_regular, new_bank, request_ok = await get_ozon_price(product['ozon_url'])

            if not request_ok:
                results.append(f"❌ Ошибка сети")
                continue

            new_price = new_bank or new_regular

            if new_price is None:
                results.append(f"❌ Цена не получена")
                continue

            db.update_product_price(product['product_id'], new_regular, new_bank, product_name)

            name = product_name or product['product_name'] or f"Товар #{product['product_id']}"
            if len(name) > 40:
                name = name[:37] + "..."

            if old_price:
                diff = new_price - old_price
                percent = (diff / old_price) * 100
                arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
                results.append(f"{arrow} *{name}*: {new_price:,.0f} ₽ ({'+' if diff > 0 else ''}{percent:.1f}%) [🔗]({product['ozon_url']})")
            else:
                results.append(f"✅ *{name}*: {new_price:,.0f} ₽ [🔗]({product['ozon_url']})")

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Ошибка проверки товара #{product['product_id']}: {e}")
            results.append(f"❌ Ошибка проверки")

    message = f"📊 *Результат проверки всех товаров:*\n\n"
    for r in results:
        message += f"{r}\n"

    message += f"\nПроверено: *{len(results)}*"
    await send_long_message(message, lambda t, **k: edit_message_with_retry(status_msg, t, **k) if status_msg else reply_text_with_retry(update.message, t, **k))


async def _add_cart_products(update, user_id, share_code, status_msg):
    """Внутренняя функция: добавить товары из корзины с обновлением кук"""
    from ozon_client import client as ozon_client
    from cart_parser import get_cart_products

    # Пробуем получить товары
    cart_items = get_cart_products(share_code)

    # Если пусто — пробуем обновить куки
    if not cart_items:
        await edit_message_with_retry(status_msg, "⏳ Не удалось загрузить корзину. Обновляю куки...")
        captcha_ok = await ozon_client._solve_captcha()
        if not captcha_ok:
            await edit_message_with_retry(status_msg,
                "❌ Не удалось загрузить товары из корзины.\n"
                "Попробуй позже или проверь, что ссылка на корзину актуальна."
            )
            return

        # Пробуем снова с новыми куками
        cart_items = get_cart_products(share_code)

    if not cart_items:
        await edit_message_with_retry(status_msg,
            "❌ Не удалось загрузить товары из корзины.\n"
            "Возможно, корзина пуста или ссылка устарела."
        )
        return

    added = 0
    skipped = 0
    updated = 0
    errors = 0

    for item in cart_items:
        try:
            url = item["url"]
            name = item.get("name", "")
            regular_price = item.get("regular_price")
            ozon_bank_price = item.get("ozon_bank_price")

            # Добавляем товар
            product_id, is_new = db.add_product(user_id, url)

            if not is_new:
                skipped += 1
                # Даже для существующих обновляем цену из корзины
                if regular_price or ozon_bank_price:
                    db.update_product_price(
                        product_id, regular_price, ozon_bank_price, name, config.check_interval
                    )
                    updated += 1
                continue

            # Обновляем цену для нового товара
            if regular_price or ozon_bank_price:
                db.update_product_price(
                    product_id, regular_price, ozon_bank_price, name, config.check_interval
                )
            added += 1

        except Exception as e:
            logger.error(f"Ошибка добавления товара из корзины: {e}")
            errors += 1

    message = f"✅ *Добавлено товаров:* {added}"
    if skipped > 0:
        message += f"\n⏭ Уже отслеживаются: {skipped}"
    if updated > 0:
        message += f"\n🔄 Цен обновлено: {updated}"
    if errors > 0:
        message += f"\n❌ Ошибок: {errors}"
    message += f"\n\nВсего в корзине: {len(cart_items)}"

    await edit_message_with_retry(status_msg, message, parse_mode='Markdown')


async def add_from_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /cart — добавить товары из общей корзины Ozon"""
    user_id = update.effective_user.id

    if not context.args:
        await reply_text_with_retry(update.message,
            "🛒 Отправь ссылку на общую корзину Ozon.\n\n"
            "Пример: /cart https://www.ozon.ru/cart?share=hogp7Uk\n\n"
            "Чтобы получить ссылку: открой корзину в приложении Ozon → "
            "нажми «Поделиться» → скопируй ссылку."
        )
        return

    cart_url = ' '.join(context.args)
    share_code = extract_share_code(cart_url)

    if not share_code:
        await reply_text_with_retry(update.message,
            "❌ Не удалось извлечь код корзины из ссылки.\n"
            "Убедись, что ссылка имеет формат: https://www.ozon.ru/cart?share=XXXXX"
        )
        return

    status_msg = await reply_text_with_retry(update.message, "⏳ Загружаю товары из корзины...")
    await _add_cart_products(update, user_id, share_code, status_msg)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /cancel — отменить текущее состояние"""
    user_id = update.effective_user.id
    db.clear_user_state(user_id)
    await reply_text_with_retry(update.message, "❌ Отменено. Напиши /help для списка команд.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик текстовых сообщений (не команд).
    Приоритет:
    1. Если пользователь в состоянии FSM — обрабатываем шаг
    2. Если сообщение содержит ссылку Ozon — добавляем товар
    3. Если артикул — добавляем товар
    4. Иначе — подсказка
    """
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # 1. Проверяем, есть ли активное FSM-состояние
    state = db.get_user_state(user_id)
    if state.get("state"):
        state_type = state["state"]

        if state_type == STATE_REMOVE_ASK_NUMBER:
            await handle_remove_step(update, context, user_id, text)
            return
        elif state_type == STATE_REMOVE_CONFIRM:
            await handle_remove_step(update, context, user_id, text)
            return
        elif state_type == STATE_HISTORY_ASK_NUMBER:
            await handle_history_step(update, context, user_id, text)
            return
        elif state_type == STATE_CHECK_ASK_NUMBER:
            await handle_check_step(update, context, user_id, text)
            return
        elif state_type == STATE_THRESHOLD_ASK:
            await handle_threshold_step(update, context, user_id, text)
            return

    # 2. Проверяем, не ссылка ли это (добавление товара)
    # Ищем URL в тексте
    import re as re_mod
    url_match = re_mod.search(r'(https?://[^\s]+)', text)
    if url_match:
        url = url_match.group(1)
        if 'ozon.ru' in url:
            # Сбрасываем состояние и добавляем товар
            db.clear_user_state(user_id)
            await _process_add(update, user_id, url)
            return

    # 3. Проверяем, не артикул ли это
    if is_valid_sku(text):
        db.clear_user_state(user_id)
        await _process_add(update, user_id, sku_to_url(text))
        return

    # 4. Неизвестное сообщение
    await reply_text_with_retry(update.message,
        "🤔 Не понял сообщение.\n\n"
        "Поддерживаются:\n"
        "• Ссылки на товары Ozon\n"
        "• Сокращённые ссылки ozon.ru/t/...\n"
        "• Артикул товара (только цифры)\n"
        "• Ссылка на корзину Ozon\n\n"
        "Напиши /help для списка команд."
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /settings"""
    keyboard = [
        [InlineKeyboardButton("ℹ️ О боте", callback_data='settings_info')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = (
        "⚙️ *Настройки бота*\n\n"
        f"🔄 Интервал проверки: *{config.check_interval // 60} мин*\n"
        f"📊 Порог изменения цены: *{config.price_change_threshold}%*\n"
        f"🔔 Направление: *{config.notification_direction}*\n\n"
        "Для изменения настроек отредактируй файл config.txt"
    )
    
    await reply_text_with_retry(update.message, message, parse_mode='Markdown', reply_markup=reply_markup)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    user_id = update.effective_user.id

    # Check admin access
    if config.admin_id and str(user_id) != config.admin_id:
        await reply_text_with_retry(update.message, "❌ Доступ запрещен. Эта команда доступна только администратору.")
        return

    # Calculate uptime
    uptime = datetime.now(timezone.utc) - BOT_START_TIME
    uptime_str = str(uptime).split('.')[0]  # Remove microseconds

    # Get stats
    users_count = db.get_users_count()
    products_count = db.get_products_count()
    active_products = db.get_active_products_count()

    status_text = (
        "📊 *Статус бота*\n\n"
        f"⏱ Время работы: {uptime_str}\n"
        f"👥 Пользователей: {users_count}\n"
        f"📦 Товаров всего: {products_count}\n"
        f"✅ Активных товаров: {active_products}\n"
        f"🔄 Интервал проверки: {config.check_interval // 60} мин\n"
        f"📊 Порог изменения: {config.price_change_threshold}%\n"
        f"🔔 Направление: {config.notification_direction}"
    )

    await reply_text_with_retry(update.message, status_text, parse_mode='Markdown')


async def monitoring_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /monitoring — расширенная статистика"""
    user_id = update.effective_user.id

    # Check admin access
    if config.admin_id and str(user_id) != config.admin_id:
        await reply_text_with_retry(update.message, "❌ Доступ запрещен. Эта команда доступна только администратору.")
        return

    # Calculate uptime
    uptime = datetime.now(timezone.utc) - BOT_START_TIME
    uptime_str = str(uptime).split('.')[0]  # Remove microseconds

    # Get stats
    users_count = db.get_users_count()
    products_count = db.get_products_count()
    active_products = db.get_active_products_count()

    # Monitoring stats
    total_checks = monitoring_stats['total_checks']
    successful_checks = monitoring_stats['successful_checks']
    failed_checks = monitoring_stats['failed_checks']
    notifications_sent = monitoring_stats['notifications_sent']
    last_check_time = monitoring_stats['last_check_time']
    last_check_str = to_msk(last_check_time.isoformat()) if last_check_time else "—"

    success_rate = (successful_checks / total_checks * 100) if total_checks > 0 else 0

    monitoring_text = (
        "📈 *Мониторинг бота*\n\n"
        f"⏱ Время работы: {uptime_str}\n"
        f"👥 Пользователей: {users_count}\n"
        f"📦 Товаров всего: {products_count}\n"
        f"✅ Активных товаров: {active_products}\n\n"
        f"🔍 Всего проверок: {total_checks}\n"
        f"✅ Успешных проверок: {successful_checks}\n"
        f"❌ Неудачных проверок: {failed_checks}\n"
        f"📊 Успешность: {success_rate:.1f}%\n"
        f"🔔 Отправлено уведомлений: {notifications_sent}\n"
        f"🕐 Последняя проверка: {last_check_str}\n\n"
        f"🔄 Интервал проверки: {config.check_interval // 60} мин\n"
        f"📊 Порог изменения: {config.price_change_threshold}%\n"
        f"🔔 Направление: {config.notification_direction}"
    )

    await reply_text_with_retry(update.message, monitoring_text, parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway, as answering is not critical
    
    if query.data == 'settings_info':
        await edit_message_with_retry(query,
            "ℹ️ *О боте*\n\n"
            "Бот отслеживает цены на товары Ozon и уведомляет об изменениях.\n\n"
            "📝 *Как это работает:*\n"
            "1. Добавь товар командой /add или отправив ссылку\n"
            "2. Бот проверяет цены с заданным интервалом\n"
            "3. При изменении цены получаешь уведомление\n\n"
            "⚙️ *Настройка:*\n"
            "Измени файл config.txt:\n"
            "- CHECK_INTERVAL - интервал в секундах\n"
            "- PRICE_CHANGE_THRESHOLD - порог изменения в %\n"
            "- NOTIFICATION_DIRECTION - both/down/up\n\n"
            "После изменения файла перезапусти бота.",
            parse_mode='Markdown',
        )


# === Фоновая задача проверки цен ===

async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка товаров, у которых пришло время"""
    import random

    logger.info("Запуск периодической проверки цен...")

    # Получаем только товары, у которых пришло время проверки
    products = db.get_due_products(config.check_interval)

    if not products:
        # Логируем детали для отладки
        all_products = db.get_active_products()
        if all_products:
            now_utc = datetime.now(timezone.utc)
            threshold_ts = now_utc.timestamp() - config.check_interval
            logger.info(
                f"Есть активные товары: {len(all_products)}, "
                f"порог: {threshold_ts:.0f}, "
                f"сейчас: {now_utc.isoformat()}"
            )
            for p in all_products:
                lc = p['last_checked']
                logger.info(f"  Товар #{p['product_id']}: last_checked={lc}, active={p['is_active']}")
        logger.info("Нет товаров, готовых к проверке")
        return

    # Перемешиваем порядок
    products = list(products)
    random.shuffle(products)
    logger.info(f"Товаров для проверки: {len(products)}")

    # Update monitoring stats
    monitoring_stats['total_checks'] += len(products)

    checked = 0
    notifications = 0
    
    for product in products:
        try:
            old_regular = product['current_price']
            old_bank = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None
            old_price = old_bank or old_regular

            product_name, new_regular, new_bank, request_ok = await get_ozon_price(product['ozon_url'])

            # Ошибка сети/капчи — НЕ меняем статус товара, просто пропускаем
            if not request_ok:
                logger.warning(f"Товар #{product['product_id']}: ошибка сети, статус не изменён")
                monitoring_stats['failed_checks'] += 1
                continue

            new_price = new_bank or new_regular

            # Товар снят с продажи (request_ok=True, но цена=None — страница 200, цены нет)
            if new_price is None:
                was_avail, is_avail = db.update_product_availability(
                    product['product_id'], False, config.check_interval
                )
                if was_avail:
                    # Уведомляем о снятии
                    name = product_name or product['product_name'] or f"Товар #{product['product_id']}"
                    message = f"❌ *Товар снят с продажи*\n\n*{name}*\n\n🔗 [Открыть товар]({product['ozon_url']})"

                    user_entries = db.get_user_product_for_notification(product['product_id'])
                    for entry in user_entries:
                        try:
                            await send_message_with_retry(
                                context.bot,
                                chat_id=entry['user_id'],
                                text=message,
                                parse_mode='Markdown',
                                disable_web_page_preview=True,
                            )
                            notifications += 1
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")

                logger.info(f"Товар #{product['product_id']} снят с продажи, время проверки обновлено")
                continue

            # Обновляем в БД и статус доступности
            old_available = bool(product['is_available']) if 'is_available' in product.keys() else True
            if not old_available:
                db.update_product_availability(product['product_id'], True, config.check_interval)
                # Уведомляем о возвращении в продажу
                name = product_name or product['product_name'] or f"Товар #{product['product_id']}"
                price_detail = f"*{new_price:,.0f} ₽*"
                if new_bank and new_regular and new_bank < new_regular:
                    price_detail += f" (обычная: {new_regular:,.0f} ₽)"

                message = f"✅ *Товар снова в продаже!*\n\n*{name}*\n💰 Цена: {price_detail}\n\n🔗 [Открыть товар]({product['ozon_url']})"
                user_entries = db.get_user_product_for_notification(product['product_id'])
                for entry in user_entries:
                    try:
                        await send_message_with_retry(
                            context.bot,
                            chat_id=entry['user_id'],
                            text=message,
                            parse_mode='Markdown',
                            disable_web_page_preview=True,
                        )
                        notifications += 1
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления: {e}")

            # Обновляем в БД
            db.update_product_price(product['product_id'], new_regular, new_bank, product_name, config.check_interval)
            checked += 1
            monitoring_stats['successful_checks'] += 1

            notifications += await notify_price_change_subscribers(
                context.bot,
                product,
                product_name,
                old_price,
                new_regular,
                new_bank,
            )

            # Небольшая задержка между запросами
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Ошибка проверки товара #{product['product_id']}: {e}")
    
    logger.info(
        f"Проверка завершена: проверено {checked}, "
        f"отправлено уведомлений {notifications}"
    )

    # Update monitoring stats
    monitoring_stats['notifications_sent'] += notifications
    monitoring_stats['last_check_time'] = datetime.now(timezone.utc)


# === Инициализация бота ===

def create_application() -> Application:
    """Создать и настроить приложение бота"""
    
    # Инициализируем БД
    db.init_db()
    logger.info("База данных инициализирована")
    
    # Загружаем токен
    token = load_bot_token()

    # Создаём HTTPXRequest с proxy только для Telegram
    proxy_url = _get_proxy_config()
    request_kwargs = {
        'connection_pool_size': 8,
        'connect_timeout': 30.0,
        'read_timeout': 30.0,
        'write_timeout': 30.0,
        'pool_timeout': 30.0,
    }
    if proxy_url:
        request_kwargs['proxy'] = proxy_url
        logger.info(f"Telegram proxy configured: {_mask_proxy_url(proxy_url)}")

    request = HTTPXRequest(**request_kwargs)
    logger.info("HTTPXRequest создан")

    # Создаем приложение с JobQueue и кастомным request
    application = (
        Application.builder()
        .token(token)
        .job_queue(JobQueue())
        .request(request)
        .get_updates_request(request)
        .build()
    )
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_product))
    application.add_handler(CommandHandler("remove", remove_product))
    application.add_handler(CommandHandler("list", list_products_paginated))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("checkall", check_all))
    application.add_handler(CommandHandler("cart", add_from_cart))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("monitoring", monitoring_command))
    
    # Обработчик нажатий на кнопки
    application.add_handler(
        # Пропускаем ссылки ozon.ru И артикулы (7-12 цифр)
        MessageHandler(
            filters.TEXT & filters.Regex(r'(ozon\.ru|^\d{7,16}$)'),
            handle_link
        )
    )
    application.add_handler(MessageHandler(filters.Regex(r'callback_query'), button_handler))

    # Обработчик callback_query для кнопок удаления
    application.add_handler(CallbackQueryHandler(handle_delete_callback, pattern=r'^del_(yes|no):'))

    # Пагинация /list
    add_list_handlers(application)
    # Обработчик callback_query для остальных кнопок
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Добавляем периодическую задачу (запускаем каждую минуту)
    job_queue = application.job_queue
    job_queue.run_repeating(
        price_check_job,
        interval=60,  # Проверяем каждую минуту, но проверяются только due-товары
        first=10,  # Первая проверка через 10 секунд после запуска
        name="price_check",
    )

    # Обработчик неизвестных сообщений (последний в цепочке)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info(
        f"Бот настроен. Интервал проверки: {config.check_interval}с, "
        f"порог: {config.price_change_threshold}%, "
        f"направление: {config.notification_direction}"
    )

    return application


def run_bot():
    """Запустить бота"""
    logger.info("Запуск бота...")
    
    try:
        application = create_application()
        logger.info("Бот запущен и ожидает сообщения")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}", exc_info=True)
        raise
