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
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
OFFICE_TO = os.getenv("OFFICE_TO", "website@eel.style")

# テストモード（JST）
FOLLOWUP_TEST_MODE = os.getenv("FOLLOWUP_TEST_MODE", "0") == "1"
TEST_CUTOFF_HOUR   = int(os.getenv("TEST_CUTOFF_HOUR", "6"))
TEST_CUTOFF_MINUTE = int(os.getenv("TEST_CUTOFF_MINUTE", "45"))
TEST_SEND_HOUR     = int(os.getenv("TEST_SEND_HOUR", "6"))
TEST_SEND_MINUTE   = int(os.getenv("TEST_SEND_MINUTE", "50"))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 状態管理（メモリ） ======
user_states = {}       # user_id -> dict(回答)
completed_users = {}   # user_id -> (完了日時, サマリー文字列)

# ====== 質問フロー ======
QUESTION_STEPS = [
    "都道府県", "お名前", "フリガナ", "電話番号",
    "生年月日_年", "生年月日_月", "生年月日_日",
    "性別", "身長", "体重",
    "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
    "病名", "お薬服用", "服用薬", "アレルギー", "アレルギー名"
]

def get_next_question(state):
    """次に聞くべきキーを返す（分岐も考慮）"""
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

# ====== メール（事務局のみ） ======
def send_summary_email_to_office(summary, user_id):
    subject = "東京MITクリニック 妊活オンライン診療：問診を受け付けました（事務局通知）"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = OFFICE_TO

    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
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
            except Exception:
                pass
            if SMTP_USER and SMTP_PASS:
                smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
    except Exception as e:
        print("【問診結果メール送信エラー（事務局）】", repr(e))

# ====== 初期化 ======
def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.pop(user_id, None)
    line_bot_api.reply_message(reply_token, TextSendMessage(text="お住まいの都道府県名を入力してください。"))

# ====== Follow（友だち追加） ======
@handler.add(FollowEvent)
def handle_follow(event):
    start_registration(event.source.user_id, event.reply_token)

# ====== Flexボタン ======
def send_buttons(reply_token, text, buttons):
    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": text, "wrap": True, "weight": "bold", "size": "md"},
                *[
                    {"type": "button", "style": "primary", "margin": "sm",
                     "action": {"type": "postback", "label": b["label"], "data": b["data"], "displayText": b["label"]}}
                    for b in buttons
                ]
            ]
        }
    }
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=text, contents=contents))

