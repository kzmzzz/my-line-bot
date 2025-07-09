from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
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

# Flex Message（症状アンケート）
def create_symptom_survey():
    contents = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "症状チェック", "weight": "bold", "size": "lg"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "現在の症状を選んでください：", "wrap": True},
                {
                    "type": "button",
                    "action": {"type": "message", "label": "咳が出る", "text": "咳が出る"},
                    "style": "secondary"
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "熱がある", "text": "熱がある"},
                    "style": "secondary"
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "喉が痛い", "text": "喉が痛い"},
                    "style": "secondary"
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="症状アンケートです", contents=contents)

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
    greetings = ["こんにちは", "こんにちわ", "おはよう", "おはようございます", "こんばんわ", "こんばんは"]

    # アンケート送信トリガー
    if "アンケート" in text or "あんけーと" in text:
        line_bot_api.reply_message(
            event.reply_token,
            create_symptom_survey()
        )
        return

    # あいさつ
    if any(greet in text for greet in greetings):
        try:
            profile = line_bot_api.get_profile(event.source.user_id)
            display_name = profile.display_name
        except Exception as e:
            print("DEBUG: プロフィール取得に失敗:", e)
            display_name = "お客さま"

        weather, temp_max, temp_min = get_weather_forecast()
        print("DEBUG:", weather, temp_max, temp_min)

        if weather:
            reply_text = (
                f"{text}、{display_name}さん。\n"
                f"東京MITクリニック付近の天気は「{weather}」です。\n"
                f"最高気温は {temp_max}℃、最低気温は {temp_min}℃ です。"
            )
        else:
            reply_text = f"{display_name}さん、天気情報の取得に失敗しました。"
    else:
        reply_text = "ご連絡ありがとうございます。ご用件をお知らせください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run()
