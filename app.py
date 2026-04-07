import logging
import json
import os
import threading
import asyncio
from datetime import datetime
from typing import Dict, Any
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
    InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonDefault
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from flask import Flask, request, session, redirect, url_for, send_from_directory
import secrets
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = 455774531
YOUR_WEB_APP_URL = "https://birdnesttgminiapp.web.app/"

logging.basicConfig(level=logging.INFO)

# ---------- Flask app ----------
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))
ADMIN_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'change_this_password')

@app.route('/')
def health():
    return "Bot is running", 200

@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['dashboard_auth'] = True
            return redirect(url_for('dashboard'))
        else:
            return "<h2>Wrong password</h2><a href='/dashboard'>Try again</a>", 401
    if not session.get('dashboard_auth'):
        return '''
            <!DOCTYPE html>
            <html>
            <head><title>Dashboard Login</title></head>
            <body style="font-family: sans-serif; text-align: center; margin-top: 100px;">
                <form method="post">
                    <label>Password: <input type="password" name="password" required></label>
                    <button type="submit">Login</button>
                </form>
            </body>
            </html>
        '''
    return send_from_directory('.', 'dashboard.html')

# ---------- Persistent order storage ----------
ORDERS_FILE = "orders.json"

def load_orders() -> Dict[str, Any]:
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load orders: {e}")
    return {}

