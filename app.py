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
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ACCOUNT_NAME = os.getenv("LINE_BOT_NAME", "東京MITクリニック")

SMTP_HOST = os.getenv("SMTP_HOST", "eel-style.sakura.ne.jp")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "website@eel.style")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "website@eel.style")
OFFICE_TO = os.getenv("OFFICE_TO", "website@eel.style")  # 事務局宛

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ====== 状態管理 ======
user_states = {}                 # user_id -> dict(回答ステート)
completed_users = {}             # user_id -> (完了日時, サマリー文字列)
greeted_users = set()            # Follow受信 or 初回発言で案内済み

# ====== 質問フロー ======
QUESTION_STEPS = [
    "都道府県", "お名前", "フリガナ", "電話番号",
    "生年月日_年", "生年月日_月", "生年月日_日",
    "性別", "身長", "体重",
    "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
    "病名",                     # 「その他病気=はい」のときのみ
    "お薬服用", "服用薬",       # 「お薬服用=はい」のときのみ
    "アレルギー", "アレルギー名" # 「アレルギー=はい」のときのみ
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

# ====== 初期化（開始メッセージ） ======
def start_registration(user_id, reply_token):
    user_states[user_id] = {}
    completed_users.pop(user_id, None)
    try:
        _nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
        _nickname = "ご利用者様"
    line_bot_api.reply_message(reply_token, TextSendMessage(text="お住まいの都道府県名を入力してください。"))

# ====== 友だち追加で即開始（取りこぼし対策：案内済みフラグ） ======
@handler.add(FollowEvent)
def handle_follow(event):
    uid = event.source.user_id
    print(f"[EVT] FollowEvent from {uid}")
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
    message = FlexSendMessage(alt_text=text, contents=contents)
    line_bot_api.reply_message(reply_token, message)

# ====== テキスト受信 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = user_states.setdefault(user_id, {})

    print(f"[MSG] user={user_id} text={repr(text)}")

    # --- フォールバック：FollowEvent取りこぼし時、初回発言で開始 ---
    if (user_id not in greeted_users) and (not state) and (user_id not in completed_users):
        print(f"[FALLBACK] start on first message: uid={user_id}")
        greeted_users.add(user_id)
        start_registration(user_id, event.reply_token)
        return

    # === 管理用コマンド ===
    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.pop(user_id, None)
        greeted_users.discard(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    # 手動開始（テスト用）
    if text in ("新規登録", "問診"):
        greeted_users.add(user_id)
        start_registration(user_id, event.reply_token)
        return

    # 事務局にテストメール送信（誰が送ってもOK）
    if text.startswith("メールテスト"):
        body = text[len("メールテスト"):].strip() or "動作確認テスト送信"
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

    # フォローアップ手動送信（即時）
    if text in {"テスト送信実行", "送信テスト実行"} or text.lower() in {"test", "sendnow", "runfollowup"}:
        sent = 0
        for uid, (_finished_at, _summary_text) in list(completed_users.items()):
            send_followup(uid)
            del completed_users[uid]
            sent += 1
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"フォローアップ送信を手動実行しました。targets={sent}"))
        print(f"[Followup:FORCE] now={datetime.now():%Y-%m-%d %H:%M:%S} targets={sent}")
        return

    # ====== フロー進行 ======
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
                # 性別ボタン
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
        # 念のためボタン提示
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
            # アルコール
            send_buttons(event.reply_token, "アルコールを常習的に摂取していますか？", [
                {"label": "はい", "data": "alcohol_yes"},
                {"label": "いいえ", "data": "alcohol_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重は20〜200の数字で入力してください。"))
        return

    if step == "アルコール":
        # ここはボタン回答のみなので通常は到達しない
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "副腎皮質ホルモン剤":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "がん":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "糖尿病":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "その他病気":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "病名":
        if text:
            state["病名"] = text
            # お薬服用
            send_buttons(event.reply_token, "現在、お薬を服用していますか？", [
                {"label": "はい", "data": "med_yes"},
                {"label": "いいえ", "data": "med_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="病名（不明なら治療内容）を入力してください。"))
        return

    if step == "お薬服用":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
        return

    if step == "服用薬":
        if text:
            state["服用薬"] = text
            # アレルギー
            send_buttons(event.reply_token, "アレルギーはありますか？", [
                {"label": "はい", "data": "allergy_yes"},
                {"label": "いいえ", "data": "allergy_no"}
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="服用薬の名称を入力してください。"))
        return

    if step == "アレルギー":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画面のボタンからお答えください。"))
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

# ====== ポストバック処理（ボタン） ======
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

    # 分岐の流れ
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
    # 生年月日が分割で入っていれば整える
    if "生年月日" not in state and all(k in state for k in ("生年月日_年", "生年月日_月", "生年月日_日")):
        birth = date(state["生年月日_年"], state["生年月日_月"], state["生年月日_日"])
        state["生年月日"] = birth.strftime("%Y-%m-%d")

    ordered_keys = [
        "都道府県", "お名前", "フリガナ", "電話番号",
        "生年月日", "性別", "身長", "体重",
        "アルコール", "副腎皮質ホルモン剤", "がん", "糖尿病", "その他病気",
        "病名", "お薬服用", "服用薬", "アレルギー", "アレルギー名"
    ]

    lines = []
    # お名前（フリガナ）を最初に
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

    # ニックネーム
    try:
        nickname = line_bot_api.get_profile(user_id).display_name
    except Exception:
        nickname = "ご利用者様"

    # 問診完了メッセージ
    user_message = (
        f"{nickname}様\n"
        "ご回答、ありがとうございました。\n"
        "以下がご入力いただいた内容になりますので、ご確認ください。\n\n"
        f"{summary_text}\n\n"
        "このあと、問診に対する記入内容を確認し、お薬を処方できるか否か、お返事いたします。\n"
        "医師による回答までに最大24時間（翌日午前9時までに回答）をいただきますことを、ご了承ください。"
    )

    # 返信 & 事務局メール送信
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=user_message))
    send_summary_email_to_office(summary_text, user_id)

    # 翌朝送信用に完了時刻を保存
    completed_users[user_id] = (datetime.now(), summary_text)

    # ステート破棄
    user_states.pop(user_id, None)

# ====== フォローアップ送信（本文＋詳細） ======
def send_followup(uid):
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
        "※半年ごとに定期問診を行います（無料）。"
    )

    details_text = (
        "■GHRP-2（商品名：セルアクチン）定期処方内容　\n"
        "※定期回数に決まり（縛り）はありません。\n\n"
        "お薬は初回３カ月分（180錠）￥79,980です（一日あたり￥888）。ご購入手続き後、医師がビデオ通話によるオンライン診察ののち「処方可」と診断した場合にお届けします。「処方不可」と診断した場合は当方で購入キャンセルの手続きを行います。\n\n"
        "４カ月目からは１カ月分（60錠）￥36,000です（一日あたり￥1,200）。初回処方時より85～90日後にご自宅にお届け、以降25～30日ごとにお届けします。\n\n"
        "申し出がない限り処方を自動継続します（医師は６カ月ごとに問診を無料で行い処方中断の診断を下す可能性があります）。\n\n"
        "お薬をやめられる際は、次回分お届け10日前までに、ECサイトを運営する株式会社アネラジャパン：TEL 03-5542-1986（火・水・金曜日の10：00～17：00）までお電話ください。\n\n"
        "金額はすべて税込。医療費控除対象です。\n\n"
        "お届けはヤマト運輸の宅急便コンパクトで行います。送料は無料。お支払い方法はクレジットカードまたは口座振替です。手数料はかかりません。なお、VISA、MASTERカードをご使用の方はECサイトを運営する株式会社アネラジャパン：TEL 03-5542-1986（火・水・金曜日の10：00～17：00）までお電話ください。\n\n"
        "【重要事項】\n"
        "ご購入手続きの終了確認後に当方がお送りする「医師のLINEアカウント」を友だち登録していただくと、一両日中に医師より着信がありますので、ビデオ通話にて診察にお進みください。医師との画面を通した診察をお済ましでない方は、お薬をお受け取りになることができません。ご注意ください。\n\n"
        "https://credit.j-payment.co.jp/link/creditcard?aid=132942&iid=10001&mailauthckey=d05678f282ce4092adeb1a327f06803f\n\n"
        "■特定商取引法に基づく表記\n"
        "業者名：株式会社アネラジャパン\n"
        "運営責任者：代表取締役 保川敏克\n"
        "所在地：〒104-0031 東京都中央区京橋2-7-14  BUREX京橋\n"
        "電話番号：03-5542-1986（火・水・金曜日の10：00～17：00）\n"
        "メールアドレス：yasukawatoshi@gmail.com\n"
        "業務内容：オンライン診療・処方薬の代金の受領代行。東京MITクリニックの指示のもと、オンライン診療（自由診療）に関する決済業務を行っております。\n"
        "※当社は医療機関ではありません。医療行為自体は当社では行っておりません。\n"
        "処方薬の価格：処方内容により異なります。詳細は東京MITクリニックでのオンライン診察（問診）のあとお伝えいたします。\n"
        "商品代金以外の必要料金：通信費、インターネット接続費（お客様負担）\n\n"
        "支払方法：クレジットカード決済、口座振替\n"
        "支払時期：東京MITクリニックでのオンライン診察（問診）のあと、ご案内に沿ってお支払いください。\n"
        "引渡時期：ビデオ通話によるオンライン診察完了後、東京MITクリニックの１～４営業日以内に発送いたします。\n"
        "返品・キャンセルについて：医療用医薬品という性質上、お受け取り後の返品・交換はできません。ただし、誤発送・破損等があった場合は速やかに東京MITクリニックが対応いたします。"
    )

    line_bot_api.push_message(
        uid,
        messages=[
            TextSendMessage(text=followup_text),
            TextSendMessage(text=details_text)
        ]
    )

# ====== 翌朝9時の自動送信（23:59締切分を配信） ======
def schedule_daily_followup():
    now = datetime.now()
    yesterday = now.date() - timedelta(days=1)
    cutoff = datetime.combine(yesterday, time(23, 59, 59))

    targets = [uid for uid, (finished_at, _summary_text) in completed_users.items() if finished_at <= cutoff]
    print(f"[Followup] now={now:%Y-%m-%d %H:%M:%S} cutoff={cutoff:%Y-%m-%d %H:%M:%S} targets={len(targets)}")

    for uid in targets:
        send_followup(uid)
        del completed_users[uid]

# ====== APScheduler 起動（毎日9:00 JST） ======
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(schedule_daily_followup, 'cron', hour=9, minute=0)
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
    greeted_users.clear()
    return "All states reset", 200

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    # ヘルスチェック / Keep-Alive 用
    return "pong", 200

if __name__ == "__main__":
    app.run()
