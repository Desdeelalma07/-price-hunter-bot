"""Клиент для получения цен с Ozon."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from urllib.parse import quote

from curl_cffi import requests

from price_checker import extract_product_id, resolve_short_url

logger = logging.getLogger(__name__)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "ozon_cookies.json")
COOKIE_TTL_HOURS = 11

class OzonClient:
    def __init__(self):
        self._cookies: Dict[str, str] = {}
        self._cookies_loaded_at: Optional[datetime] = None
        self._load_cookies_from_file()

    def _load_cookies_from_file(self):
        if not os.path.exists(COOKIES_FILE):
            return

        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cookies = data.get("cookies", {})
            loaded_at_str = data.get("loaded_at")
            if loaded_at_str:
                self._cookies_loaded_at = datetime.fromisoformat(loaded_at_str)
            logger.info(
                f"Cookies загружены из файла: {len(self._cookies)} шт, "
                f"время: {self._cookies_loaded_at}"
            )
        except Exception as e:
            logger.error(f"Ошибка загрузки cookies: {e}")

    def _save_cookies_to_file(self):
        data = {
            "cookies": self._cookies,
            "loaded_at": datetime.now().isoformat(),
        }
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Cookies сохранены в файл: {COOKIES_FILE}")

    def _build_api_headers(self, product_url: str) -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Referer": product_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "x-o3-app-name": "dweb_client",
        }

    def _cookies_expired(self) -> bool:
        if not self._cookies_loaded_at:
            return True
        return datetime.now() - self._cookies_loaded_at > timedelta(hours=COOKIE_TTL_HOURS)

    async def _solve_captcha(self, page_url: str = "https://www.ozon.ru/") -> bool:
        from bot import config

        if config.use_test_cookie_method:
            if self._solve_captcha_selenium():
                return True
            logger.warning("Selenium-метод не получил cookies, пробую Playwright...")

        return await self._solve_captcha_playwright(page_url)

    def _solve_captcha_selenium(self) -> bool:
        logger.info("Запуск Selenium для тестового получения cookies...")
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from selenium_stealth import stealth

            chrome_options = Options()
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option("useAutomationExtension", False)
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-plugins")
            chrome_options.add_argument("--window-size=1920,1080")

            driver = webdriver.Chrome(service=Service(), options=chrome_options)
            stealth(
                driver,
                languages=["ru-RU", "ru"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )
            driver.set_page_load_timeout(60)
            driver.implicitly_wait(20)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            try:
                target_url = "https://www.ozon.ru/"
                logger.info(f"Открываю {target_url} через Selenium...")
                driver.get(target_url)

                logger.info("Жду получения cookies через Selenium (до 60 сек)...")
                for i in range(60, 0, -1):
                    title = ""
                    try:
                        title = driver.title or ""
                    except Exception:
                        pass

                    page_source = ""
                    try:
                        page_source = (driver.page_source or "").lower()
                    except Exception:
                        pass

                    blocked = (
                        "antibot" in title.lower()
                        or "ограничен" in title.lower()
                        or "challenge" in page_source
                        or "доступ ограничен" in page_source
                    )
                    cookies = driver.get_cookies()
                    has_cookies = bool(cookies)

                    if has_cookies and not blocked:
                        self._cookies = {cookie["name"]: cookie["value"] for cookie in cookies}
                        self._cookies_loaded_at = datetime.now()
                        self._save_cookies_to_file()
                        logger.info(f"Selenium: получено {len(self._cookies)} cookies")
                        return True

                    if i % 10 == 0:
                        logger.info(f"  Selenium: осталось {i} сек...")
                    time.sleep(1)

                logger.error("Selenium: не удалось получить cookies за 60 секунд")
                return False
            finally:
                driver.quit()
        except Exception as e:
            logger.error(f"Selenium: ошибка — {e}")
            return False

    async def _solve_captcha_playwright(self, page_url: str = "https://www.ozon.ru/") -> bool:
        logger.info("Запуск Playwright для решения капчи...")
        from playwright.async_api import async_playwright

        cookies_dict = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = await context.new_page()

            test_url = "https://www.ozon.ru/"
            logger.info(f"Открываю {test_url}...")
            await page.goto(test_url, wait_until="domcontentloaded", timeout=30000)

            logger.info("Жду решения капчи (до 60 сек)...")
            for i in range(60, 0, -1):
                try:
                    title = await page.title()
                except Exception:
                    await asyncio.sleep(1)
                    continue

                if "Antibot" not in title and "ограничен" not in title:
                    logger.info(f"Капча решена! Заголовок: {title[:60]}")
                    break
                if i % 10 == 0:
                    logger.info(f"  осталось {i} сек...")
                await asyncio.sleep(1)
            else:
                logger.error("Капча не решена за 60 секунд")
                await browser.close()
                return False

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(5)

            cookies = await context.cookies()
            cookies_dict = {c["name"]: c["value"] for c in cookies}
            logger.info(f"Получено {len(cookies_dict)} cookies")
            await browser.close()

        self._cookies = cookies_dict
        self._cookies_loaded_at = datetime.now()
        self._save_cookies_to_file()
        return True

    def _build_api_url(self, product_url: str) -> Optional[str]:
        if "ozon.ru/" not in product_url:
            return None

        path = "/" + product_url.split("ozon.ru/", 1)[1]
        return f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url={quote(path, safe='')}"

    def _make_request(self, url: str, cookies: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        api_url = self._build_api_url(url)
        if not api_url:
            logger.error(f"Не удалось построить API URL для товара: {url}")
            return 0, ""

        try:
            resp = requests.get(
                api_url,
                impersonate="chrome124",
                headers=self._build_api_headers(url),
                cookies=cookies if cookies is not None else self._cookies,
                timeout=20,
                allow_redirects=True,
            )
            return resp.status_code, resp.text
        except Exception as e:
            logger.error(f"Ошибка HTTP-запроса: {e}")
            return 0, ""

    def _is_captcha_response(self, status: int, body: str) -> bool:
        if status == 403:
            return True
        if not body:
            return False
        lower = body.lower()
        return (
            '"challengeurl"' in lower
            or '"incidentid"' in lower
            or "challenge.html" in lower
        )

    def _parse_price_text(self, text: str) -> Optional[float]:
        import re

        if not text:
            return None
        digits_only = re.sub(r"\D", "", text)
        if not digits_only:
            return None
        value = int(digits_only)
        return float(value) if value > 0 else None

    def _fix_mojibake_text(self, text: Optional[str]) -> Optional[str]:
        if not text or not isinstance(text, str):
            return text

        if all(marker not in text for marker in ("Ð", "Ñ", "â")):
            return text

        for source_encoding in ("latin1", "cp1252"):
            try:
                fixed = text.encode(source_encoding).decode("utf-8")
            except Exception:
                continue
            if fixed:
                return fixed.replace("\xa0", " ").replace("&nbsp;", " ")

        return text.replace("\xa0", " ").replace("&nbsp;", " ")

    def _extract_product_id_from_page_url(self, page_url: Optional[str]) -> Optional[str]:
        if not page_url:
            return None
        return extract_product_id(page_url)

    def _parse_widget_states(self, body: str) -> Optional[Dict[str, Dict]]:
        try:
            data = json.loads(body)
        except Exception as e:
            logger.warning(f"Не удалось разобрать JSON ответ Ozon: {e}")
            return None

        widget_states = data.get("widgetStates")
        if not isinstance(widget_states, dict):
            return None

        parsed_states: Dict[str, Dict] = {}
        for key, raw in widget_states.items():
            if not isinstance(raw, str):
                continue
            try:
                parsed_states[key] = json.loads(raw)
            except Exception:
                continue

        return parsed_states

    def _extract_product_name_from_states(self, states: Dict[str, Dict]) -> Optional[str]:
        for key, state in states.items():
            if not key.startswith("webProductHeading-"):
                continue
            title = state.get("title")
            title = self._fix_mojibake_text(title)
            if title:
                return title.strip()
        return None

    def _extract_price_state(
        self,
        states: Dict[str, Dict],
        expected_product_id: Optional[str],
    ) -> Optional[Dict]:
        for key, state in states.items():
            if not key.startswith("webPrice-"):
                continue
            if not isinstance(state, dict):
                continue

            if not {"isAvailable", "price", "cardPrice", "link"}.issubset(state.keys()):
                continue

            link = state.get("link")
            if not isinstance(link, str) or "product_id=" not in link:
                continue

            link_product_id = link.split("product_id=", 1)[1].split("&", 1)[0]
            if expected_product_id and link_product_id != expected_product_id:
                continue

            regular_price = self._parse_price_text(self._fix_mojibake_text(state.get("price", "")))
            ozon_bank_price = self._parse_price_text(self._fix_mojibake_text(state.get("cardPrice", "")))
            if regular_price is None and ozon_bank_price is None:
                continue

            lexemes = state.get("lexemes", {})
            if not isinstance(lexemes, dict):
                continue

            if not any(key in lexemes for key in ("withOzonCard", "withoutOzonCard")):
                continue

            return state

        return None

    def _is_removed_product_response(
        self,
        status: int,
        states: Dict[str, Dict],
    ) -> bool:
        if status != 404:
            return False

        if any(key.startswith("webPrice-") for key in states):
            return False

        return any(key.startswith("statusWidget-") for key in states)

    def parse_price_from_json(
        self,
        body: str,
        page_url: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        states = self._parse_widget_states(body)
        if not states:
            return None, None, None

        expected_product_id = self._extract_product_id_from_page_url(page_url)
        name = self._extract_product_name_from_states(states)
        price_state = self._extract_price_state(states, expected_product_id=expected_product_id)

        if not price_state:
            return name, None, None

        regular_price = self._parse_price_text(self._fix_mojibake_text(price_state.get("price", "")))
        ozon_bank_price = self._parse_price_text(self._fix_mojibake_text(price_state.get("cardPrice", "")))
        return name, regular_price, ozon_bank_price

    async def _get_product_price_json(
        self,
        url: str,
        max_retries: int = 1,
    ) -> Tuple[Optional[str], Optional[float], Optional[float], bool]:
        """
        Получение цены через JSON endpoint Ozon.
        На вход принимает обычный product URL, а внутри преобразует его в endpoint `...page/json/v2?url=...`.
        """
        if self._cookies_expired():
            logger.info("JSON-режим: cookies истекли, запускаю эмуляцию браузера...")
            if not await self._solve_captcha(url):
                return None, None, None, False

        for attempt in range(max_retries + 1):
            status, body = self._make_request(url)
            logger.info(f"JSON-запрос к Ozon: статус {status}, размер {len(body)} байт")

            if self._is_captcha_response(status, body):
                logger.warning(f"JSON-метод наткнулся на капчу, попытка {attempt + 1}/{max_retries + 1}")
                if attempt < max_retries:
                    logger.warning("Перехожу к эмуляции браузера: сначала Selenium, затем Playwright при неудаче")
                    if not await self._solve_captcha(url):
                        return None, None, None, False
                    continue
                logger.error("Не удалось пройти капчу JSON-методом")
                return None, None, None, False

            states = self._parse_widget_states(body)
            if not states:
                logger.warning(f"JSON-метод получил неожиданный ответ: статус={status}, размер={len(body)}")
                return None, None, None, False

            if self._is_removed_product_response(status, states):
                logger.info("JSON endpoint вернул 404 и распознан как снятый с продажи товар")
                name, regular_price, ozon_bank_price = self.parse_price_from_json(body, page_url=url)
                return name, regular_price, ozon_bank_price, True

            if status == 200:
                name, regular_price, ozon_bank_price = self.parse_price_from_json(body, page_url=url)
                logger.info(
                    f"Цена получена через JSON endpoint: {name or 'без названия'}, "
                    f"обычная={regular_price}, Ozon Банк={ozon_bank_price}"
                )
                return name, regular_price, ozon_bank_price, True

            logger.warning(f"JSON-метод получил неожиданный ответ: статус={status}, размер={len(body)}")
            return None, None, None, False

        return None, None, None, False

    async def get_product_price(
        self,
        url: str,
        max_retries: int = 1,
    ) -> Tuple[Optional[str], Optional[float], Optional[float], bool]:
        """
        Возвращает: (название, обычная_цена, цена_ozon_банк, request_ok)

        request_ok=True — запрос выполнен; цена может быть None, если товар снят
        request_ok=False — ошибка сети/капчи; статус товара не менять
        """
        url = await resolve_short_url(url)
        logger.info("Использую JSON endpoint для получения цены")
        return await self._get_product_price_json(url, max_retries=max_retries)


client = OzonClient()


async def get_ozon_price(url: str) -> Tuple[Optional[str], Optional[float], Optional[float], bool]:
    return await client.get_product_price(url)

