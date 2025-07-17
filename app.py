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
from datetime import datetime

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

user_states = {}
completed_users = set()

def get_next_question(state):
    steps = [
        "prefecture", "name", "phone", "birthday",
        "gender", "height", "weight"
    ]
    for step in steps:
        if step not in state:
            return step
    return None

def calculate_age(birthdate_str):
    try:
        birthdate = datetime.strptime(birthdate_str, "%Y/%m/%d")
        today = datetime.today()
        age = today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))
        return age
    except:
        return None

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    if user_id in completed_users:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="すでに問診にご回答いただいています。ありがとうございました。"))
        return

    if text == "問診":
        user_states[user_id] = {}
        reply = "お住まいの都道府県を教えてください。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    step = get_next_question(state)

    if step == "prefecture":
        if text:
            state["prefecture"] = text
            reply = "保険証と同じ漢字のフルネームでお名前を教えてください。"
        else:
            reply = "都道府県名を入力してください。"

    elif step == "name":
        if text:
            state["name"] = text
            reply = "電話番号をハイフンなしで入力してください。"
        else:
            reply = "お名前を入力してください。"

    elif step == "phone":
        if text.isdigit() and len(text) == 11:
            state["phone"] = text
            reply = "生年月日を yyyy/mm/dd の形式で入力してください。"
        else:
            reply = "電話番号は11桁の数字で入力してください。例：09012345678"

    elif step == "birthday":
        age = calculate_age(text)
        if age is None:
            reply = "正しい生年月日形式（yyyy/mm/dd）で入力してください。"
        else:
            state["birthday"] = text
            state["age"] = age
            buttons = [
                {"label": "女", "data": "gender_female"},
                {"label": "男", "data": "gender_male"}
            ]
            send_buttons(event.reply_token, "性別を選択してください。", buttons)
            return

    elif step == "height":
        if text.isdigit():
            state["height"] = text
            reply = "体重を数字（kg）で入力してください。"
        else:
            reply = "身長は数字（cm）で入力してください。"

    elif step == "weight":
        if text.isdigit():
            state["weight"] = text
            reply = "✅ ご回答ありがとうございました。"
            completed_users.add(user_id)
            del user_states[user_id]
        else:
            reply = "体重は数字（kg）で入力してください。"

    else:
        reply = "次の入力をお願いします。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    state = user_states.setdefault(user_id, {})
    data = event.postback.data

    if user_id in completed_users:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="すでに問診にご回答いただいています。"))
        return

    if data.startswith("gender_"):
        state["gender"] = "女" if data == "gender_female" else "男"
        reply = "身長を数字（cm）で入力してください。"

    else:
        reply = "次の入力をお願いします。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def send_buttons(reply_token, text, buttons):
    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": text, "wrap": True, "weight": "bold", "size": "md"},
                *[
                    {
                        "type": "button",
                        "style": "primary",
                        "margin": "sm",
                        "action": {
                            "type": "postback",
                            "label": b["label"],
                            "data": b["data"],
                            "displayText": b["label"]
                        }
                    } for b in buttons
                ]
            ]
        }
    }
    message = FlexSendMessage(alt_text=text, contents=contents)
    line_bot_api.reply_message(reply_token, message)

if __name__ == "__main__":
    app.run()
