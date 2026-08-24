import os
import time
import json
import urllib.request
import urllib.parse

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
                        "/add — добавить товар\n"
                        "/add ссылка — добавить товар сразу\n"
                        "/list — показать товары"
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
                            "📭 Список пока пуст.\n\n"
                            "Добавь товар командой:\n"
                            "/add ссылка"
                        )
                    else:
                        result_text = "📦 Отслеживаемые товары:\n\n"

                        for i, product in enumerate(products, 1):
                            result_text += f"{i}. {product}\n\n"

                        send_message(chat_id, result_text)

                elif text.startswith("http://") or text.startswith("https://"):
                    send_message(
                        chat_id,
                        "🔎 Ссылка получена.\n\n"
                        "Чтобы добавить товар в мониторинг, "
                        "используй:\n\n"
                        f"/add {text}"
                    )

                else:
                    send_message(
                        chat_id,
                        "🤔 Я понимаю:\n\n"
                        "/add\n"
                        "/add ссылка\n"
                        "/list"
                    )

        except Exception as e:
            print("❌ Ошибка:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
