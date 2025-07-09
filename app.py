from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import requests
import os

app = Flask(__name__)

# LINE Botの設定（Renderの環境変数から取得）
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 天気情報取得関数（OpenWeatherMap使用）
def get_weather_forecast():
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        print("DEBUG: APIキーが取得できませんでした")
        return None, None, None

    city = "Tokyo,jp"
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&lang=ja&units=metric"

    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print("DEBUG: Request failed:", e)
        return None, None, None

    data = response.json()
    weather = data["weather"][0]["description"]
    temp_max = data["main"]["temp_max"]
    temp_min = data["main"]["temp_min"]
    return weather, temp_max, temp_min

# テスト用エンドポイント
@app.route("/test_api")
def test_api():
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        return "APIキーが取得できませんでした", 500

    print("DEBUG: APIキー取得成功 →", api_key[:5] + "..." + api_key[-5:])
    city = "Tokyo,jp"
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&lang=ja&units=metric"

    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print("DEBUG: リクエスト失敗", e)
        return f"リクエストエラー: {e}", 500

    return "天気情報取得成功", 200

# LINE Webhookエンドポイント
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# メッセージイベント処理
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text

    if "こんにちは" in text:
        weather, temp_max, temp_min = get_weather_forecast()
        print("DEBUG:", weather, temp_max, temp_min)

        if weather:
            reply_text = (
                f"東京MITクリニック付近の天気は「{weather}」です。\n"
                f"最高気温は {temp_max}℃、最低気温は {temp_min}℃ です。"
            )
        else:
            reply_text = "天気情報の取得に失敗しました。"
    else:
        reply_text = f"「{text}」って言ったね！"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run()
