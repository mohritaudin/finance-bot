import os
import json
import logging
import gspread
import google.generativeai as genai
from datetime import datetime, date
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ====== LOGGING ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== KONFIGURASI ======
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
YOUR_CHAT_ID = os.environ.get("YOUR_CHAT_ID")

# ====== SETUP GEMINI ======
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ====== SETUP GOOGLE SHEETS ======
def get_sheet():
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

# ====== AMBIL DATA KEUANGAN ======
def get_financial_summary():
    try:
        sheet = get_sheet()
        transaksi = sheet.worksheet("Transaksi").get_all_records()
        wishlist = sheet.worksheet("Wishlist").get_all_records()
        rencana = sheet.worksheet("Rencana").get_all_records()

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
        logger.error(f"Error get_financial_summary: {e}")
        return None

# ====== TAMBAH DATA KE SHEET ======
def add_to_sheet(worksheet_name, row_data):
    try:
        sheet = get_sheet()
        ws = sheet.worksheet(worksheet_name)
        ws.append_row(row_data)
        return True
    except Exception as e:
        logger.error(f"Error add_to_sheet: {e}")
        return False

# ====== PROSES PESAN DENGAN AI ======
def process_with_ai(user_message, financial_data):
    today = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
Kamu adalah asisten keuangan pribadi yang ramah dan berbahasa Indonesia.
Hari ini: {today}

Data keuangan bulan ini:
- Pemasukan: Rp {financial_data['pemasukan']:,}
- Pengeluaran: Rp {financial_data['pengeluaran']:,}
- Sisa uang: Rp {financial_data['sisa']:,}
- Total rencana pengeluaran: Rp {financial_data['total_rencana']:,}
- Sisa setelah rencana: Rp {financial_data['sisa_setelah_rencana']:,}
- Wishlist: {json.dumps(financial_data['wishlist'], ensure_ascii=False)}
- Rencana belum terlaksana: {json.dumps(financial_data['rencana_belum'], ensure_ascii=False)}

Pesan dari pengguna: "{user_message}"

Tugasmu:
1. Tentukan INTENT dari pesan (catat_pemasukan / catat_pengeluaran / catat_wishlist / catat_rencana / tanya_saldo / tanya_bisa_beli / pertanyaan_umum)
2. Jika mencatat transaksi, ekstrak: tanggal (default hari ini), deskripsi, nominal (dalam angka saja), kategori
3. Berikan respons yang ramah dan informatif
4. Jika ditanya apakah bisa beli sesuatu, analisis berdasarkan data keuangan

Balas dalam format JSON:
{{
  "intent": "...",
  "action": {{
    "type": "tambah_transaksi" | "tambah_wishlist" | "tambah_rencana" | "tidak_ada",
    "data": {{
      "tanggal": "YYYY-MM-DD",
      "tipe": "pemasukan" | "pengeluaran",
      "kategori": "...",
      "deskripsi": "...",
      "nominal": 0,
      "nama_barang": "...",
      "harga": 0,
      "tanggal_rencana": "...",
      "status": "belum"
    }}
  }},
  "response": "pesan balasan ke pengguna"
}}
"""
    
    response = model.generate_content(prompt)
    text = response.text.strip()
    
    # Bersihkan markdown jika ada
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    
    return json.loads(text.strip())

# ====== HANDLER PESAN ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = str(update.message.chat_id)
    
    # Keamanan: hanya terima dari chat ID kamu
    if YOUR_CHAT_ID and chat_id != YOUR_CHAT_ID:
        await update.message.reply_text("Maaf, bot ini bersifat privat.")
        return

    await update.message.reply_text("⏳ Sedang memproses...")

    try:
        financial_data = get_financial_summary()
        if not financial_data:
            await update.message.reply_text("❌ Gagal mengambil data keuangan.")
            return

        result = process_with_ai(user_message, financial_data)
        
        # Eksekusi aksi jika ada
        action = result.get("action", {})
        if action.get("type") == "tambah_transaksi":
            d = action["data"]
            success = add_to_sheet("Transaksi", [
                d.get("tanggal", datetime.now().strftime("%Y-%m-%d")),
                d.get("tipe", ""),
                d.get("kategori", ""),
                d.get("deskripsi", ""),
                d.get("nominal", 0)
            ])
            if not success:
                await update.message.reply_text("❌ Gagal menyimpan ke spreadsheet.")
                return

        elif action.get("type") == "tambah_wishlist":
            d = action["data"]
            success = add_to_sheet("Wishlist", [
                datetime.now().strftime("%Y-%m-%d"),
                d.get("nama_barang", ""),
                d.get("harga", 0),
                "diinginkan"
            ])
            if not success:
                await update.message.reply_text("❌ Gagal menyimpan ke spreadsheet.")
                return

        elif action.get("type") == "tambah_rencana":
            d = action["data"]
            success = add_to_sheet("Rencana", [
                d.get("tanggal_rencana", ""),
                d.get("deskripsi", ""),
                d.get("nominal", 0),
                "belum"
            ])
            if not success:
                await update.message.reply_text("❌ Gagal menyimpan ke spreadsheet.")
                return

        await update.message.reply_text(result.get("response", "✅ Selesai!"))

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Terjadi error: {str(e)}")

# ====== RANGKUMAN BULANAN ======
async def send_monthly_summary(app):
    try:
        financial_data = get_financial_summary()
        bulan = datetime.now().strftime("%B %Y")
        
        prompt = f"""
Buat rangkuman keuangan bulan {bulan} yang informatif dan berikan saran dalam bahasa Indonesia.

Data:
- Pemasukan: Rp {financial_data['pemasukan']:,}
- Pengeluaran: Rp {financial_data['pengeluaran']:,}
- Sisa: Rp {financial_data['sisa']:,}
- Semua transaksi: {json.dumps(financial_data['semua_transaksi'], ensure_ascii=False)}

Format:
📊 RANGKUMAN BULAN {bulan.upper()}
[rangkuman singkat]

💰 PEMASUKAN: ...
💸 PENGELUARAN: ...
💵 SISA: ...

📈 ANALISIS:
[analisis pengeluaran terbesar, pola belanja, dll]

💡 SARAN:
[3-5 saran spesifik untuk bulan depan]
"""
        response = model.generate_content(prompt)
        
        await app.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=response.text
        )
    except Exception as e:
        logger.error(f"Error monthly summary: {e}")

# ====== MAIN ======
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Scheduler untuk rangkuman bulanan (setiap tanggal 1, jam 08:00)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_monthly_summary,
        "cron",
        day=1,
        hour=8,
        minute=0,
        args=[app]
    )
    scheduler.start()
    
    logger.info("Bot berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
