import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# خادم ويب وهمي لإرضاء Render ومنع خطأ الـ Timed out
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    server.serve_forever()

# تشغيل الخادم الوهمي في الخلفية
threading.Thread(target=run_server, daemon=True).start()
import sys
import time
from datetime import datetime, timedelta
import ccxt
import pandas as pd
import requests

import asyncio
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
import ccxt.async_support as ccxt

# إعدادات البوت الأساسية
TOKEN = "8933033589:AAGy02S3IKEssxgktTXjdv4ri5l6hHo7Agw"
ADMIN_ID = 786668548
CHECK_INTERVAL = 300  # 5 دقائق بالثواني

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# تهيئة منصة Binance Futures عبر CCXT
exchange = ccxt.binance({
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

def analyze_trend(daily_data, h4_data, m5_data):
    if not daily_data or not h4_data or not m5_data:
        return "بيانات السوق غير متوفرة حالياً"
    
    daily_close = daily_data[-1][4]
    daily_open = daily_data[-1][1]
    daily_trend = "صاعد 📈" if daily_close > daily_open else "هابط 📉"
    
    h4_close = h4_data[-1][4]
    h4_open = h4_data[-1][1]
    h4_trend = "صاعد 📈" if h4_close > h4_open else "هابط 📉"
    
    m5_close = m5_data[-1][4]
    m5_high = max([x[2] for x in m5_data[-5:]])
    m5_low = min([x[3] for x in m5_data[-5:]])
    
    signal_type = "LONG (شراء) 🟢" if daily_trend.startswith("صاعد") and h4_trend.startswith("صاعد") else "SHORT (بيع) 🔴"
    entry_price = m5_close
    leverage = "5x - 10x"
    
    report = (
        f"📊 **تحليل العقود الآجلة (Binance Futures)**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• الاتجاه اليومي (Daily): {daily_trend}\n"
        f"• اتجاه التأكيد (4 ساعات): {h4_trend}\n"
        f"• فريم التنفيذ (5 دقائق): نشط\n\n"
        f"🎯 **التوصية المقترحة:**\n"
        f"• الإشارة: {signal_type}\n"
        f"• سعر الدخول: `{entry_price}`\n"
        f"• الرافعة المالية المقترحة: `{leverage}`\n"
        f"• نطاق الدعم/المقاومة: `{m5_low} - {m5_high}`\n"
    )
    return report

async def generate_all_analyses():
    results = []
    for symbol in SYMBOLS:
        daily = await fetch_ohlcv(symbol, '1d', 30)
        h4 = await fetch_ohlcv(symbol, '4h', 30)
        m5 = await fetch_ohlcv(symbol, '5m', 50)
        
        analysis = analyze_trend(daily, h4, m5)
        coin_name = symbol.split('/')[0]
        results.append(f"🪙 **العملة: {coin_name}**\n{analysis}\n")
    return "\n".join(results)

async def background_loop(application):
    while True:
        try:
            market_reports = await generate_all_analyses()
            message = f"⏱ **توصية دورية آلية (تحديث كل 5 دقائق):**\n\n{market_reports}"
            await application.bot.send_message(chat_id=ADMIN_ID, text=message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error in background loop: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 إرسال توصية فورية الآن", callback_data="instant_analysis")],
        [InlineKeyboardButton("💳 خطط الاشتراك والإيداع", callback_data="plans")],
        [InlineKeyboardButton("🆔 حالة الاشتراك والـ ID", callback_data="status")],
        [InlineKeyboardButton("📞 الدعم والإدارة", callback_data="support")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_id = update.effective_user.id
    welcome_text = (
        f"🤖 مرحباً بك في بوت توصيات العقود الآجلة\n🆔 الـ ID الخاص بك: {user_id}\n\n"
        "البوت يقوم بتحليل (DOGE, XRP, SOL) عبر الفريمات (يومي + 4 ساعات + 5 دقائق) ويرسل توصيات تلقائية كل 5 دقائق."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "instant_analysis":
        await query.message.reply_text("🔄 جاري سحب البيانات الفورية من بايننس وتحليل (DOGE, XRP, SOL)...")
        reports = await generate_all_analyses()
        await query.message.reply_text(f"⚡ **التوصية الفورية:**\n\n{reports}", parse_mode="Markdown")
    elif query.data == "plans":
        plans_text = (
            "💳 **خطط الاشتراك وعنوان الإيداع:**\n\n"
            "• شبكة USDT (TRC20):\n`TYyourDepositAddressHere`\n\n"
            "أرسل قيمة الاشتراك ثم اضغط على تحقق أو تواصل مع الدعم."
        )
        await query.message.reply_text(plans_text, parse_mode="Markdown")
    elif query.data == "status":
        status_text = (
            f"🆔 بيانات الحساب:\n• الـ ID الخاص بك: {query.from_user.id}\n"
            f"• حالة الاشتراك: مفعل ✅\n• انتهاء الاشتراك: 2099-01-01 00:00"
        )
        await query.message.reply_text(status_text)
    elif query.data == "support":
        await query.message.reply_text("📞 للتواصل والدعم الفني: @AdminUsername")

def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    async def post_init(app):
        asyncio.create_task(background_loop(app))

    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()

                              
