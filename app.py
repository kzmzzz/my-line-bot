from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, FollowEvent
)
import os
from threading import Timer
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ACCOUNT_NAME = os.getenv("LINE_BOT_NAME", "東京MITクリニック")

SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "hadfi0609")
SMTP_FROM = os.getenv("SMTP_FROM", "website@eel.style")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

user_states = {}
completed_users = set()

def get_next_question(state):
    for step in ["お名前", "電話番号", "メールアドレス",
                 "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気"]:
        if step not in state:
            return step
    if state.get("その他病気") == "はい" and "病名" not in state:
        return "病名"
    return None

def send_notification_email(user_id, nickname):
    msg = EmailMessage()
    msg['Subject'] = f"【新規登録通知】{nickname} 様"
    msg['From'] = SMTP_FROM
    msg['To'] = 'website@eel.style'
    msg.set_content(
        f"LINE Botで新規登録がありました。\n"
        f"ユーザーID: {user_id}\n"
        f"表示名: {nickname}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)

def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.discard(user_id)
    profile = line_bot_api.get_profile(user_id)
    nickname = profile.display_name
    greeting = (
        f"{nickname}様\n\n"
        f"{ACCOUNT_NAME}でございます。\n"
        "このたびはご登録くださり、誠にありがとうございます。\n"
        "『GHPR-2（セルアクチン）』の処方を希望される方は、LINEによるオンライン診療（問診）にお進みください。\n\n"
        "☆今後のオンライン診療の進め方\n\n"
        "１．簡単な問診\n"
        "　　　↓\n"
        "２．お薬のご選択\n"
        "　　　↓\n"
        "３．LINEビデオ通話による診察\n"
        "　　　↓\n"
        "４．お薬をご自宅に発送"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=greeting))
    try:
        send_notification_email(user_id, nickname)
    except Exception as e:
        print("【メール送信エラー】", repr(e))

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    user_states.clear()
    completed_users.clear()
    return "All states reset", 200

@handler.add(FollowEvent)
def handle_follow(event):
    start_registration(event.source.user_id, event.reply_token)

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.discard(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    # ↓↓↓ 一時的に再回答制限をOFFにするためコメントアウト ↓↓↓
    # if user_id in completed_users and text in ("新規登録","問診"):
    #     line_bot_api.reply_message(event.reply_token, TextSendMessage(text="すでに問診にご回答いただいています。"))
    #     return

    # if user_id in completed_users:
    #     line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答いただいております。ありがとうございました。"))
    #     return
    # ↑↑↑ コメントアウトここまで ↑↑↑

    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        line_bot_api.push_message(user_id, TextSendMessage(text="お名前を教えてください。"))
        return

    step = get_next_question(state)
    if step == "お名前":
        if text:
            state["お名前"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号をハイフンなしで入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お名前を入力してください。"))
        return

    elif step == "電話番号":
        if text.isdigit() and len(text) == 11:
            state["電話番号"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="メールアドレスを入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号は11桁の数字で入力してください。"))
        return

    elif step == "メールアドレス":
        if "@" in text and "." in text:
            state["メールアドレス"] = text
            buttons = [{"label":"はい","data":"alcohol_yes"},{"label":"いいえ","data":"alcohol_no"}]
            send_buttons(event.reply_token, "アルコールを常習的に摂取していますか？", buttons)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="メールアドレスの形式が正しくありません。"))
        return

    elif step == "病名":
        if text:
            state["病名"] = text
            finalize_response(event, user_id, state)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病名を入力してください。"))
        return

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="次の入力をお願いします。"))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    state = user_states.setdefault(user_id, {})
    data = event.postback.data

    # ↓↓↓ 一時的に再回答制限をOFFにするためコメントアウト ↓↓↓
    # if user_id in completed_users:
    #     line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答いただいております。ありがとうございました。"))
    #     return
    # ↑↑↑ コメントアウトここまで ↑↑↑

    if data.startswith("alcohol_"):
        state["アルコール"] = "はい" if data == "alcohol_yes" else "いいえ"
        buttons = [{"label":"はい","data":"steroid_yes"},{"label":"いいえ","data":"steroid_no"}]
        send_buttons(event.reply_token, "副腎皮質ホルモン剤を投与中ですか？", buttons)

    elif data.startswith("steroid_"):
        state["副腎皮質ホルモン剤"] = "はい" if data == "steroid_yes" else "いいえ"
        buttons = [{"label":"はい","data":"cancer_yes"},{"label":"いいえ","data":"cancer_no"}]
        send_buttons(event.reply_token, "がんにかかっていて治療中ですか？", buttons)

    elif data.startswith("cancer_"):
        state["がん"] = "はい" if data == "cancer_yes" else "いいえ"
        buttons = [{"label":"はい","data":"diabetes_yes"},{"label":"いいえ","data":"diabetes_no"}]
        send_buttons(event.reply_token, "糖尿病で治療中ですか？", buttons)

    elif data.startswith("diabetes_"):
        state["糖尿病"] = "はい" if data == "diabetes_yes" else "いいえ"
        buttons = [{"label":"はい","data":"otherdisease_yes"},{"label":"いいえ","data":"otherdisease_no"}]
        send_buttons(event.reply_token, "そのほか、なにか病気にかかっていますか？", buttons)

    elif data.startswith("otherdisease_"):
        state["その他病気"] = "はい" if data == "otherdisease_yes" else "いいえ"
        if state["その他病気"] == "はい":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病名を教えてください。"))
        else:
            finalize_response(event, user_id, state)

def finalize_response(event, user_id, state):
    summary_lines = [f"{k}: {v}" for k, v in state.items()]
    summary = "\n".join(summary_lines)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答ありがとうございました。内容をプッシュでお送りします。"))
    line_bot_api.push_message(user_id, TextSendMessage(text=f"以下の内容で承りました：\n\n{summary}"))
    completed_users.add(user_id)
    user_states.pop(user_id, None)

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
