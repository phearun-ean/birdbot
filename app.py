"""
Bird Nest House — Telegram Bot + Flask Backend
Complete working version with KHQR integration
"""

import fcntl
import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import qrcode
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session, url_for
from flask_cors import CORS
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
def _require_env(key: str, fallback: str = None) -> str:
    """Return env var or raise clearly if it is missing and no fallback is given."""
    val = os.getenv(key, fallback)
    if val is None:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Add it to your .env file and restart."
        )
    return val


BOT_TOKEN         = _require_env("BOT_TOKEN")
SELLER_CHAT_ID    = int(_require_env("SELLER_CHAT_ID", "5131306408"))
WEB_APP_URL       = _require_env("WEB_APP_URL", "https://birdnesttgminiapp.web.app/")
APP_SECRET        = _require_env("APP_SECRET")
ADMIN_SECRET      = _require_env("ADMIN_SECRET")
ADMIN_PASSWORD    = _require_env("DASHBOARD_PASSWORD")
FLASK_SECRET_KEY  = _require_env("FLASK_SECRET_KEY", secrets.token_hex(32))

# Store Location with KHQR Image URL
STORE_LOCATION = {
    "name":      _require_env("STORE_NAME", "Bird Nest House"),
    "address":   _require_env("STORE_ADDRESS", "281 Street, Phnom Penh, Cambodia"),
    "latitude":  float(_require_env("STORE_LATITUDE", "11.58145")),
    "longitude": float(_require_env("STORE_LONGITUDE", "104.90451")),
    "phone":     _require_env("STORE_PHONE", "+855 78 999 685"),
    # KHQR Image URL from Firebase Storage
    "khqr_image_url": "https://firebasestorage.googleapis.com/v0/b/birdnesttgminiapp.firebasestorage.app/o/ABA_KHQR.jpeg?alt=media&token=7eb2c7a1-720c-4758-9b2a-5f4f69fac285",
    # KHQR Account Details
    "khqr_usd_account": "000 158 431",
    "khqr_khr_account": "002 750 675",
    "khqr_account_name": "PHEARUN EAN",
}
STORE_LOCATION["map_url"] = (
    f"https://maps.google.com/?q={STORE_LOCATION['latitude']},{STORE_LOCATION['longitude']}"
)

KHQR_MERCHANT_ID   = _require_env("KHQR_MERCHANT_ID", "000158431")
KHQR_MERCHANT_NAME = _require_env("KHQR_MERCHANT_NAME", "Phearun Ean")
KHQR_ACQUIRER      = _require_env("KHQR_ACQUIRER", "ABA")

CHAT_RETENTION_DAYS    = int(_require_env("CHAT_RETENTION_DAYS", "30"))
QR_RETENTION_DAYS      = int(_require_env("QR_RETENTION_DAYS", "7"))
AUTO_CLEANUP_ENABLED   = _require_env("AUTO_CLEANUP_ENABLED", "true").lower() == "true"
CLEANUP_INTERVAL_HOURS = int(_require_env("CLEANUP_INTERVAL_HOURS", "24"))

INVOICE_DIR = "invoices"
QR_DIR      = "qr_codes"
BACKUP_DIR  = "backups"
CHAT_DIR    = "chat_sessions"
ORDERS_FILE = "orders.json"
CHAT_FILE   = "chat_messages.json"

for _d in (INVOICE_DIR, QR_DIR, BACKUP_DIR, CHAT_DIR):
    os.makedirs(_d, exist_ok=True)

# ==================== SINGLE-INSTANCE LOCK ====================
_lock_file = open("bot.lock", "w")
try:
    fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (IOError, BlockingIOError):
    logger.error("Another bot instance is already running — exiting.")
    sys.exit(1)

# ==================== THREAD-SAFE JSON STORAGE ====================
class JsonStore:
    """Simple thread-safe key/value store backed by a single JSON file."""

    def __init__(self, filepath: str):
        self._path = filepath
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error("Failed to load %s: %s", self._path, e)
                self._data = {}

    def _save(self):
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("Failed to save %s: %s", self._path, e)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key] = value
            self._save()

    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)
            self._save()

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def __len__(self):
        with self._lock:
            return len(self._data)


order_store = JsonStore(ORDERS_FILE)
chat_store = JsonStore(CHAT_FILE)

# ==================== PAYMENT HELPERS ====================
_KHQR_ALIASES = {"khqr", "kh qr", "khqr payment", "abakhqr", "acleda qr"}

def is_khqr(payment_method: str) -> bool:
    return payment_method.lower() in _KHQR_ALIASES

def initial_order_status(payment_method: str) -> str:
    if is_khqr(payment_method):
        return "Pending (KHQR - Verify Payment)"
    if payment_method.lower() in ("cash on delivery", "cod"):
        return "Pending (COD)"
    return "Paid"

# ==================== CHAT MESSAGE STORAGE ====================
def save_chat_message(order_id: str, sender: str, message: str, sender_name: str = None):
    """Save chat message to permanent storage"""
    key = f"chat_{order_id}"
    messages = chat_store.get(key, [])
    messages.append({
        "order_id": order_id,
        "sender": sender,
        "sender_name": sender_name,
        "message": message,
        "timestamp": datetime.now().isoformat()
    })
    chat_store.set(key, messages)
    logger.info(f"Chat message saved for order {order_id} from {sender}")

def get_chat_messages(order_id: str, limit: int = 50) -> List[Dict]:
    """Get chat messages for an order"""
    key = f"chat_{order_id}"
    messages = chat_store.get(key, [])
    return messages[-limit:] if limit else messages

