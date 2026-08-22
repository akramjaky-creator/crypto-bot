import sys
import time
from datetime import datetime, timedelta
import ccxt
import pandas as pd
import requests

# ==================== الإعدادات الثابتة ====================
TELEGRAM_BOT_TOKEN = "8933033589:AAGy02S3IKEssxgktTXjdv4ri5l6hHo7Agw"
ADMIN_ID = 786668548
CHECK_INTERVAL = 900  # 15 دقيقة بالثواني

USDT_ADDRESS = "0xb4664dc882e1ae7fb4265b2e5aa21ecffb1624fe".lower()
NETWORK_NAME = "BNB Smart Chain (BEP20) BSC"
BSCSCAN_API_KEY = "YourBscScanApiKeyHere"  # استبدل بمفتاح BscScan الخاص بك للتفعيل الآلي

# قاعدة بيانات المشتركين {chat_id: expiry_datetime}
subscribed_users = {
    ADMIN_ID: datetime(2099, 1, 1)  # اشتراك مجاني دائم للمالك
}

# تتبع الطلبات المعلقة {chat_id: {"days": int, "amount": float}}
pending_requests = {}
processed_txs = set()

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']

# تهيئة منصة بايننس للعقود الآجلة
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})


def send_message(chat_id, text, reply_markup=None):
    """إرسال رسالة عبر التلغرام"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")
        return None


def get_main_keyboard():
    """الواجهة الرئيسية"""
    return {
        "inline_keyboard": [
            [{"text": "💳 خطط الاشتراك والإيداع", "callback_data": "plans"}],
            [{"text": "🆔 حالة الاشتراك والـ ID", "callback_data": "my_id"}],
            [{"text": "📞 الدعم والإدارة", "callback_data": "support"}]
        ]
    }


def get_plans_keyboard():
    """لوحة خيارات خطط الاشتراك"""
    return {
        "inline_keyboard": [
            [{"text": "🔹 10 أيام ($15 USDT)", "callback_data": "plan_10"}],
            [{"text": "🔹 30 يوم ($50 USDT)", "callback_data": "plan_30"}],
            [{"text": "🔙 القائمة الرئيسية", "callback_data": "main_menu"}]
        ]
    }


def broadcast_message(text):
    """إرسال التوصية لجميع المشتركين المفعلين وتصفية المنتهية اشتراكاتهم"""
    now = datetime.now()
    expired_users = []

    for chat_id, expiry in list(subscribed_users.items()):
        if now < expiry:
            send_message(chat_id, text)
        else:
            if chat_id != ADMIN_ID:
                expired_users.append(chat_id)
                send_message(
                    chat_id,
                    "⚠️ **انتهت مدة اشتراكك!**\n"
                    "يرجى إعادة التجديد للاستمرار في استقبال التوصيات."
                )

    for chat_id in expired_users:
        del subscribed_users[chat_id]


def check_blockchain_deposits():
    """التحقق الآلي عبر البلوكشين وتفعيل المشترك فور وصول المبلغ"""
    if not pending_requests:
        return

    url = (
        f"https://api.bscscan.com/api?module=account&action=tokentx"
        f"&address={USDT_ADDRESS}&page=1&offset=20&sort=desc&apikey={BSCSCAN_API_KEY}"
    )
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("status") == "1" and res.get("result"):
            for tx in res["result"]:
                tx_hash = tx.get("hash")
                if tx_hash in processed_txs:
                    continue

                value = float(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
                to_addr = str(tx.get("to", "")).lower()

                if to_addr == USDT_ADDRESS:
                    processed_txs.add(tx_hash)

                    for chat_id, req in list(pending_requests.items()):
                        target_amount = req["amount"]
                        days = req["days"]

                        # قبول هامش بسيط لاختلاف الرسوم والتأكيد
                        if abs(value - target_amount) <= 0.5:
                            expiry_date = datetime.now() + timedelta(days=days)
                            subscribed_users[chat_id] = expiry_date
                            del pending_requests[chat_id]

                            success_msg = (
                                f"🎉 **تم كشف الإيداع وتفعيل اشتراكك بنجاح!**\n\n"
                                f"• **المبلغ المستلم:** `{value:.2f} USDT`\n"
                                f"• **مدة الاشتراك:** `{days}` يوم\n"
                                f"• **صالح حتى:** `{expiry_date.strftime('%Y-%m-%d %H:%M')}`\n\n"
                                f"ستتلقى التوصيات الآلية فور صدورها."
                            )
                            send_message(chat_id, success_msg)

                            admin_notification = (
                                f"🔔 **إيداع آلي جديد:**\n"
                                f"• **الـ ID:** `{chat_id}`\n"
                                f"• **المبلغ:** `{value:.2f} USDT`\n"
                                f"• **المدة:** `{days}` يوم"
                            )
                            send_message(ADMIN_ID, admin_notification)
                            break
    except Exception as e:
        print(f"BscScan API Error: {e}")


def handle_telegram_updates(offset):
    """معالجة التفاعلات والأوامر من مستخدمي تلغرام"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=5"
        response = requests.get(url, timeout=10).json()

        if response.get("ok") and response.get("result"):
            for update in response["result"]:
                offset = update["update_id"] + 1

                # 1. معالجة الضغط على الأزرار
                if "callback_query" in update:
                    cb = update["callback_query"]
                    chat_id = cb["message"]["chat"]["id"]
                    data = cb.get("data")

                    if data == "plans":
                        msg = (
                            "📊 **اختر خطة الاشتراك:**\n\n"
                            "1️⃣ **خطة 10 أيام:** `15 USDT`\n"
                            "2️⃣ **خطة 30 يوم:** `50 USDT`"
                        )
                        send_message(chat_id, msg, get_plans_keyboard())

                    elif data in ["plan_10", "plan_30"]:
                        days = 10 if data == "plan_10" else 30
                        amount = 15 if data == "plan_10" else 50

                        pending_requests[chat_id] = {"days": days, "amount": amount}

                        deposit_instructions = (
                            f"💳 **تعليمات الإيداع والتفعيل الآلي ({days} أيام):**\n\n"
                            f"• **الـ ID الخاص بك:** `{chat_id}`\n"
                            f"• **المبلغ المطلوب:** `{amount} USDT`\n"
                            f"• **الشبكة:** `{NETWORK_NAME}`\n"
                            f"• **عنوان المحفظة:**\n`{USDT_ADDRESS}`\n\n"
                            f"⚠️ **ملاحظة:** قم بتمويل المبلغ الدقيق إلى العنوان أعلاه. "
                            f"سيقوم النظام بالتحقق آلياً عبر الشبكة وتفعيل حسابك فور تأكيد المعاملة."
                        )
                        send_message(chat_id, deposit_instructions)

                    elif data == "my_id":
                        is_active = chat_id in subscribed_users
                        status_str = "مفعل ✅" if is_active else "غير مفعل ❌"
                        expiry = subscribed_users.get(chat_id)
                        exp_str = expiry.strftime('%Y-%m-%d %H:%M') if expiry else "لا يوجد"

                        user_info = (
                            f"🆔 **بيانات الحساب:**\n\n"
                            f"• **الـ ID الخاص بك:** `{chat_id}`\n"
                            f"• **حالة الاشتراك:** `{status_str}`\n"
                            f"• **انتهاء الاشتراك:** `{exp_str}`"
                        )
                        send_message(chat_id, user_info, get_main_keyboard())

                    elif data == "support":
                        send_message(
                            chat_id,
                            "📞 للتواصل المباشر مع الدعم الفني والإدارة: @ADMIN",
                            get_main_keyboard()
                        )

                    elif data == "main_menu":
                        send_message(chat_id, "القائمة الرئيسية:", get_main_keyboard())

                    continue

                # 2. معالجة الرسائل النصية
                message = update.get("message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text = message.get("text", "").strip()

                # أمر التفعيل اليدوي المخصص للمالك فقط: /add ID DAYS
                if chat_id == ADMIN_ID and text.startswith("/add"):
                    parts = text.split()
                    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                        target_id = int(parts[1])
                        days = int(parts[2])
                        exp_date = datetime.now() + timedelta(days=days)
                        subscribed_users[target_id] = exp_date

                        send_message(ADMIN_ID, f"✅ تم تفعيل الـ ID `{target_id}` لمدة `{days}` يوم.")
                        send_message(
                            target_id,
                            f"🎉 **تم تفعيل اشتراكك يدوياً بواسطة الإدارة!**\n\n"
                            f"• **المدة:** `{days}` يوم\n"
                            f"• **الصلاحية حتى:** `{exp_date.strftime('%Y-%m-%d')}`"
                        )
                    continue

                if text == "/start":
                    welcome = (
                        f"🤖 **مرحباً بك في بوت توصيات العقود الآجلة**\n\n"
                        f"🆔 **الـ ID الخاص بك:** `{chat_id}`\n\n"
                        f"استخدم الواجهة أدناه لاختيار الخطة المطلوبة والتفعيل."
                    )
                    send_message(chat_id, welcome, get_main_keyboard())

    except Exception as e:
        print(f"Telegram Updates Handling Error: {e}")

    return offset


def calculate_rsi(series, period=14):
    """حساب مؤشر RSI"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def analyze_and_generate_signals():
    """تحليل سوق العقود الآجلة واستخراج التوصيات الفنية"""
    if not subscribed_users:
        return

    print("جاري فحص أسواق العقود الآجلة...")
    for symbol in SYMBOLS:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            df['sma_20'] = df['close'].rolling(window=20).mean()
            df['rsi'] = calculate_rsi(df['close'], period=14)

            last_close = df['close'].iloc[-1]
            last_sma = df['sma_20'].iloc[-1]
            last_rsi = df['rsi'].iloc[-1]

            symbol_name = symbol.split(':')[0]

            # إشارة LONG
            if last_rsi < 32 and last_close > last_sma:
                sl = last_close * 0.985
                tp1 = last_close * 1.015
                tp2 = last_close * 1.030

                signal_msg = (
                    f"📈 **توصية عقود آجلة: LONG (شراء)**\n\n"
                    f"• **الزوج:** `{symbol_name}`\n"
                    f"• **سعر الدخول:** `${last_close:.2f}`\n"
                    f"• **الرافعة المالية:** `3x - 5x` (إدارة مخاطر حذرة)\n"
                    f"• **الهامش المقترح:** `$2 - $3` (لمحافظ $10-$20)\n\n"
                    f"🎯 **الهدف الأول:** `${tp1:.2f}`\n"
                    f"🎯 **الهدف الثاني:** `${tp2:.2f}`\n"
                    f"🛑 **وقف الخسارة:** `${sl:.2f}`\n\n"
                    f"⚠️ *التزم بوقف الخسارة للحد من المخاطر.*"
                )
                broadcast_message(signal_msg)

            # إشارة SHORT
            elif last_rsi > 68 and last_close < last_sma:
                sl = last_close * 1.015
                tp1 = last_close * 0.985
                tp2 = last_close * 0.970

                signal_msg = (
                    f"📉 **توصية عقود آجلة: SHORT (بيع)**\n\n"
                    f"• **الزوج:** `{symbol_name}`\n"
                    f"• **سعر الدخول:** `${last_close:.2f}`\n"
                    f"• **الرافعة المالية:** `3x - 5x` (إدارة مخاطر حذرة)\n"
                    f"• **الهامش المقترح:** `$2 - $3` (لمحافظ $10-$20)\n\n"
                    f"🎯 **الهدف الأول:** `${tp1:.2f}`\n"
                    f"🎯 **الهدف الثاني:** `${tp2:.2f}`\n"
                    f"🛑 **وقف الخسارة:** `${sl:.2f}`\n\n"
                    f"⚠️ *التزم بوقف الخسارة للحد من المخاطر.*"
                )
                broadcast_message(signal_msg)

        except Exception as e:
            print(f"Error analyzing {symbol}: {e}")


def main():
    print("تم تشغيل محرك البوت المحترف والتفعيل التلقائي 24/7...")
    offset = 0
    last_analysis_time = 0

    while True:
        offset = handle_telegram_updates(offset)
        check_blockchain_deposits()

        current_time = time.time()
        if current_time - last_analysis_time >= CHECK_INTERVAL:
            analyze_and_generate_signals()
            last_analysis_time = current_time

        time.sleep(2)


if __name__ == "__main__":
    main()
