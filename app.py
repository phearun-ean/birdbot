import logging
import json
import os
import threading
import urllib.request
import urllib.error
import time
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
from flask import Flask, request, session, redirect, url_for, send_from_directory, jsonify, send_file
import secrets
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SELLER_CHAT_ID = 455774531
YOUR_WEB_APP_URL = "https://birdnesttgminiapp.web.app/"

logging.basicConfig(level=logging.INFO)

# ---------- Invoice generation ----------
INVOICE_DIR = "invoices"
os.makedirs(INVOICE_DIR, exist_ok=True)

def generate_invoice(order_data: dict) -> str:
    order_id = order_data.get('orderId')
    filename = f"{INVOICE_DIR}/invoice_{order_id}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], alignment=TA_CENTER, textColor=colors.orange)
    story = []
    story.append(Paragraph("Bird Nest House", title_style))
    story.append(Paragraph("Official Invoice", styles['Heading2']))
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(f"Order ID: {order_id}", styles['Normal']))
    story.append(Paragraph(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    story.append(Paragraph(f"Customer: {order_data.get('user_name', 'N/A')}", styles['Normal']))
    story.append(Paragraph(f"Payment Method: {order_data.get('paymentMethod', 'N/A')}", styles['Normal']))
    story.append(Spacer(1, 0.2*inch))
    items_data = [["Item", "Quantity", "Unit Price", "Total"]]
    for item in order_data.get('items', []):
        qty = item.get('quantity', 1)
        price = item.get('price', 0)
        items_data.append([item.get('name', '?'), str(qty), f"${price:.2f}", f"${price * qty:.2f}"])
    if order_data.get('discountApplied', 0) > 0:
        items_data.append(["Discount", "", "", f"-${order_data['discountApplied']:.2f}"])
    items_data.append(["", "", "Subtotal:", f"${order_data.get('total', 0) + order_data.get('discountApplied', 0):.2f}"])
    items_data.append(["", "", "Total:", f"${order_data.get('total', 0):.2f}"])
    table = Table(items_data, colWidths=[3*inch, 1*inch, 1.5*inch, 1.5*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph("Thank you for shopping at Bird Nest House!", styles['Normal']))
    story.append(Paragraph("For support, contact us on Telegram.", styles['Normal']))
    doc.build(story)
    return filename

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

# ---------- Flask app ----------
flask_app = Flask(__name__)
flask_app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))
ADMIN_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'change_this_password')
ADMIN_SECRET = os.getenv('ADMIN_SECRET', 'my-secret-key')

@flask_app.route('/')
def health():
    return "Bot is running", 200

@flask_app.route('/dashboard', methods=['GET', 'POST'])
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

@flask_app.route('/api/orders')
def api_orders():
    if not os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, 'w') as f:
            json.dump({}, f)
        return jsonify([])
    try:
        with open(ORDERS_FILE, 'r') as f:
            orders = json.load(f)
        orders_list = []
        for order_id, order_data in orders.items():
            order_data['orderId'] = order_id
            orders_list.append(order_data)
        return jsonify(orders_list)
    except Exception as e:
        logging.error(f"Error reading orders: {e}")
        return jsonify([])

@flask_app.route('/api/update-status', methods=['POST'])
def update_status():
    data = request.get_json()
    order_id = data.get('orderId')
    new_status = data.get('status')
    if not order_id or not new_status:
        return jsonify({'success': False, 'error': 'Missing orderId or status'}), 400
    if not os.path.exists(ORDERS_FILE):
        return jsonify({'success': False, 'error': 'Orders file not found'}), 404
    with open(ORDERS_FILE, 'r') as f:
        orders = json.load(f)
    if order_id not in orders:
        return jsonify({'success': False, 'error': 'Order not found'}), 404
    orders[order_id]['status'] = new_status
    with open(ORDERS_FILE, 'w') as f:
        json.dump(orders, f, indent=2)
    return jsonify({'success': True})

@flask_app.route('/api/send-message', methods=['POST'])
def send_message():
    data = request.get_json()
    chat_id = data.get('chatId')
    message = data.get('message')
    if not chat_id or not message:
        return jsonify({'success': False, 'error': 'Missing chatId or message'}), 400
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        'chat_id': chat_id,
        'text': f"📢 *Seller Message:*\n{message}",
        'parse_mode': 'Markdown'
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Telegram API error'}), 500
    except urllib.error.URLError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/invoice/<order_id>')
def get_invoice(order_id):
    invoice_path = f"{INVOICE_DIR}/invoice_{order_id}.pdf"
    if os.path.exists(invoice_path):
        return send_file(invoice_path, as_attachment=True, download_name=f"invoice_{order_id}.pdf")
    return jsonify({'error': 'Invoice not found'}), 404

@flask_app.route('/api/send-invoice/<order_id>', methods=['POST'])
def send_invoice_manual(order_id):
    if order_id not in order_storage:
        return jsonify({'error': 'Order not found'}), 404
    if order_storage[order_id].get('invoiceSent'):
        return jsonify({'error': 'Invoice already sent'}), 400
    try:
        invoice_path = generate_invoice(order_storage[order_id])
        return jsonify({'success': True, 'invoiceUrl': f'/api/invoice/{order_id}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)

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

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == SELLER_CHAT_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open Dashboard", web_app=WebAppInfo(url="https://birdbot-5sgv.onrender.com/dashboard"))]
        ])
        await update.message.reply_text(
            "📊 *Seller Dashboard*\n\nClick the button below to manage orders.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text("Unauthorized.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SELLER_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    if not os.path.exists(ORDERS_FILE):
        await update.message.reply_text("No orders found.")
        return
    with open(ORDERS_FILE, 'r') as f:
        orders = json.load(f)
    chat_ids = set()
    for order in orders.values():
        if 'chat_id' in order:
            chat_ids.add(order['chat_id'])
    if not chat_ids:
        await update.message.reply_text("No customers found.")
        return
    sent = 0
    failed = 0
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📢 *Announcement:*\n{message}",
                parse_mode="Markdown"
            )
            sent += 1
        except Exception as e:
            logging.error(f"Failed to send to {chat_id}: {e}")
            failed += 1
    await update.message.reply_text(f"Broadcast complete. Sent: {sent}, Failed: {failed}")

