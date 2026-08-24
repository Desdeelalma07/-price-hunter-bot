"""
Парсинг товаров из корзины Ozon (shared cart).
"""

import json
import re
import logging
from typing import List, Dict, Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)

# Загружаем куки из файла
def _load_cookies() -> Dict[str, str]:
    try:
        with open("ozon_cookies.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("cookies", {})
    except FileNotFoundError:
        logger.warning("ozon_cookies.json не найден")
        return {}


def _parse_price_text(text: str) -> Optional[int]:
    """Извлечь число из текста типа '4 796 ₽' или '19 179 ₽'"""
    if not text:
        return None
    digits = re.sub(r'\D', '', text)
    return int(digits) if digits else None


def extract_share_code(url: str) -> Optional[str]:
    """Извлечь код общей корзины из ссылки.
    Примеры:
    - https://www.ozon.ru/cart?share=hogp7Uk → hogp7Uk
    - https://ozon.ru/cart?share=ABC123 → ABC123
    """
    match = re.search(r'share=([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None


def get_cart_products(share_code: str) -> List[Dict]:
    """
    Получить список товаров из общей корзии Ozon.
    Возвращает список словарей:
    [
        {
            "product_id": "123456789",
            "name": "Название товара",
            "regular_price": 1000,
            "ozon_bank_price": 900,
            "url": "https://www.ozon.ru/product/..."
        },
        ...
    ]
    """
    cookies = _load_cookies()
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    api_url = (
        f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
        f"?url=%2Fmodal%2FshareCartFrom%3Fshare%3D{share_code}%26page_changed%3Dtrue"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "Referer": f"https://www.ozon.ru/cart?share={share_code}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "x-o3-app-name": "dweb_client",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        resp = requests.get(
            api_url,
            impersonate="chrome124",
            headers=headers,
            timeout=20,
        )

        if resp.status_code != 200:
            logger.error(f"API корзины вернул статус {resp.status_code}")
            return []

        data = resp.json()
    except Exception as e:
        logger.error(f"Ошибка запроса к API корзины: {e}")
        return []

    # Находим widgetStates.sharingCart-*
    widget_states = data.get("widgetStates", {})
    cart_data = None

    for key in widget_states:
        if key.startswith("sharingCart-"):
            try:
                cart_data = json.loads(widget_states[key])
                break
            except json.JSONDecodeError:
                continue

    if not cart_data:
        logger.error("Блок sharingCart не найден в ответе API")
        return []

    items = cart_data.get("items", [])
    products = []

    for group in items:
        group_products = group.get("products", [])

        for prod in group_products:
            prod_id = str(prod.get("id", ""))
            link = prod.get("link", "")

            if not prod_id:
                continue

            # Название из titleColumn
            name = ""
            for tc in prod.get("titleColumn", []):
                if tc.get("type") == "actionText":
                    action_text = tc.get("actionText", {})
                    text_obj = action_text.get("text", {})
                    name = text_obj.get("text", "")
                    break

            # Цены из priceColumn
            regular_price = None
            ozon_bank_price = None

            for pc in prod.get("priceColumn", []):
                if pc.get("type") != "priceList":
                    continue

                price_list = pc.get("priceList", {}).get("list", [])
                for pl in price_list:
                    price_texts = pl.get("price", [])
                    style_type = pl.get("priceStyle", {}).get("styleType", "")

                    for pt in price_texts:
                        if pt.get("textStyle") == "PRICE":
                            price_val = _parse_price_text(pt.get("text", ""))
                            if price_val:
                                if "CARD" in style_type:
                                    ozon_bank_price = price_val
                                elif "SECOND" in style_type:
                                    regular_price = price_val

            url = f"https://www.ozon.ru{link}" if link.startswith("/") else link

            products.append({
                "product_id": prod_id,
                "name": name,
                "regular_price": regular_price,
                "ozon_bank_price": ozon_bank_price,
                "url": url,
            })

    logger.info(f"Из корзины получено {len(products)} товаров")
    return products