# ====== テキスト受信 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    print(f"[MSG] user={user_id} text={repr(text)}")

    # ---- 手動フォローアップ送信（本番/テスト問わず即送信）----
    norm = text.replace("　", " ").strip()
    if any(t in norm for t in {"テスト送信実行", "送信テスト実行"}) or norm.lower() in {"test", "sendnow", "runfollowup"}:
        # 現在 completed_users に居るユーザーへ強制送信
        targets = list(completed_users.keys())
        print(f"[Followup:TEST] now={datetime.now():%Y-%m-%d %H:%M:%S} targets={len(targets)} (force-send)")
        for uid in targets:
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
            # 本番の送信と同じ扱いでキューから外す
            completed_users.pop(uid, None)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="フォローアップ送信を手動実行しました。ログを確認してください。"))
        return

    # ---- メールテスト（誰が送っても事務局へ）----
    if norm.startswith("メールテスト"):
        body = norm[len("メールテスト"):].strip() or "動作確認テスト送信"
        try:
            msg = EmailMessage()
            msg["Subject"] = "【テスト送信】東京MITクリニック 妊活オンライン診療"
            msg["From"] = SMTP_FROM
            msg["To"] = OFFICE_TO
            msg.set_content(body)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.ehlo()
                try:
                    smtp.starttls()
                    smtp.ehlo()
                except Exception:
                    pass
                if SMTP_USER and SMTP_PASS:
                    smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"事務局宛にテストメールを送信しました。\nTo: {OFFICE_TO}"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"テスト送信に失敗しました。\n原因: {repr(e)}"))
        return

    # ---- リセット / 手動開始 ----
    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return
    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        return

    # ここから質問フロー
    step = get_next_question(state)

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

    if step == "生年月日_年":
        if text.isdigit() and len(text) == 4 and 1900 <= int(text) <= 2100:
            state["生年月日_年"] = int(text)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた月（1〜12）を入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="西暦4桁で入力してください（例：1988）"))
        return

    if step == "生年月日_月":
        if text.isdigit() and 1 <= int(text) <= 12:
            state["生年月日_月"] = int(text)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた日（1〜31）を入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="月は1〜12の数字で入力してください。"))
        return

    if step == "生年月日_日":
        if text.isdigit():
            d = int(text)
            y = state.get("生年月日_年")
            m = state.get("生年月日_月")
            try:
                birth = date(y, m, d)
                state["生年月日_日"] = d
                state["生年月日"] = birth.strftime("%Y-%m-%d")
                today = date.today()
                age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
                state["満年齢"] = age
                send_buttons(event.reply_token, "性別を選択してください。", [
                    {"label": "女", "data": "gender_female"},
                    {"label": "男", "data": "gender_male"}
                ])
                return
            except ValueError:
                pass
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正しい日付を入力してください。"))
        return

    if step == "性別":
        send_buttons(event.reply_token, "性別を選択してください。", [
            {"label": "女", "data": "gender_female"},
            {"label": "男", "data": "gender_male"}
        ])
        return

    if step == "身長":
        if text.isdigit() and 100 <= int(text) <= 250:
            state["身長"] = f"{int(text)}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重（kg）を入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="身長は100〜250の数字で入力してください。"))
        return

    if step == "体重":
        if text.isdigit() and 20 <= int(text) <= 200:
            state["体重"] = f"{int(text)}"
            send_buttons(event.reply_token, "アルコールを常習的に摂取していますか？", [
                {"label": "はい", "data": "alcohol_yes"},
                {"label": "いいえ", "data": "alcohol_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重は20〜200の数字で入力してください。"))
        return

    if step == "病名":
        if text:
            state["病名"] = text
            send_buttons(event.reply_token, "現在、お薬を服用していますか？", [
                {"label": "はい", "data": "med_yes"},
                {"label": "いいえ", "data": "med_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病名（不明なら治療内容）を入力してください。"))
        return

    if step == "服用薬":
        if text:
            state["服用薬"] = text
            send_buttons(event.reply_token, "アレルギーはありますか？", [
                {"label": "はい", "data": "allergy_yes"},
                {"label": "いいえ", "data": "allergy_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="服用薬の名称を入力してください。"))
        return

    if step == "アレルギー名":
        if text:
            state["アレルギー名"] = text
            finalize_response(event, user_id, state)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="アレルギー名を入力してください。"))
        return

    # 予期せぬ入力
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="次の入力をお願いします。"))

# ====== ポストバック（ボタン） ======
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    state = user_states.setdefault(user_id, {})
    data = event.postback.data

    mapping = {
        "gender_female": ("性別", "女"),
        "gender_male":   ("性別", "男"),
        "alcohol_yes":   ("アルコール", "はい"),
        "alcohol_no":    ("アルコール", "いいえ"),
        "steroid_yes":   ("副腎皮質ホルモン剤", "はい"),
        "steroid_no":    ("副腎皮質ホルモン剤", "いいえ"),
        "cancer_yes":    ("がん", "はい"),
        "cancer_no":     ("がん", "いいえ"),
        "diabetes_yes":  ("糖尿病", "はい"),
        "diabetes_no":   ("糖尿病", "いいえ"),
        "other_yes":     ("その他病気", "はい"),
        "other_no":      ("その他病気", "いいえ"),
        "med_yes":       ("お薬服用", "はい"),
        "med_no":        ("お薬服用", "いいえ"),
        "allergy_yes":   ("アレルギー", "はい"),
        "allergy_no":    ("アレルギー", "いいえ"),
    }
    if data in mapping:
        key, val = mapping[data]
        state[key] = val

    if data in ("gender_female", "gender_male"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="身長（cm）を入力してください。"))
        return

    if data in ("alcohol_yes", "alcohol_no"):
        send_buttons(event.reply_token, "副腎皮質ホルモン剤を投与中ですか？", [
            {"label": "はい", "data": "steroid_yes"},
            {"label": "いいえ", "data": "steroid_no"}
        ])
        return

    if data in ("steroid_yes", "steroid_no"):
        send_buttons(event.reply_token, "がんにかかっていて治療中ですか？", [
            {"label": "はい", "data": "cancer_yes"},
            {"label": "いいえ", "data": "cancer_no"}
        ])
        return

    if data in ("cancer_yes", "cancer_no"):
        send_buttons(event.reply_token, "糖尿病で治療中ですか？", [
            {"label": "はい", "data": "diabetes_yes"},
            {"label": "いいえ", "data": "diabetes_no"}
        ])
        return

    if data in ("diabetes_yes", "diabetes_no"):
        send_buttons(event.reply_token, "そのほか現在、治療中、通院中の病気はありますか？", [
            {"label": "はい", "data": "other_yes"},
            {"label": "いいえ", "data": "other_no"}
        ])
        return

    if data == "other_yes":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病気の名称（わからなければ治療内容）を入力してください。"))
        return
    if data == "other_no":
        send_buttons(event.reply_token, "現在、お薬を服用していますか？", [
            {"label": "はい", "data": "med_yes"},
            {"label": "いいえ", "data": "med_no"}
        ])
        return

    if data == "med_yes":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="お薬の名前をすべてお伝えください。"))
        return
    if data == "med_no":
        send_buttons(event.reply_token, "アレルギーはありますか？", [
            {"label": "はい", "data": "allergy_yes"},
            {"label": "いいえ", "data": "allergy_no"}
        ])
        return

    if data == "allergy_yes":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="アレルギー名をお伝えください。"))
        return
    if data == "allergy_no":
        finalize_response(event, user_id, state)
        return

