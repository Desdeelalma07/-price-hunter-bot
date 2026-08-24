import os
import time
import json
import urllib.request
import urllib.parse
import urllib.error

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

FILE = "products.json"


def load_products():
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_products(products):
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def telegram(method, data=None):
    url = f"{API}/{method}"

    if data:
        data = urllib.parse.urlencode(data).encode()

    with urllib.request.urlopen(url, data=data, timeout=60) as response:
        return json.loads(response.read().decode())


def send_message(chat_id, text):
    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text
    })


def check_price(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            )
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            html = response.read().decode("utf-8", errors="ignore")

            return {
                "status": status,
                "length": len(html),
                "error": None
            }

    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "length": 0,
            "error": f"HTTP {e.code}"
        }

    except Exception as e:
        return {
            "status": None,
            "length": 0,
            "error": str(e)
        }


def main():
    print("🤖 Бот запущен")

    telegram("deleteWebhook", {
        "drop_pending_updates": False
    })

    updates = telegram("getUpdates", {
        "timeout": 1
    })

    if updates.get("result"):
        offset = updates["result"][-1]["update_id"] + 1
    else:
        offset = 0

    products = load_products()

    print(f"📦 Загружено товаров: {len(products)}")
    print("📡 Ожидаю сообщения...")

    while True:
        try:
            result = telegram("getUpdates", {
                "offset": offset,
                "timeout": 30
            })

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "").strip()

                if not chat_id or not text:
                    continue

                print(f"📩 Получено: {text}")

                if text == "/start":
                    send_message(
                        chat_id,
                        "🤖 Охотник за ценами онлайн!\n\n"
                        "/add ссылка — добавить товар\n"
                        "/list — список товаров\n"
                        "/price — проверить цену"
                    )

                elif text == "/add":
                    send_message(
                        chat_id,
                        "🔗 Отправь ссылку на товар."
                    )

                elif text.startswith("/add "):
                    link = text[5:].strip()

                    if not link.startswith(("http://", "https://")):
                        send_message(
                            chat_id,
                            "❌ После /add должна быть ссылка."
                        )
                        continue

                    if link in products:
                        send_message(
                            chat_id,
                            "⚠️ Этот товар уже есть в списке."
                        )
                        continue

                    products.append(link)
                    save_products(products)

                    send_message(
                        chat_id,
                        "✅ Товар добавлен!\n\n"
                        f"🔗 {link}\n\n"
                        f"📦 Всего товаров: {len(products)}"
                    )

                elif text == "/list":
                    if not products:
                        send_message(
                            chat_id,
                            "📭 Список пока пуст."
                        )
                    else:
                        result_text = "📦 Отслеживаемые товары:\n\n"

                        for i, product in enumerate(products, 1):
                            result_text += f"{i}. {product}\n\n"

                        send_message(chat_id, result_text)

                elif text == "/price":
                    if not products:
                        send_message(
                            chat_id,
                            "📭 Сначала добавь товар через /add."
                        )
                        continue

                    link = products[0]

                    send_message(
                        chat_id,
                        "🔎 Пытаюсь получить страницу товара..."
                    )

                    result = check_price(link)

                    if result["status"] == 200:
                        send_message(
                            chat_id,
                            "✅ Ozon отдал страницу!\n\n"
                            f"HTTP: {result['status']}\n"
                            f"Размер ответа: {result['length']} байт\n\n"
                            "Следующий этап — найти в ответе "
                            "название и цену."
                        )

                    elif result["status"]:
                        send_message(
                            chat_id,
                            "⚠️ Ozon не отдал страницу.\n\n"
                            f"HTTP: {result['status']}\n\n"
                            "Это значит, что обычный серверный "
                            "запрос заблокирован. Будем использовать "
                            "другой способ получения цены."
                        )

                    else:
                        send_message(
                            chat_id,
                            "❌ Не удалось получить страницу.\n\n"
                            f"Ошибка: {result['error']}."
                        )

                else:
                    send_message(
                        chat_id,
                        "🤔 Доступные команды:\n\n"
                        "/add ссылка\n"
                        "/list\n"
                        "/price"
                    )

        except Exception as e:
            print("❌ Ошибка:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
