import os
import re
import json
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Render kapanmasın diye port dinleyici
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Aktif!")

def run_fake_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def extract_video_id(url):
    match = re.search(r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None

def get_transcript_via_ytdlp(video_id):
    """Harici kütüphane kullanmadan doğrudan yt-dlp ile Türkçe altyazıyı çeker."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-lang", "tr",
        "--sub-format", "json3",
        "-o", "subs.%(ext)s",
        "--force-overwrites",
        url
    ]
    subprocess.run(cmd, check=True)
    
    sub_file = "subs.tr.json3"
    if not os.path.exists(sub_file):
        raise Exception("Bu videoda Türkçe altyazı veya otomatik altyazı bulunamadı.")

    with open(sub_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Geçici altyazı dosyasını sil
    os.remove(sub_file)

    formatted_segments = []
    events = data.get("events", [])
    for ev in events:
        if "segs" in ev and "tStartMs" in ev:
            text = "".join([s.get("utf8", "") for s in ev["segs"]]).strip()
            if text and text != "\n":
                start_sec = round(ev["tStartMs"] / 1000.0, 1)
                duration_sec = round(ev.get("dDurationMs", 2000) / 1000.0, 1)
                formatted_segments.append({
                    "start": start_sec,
                    "end": round(start_sec + duration_sec, 1),
                    "text": text
                })

    return formatted_segments

def find_best_segment(transcript_segments):
    """Gemini ile en etkileyici 35-50 saniyelik aralığı bulur."""
    prompt = f"""
    Sen tecrübeli bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
    Instagram Reels formatına en uygun, çarpıcı, öğüt verici, duygu yoğunluğu yüksek ve tek başına dinlendiğinde anlamlı olan 35-50 saniyelik kesiti seç.

    Transkript parçaları:
    {json.dumps(transcript_segments[:500], ensure_ascii=False)}

    SADECE şu JSON şablonunda cevap ver:
    {{"start": 120.0, "end": 165.0, "reason": "Kesitin seçilme sebebi"}}
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def download_and_crop(video_id, start, end, output_filename="reels.mp4"):
    """Videonun o aralığını dikey (9:16) formatta indirir."""
    if os.path.exists(output_filename):
        os.remove(output_filename)

    url = f"https://www.youtube.com/watch?v={video_id}"
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    
    cmd = [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android",
        "--download-sections", f"*{start}-{end}",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best",
        "--postprocessor-args", f"ffmpeg:-vf {filter_complex}",
        "-o", output_filename,
        "--force-overwrites",
        url
    ]
    subprocess.run(cmd, check=True)
    return output_filename

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Selamlar Kral! Siyer Vakfı video linkini gönder, en etkileyici kesiti hemen dikey Reels yapayım.")

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_url = update.message.text.strip()
    video_id = extract_video_id(raw_url)
    
    if not video_id:
        await update.message.reply_text("Geçerli bir YouTube linki tespit edilemedi kral.")
        return

    status_msg = await update.message.reply_text("⚡ Konuşma metni doğrudan YouTube'dan çekiliyor...")
    
    try:
        # 1. Altyazı çekme
        segments = get_transcript_via_ytdlp(video_id)
        await status_msg.edit_text("🤖 Gemini en etkili kesiti seçiyor...")
        
        # 2. Vurucu Kesiti Bulma
        best = find_best_segment(segments)
        start = float(best["start"])
        end = float(best["end"])
        reason = best.get("reason", "Öne çıkan bölüm")
        
        await status_msg.edit_text(f"✂️ Dikey Reels hazırlanıyor ({int(end - start)} sn)...")
        
        # 3. Kesme ve Dikey Yapma
        output_file = download_and_crop(video_id, start, end)
        
        # 4. Telegram'a Gönderme
        caption = f"🎬 **Reels Kesiti Hazır!**\n\n📌 Süre: {int(end - start)} sn\n💡 Vurgu: {reason}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Instagram'da Paylaş", callback_data="share")],
            [InlineKeyboardButton("🔄 Başka Kesit Bul", callback_data="retry")]
        ])
        
        with open(output_file, "rb") as video:
            await update.message.reply_video(video=video, caption=caption, reply_markup=keyboard)
        await status_msg.delete()
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Bir hata oluştu:\n{str(e)}")

if __name__ == "__main__":
    threading.Thread(target=run_fake_web_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url))
    print("Bot dinliyor...")
    app.run_polling()
