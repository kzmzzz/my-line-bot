from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, FollowEvent
)
import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
from datetime import date, datetime

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
    for step in [
        "お名前", "電話番号",
        "生年月日_年", "生年月日_月", "生年月日_日",
        "メールアドレス",
        "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気"
    ]:
        if step not in state:
            return step
    if state.get("その他病気") == "はい" and "病名" not in state:
        return "病名"
    return None

def send_notification_email(user_id, nickname):
    msg = EmailMessage()
    msg['Subject'] = f"【新規登録通知】{nickname} 様"
    msg['From'] = SMTP_USER
    msg['To'] = SMTP_FROM
    msg.set_content(
        f"LINE Botで新規登録がありました。\n"
        f"ユーザーID: {user_id}\n"
        f"表示名: {nickname}"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)

def send_summary_email_to_admin_and_user(summary, user_id, user_email):
    subject_admin = "東京MITクリニック妊活オンライン診療で受け付けました。"
    subject_user = "東京MITクリニック妊活オンライン診療で受け付けました。"

    print("\n📨 メール送信準備中...")
    print("管理者宛:", SMTP_FROM)
    print("本人宛:", user_email)

    msg_admin = EmailMessage()
    msg_admin['Subject'] = subject_admin
    msg_admin['From'] = SMTP_USER
    msg_admin['To'] = SMTP_FROM
    msg_admin.set_content(f"以下の内容で新規受付がありました。\n\nユーザーID: {user_id}\n\n{summary}")

    msg_user = EmailMessage()
    msg_user['Subject'] = subject_user
    msg_user['From'] = SMTP_USER
    msg_user['To'] = user_email
    msg_user.set_content(
        f"{ACCOUNT_NAME}より\n\n"
        "以下の内容で妊活オンライン診療の問診を受け付けました。\n\n"
        f"{summary}\n\n"
        "ご不明点がございましたらご連絡ください。\n\n"
        "このメールは自動送信です。"
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            print("✅ SMTP接続中...")
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            print("✅ SMTPログイン成功")
            smtp.send_message(msg_admin)
            smtp.send_message(msg_user)
            print("✅ メール送信完了")
    except Exception as e:
        print("【問診結果メール送信エラー】", repr(e))

def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.discard(user_id)
    profile = line_bot_api.get_profile(user_id)
    nickname = profile.display_name
    greeting = (
        f"{nickname}様\n\n"
        f"{ACCOUNT_NAME}でございます。ver0722.1600\n"
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
        msg = EmailMessage()
        msg['Subject'] = "【新規登録】東京MITクリニック妊活オンライン診療から新しい登録がありました。"
        msg['From'] = SMTP_USER
        msg['To'] = SMTP_USER
        msg.set_content(
            f"{nickname} 様より新規登録がありました。\n\n"
            f"ユーザーID: {user_id}\n"
            f"LINE表示名: {nickname}\n\n"
            "オンライン診療の問診がまもなく開始されます。"
        )
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
            print("✅ 新規登録通知メール送信完了")
    except Exception as e:
        print("【新規登録通知メール送信エラー】", repr(e))

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

    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        line_bot_api.push_message(user_id, TextSendMessage(text="お名前を入力してください。"))
        return

    step = get_next_question(state)

    if step == "お名前":
        if text:
            state["お名前"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お電話番号をハイフンなしで入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お名前を入力してください。"))
        return

    elif step == "電話番号":
        if text.isdigit() and len(text) in (10, 11):
            state["電話番号"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた西暦（4桁）を入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号は10桁または11桁の数字で入力してください。"))
        return

    elif step == "生年月日_年":
        if text.isdigit() and len(text) == 4:
            year = int(text)
            if 1900 <= year <= 2100:
                state["生年月日_年"] = year
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた月（1〜12）を入力してください。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="1900年〜2100年の間で入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="西暦4桁で入力してください（例：1980）。"))
        return

    elif step == "生年月日_月":
        if text.isdigit():
            month = int(text)
            if 1 <= month <= 12:
                state["生年月日_月"] = month
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた日（1〜31）を入力してください。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="月は1〜12の数字で入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="月は数字で入力してください（例：6）。"))
        return

    elif step == "生年月日_日":
        if text.isdigit():
            day = int(text)
            year = state.get("生年月日_年")
            month = state.get("生年月日_月")
            try:
                birth = date(year, month, day)
                state["生年月日_日"] = day
                state["生年月日"] = birth.strftime("%Y-%m-%d")
                today = date.today()
                age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
                state["年齢"] = str(age)
                readable = f"{year}年{month}月{day}日"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{readable}ですね。\n次にメールアドレスを入力してください。"))
            except ValueError:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="その日は存在しない日付です。もう一度入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="日にちは数字で入力してください（例：10）。"))
        return

    elif step == "メールアドレス":
        if "@" in text and "." in text:
            state["メールアドレス"] = text
            buttons = [{"label": "はい", "data": "alcohol_yes"}, {"label": "いいえ", "data": "alcohol_no"}]
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

    if data.startswith("alcohol_"):
        state["アルコール"] = "はい" if data == "alcohol_yes" else "いいえ"
        buttons = [{"label": "はい", "data": "steroid_yes"}, {"label": "いいえ", "data": "steroid_no"}]
        send_buttons(event.reply_token, "副腎皮質ホルモン剤を投与中ですか？", buttons)

    elif data.startswith("steroid_"):
        state["副腎皮質ホルモン剤"] = "はい" if data == "steroid_yes" else "いいえ"
        buttons = [{"label": "はい", "data": "cancer_yes"}, {"label": "いいえ", "data": "cancer_no"}]
        send_buttons(event.reply_token, "ガンを治療中ですか？", buttons)

    elif data.startswith("cancer_"):
        state["がん"] = "はい" if data == "cancer_yes" else "いいえ"
        buttons = [{"label": "はい", "data": "diabetes_yes"}, {"label": "いいえ", "data": "diabetes_no"}]
        send_buttons(event.reply_token, "糖尿病を治療中ですか？", buttons)

    elif data.startswith("diabetes_"):
        state["糖尿病"] = "はい" if data == "diabetes_yes" else "いいえ"
        buttons = [{"label": "はい", "data": "otherdisease_yes"}, {"label": "いいえ", "data": "otherdisease_no"}]
        send_buttons(event.reply_token, "その他、何か病気で通院していますか？", buttons)

    elif data.startswith("otherdisease_"):
        state["その他病気"] = "はい" if data == "otherdisease_yes" else "いいえ"
        if state["その他病気"] == "はい":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病名を教えてください。"))
        else:
            finalize_response(event, user_id, state)

def finalize_response(event, user_id, state):
    summary_lines = []
    for k, v in state.items():
        if k in ("生年月日_年", "生年月日_月", "生年月日_日", "年齢"):
            continue
        elif k == "生年月日":
            birth_date = datetime.strptime(v, "%Y-%m-%d").date()
            today = date.today()
            age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
            formatted_birth = f"{birth_date.year}年{birth_date.month}月{birth_date.day}日（満{age}歳）"
            summary_lines.append(f"生年月日: {formatted_birth}")
        else:
            summary_lines.append(f"{k}: {v}")

    summary_text = "\n".join(summary_lines)

    # 管理者宛にはECリンクなし
    admin_summary = f"以下の内容で承りました：\n\n{summary_text}"

    # ユーザー宛にはECリンクあり
    followup_text = (
        "\n\nでは早速ECサイトのURLをクリックして、商品をご選択ください。\n\n"
        "https://70vhnafm3wj1pjo0yitq.stores.jp"
    )
    user_summary = f"以下の内容で承りました：\n\n{summary_text}{followup_text}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答ありがとうございました。ご回答内容をお送りします。"))
    line_bot_api.push_message(user_id, TextSendMessage(text=user_summary))
    send_summary_email_to_admin_and_user(admin_summary, user_id, state.get("メールアドレス", ""))
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

if __name__ == "__main__":
    app.run()
