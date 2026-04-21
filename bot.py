import os
import json
import logging
import time
import pytz
import gspread
import google.generativeai as genai

from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters
from telegram import Update
from apscheduler.schedulers.background import BackgroundScheduler

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
YOUR_CHAT_ID = os.getenv("YOUR_CHAT_ID")

# ===== VALIDATE ENV =====
def validate_env():
    required = [
        "TELEGRAM_TOKEN",
        "GEMINI_API_KEY",
        "SPREADSHEET_ID",
        "SERVICE_ACCOUNT_JSON",
        "YOUR_CHAT_ID"
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise Exception(f"ENV missing: {', '.join(missing)}")

# ===== RETRY =====
def retry(func, retries=3, delay=2):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            logger.warning(f"Retry {i+1}: {e}")
            time.sleep(delay)
    raise Exception("Max retry reached")

# ===== GEMINI =====
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ===== GOOGLE SHEETS =====
def get_sheet():
    def _get():
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        client = gspread.authorize(creds)
        return client.open_by_key(SPREADSHEET_ID)
    return retry(_get)

# ===== DATA =====
def get_financial_summary():
    try:
        sheet = get_sheet()

        transaksi = retry(lambda: sheet.worksheet("Transaksi").get_all_records())
        wishlist = retry(lambda: sheet.worksheet("Wishlist").get_all_records())
        rencana = retry(lambda: sheet.worksheet("Rencana").get_all_records())

        bulan_ini = datetime.now().strftime("%Y-%m")

        def to_int(val):
            return int(str(val).replace(".", "").replace(",", ""))

        pemasukan = sum(
            to_int(r["Nominal"])
            for r in transaksi
            if r["Tipe"] == "pemasukan" and str(r["Tanggal"]).startswith(bulan_ini)
        )

        pengeluaran = sum(
            to_int(r["Nominal"])
            for r in transaksi
            if r["Tipe"] == "pengeluaran" and str(r["Tanggal"]).startswith(bulan_ini)
        )

        sisa = pemasukan - pengeluaran

        return {
            "pemasukan": pemasukan,
            "pengeluaran": pengeluaran,
            "sisa": sisa
        }

    except Exception:
        logger.exception("Financial error")
        return None

# ===== AI =====
def process_with_ai(msg, data):
    try:
        prompt = f"""
Pemasukan: {data['pemasukan']}
Pengeluaran: {data['pengeluaran']}
Sisa: {data['sisa']}

User: {msg}

Balas JSON:
{{"response":"..."}}
"""

        res = model.generate_content(prompt, request_options={"timeout": 10})
        text = res.text.strip()

        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()

        return json.loads(text)

    except Exception:
        logger.exception("AI error")
        return {"response": "⚠️ AI error"}

# ===== HANDLER =====
def handle_message(update: Update, context):
    try:
        chat_id = str(update.message.chat_id)

        if YOUR_CHAT_ID and chat_id != YOUR_CHAT_ID:
            return

        update.message.reply_text("⏳ Processing...")

        data = get_financial_summary()
        if not data:
            update.message.reply_text("❌ Data error")
            return

        result = process_with_ai(update.message.text, data)
        update.message.reply_text(result.get("response", "OK"))

    except Exception:
        logger.exception("Handler error")
        update.message.reply_text("❌ Error")

# ===== SCHEDULER =====
def send_summary(bot):
    try:
        data = get_financial_summary()
        if not data:
            return

        msg = f"""
📊 SUMMARY
Pemasukan: {data['pemasukan']}
Pengeluaran: {data['pengeluaran']}
Sisa: {data['sisa']}
"""
        bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

    except Exception:
        logger.exception("Scheduler error")

# ===== MAIN =====
def main():
    validate_env()

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    scheduler = BackgroundScheduler(
        timezone=pytz.timezone("Asia/Jakarta")
    )

    scheduler.add_job(
        send_summary,
        "cron",
        day=1,
        hour=8,
        minute=0,
        timezone=pytz.timezone("Asia/Jakarta"),
        args=[updater.bot]
    )

    scheduler.start()

    logger.info("Bot running...")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