def save_orders(orders: Dict[str, Any]) -> None:
    try:
        with open(ORDERS_FILE, 'w') as f:
            json.dump(orders, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save orders: {e}")

order_storage = load_orders()

# ---------- Bot Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    button = KeyboardButton("🍽️ Open Order menu", web_app=WebAppInfo(url=YOUR_WEB_APP_URL))
    await update.message.reply_text(
        "Welcome to Bird Nest House! 🥚\nClick the button below to place your order:",
        reply_markup=ReplyKeyboardMarkup([[button]], resize_keyboard=True)
    )

async def reset_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.set_chat_menu_button(
        chat_id=update.effective_chat.id,
        menu_button=MenuButtonDefault()
    )
    await update.message.reply_text("✅ Persistent menu button removed. Use the keyboard button.")

async def close_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'reply_to_order' in context.user_data:
        order_id = context.user_data['reply_to_order']
        del context.user_data['reply_to_order']
        await update.message.reply_text(f"✅ Chat for order {order_id} closed.")
    else:
        await update.message.reply_text("No active chat to close.")

async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🔔 handle_order triggered")
    if not update.message:
        print("❌ No message object")
        return
    if not update.message.web_app_data:
        print("❌ No web_app_data in message")
        await update.message.reply_text("No order data received. Please use the 'Open Order menu' button.")
        return

    raw_data = update.message.web_app_data.data
    print(f"📦 Raw order data: {raw_data}")

    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON: {e}")
        await update.message.reply_text("Error processing order.")
        return

    user_id = str(data.get('userId', 'Unknown'))
    user_name = data.get('userName', 'Guest')
    username = data.get('username', '')
    first_name = data.get('firstName', '')
    last_name = data.get('lastName', '')
    items = data.get('items', [])
    total = data.get('total', '0.00')
    points = data.get('points', 0)
    timestamp = data.get('timestamp', 'N/A')

    print(f"✅ Order from {user_name} (ID: {user_id}) - ${total}")

    order_id = f"ORD_{user_id}_{int(datetime.now().timestamp())}"
    buyer_chat_id = update.effective_chat.id

    order_storage[order_id] = {
        'chat_id': buyer_chat_id,
        'user_id': user_id,
        'user_name': user_name,
        'username': username,
        'first_name': first_name,
        'last_name': last_name,
        'timestamp': timestamp,
        'total': total,
        'points': points,
        'items': items
    }
    save_orders(order_storage)

    customer_info = f"👤 <b>Customer:</b> {user_name}\n"
    if username:
        customer_info += f"🆔 <b>Username:</b> @{username}\n"
    customer_info += f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
    if first_name:
        customer_info += f"📛 <b>First Name:</b> {first_name}\n"
    if last_name:
        customer_info += f"📛 <b>Last Name:</b> {last_name}\n"

    items_text = "\n".join([f"  • {item.get('name', '?')} - ${item.get('price', 0)}" for item in items])
    order_text = (
        f"🆕 <b>NEW ORDER!</b>\n\n{customer_info}\n"
        f"📦 <b>Items:</b>\n{items_text}\n"
        f"💰 <b>Total:</b> ${total}\n"
        f"⭐ <b>Points Earned:</b> {points}\n"
        f"🕐 <b>Time:</b> {timestamp}\n"
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to Customer", callback_data=f"reply_{order_id}")],
        [InlineKeyboardButton("✅ Mark as Ready", callback_data=f"ready_{order_id}")]
    ])

    await context.bot.send_message(
        chat_id=SELLER_CHAT_ID,
        text=order_text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await update.message.reply_text(
        f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
        f"Total: ${total}\nYou earned {points} loyalty points 🎉\n\n"
        f"We'll notify you when your order is ready.",
        parse_mode="HTML"
    )
    logging.info(f"Order {order_id} from {user_name} - ${total}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("reply_"):
        order_id = data.split("_", 1)[1]
        if order_id in order_storage:
            context.user_data['reply_to_order'] = order_id
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✏️ Chat with customer (Order {order_id}). Send any message.\nTo close chat, use /closechat"
            )
        else:
            await query.message.reply_text("⚠️ Order not found.")
    elif data.startswith("ready_"):
        order_id = data.split("_", 1)[1]
        if order_id in order_storage:
            buyer_chat_id = order_storage[order_id]['chat_id']
            user_name = order_storage[order_id]['user_name']
            try:
                await context.bot.send_message(
                    chat_id=buyer_chat_id,
                    text=f"🍽️ <b>Your order is ready for pickup!</b>\n\nThank you {user_name}!",
                    parse_mode="HTML"
                )
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ Ready notification sent to {user_name}.")
            except Exception as e:
                await query.message.reply_text("⚠️ Failed to send notification.")
        else:
            await query.message.reply_text("⚠️ Order not found.")

async def forward_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'reply_to_order' not in context.user_data:
        return
    order_id = context.user_data['reply_to_order']
    if order_id not in order_storage:
        await update.message.reply_text("⚠️ Order session expired.")
        del context.user_data['reply_to_order']
        return
    buyer_chat_id = order_storage[order_id]['chat_id']
    user_name = order_storage[order_id]['user_name']
    try:
        if update.message.text:
            await context.bot.send_message(
                chat_id=buyer_chat_id,
                text=f"📨 <b>Message from Bird Nest House:</b>\n\n{update.message.text}",
                parse_mode="HTML"
            )
        elif update.message.photo:
            caption = update.message.caption or ""
            await context.bot.send_photo(
                chat_id=buyer_chat_id,
                photo=update.message.photo[-1].file_id,
                caption=f"📨 <b>Message from Bird Nest House:</b>\n\n{caption}",
                parse_mode="HTML"
            )
        elif update.message.sticker:
            await context.bot.send_sticker(chat_id=buyer_chat_id, sticker=update.message.sticker.file_id)
        else:
            await update.message.reply_text("Unsupported media.")
            return
        await update.message.reply_text(f"✅ Message sent to {user_name}!")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to send message.")

# ---------- Bot Polling (with event loop fix) ----------
def run_bot():
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("resetmenu", reset_menu))
    application.add_handler(CommandHandler("closechat", close_chat))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_reply))
    application.add_handler(MessageHandler(filters.PHOTO, forward_reply))
    application.add_handler(MessageHandler(filters.Sticker.ALL, forward_reply))
    print("🤖 Bot started polling...")
    application.run_polling(allowed_updates=["message", "callback_query"])

# ---------- Start both ----------
if __name__ == "__main__":
    print("=" * 50)
    print("⚠️  IMPORTANT: Ensure webhook is deleted!")
    print("Run this command once (in terminal or Render Shell):")
    print(f"curl -X POST \"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook\"")
    print("=" * 50)
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)