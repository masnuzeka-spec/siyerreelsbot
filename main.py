import os
import re
import json
import threading
import subprocess
import requests
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

# Render uyku moduna geçmesin diye sahte web sunucusu
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Aktif ve YouTube'u By-Pass Ediyor!")

def run_fake_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def extract_video_id(url):
    match = re.search(r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None

def get_direct_urls(video_id):
    """yt-dlp KULLANMADAN, Piped API ile YouTube IP engellerini tamamen aşar."""
    instances = [
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.tokhmi.xyz",
        "https://api.piped.projectsegfau.lt"
    ]
    for inst in instances:
        try:
            r = requests.get(f"{inst}/streams/{video_id}", timeout=15)
            if r.status_code == 200:
                data = r.json()
                
                # Hem ses hem görüntü içeren (muxed) en iyi linki bul
                video_streams = [s for s in data.get("videoStreams", []) if not s.get("videoOnly", False)]
                if video_streams:
                    video_streams.sort(key=lambda x: int(x.get("quality", "0").replace("p", "")), reverse=True)
                    video_url = video_streams[0]["url"]
                else:
                    video_url = data.get("videoStreams", [{}])[0].get("url")
                    
                audio_streams = data.get("audioStreams", [])
                audio_url = audio_streams[0]["url"] if audio_streams else video_url
                
                if video_url and audio_url:
                    return video_url, audio_url
        except Exception:
            continue
    raise Exception("Piped API sunucuları yanıt vermedi. YouTube geçici bir engelleme yapıyor olabilir.")

def download_audio_and_transcribe(audio_url):
    """Doğrudan ham linkten FFmpeg ile sesi indirip Groq Whisper'a verir."""
    audio_file = "temp_audio.m4a"
    if os.path.exists(audio_file):
        os.remove(audio_file)
        
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-i", audio_url,
        "-vn", "-c:a", "aac", "-b:a", "64k",
        audio_file
    ]
    # Sesi hızlıca indir (Video çok uzun olsa bile ses çok ufaktır, Render'ı yormaz)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    with open(audio_file, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_file, f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
        
    os.remove(audio_file)
    return transcription.segments

def find_best_segment(transcript_segments):
    """Gemini 2.5 Flash ile 40 saniyelik en can alıcı kesiti seçer."""
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

def download_and_crop_video(video_url, start, end, output_filename="reels.mp4"):
    """Videonun tamamını indirmeden, SADECE seçilen saniyeleri bulur ve dikey olarak kaydeder."""
    if os.path.exists(output_filename):
        os.remove(output_filename)

    duration = end - start
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0",
        "-ss", str(start),
        "-i", video_url,
        "-t", str(duration),
        "-vf", filter_complex,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac",
        output_filename
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_filename

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Selamlar Kral! Siyer Vakfı video linkini gönder, en etkileyici kesiti hemen dikey Reels yapayım. (yt-dlp engelleri tamamen kaldırıldı!)")

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_url = update.message.text.strip()
    video_id = extract_video_id(raw_url)
    
    if not video_id:
        await update.message.reply_text("Geçerli bir YouTube linki tespit edilemedi kral.")
        return

    status_msg = await update.message.reply_text("⚡ Güvenlik kalkanları aşılıyor, doğrudan YouTube medya sunucularına bağlanılıyor...")
    
    try:
        # 1. IP engelini aşan temiz linkleri al
        video_url, audio_url = get_direct_urls(video_id)
        await status_msg.edit_text("🎙️ Ses indiriliyor ve Groq Whisper ile dinleniyor...")
        
        # 2. Sesi indir ve Transkript al
        segments = download_audio_and_transcribe(audio_url)
        await status_msg.edit_text("🤖 Gemini en etkili kesiti seçiyor...")
        
        # 3. En iyi yeri bul
        best = find_best_segment(segments)
        start = float(best["start"])
        end = float(best["end"])
        reason = best.get("reason", "Öne çıkan bölüm")
        
        await status_msg.edit_text(f"✂️ Sadece hedef saniyeler ({int(end - start)} sn) dikey olarak kırpılıyor...")
        
        # 4. Kes ve Dikey Yap (Hızlı Mod)
        output_file = download_and_crop_video(video_url, start, end)
        
        # 5. Telegram'a Gönder
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
    print("Bot dinliyor (Piped API By-Pass devrede)...")
    app.run_polling()
