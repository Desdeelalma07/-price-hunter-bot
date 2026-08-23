import os
import urllib.request
import urllib.parse

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = "781600623"

message = "🤖 Охотник за ценами запущен!"

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
data = urllib.parse.urlencode({
    "chat_id": CHAT_ID,
    "text": message
}).encode()

urllib.request.urlopen(url, data=data)

print("Сообщение отправлено!")
