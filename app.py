import logging
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = 455774531
YOUR_WEB_APP_URL = "https://birdnesttgminiapp.web.app/"

logging.basicConfig(level=logging.INFO)

# ------------------ Bot Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    button = KeyboardButton("🍽️ Open Order menu", web_app=WebAppInfo(url=YOUR_WEB_APP_URL))
    await update.message.reply_text(
        "Welcome to Bird Nest House! 🥚\nClick the button below to place your order:",
        reply_markup=ReplyKeyboardMarkup([[button]], resize_keyboard=True)
    )

async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.web_app_data:
        await update.message.reply_text("No order data received.")
        return
    
    raw_data = update.message.web_app_data.data
    logging.info(f"Raw order data: {raw_data}")
    
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON: {e}")
        await update.message.reply_text("Error processing order.")
        return
    
    # Extract fields with fallbacks
    user_id = data.get('userId', 'Unknown')
    user_name = data.get('userName', 'Guest')
    total = data.get('total', '0.00')
    items = data.get('items', [])
    
    # Log to console for debugging
    logging.info(f"Order from {user_name} (ID: {user_id}): {len(items)} items, total ${total}")
    
    # Build a readable order summary for the seller
    items_text = "\n".join([f"  • {item.get('name', '?')} - ${item.get('price', 0)}" for item in items])
    order_summary = (
        f"🆕 New order from {user_name}\n"
        f"🆔 User ID: {user_id}\n"
        f"📦 Items:\n{items_text}\n"
        f"💰 Total: ${total}"
    )
    
    # Send to seller
    await context.bot.send_message(chat_id=SELLER_CHAT_ID, text=order_summary)
    await update.message.reply_text("✅ Order received! We'll notify you when it's ready.")

# ------------------ Bot Polling ------------------
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    print("🤖 Bot started polling...")
    app.run_polling()

# ------------------ HTTP Health Check Server ------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http():
    server = HTTPServer(('0.0.0.0', 10000), HealthHandler)
    server.serve_forever()

# ------------------ Start Both ------------------
if __name__ == "__main__":
    # Start HTTP server in background thread (keeps Render web service alive)
    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()
    
    # Start the bot in the main thread
    run_bot()