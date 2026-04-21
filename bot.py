import os
import json
import logging
import time
import gspread
import google.generativeai as genai

from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters
from telegram import Update
from apscheduler.schedulers.background import BackgroundScheduler

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ====== ENV ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
YOUR_CHAT_ID = os.environ.get("YOUR_CHAT_ID")

# ====== VALIDASI ENV ======
def validate_env():
    required = [
        "TELEGRAM_TOKEN",
        "GEMINI_API_KEY",
        "SPREADSHEET_ID",
        "SERVICE_ACCOUNT_JSON",
        "YOUR_CHAT_ID"
    ]
    
    missing = [k for k in required if not os.environ.get(k)]
    
    if missing:
        raise Exception(f"ENV missing: {', '.join(missing)}")

# ====== RETRY HELPER ======
def retry(func, retries=3, delay=2):
    for i in range(retries):
        try:
            return func()
        except Exception as e:
            logger.warning(f"Retry {i+1}/{retries} failed: {e}")
            time.sleep(delay)
    raise Exception("Max retry reached")

# ====== GEMINI SETUP ======
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ====== GOOGLE SHEETS ======
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

# ====== GET FINANCIAL ======
def get_financial_summary():
    try:
        sheet = get_sheet()

        transaksi = retry(lambda: sheet.worksheet("Transaksi").get_all_records())
        wishlist = retry(lambda: sheet.worksheet("Wishlist").get_all_records())
        rencana = retry(lambda: sheet.worksheet("Rencana").get_all_records())

        bulan_ini = datetime.now().strftime("%Y-%m")

        pemasukan = sum(
            int(str(r["Nominal"]).replace(".", "").replace(",", ""))
            for r in transaksi
            if r["Tipe"] == "pemasukan" and str(r["Tanggal"]).startswith(bulan_ini)
        )

        pengeluaran = sum(
            int(str(r["Nominal"]).replace(".", "").replace(",", ""))
            for r in transaksi
            if r["Tipe"] == "pengeluaran" and str(r["Tanggal"]).startswith(bulan_ini)
        )

        sisa = pemasukan - pengeluaran

        rencana_belum = [r for r in rencana if r.get("Status") == "belum"]

        total_rencana = sum(
            int(str(r["Nominal"]).replace(".", "").replace(",", ""))
            for r in rencana_belum
        )

        return {
            "pemasukan": pemasukan,
            "pengeluaran": pengeluaran,
            "sisa": sisa,
            "wishlist": wishlist,
            "rencana_belum": rencana_belum,
            "total_rencana": total_rencana,
            "sisa_setelah_rencana": sisa - total_rencana,
            "semua_transaksi": transaksi
        }

    except Exception as e:
        logger.exception("Error get_financial_summary")
        return None

# ====== ADD TO SHEET ======
def add_to_sheet(worksheet_name, row_data):
    try:
        sheet = get_sheet()
        ws = sheet.worksheet(worksheet_name)
        retry(lambda: ws.append_row(row_data))
        return True
    except Exception as e:
        logger.exception("Error add_to_sheet")
        return False

# ====== AI PROCESS ======
def process_with_ai(user_message, financial_data):
    try:
        today = datetime.now().strftime("%Y-%m-%d")

        prompt = f"""
Kamu adalah asisten keuangan pribadi berbahasa Indonesia.

Hari ini: {today}

Data:
- Pemasukan: Rp {financial_data['pemasukan']:,}
- Pengeluaran: Rp {financial_data['pengeluaran']:,}
- Sisa: Rp {financial_data['sisa']:,}

Pesan: "{user_message}"

Balas dalam JSON:
{{
  "intent": "...",
  "action": {{"type": "...", "data": {{}}}},
  "response": "..."
}}
"""

        response = model.generate_content(
            prompt,
            request_options={"timeout": 10}
        )

        text = response.text.strip()

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            return json.loads(text)
        except Exception:
            logger.error(f"Invalid JSON AI: {text}")
            return {
                "intent": "error",
                "action": {"type": "tidak_ada"},
                "response": "⚠️ AI tidak memberikan respon valid."
            }

    except Exception as e:
        logger.exception("Gemini error")
        return {
            "intent": "error",
            "action": {"type": "tidak_ada"},
            "response": "❌ AI error, coba lagi nanti."
        }

# ====== HANDLER ======
def handle_message(update: Update, context):
    try:
        user_message = update.message.text
        chat_id = str(update.message.chat_id)

        if YOUR_CHAT_ID and chat_id != YOUR_CHAT_ID:
            update.message.reply_text("Bot privat.")
            return

        update.message.reply_text("⏳ Processing...")

        financial_data = get_financial_summary()
        if not financial_data:
            update.message.reply_text("❌ Gagal ambil data.")
            return

        result = process_with_ai(user_message, financial_data)

        action = result.get("action", {})

        try:
            if action.get("type") == "tambah_transaksi":
                d = action["data"]
                add_to_sheet("Transaksi", [
                    d.get("tanggal", datetime.now().strftime("%Y-%m-%d")),
                    d.get("tipe", ""),
                    d.get("kategori", ""),
                    d.get("deskripsi", ""),
                    d.get("nominal", 0)
                ])

        except Exception:
            logger.exception("Action error")

        update.message.reply_text(result.get("response", "OK"))

    except Exception:
        logger.exception("FATAL ERROR")
        update.message.reply_text("❌ Error sistem")

# ====== SCHEDULER ======
def send_monthly_summary(bot):
    try:
        data = get_financial_summary()
        if not data:
            return

        msg = f"""
📊 RANGKUMAN
Pemasukan: {data['pemasukan']}
Pengeluaran: {data['pengeluaran']}
Sisa: {data['sisa']}
"""
        bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

    except Exception:
        logger.exception("Scheduler error")

# ====== MAIN ======
def main():
    validate_env()

    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_monthly_summary,
        "cron",
        day=1,
        hour=8,
        args=[updater.bot]
    )
    scheduler.start()

    logger.info("Bot running...")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
