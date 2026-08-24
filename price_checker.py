import aiohttp
import re
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def is_valid_sku(text: str) -> bool:
    """Проверить, является ли текст валидным артикулом Ozon.
    Артикул: только цифры, длина 7-12 символов.
    """
    return bool(re.fullmatch(r'\d{7,12}', text.strip()))


def sku_to_url(sku: str) -> str:
    """Преобразовать артикул в ссылку на товар."""
    return f"https://www.ozon.ru/product/{sku.strip()}/"


def extract_product_id(url: str) -> Optional[str]:
    """
    Извлечь ID товара из URL Ozon.
    Поддерживаемые форматы:
    - https://www.ozon.ru/product/tovar-123456789/
    - https://ozon.ru/product/123456789/
    - https://ozon.ru/t/C6Aoc4r
    - product_id=123456789
    """
    patterns = [
        r'/product/[^/]+-(\d+)/?',
        r'/product/(\d+)/?',
        r'product_id=(\d+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


async def resolve_short_url(url: str) -> str:
    """
    Разрешить короткую ссылку Ozon (ozon.ru/t/XXXXX) в полную.
    Возвращает полный URL или оригинальный если это не короткая ссылка.
    """
    if not re.match(r'https?://ozon\.ru/t/', url):
        return url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=False, timeout=10) as resp:
                location = resp.headers.get('Location')
                if location:
                    if location.startswith('//'):
                        location = 'https:' + location
                    elif not location.startswith('http'):
                        location = 'https://' + location
                    return location
    except Exception as e:
        logger.debug(f"Ошибка разрешения короткой ссылки: {e}")

    return url