async def test_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test if bot can send message to seller"""
    print(f"🧪 Test message command received from {update.effective_chat.id}")
    try:
        await context.bot.send_message(
            chat_id=SELLER_CHAT_ID,
            text="🧪 Test message from bot to seller"
        )
        await update.message.reply_text("✅ Test message sent to seller!")
        print("✅ Test message sent successfully")
    except Exception as e:
        error_msg = f"❌ Failed to send test message: {e}"
        print(error_msg)
        await update.message.reply_text(error_msg)

async def handle_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🔔 handle_order triggered")
    if not update.message or not update.message.web_app_data:
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
    payment_method = data.get('paymentMethod', 'Unknown')
    send_invoice_now = data.get('sendInvoiceImmediately', False)

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
        'total': float(total),
        'points': points,
        'items': items,
        'status': 'Paid' if send_invoice_now else 'Pending (COD)',
        'paymentMethod': payment_method,
        'invoiceSent': send_invoice_now
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
        f"💳 <b>Payment:</b> {payment_method}\n"
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to Customer", callback_data=f"reply_{order_id}")],
        [InlineKeyboardButton("✅ Mark as Paid & Send Invoice", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("📄 Mark as Ready", callback_data=f"ready_{order_id}")]
    ])

    # Debug: Print what we're about to send
    print(f"📤 Attempting to send to seller chat ID: {SELLER_CHAT_ID}")
    print(f"📝 Order ID: {order_id}")
    print(f"👤 Customer: {user_name}")
    print(f"💰 Total: ${total}")

    try:
        await context.bot.send_message(
            chat_id=SELLER_CHAT_ID,
            text=order_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        print("✅ Seller notification sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send seller notification: {e}")
        logging.error(f"Failed to send seller notification: {e}")
        await update.message.reply_text("⚠️ Order received but seller notification failed. We'll still process your order.")

    await update.message.reply_text(
        f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
        f"Total: ${total}\nYou earned {points} loyalty points 🎉\n\n"
        f"We'll notify you when your order is ready.",
        parse_mode="HTML"
    )
    logging.info(f"Order {order_id} from {user_name} - ${total}")

    # Auto‑send invoice for paid orders
    if send_invoice_now:
        try:
            invoice_path = generate_invoice(order_storage[order_id])
            with open(invoice_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=buyer_chat_id,
                    document=f,
                    filename=f"invoice_{order_id}.pdf",
                    caption=f"🧾 *Invoice for Order {order_id}*\nThank you for your payment!",
                    parse_mode="Markdown"
                )
            logging.info(f"Invoice automatically sent to {user_name} for order {order_id}")
        except Exception as e:
            logging.error(f"Failed to generate/send invoice: {e}")

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

    elif data.startswith("paid_"):
        order_id = data.split("_", 1)[1]
        if order_id in order_storage:
            buyer_chat_id = order_storage[order_id]['chat_id']
            user_name = order_storage[order_id]['user_name']
            try:
                invoice_path = generate_invoice(order_storage[order_id])
                with open(invoice_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=buyer_chat_id,
                        document=f,
                        filename=f"invoice_{order_id}.pdf",
                        caption=f"🧾 *Invoice for Order {order_id}*\nPayment received. Thank you!",
                        parse_mode="Markdown"
                    )
                order_storage[order_id]['status'] = 'Paid'
                order_storage[order_id]['invoiceSent'] = True
                save_orders(order_storage)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ Invoice sent to {user_name} and order marked as Paid.")
                logging.info(f"Manual invoice sent to {user_name} for order {order_id}")
            except Exception as e:
                logging.error(f"Failed to send manual invoice: {e}")
                await query.message.reply_text("⚠️ Failed to generate invoice.")
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
                order_storage[order_id]['status'] = 'Ready'
                save_orders(order_storage)
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

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if bot is running"""
    await update.message.reply_text(
        "🤖 **Bot Status**\n\n"
        "✅ Bot is running normally!\n"
        f"🕐 Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "🔄 Auto-restart enabled\n"
        "⏰ Cron job active (every 10 minutes)",
        parse_mode="Markdown"
    )

# ---------- Bot Polling (in main thread) ----------
def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("resetmenu", reset_menu))
    application.add_handler(CommandHandler("closechat", close_chat))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("testmsg", test_message))  # Test command
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_order))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_reply))
    application.add_handler(MessageHandler(filters.PHOTO, forward_reply))
    application.add_handler(MessageHandler(filters.Sticker.ALL, forward_reply))
    print("🤖 Bot started polling...")
    application.run_polling(allowed_updates=["message", "callback_query"])

# ---------- Start with auto-restart ----------
if __name__ == "__main__":
    print("=" * 50)
    print("⚠️  IMPORTANT: Ensure webhook is deleted!")
    print(f"curl -X POST \"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook\"")
    print("=" * 50)
    
    # Auto-restart loop - keeps bot alive even if it crashes
    while True:
        try:
            print("🚀 Starting bot...")
            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            run_bot()
        except Exception as e:
            print(f"❌ Bot crashed: {e}")
            print("🔄 Restarting in 10 seconds...")
            time.sleep(10)
        else:
            print("✅ Bot stopped normally")
            break