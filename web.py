#!/usr/bin/env python3
"""
Aadhaar EID/UID Retrieval & PDF Download Telegram Bot
Riven / Webhook Compatible Version
"""

import os
import sys
import logging
import json
import base64
import io
import time
import uuid
import requests
import pikepdf
import sqlite3
import asyncio
import concurrent.futures
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    JobQueue,
)
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# States
(NAME, MOBILE, EID_CAPTCHA, EID_OTP, DOWNLOAD_CAPTCHA, DOWNLOAD_OTP) = range(6)
(ADMIN_SET_LIMIT, ADMIN_BROADCAST) = range(10, 12)

BASE_URL = "https://tathya.uidai.gov.in"
EID_ENDPOINT = f"{BASE_URL}/retrieveEidUid/ext/v1/generic/retrieveuideid"
CAPTCHA_ENDPOINT = f"{BASE_URL}/audioCaptchaService/api/captcha/v3/generation"
DOWNLOAD_OTP_ENDPOINT = f"{BASE_URL}/unifiedAppAuthService/api/v2/generate/aadhaar/otp"
DOWNLOAD_PDF_ENDPOINT = f"{BASE_URL}/downloadAadhaarService/api/aadhaar/download"

# ==================== CONFIGURATION ====================
# Apni details yahan daalein
ADMIN_IDS = [8547023248, 8374177675, 123456789] # Apni Telegram ID daalein
BOT_NAME = "Aadhaar PDF Bot"
SUPPORT_ID = "@YourSupportUsername" # Apna support ID daalein
CHANNEL_LINK = "https://t.me/YourChannel"
COMMUNITY_LINK = "https://t.me/YourCommunity"
CHANNEL_ID = -1003264743327 # Apna channel ID daalein

DEFAULT_FREE_LIMIT = 5
DEFAULT_FREE_WINDOW_ENABLED = True

PLANS = {
    "basic": {"aadhaars": 10, "price_inr": 130, "price_usdt": 2},
    "standard": {"aadhaars": 20, "price_inr": 250, "price_usdt": 3},
    "premium": {"aadhaars": 50, "price_inr": 550, "price_usdt": 6},
    "enterprise": {"aadhaars": 100, "price_inr": 1000, "price_usdt": 10},
    "ultimate": {"aadhaars": 200, "price_inr": 1800, "price_usdt": 18},
}

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en_IN",
    "appid": "MYAADHAAR",
    "content-type": "application/json",
    "origin": "https://myaadhaar.uidai.gov.in",
    "referer": "https://myaadhaar.uidai.gov.in/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "connection": "keep-alive"
}

