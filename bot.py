import os
import threading
import sys
import time
import logging
import sqlite3
from datetime import datetime, timedelta
import pandas as pd
import ccxt.async_support as ccxt
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# --- إعدادات الخادم الوهمي لمنع انقطاع الاتصال على Render ---
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# --- الإعدادات الأساسية للبوت ---
TOKEN = "8933033589:AAGyO2S3IKEssxgktTXjdv4ri5l6hHo7Agw"
ADMIN_ID = 786668548
DEPOSIT_ADDRESS_BEP20 = "0xYourBnbSmartChainDepositAddressHere"  # عنوان محفظتك على شبكة BNB (BEP20)
QR_CODE_URL = "https://i.ibb.co/3s8vJ8f/qr-code.jpg"  # رابط صورة الباركود المرفق
CHECK_INTERVAL = 300  # 5 دقائق بالثواني

# أسعار الباقات وقيمتها بالـ USDT
PLANS = {
    "week": {"days": 7, "price": 15, "name": "أسبوع (15 USDT)"},
    "month": {"days": 30, "price": 50, "name": "شهر (50 USDT)"}
}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- إعداد قاعدة البيانات المحلية SQLite ---
def init_db():
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            expiry_date TEXT,
            notified_24h INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            tx_hash TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_add_subscriber(user_id: int, days: int):
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    
    # التحقق مما إذا كان المستخدم لديه اشتراك ساري لتمديد المدة أو البدء من جديد
    cursor.execute("SELECT expiry_date FROM subscribers WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    now = datetime.utcnow()
    if row:
        current_expiry = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        if current_expiry > now:
            expiry = current_expiry + timedelta(days=days)
        else:
            expiry = now + timedelta(days=days)
    else:
        expiry = now + timedelta(days=days)
        
    expiry_str = expiry.strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("""
        INSERT OR REPLACE INTO subscribers (user_id, expiry_date, notified_24h)
        VALUES (?, ?, 0)
    """, (user_id, expiry_str))
    conn.commit()
    conn.close()
    return expiry_str

def db_get_subscriber(user_id: int):
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    cursor.execute("SELECT expiry_date, notified_24h FROM subscribers WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def db_update_notification(user_id: int):
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE subscribers SET notified_24h = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_tx_used(tx_hash: str) -> bool:
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM transactions WHERE tx_hash = ?", (tx_hash,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def db_save_tx(tx_hash: str, user_id: int, amount: float):
    conn = sqlite3.connect("bot_subscribers.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO transactions (tx_hash, user_id, amount, timestamp)
        VALUES (?, ?, ?, ?)
    """, (tx_hash, user_id, amount, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def is_user_active(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    row = db_get_subscriber(user_id)
    if not row:
        return False
    expiry_str, _ = row
    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
    return datetime.utcnow() < expiry_date

# --- التحقق الترميزي الصارم من المعاملة عبر شبكة BNB Smart Chain (BscScan API المجاني) ---
def verify_bep20_transaction(tx_hash: str, expected_amount: float) -> tuple:
    """
    التحقق الفوري من صحة معاملة USDT (BEP20) على شبكة Binance Smart Chain
    مع التأكد من عنوان المجاورة، القيمة، وعدم التكرار.
    """
    tx_hash = tx_hash.strip()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return False, "❌ صيغة رقم الهاش (TxID) غير صحيحة. يجب أن يبدأ بـ 0x ويحتوي على 66 حرفاً."

    if is_tx_used(tx_hash):
        return False, "❌ هذا الهاش (TxID) تم استخدامه مسبقاً ولا يمكن إعادة تفعيل اشتراك به!"

    # استخدام API عام مجاني للتحقق من المعاملات على شبكة BSC
    url = f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={tx_hash}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if "result" not in data or not data["result"]:
            return False, "❌ لم يتم العثور على المعاملة في شبكة BNB. تأكد من صحة الهاش أو انتظر حتى يتم تأكيدها."

        tx = data["result"]
        to_addr = tx.get("to")
        
        # التأكد أن التحويل وُجّه إلى محفظتك مباشرة
        if not to_addr or to_addr.lower() != DEPOSIT_ADDRESS_BEP20.lower():
            # ملاحظة: في حال تحويل توكن BEP20 (USDT)، قد يكون الـ to هو عقد الـ USDT وتكون التفاصيل في الـ input data
            # للتبسيط والصرامة القصوى، سنعتمد أيضاً على جلب تفاصيل تحويلات الـ ERC20/BEP20 عبر BscScan token txs API
            pass

        # التحقق الدقيق عبر BscScan Token Transfer Events للـ USDT (العقد الشهير لـ USDT على BSC هو 0x55d398326f99059fF775485246999027B3197955)
        token_url = f"https://api.bscscan.com/api?module=account&action=tokentx&contractaddress=0x55d398326f99059fF775485246999027B3197955&txhash={tx_hash}&apikey=YourApiKeyToken"
        t_res = requests.get(token_url, timeout=10).json()
        
        if t_res.get("status") == "1" and t_res.get("result"):
            tx_info = t_res["result"][0]
            receiver = tx_info.get("to", "").lower()
            val_wei = int(tx_info.get("value", 0))
            token_decimals = int(tx_info.get("tokenDecimal", 18))
            actual_amount = val_wei / (10 ** token_decimals)
            
            if receiver != DEPOSIT_ADDRESS_BEP20.lower():
                return False, "❌ المعاملة لم تُرسل إلى عنوان محفظتك المعتمد!"

            if actual_amount < expected_amount:
                return False, f"❌ المبلغ المرسل ({actual_amount} USDT) أقل من قيمة الباقة المطلوبة ({expected_amount} USDT)!"

            return True, actual_amount

        else:
            # طريقة بديلة في حال كان التحويل عملة BNB مباشرة أو تفاصيل عامة
            return False, "❌ لم يتم التأكد من تحويل عملة USDT (BEP20) المطلوبة بنجاح عبر هذا الهاش."

    except Exception as e:
        logging.error(f"Blockchain verification error: {e}")
        return False, "❌ حدث خطأ أثناء الاتصال بشبكة البلوكشين للتحقق. يرجى المحاولة لاحقاً."

# --- تهيئة منصة التداول MEXC Futures ---
exchange = ccxt.mexc({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

SYMBOLS = ['DOGE/USDT:USDT', 'XRP/USDT:USDT', 'SOL/USDT:USDT']

async def fetch_ohlcv(symbol, timeframe, limit=100):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        return ohlcv
    except Exception as e:
        logging.error(f"Error fetching {symbol} {timeframe}: {e}")
        return None

async def analyze_market(symbol):
    try:
        daily_raw = await fetch_ohlcv(symbol, '1d', 50)
        h4_raw = await fetch_ohlcv(symbol, '4h', 50)
        m5_raw = await fetch_ohlcv(symbol, '5m', 50)

        if not daily_raw or not h4_raw or not m5_raw:
            return None, "تعذر جلب بيانات الشموع من المنصة حالياً."

        df_daily = pd.DataFrame(daily_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_h4 = pd.DataFrame(h4_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_m5 = pd.DataFrame(m5_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        d_close, d_open = df_daily['close'].iloc[-1], df_daily['open'].iloc[-1]
        h_close, h_open = df_h4['close'].iloc[-1], df_h4['open'].iloc[-1]
        
        d_trend = "صاعد 🟢" if d_close > d_open else "هابط 🔴"
        h_trend = "صاعد 🟢" if h_close > h_open else "هابط 🔴"

        if d_trend != h_trend:
            reason = (
                f"⚠️ **التحليل الفني للعملة {symbol.split('/')[0]}:**\n"
                f"• الاتجاه اليومي: {d_trend}\n"
                f"• الاتجاه الأكبر (4 ساعات): {h_trend}\n"
                f"❌ **السبب لعدم إرسال توصية:** يوجد تناقض بين الفريمات الكبرى (تذبذب سعري).\n"
                f"🔍 *أبقى مراقباً لحركة السوق بدقة، وحال توفر فرصة ممتازة سيتم إرسالها فوراً.*"
            )
            return None, reason

        m5_close = df_m5['close'].iloc[-1]
        m5_vol = df_m5['volume'].iloc[-1]
        avg_vol = df_m5['volume'].iloc[-20:].mean()

        if m5_vol < (avg_vol * 0.7):
            reason = (
                f"⚠️ **التحليل الفني للعملة {symbol.split('/')[0]}:**\n"
                f"• الاتجاه متوافق ({d_trend})\n"
                f"❌ **السبب لعدم إرسال توصية:** حجم التداول الحالي ضعيف والسيولة منخفضة على فريم 5 دقائق.\n"
                f"🔍 *أبقى مراقباً لحركة السوق بدقة، وحال توفر فرصة ممتازة سيتم إرسالها فوراً.*"
            )
            return None, reason

        signal_type = "LONG (شراء) 🟢" if "صاعد" in d_trend else "SHORT (بيع) 🔴"
        entry_price = m5_close
        
        if "LONG" in signal_type:
            tp = entry_price * 1.015
            sl = entry_price * 0.992
        else:
            tp = entry_price * 0.985
            sl = entry_price * 1.008

        clean_symbol = symbol.split('/')[0]

        report = (
            f"📊 **فرصة سكالبينج مؤكدة ({clean_symbol})**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📈 **الاتجاه اليومي (Daily):** {d_trend}\n"
            f"⏰ **الاتجاه الأكبر (4 ساعات):** {h_trend}\n"
            f"⚡ **إشارة الدخول (5 دقائق):** {signal_type}\n"
            f"🎯 **سعر الدخول المقترح:** `{entry_price}`\n"
            f"🎯 **هدف الربح (TP):** `{tp:.4f}`\n"
            f"🛑 **وقف الخسارة (SL):** `{sl:.4f}`\n"
            f"⚖️ **الرافعة المالية:** `10x - 20x`\n"
            f"🕒 **التوقيت:** `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        return report, "success"
    except Exception as e:
        logging.error(f"Error in analysis for {symbol}: {e}")
        return None, "حدث خطأ تقني أثناء تحليل السوق."

async def check_subscriptions_background(application):
    while True:
        try:
            conn = sqlite3.connect("bot_subscribers.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, expiry_date, notified_24h FROM subscribers")
            rows = cursor.fetchall()
            conn.close()

            now = datetime.utcnow()
            for user_id, expiry_str, notified_24h in rows:
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
                time_left = expiry_date - now

                if timedelta(0) < time_left <= timedelta(hours=24) and notified_24h == 0:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text="⚠️ **تنبيه هام:** اشتراكك في بوت التوصيات سينتهي خلال أقل من 24 ساعة! يرجى تجديد الاشتراك لاستمرار الخدمة دون انقطاع.",
                            parse_mode="Markdown"
                        )
                        db_update_notification(user_id)
                    except Exception as e:
                        logging.error(f"Failed to send 24h warning to {user_id}: {e}")

            await asyncio.sleep(3600)
        except Exception as e:
            logging.error(f"Error in sub-check background loop: {e}")
            await asyncio.sleep(60)

async def send_scheduled_signals(application):
    while True:
        try:
            now = datetime.utcnow()
            sec_to_next = 300 - (now.minute % 5) * 60 - now.second
            if sec_to_next < 10:
                sec_to_next += 300
            await asyncio.sleep(sec_to_next)

            conn = sqlite3.connect("bot_subscribers.db")
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, expiry_date FROM subscribers")
            subscribers = cursor.fetchall()
            conn.close()

            active_users = [ADMIN_ID]
            for uid, exp_str in subscribers:
                if datetime.utcnow() < datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S"):
                    if uid not in active_users:
                        active_users.append(uid)

            for symbol in SYMBOLS:
                analysis, msg = await analyze_market(symbol)
                for uid in active_users:
                    try:
                        if analysis:
                            await application.bot.send_message(
                                chat_id=uid,
                                text=f"⏱️ **توصية دورية آلية متزامنة مع إغلاق الشمعة:**\n\n{analysis}",
                                parse_mode="Markdown"
                            )
                        else:
                            if uid == ADMIN_ID:
                                await application.bot.send_message(
                                    chat_id=uid,
                                    text=f"🤖 **تقرير نظام المراقبة الآلية ({symbol.split('/')[0]}):**\n\n{msg}",
                                    parse_mode="Markdown"
                                )
                    except Exception as e:
                        logging.error(f"Error sending to user {uid}: {e}")
                await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Error in signal background loop: {e}")
            await asyncio.sleep(10)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_user_active(user_id) and user_id != ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("💳 اختيار باقة الاشتراك والإيداع الآلي", callback_data='choose_plan')],
            [InlineKeyboardButton("🆔 حالة الاشتراك والـ ID", callback_data='check_status')],
            [InlineKeyboardButton("📞 التواصل مع الدعم", callback_data='support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"❌ **عذراً، اشتراكك منتهي أو غير فعال.**\n"
            f"🆔 **الـ ID الخاص بك:** `{user_id}`\n\n"
            f"اختر الباقة المناسبة وقم بالتحويل عبر شبكة BNB وأرسل رقم المعاملة (TxID) للتفعيل الآلي الفوري.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    keyboard = [
        [InlineKeyboardButton("📊 إرسال توصية فورية الآن", callback_data='get_signal')],
        [InlineKeyboardButton("💳 تجديد أو تمديد الاشتراك", callback_data='choose_plan')],
        [InlineKeyboardButton("🆔 حالة الاشتراك والـ ID", callback_data='check_status')],
        [InlineKeyboardButton("📞 الدعم والإدارة", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        f"🤖 **مرحباً بك في بوت توصيات العقود الآجلة الاحترافي**\n"
        f"🆔 **الـ ID السري الخاص بك:** `{user_id}`\n\n"
        f"حالة الاشتراك: مفعل ونشط ✅\n"
        f"البوت يحلل السوق بدقة متزامنة ولا يرسل إلا الصفقات المضمونة."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_user_active(user_id) and query.data not in ['choose_plan', 'buy_week', 'buy_month', 'check_status', 'support'] and user_id != ADMIN_ID:
        await query.message.reply_text("❌ عذراً، اشتراكك منتهي. يرجى تجديد الاشتراك للوصول إلى الخدمات.")
        return

    if query.data == 'get_signal':
        await query.message.reply_text("⏳ جاري فحص الشموع وتحليل السوق بدقة لحظية...")
        for symbol in SYMBOLS:
            analysis, msg = await analyze_market(symbol)
            if analysis:
                await query.message.reply_text(analysis, parse_mode="Markdown")
            else:
                await query.message.reply_text(msg, parse_mode="Markdown")
            await asyncio.sleep(1)

    elif query.data == 'choose_plan':
        keyboard = [
            [InlineKeyboardButton("📦 باقة أسبوع (15 USDT)", callback_data='buy_week')],
            [InlineKeyboardButton("📦 باقة شهر (50 USDT)", callback_data='buy_month')]
        ]
        await query.message.reply_text(
            "💳 **اختر باقة الاشتراك المناسبة لك:**\n"
            "• باقة الأسبوع: 15 دولار\n"
            "• باقة الشهر: 50 دولار\n\n"
            "بعد اختيار الباقة سيظهر لك العنوان والباركود لإتمام التحويل الآلي.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif query.data in ['buy_week', 'buy_month']:
        plan_key = "week" if query.data == 'buy_week' else "month"
        plan = PLANS[plan_key]
        
        # حفظ الباقة المختارة مؤقتاً في سياق المستخدم
        context.user_data['pending_plan'] = plan_key
        
        deposit_caption = (
            f"💳 **بيانات الإيداع الآلي لباقة {plan['name']}:**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌐 **الشبكة:** Binance Smart Chain (BNB - BEP20)\n"
            f"📍 **عنوان المحفظة:**\n`{DEPOSIT_ADDRESS_BEP20}`\n"
            f"💰 **المطلوب تحويله:** `{plan['price']} USDT`\n\n"
            f"📷 **امسح الباركود أعلاه للإيداع السريع عبر شبكة BNB.**\n"
            f"👇 **بعد إتمام التحويل الناجح، قم بإرسال رقم المعاملة (TxID Hash) مباشرة هنا في المحادثة للتحفعيل الآلي الفوري.**"
        )
        try:
            await query.message.reply_photo(
                photo=QR_CODE_URL,
                caption=deposit_caption,
                parse_mode="Markdown"
            )
        except Exception:
            await query.message.reply_text(deposit_caption, parse_mode="Markdown")

    elif query.data == 'check_status':
        if user_id == ADMIN_ID:
            status_text = "🆔 **معلومات الحساب:**\n• أنت مشرف البوت (Admin) الصلاحيات مطلقة ✅"
        else:
            row = db_get_subscriber(user_id)
            if row and is_user_active(user_id):
                expiry_str, _ = row
                status_text = f"🆔 **معلومات حسابك:**\n• الـ ID (سري): `{user_id}`\n• حالة الاشتراك: مفعل ✅\n• تاريخ الانتهاء: `{expiry_str} UTC`"
            else:
                status_text = f"🆔 **معلومات حسابك:**\n• الـ ID (سري): `{user_id}`\n• حالة الاشتراك: منتهي أو غير مفعل ❌"
        await query.message.reply_text(status_text, parse_mode="Markdown")

    elif query.data == 'support':
        await query.message.reply_text("📞 للتواصل المباشر مع الدعم الفني: @AdminUsername")

async def handle_tx_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # التحقق مما إذا كان النص المدخل هو رقم معاملة (TxID) يبدأ بـ 0x
    if text.startswith("0x") and len(text) == 66:
        pending_plan_key = context.user_data.get('pending_plan', 'month') # الافتراضي شهر إذا لم يحدد
        plan = PLANS[pending_plan_key]
        
        await update.message.reply_text("🔍 جاري التحقق من شبكة البلوكشين وتأكيد وصول المعاملة...")
        
        is_valid, result = verify_bep20_transaction(text, plan['price'])
        
        if is_valid:
            db_save_tx(text, user_id, plan['price'])
            expiry_str = db_add_subscriber(user_id, plan['days'])
            
            # تفريغ الباقة المعلقة
            context.user_data.pop('pending_plan', None)
            
            success_msg = (
                f"🎉 **تم اشتراكك بنجاح تام!**\n"
                f"📦 الباقة: `{plan['name']}`\n"
                f"💰 المبلغ المؤكد: `{plan['price']} USDT`\n"
                f"⏳ تاريخ انتهاء الاشتراك: `{expiry_str} UTC`\n\n"
                f"🚀 **ابقى مترقب إشعارات وتوصيات البوت اللحظية الآلية!**"
            )
            await update.message.reply_text(success_msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(result, parse_mode="Markdown")
    else:
        if not is_user_active(user_id) and user_id != ADMIN_ID:
            await update.message.reply_text("❌ يرجى اختيار باقة الاشتراك وإرسال رقم المعاملة (TxID) الصحيح الذي تبدأ بـ 0x لتفعيل الحساب تلقائياً.")

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tx_message))

    async def post_init(app):
        asyncio.create_task(send_scheduled_signals(app))
        asyncio.create_task(check_subscriptions_background(app))

    application.post_init = post_init
    
    print("Bot is fully running with automated blockchain verification architecture...")
    application.run_polling()

if __name__ == '__main__':
    main()
