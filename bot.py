import os
import json
import logging
import time
import traceback
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

print("GEMINI KEY:", GEMINI_API_KEY[:10] if GEMINI_API_KEY else "NONE")

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

# pakai model stabil
model = genai.GenerativeModel("gemini-pro")

# ===== GOOGLE SHEETS =====
def get_sheet():
    def _get():
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)

        # FIX newline private key
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

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
        ws = sheet.worksheet("Transaksi")

        data = ws.get_all_records()
        print("DATA:", data)

        if not data:
            return {
                "pemasukan": 0,
                "pengeluaran": 0,
                "sisa": 0
            }

        def to_int(val):
            try:
                return int(str(val).replace(".", "").replace(",", ""))
            except:
                return 0

        pemasukan = sum(
            to_int(r.get("Nominal"))
            for r in data
            if r.get("Tipe") == "pemasukan"
        )

        pengeluaran = sum(
            to_int(r.get("Nominal"))
            for r in data
            if r.get("Tipe") == "pengeluaran"
        )

        return {
            "pemasukan": pemasukan,
            "pengeluaran": pengeluaran,
            "sisa": pemasukan - pengeluaran
        }

    except Exception as e:
        print("🔥 SHEET ERROR:", str(e))
        traceback.print_exc()
        return None

# ===== AI =====
def process_with_ai(msg, data):
    try:
        prompt = f"""
Jawab singkat dan jelas.

Data:
Pemasukan: {data['pemasukan']}
Pengeluaran: {data['pengeluaran']}
Sisa: {data['sisa']}

User: {msg}
"""

        print("=== CALL GEMINI ===")

        res = model.generate_content(prompt)

        print("=== RESPONSE OBJECT ===", res)

        if not res:
            raise Exception("Response kosong")

        if not hasattr(res, "text") or not res.text:
            raise Exception(f"Tidak ada text. Full response: {res}")

        print("=== RAW TEXT ===", res.text)

        return {
            "response": res.text
        }

    except Exception as e:
        print("🔥 GEMINI ERROR:", str(e))
        traceback.print_exc()

        return {
            "response": f"❌ AI ERROR: {str(e)}"
        }

# ===== HANDLER =====
def handle_message(update: Update, context):
    try:
        print("=== MASUK HANDLE ===")

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

    except Exception as e:
        print("🔥 HANDLER ERROR:", str(e))
        traceback.print_exc()
        update.message.reply_text("❌ Error sistem")

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
        traceback.print_exc()

# ===== MAIN =====
def main():
    validate_env()

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)

    # FIX conflict polling
    updater.bot.delete_webhook()

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

    print("🚀 BOT RUNNING")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
