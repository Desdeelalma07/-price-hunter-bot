import os
import time
import json
import urllib.request
import urllib.parse

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

CHAT_ID = "781600623"


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

    # Удаляем старый webhook
    telegram("deleteWebhook", {
        "drop_pending_updates": False
    })

    # Получаем последние сообщения
    updates = telegram("getUpdates", {
        "timeout": 1
    })

    if updates.get("result"):
        offset = updates["result"][-1]["update_id"] + 1
    else:
        offset = 0

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
                        "Используй:\n"
                        "/add — добавить товар"
                    )

                elif text == "/add":
                    send_message(
                        chat_id,
                        "🔗 Отлично!\n\n"
                        "Теперь отправь ссылку на товар "
                        "следующим сообщением."
                    )

                elif text.startswith("http://") or text.startswith("https://"):
                    send_message(
                        chat_id,
                        "🔎 Ссылка получена!\n\n"
                        f"{text}\n\n"
                        "Следующий этап — научить меня "
                        "получать цену этого товара."
                    )

                else:
                    send_message(
                        chat_id,
                        "🤔 Я пока понимаю только:\n\n"
                        "/start\n"
                        "/add\n\n"
                        "И ссылки на товары."
                    )

        except Exception as e:
            print("❌ Ошибка:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
