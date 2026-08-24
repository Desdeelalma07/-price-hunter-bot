import sqlite3
import os
import random
from datetime import datetime, timezone, timedelta


DB_PATH = os.path.join(os.path.dirname(__file__), 'ozon_tracker.db')


def calculate_next_check(check_interval_seconds: int) -> str:
    """
    Рассчитать время следующей проверки с случайным смещением.
    Товары будут проверяться в разное время, а не все скопом.
    Смещение: от 0 до 25% от интервала.
    """
    jitter = random.randint(0, int(check_interval_seconds * 0.25))
    next_check = datetime.now(timezone.utc) + timedelta(seconds=check_interval_seconds + jitter)
    return next_check.isoformat()


def get_connection():
    """Получить соединение с базой данных"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Инициализировать базу данных и создать таблицы"""
    conn = get_connection()
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    try:
        cursor.execute('ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1')
    except Exception:
        pass

    cursor.execute('UPDATE users SET is_active = 1 WHERE is_active IS NULL')

    # Таблица товаров (глобальная, без user_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ozon_url TEXT NOT NULL UNIQUE,
            product_name TEXT,
            current_price REAL,
            current_ozon_bank_price REAL,
            previous_price REAL,
            previous_ozon_bank_price REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_checked TIMESTAMP,
            next_check TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            is_available BOOLEAN DEFAULT 1
        )
    ''')

    # Миграция: добавляем is_available если нет
    try:
        cursor.execute('ALTER TABLE products ADD COLUMN is_available BOOLEAN DEFAULT 1')
    except Exception:
        pass

    # Таблица связей пользователь ↔ товар
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_products (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            price_threshold REAL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, product_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
        )
    ''')

    # Миграция: добавляем price_threshold если нет
    try:
        cursor.execute('ALTER TABLE user_products ADD COLUMN price_threshold REAL')
    except Exception:
        pass

    # Таблица истории цен (без ON DELETE CASCADE — история сохраняется
    # даже при удалении товара)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            price REAL NOT NULL,
            old_price REAL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        )
    ''')

    # Таблица состояний пользователей (FSM)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
    ''')

    conn.commit()
    conn.close()


# === Операции с пользователями ===

def add_user(user_id, username=None, first_name=None, last_name=None):
    """Добавить или обновить пользователя без удаления связанных данных"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    exists = cursor.fetchone()

    if exists:
        cursor.execute('''
            UPDATE users SET username = ?, first_name = ?, last_name = ?, is_active = 1
            WHERE user_id = ?
        ''', (username, first_name, last_name, user_id))
    else:
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, is_active)
            VALUES (?, ?, ?, ?, 1)
        ''', (user_id, username, first_name, last_name))

    conn.commit()
    conn.close()


def get_user(user_id):
    """Получить пользователя по ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result


