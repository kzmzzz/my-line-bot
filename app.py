from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage
)
import requests
import os
import json

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ユーザーごとの状態保持
user_states = {}

# アンケート質問
questions = [
    {"question": "今の体調は？", "options": ["良い", "悪い", "わからない"]},
    {"question": "咳は出ますか？", "options": ["はい", "いいえ"]},
    {"question": "熱はありますか？", "options": ["高熱", "微熱", "平熱"]}
]

# Flex Message 生成
def create_question_bubble(index):
    q = questions[index]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"Q{index+1}. {q['question']}", "wrap": True, "weight": "bold", "size": "md"},
                *[
                    {
                        "type": "button",
                        "style": "primary",
                        "margin": "sm",
                        "action": {
                            "type": "postback",
                            "label": option,
                            "data": json.dumps({"index": index, "answer": option}),
                            "displayText": option
                        }
                    } for option in q["options"]
                ]
            ]
        }
    }

# 天気情報取得
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

# Webhook受信
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# テキストメッセージ処理
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    greetings = ["こんにちは", "こんにちわ", "おはよう", "おはようございます", "こんばんわ", "こんばんは"]

    if "症状チェック" in text:
        user_states[user_id] = {"answers": [], "step": 0}
        question = create_question_bubble(0)
        message = FlexSendMessage(alt_text="症状チェック Q1", contents=question)
        line_bot_api.reply_message(event.reply_token, message)
        return

    elif any(greet in text for greet in greetings):
        try:
            profile = line_bot_api.get_profile(user_id)
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

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="「症状チェック」またはご挨拶を送ってください。")
        )

# アンケート回答処理
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = json.loads(event.postback.data)
    index = data["index"]
    answer = data["answer"]

    if user_id not in user_states:
        user_states[user_id] = {"answers": [], "step": 0}

    user_states[user_id]["answers"].append(f"Q{index+1}: {answer}")
    user_states[user_id]["step"] += 1

    step = user_states[user_id]["step"]

    if step < len(questions):
        next_question = create_question_bubble(step)
        message = FlexSendMessage(alt_text=f"症状チェック Q{step+1}", contents=next_question)
        line_bot_api.reply_message(event.reply_token, message)
    else:
        result = "\n".join(user_states[user_id]["answers"])
        result += "\n\n✅ オンライン診療はこちらから:\nhttps://your-clinic-url.com/"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        del user_states[user_id]  # 回答リセット

if __name__ == "__main__":
    app.run()