# ==================== QR CODE ====================
def generate_khqr(order_id: str, amount: float, description: str = "") -> str:
    filepath = os.path.join(QR_DIR, f"khqr_{order_id}.png")
    khqr_data = (
        f"khqr://{KHQR_MERCHANT_ID}"
        f"?amount={amount:.2f}&currency=USD"
        f"&description={description or f'Order {order_id}'}"
    )
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(khqr_data)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(filepath)
    return filepath

def cleanup_old_qr_codes(days: int = 7) -> int:
    deleted = 0
    cutoff = time.time() - days * 86400
    for fname in os.listdir(QR_DIR):
        fpath = os.path.join(QR_DIR, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            os.remove(fpath)
            deleted += 1
    logger.info("QR cleanup: deleted %d files", deleted)
    return deleted

# ==================== INVOICE ====================
def generate_invoice(order_data: dict) -> str:
    order_id = order_data.get("orderId", order_data.get("order_id", "unknown"))
    filepath = os.path.join(INVOICE_DIR, f"invoice_{order_id}.pdf")
    doc = SimpleDocTemplate(filepath, pagesize=letter,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleStyle", parent=styles["Heading1"],
        alignment=TA_CENTER, textColor=colors.HexColor("#ff6b00"),
    )

    story = [
        Paragraph("Bird Nest House", title_style),
        Paragraph("Official Invoice", styles["Heading2"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"Order ID: {order_id}", styles["Normal"]),
        Paragraph(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]),
        Paragraph(f"Customer: {order_data.get('user_name', order_data.get('userName', 'N/A'))}", styles["Normal"]),
        Paragraph(f"Payment: {order_data.get('paymentMethod', 'N/A')}", styles["Normal"]),
    ]

    delivery = order_data.get("delivery_location", {})
    if delivery:
        story.append(Paragraph(f"Delivery: {delivery.get('address', 'N/A')}", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))

    rows = [["Item", "Qty", "Unit Price", "Total"]]
    subtotal = 0.0
    for item in order_data.get("items", []):
        qty   = int(item.get("quantity", 1))
        price = float(item.get("price", 0))
        total = price * qty
        subtotal += total
        rows.append([item.get("name", "?"), str(qty), f"${price:.2f}", f"${total:.2f}"])

    delivery_fee = float(order_data.get("deliveryFee", 0))
    if delivery_fee:
        rows.append(["Delivery Fee", "1", f"${delivery_fee:.2f}", f"${delivery_fee:.2f}"])
        subtotal += delivery_fee

    discount = float(order_data.get("discountApplied", 0))
    if discount:
        rows.append(["Discount", "", "", f"-${discount:.2f}"])

    rows.append(["", "", "Total:", f"${order_data.get('total', subtotal - discount):.2f}"])

    table = Table(rows, colWidths=[3 * inch, inch, 1.5 * inch, 1.5 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.grey),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.whitesmoke),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  12),
        ("BACKGROUND",    (0, 1), (-1, -1), colors.beige),
        ("GRID",          (0, 0), (-1, -1), 1, colors.black),
        ("ALIGN",         (2, 1), (-1, -1), "RIGHT"),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.3 * inch))

    notes = order_data.get("orderNotes", "")
    if notes:
        story += [Paragraph("<b>Order Notes:</b>", styles["Normal"]),
                  Paragraph(notes, styles["Normal"]),
                  Spacer(1, 0.2 * inch)]

    story += [
        Paragraph("<b>Store Information:</b>", styles["Normal"]),
        Paragraph(f"📍 {STORE_LOCATION['name']}", styles["Normal"]),
        Paragraph(f"🏠 {STORE_LOCATION['address']}", styles["Normal"]),
        Paragraph(f"📞 {STORE_LOCATION['phone']}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"Points Earned: ⭐ {order_data.get('points', 0)}", styles["Normal"]),
        Spacer(1, 0.1 * inch),
        Paragraph("Thank you for shopping at Bird Nest House!", styles["Normal"]),
        Paragraph("For support, contact us on Telegram.", styles["Normal"]),
    ]

    doc.build(story)
    return filepath

# ==================== TELEGRAM HELPERS ====================
def _tg_post(endpoint: str, payload: dict) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception as e:
        logger.error("Telegram %s failed: %s", endpoint, e)
        return False

def send_telegram_message(chat_id: int, text: str,
                          parse_mode: str = "HTML",
                          reply_markup: dict = None) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _tg_post("sendMessage", payload)

def send_telegram_location(chat_id: int, lat: float, lon: float) -> bool:
    return _tg_post("sendLocation", {"chat_id": chat_id, "latitude": lat, "longitude": lon})

def send_telegram_document(chat_id: int, document_path: str, caption: str = "") -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        with open(document_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                files={"document": (os.path.basename(document_path), f, "application/pdf")},
                timeout=30,
            )
        return resp.ok
    except Exception as e:
        logger.error("Failed to send document to %s: %s", chat_id, e)
        return False

# ==================== AUTH HELPERS ====================
def require_dashboard_auth(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("dashboard_auth"):
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper

# ==================== FLASK APP ====================
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET_KEY

CORS(flask_app, resources={
    r"/api/*": {
        "origins": [
            "https://birdnesttgminiapp.web.app",
            "https://birdnesttgminiapp.firebaseapp.com",
            "http://localhost:5000",
            "http://localhost:3000",
            "http://127.0.0.1:5000",
            "http://127.0.0.1:3000"
        ],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-App-Secret", "Authorization"],
        "supports_credentials": True
    }
})

# ==================== API ENDPOINTS ====================

@flask_app.route("/api/store/location", methods=["GET"])
def get_store_location():
    """Return store information including KHQR image URL"""
    return jsonify({
        "success": True, 
        "store": STORE_LOCATION
    })

@flask_app.route("/api/khqr-image", methods=["GET"])
def get_khqr_image():
    """Return KHQR image URL directly"""
    return jsonify({
        "success": True,
        "image_url": STORE_LOCATION["khqr_image_url"],
        "usd_account": STORE_LOCATION["khqr_usd_account"],
        "khr_account": STORE_LOCATION["khqr_khr_account"],
        "account_name": STORE_LOCATION["khqr_account_name"]
    })

@flask_app.route("/api/new-order", methods=["POST", "OPTIONS"])
def receive_order():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    user_id    = str(data.get("userId", "web_unknown"))
    user_name  = data.get("userName", "Guest")
    items      = data.get("items", [])
    total      = float(data.get("total", 0))
    payment    = data.get("paymentMethod", "Unknown")
    chat_id    = data.get("chatId")
    timestamp  = data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    delivery_fee = float(data.get("deliveryFee", 0))
    notes      = data.get("orderNotes", "")
    discount   = float(data.get("discountApplied", 0))
    delivery_location = data.get("deliveryLocation")
    points     = int(data.get("points", 0))

    order_id = f"ORD_{user_id}_{secrets.token_hex(4)}"
    status   = initial_order_status(payment)

    order_record = {
        "orderId":         order_id,
        "chat_id":         chat_id,
        "user_id":         user_id,
        "user_name":       user_name,
        "username":        data.get("username", ""),
        "first_name":      data.get("firstName", ""),
        "last_name":       data.get("lastName", ""),
        "timestamp":       timestamp,
        "created_at":      datetime.now().isoformat(),
        "total":           total,
        "points":          points,
        "items":           items,
        "status":          status,
        "paymentMethod":   payment,
        "deliveryFee":     delivery_fee,
        "orderNotes":      notes,
        "discountApplied": discount,
        "delivery_location": delivery_location,
        "invoiceSent":     False,
        "source":          "web",
    }
    order_store.set(order_id, order_record)

    if is_khqr(payment):
        try:
            generate_khqr(order_id, total, f"Order {order_id}")
        except Exception as e:
            logger.error("KHQR generation failed for %s: %s", order_id, e)

    # Notify seller
    items_text = "\n".join(
        f"  • {i.get('name','?')} x{i.get('quantity',1)} — ${float(i.get('price',0)):.2f}"
        for i in items
    )
    delivery_info = ""
    if delivery_location:
        delivery_info = f"\n📍 <b>Delivery:</b> {delivery_location.get('address', 'Provided')}"

    seller_text = (
        f"🆕 <b>NEW ORDER #{order_id}</b>\n\n"
        f"👤 <b>Customer:</b> {user_name}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"📦 <b>Items:</b>\n{items_text}\n\n"
        f"💰 <b>Total:</b> ${total:.2f}\n"
        f"⭐ <b>Points Earned:</b> {points}\n"
        f"💳 <b>Payment:</b> {payment}"
        + ("\n⚠️ <b>KHQR — verify payment in banking app!</b>" if is_khqr(payment) else "")
        + delivery_info
        + f"\n📝 <b>Notes:</b> {notes or 'None'}"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ Mark as Paid",     "callback_data": f"paid_{order_id}"}],
            [{"text": "📄 Mark as Ready",    "callback_data": f"ready_{order_id}"}],
            [{"text": "📍 Send Store Location", "callback_data": f"send_location_{order_id}"}],
            [{"text": "💬 Reply to Customer", "callback_data": f"reply_{order_id}"}],
            [{"text": "📋 View Order",       "callback_data": f"view_order_{order_id}"}],
        ]
    }
    send_telegram_message(SELLER_CHAT_ID, seller_text, "HTML", keyboard)

    # Confirm to buyer with KHQR info
    if chat_id:
        if is_khqr(payment):
            buyer_text = (
                f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
                f"📦 <b>Order ID:</b> <code>{order_id}</code>\n"
                f"💰 <b>Total:</b> ${total:.2f}\n"
                f"⭐ <b>Points Earned:</b> {points}\n\n"
                f"💳 <b>KHQR Payment Instructions:</b>\n"
                f"🏦 Bank: {KHQR_ACQUIRER}\n"
                f"👤 Account: {KHQR_MERCHANT_NAME}\n"
                f"🔢 USD Account: {STORE_LOCATION['khqr_usd_account']}\n"
                f"🔢 KHR Account: {STORE_LOCATION['khqr_khr_account']}\n\n"
                f"📱 Scan the QR code in the app to pay.\n"
                f"⏳ We will confirm your payment shortly."
            )
        else:
            buyer_text = (
                f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
                f"📦 <b>Order ID:</b> <code>{order_id}</code>\n"
                f"💰 <b>Total:</b> ${total:.2f}\n"
                f"⭐ <b>Points Earned:</b> {points}\n\n"
                f"We will notify you when your order is ready."
            )
        send_telegram_message(int(chat_id), buyer_text, "HTML")

    logger.info("New order %s | customer: %s | total: $%.2f | payment: %s",
                order_id, user_name, total, payment)
    return jsonify({
        "success": True, 
        "orderId": order_id, 
        "storeLocation": STORE_LOCATION,
        "khqr_image_url": STORE_LOCATION["khqr_image_url"]
    })

@flask_app.route("/api/orders", methods=["GET"])
def api_orders():
    """Get all orders"""
    orders = list(order_store.all().values())
    for order in orders:
        order_id = order.get("orderId")
        messages = get_chat_messages(order_id)
        order["chat_messages_count"] = len(messages)
    return jsonify(orders)

@flask_app.route("/api/update-status", methods=["POST"])
def update_status():
    """Update order status"""
    data = request.get_json(silent=True) or {}
    order_id   = data.get("orderId")
    new_status = data.get("status")
    if not order_id or not new_status:
        return jsonify({"success": False, "error": "Missing orderId or status"}), 400

    order = order_store.get(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404

    order["status"] = new_status
    order_store.set(order_id, order)

    buyer_chat_id = order.get("chat_id")
    status_msgs = {
        "Paid":       "✅ Your payment has been confirmed! We are processing your order.",
        "Ready":      "📦 Your order is ready for pickup/delivery!",
        "Completed":  "🎉 Your order has been completed! Thank you for shopping with us!",
        "Processing": "🔄 Your order is being prepared.",
    }
    if buyer_chat_id and new_status in status_msgs:
        send_telegram_message(int(buyer_chat_id),
                              f"📦 Order #{order_id}\n\n{status_msgs[new_status]}")

    return jsonify({"success": True})

@flask_app.route("/api/chat/messages/<order_id>", methods=["GET"])
def get_chat_messages_api(order_id):
    """Get all chat messages for an order"""
    messages = get_chat_messages(order_id)
    return jsonify({"success": True, "messages": messages})

@flask_app.route("/api/chat/send", methods=["POST"])
def send_chat_message_api():
    """Send a message from seller to customer"""
    data = request.get_json(silent=True) or {}
    order_id = data.get("orderId")
    message = data.get("message")
    
    if not order_id or not message:
        return jsonify({"success": False, "error": "Missing orderId or message"}), 400
    
    order = order_store.get(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    
    buyer_chat_id = order.get("chat_id")
    if not buyer_chat_id:
        return jsonify({"success": False, "error": "No buyer chat ID"}), 404
    
    save_chat_message(order_id, "seller", message, "Bird Nest House")
    customer_msg = (
        f"📨 <b>Message from Bird Nest House</b>\n\n"
        f"<b>Order:</b> <code>{order_id}</code>\n\n"
        f"{message}\n\n"
        f"💡 Reply to this message to continue the conversation."
    )
    send_telegram_message(int(buyer_chat_id), customer_msg, "HTML")
    
    return jsonify({"success": True})

@flask_app.route("/api/chat/seller-reply", methods=["POST"])
def seller_chat_reply():
    """Handle seller reply with optional location"""
    data = request.get_json(silent=True) or {}
    order_id = data.get("orderId")
    message = data.get("message")
    send_loc = data.get("sendLocation", False)
    
    if not order_id:
        return jsonify({"success": False, "error": "Missing orderId"}), 400
    
    order = order_store.get(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    
    buyer_chat_id = order.get("chat_id")
    if not buyer_chat_id:
        return jsonify({"success": False, "error": "No buyer chat ID"}), 404
    
    if send_loc:
        send_telegram_location(int(buyer_chat_id), STORE_LOCATION["latitude"], STORE_LOCATION["longitude"])
        loc_msg = (
            f"📍 <b>Our Store Location</b>\n\n"
            f"🏠 <b>{STORE_LOCATION['name']}</b>\n"
            f"📌 {STORE_LOCATION['address']}\n"
            f"📞 {STORE_LOCATION['phone']}\n\n"
            f"<a href='{STORE_LOCATION['map_url']}'>Open in Google Maps</a>"
        )
        send_telegram_message(int(buyer_chat_id), loc_msg, "HTML")
        save_chat_message(order_id, "seller", f"📍 Store location sent: {STORE_LOCATION['address']}", "Bird Nest House")
        return jsonify({"success": True, "locationSent": True})
    
    if not message:
        return jsonify({"success": False, "error": "Missing message"}), 400
    
    save_chat_message(order_id, "seller", message, "Bird Nest House")
    customer_msg = f"📨 <b>Message from Bird Nest House</b>\n\n<b>Order:</b> <code>{order_id}</code>\n\n{message}"
    send_telegram_message(int(buyer_chat_id), customer_msg, "HTML")
    
    return jsonify({"success": True})

@flask_app.route("/api/invoice/<order_id>", methods=["GET"])
def get_invoice(order_id):
    path = os.path.join(INVOICE_DIR, f"invoice_{order_id}.pdf")
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name=f"invoice_{order_id}.pdf")
    return jsonify({"error": "Invoice not found"}), 404

@flask_app.route("/api/khqr-instructions/<order_id>", methods=["GET"])
def get_khqr_instructions(order_id):
    order = order_store.get(order_id, {})
    html = (
        f"<div style='text-align:center;padding:20px;'>"
        f"<h3>🇰🇭 KHQR Payment</h3>"
        f"<img src='{STORE_LOCATION['khqr_image_url']}' style='width:200px;height:200px;margin:10px auto;'/>"
        f"<table style='margin:0 auto;text-align:left;'>"
        f"<tr><td><b>Bank:</b></td><td>{KHQR_ACQUIRER}</td></tr>"
        f"<tr><td><b>Account Name:</b></td><td>{KHQR_MERCHANT_NAME}</td></tr>"
        f"<tr><td><b>USD Account:</b></td><td>{STORE_LOCATION['khqr_usd_account']}</td></tr>"
        f"<tr><td><b>KHR Account:</b></td><td>{STORE_LOCATION['khqr_khr_account']}</td></tr>"
        f"<tr><td><b>Amount:</b></td><td>${order.get('total', 0):.2f}</td></tr>"
        f"<tr><td><b>Reference:</b></td><td>{order_id}</td></tr>"
        f"</table>"
        f"<p style='margin-top:20px;'>After payment, the seller will confirm and send your invoice.</p>"
        f"</div>"
    )
    return jsonify({"success": True, "instructions": html})

@flask_app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, ADMIN_PASSWORD):
            session["dashboard_auth"] = True
            return redirect(url_for("dashboard_home"))
        return "<h2>Wrong password</h2><a href='/dashboard'>Try again</a>", 401

    return """<!DOCTYPE html>
<html>
<head>
    <title>Bird Nest House - Admin Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .login-card {
            background: white;
            border-radius: 20px;
            padding: 40px;
            width: 90%;
            max-width: 400px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            text-align: center;
        }
        .logo { font-size: 64px; margin-bottom: 20px; }
        h2 { color: #333; margin-bottom: 10px; }
        p { color: #666; margin-bottom: 30px; }
        input {
            width: 100%;
            padding: 14px;
            margin: 10px 0;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #ff6b00;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #ff6b00;
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            margin-top: 20px;
            transition: background 0.3s;
        }
        button:hover { background: #e65100; }
        .footer { margin-top: 20px; font-size: 12px; color: #999; }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">🐦</div>
        <h2>Bird Nest House</h2>
        <p>Admin Dashboard</p>
        <form method="post">
            <input type="password" name="password" placeholder="Enter password" required autofocus>
            <button type="submit">Login to Dashboard</button>
        </form>
        <div class="footer">Secure admin access only</div>
    </div>
</body>
</html>"""

@flask_app.route("/dashboard/home")
@require_dashboard_auth
def dashboard_home():
    return send_from_directory(".", "dashboard.html")

@flask_app.route("/")
def health():
    return jsonify({
        "status":        "running",
        "timestamp":     datetime.now().isoformat(),
        "orders_count":  len(order_store),
        "chat_messages": sum(len(v) for v in chat_store.all().values()),
        "store":         STORE_LOCATION["name"],
        "khqr_available": bool(STORE_LOCATION.get("khqr_image_url")),
        "cleanup": {
            "chat_retention_days": CHAT_RETENTION_DAYS,
            "qr_retention_days":   QR_RETENTION_DAYS,
            "auto_cleanup_enabled": AUTO_CLEANUP_ENABLED,
        },
    })

# ==================== AUTO CLEANUP ====================
def _run_scheduled_cleanup():
    while True:
        time.sleep(CLEANUP_INTERVAL_HOURS * 3600)
        try:
            deleted = cleanup_old_qr_codes(days=QR_RETENTION_DAYS)
            logger.info("Auto-cleanup: %d QR codes deleted", deleted)
        except Exception as e:
            logger.error("Auto-cleanup error: %s", e)

# ==================== TELEGRAM BOT HANDLERS ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_btn    = KeyboardButton("🍽️ Open Order Menu", web_app=WebAppInfo(url=WEB_APP_URL))
    location_btn = KeyboardButton("📍 Share My Location", request_location=True)
    await update.message.reply_text(
        "Welcome to Bird Nest House! 🥚\n\n"
        "🇰🇭 Premium bird nest products\n"
        "✅ Halal, GHPs/HACCP Certified | 🏆 5S & Kaizen\n\n"
        "<b>Features:</b>\n"
        "• Place orders easily\n"
        "• Pay with KHQR (ABA Bank)\n"
        "• Share your location for delivery\n"
        "• Get store location instantly\n"
        "• Chat with seller about your order\n\n"
        "Tap below to start:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[order_btn], [location_btn]], resize_keyboard=True
        ),
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.location:
        return
    loc  = update.message.location
    user = update.effective_user

    active_order_id = None
    for oid, order in order_store.all().items():
        if (str(order.get("chat_id")) == str(update.effective_chat.id)
                and order.get("status") not in ("Completed", "Cancelled")):
            active_order_id = oid
            break

    maps_url = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"
    seller_text = (
        f"📍 <b>Customer Location Shared!</b>\n\n"
        f"👤 <b>Customer:</b> {user.first_name}\n"
        f"🆔 <b>Chat ID:</b> <code>{update.effective_chat.id}</code>\n"
        f"📦 <b>Active Order:</b> {active_order_id or 'None'}\n\n"
        f"Lat: {loc.latitude} | Lon: {loc.longitude}\n"
        f"<a href='{maps_url}'>View on Map</a>"
    )
    send_telegram_message(SELLER_CHAT_ID, seller_text, "HTML")

    await update.message.reply_text(
        "✅ Location shared with the seller!\nThey can now plan your delivery."
    )

async def handle_webapp_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.web_app_data:
        await update.message.reply_text("No order data received. Please use the Order Menu button.")
        return

    raw = update.message.web_app_data.data
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await update.message.reply_text("Error processing order data. Please try again.")
        return

    logger.info(f"Received order from bot: {data.get('orderId', 'unknown')}")

    user_id   = str(data.get("userId", update.effective_chat.id))
    user_name = data.get("userName", update.effective_user.first_name or "Guest")
    items     = data.get("items", [])
    total     = float(data.get("total", 0))
    payment   = data.get("paymentMethod", "Unknown")
    points    = int(data.get("points", 0))
    delivery_fee = float(data.get("deliveryFee", 0))
    notes     = data.get("orderNotes", "")
    buyer_chat_id = update.effective_chat.id

    order_id = f"ORD_{user_id}_{secrets.token_hex(4)}"
    status   = initial_order_status(payment)

    order_record = {
        "orderId":         order_id,
        "chat_id":         buyer_chat_id,
        "user_id":         user_id,
        "user_name":       user_name,
        "username":        data.get("username", update.effective_chat.username or ""),
        "first_name":      data.get("firstName", update.effective_user.first_name or ""),
        "last_name":       data.get("lastName", update.effective_user.last_name or ""),
        "timestamp":       data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "created_at":      datetime.now().isoformat(),
        "total":           total,
        "points":          points,
        "items":           items,
        "status":          status,
        "paymentMethod":   payment,
        "deliveryFee":     delivery_fee,
        "orderNotes":      notes,
        "discountApplied": float(data.get("discountApplied", 0)),
        "delivery_location": None,
        "invoiceSent":     False,
        "source":          "bot",
    }
    order_store.set(order_id, order_record)

    if is_khqr(payment):
        try:
            generate_khqr(order_id, total, f"Order {order_id}")
        except Exception as e:
            logger.error("KHQR gen failed %s: %s", order_id, e)

    items_text = "\n".join(
        f"  • {i.get('name','?')} x{i.get('quantity',1)} — ${float(i.get('price',0)):.2f}"
        for i in items
    )

    seller_text = (
        f"🆕 <b>NEW ORDER #{order_id}</b>\n\n"
        f"👤 <b>Customer:</b> {user_name}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"📦 <b>Items:</b>\n{items_text}\n\n"
        f"💰 <b>Total:</b> ${total:.2f}\n"
        f"⭐ <b>Points:</b> {points}\n"
        f"💳 <b>Payment:</b> {payment}"
        + ("\n⚠️ <b>KHQR — verify in banking app!</b>" if is_khqr(payment) else "")
        + f"\n📝 <b>Notes:</b> {notes or 'None'}"
    )

    try:
        await context.bot.send_message(
            chat_id=SELLER_CHAT_ID,
            text=seller_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Mark as Paid",        callback_data=f"paid_{order_id}")],
                [InlineKeyboardButton("📄 Mark as Ready",       callback_data=f"ready_{order_id}")],
                [InlineKeyboardButton("📍 Send Store Location", callback_data=f"send_location_{order_id}")],
                [InlineKeyboardButton("💬 Reply to Customer",   callback_data=f"reply_{order_id}")],
                [InlineKeyboardButton("📋 View Order",          callback_data=f"view_order_{order_id}")],
            ]),
        )
    except Exception as e:
        logger.error("Seller notification failed: %s", e)

    if is_khqr(payment):
        confirm_text = (
            f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
            f"📦 <b>Order ID:</b> <code>{order_id}</code>\n"
            f"💰 <b>Total:</b> ${total:.2f}\n"
            f"⭐ <b>Points Earned:</b> {points}\n\n"
            f"💳 <b>KHQR Payment:</b>\n"
            f"🏦 Bank: {KHQR_ACQUIRER}\n"
            f"👤 Account: {KHQR_MERCHANT_NAME}\n"
            f"🔢 USD: {STORE_LOCATION['khqr_usd_account']}\n\n"
            f"📱 Scan the QR code in the app to pay.\n"
            f"⏳ We will confirm your payment shortly.\n\n"
            f"💬 You can chat with the seller about your order."
        )
    else:
        confirm_text = (
            f"✅ <b>Order Confirmed, {user_name}!</b>\n\n"
            f"📦 <b>Order ID:</b> <code>{order_id}</code>\n"
            f"💰 <b>Total:</b> ${total:.2f}\n"
            f"⭐ <b>Points Earned:</b> {points}\n\n"
            f"We will notify you when your order is ready.\n\n"
            f"💬 You can chat with the seller about your order."
        )
    await update.message.reply_text(confirm_text, parse_mode="HTML")
    logger.info("Bot order %s processed — %s $%.2f", order_id, payment, total)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb_data = query.data

    if cb_data.startswith("reply_"):
        order_id = cb_data[6:]
        order = order_store.get(order_id)
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        
        context.user_data["reply_to_order"] = order_id
        messages = get_chat_messages(order_id, limit=10)
        chat_history = ""
        if messages:
            chat_history = "\n📜 <b>Recent messages:</b>\n"
            for msg in messages[-5:]:
                sender = "👤 Customer" if msg.get("sender") == "customer" else "🛒 You"
                chat_history += f"{sender}: {msg.get('message', '')[:100]}\n"
        
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"💬 <b>Chat with {order['user_name']}</b>\n"
            f"📦 Order: <code>{order_id}</code>\n\n"
            f"{chat_history}\n"
            f"✏️ Send any message to reply.\n"
            f"📍 Use /sendlocation {order_id} to share store location.\n\n"
            f"Type /closechat when done.",
            parse_mode="HTML",
        )
        return

    if cb_data.startswith("paid_"):
        order_id = cb_data[5:]
        order = order_store.get(order_id)
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        buyer_chat_id = order.get("chat_id")
        if not buyer_chat_id:
            await query.message.reply_text("⚠️ No buyer chat ID.")
            return
        try:
            invoice_path = generate_invoice(order)
            sent = send_telegram_document(
                int(buyer_chat_id), invoice_path,
                f"🧾 *Invoice for Order {order_id}*\nPayment received. Thank you!"
            )
            if sent:
                order["status"]      = "Paid"
                order["invoiceSent"] = True
                order_store.set(order_id, order)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ Invoice sent to {order['user_name']} and order marked Paid.")
            else:
                await query.message.reply_text("⚠️ Failed to send invoice.")
        except Exception as e:
            logger.error("Invoice send error: %s", e)
            await query.message.reply_text(f"⚠️ Invoice error: {e}")

    elif cb_data.startswith("ready_"):
        order_id = cb_data[6:]
        order = order_store.get(order_id)
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        buyer_chat_id = order.get("chat_id")
        if not buyer_chat_id:
            await query.message.reply_text("⚠️ No buyer chat ID.")
            return
        ready_msg = (
            f"🍽️ <b>Your order is ready for pickup!</b>\n\n"
            f"Thank you, {order['user_name']}!\n\n"
            f"📍 <b>Pickup Location:</b>\n"
            f"{STORE_LOCATION['name']}\n"
            f"{STORE_LOCATION['address']}\n"
            f"📞 {STORE_LOCATION['phone']}\n\n"
            f"<a href='{STORE_LOCATION['map_url']}'>Open in Maps</a>\n\n"
            f"Please bring your order ID: <code>{order_id}</code>"
        )
        if send_telegram_message(int(buyer_chat_id), ready_msg, "HTML"):
            order["status"] = "Ready"
            order_store.set(order_id, order)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Ready notification sent to {order['user_name']}.")
        else:
            await query.message.reply_text("⚠️ Failed to send notification.")

    elif cb_data.startswith("send_location_"):
        order_id = cb_data[14:]
        order = order_store.get(order_id)
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        buyer_chat_id = order.get("chat_id")
        if not buyer_chat_id:
            await query.message.reply_text("⚠️ No buyer chat ID.")
            return
        await context.bot.send_location(
            chat_id=int(buyer_chat_id),
            latitude=STORE_LOCATION["latitude"],
            longitude=STORE_LOCATION["longitude"],
        )
        loc_msg = (
            f"📍 <b>Our Store Location</b>\n\n"
            f"🏠 <b>{STORE_LOCATION['name']}</b>\n"
            f"📌 {STORE_LOCATION['address']}\n"
            f"📞 {STORE_LOCATION['phone']}\n\n"
            f"<a href='{STORE_LOCATION['map_url']}'>Open in Google Maps</a>"
        )
        send_telegram_message(int(buyer_chat_id), loc_msg, "HTML")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Store location sent to {order['user_name']}.")

    elif cb_data.startswith("view_order_"):
        order_id = cb_data[11:]
        order = order_store.get(order_id)
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        items_text = "\n".join(
            f"  • {i.get('name','?')} x{i.get('quantity',1)} — ${float(i.get('price',0)):.2f}"
            for i in order.get("items", [])
        )
        details = (
            f"📋 <b>Order #{order_id}</b>\n\n"
            f"👤 {order.get('user_name','N/A')}\n"
            f"📅 {order.get('timestamp','N/A')}\n"
            f"💰 ${order.get('total',0):.2f}\n"
            f"💳 {order.get('paymentMethod','N/A')}\n"
            f"📊 {order.get('status','N/A')}\n\n"
            f"📦 Items:\n{items_text}\n\n"
            f"📝 Notes: {order.get('orderNotes','None')}"
        )
        await query.message.reply_text(details, parse_mode="HTML")

