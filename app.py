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
from datetime import date, datetime, timedelta, time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

app = Flask(__name__)

# ====== 環境変数 ======
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ACCOUNT_NAME = os.getenv("LINE_BOT_NAME", "東京MITクリニック")

SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "hadfi0609")
SMTP_FROM = os.getenv("SMTP_FROM", "website@eel.style")
OFFICE_TO = os.getenv("OFFICE_TO", "website@eel.style")

# テストモード設定
FOLLOWUP_TEST_MODE = os.getenv("FOLLOWUP_TEST_MODE", "0") == "1"
TEST_CUTOFF_HOUR = int(os.getenv("TEST_CUTOFF_HOUR", "6"))
TEST_CUTOFF_MINUTE = int(os.getenv("TEST_CUTOFF_MINUTE", "45"))
TEST_SEND_HOUR = int(os.getenv("TEST_SEND_HOUR", "6"))
TEST_SEND_MINUTE = int(os.getenv("TEST_SEND_MINUTE", "50"))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 状態管理 ======
user_states = {}                 # user_id -> dict(回答ステート)
completed_users = {}             # user_id -> (完了日時, サマリー文字列)

# ====== 質問フロー ======
QUESTION_STEPS = [
    "都道府県", "お名前", "フリガナ", "電話番号",
    "生年月日_年", "生年月日_月", "生年月日_日",
    "性別", "身長", "体重",
    "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
    "病名", "お薬服用", "服用薬", "アレルギー", "アレルギー名"
]

def get_next_question(state):
    for step in QUESTION_STEPS:
        if step == "病名" and state.get("その他病気") != "はい":
            continue
        if step == "服用薬" and state.get("お薬服用") != "はい":
            continue
        if step == "アレルギー名" and state.get("アレルギー") != "はい":
            continue
        if step not in state:
            return step
    return None

# ====== メール送信 ======
def send_summary_email_to_admin(summary, user_id):
    subject_admin = "東京MITクリニック 妊活オンライン診療：問診を受け付けました"
    msg_admin = EmailMessage()
    msg_admin["Subject"] = subject_admin
    msg_admin["From"] = SMTP_USER
    msg_admin["To"] = OFFICE_TO
    msg_admin.set_content(f"以下の内容で新規受付がありました。\n\nユーザーID: {user_id}\n\n{summary}")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg_admin)
    except Exception as e:
        print("【問診結果メール送信エラー】", repr(e))

# ====== 初期化 ======
def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.pop(user_id, None)
    line_bot_api.reply_message(reply_token, TextSendMessage(text="お住まいの都道府県名を入力してください。"))

# ====== Flexボタン送信 ======
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

# ====== テキスト受信 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    print(f"[MSG] user={user_id} text={repr(text)}")

    # ---- 手動テスト送信 ----
    norm = text.replace("　", " ").strip()
    triggers = {"テスト送信実行", "送信テスト実行", "テスト 送信 実行", "送信 テスト 実行",
                "TEST", "sendnow", "runfollowup"}
    if any(t in norm for t in triggers) or norm.lower() in {"test", "sendnow", "runfollowup"}:
        schedule_daily_followup()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="フォローアップ送信を手動実行しました。ログを確認してください。")
        )
        return

    # リセット
    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    # 開始
    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        return

    step = get_next_question(state)

    # ====== 各ステップ処理 ======
    if step == "都道府県":
        state["都道府県"] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご氏名（保険証と同じお名前を漢字フルネーム）を入力してください。"))
        return

    if step == "お名前":
        state["お名前"] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="フリガナ（カタカナ）を入力してください。"))
        return

    if step == "フリガナ":
        state["フリガナ"] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お電話番号（ハイフンなし）を入力してください。"))
        return

    if step == "電話番号":
        if text.isdigit() and len(text) in (10, 11):
            state["電話番号"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた西暦（4桁）を入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号は10桁または11桁の数字で入力してください。"))
        return

    # …（以降の処理は元のまま）…

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="次の入力をお願いします。"))

