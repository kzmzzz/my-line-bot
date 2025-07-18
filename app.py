from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, FollowEvent
)
import os
from datetime import datetime
from threading import Timer
import smtplib
from email.message import EmailMessage

app = Flask(__name__)

# LINEチャンネル設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
# BOT名（表示用）
ACCOUNT_NAME = os.getenv("LINE_BOT_NAME", "東京MITクリニック")
# SMTP設定（さくらインターネット用デフォルト）
SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
# SMTP_USER と SMTP_PASS はさくらのメールアカウントを環境変数にセットしてください
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "hadfi0609")
# 差出人アドレス（適宜変更）
SMTP_FROM = os.getenv("SMTP_FROM", "website@eel.style")

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

# メモリ管理
user_states = {}
completed_users = set()


def get_next_question(state):
    for step in ["都道府県","お名前","電話番号","生年月日","性別","身長","体重"]:
        if step not in state:
            return step
    return None


def calculate_age(birthdate_str):
    try:
        y, m, d = map(int, birthdate_str.split('/'))
        if y < 1900:
            return None
        bd = datetime(y, m, d)
        today = datetime.today()
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except:
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
        "このたびはご登録くださり、誠にありがとうございます。\n\n"
        "では『GHPR-2（セルアクチン）』の処方を希望される方は、\n"
        "LINEによるオンライン診療（問診）にお進みください。\n\n"
        "今後の診察の流れ\n"
        "１ 簡単な問診 → 2. 商品のご選択 → 3. LINEビデオオンライン診療（国家資格医師）→ ご自宅へ配送（送料無料です）\n\n"
        "ご不明な点がございましたらお気軽にお問い合わせください"
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
    if user_id in completed_users and text in (
        "新規登録","問診"
    ):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="すでに問診にご回答いただいています。"))
        return
    if user_id in completed_users:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答いただいております。ありがとうございました。"))
        return
    if text in ("新規登録","問診"):
        start_registration(user_id, event.reply_token)
        line_bot_api.push_message(user_id, TextSendMessage(text="お住まいの都道府県を教えてください。"))
        return
    step = get_next_question(state)
    if step == "都道府県":
        match = next((p for p in PREFECTURES if text==p or p.startswith(text)), None)
        if match:
            state["都道府県"] = match
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="保険証と同じ漢字のフル네임でお名前を教えてください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="47都道府県から正しい都道府県名を入力してください。"))
        return
    elif step == "お名前":
        if text:
            state["お名前"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号をハイフンなしで入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お名前を入力してください。"))
        return
    elif step == "電話番号":
        if text.isdigit() and len(text)==11:
            state["電話番号"] = text
            line_bot_api.reply_message(event