user_sessions: Dict[int, 'UserSession'] = {}

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, total_downloads INTEGER DEFAULT 0, last_download_date TEXT, daily_downloads INTEGER DEFAULT 0, joined_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''INSERT OR IGNORE INTO settings (key, value) VALUES ('free_window_enabled', 'True'), ('free_limit', '5'), ('maintenance_mode', 'False')''')
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def update_setting(key, value):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
    conn.commit()
    conn.close()

def get_free_window_enabled(): return get_setting('free_window_enabled').lower() == 'true'
def get_free_limit(): return int(get_setting('free_limit'))
def get_maintenance_mode(): return get_setting('maintenance_mode').lower() == 'true'

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def create_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, credits, joined_date) VALUES (?, 0, ?)', (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_credits(user_id, amount):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET credits = credits + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_credits(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT credits FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_download(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute('SELECT daily_downloads, last_download_date FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    if result:
        daily, last_date = result
        if last_date != today: daily = 0
        daily += 1
        c.execute('UPDATE users SET total_downloads = total_downloads + 1, daily_downloads = ?, last_download_date = ? WHERE user_id = ?', (daily, today, user_id))
    conn.commit()
    conn.close()
    return daily

def can_download(user_id):
    if get_maintenance_mode(): return False, "Bot is under maintenance."
    user = get_user(user_id)
    if not user:
        create_user(user_id)
        if get_free_window_enabled(): return True, "Free window enabled"
        return False, "Free window disabled."
    if user[1] > 0: return True, f"Premium credits: {user[1]}"
    if not get_free_window_enabled(): return False, "Free window disabled."
    
    free_limit = get_free_limit()
    today = datetime.now().date().isoformat()
    daily = user[4] if user[4] else 0
    last_date = user[3] if user[3] else ""
    
    if last_date != today: return True, "New day, free downloads reset"
    if daily < free_limit: return True, f"Free downloads left: {free_limit - daily}"
    return False, f"Daily limit reached ({free_limit}/{free_limit})."

class UserSession:
    def __init__(self):
        self.name = self.mobile = self.eid_number = None
        self.eid_captcha_txn_id = self.eid_otp_txn_id = self.eid_captcha_text = None
        self.download_captcha_txn_id = self.download_otp_txn_id = self.download_captcha_text = None
        self.request_id = None
        self.session = requests.Session()
        self.otp_attempts = 0
        self.max_otp_attempts = 3
        self.full_name = None

def generate_captcha(session):
    try:
        payload = {"captchaLength": "6", "captchaType": "2", "audioCaptchaRequired": True}
        response = session.post(CAPTCHA_ENDPOINT, headers=BASE_HEADERS, json=payload, timeout=15)
        result = response.json()
        if result.get("imageBase64") and result.get("transactionId"):
            return base64.b64decode(result["imageBase64"]), result["transactionId"], None
        return None, None, {"error": "No image in response"}
    except Exception as e:
        return None, None, {"error": str(e)}

def api_call(session, url, payload, label, custom_headers=None):
    try:
        headers = custom_headers if custom_headers else BASE_HEADERS
        response = session.post(url, headers=headers, json=payload, timeout=15)
        return response.json(), None
    except Exception as e:
        return None, {"error": str(e)}

def crack_pdf(pdf_bytes, name):
    if name:
        name_clean = ' '.join(name.split()).upper()
        name_prefix = name_clean[:4] if len(name_clean) >= 4 else name_clean.ljust(4, 'X')
    else: name_prefix = "MR"
    try:
        try:
            pdf = pikepdf.open(io.BytesIO(pdf_bytes))
            output = io.BytesIO(); pdf.save(output); pdf.close()
            return output.getvalue(), None
        except: pass
        for year in range(1950, 2016):
            password = f"{name_prefix}{year}"
            try:
                pdf = pikepdf.open(io.BytesIO(pdf_bytes), password=password, allow_overwriting_input=True)
                output = io.BytesIO(); pdf.save(output); pdf.close()
                return output.getvalue(), password
            except: continue
        return None, None
    except: return None, None

# ==================== MENUS ====================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("📥 Download Aadhaar", callback_data="start_download")],
        [InlineKeyboardButton("💰 Premium Plans", callback_data="view_plans")],
        [InlineKeyboardButton("👤 My Credits", callback_data="check_credits")],
        [InlineKeyboardButton("❓ Help", callback_data="help_menu")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"🏠 Main Menu\n\nWelcome to {BOT_NAME}!\nSelect an option below:\n\n📩 Support: {SUPPORT_ID}"
    
    if edit and hasattr(update, 'callback_query'):
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if get_maintenance_mode():
        await update.message.reply_text(f"⛔ Bot Under Maintenance. Please try later.")
        return ConversationHandler.END

    if user_id in user_sessions: del user_sessions[user_id]
    create_user(user_id)
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==================== DOWNLOAD FLOW ====================

async def start_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    can_dl, msg = can_download(user_id)
    if not can_dl:
        keyboard = [[InlineKeyboardButton("💰 View Plans", callback_data="view_plans")], [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Access Restricted\n\n{msg}\n\nPurchase premium plans to continue!", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    
    if user_id in user_sessions: del user_sessions[user_id]
    user_sessions[user_id] = UserSession()
    
    await query.edit_message_text("🔐 Aadhaar Download\n\n📝 Enter your Full Name as in Aadhaar\n_Type Mr to skip_")
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    name = update.message.text.strip()
    us.name = name if name else "Mr"
    await update.message.reply_text(f"✅ Name: {us.name}\n\n📱 Enter your 10-digit Mobile Number")
    return MOBILE

async def get_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) != 10:
        await update.message.reply_text("❌ Enter exactly 10 digits.")
        return MOBILE
    
    us.mobile = mobile
    progress = await update.message.reply_text("🔄 Generating captcha...")
    image_bytes, txn_id, _ = generate_captcha(us.session)
    await progress.delete()
    
    if not image_bytes:
        await update.message.reply_text("❌ Failed to generate captcha. /start to retry")
        return ConversationHandler.END
    
    us.eid_captcha_txn_id = txn_id
    await update.message.reply_photo(photo=io.BytesIO(image_bytes), caption="📸 Enter the captcha text")
    return EID_CAPTCHA

async def get_eid_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    captcha_text = update.message.text.strip()
    us.eid_captcha_text = captcha_text
    progress = await update.message.reply_text("📤 Requesting OTP...")
    
    otp_payload = {
        "mobileNumber": us.mobile, "dob": None, "email": None, "name": us.name,
        "option": "EID", "otp": None, "otpTxnId": None,
        "captchaTxnId": us.eid_captcha_txn_id, "captcha": captcha_text, "resendOtp": False
    }
    
    otp_result, _ = api_call(us.session, EID_ENDPOINT, otp_payload, "EID OTP")
    await progress.delete()
    
    if not otp_result or otp_result.get("status") != "Success" or not otp_result.get("responseData", {}).get("otpSent"):
        await update.message.reply_text("❌ OTP Failed. /start to retry")
        return ConversationHandler.END
    
    us.eid_otp_txn_id = otp_result["responseData"]["otpTxnId"]
    masked = f"{us.mobile[:2]}****{us.mobile[-4:]}"
    await update.message.reply_text(f"✅ OTP Sent to {masked}\n\n📝 Enter the 6-digit OTP")
    return EID_OTP

async def get_eid_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be 6 digits.")
        return EID_OTP
    
    us.otp_attempts += 1
    if us.otp_attempts > us.max_otp_attempts:
        await update.message.reply_text("❌ Too many attempts. /start to retry.")
        return ConversationHandler.END
    
    progress = await update.message.reply_text("🔍 Verifying OTP...")
    verify_payload = {
        "mobileNumber": us.mobile, "dob": None, "email": None, "name": us.name,
        "option": "EID", "otp": otp, "otpTxnId": us.eid_otp_txn_id,
        "captchaTxnId": us.eid_captcha_txn_id, "captcha": us.eid_captcha_text, "resendOtp": False
    }
    
    verify_result, _ = api_call(us.session, EID_ENDPOINT, verify_payload, "EID VERIFY")
    await progress.delete()
    
    if not verify_result or verify_result.get("status") != "Success" or not verify_result.get("responseData", {}).get("eidNumber"):
        await update.message.reply_text("❌ Invalid OTP. /start to retry")
        return ConversationHandler.END
    
    us.eid_number = verify_result["responseData"]["eidNumber"]
    us.full_name = verify_result["responseData"].get("name", us.name)
    us.request_id = str(uuid.uuid4())
    
    await update.message.reply_text(f"✅ EID Retrieved!\n👤 {us.full_name}\n🆔 {us.eid_number}\n\n🔄 Generating download captcha...")
    dl_image_bytes, dl_txn_id, _ = generate_captcha(us.session)
    
    if not dl_image_bytes:
        await update.message.reply_text("❌ Failed to generate captcha. /start to retry")
        return ConversationHandler.END
    
    us.download_captcha_txn_id = dl_txn_id
    await update.message.reply_photo(photo=io.BytesIO(dl_image_bytes), caption="📸 Enter the download captcha")
    return DOWNLOAD_CAPTCHA

async def get_download_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    captcha_text = update.message.text.strip()
    us.download_captcha_text = captcha_text
    progress = await update.message.reply_text("📤 Requesting download OTP...")
    
    dl_otp_payload = {
        "eidNumber": us.eid_number, "idType": "eid",
        "captchaTxnId": us.download_captcha_txn_id, "captchaValue": captcha_text,
        "transactionId": us.request_id, "resendOTP": False
    }
    
    dl_otp_result, _ = api_call(us.session, DOWNLOAD_OTP_ENDPOINT, dl_otp_payload, "DL OTP")
    await progress.delete()
    
    if not dl_otp_result or dl_otp_result.get("status") != "Success":
        await update.message.reply_text("❌ Failed. /start to retry")
        return ConversationHandler.END
    
    txn_id = dl_otp_result.get("txnId")
    us.download_otp_txn_id = txn_id if txn_id else dl_otp_result.get("responseData", {}).get("otpTxnId")
    us.otp_attempts = 0
    
    await update.message.reply_text("✅ Download OTP Sent!\n\n📝 Enter the Download OTP")
    return DOWNLOAD_OTP

async def get_download_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us: return ConversationHandler.END
    
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be 6 digits.")
        return DOWNLOAD_OTP
    
    progress = await update.message.reply_text("📥 Downloading PDF...")
    pdf_payload = {"eid": us.eid_number, "mask": False, "otp": otp, "otpTxnId": us.download_otp_txn_id}
    transaction_id = str(uuid.uuid4())
    pdf_headers = BASE_HEADERS.copy()
    pdf_headers["transactionid"] = transaction_id
    pdf_headers["x-request-id"] = transaction_id
    
    try:
        response = us.session.post(DOWNLOAD_PDF_ENDPOINT, headers=pdf_headers, json=pdf_payload, timeout=30)
        await progress.delete()
        response_json = response.json()
        
        if response_json.get("status") == "Success" and response_json.get("statusCode") == 200:
            pdf_data = response_json.get("data", {}).get("aadhaarPdf") or response_json.get("aadhaarPdf")
            if pdf_data:
                pdf_bytes = base64.b64decode(pdf_data)
                if pdf_bytes[:4] == b'%PDF':
                    await update.message.reply_text("🔓 Attempting to crack PDF...")
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(crack_pdf, pdf_bytes, us.full_name)
                        try: unlocked_pdf, password = future.result(timeout=30)
                        except: unlocked_pdf, password = None, None
                    
                    credits = get_credits(user_id)
                    if credits > 0:
                        update_credits(user_id, -1)
                        credit_msg = f"Credits Remaining: {credits - 1}"
                    else:
                        increment_download(user_id)
                        free_limit = get_free_limit()
                        daily = get_user(user_id)[4] if get_user(user_id) else 0
                        credit_msg = f"Free Downloads: {daily}/{free_limit}"
                    
                    if unlocked_pdf:
                        pdf_to_send = unlocked_pdf
                        caption_text = f"🔓 Unlocked\n🔑 Password: {password}" if password else ""
                    else:
                        pdf_to_send = pdf_bytes
                        caption_text = f"🔒 Password Protected\nHint: {us.full_name[:4].upper() if us.full_name else 'MR'}YYYY"
                    
                    pdf_file = io.BytesIO(pdf_to_send)
                    pdf_file.name = f"aadhaar_{us.eid_number[:8]}.pdf"
                    
                    await update.message.reply_document(document=pdf_file, caption=f"📄 Aadhaar PDF\n👤 {us.full_name}\n🆔 {us.eid_number}\n{caption_text}\n{credit_msg}\n\n📩 Support: {SUPPORT_ID}")
                    await show_main_menu(update, context)
                else: await update.message.reply_text("❌ Invalid PDF data")
            else: await update.message.reply_text("❌ No PDF data")
        else: await update.message.reply_text(f"❌ Download Failed: {response_json.get('statusMessage', 'Error')}")
    except Exception as e:
        await progress.delete()
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    if user_id in user_sessions: del user_sessions[user_id]
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in user_sessions: del user_sessions[update.effective_user.id]
    await update.message.reply_text("❌ Cancelled. /start to begin again.")
    return ConversationHandler.END

# ==================== CALLBACKS (BUTTONS) ====================

async def check_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    credits = get_credits(user_id)
    user = get_user(user_id)
    daily = user[4] if user else 0
    last_date = user[3] if user else ""
    today = datetime.now().date().isoformat()
    if last_date != today: daily = 0
    
    free_limit = get_free_limit()
    text = f"👤 Your Credits\n\n💳 Premium Credits: {credits}\n📊 Free Downloads Today: {daily}/{free_limit}\n🔓 Free Window: {'✅ Enabled' if get_free_window_enabled() else '❌ Disabled'}\n\n💰 Buy more credits: /plans"
    
    keyboard = [[InlineKeyboardButton("💰 View Plans", callback_data="view_plans")], [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def view_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    plans_text = "💰 Premium Plans\n\n"
    for key, plan in PLANS.items():
        plans_text += f"🔹 {key.title()} Plan\n📊 {plan['aadhaars']} Aadhaars\n💵 ₹{plan['price_inr']} / {plan['price_usdt']} USDT\n\n"
    plans_text += f"\n📩 Contact {SUPPORT_ID} to purchase\n💳 Check /credits to see your balance"
    
    keyboard = [[InlineKeyboardButton("👤 Check Credits", callback_data="check_credits")], [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(plans_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = f"❓ Help & Support\n\n1️⃣ /start to begin\n2️⃣ Enter details & OTPs\n3️⃣ Get PDF\n\n📩 Support: {SUPPORT_ID}"
    keyboard = [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, edit=True)

# ==================== ADMIN PANEL ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    
    text = f"⚙️ Admin Dashboard\n\n📊 Free Window: {'✅' if get_free_window_enabled() else '❌'}\n📊 Free Limit: {get_free_limit()}/day\n🔧 Maintenance: {'⛔' if get_maintenance_mode() else '✅'}\n\nSelect an option:"
    
    keyboard = [
        [InlineKeyboardButton(f"{'🔴' if get_free_window_enabled() else '🟢'} Toggle Free Window", callback_data="admin_toggle_free")],
        [InlineKeyboardButton("📊 Set Free Limit", callback_data="admin_set_limit")],
        [InlineKeyboardButton(f"{'🟢' if get_maintenance_mode() else '🔴'} Toggle Maintenance", callback_data="admin_toggle_maintenance")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_toggle_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    update_setting('free_window_enabled', str(not get_free_window_enabled()))
    await admin_panel(update, context)

async def admin_toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    update_setting('maintenance_mode', str(not get_maintenance_mode()))
    await admin_panel(update, context)

async def admin_set_limit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    await query.edit_message_text("📊 Enter new daily free limit (number):\n\nSend /cancel to cancel.")
    return ADMIN_SET_LIMIT

async def admin_set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    try:
        new_limit = int(update.message.text.strip())
        if new_limit < 1: raise ValueError
        update_setting('free_limit', str(new_limit))
        await update.message.reply_text(f"✅ Limit updated to {new_limit}. /admin to return.")
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Invalid number. Try again.")
        return ADMIN_SET_LIMIT

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    await query.edit_message_text("📢 Send the message to broadcast to all users:\n\nSend /cancel to cancel.")
    return ADMIN_BROADCAST

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    msg = update.message.text.strip()
    status = await update.message.reply_text("📤 Broadcasting...")
    
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    c.execute('SELECT user_id FROM users'); users = c.fetchall(); conn.close()
    success = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 Announcement\n\n{msg}")
            success += 1; await asyncio.sleep(0.05)
        except: pass
    await status.edit_text(f"✅ Sent to {success} users.")
    return ConversationHandler.END

async def admin_stats_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS: return
    
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users'); total_u = c.fetchone()[0]
    c.execute('SELECT SUM(credits) FROM users'); total_c = c.fetchone()[0] or 0
    c.execute('SELECT SUM(total_downloads) FROM users'); total_d = c.fetchone()[0] or 0
    conn.close()
    
    text = f"📊 Stats\n\n👥 Users: {total_u}\n💳 Credits: {total_c}\n📊 Downloads: {total_d}"
    keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ==================== MAIN & WEBHOOK ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data == "view_plans": await view_plans(update, context)
    elif data == "check_credits": await check_credits(update, context)
    elif data == "main_menu": await main_menu_callback(update, context)
    elif data == "start_download": 
        return await start_download_callback(update, context)
    elif data == "help_menu": await help_menu(update, context)
    elif data == "admin_panel": await admin_panel(update, context)
    elif data == "admin_toggle_free": await admin_toggle_free(update, context)
    elif data == "admin_set_limit": 
        return await admin_set_limit_start(update, context)
    elif data == "admin_toggle_maintenance": await admin_toggle_maintenance(update, context)
    elif data == "admin_broadcast": 
        return await admin_broadcast_start(update, context)
    elif data == "admin_stats": await admin_stats_panel(update, context)

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot and show menu"),
        BotCommand("credits", "Check your credits"),
        BotCommand("plans", "View premium plans"),
        BotCommand("admin", "Admin panel (admins only)"),
        BotCommand("cancel", "Cancel current operation"),
    ])

def create_application() -> Application:
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        sys.exit(1)
    
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0)
    application = Application.builder().token(TOKEN).request(request).job_queue(JobQueue()).post_init(post_init).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_download_callback, pattern="^start_download$")
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mobile)],
            EID_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_eid_captcha)],
            EID_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_eid_otp)],
            DOWNLOAD_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_download_captcha)],
            DOWNLOAD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_download_otp)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True, conversation_timeout=600
    )
    
    admin_limit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_limit_start, pattern="^admin_set_limit$")],
        states={ADMIN_SET_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_limit)]},
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("admin", admin_panel)], allow_reentry=True
    )
    
    admin_broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$")],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("admin", admin_panel)], allow_reentry=True
    )
    
    application.add_handler(conv_handler)
    application.add_handler(admin_limit_conv)
    application.add_handler(admin_broadcast_conv)
    application.add_handler(CommandHandler("credits", check_credits))
    application.add_handler(CommandHandler("plans", view_plans))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    return application

if __name__ == "__main__":
    init_db()
    logger.info("🤖 Starting Bot...")
    application = create_application()
    
    # Riven / Webhook Configuration
    PORT = int(os.environ.get("PORT", 3000))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
    
    if WEBHOOK_URL:
        WEBHOOK_URL = WEBHOOK_URL.rstrip('/')
        logger.info(f"✅ Running in WEBHOOK mode on port {PORT}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
            drop_pending_updates=True
        )
    else:
        logger.info("✅ Running in POLLING mode")
        application.run_polling(drop_pending_updates=True)