# ====== ポストバック処理 ======
@handler.add(PostbackEvent)
def handle_postback(event):
    pass  # 省略、元のまま

# ====== まとめ & 送信 ======
def finalize_response(event, user_id, state):
    # （サマリ生成処理は元のまま、名前＋フリガナは お名前（フリガナ）の形で表示）

    lines = []
    if "お名前" in state:
        name_line = state["お名前"]
        if "フリガナ" in state:
            name_line += f"（{state['フリガナ']}）"
        lines.append(f"お名前: {name_line}")
    # …（他の項目も整形してlinesに追加）…

    summary_text = "\n".join(lines)

    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
        nickname = "ご利用者様"

    user_message = (
        f"{nickname}様\n"
        "ご回答、ありがとうございました。\n"
        "以下がご入力いただいた内容になりますので、ご確認ください。\n\n"
        f"{summary_text}\n\n"
        "このあと、問診に対する記入内容を確認し、お薬を処方できるか否か、お返事いたします。\n"
        "医師による回答までに最大24時間（翌日午前9時までに回答）をいただきますことを、ご了承ください。"
    )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=user_message))
    send_summary_email_to_admin(summary_text, user_id)

    completed_users[user_id] = (datetime.now(), summary_text)
    user_states.pop(user_id, None)

# ====== フォローアップ ======
def schedule_daily_followup():
    now = datetime.now()
    if FOLLOWUP_TEST_MODE:
        cutoff = datetime.combine(now.date(), time(TEST_CUTOFF_HOUR, TEST_CUTOFF_MINUTE))
    else:
        yesterday = now.date() - timedelta(days=1)
        cutoff = datetime.combine(yesterday, time(23, 59, 59))

    for uid, (finished_at, _summary_text) in list(completed_users.items()):
        if finished_at <= cutoff:
            try:
                nickname = line_bot_api.get_profile(uid).display_name
            except Exception:
                nickname = "ご利用者様"

            followup_text = (
                f"{nickname}様の問診内容を確認しました。\n"
                "GHRP-2を定期的に服用されることについて、問題はありません。\n"
                "処方の手続きにお進みください。\n"
                "処方計画は次のとおりです。\n"
                "この計画にもとづき、継続的に医療用医薬品をお届けします。\n\n"
                "１クール　30日分\n"
                "GHRP-2　60錠　一日２錠を眠前１時間以内を目安に服用\n\n"
                "初回は３クール（90日分＝180錠）をお届けします。\n"
                "以降、服用中止の申し出をいただくまでの間、30日ごとに１クールを継続的にお届けします。\n"
                "※半年ごとに定期問診を行います（無料）。\n\n"
                "ご購入はこちらから\n"
                "https://70vhnafm3wj1pjo0yitq.stores.jp/items/68649249b7ac333809c9545b"
            )

            line_bot_api.push_message(uid, TextSendMessage(text=followup_text))
            del completed_users[uid]

# ====== APScheduler 起動 ======
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(lambda: print(f"[HB] {datetime.now():%Y-%m-%d %H:%M:%S} scheduler alive (test_mode={FOLLOWUP_TEST_MODE})"),
                  CronTrigger(minute="*/1"))
if FOLLOWUP_TEST_MODE:
    scheduler.add_job(schedule_daily_followup, 'cron', hour=TEST_SEND_HOUR, minute=TEST_SEND_MINUTE)
    print(f"[Followup] MODE=TEST  cutoff={TEST_CUTOFF_HOUR:02d}:{TEST_CUTOFF_MINUTE:02d}  send={TEST_SEND_HOUR:02d}:{TEST_SEND_MINUTE:02d} JST")
else:
    scheduler.add_job(schedule_daily_followup, 'cron', hour=9, minute=0)
    print("[Followup] MODE=PROD  cutoff=23:59  send=09:00 JST")
scheduler.start()

# ====== ルーティング ======
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
