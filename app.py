import logging
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from typing import Dict, Any
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = 455774531  # integer – your numeric chat ID
YOUR_WEB_APP_URL = "https://birdnesttgminiapp.web.app/"

logging.basicConfig(level=logging.INFO)

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

    # Extract fields
    user_id = str(data.get('userId', 'Unknown'))
    user_name = data.get('userName', 'Guest')
    username = data.get('username', '')
    first_name = data.get('firstName', '')
    last_name = data.get('lastName', '')
    items = data.get('items', [])
    total = data.get('total', '0.00')
    points = data.get('points', 0)
    timestamp = data.get('timestamp', 'N/A')

    # Create a unique order ID
    order_id = f"ORD_{user_id}_{int(datetime.now().timestamp())}"
    buyer_chat_id = update.effective_chat.id

    # Store order
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

    # Build customer info for seller
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

    # Inline keyboard for seller
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to Customer", callback_data=f"reply_{order_id}")],
        [InlineKeyboardButton("✅ Mark as Ready", callback_data=f"ready_{order_id}")]
    ])

    # Send order details to seller
    await context.bot.send_message(
        chat_id=SELLER_CHAT_ID,
        text=order_text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    # Confirm to customer
    await update.message.reply_text(
        f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
        f"Total: ${total}\nYou earned {points} loyalty points 🎉\n\n"
        f"We'll notify you when your order is ready.",
        parse_mode="HTML"
    )
    logging.info(f"Order {order_id} from {user_name} (ID: {user_id}) - ${total}")

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
                f"✏️ Type your reply for order {order_id}:\n(Send text, photo, or sticker)"
            )
        else:
            await query.message.reply_text("⚠️ Order not found. It may have expired.")
    elif data.startswith("ready_"):
        order_id = data.split("_", 1)[1]
        if order_id in order_storage:
            buyer_chat_id = order_storage[order_id]['chat_id']
            user_name = order_storage[order_id]['user_name']
            try:
                await context.bot.send_message(
                    chat_id=buyer_chat_id,
                    text=f"🍽️ <b>Your order is ready for pickup!</b>\n\n"
                         f"Thank you {user_name}! Come to Bird Nest House to collect your order.\n\n"
                         f"📍 Location: [Your address]\n⏰ Hours: 9AM - 9PM",
                    parse_mode="HTML"
                )
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ Ready notification sent to {user_name} (Order {order_id}).")
                logging.info(f"Notified {user_name} for order {order_id}")
            except Exception as e:
                logging.error(f"Failed to send ready notification: {e}")
                await query.message.reply_text("⚠️ Failed to send notification. Customer may have blocked the bot.")
        else:
            await query.message.reply_text("⚠️ Order not found.")

async def forward_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'reply_to_order' not in context.user_data:
        return

    order_id = context.user_data['reply_to_order']
    if order_id not in order_storage:
        await update.message.reply_text("⚠️ Order session expired. Cannot send message.")
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
            await context.bot.send_sticker(
                chat_id=buyer_chat_id,
                sticker=update.message.sticker.file_id
            )
        else:
            await update.message.reply_text("⚠️ Unsupported media. Send text, photo, or sticker.")
            return

        await update.message.reply_text(f"✅ Message sent to {user_name}!")
        logging.info(f"Reply sent to {user_name} for order {order_id}")
        del context.user_data['reply_to_order']

    except Exception as e:
        logging.error(f"Failed to forward reply: {e}")
        await update.message.reply_text("⚠️ Failed to send message. Customer may have blocked the bot.")

# ---------- Bot Polling ----------
def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_reply))
    application.add_handler(MessageHandler(filters.PHOTO, forward_reply))
    application.add_handler(MessageHandler(filters.Sticker.ALL, forward_reply))
    print("🤖 Bot started polling...")
    application.run_polling(allowed_updates=["message", "callback_query"])

# ---------- HTTP Health Check Server ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http():
    server = HTTPServer(('0.0.0.0', 10000), HealthHandler)
    server.serve_forever()

# ---------- Start Both ----------
if __name__ == "__main__":
    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()
    run_bot()