async def handle_customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store customer messages when they reply to seller"""
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    active_order_id = None
    
    for oid, order in order_store.all().items():
        if str(order.get("chat_id")) == str(chat_id):
            active_order_id = oid
            break
    
    if not active_order_id:
        return
    
    msg_text = update.message.text
    save_chat_message(active_order_id, "customer", msg_text, 
                     update.effective_user.first_name or "Customer")
    
    seller_msg = (
        f"💬 <b>New message from {update.effective_user.first_name}</b>\n\n"
        f"📦 <b>Order:</b> <code>{active_order_id}</code>\n\n"
        f"<b>Message:</b>\n{msg_text}\n\n"
        f"Reply using /chat {active_order_id}"
    )
    send_telegram_message(SELLER_CHAT_ID, seller_msg, "HTML")

async def forward_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward seller's typed reply to the relevant customer."""
    if "reply_to_order" not in context.user_data:
        return

    order_id = context.user_data["reply_to_order"]
    order    = order_store.get(order_id)
    if not order:
        await update.message.reply_text("⚠️ Order not found. Use /closechat to reset.")
        del context.user_data["reply_to_order"]
        return

    buyer_chat_id = order.get("chat_id")
    user_name     = order.get("user_name", "Customer")

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption or ""
        try:
            save_chat_message(order_id, "seller", f"[Photo] {caption}", "Bird Nest House")
            await context.bot.send_photo(
                chat_id=int(buyer_chat_id),
                photo=file_id,
                caption=f"📨 Message from Bird Nest House\n\n{caption}",
                parse_mode="HTML",
            )
            await update.message.reply_text(f"✅ Photo sent to {user_name}!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed: {e}")
    elif update.message.text:
        msg_text = update.message.text
        save_chat_message(order_id, "seller", msg_text, "Bird Nest House")
        customer_msg = (
            f"📨 <b>Message from Bird Nest House</b>\n\n"
            f"<b>Order:</b> <code>{order_id}</code>\n\n"
            f"{msg_text}"
        )
        if send_telegram_message(int(buyer_chat_id), customer_msg, "HTML"):
            await update.message.reply_text(f"✅ Message sent to {user_name}!")
        else:
            await update.message.reply_text("⚠️ Failed to send message.")
    else:
        await update.message.reply_text("Unsupported message type. Send text or photo.")