# ====== まとめ & 送信 ======
def finalize_response(event, user_id, state):
    # 生年月日統合
    if "生年月日" not in state and all(k in state for k in ("生年月日_年", "生年月日_月", "生年月日_日")):
        birth = date(state["生年月日_年"], state["生年月日_月"], state["生年月日_日"])
        state["生年月日"] = birth.strftime("%Y-%m-%d")

    # 表示順
    ordered_keys = [
        "都道府県", "お名前", "フリガナ", "電話番号",
        "生年月日", "性別", "身長", "体重",
        "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
        "病名", "お薬服用", "服用薬", "アレルギー", "アレルギー名"
    ]

    # 整形
    lines = []
    # お名前（フリガナ）をまとめて一行
    if "お名前" in state:
        if "フリガナ" in state:
            lines.append(f"お名前: {state['お名前']}（{state['フリガナ']}）")
        else:
            lines.append(f"お名前: {state['お名前']}")

    for k in ordered_keys:
        if k in ("お名前", "フリガナ"):
            continue
        if k not in state:
            continue
        v = state[k]
        if k == "生年月日":
            try:
                bd = datetime.strptime(v, "%Y-%m-%d").date()
                age = state.get("満年齢")
                lines.append(f"生年月日: {bd.year}年{bd.month}月{bd.day}日（満{age}歳）")
            except Exception:
                lines.append(f"生年月日: {v}")
        elif k == "身長":
            lines.append(f"身長: {v} cm")
        elif k == "体重":
            lines.append(f"体重: {v} kg")
        else:
            lines.append(f"{k}: {v}")

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
    send_summary_email_to_office(summary_text, user_id)

    completed_users[user_id] = (datetime.now(), summary_text)
    user_states.pop(user_id, None)

# ====== フォローアップ自動送信 ======
def schedule_daily_followup():
    now = datetime.now()
    if FOLLOWUP_TEST_MODE:
        cutoff = datetime.combine(now.date(), time(TEST_CUTOFF_HOUR, TEST_CUTOFF_MINUTE))
        mode = "TEST"
    else:
        yesterday = now.date() - timedelta(days=1)
        cutoff = datetime.combine(yesterday, time(23, 59, 59))
        mode = "PROD"

    targets = [uid for uid, (finished_at, _) in completed_users.items() if finished_at <= cutoff]
    print(f"[Followup:{mode}] now={now:%Y-%m-%d %H:%M:%S} cutoff={cutoff:%Y-%m-%d %H:%M:%S} targets={len(targets)}")

    for uid in targets:
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

def _heartbeat():
    print(f"[HB] {datetime.now():%Y-%m-%d %H:%M:%S} scheduler alive (test_mode={FOLLOWUP_TEST_MODE})")

# ====== スケジューラ（JST） ======
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(_heartbeat, CronTrigger(minute="*/1"))  # 毎分ハートビート
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
