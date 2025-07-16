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

def get_next_question(state):
    steps = [
        "prefecture", "name", "phone", "birthday",
        "gender", "height", "weight", "illness", "illness_detail",
        "medication", "medication_detail", "allergy", "allergy_detail",
        "confirm_answers", "confirm_self"
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

    # 症状チェック開始
    if text == "症状チェック":
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
            reply = f"確認：あなたの満年齢は {age} 歳です。\n次に、性別を教えてください。"
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
            reply = "現在、治療中または通院中の病気はありますか？（はい／いいえ）"
            return send_yes_no(event.reply_token, "illness")
        else:
            reply = "体重は数字（kg）で入力してください。"

    elif step == "illness_detail":
        if text:
            state["illness_detail"] = text
            reply = "現在、おくすりを服用していますか？（はい／いいえ）"
            return send_yes_no(event.reply_token, "medication")
        else:
            reply = "病名または治療内容を入力してください。"

    elif step == "medication_detail":
        if text:
            state["medication_detail"] = text
            reply = "アレルギーはありますか？（はい／いいえ）"
            return send_yes_no(event.reply_token, "allergy")
        else:
            reply = "服用中のお薬の名前を入力してください。"

    elif step == "allergy_detail":
        if text:
            state["allergy_detail"] = text
            return show_confirmation(event.reply_token, state)
        else:
            reply = "アレルギー名を入力してください。"

    else:
        reply = "次の入力をお願いします。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    state = user_states.setdefault(user_id, {})
    data = event.postback.data

    if data.startswith("gender_"):
        state["gender"] = "女" if data == "gender_female" else "男"
        reply = "身長を数字（cm）で入力してください。"

    elif data.startswith("yesno_"):
        key, value = data[7:].split("_")
        state[key] = value
        if key == "illness" and value == "yes":
            reply = "病気の名称や治療内容を教えてください。"
        elif key == "illness" and value == "no":
            reply = "現在、おくすりを服用していますか？（はい／いいえ）"
            return send_yes_no(event.reply_token, "medication")
        elif key == "medication" and value == "yes":
            reply = "お薬の名前をすべてお伝えください。"
        elif key == "medication" and value == "no":
            reply = "アレルギーはありますか？（はい／いいえ）"
            return send_yes_no(event.reply_token, "allergy")
        elif key == "allergy" and value == "yes":
            reply = "アレルギー名を教えてください。"
        elif key == "allergy" and value == "no":
            return show_confirmation(event.reply_token, state)

    elif data == "confirm_ok":
        reply = "問診票に記入したのはご本人さまですか？\n（ご本人以外は申し込みできません）"
        buttons = [{"label": "はい。承知しました", "data": "self_confirmed"}]
        return send_buttons(event.reply_token, reply, buttons)

    elif data == "self_confirmed":
        reply = "✅ ご回答ありがとうございました。"
        del user_states[user_id]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

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

def send_yes_no(reply_token, key):
    buttons = [
        {"label": "はい", "data": f"yesno_{key}_yes"},
        {"label": "いいえ", "data": f"yesno_{key}_no"}
    ]
    send_buttons(reply_token, f"{key.capitalize()}についてお答えください。", buttons)

def show_confirmation(reply_token, state):
    summary = "\n".join([f"{k}: {v}" for k, v in state.items() if k != "age"])
    text = f"以下の内容で間違いないですか？\n\n{summary}\n\n□ はい。正しく記入したことを確認しました"
    buttons = [{"label": "はい。確認しました", "data": "confirm_ok"}]
    send_buttons(reply_token, text, buttons)

if __name__ == "__main__":
    app.run()
