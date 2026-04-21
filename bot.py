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

        print("STEP 1: sheet connected")

        ws = sheet.worksheet("Transaksi")
        print("STEP 2: worksheet found")

        data = ws.get_all_records()
        print("STEP 3: data fetched:", data)

        if not data:
            print("STEP 4: data kosong")
            return {
                "pemasukan": 0,
                "pengeluaran": 0,
                "sisa": 0
            }

        def to_int(val):
            try:
                return int(str(val).replace(".", "").replace(",", ""))
            except Exception as e:
                print("ERROR PARSE NOMINAL:", val, e)
                return 0

        pemasukan = sum(
            to_int(r.get("Nominal", 0))
            for r in data
            if r.get("Tipe") == "pemasukan"
        )

        pengeluaran = sum(
            to_int(r.get("Nominal", 0))
            for r in data
            if r.get("Tipe") == "pengeluaran"
        )

        print("STEP 5: success")

        return {
            "pemasukan": pemasukan,
            "pengeluaran": pengeluaran,
            "sisa": pemasukan - pengeluaran
        }

    except Exception as e:
        print("🔥 ERROR BESAR:", str(e))
        import traceback
        traceback.print_exc()
        return None

# ===== AI =====
def process_with_ai(msg, data):
    try:
        prompt = f"""
Balas dengan JSON VALID tanpa teks tambahan.

Format:
{{"response":"..."}} 

Data:
Pemasukan: {data['pemasukan']}
Pengeluaran: {data['pengeluaran']}
Sisa: {data['sisa']}

User: {msg}
"""

        res = model.generate_content(
            prompt,
            request_options={"timeout": 10}
        )

        if not res or not res.text:
            raise Exception("Empty response from AI")

        text = res.text.strip()

        print("RAW AI:", text)

        # bersihin markdown
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()

        try:
            return json.loads(text)
        except:
            return {
                "response": text  # fallback langsung tampilkan
            }

    except Exception as e:
        print("🔥 AI ERROR:", str(e))
        return {
            "response": "⚠️ AI lagi bermasalah, coba lagi"
        }

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
    print("GEMINI KEY:", GEMINI_API_KEY[:10])

if __name__ == "__main__":
    main()
