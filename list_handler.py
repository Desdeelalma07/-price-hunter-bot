"""
Обработчик пагинации для /list — кнопки страниц и детальный просмотр товаров.
"""

import re
import logging
import asyncio
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
from price_chart import build_price_history_chart

logger = logging.getLogger(__name__)

# Retry settings for free proxy
MAX_EDIT_RETRIES = 3
MAX_REPLY_RETRIES = 3
RETRY_DELAY = 1  # seconds


async def edit_message_with_retry(query, text: str, **kwargs) -> object:
    """Edit message with retry on failure"""
    for attempt in range(MAX_EDIT_RETRIES):
        try:
            return await query.edit_message_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"Edit attempt {attempt + 1}/{MAX_EDIT_RETRIES} failed: {e}")
            if attempt == MAX_EDIT_RETRIES - 1:
                logger.error(f"Failed to edit message after {MAX_EDIT_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def reply_text_with_retry(message, text: str, **kwargs) -> object:
    """Reply to message with retry on failure"""
    for attempt in range(MAX_REPLY_RETRIES):
        try:
            return await message.reply_text(text, **kwargs)
        except Exception as e:
            logger.warning(f"Reply attempt {attempt + 1}/{MAX_REPLY_RETRIES} failed: {e}")
            if attempt == MAX_REPLY_RETRIES - 1:
                logger.error(f"Failed to reply after {MAX_REPLY_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)


async def edit_message_media_with_retry(query, media, **kwargs) -> object:
    """Edit message media with retry on failure"""
    for attempt in range(MAX_REPLY_RETRIES):
        try:
            if hasattr(media.media, "seek"):
                media.media.seek(0)
            return await query.edit_message_media(media=media, **kwargs)
        except Exception as e:
            logger.warning(f"Media edit attempt {attempt + 1}/{MAX_REPLY_RETRIES} failed: {e}")
            if attempt == MAX_REPLY_RETRIES - 1:
                logger.error(f"Failed to edit media after {MAX_REPLY_RETRIES} attempts")
                raise
            await asyncio.sleep(RETRY_DELAY)

import database as db

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
PRODUCTS_PER_ROW = 5

# FSM states (defined here to avoid circular import with bot.py)
STATE_THRESHOLD_ASK = "threshold_ask"

# Timezone for display
MSK_TZ = timezone(timedelta(hours=3))


def to_msk(dt_str: str) -> str:
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


def get_user_state(user_id: int) -> dict:
    return db.get_user_state(user_id)


def set_user_state(user_id: int, state: str, data: dict = None):
    db.set_user_state(user_id, state, data)


def clear_user_state(user_id: int):
    db.clear_user_state(user_id)


async def list_products_paginated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /list с пагинацией"""
    user_id = update.effective_user.id
    products = db.get_user_products(user_id)

    if not products:
        await reply_text_with_retry(update.message,
            "📭 У тебя пока нет отслеживаемых товаров.\n"
            "Отправь ссылку на товар Ozon, чтобы начать!"
        )
        return

    # Первая страница
    await _send_list_page(update, user_id, products, 0, edit=False)


async def _send_list_page(update, user_id, products, page, edit=True):
    """Отправить страницу списка товаров"""
    total = len(products)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_products = products[start:end]

    # Формируем текст страницы
    text = f"📋 *Твои товары (стр. {page + 1}/{total_pages}):*\n\n"

    # Собираем статистику и пороги для товаров на странице
    product_ids = [p['product_id'] for p in page_products]
    stats_cache = {}
    threshold_cache = {}
    for pid in product_ids:
        min_p, max_p = db.get_price_stats(pid)
        stats_cache[pid] = (min_p, max_p)
        threshold_cache[pid] = db.get_user_product_threshold(user_id, pid)

    # Формируем текст страницы
    text = f"📋 *Твои товары (стр. {page + 1}/{total_pages}):*\n\n"

    for idx, product in enumerate(page_products):
        num = start + idx + 1
        regular_price = product['current_price']
        bank_price = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None
        is_available = bool(product['is_available']) if 'is_available' in product.keys() else True
        product_id = product['product_id']

        # Статус
        if is_available:
            status = "✅" if (regular_price or bank_price) else "⏳"
        else:
            status = "❌"

        # Цена
        if bank_price:
            price = f"{bank_price:,.0f} ₽"
            if regular_price and bank_price < regular_price:
                price += f" ~~{regular_price:,.0f}~~"
        elif regular_price:
            price = f"{regular_price:,.0f} ₽"
        else:
            price = "—"

        # Время проверки
        last_check = to_msk(product['last_checked']) if product['last_checked'] else "—"

        # Min/Max цены
        min_p, max_p = stats_cache.get(product_id, (None, None))
        stats_line = ""
        if min_p is not None and max_p is not None:
            stats_line = f"\n   📊 Мин: {min_p:,.0f} ₽  Макс: {max_p:,.0f} ₽"

        # Порог уведомления
        user_thresh = threshold_cache.get(product_id)
        from bot import config
        eff_thresh = user_thresh if user_thresh is not None else config.price_change_threshold
        thresh_line = f"\n   🔔 {eff_thresh}%"

        # Название
        name = product['product_name'] or f"Товар #{product_id}"
        if len(name) > 35:
            name = name[:32] + "..."

        # Ссылка (Markdown: [текст](url))
        url = product['ozon_url']

        text += f"*{num}.* {status} {name}\n"
        text += f"   💰 {price}"
        text += stats_line
        text += thresh_line
        text += f"\n   🕐 {last_check}  [🔗 товар]({url})\n\n"

    text += f"Всего: *{total}*"

    # Формируем кнопки
    keyboard = []

    # Кнопки товаров (по PRODUCTS_PER_ROW в ряд) — всегда добавляем
    if page_products:
        for row_start in range(0, len(page_products), PRODUCTS_PER_ROW):
            row_items = page_products[row_start:row_start + PRODUCTS_PER_ROW]
            btn_row = []
            for idx, p in enumerate(row_items):
                num = start + row_start + idx + 1
                btn_row.append(InlineKeyboardButton(str(num), callback_data=f"list_prod:{p['product_id']}"))
            keyboard.append(btn_row)

    # Навигация — добавляем ПОСЛЕ кнопок товаров
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"list_pg:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"list_pg:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    if edit:
        try:
            await edit_message_with_retry(update.callback_query, text, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            await reply_text_with_retry(update.callback_query.message, text, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)
    else:
        await reply_text_with_retry(update.message, text, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)

    # Сохраняем текущую страницу
    set_user_state(user_id, "list_page", {"page": page})


async def handle_list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопки страницы"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    products = db.get_user_products(user_id)
    if not products:
        await edit_message_with_retry(query, "📭 Список пуст.")
        return

    page = int(query.data.split(":")[1])
    await _send_list_page(update, user_id, products, page, edit=True)


async def handle_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопки товара"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    product_id = int(query.data.split(":")[1])
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    # Формируем детали
    name = product['product_name'] or f"Товар #{product['product_id']}"
    regular_price = product['current_price']
    bank_price = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None

    if bank_price:
        price_text = f"💰 *Цена с Ozon Банком:* {bank_price:,.0f} ₽"
        if regular_price:
            price_text += f"\n💰 *Обычная цена:* ~~{regular_price:,.0f}~~ ₽"
    elif regular_price:
        price_text = f"💰 *Цена:* {regular_price:,.0f} ₽"
    else:
        price_text = "💰 Цена: не определена"

    # Последняя проверка
    last_check = to_msk(product['last_checked']) if product['last_checked'] else "Никогда"

    # Min/Max цены за всю историю
    min_price, max_price = db.get_price_stats(product_id)
    stats_text = ""
    if min_price is not None and max_price is not None:
        stats_text = f"\n📊 *Мин:* {min_price:,.0f} ₽  *Макс:* {max_price:,.0f} ₽"

    # Порог уведомления
    user_thresh = db.get_user_product_threshold(user_id, product_id)
    from bot import config
    eff_thresh = user_thresh if user_thresh is not None else config.price_change_threshold
    thresh_text = f"\n🔔 *Порог уведомления:* {eff_thresh}%"

    text = (
        f"📦 *{name}*\n\n"
        f"{price_text}\n"
        f"🕐 *Последняя проверка:* {last_check}"
        f"{stats_text}"
        f"{thresh_text}\n"
        f"🔗 [Открыть товар]({product['ozon_url']})"
    )

    # Кнопки действий
    keyboard = [
        [
            InlineKeyboardButton("📈 История цен", callback_data=f"detail_hist:{product_id}"),
            InlineKeyboardButton("🔍 Проверить цену", callback_data=f"detail_check:{product_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"detail_del:{product_id}"),
            InlineKeyboardButton("🔔 Порог", callback_data=f"detail_thresh:{product_id}"),
        ],
        [
            InlineKeyboardButton("◀️ Назад к списку", callback_data=f"list_back:{product_id}"),
        ],
    ]

    if getattr(query.message, "photo", None):
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete chart message before showing product detail: {e}")
        await reply_text_with_retry(query.message, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    else:
        await edit_message_with_retry(query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    set_user_state(user_id, "product_detail", {"product_id": product_id})


async def show_product_detail(update, user_id, product_id):
    """Показать детальную карточку товара (для вызова из bot.py при отмене удаления)"""
    logger.info(f"show_product_detail: user={user_id}, product={product_id}")
    query = update.callback_query if update.callback_query else None
    if not query:
        logger.error("show_product_detail: query is None!")
        return
    logger.info(f"show_product_detail: query OK, editing message")
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        if query:
            await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    name = product['product_name'] or f"Товар #{product['product_id']}"
    regular_price = product['current_price']
    bank_price = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None

    if bank_price:
        price_text = f"💰 *Цена с Ozon Банком:* {bank_price:,.0f} ₽"
        if regular_price:
            price_text += f"\n💰 *Обычная цена:* ~~{regular_price:,.0f}~~ ₽"
    elif regular_price:
        price_text = f"💰 *Цена:* {regular_price:,.0f} ₽"
    else:
        price_text = "💰 Цена: не определена"

    last_check = to_msk(product['last_checked']) if product['last_checked'] else "Никогда"

    # Min/Max цены за всю историю
    min_price, max_price = db.get_price_stats(product_id)
    stats_text = ""
    if min_price is not None and max_price is not None:
        stats_text = f"\n📊 *Мин:* {min_price:,.0f} ₽  *Макс:* {max_price:,.0f} ₽"

    # Порог уведомления
    user_thresh = db.get_user_product_threshold(user_id, product_id)
    from bot import config
    eff_thresh = user_thresh if user_thresh is not None else config.price_change_threshold
    thresh_text = f"\n🔔 *Порог уведомления:* {eff_thresh}%"

    text = (
        f"📦 *{name}*\n\n"
        f"{price_text}\n"
        f"🕐 *Последняя проверка:* {last_check}"
        f"{stats_text}"
        f"{thresh_text}\n"
        f"🔗 [Открыть товар]({product['ozon_url']})"
    )

    keyboard = [
        [
            InlineKeyboardButton("📈 История цен", callback_data=f"detail_hist:{product_id}"),
            InlineKeyboardButton("🔍 Проверить цену", callback_data=f"detail_check:{product_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"detail_del:{product_id}"),
            InlineKeyboardButton("🔔 Порог", callback_data=f"detail_thresh:{product_id}"),
        ],
        [
            InlineKeyboardButton("◀️ Назад к списку", callback_data=f"list_back:{product_id}"),
        ],
    ]

    if query:
        if getattr(query.message, "photo", None):
            try:
                await query.message.delete()
            except Exception as e:
                logger.warning(f"Failed to delete chart message before showing product detail: {e}")
            await reply_text_with_retry(query.message, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        else:
            await edit_message_with_retry(query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)


async def handle_list_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки 'Назад к списку'"""
    logger.info("handle_list_back triggered")
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    products = db.get_user_products(user_id)
    if not products:
        await edit_message_with_retry(query, "📭 Список пуст.")
        return

    # Восстанавливаем страницу из состояния
    state = get_user_state(user_id)
    page = state.get("data", {}).get("page", 0) if isinstance(state, dict) else 0

    await _send_list_page(update, user_id, products, page, edit=True)


def add_list_handlers(application):
    """Зарегистрировать все обработчики пагинации"""
    from telegram.ext import CallbackQueryHandler

    application.add_handler(CallbackQueryHandler(handle_list_page_callback, pattern=r'^list_pg:'))
    application.add_handler(CallbackQueryHandler(handle_product_detail, pattern=r'^list_prod:'))
    application.add_handler(CallbackQueryHandler(handle_list_back, pattern=r'^list_back:'))
    application.add_handler(CallbackQueryHandler(handle_detail_history, pattern=r'^detail_hist:'))
    application.add_handler(CallbackQueryHandler(handle_detail_check, pattern=r'^detail_check:'))
    application.add_handler(CallbackQueryHandler(handle_detail_delete, pattern=r'^detail_del:'))
    application.add_handler(CallbackQueryHandler(handle_detail_threshold, pattern=r'^detail_thresh:'))


async def handle_detail_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """История цен из детального просмотра"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    product_id = int(query.data.split(":")[1])
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    price_changes = db.get_price_changes(product_id, limit=15)
    full_history = db.get_price_history(product_id, limit=None, ascending=True)
    min_price, max_price = db.get_price_stats(product_id)
    name = product['product_name'] or f"Товар #{product_id}"

    # Заголовок с min/max
    stats_line = ""
    if min_price is not None and max_price is not None:
        stats_line = f"\n📊 *Мин:* {min_price:,.0f} ₽  *Макс:* {max_price:,.0f} ₽"

    chart = build_price_history_chart(name, full_history)
    if chart is None:
        text = f"📈 *История изменений: {name}*{stats_line}\n\n📭 История цен пока пуста."
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"list_prod:{product_id}")]]
        await edit_message_with_retry(query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        return

    latest_price = full_history[-1]["price"] if full_history else None
    caption = f"📈 *История цены: {name}*"
    if latest_price is not None:
        caption += f"\n💰 *Текущая:* {latest_price:,.0f} ₽"
    if min_price is not None and max_price is not None:
        caption += f"\n📊 *Мин:* {min_price:,.0f} ₽  *Макс:* {max_price:,.0f} ₽"
    caption += f"\n🔗 [Открыть товар]({product['ozon_url']})"

    if not price_changes:
        caption += "\n\n📭 Изменений не было."
    else:
        caption += "\n\n*Последние изменения:*"
        for idx, record in enumerate(price_changes[:8], 1):
            price = f"{record['price']:,.0f} ₽"
            change = ""
            if record['old_price']:
                diff = record['price'] - record['old_price']
                arrow = "📈" if diff > 0 else "📉"
                change = f" {arrow} {'+' if diff > 0 else ''}{diff:,.0f} ₽"
            time_str = to_msk(record['checked_at']) if record['checked_at'] else "?"
            line = f"\n*{idx}.* {time_str} — {price}{change}"
            if len(caption) + len(line) > 980:
                break
            caption += line

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"list_prod:{product_id}")]]
    media = InputMediaPhoto(media=chart, caption=caption, parse_mode='Markdown')
    await edit_message_media_with_retry(query, media, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_detail_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка цены из детального просмотра"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    product_id = int(query.data.split(":")[1])
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    from ozon_client import get_ozon_price
    product_name, regular_price, ozon_bank_price, request_ok = await get_ozon_price(product['ozon_url'])
    

    if ozon_bank_price is None and regular_price is None and request_ok:
        text = f"❌ Не удалось получить цену."
    else:
        new_price = ozon_bank_price or regular_price
        old_regular = product['current_price']
        old_bank = product['current_ozon_bank_price'] if 'current_ozon_bank_price' in product.keys() else None
        old_price = old_bank or old_regular

        db.update_product_price(product_id, regular_price, ozon_bank_price, product_name)

        name = product_name or product['product_name'] or f"Товар #{product_id}"
        price_detail = f"*{new_price:,.0f} ₽*"
        if ozon_bank_price and regular_price and ozon_bank_price < regular_price:
            price_detail += f"\nОбычная: {regular_price:,.0f} ₽"
        elif regular_price:
            price_detail += f"\nОбычная: {regular_price:,.0f} ₽"

        if old_price and new_price:
            diff = new_price - old_price
            percent = (diff / old_price) * 100
            arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
            text = (
                f"{arrow} *{name}*\n\n"
                f"💰 Цена: {price_detail}\n"
                f"Изменение: {'+' if diff > 0 else ''}{diff:,.0f} ₽ ({'+' if percent > 0 else ''}{percent:.1f}%)"
            )
        else:
            text = f"✅ *{name}*\n\n💰 Цена: {price_detail}"

    # Кнопка "Назад к товару"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"list_prod:{product_id}")]]

    await edit_message_with_retry(query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)


async def handle_detail_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления из детального просмотра"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    product_id = int(query.data.split(":")[1])
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    name = product['product_name'] or f"Товар #{product_id}"
    if len(name) > 50:
        name = name[:47] + "..."

    text = f"🗑 *Удалить этот товар?*\n\n{name}"

    keyboard = [
        [
            InlineKeyboardButton("✅ Да", callback_data=f"del_yes:{product_id}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"del_no:{product_id}"),
        ],
    ]

    await edit_message_with_retry(query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    set_user_state(user_id, "remove_confirm", {"product_id": product_id})


async def handle_detail_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос порога уведомления"""
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")
        # Continue anyway

    user_id = update.effective_user.id
    product_id = int(query.data.split(":")[1])
    products = db.get_user_products(user_id)
    product = next((p for p in products if p['product_id'] == product_id), None)

    if not product:
        await edit_message_with_retry(query, "❌ Товар не найден.")
        return

    # Получаем текущий порог
    threshold = db.get_user_product_threshold(user_id, product_id)
    from bot import config
    default_thresh = config.price_change_threshold
    current_str = f"{threshold}%" if threshold is not None else f"{default_thresh}% (по умолчанию)"

    name = product['product_name'] or f"Товар #{product_id}"
    if len(name) > 50:
        name = name[:47] + "..."

    text = (
        f"🔔 *Настройка порога уведомления*\n\n"
        f"{name}\n"
        f"Текущий порог: *{current_str}*\n\n"
        f"Отправь число от 0 до 100 (например, 5 для 5%).\n"
        f"0 = уведомлять при любом изменении.\n"
        f"Или /cancel для отмены."
    )

    await edit_message_with_retry(query, text, parse_mode='Markdown')
    set_user_state(user_id, STATE_THRESHOLD_ASK, {"product_id": product_id})