def _seller_only(func):
    from functools import wraps
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != SELLER_CHAT_ID:
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, context)
    return wrapper

@_seller_only
async def cmd_sendlocation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /sendlocation <order_id>")
        return
    order_id = context.args[0]
    order    = order_store.get(order_id)
    if not order:
        await update.message.reply_text("⚠️ Order not found.")
        return
    buyer_chat_id = order.get("chat_id")
    if not buyer_chat_id:
        await update.message.reply_text("⚠️ No chat ID for this customer.")
        return
    await context.bot.send_location(
        chat_id=int(buyer_chat_id),
        latitude=STORE_LOCATION["latitude"],
        longitude=STORE_LOCATION["longitude"],
    )
    loc_msg = (
        f"📍 <b>{STORE_LOCATION['name']}</b>\n"
        f"{STORE_LOCATION['address']}\n"
        f"📞 {STORE_LOCATION['phone']}\n\n"
        f"<a href='{STORE_LOCATION['map_url']}'>Open in Google Maps</a>"
    )
    send_telegram_message(int(buyer_chat_id), loc_msg, "HTML")
    await update.message.reply_text(f"✅ Store location sent to {order['user_name']}.")

@_seller_only
async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /chat <order_id>")
        return
    order_id = context.args[0]
    order    = order_store.get(order_id)
    if not order:
        await update.message.reply_text("⚠️ Order not found.")
        return
    
    context.user_data["reply_to_order"] = order_id
    messages = get_chat_messages(order_id, limit=20)
    history = ""
    if messages:
        history = "\n📜 <b>Chat History:</b>\n"
        for msg in messages[-10:]:
            sender = "👤 Customer" if msg.get("sender") == "customer" else "🛒 You"
            time_str = datetime.fromisoformat(msg.get("timestamp", datetime.now().isoformat())).strftime("%H:%M")
            history += f"{sender} ({time_str}): {msg.get('message', '')[:100]}\n"
    
    await update.message.reply_text(
        f"💬 <b>Chat with {order['user_name']}</b>\n"
        f"📦 Order: <code>{order_id}</code>\n\n"
        f"{history}\n"
        f"✏️ Send any text or photo to reply.\n"
        f"📍 Use /sendlocation {order_id} to share store location.\n\n"
        f"Use /closechat when done.",
        parse_mode="HTML",
    )

