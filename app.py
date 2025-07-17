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
        "都道府県", "お名前", "電話番号", "生年月日",
        "性別", "身長", "体重"
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

    if step == "都道府県":
        if text:
            state["都道府県"] = text
            reply = "保険証と同じ漢字のフルネームでお名前を教えてください。"
        else:
            reply = "都道府県名を入力してください。"

    elif step == "お名前":
        if text:
            state["お名前"] = text
            reply = "電話番号をハイフンなしで入力してください。"
        else:
            reply = "お名前を入力してください。"

    elif step == "電話番号":
        if text.isdigit() and len(text) == 11:
            state["電話番号"] = text
            reply = "生年月日を yyyy/mm/dd の形式で入力してください。"
        else:
            reply = "電話番号は11桁の数字で入力してください。例：09012345678"

    elif step == "生年月日":
        age = calculate_age(text)
        if age is None:
            reply = "正しい生年月日形式（yyyy/mm/dd）で入力してください。"
        else:
            state["生年月日"] = text
            state["年齢"] = age
            buttons = [
                {"label": "女", "data": "gender_female"},
                {"label": "男", "data": "gender_male"}
            ]
            send_buttons(event.reply_token, "性別を選択してください。", buttons)
            return

    elif step == "身長":
        if text.isdigit():
            state["身長"] = text
            reply = "体重を数字（kg）で入力してください。"
        else:
            reply = "身長は数字（cm）で入力してください。"

    elif step == "体重":
        if text.isdigit():
            state["体重"] = text
            summary_lines = []
            for k, v in state.items():
                if k == "年齢":
                    continue
                elif k == "生年月日":
                    summary_lines.append(f"{k}: {v}（満{state['年齢']}歳）")
                else:
                    summary_lines.append(f"{k}: {v}")
            summary = "\n".join(summary_lines)
            followup = (
                "(⚫分後に問診結果を送らせていただきます。)\n\n"
                "では早速、ECサイトストアーズURLをクリックして商品をご選択下さい。\n\n"
                "https://70vhnafm3wj1pjo0yitq.stores.jp\n\n"
                "代金のお支払い手続きを確認後、（LINEビデオ通話による）診察日の候補をいくつかお送りいたしますので、"
                "ご都合のよい日時をお選びください。\n"
                "なお、オンライン診察では法令により「身分証明証による本人確認」が必要となります。\n"
                "保険証、マイナ保険証、マイナンバーカード、運転免許証、パスポートのうち、いずれか１つをお手元にご用意ください。"
            )
            reply = f"以下の内容で承りました：\n\n{summary}\n\n✅ ご回答ありがとうございました。\n\n{followup}"
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
        state["性別"] = "女" if data == "gender_female" else "男"
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
