from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, FollowEvent
)
import os
import smtplib
import time as time_module
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

# SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)          # 認証ユーザーと合わせるのが無難
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "0") == "1"   # 1=465/SSL, 0=587/STARTTLS
SMTP_DEBUG = os.getenv("SMTP_DEBUG", "0") == "1"       # 1でSMTP詳細ログ

# 事務局宛先
OFFICE_TO = os.getenv("OFFICE_TO", "website@eel.style")
OFFICE_CC = os.getenv("OFFICE_CC", "")  # 空ならCCなし

# メールテスト機能（任意宛先は管理者のみ）
MAIL_TEST_ENABLED = os.getenv("MAIL_TEST_ENABLED", "0") == "1"
ADMIN_USER_IDS = [u.strip() for u in os.getenv("ADMIN_USER_IDS", "").split(",") if u.strip()]

# フォローアップ送信のテスト切替（JST）
# 本番：前日23:59まで → 翌日09:00送信
# テスト：当日 TEST_CUTOFF_* まで → 当日 TEST_SEND_* に送信
FOLLOWUP_TEST_MODE = os.getenv("FOLLOWUP_TEST_MODE", "0") == "1"
TEST_SEND_HOUR     = int(os.getenv("TEST_SEND_HOUR", "6"))
TEST_SEND_MINUTE   = int(os.getenv("TEST_SEND_MINUTE", "50"))
TEST_CUTOFF_HOUR   = int(os.getenv("TEST_CUTOFF_HOUR", "6"))
TEST_CUTOFF_MINUTE = int(os.getenv("TEST_CUTOFF_MINUTE", "45"))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 状態管理（メモリ保持：再起動/再デプロイで消えます） ======
user_states = {}                 # user_id -> dict(回答ステート)
completed_users = {}             # user_id -> (完了日時, サマリー文字列)

