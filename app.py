from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import requests
import os

app = Flask(__name__)

# 環境変数から読み込む
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")  # ← OpenWeatherMapのAPIキー

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# 天気情報取得関数
def get_weather_mit_clinic():
    lat = 35.6828
    lon = 139.7745
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}&units=metric&lang=ja"

    try:
        res = requests.get(url)
        res.raise_for_status()
        data = res.json()
        description = data["weather"][0]["description"]
        temp_max = round(data["main"]["temp_max"])
        temp_min = round(data["main"]["temp_min"])
        return f"東京MITクリニック付近の天気は{description}です。最高気温は{temp_max}℃、最低気温は{temp_min}℃です。"
    except Exception as e:
        return "天気情報が取得できませんでした。"

# メッセージハンドラー
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    if any(greet in text for greet in ["こんにちは", "おはよう", "こんばんは"]):
        reply = get_weather_mit_clinic()
    else:
        reply = "ご用件をお知らせください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

if __name__ == "__main__":
    app.run()
