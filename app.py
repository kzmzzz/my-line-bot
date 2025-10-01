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

load_dotenv()

app = Flask(__name__)

# ====== 環境変数 ======
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET")
ACCOUNT_NAME              = os.getenv("LINE_BOT_NAME", "東京MITクリニック")

SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "website@eel.style")
OFFICE_TO = os.getenv("OFFICE_TO", "website@eel.style")  # 事務局宛

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 状態管理 ======
user_states     = {}  # user_id -> dict(回答ステート)
completed_users = {}  # user_id -> (完了日時, サマリー文字列)
greeted_users   = set()
released_users  = set()

# ====== 質問フロー ======
QUESTION_STEPS = [
    "都道府県", "お名前", "フリガナ", "電話番号",
    "生年月日_年", "生年月日_月", "生年月日_日",
    "性別", "身長", "体重",
    "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
    "病名",       # 「その他病気=はい」のときのみ
    "お薬服用", "服用薬",  # 「お薬服用=はい」のときのみ
    "アレルギー", "アレルギー名"  # 「アレルギー=はい」のときのみ
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

# ====== メール送信（事務局通知） ======
def send_summary_email_to_office(summary, user_id):
    subject = "東京MITクリニック 妊活オンライン診療：問診を受け付けました（事務局通知）"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = OFFICE_TO

    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except:
        nickname = "ご利用者様"

    msg.set_content(
        "以下の内容で問診の受け付けが完了しました。\n\n"
        f"ユーザーID: {user_id}\n"
        f"表示名: {nickname}\n\n"
        f"{summary}"
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except:
                pass
            if SMTP_USER and SMTP_PASS:
                smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
    except Exception as e:
        print("【問診結果メール送信エラー（事務局）】", repr(e))

# ====== 初期化（開始メッセージ） ======
def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.pop(user_id, None)
    try:
        _ = line_bot_api.get_profile(user_id).display_name
    except:
        pass
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="お住まいの都道府県名を入力してください。")
    )

# ====== 友だち追加で即開始 ======
@handler.add(FollowEvent)
def handle_follow(event):
    uid = event.source.user_id
    greeted_users.add(uid)
    start_registration(uid, event.reply_token)

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
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=text, contents=contents))

# ====== テスト用 SMTP 到達確認エンドポイント ======
@app.route("/debug/smtp-test", methods=["GET"])
def debug_smtp():
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as smtp:
            smtp.ehlo()
        return "SMTP reachable", 200
    except Exception as e:
        return f"SMTP error: {e}", 500

# ====== テキスト受信 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text    = event.message.text.strip()

    # フォローアップ後は通常チャットへ移行
    if user_id in released_users:
        return

    # 完了後〜翌朝9時までは固定メッセージ
    if user_id in completed_users:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="問診を受け付けました。回答まで今しばらくお待ち下さい。")
        )
        return

    state = user_states.setdefault(user_id, {})

    # フォールバック：FollowEvent取りこぼし時
    if (user_id not in greeted_users) and not state:
        greeted_users.add(user_id)
        start_registration(user_id, event.reply_token)
        return

    # フロー進行
    step = get_next_question(state)

    if step == "都道府県":
        state["都道府県"] = text
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="ご氏名（保険証と同じお名前を漢字フルネーム）を入力してください。")
        )
        return

    # ... (既存の各ステップ処理が続きます) ...

    if step == "アレルギー名":
        if text:
            state["アレルギー名"] = text
            finalize_response(event, user_id, state)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="アレルギー名を入力してください。")
            )
        return

    # デフォルト
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="次の入力をお願いします。")
    )

# ====== ポストバック処理 ======
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id

    # 完了後〜翌朝9時までは固定メッセージ
    if user_id in completed_users:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="問診を受け付けました。回答まで今しばらくお待ち下さい。")
        )
        return
    # フォローアップ後は通常チャットへ移行
    if user_id in released_users:
        return

    state = user_states.setdefault(user_id, {})
    data  = event.postback.data

    # ... (既存のポストバック処理が続きます) ...

# ====== まとめ & 送信 ======
def finalize_response(event, user_id, state):
    if "生年月日" not in state and all(k in state for k in ("生年月日_年","生年月日_月","生年月日_日")):
        birth = date(state["生年月日_年"], state["生年月日_月"], state["生年月日_日"])
        state["生年月日"] = birth.strftime("%Y-%m-%d")

    ordered_keys = [
        "都道府県","お名前","フリガナ","電話番号",
        "生年月日","性別","身長","体重",
        "アルコール","副腎皮質ホルモン剤","がん","糖尿病","その他病気",
        "病名","お薬服用","服用薬","アレルギー","アレルギー名"
    ]
    lines = []
    if "お名前" in state:
        if "フリガナ" in state:
            lines.append(f"お名前: {state['お名前']}（{state['フリガナ']}）")
        else:
            lines.append(f"お名前: {state['お名前']}")
    for k in ordered_keys:
        if k in ("お名前","フリガナ") or k not in state:
            continue
        v = state[k]
        # ... (サマリー作成処理) ...
    summary_text = "\n".join(lines)

    # 元の問診完了メッセージを表示
    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except:
        nickname = "ご利用者様"
    user_message = (
        f"{nickname}様\n"
        "ご回答、ありがとうございました。\n"
        "以下がご入力いただいた内容になりますので、ご確認ください。\n\n"
        f"{summary_text}\n\n"
        "このあと、問診に対する記入内容を確認し、お薬を処方できるか否か、お返事いたします。\n"
        "医師による回答までに最大24時間（翌日午前9時までに回答）をいただきますことを、ご了承ください。"
    )

    line_bot_api.reply_message(
        event.reply_token,
        [
            TextSendMessage(text=user_message),
            TextSendMessage(text="問診を受け付けました。回答まで今しばらくお待ち下さい。")
        ]
    )
    send_summary_email_to_office(summary_text, user_id)
    completed_users[user_id] = (datetime.now(), summary_text)
    user_states.pop(user_id, None)

# ====== フォローアップ送信 ======
from linebot.models import TextSendMessage

def send_followup(uid):
    try:
        nickname = line_bot_api.get_profile(uid).display_name
    except:
        nickname = "ご利用者様"
    combined_text = (
        f"{nickname}様の問診内容を確認しました。\n"
        "GHRP-2を定期的に服用されることについて、問題はありません。\n"
        "下記より処方のお手続きにお進みください。\n\n"
        # ... (フォローアップ詳細本文) ...
    )
    line_bot_api.push_message(uid, messages=[TextSendMessage(text=combined_text)])
    released_users.add(uid)

# ====== スケジュールジョブ ======
def schedule_daily_followup():
    now       = datetime.now()
    yesterday = now.date() - timedelta(days=1)
    cutoff    = datetime.combine(yesterday, time(23,59,59))
    targets   = [uid for uid,(finished_at,_) in completed_users.items() if finished_at <= cutoff]
    for uid in targets:
        send_followup(uid)
        del completed_users[uid]

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(schedule_daily_followup, 'cron', hour=9, minute=0)
scheduler.start()

# ====== ルーティング ======
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    user_states.clear()
    completed_users.clear()
    greeted_users.clear()
    released_users.clear()
    return "All states reset", 200

@app.route("/ping", methods=["GET","HEAD"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    app.run()