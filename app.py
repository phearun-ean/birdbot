# version 1.0.1 – async fixed
import logging
import json
import os
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = os.getenv("SELLER_CHAT_ID")

logging.basicConfig(level=logging.INFO)

# Flask app
app = Flask(__name__)

# Telegram application (no polling, we'll use webhook)
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Store orders (temporary; for production use a database)
order_storage = {}

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to Bird Nest House! Use the menu button to order.")

async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer_chat_id = update.effective_chat.id
    data = json.loads(update.message.web_app_data.data)
    
    user_id = data.get('userId')
    user_name = data.get('userName')
    
    order_storage[user_id] = {
        'chat_id': buyer_chat_id,
        'user_name': user_name,
        'order_id': f"ORD_{user_id}_{int(datetime.now().timestamp())}"
    }
    
    items_text = "\n".join([f"  • {i['name']} - ${i['price']}" for i in data['items']])
    order_text = (
        f"🆕 <b>NEW ORDER!</b>\n\n"
        f"👤 <b>Customer:</b> {user_name}\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"📦 <b>Items:</b>\n{items_text}\n\n"
        f"💰 <b>Total:</b> ${data['total']}\n"
        f"⭐ <b>Points Earned:</b> {data['points']}\n"
        f"🕐 <b>Time:</b> {data.get('timestamp', 'N/A')}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user_id}")],
        [InlineKeyboardButton("✅ Mark Ready", callback_data=f"ready_{user_id}")]
    ])
    
    await telegram_app.bot.send_message(chat_id=SELLER_CHAT_ID, text=order_text, parse_mode="HTML", reply_markup=keyboard)
    await update.message.reply_text(f"✅ Order confirmed, {user_name}! Thank you.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[1]
    if user_id in order_storage:
        if query.data.startswith("reply_"):
            context.user_data['reply_to'] = user_id
            await query.message.reply_text(f"Send your reply to {order_storage[user_id]['user_name']}:")
        elif query.data.startswith("ready_"):
            buyer_id = order_storage[user_id]['chat_id']
            await telegram_app.bot.send_message(chat_id=buyer_id, text="🍽️ Your order is ready for pickup!")
            await query.message.reply_text("Notification sent.")
    await query.edit_message_reply_markup(reply_markup=None)

async def forward_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'reply_to' not in context.user_data:
        return
    user_id = context.user_data['reply_to']
    buyer_id = order_storage[user_id]['chat_id']
    await telegram_app.bot.send_message(chat_id=buyer_id, text=f"📨 Message from shop: {update.message.text}")
    await update.message.reply_text("Reply sent.")
    del context.user_data['reply_to']

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_reply))

# ---------- Flask Webhook Endpoint ----------
@app.route('/webhook', methods=['POST'])
async def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    await telegram_app.process_update(update)
    return jsonify({"ok": True})

@app.route('/')
def index():
    return "Bot is running!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)