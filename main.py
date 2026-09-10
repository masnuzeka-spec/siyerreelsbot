import os
import re
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq
from google import genai
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Render kapanmasın diye basit HTTP sunucusu
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot 7/24 Aktif Calisiyor!")

def run_fake_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def clean_youtube_url(url):
    """Linkteki /live/ veya takip kodlarını standart video formatına çevirir."""
    video_id_match = re.search(r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    if video_id_match:
        return f"https://www.youtube.com/watch?v={video_id_match.group(1)}"
    return url

def get_audio_and_transcript(youtube_url):
    """Videonun sesini doğrudan yt_dlp kütüphanesiyle indirir ve transkript alır."""
    cleaned_url = clean_youtube_url(youtube_url)
    audio_file = "temp_audio.m4a"
    
    if os.path.exists(audio_file):
        os.remove(audio_file)

    ydl_opts = {
        'format': 'm4a/bestaudio/best',
        'outtmpl': 'temp_audio.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
        'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'web']}},
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([cleaned_url])

    with open(audio_file, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_file, f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
    return transcription, cleaned_url

def find_best_segment(transcript_segments):
    """Gemini ile en etkileyici 35-50 saniyelik kesiti seçer."""
    prompt = f"""
    Sen usta bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
    Instagram Reels formatına en uygun, çarpıcı, düşündürücü, duygu yoğunluğu yüksek ve bağımsız 35-50 saniyelik aralığı bul.

    Transkript:
    {json.dumps(transcript_segments, ensure_ascii=False)}

    SADECE şu JSON şablonunda cevap ver:
    {{"start": 120.0, "end": 165.0, "reason": "Kesitin seçilme sebebi"}}
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def process_and_crop(youtube_url, start, end, output_filename="reels.mp4"):
    """Videonun sadece seçilen aralığını dikey (9:16) formatta kaydeder."""
    if os.path.exists(output_filename):
        os.remove(output_filename)

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best',
        'outtmpl': output_filename,
        'download_ranges': yt_dlp.utils.download_range_func(None, [(start, end)]),
        'force_keyframes_at_cuts': True,
        'postprocessor_args': {
            'ffmpeg': ['-vf', 'crop=ih*(9/16):ih,scale=1080:1920']
        },
        'quiet': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])

    return output_filename

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Selamlar Kral! Analiz edilmesini istediğin Siyer Vakfı video linkini gönder, en etkili kesiti hemen hazırlayayım.")

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_url = update.message.text.strip()
    if "youtube.com" not in raw_url and "youtu.be" not in raw_url:
        await update.message.reply_text("Lütfen geçerli bir YouTube linki gönder kral.")
        return

    status_msg = await update.message.reply_text("⏳ Ses çıkarılıyor ve transkript hazırlanıyor...")
    
    try:
        # 1. Temiz URL & Transkript
        transcription, clean_url = get_audio_and_transcript(raw_url)
        await status_msg.edit_text("🤖 Gemini en etkili kesiti seçiyor...")
        
        # 2. Vurucu Kesiti Bulma
        segment = find_best_segment(transcription.segments)
        start = float(segment["start"])
        end = float(segment["end"])
        reason = segment.get("reason", "Öne çıkan kesit")
        
        await status_msg.edit_text(f"✂️ Dikey Reels hazırlanıyor ({int(end - start)} sn)...")
        
        # 3. Kırpma
        output_file = process_and_crop(clean_url, start, end)
        
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
