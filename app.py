@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    # 強制的にステート初期化してから開始するように
    if text in ("新規登録", "問診"):
        user_states.pop(user_id, None)
        completed_users.discard(user_id)
        start_registration(user_id, event.reply_token)
        line_bot_api.push_message(user_id, TextSendMessage(text="お住まいの都道府県を入力してください。"))
        return

    if text == "リセット":
        user_states.pop(user_id, None)
        completed_users.discard(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="状態をリセットしました。"))
        return

    if user_id in completed_users:
        handle_general_message(event)
        return

    state = user_states.setdefault(user_id, {})
    step = get_next_question(state)

    if step == "都道府県":
        match = next((p for p in PREFECTURES if text == p or p.startswith(text)), None)
        if match:
            state["都道府県"] = match
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="保険証と同じお名前を入力してください。"))
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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生年月日をYYYY/MM/DD形式で入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="電話番号を正しく入力してください。"))
        return

    elif step == "生年月日":
        age = calculate_age(text)
        if age is not None:
            state["生年月日"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"年齢は {age} 歳ですね。\n性別を「男」または「女」で入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="生年月日をYYYY/MM/DD形式で入力してください。"))
        return

    elif step == "性別":
        if text in ("男", "女"):
            state["性別"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="身長をcm単位で数字のみで入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="「男」または「女」で入力してください。"))
        return

    elif step == "身長":
        if text.isdigit():
            state["身長"] = text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重をkg単位で数字のみで入力してください。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="身長を数字で入力してください。"))
        return

    elif step == "体重":
        if text.isdigit():
            state["体重"] = text
            completed_users.add(user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="問診のご回答、ありがとうございました。\nその他のご用件があればお知らせください。"
            ))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="体重を数字で入力してください。"))
        return

    # それ以外は通常応答に回す
    handle_general_message(event)