def set_user_active(user_id, is_active: bool):
    """Обновить статус активности пользователя."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE users SET is_active = ? WHERE user_id = ?',
        (1 if is_active else 0, user_id)
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


# === Операции с товарами ===

def get_product_by_sku(sku: str):
    """Найти товар по SKU (product_id), извлечённому из URL.
    Ищет вхождение '-{sku}/' или '/{sku}/' в ozon_url.
    Возвращает строку товара или None.
    """
    import re
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM products')
    for row in cursor.fetchall():
        url = row['ozon_url']
        # Ищем SKU в URL: -{sku}/ или /{sku}/
        if re.search(rf'[-/]{sku}/?', url):
            conn.close()
            return row
    conn.close()
    return None


def add_product(user_id, ozon_url):
    """
    Добавить товар для отслеживания.
    Сначала проверяет по SKU (product_id из URL), затем по точному URL.
    Если товар с таким SKU уже существует — использует его.
    Если товар с таким URL уже существует у другого пользователя — создаёт связь.
    Если товар уже есть у этого пользователя — возвращается (product_id, False).
    Возвращает: (product_id, is_new) — ID товара и флаг, что он новый для этого пользователя.
    """
    import re
    from price_checker import extract_product_id

    conn = get_connection()
    cursor = conn.cursor()

    # 1. Извлекаем SKU из URL
    sku = extract_product_id(ozon_url)

    # 2. Проверяем по SKU (другие URL того же товара)
    if sku:
        existing_by_sku = get_product_by_sku(sku)
        if existing_by_sku:
            product_id = existing_by_sku['product_id']

            # Реактивируем товар, если он был деактивирован
            cursor.execute('UPDATE products SET is_active = 1 WHERE product_id = ?', (product_id,))

            # Обновляем URL на более полный (если новый URL длиннее/информативнее)
            if len(ozon_url) > len(existing_by_sku['ozon_url']):
                cursor.execute('UPDATE products SET ozon_url = ? WHERE product_id = ?', (ozon_url, product_id))

            # Проверяем связь с пользователем
            cursor.execute(
                'SELECT product_id FROM user_products WHERE user_id = ? AND product_id = ?',
                (user_id, product_id)
            )
            already_linked = cursor.fetchone()

            if already_linked:
                conn.commit()
                conn.close()
                return product_id, False

            cursor.execute('''
                INSERT OR IGNORE INTO user_products (user_id, product_id)
                VALUES (?, ?)
            ''', (user_id, product_id))
            conn.commit()
            conn.close()
            return product_id, True

    # 3. Проверяем по точному URL
    cursor.execute('SELECT product_id FROM products WHERE ozon_url = ?', (ozon_url,))
    existing = cursor.fetchone()

    if existing:
        product_id = existing['product_id']
        # Реактивируем товар, если он был деактивирован
        cursor.execute('UPDATE products SET is_active = 1 WHERE product_id = ?', (product_id,))
        cursor.execute(
            'SELECT product_id FROM user_products WHERE user_id = ? AND product_id = ?',
            (user_id, product_id)
        )
        already_linked = cursor.fetchone()

        if already_linked:
            conn.commit()
            conn.close()
            return product_id, False

        cursor.execute('''
            INSERT OR IGNORE INTO user_products (user_id, product_id)
            VALUES (?, ?)
        ''', (user_id, product_id))
    else:
        cursor.execute('''
            INSERT INTO products (ozon_url) VALUES (?)
        ''', (ozon_url,))
        product_id = cursor.lastrowid

        cursor.execute('''
            INSERT INTO user_products (user_id, product_id)
            VALUES (?, ?)
        ''', (user_id, product_id))

    conn.commit()
    conn.close()
    return product_id, True


def get_user_products(user_id):
    """Получить все товары пользователя"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, up.added_at
        FROM products p
        JOIN user_products up ON p.product_id = up.product_id
        WHERE up.user_id = ? AND p.is_active = 1
        ORDER BY up.added_at DESC
    ''', (user_id,))
    results = cursor.fetchall()
    conn.close()
    return results


def get_product(product_id):
    """Получить товар по ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM products WHERE product_id = ?', (product_id,))
    result = cursor.fetchone()
    conn.close()
    return result


def delete_product(product_id, user_id):
    """Удалить привязку товара к пользователю.
    Если товар больше никем не используется, деактивируем его.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Удаляем связь
    cursor.execute('''
        DELETE FROM user_products
        WHERE user_id = ? AND product_id = ?
    ''', (user_id, product_id))

    # Проверяем, остался ли товар у других пользователей
    cursor.execute('''
        SELECT COUNT(*) as cnt FROM user_products WHERE product_id = ?
    ''', (product_id,))
    remaining = cursor.fetchone()['cnt']

    if remaining == 0:
        # Товар никем не используется — деактивируем
        cursor.execute('''
            UPDATE products SET is_active = 0 WHERE product_id = ?
        ''', (product_id,))

    conn.commit()
    changes = cursor.rowcount > 0
    conn.close()
    return changes


def update_product_price(product_id, new_price, ozon_bank_price=None, product_name=None, check_interval=3600):
    """Обновить цену товара (обычную и/или с Ozon Банком)"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        'SELECT current_price, current_ozon_bank_price FROM products WHERE product_id = ?',
        (product_id,)
    )
    row = cursor.fetchone()
    old_price = row['current_price'] if row else None
    old_ozon_bank_price = row['current_ozon_bank_price'] if row else None

    effective_old = old_ozon_bank_price or old_price
    effective_new = ozon_bank_price or new_price

    next_check = calculate_next_check(check_interval)

    cursor.execute('''
        UPDATE products
        SET previous_price = current_price,
            previous_ozon_bank_price = current_ozon_bank_price,
            current_price = ?,
            current_ozon_bank_price = COALESCE(?, current_ozon_bank_price),
            product_name = COALESCE(?, product_name),
            last_checked = CURRENT_TIMESTAMP,
            next_check = ?
        WHERE product_id = ?
    ''', (new_price, ozon_bank_price, product_name, next_check, product_id))

    if effective_new is not None:
        cursor.execute('''
            INSERT INTO price_history (product_id, price, old_price)
            VALUES (?, ?, ?)
        ''', (product_id, effective_new, effective_old))

    conn.commit()
    conn.close()

    return old_price, new_price, old_ozon_bank_price, ozon_bank_price


def update_product_availability(product_id, is_available, check_interval=3600):
    """Обновить статус доступности товара и время проверки.
    Возвращает кортеж (was_available, is_available) для определения изменений.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Получаем текущий статус
    cursor.execute('SELECT is_available FROM products WHERE product_id = ?', (product_id,))
    row = cursor.fetchone()
    was_available = bool(row['is_available']) if row else True

    # Обновляем статус
    next_check = calculate_next_check(check_interval)
    cursor.execute('''
        UPDATE products
        SET is_available = ?,
            last_checked = CURRENT_TIMESTAMP,
            next_check = ?
        WHERE product_id = ?
    ''', (1 if is_available else 0, next_check, product_id))
    conn.commit()
    conn.close()

    return was_available, is_available


def get_due_products(check_interval_seconds: int):
    """Получить товары, у которых пришло время проверки.
    Возвращает товары с информацией о пользователях, которые их отслеживают.
    """
    conn = get_connection()
    cursor = conn.cursor()
    threshold = int(datetime.now(timezone.utc).timestamp()) - check_interval_seconds
    cursor.execute('''
        SELECT p.*, GROUP_CONCAT(DISTINCT u.user_id) as user_ids
        FROM products p
        JOIN user_products up ON p.product_id = up.product_id
        JOIN users u ON u.user_id = up.user_id
        WHERE p.is_active = 1
          AND u.is_active = 1
          AND p.last_checked IS NOT NULL
          AND CAST(strftime('%s', p.last_checked) AS INTEGER) <= ?
        GROUP BY p.product_id
    ''', (threshold,))
    results = cursor.fetchall()
    conn.close()
    return results


def get_user_product_for_notification(product_id):
    """Получить товар и всех пользователей, которые его отслеживают.
    Для фоновой проверки цен и уведомлений.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, u.user_id, u.username, u.first_name, up.price_threshold
        FROM products p
        JOIN user_products up ON p.product_id = up.product_id
        JOIN users u ON u.user_id = up.user_id
        WHERE p.product_id = ? AND p.is_active = 1 AND u.is_active = 1
    ''', (product_id,))
    results = cursor.fetchall()
    conn.close()
    return results


