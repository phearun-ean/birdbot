import logging
import json
import os
from datetime import datetime
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = 455774531
YOUR_WEB_APP_URL = "https://birdnesttgminiapp.web.app/"

logging.basicConfig(level=logging.INFO)

async def start(update, context):
    button = KeyboardButton("🍽️ Open Order menu", web_app=WebAppInfo(url=YOUR_WEB_APP_URL))
    await update.message.reply_text("Welcome!", reply_markup=ReplyKeyboardMarkup([[button]], resize_keyboard=True))

async def handle_order(update, context):
    if not update.message.web_app_data:
        return
    data = json.loads(update.message.web_app_data.data)
    # Simplified: just log and notify seller
    logging.info(f"Order from {data.get('userName')}: ${data.get('total')}")
    await context.bot.send_message(chat_id=SELLER_CHAT_ID, text=f"New order from {data.get('userName')}!\nTotal: ${data.get('total')}")
    await update.message.reply_text("Order received! ✅")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    print("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()

    from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http():
    server = HTTPServer(('0.0.0.0', 10000), HealthHandler)
    server.serve_forever()

# Start HTTP server in a background thread
threading.Thread(target=run_http, daemon=True).start()

# Start the bot in the main thread
main()