# ====== 質問フロー ======
QUESTION_STEPS = [
    "都道府県", "お名前", "フリガナ", "電話番号",
    "生年月日_年", "生年月日_月", "生年月日_日",
    "性別", "身長", "体重",
    "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
    "病名",
    "お薬服用", "服用薬",
    "アレルギー", "アレルギー名"
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

# ====== 権限ユーティリティ ======
def is_admin(user_id: str) -> bool:
    if not MAIL_TEST_ENABLED:
        return False
    if not ADMIN_USER_IDS:
        return False
    return user_id in ADMIN_USER_IDS

# ====== SMTPユーティリティ（SSL/STARTTLS切替・デバッグ・リトライ） ======
def _send_email(msg: EmailMessage):
    retries = 2
    delay = 1.5
    for attempt in range(retries + 1):
        try:
            if SMTP_USE_SSL:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                    if SMTP_DEBUG: smtp.set_debuglevel(1)
                    if SMTP_USER and SMTP_PASS:
                        smtp.login(SMTP_USER, SMTP_PASS)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                    if SMTP_DEBUG: smtp.set_debuglevel(1)
                    smtp.ehlo()
                    try:
                        smtp.starttls()
                        smtp.ehlo()
                    except smtplib.SMTPException as _e:
                        if SMTP_DEBUG: print("STARTTLS not supported or failed, continuing without TLS:", repr(_e))
                    if SMTP_USER and SMTP_PASS:
                        smtp.login(SMTP_USER, SMTP_PASS)
                    smtp.send_message(msg)
            return
        except Exception as e:
            print(f"【SMTP送信エラー: attempt {attempt+1}/{retries+1}】", repr(e))
            if attempt < retries:
                time_module.sleep(delay)
                delay *= 2
            else:
                raise

# ====== メール送信（事務局のみ） ======
def send_summary_email_to_office(summary, user_id):
    subject_admin = "東京MITクリニック 妊活オンライン診療：問診を受け付けました（事務局通知）"
    msg_admin = EmailMessage()
    msg_admin["Subject"] = subject_admin
    msg_admin["From"] = SMTP_FROM
    msg_admin["To"] = OFFICE_TO
    if OFFICE_CC and OFFICE_CC.strip() and OFFICE_CC.strip().lower() != OFFICE_TO.strip().lower():
        msg_admin["Cc"] = OFFICE_CC

    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
        nickname = "ご利用者様"

    msg_admin.set_content(
        "以下の内容で問診の受け付けが完了しました。\n\n"
        f"ユーザーID: {user_id}\n"
        f"表示名: {nickname}\n\n"
        f"{summary}"
    )

    try:
        _send_email(msg_admin)
    except Exception as e:
        print("【問診結果メール送信エラー（事務局）】", repr(e))

# ====== テスト送信（メールテスト） ======
def send_test_email(to_addr: str, body: str, user_id: str):
    subject = "【テスト送信】東京MITクリニック 妊活オンライン診療"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr

    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
        nickname = "ご利用者様"

    content = (
        "このメールはテスト送信です。\n\n"
        f"送信者（LINE表示名）: {nickname}\n"
        f"ユーザーID: {user_id}\n\n"
        f"本文:\n{body or '（本文なし）'}"
    )
    msg.set_content(content)

    try:
        _send_email(msg)
        return True, None
    except Exception as e:
        return False, repr(e)

# ====== 初期化（友だち追加/新規登録/問診） ======
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

# ====== 友だち追加（FollowEvent） ======
@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    start_registration(user_id, event.reply_token)

# ====== テキスト受信（入力フロー & コマンド） ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    # ---- 手動テスト送信（最優先で処理）----
    if text == "テスト送信実行":
        schedule_daily_followup()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="フォローアップ送信を手動実行しました。ログを確認してください。")
        )
        return

    # 🔹誰でも：「メールテスト [本文任意]」 -> 事務局(OFFICE_TO)に送信
    if text.startswith("メールテスト"):
        body = text[len("メールテスト"):].strip() or "動作確認テスト送信"
        ok, err = send_test_email(OFFICE_TO, body, user_id)
        if ok:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"事務局宛にテストメールを送信しました。\nTo: {OFFICE_TO}")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"テスト送信に失敗しました。\n原因: {err}")
            )
        return

    # 🔒管理者のみ：「メール <宛先> <本文>」 -> 任意宛先に送信（簡易バリデーション）
    if is_admin(user_id) and text.startswith("メール "):
        parts = text.split(maxsplit=2)
        if len(parts) >= 2:
            to_addr = parts[1]
            body = parts[2] if len(parts) >= 3 else "動作確認テスト送信"
            if "@" in to_addr and "." in to_addr and " " not in to_addr:
                ok, err = send_test_email(to_addr, body, user_id)
                if ok:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"テストメールを送信しました。\nTo: {to_addr}"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"テスト送信に失敗しました。\n原因: {err}"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="宛先メールアドレスの形式が正しくありません。例：\nメール test@example.com 本文"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="使い方：\nメール test@example.com 本文"))
        return

    # リセット
    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    # 開始（テスト用手動トリガ）
    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        return

    step = get_next_question(state)

    # ====== 各ステップ ======
    if step == "都道府県":
        state["都道府県"] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご氏名（保険証と同じお名前を漢字フルネーム）を入力してください。"))
        return

    if step == "お名前":
        state["お名前"] = text
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="フリガナを入力してください。"))
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
        if text.isdigit() and len(text) == 4:
            y = int(text)
            if 1900 <= y <= 2100:
                state["生年月日_年"] = y
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生まれた月（1〜12）を入力してください。"))
                return
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

    # デフォルト
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="次の入力をお願いします。"))

# ====== ポストバック処理 ======
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
    ordered_keys = [
        "都道府県", "お名前", "フリガナ", "電話番号",
        "生年月日", "性別", "身長", "体重",
        "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
        "病名", "お薬服用", "服用薬", "アレルギー", "アレルギー名"
    ]

    # 生年月日の統合
    if "生年月日" not in state and all(k in state for k in ("生年月日_年", "生年月日_月", "生年月日_日")):
        birth = date(state["生年月日_年"], state["生年月日_月"], state["生年月日_日"])
        state["生年月日"] = birth.strftime("%Y-%m-%d")

    # 表示整形
    lines = []
    name = state.get("お名前")
    furigana = state.get("フリガナ")
    if name:
        if furigana:
            lines.append(f"お名前: {name}（{furigana}）")
        else:
            lines.append(f"お名前: {name}")

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
        cutoff = datetime.combine(now.date(), time(TEST_CUTOFF_HOUR, TEST_CUTOFF_MINUTE))  # 必要なら秒に59を追加
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

# ====== APScheduler 起動（JST） ======
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
# 心拍ログ：毎分
scheduler.add_job(_heartbeat, CronTrigger(minute="*/1"))

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