def get_user_product_threshold(user_id, product_id):
    """Получить порог уведомления для конкретной связи пользователь-товар.
    Если не установлен — возвращает None (использовать конфиг).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT price_threshold FROM user_products WHERE user_id = ? AND product_id = ?',
        (user_id, product_id)
    )
    row = cursor.fetchone()
    conn.close()
    if row and row['price_threshold'] is not None:
        return row['price_threshold']
    return None


def set_user_product_threshold(user_id, product_id, threshold):
    """Установить порог уведомления для связи пользователь-товар."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE user_products SET price_threshold = ?
        WHERE user_id = ? AND product_id = ?
    ''', (threshold, user_id, product_id))
    conn.commit()
    conn.close()


def get_active_products():
    """Получить все активные товары с пользователями (для /checkall)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, u.user_id, u.username, u.first_name
        FROM products p
        JOIN user_products up ON p.product_id = up.product_id
        JOIN users u ON u.user_id = up.user_id
        WHERE p.is_active = 1 AND u.is_active = 1
    ''')
    results = cursor.fetchall()
    conn.close()
    return results


# === Операции с историей цен ===

def get_price_history(product_id, limit=10, ascending=False):
    """Получить записи истории цен для товара."""
    conn = get_connection()
    cursor = conn.cursor()
    order = 'ASC' if ascending else 'DESC'
    if limit is None:
        cursor.execute(f'''
            SELECT * FROM price_history
            WHERE product_id = ?
            ORDER BY checked_at {order}
        ''', (product_id,))
    else:
        cursor.execute(f'''
            SELECT * FROM price_history
            WHERE product_id = ?
            ORDER BY checked_at {order}
            LIMIT ?
        ''', (product_id, limit))
        results = cursor.fetchall()
        conn.close()
        return results

    results = cursor.fetchall()
    conn.close()
    return results


def get_price_changes(product_id, limit=10):
    """Получить только изменения цен (где price != old_price)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM price_history
        WHERE product_id = ? AND old_price IS NOT NULL AND price != old_price
        ORDER BY checked_at DESC
        LIMIT ?
    ''', (product_id, limit))
    results = cursor.fetchall()
    conn.close()
    return results


def get_price_stats(product_id):
    """Получить минимальную и максимальную цену за всю историю"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT MIN(price) as min_price, MAX(price) as max_price
        FROM price_history
        WHERE product_id = ?
    ''', (product_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row['min_price'], row['max_price']
    return None, None


# === Операции с состояниями пользователей (FSM) ===

def get_user_state(user_id: int) -> dict:
    """Получить состояние пользователя из БД"""
    import json
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT state, data FROM user_states WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        try:
            data = json.loads(row['data']) if row['data'] else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        return {"state": row['state'], "data": data}
    return {}


def set_user_state(user_id: int, state: str, data: dict = None):
    """Установить состояние пользователя в БД"""
    import json
    conn = get_connection()
    cursor = conn.cursor()
    data_json = json.dumps(data, ensure_ascii=False) if data else None
    cursor.execute('''
        INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, state, data_json))
    conn.commit()
    conn.close()


def clear_user_state(user_id: int):
    """Очистить состояние пользователя в БД"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()


def get_users_count():
    """Получить количество пользователей"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_products_count():
    """Получить общее количество товаров"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM products')
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_active_products_count():
    """Получить количество активных товаров с хотя бы одним активным подписчиком."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(DISTINCT up.product_id)
        FROM user_products up
        JOIN products p ON up.product_id = p.product_id
        JOIN users u ON up.user_id = u.user_id
        WHERE p.is_available = 1
          AND p.is_active = 1
          AND u.is_active = 1
    ''')
    count = cursor.fetchone()[0]
    conn.close()
    return count
