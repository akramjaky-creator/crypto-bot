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

TOKEN = "8933033589:AAHQYl8c5YqisgwZGWajYC63c7rHevS0Ms0"
ADMIN_ID = 786668548
DEPOSIT_ADDRESS_BEP20 = "0xYourBnbSmartChainDepositAddressHere"
QR_CODE_URL = "https://i.ibb.co/3s8vJ8f/qr-code.jpg"
CHECK_INTERVAL = 300

PLANS = {
    "week": {"days": 7, "price": 15, "name": "أسبوع (15 USDT)"},
    "month": {"days": 30, "price": 50, "name": "شهر (50 USDT)"}
}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

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

def verify_bep20_transaction(tx_hash: str, expected_amount: float) -> tuple:
    tx_hash = tx_hash.strip()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return False, "❌ صيغة رقم الهاش (TxID) غير صحيحة. يجب أن يبدأ بـ 0x ويحتوي على 66 حرفاً."

    if is_tx_used(tx_hash):
        return False, "❌ هذا الهاش (TxID) تم استخدامه مسبقاً ولا يمكن إعادة تفعيل اشتراك به!"

    url = f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={tx_hash}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if "result" not in data or not data["result"]:
            return False, "❌ لم يتم العثور على المعاملة في شبكة BNB. تأكد من صحة الهاش أو انتظر حتى يتم تأكيدها."

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
            return False, "❌ لم يتم التأكد من تحويل عملة USDT (BEP20) المطلوبة بنجاح عبر هذا الهاش."

    except Exception as e:
        logging.error(f"Blockchain verification error: {e}")
        return False, "❌ حدث خطأ أثناء الاتصال بشبكة البلوكشين للتحقق. يرجى المحاولة لاحقاً."

