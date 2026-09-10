import os
import json
import subprocess
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Çevre Değişkenleri
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Render'ın "Application exited" hatası vermemesi için arka plan web sunucusu
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot 7/24 Aktif Calisiyor!")

def run_fake_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def get_audio_and_transcript(youtube_url):
    """Videonun yalnızca sesini hafif formatta indirir ve Groq Whisper ile çözer."""
    if os.path.exists("temp_audio.m4a"):
        os.remove("temp_audio.m4a")
        
    cmd_audio = f'yt-dlp -x --audio-format m4a -o "temp_audio.%(ext)s" "{youtube_url}"'
    subprocess.run(cmd_audio, shell=True, check=True)
    
    with open("temp_audio.m4a", "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=("temp_audio.m4a", f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
    return transcription

def find_best_segment(transcript_segments):
    """Gemini ile en etkileyici 35-50 saniyelik aralığı bulur."""
    prompt = f"""
    Sen usta bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
    Instagram Reels formatına en uygun, çarpıcı, düşündürücü, duygu yoğunluğu yüksek ve bağımsız 35-50 saniyelik aralığı bul.

    Transkript:
    {json.dumps(transcript_segments, ensure_ascii=False)}

    SADECE şu JSON şablonunda cevap ver:
    {{"start": 120.0, "end": 165.0, "reason": "Kesitin çarpıcı olma sebebi"}}
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def process_and_crop(youtube_url, start, end, output_filename="reels.mp4"):
    """Videonun seçilen aralığını dikey (9:16) formatta indirir."""
    if os.path.exists(output_filename):
        os.remove(output_filename)
        
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    cmd = (
        f'yt-dlp -ss {start} -to {end} "{youtube_url}" '
        f'-f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4" '
        f'--postprocessor-args "ffmpeg:-vf {filter_complex}" '
        f'-o "{output_filename}" --force-overwrites'
    )
    subprocess.run(cmd, shell=True, check=True)
    return output_filename

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Selamlar Kral! Bana analiz etmemi istediğin Siyer Vakfı YouTube video linkini at, hemen en vurucu Reels kesitini hazırlayayım.")

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text("Lütfen geçerli bir YouTube video linki gönder kral.")
        return

    status_msg = await update.message.reply_text("⏳ Video taranıyor, ses transkripti çıkarılıyor...")
    
    try:
        # 1. Transkript
        transcription = get_audio_and_transcript(url)
        await status_msg.edit_text("🤖 Gemini en etkili kesiti tespit ediyor...")
        
        # 2. Vurucu kesiti bulma
        segment = find_best_segment(transcription.segments)
        start = segment["start"]
        end = segment["end"]
        reason = segment.get("reason", "Öne çıkan bölüm")
        
        await status_msg.edit_text(f"✂️ Dikey Reels videosu kesiliyor ({int(end - start)} sn)...")
        
        # 3. Kırpma
        output_file = process_and_crop(url, start, end)
        
        # 4. Telegram'a gönderme
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
    # 1. Render için HTTP sunucusunu arka planda başlat
    threading.Thread(target=run_fake_web_server, daemon=True).start()
    
    # 2. Telegram Botunu başlat ve sürekli dinle
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url))
    
    print("Bot basariyla baslatildi ve dinliyor...")
    app.run_polling()
