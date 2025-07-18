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

app = Flask(__name__)

# LINEチャンネル設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
# BOT名（表示用）
ACCOUNT_NAME = os.getenv("LINE_BOT_NAME", "東京MITクリニック")

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

# メモリ上の状態管理
user_states = {}
completed_users = set()


def get_next_question(state):
    steps = ["都道府県", "お名前", "電話番号", "生年月日", "性別", "身長", "体重"]
    for step in steps:
        if step not in state:
            return step
    return None


def calculate_age(birthdate_str):
    try:
        parts = birthdate_str.split('/')
        if len(parts) != 3:
            return None
        year, month, day = map(int, parts)
        # 1900年以前は無効
        if year < 1900:
            return None
        birthdate = datetime(year, month, day)
        today = datetime.today()
        age = today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))
        return age
    except:
        return None


def start_registration(user_id, reply_token):
    # 初期化
    user_states[user_id] = {}
    completed_users.discard(user_id)
    # プロフィール取得
    profile = line_bot_api.get_profile(user_id)
    nickname = profile.display_name
    # あいさつメッセージ
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


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# 管理用: 全ユーザーリセット
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

    # テスト用リセット
    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.discard(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    # 完了ユーザーのブロック
    if user_id in completed_users and text in ("新規登録", "問診"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="すでに問診にご回答いただいています。"))
        return
    if user_id in completed_users:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご回答いただいております。ありがとうございました。"))
        return

    # 新規登録／問診開始
    if text in ("新規登録", "問診"):
        start_registration(user_id, event.reply_token)
        line_bot_api.push_message(user_id, TextSendMessage(text="お住まいの都道府県を教えてください。"))
        return

    # 各ステップ処理
    step = get_next_question(state)

    if step == "都道府県":
        match = next((p for p in PREFECTURES if text == p or p.startswith(text)), None)
        if match:
            state["都道府県"] = match
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="保険証と同じ漢字のフルネームでお名前を教えてください。"))
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
        if text.isdigit() and len(text) == 11:
            state["電話番号"] = text
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="生年月日を 2000/01/01 の形式で入力してください。")
            )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号は11桁の数字で入力してください。（例:09012345678）"))
        return

    elif step == "生年月日":
        age = calculate_age(text)
        if age is None:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="日付が正しくありません。生年月日を 2000/01/01 の形式で正しく入力し直してください。")
            )
            return
        state["生年月日"] = text
        state["年齢"] = age
        buttons = [{"label":"女性","data":"gender_female"},{"label":"男性","data":"gender_male"}]
        send_buttons(event.reply_token, "性別を選択してください。", buttons)
        return

    elif step == "身長":
        if text.isdigit() and 120 <= int(text) <= 255:
            state["身長"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重を数字（kg）で入力してください。"))
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="身長は120〜255の範囲内で、それ以外は数値が間違っています。再度入力してください。")
            )
        return

    elif step == "体重":
        if text.isdigit() and 35 <= int(text) <= 255:
            state["体重"] = text
            # 回答内容をまとめて確認
            summary_lines = []
            for k, v in state.items():
                if k == "年齢":
                    continue
                if k == "生年月日":
                    summary_lines.append(f"{k}: {v}（満{state['年齢']}歳）")
                elif k == "お名前":
                    summary_lines.append(f"{k}: {v}様")
                elif k == "身長":
                    summary_lines.append(f"{k}: {v}cm")
                elif k == "体重":
                    summary_lines.append(f"{k}: {v}kg")
                else:
                    summary_lines.append(f"{k}: {v}")
            summary = "\n".join(summary_lines)
            # フォローアップメッセージ
            followup = (
                "(1分後に自動で結果をお送りします)\n\n"
                "では早速ECサイトストアーズURLをクリックして商品をご選択ください。\n\n"
                "https://70vhnafm3wj1pjo0yitq.stores.jp"
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"以下の内容で承りました：\n\n{summary}\n\n{followup}")
            )
            # 1分後に結果をプッシュ
            Timer(60, lambda uid=user_id: line_bot_api.push_message(uid, TextSendMessage(text="お待たせしました。問診結果をご案内します。"))).start()
            completed_users.add(user_id)
            user_states.pop(user_id, None)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="体重は35〜255の範囲内で、それ以外は数値が間違っています。再度入力してください。")
            )
        return

    # デフォルト応答
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="次の入力をお願いします。　"))


@handler.add(PostbackEvent)