exchange = ccxt.mexc({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

CANDIDATE_SYMBOLS = ['DOGE/USDT:USDT', 'XRP/USDT:USDT', 'SOL/USDT:USDT']

async def fetch_ohlcv(symbol, timeframe, limit=100):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        return ohlcv
    except Exception as e:
        logging.error(f"Error fetching {symbol} {timeframe}: {e}")
        return None

async def find_best_opportunity():
    best_symbol = None
    best_score = -1
    best_report = None

    for symbol in CANDIDATE_SYMBOLS:
        try:
            h1_raw = await fetch_ohlcv(symbol, '1h', 50)
            m5_raw = await fetch_ohlcv(symbol, '5m', 50)

            if not h1_raw or not m5_raw:
                continue

            df_h1 = pd.DataFrame(h1_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_m5 = pd.DataFrame(m5_raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            h1_close, h1_open = df_h1['close'].iloc[-1], df_h1['open'].iloc[-1]
            m5_close = df_m5['close'].iloc[-1]
            
            is_h1_long = h1_close > h1_open
            
            if is_h1_long:
                h1_strength = (h1_close - h1_open) / h1_open
                
                if h1_strength > best_score:
                    best_score = h1_strength
                    best_symbol = symbol
                    
                    clean_symbol = symbol.split('/')[0]
                    buy_tp1 = m5_close * 1.015
                    buy_tp2 = m5_close * 1.030
                    buy_sl = m5_close * 0.990

                    best_report = (
                        f"🚀 **فرصة صفقة شراء (LONG) مختارة بعناية!**\n"
                        f"📊 **العملة الأفضل:** {clean_symbol}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"⏱️ **الإطار الزمني للفتح:** فريم الساعة (1H) + فريم التنفيذ (5M)\n"
                        f"📈 **حالة شمعة الساعة:** صاعدة وقوية 🟢\n"
                        f"⚡ **سعر الدخول الحالي:** `{m5_close}`\n\n"
                        f"🎯 **مستويات الأهداف (Take Profit):**\n"
                        f"• الهدف الأول (TP1): `{buy_tp1:.4f}`\n"
                        f"• الهدف الثاني (TP2): `{buy_tp2:.4f}`\n\n"
                        f"🛑 **وقف الخسارة (Stop Loss):**\n"
                        f"• اغلق الصفقة فوراً إذا وصل السعر إلى: `{buy_sl:.4f}`\n\n"
                        f"⚖️ **الرافعة المالية المقترحة:** `10x - 20x`\n"
                        f"🕒 **التوقيت:** `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`"
                    )
        except Exception as e:
            logging.error(f"Error evaluating symbol {symbol}: {e}")
            continue

    if best_symbol:
        return True, best_report
    else:
        return False, "لا توجد فرص شراء (LONG) مطابقة للشروط على العملات المتاحة حالياً."

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
    last_sent_status = False
    
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

            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("الرجوع للقائمة", callback_data='main_menu')]])

            has_opportunity, report = await find_best_opportunity()
            
            if has_opportunity and not last_sent_status:
                for uid in active_users:
                    try:
                        await application.bot.send_message(
                            chat_id=uid,
                            text=report,
                            parse_mode="Markdown",
                            reply_markup=back_keyboard
                        )
                    except Exception as e:
                        logging.error(f"Error sending analysis to user {uid}: {e}")
                last_sent_status = True
            elif not has_opportunity:
                last_sent_status = False

            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Error in signal background loop: {e}")
            await asyncio.sleep(10)

def get_main_menu_keyboard(user_id):
    if is_user_active(user_id) or user_id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("📊 فحص السوق وإرسال أفضل صفقة الآن", callback_data='get_signal')],
            [InlineKeyboardButton("💳 تجديد أو تمديد الاشتراك", callback_data='choose_plan')],
            [InlineKeyboardButton("🆔 حالة الاشتراك والـ ID", callback_data='check_status')],
            [InlineKeyboardButton("📞 الدعم والإدارة", callback_data='support')]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("💳 اختيار باقة الاشتراك والإيداع الآلي", callback_data='choose_plan')],
            [InlineKeyboardButton("🆔 حالة الاشتراك والـ ID", callback_data='check_status')],
            [InlineKeyboardButton("📞 التواصل مع الدعم", callback_data='support')]
        ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    welcome_text = (
        f"🤖 أنا بوت لأصحاب رأس المال البسيط. أحلل السوق وأختار لك العملة الأفضل وأرسل صفقة الشراء (LONG) بدقة. "
        f"التزم معي بقواعد التوصية لكي أحافظ على رأس مالك ونقوم بخطة تكبير رأس مالك وجني الأرباح.\n\n"
        f"🆔 **الـ ID الخاص بك:** `{user_id}`\n"
        f"حالة الاشتراك: {'مفعل ونشط ✅' if is_user_active(user_id) or user_id == ADMIN_ID else 'منتهي أو غير فعال ❌'}"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=get_main_menu_keyboard(user_id), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'main_menu':
        menu_text = (
            f"🤖 أنا بوت لأصحاب رأس المال البسيط. أحلل السوق وأختار لك العملة الأفضل وأرسل صفقة الشراء (LONG) بدقة. "
            f"التزم معي بقواعد التوصية لكي أحافظ على رأس مالك ونقوم بخطة تكبير رأس مالك وجني الأرباح.\n\n"
            f"🆔 **الـ ID الخاص بك:** `{user_id}`\n"
            f"اختر من القائمة أدناه:"
        )
        try:
            await query.message.edit_text(menu_text, reply_markup=get_main_menu_keyboard(user_id), parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(menu_text, reply_markup=get_main_menu_keyboard(user_id), parse_mode="Markdown")
        return

    if not is_user_active(user_id) and query.data not in ['choose_plan', 'buy_week', 'buy_month', 'check_status', 'support'] and user_id != ADMIN_ID:
        await query.message.reply_text("❌ عذراً، اشتراكك منتهي. يرجى تجديد الاشتراك للوصول إلى الخدمات.")
        return

    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("الرجوع للقائمة", callback_data='main_menu')]])

    if query.data == 'get_signal':
        await query.message.reply_text("⏳ جاري فحص جميع العملات واختيار أقوى فرصة شراء (LONG) على فريم الساعة...")
        has_opp, report = await find_best_opportunity()
        await query.message.reply_text(report, parse_mode="Markdown", reply_markup=back_keyboard)

    elif query.data == 'choose_plan':
        keyboard = [
            [InlineKeyboardButton("📦 باقة أسبوع (15 USDT)", callback_data='buy_week')],
            [InlineKeyboardButton("📦 باقة شهر (50 USDT)", callback_data='buy_month')],
            [InlineKeyboardButton("الرجوع للقائمة", callback_data='main_menu')]
        ]
        text_plan = (
            "💳 **اختر باقة الاشتراك المناسبة لك:**\n"
            "• باقة الأسبوع: 15 دولار\n"
            "• باقة الشهر: 50 دولار\n\n"
            "بعد اختيار الباقة سيظهر لك العنوان والباركود لإتمام التحويل الآلي."
        )
        try:
            await query.message.edit_text(text_plan, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(text_plan, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif query.data in ['buy_week', 'buy_month']:
        plan_key = "week" if query.data == 'buy_week' else "month"
        plan = PLANS[plan_key]
        
        context.user_data['pending_plan'] = plan_key
        
        deposit_caption = (
            f"💳 **بيانات الإيداع الآلي لباقة {plan['name']}:**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌐 **الشبكة:** Binance Smart Chain (BNB - BEP20)\n"
            f"📍 **عنوان المحفظة:**\n`{DEPOSIT_ADDRESS_BEP20}`\n"
            f"💰 **المطلوب تحويله:** `{plan['price']} USDT`\n\n"
            f"📷 **امسح الباركود أعلاه للإيداع السريع عبر شبكة BNB.**\n"
            f"👇 **بعد إتمام التحويل الناجح، قم بإرسال رقم المعاملة (TxID Hash) مباشرة هنا في المحادثة للتفعيل الآلي الفوري.**"
        )
        try:
            await query.message.reply_photo(
                photo=QR_CODE_URL,
                caption=deposit_caption,
                parse_mode="Markdown",
                reply_markup=back_keyboard
            )
        except Exception:
            await query.message.reply_text(deposit_caption, parse_mode="Markdown", reply_markup=back_keyboard)

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
        try:
            await query.message.edit_text(status_text, parse_mode="Markdown", reply_markup=back_keyboard)
        except Exception:
            await query.message.reply_text(status_text, parse_mode="Markdown", reply_markup=back_keyboard)

    elif query.data == 'support':
        text_sup = "📞 للتواصل المباشر مع الدعم الفني: @AdminUsername"
        try:
            await query.message.edit_text(text_sup, reply_markup=back_keyboard)
        except Exception:
            await query.message.reply_text(text_sup, reply_markup=back_keyboard)

async def handle_tx_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text.startswith("0x") and len(text) == 66:
        pending_plan_key = context.user_data.get('pending_plan', 'month')
        plan = PLANS[pending_plan_key]
        
        await update.message.reply_text("🔍 جاري التحقق من شبكة البلوكشين وتأكيد وصول المعاملة...")
        
        is_valid, result = verify_bep20_transaction(text, plan['price'])
        
        if is_valid:
            db_save_tx(text, user_id, plan['price'])
            expiry_str = db_add_subscriber(user_id, plan['days'])
            
            context.user_data.pop('pending_plan', None)
            
            success_msg = (
                f"🎉 **تم اشتراكك بنجاح تام!**\n"
                f"📦 الباقة: `{plan['name']}`\n"
                f"💰 المبلغ المؤكد: `{plan['price']} USDT`\n"
                f"⏳ تاريخ انتهاء الاشتراك: `{expiry_str} UTC`\n\n"
                f"🚀 **ابقى مترقب إشعارات وتقارير البوت اللحظية الآلية!**"
            )
            back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("الرجوع للقائمة", callback_data='main_menu')]])
            await update.message.reply_text(success_msg, parse_mode="Markdown", reply_markup=back_keyboard)
        else:
            await update.message.reply_text(result, parse_mode="Markdown")
    else:
        if not is_user_active(user_id) and user_id != ADMIN_ID:
            await update.message.reply_text("❌ يرجى اختيار باقة الاشتراك وإرسال رقم المعاملة (TxID) الصحيح الذي يبدأ بـ 0x لتفعيل الحساب تلقائياً.")

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tx_message))

    async def post_init(app):
        asyncio.create_task(send_scheduled_signals(app))
        asyncio.create_task(check_subscriptions_background(app))

    application.post_init = post_init
    
    print("Bot is fully running with best-opportunity selection and hourly long configuration...")
    application.run_polling()

if __name__ == '__main__':
    main()
