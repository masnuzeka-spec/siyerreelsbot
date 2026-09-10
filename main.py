import os
import re
import json
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Render'ı uyanık tutan sunucu
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

def get_audio_and_transcribe(video_id):
    """Sesi YouTube bot engeline takılmadan Android kimliğiyle indirir ve Groq ile çözer."""
    audio_file = "temp_audio.m4a"
    if os.path.exists(audio_file):
        os.remove(audio_file)

    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # YouTube bot korumasını aşan istemci parametresi
    cmd = [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android_creator,android",
        "-x",
        "--audio-format", "m4a",
        "-o", audio_file,
        "--force-overwrites",
        url
    ]
    subprocess.run(cmd, check=True)

    # Groq Whisper ile sesi metne dönüştür
    with open(audio_file, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_file, f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
    
    # İşi biten ses dosyasını sil
    if os.path.exists(audio_file):
        os.remove(audio_file)

    return transcription.segments

def find_best_segment(transcript_segments):
    """Gemini ile en vurucu 35-50 saniyelik kesiti bulur."""
    # Metin parçalarını kompakt hale getir
    condensed = [{"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]} for s in transcript_segments[:500]]
    
    prompt = f"""
    Sen tecrübeli bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
    Instagram Reels formatına en uygun, çarpıcı, öğüt verici, duygu yoğunluğu yüksek ve tek başına dinlendiğinde anlamlı olan 35-50 saniyelik kesiti seç.

    Transkript:
    {json.dumps(condensed, ensure_ascii=False)}

    SADECE şu JSON şablonunda cevap ver:
    {{"start": 120.0, "end": 165.0, "reason": "Kesitin seçilme gerekçesi"}}
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def download_and_crop(video_id, start, end, output_filename="reels.mp4"):
    """Sadece seçilen aralığı dikey (9:16) formatta indirir."""
    if os.path.exists(output_filename):
        os.remove(output_filename)

    url = f"https://www.youtube.com/watch?v={video_id}"
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    
    cmd = [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android_creator,android",
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

    status_msg = await update.message.reply_text("🎙️ Ses indiriliyor ve Groq Whisper ile dinleniyor...")
    
    try:
        # 1. Groq ile doğrudan sesi dinleyip metne dökme
        segments = get_audio_and_transcribe(video_id)
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
    print("Bot hazır ve dinliyor...")
    app.run_polling()