@_seller_only
async def cmd_closechat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.pop("reply_to_order", None)
    if order_id:
        await update.message.reply_text(f"✅ Chat for order {order_id} closed.")
    else:
        await update.message.reply_text("No active chat to close.")

@_seller_only
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = order_store.all()
    if not orders:
        await update.message.reply_text("No orders yet.")
        return
    lines = []
    for oid, o in list(orders.items())[-20:]:
        msg_count = len(get_chat_messages(oid))
        lines.append(f"• <code>{oid}</code> — {o.get('user_name','?')} — ${o.get('total',0):.2f} — {o.get('status','?')} — 💬{msg_count}")
    await update.message.reply_text(
        "📋 <b>Recent Orders (last 20)</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )

@_seller_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders  = order_store.all()
    total   = len(orders)
    active  = sum(1 for o in orders.values() if o.get("status") not in ("Completed", "Cancelled"))
    revenue = sum(float(o.get("total", 0)) for o in orders.values())
    chat_msgs = sum(len(v) for v in chat_store.all().values())
    await update.message.reply_text(
        f"📊 <b>System Stats</b>\n\n"
        f"Total orders: {total}\n"
        f"Active orders: {active}\n"
        f"Total revenue: ${revenue:.2f}\n"
        f"Chat messages: {chat_msgs}\n"
        f"Auto-cleanup: {'✅' if AUTO_CLEANUP_ENABLED else '❌'}",
        parse_mode="HTML",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 <b>Bot Status</b>\n\n"
        f"✅ Running\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📦 Orders: {len(order_store)}\n"
        f"💬 Chat sessions: {len([k for k in chat_store.all().keys() if k.startswith('chat_')])}\n"
        f"🏠 Store: {STORE_LOCATION['name']}\n"
        f"🇰🇭 KHQR: {'✅' if STORE_LOCATION.get('khqr_image_url') else '❌'}",
        parse_mode="HTML",
    )

# ==================== BOT RUNNER ====================
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("orders",       cmd_orders))
    app.add_handler(CommandHandler("chat",         cmd_chat))
    app.add_handler(CommandHandler("closechat",    cmd_closechat))
    app.add_handler(CommandHandler("sendlocation", cmd_sendlocation))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_order))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_customer_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_reply))
    app.add_handler(MessageHandler(filters.PHOTO, forward_reply))

    logger.info("🤖 Bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ==================== MAIN ====================
if __name__ == "__main__":
    print("=" * 60)
    print("🚀  BIRD NEST HOUSE — BOT + FLASK")
    print("=" * 60)
    print(f"Store  : {STORE_LOCATION['name']}")
    print(f"Web App: {WEB_APP_URL}")
    print(f"Dashboard: http://localhost:{os.environ.get('PORT', 5000)}/dashboard")
    print(f"KHQR Image: {'✅ Loaded' if STORE_LOCATION.get('khqr_image_url') else '❌ Missing'}")
    print(f"Cleanup: {'enabled' if AUTO_CLEANUP_ENABLED else 'disabled'} every {CLEANUP_INTERVAL_HOURS}h")
    print("=" * 60)

    if AUTO_CLEANUP_ENABLED:
        t = threading.Thread(target=_run_scheduled_cleanup, daemon=True)
        t.start()
        logger.info("Auto-cleanup scheduler started")

    while True:
        try:
            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            run_bot()
        except Exception as e:
            logger.error("Crash: %s — restarting in 10 s", e)
            time.sleep(10)
        else:
            logger.info("Bot stopped normally")
            break