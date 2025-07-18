from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, FollowEvent
)
import os
import json
from datetime import datetime
from threading import Timer

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 47都道府県リスト
PREFECTURES = [
    "北海道","青森県","岩手県","宮城県","秋田県","山形県","福島県",
    "茨城県","栃木県","群馬県","埼玉県","千葉県","東京都","神奈川県",
    "新潟県","富山県","石川県","福井県","山梨県","長野県",
    "岐阜県","静岡県","愛知県","三重県",
    "滋賀県","京都府","大阪府","兵庫県","奈良県","和歌山県",
    "鳥取県","島根県","岡山県","広島県","山口県",
    "徳島県","香川県","愛媛県","高知県",
    "福岡県","佐賀県","長崎県","熊本県","大分県","宮崎県","鹿児島県","沖縄県"
]

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


def start_registration(user_id, reply_token):
    # state の初期化と既存完了フラグの解除
    user_states[user_id] = {}
    completed_users.discard(user_id)
    # ウェルカムメッセージ送信
    welcome = TextSendMessage(
        text="新規登録ありがとうございます！\nまずはお住まいの都道府県を教えてください。"
    )
    line_bot_api.reply_message(reply_token, welcome)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    start_registration(user_id, event.reply_token)


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    # 「新規登録」テキストでフォロー動作を擬似トリガー
    if text == "新規登録":
        start_registration(user_id, event.reply_token)
        return

    if user_id in completed_users:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="すでに問診にご回答いただいています。ありがとうございました。")
        )
        return

    if text == "問診":
        start_registration(user_id, event.reply_token)
        return

    step = get_next_question(state)

    if step == "都道府県":
        match = None
        for p in PREFECTURES:
            if text == p or p.startswith(text):
                match = p
                break
        if match:
            state["都道府県"] = match
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="保険証と同じ漢字のフルネームでお名前を教えてください。")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="47都道府県から正しい都道府県名を入力してください。")
            )
        return

    elif step == "お名前":
        if text:
            state["お名前"] = text
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="電話番号をハイフンなしで入力してください。")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="お名前を入力してください。")
            )
        return

    elif step == "電話番号":
        if text.isdigit() and len(text) == 11:
            state["電話番号"] = text
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="生年月日を yyyy/MM/dd の形式で入力してください。")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="電話番号は11桁の数字で入力してください。例：09012345678")
            )
        return

    elif step == "生年月日":
        age = calculate_age(text)
        if age is None:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正しい生年月日形式（yyyy/MM/dd）で入力してください。")
            )
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
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="体重を数字（kg）で入力してください。")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="身長は数字（cm）で入力してください。")
            )
        return

    elif step == "体重":
        if text.isdigit():
            state["体重"] = text
            # 内容確認メッセージ
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
                "(5分後に自動で結果をお送りします。)\n\n"
                "では早速ECサイトストアーズURLをクリックして商品をご選択ください。\n\n"
                "https://70vhnafm3wj1pjo0yitq.stores.jp\n\n"
                "お支払い手続き完了後、診察候補日時をお送りします..."
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"以下の内容で承りました：\n\n{summary}\n\n✅ご回答ありがとうございました。\n\n{followup}")
            )
            Timer(300, lambda uid=user_id: line_bot_api.push_message(uid, TextSendMessage(text="🔔お待たせしました！問診結果をご案内します。"))).start()
            completed_users.add(user_id)
            del user_states[user_id]
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="体重は数字（kg）で入力してください。")
            )
        return

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="次の入力をお願いします。")
        )


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    state = user_states.setdefault(user_id, {})
    data = event.postback.data

    if user_id in completed_users:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="すでに問診にご回答いただいています。")
        )
        return

    if data.startswith("gender_"):
        state["性別"] = "女" if data == "gender_female" else "男"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="身長を数字（cm）で入力してください。")
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="次の入力をお願いします。")
        )


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
