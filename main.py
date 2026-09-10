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
from pytubefix import YouTube

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Render uyku moduna geçmesin diye
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Aktif ve IP Korumalari Asildi!")

def run_fake_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def extract_video_id(url):
    match = re.search(r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None

def get_media_url_and_audio(video_id):
    """Pytubefix ve Invidious ile YouTube kalkanını çift katmanlı aşar."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    audio_file = "temp_audio.m4a"
    if os.path.exists(audio_file):
        os.remove(audio_file)
        
    try:
        # YÖNTEM 1: Pytubefix (Dahili PoToken sahte kimliği atlatıcısı)
        yt = YouTube(url, client='WEB')
        
        # Sesi indir (Groq transkripti için)
        audio_stream = yt.streams.get_audio_only()
        audio_stream.download(filename=audio_file)
        
        # Görüntü + Ses ham URL'sini al (FFmpeg ile kırpmak için)
        video_stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        
        return audio_file, video_stream.url
        
    except Exception as e:
        # YÖNTEM 2: Invidious API (Yedek Zırh)
        print(f"Pytubefix takıldı, Invidious deneniyor: {e}")
        instances = [
            "https://inv.tux.pizza", 
            "https://vid.puffyan.us", 
            "https://invidious.jing.rocks",
            "https://invidious.nerdvpn.de"
        ]
        for inst in instances:
            try:
                r = requests.get(f"{inst}/api/v1/videos/{video_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    formats = data.get("formatStreams", [])
                    if not formats:
                        continue
                    
                    formats.sort(key=lambda x: int(x.get("resolution", "0p").replace("p", "")), reverse=True)
                    video_url = formats[0]["url"]
                    
                    # Invidious üzerinden sesi FFmpeg ile indir
                    cmd = ["ffmpeg", "-y", "-i", video_url, "-vn", "-c:a", "aac", audio_file]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    return audio_file, video_url
            except Exception:
                continue
                
        raise Exception("Tüm güvenlik aşma yöntemleri başarısız oldu. YouTube tam blokaj uyguluyor olabilir.")

def transcribe_audio(audio_file):
    """Groq ile sesi metne döker."""
    with open(audio_file, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_file, f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
    os.remove(audio_file)
    return transcription.segments

def find_best_segment(transcript_segments):
    """Gemini ile en vurucu aralığı bulur."""
    condensed = [{"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]} for s in transcript_segments[:500]]
    
    prompt = f"""
    Sen usta bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
    Instagram Reels formatına en uygun, çarpıcı, düşündürücü, duygu yoğunluğu yüksek ve tek başına dinlendiğinde anlamlı olan 35-50 saniyelik kesiti seç.

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
    """Belirlenen aralığı bulut üzerinden doğrudan indirip dikey keser."""
    if os.path.exists(output_filename):
        os.remove(output_filename)

    duration = end - start
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
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
    await update.message.reply_text("Selamlar Kral! Bot yepyeni çift katmanlı zırhla güncellendi. İstediğin Siyer Vakfı linkini gönder.")

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_url = update.message.text.strip()
    video_id = extract_video_id(raw_url)
    
    if not video_id:
        await update.message.reply_text("Geçerli bir YouTube linki bulamadım kral.")
        return

    status_msg = await update.message.reply_text("⚡ Güvenlik kalkanları Pytubefix Zırhı ile aşılıyor...")
    
    try:
        # 1. Bypass ve Medya Bağlantıları
        audio_file, video_url = get_media_url_and_audio(video_id)
        await status_msg.edit_text("🎙️ Ses çözümleniyor (Groq Whisper)...")
        
        # 2. Transkript
        segments = transcribe_audio(audio_file)
        await status_msg.edit_text("🤖 Gemini vurucu kesiti arıyor...")
        
        # 3. En iyi yeri bul
        best = find_best_segment(segments)
        start = float(best["start"])
        end = float(best["end"])
        reason = best.get("reason", "Öne çıkan bölüm")
        
        await status_msg.edit_text(f"✂️ Dikey Reels kırpılıyor ({int(end - start)} sn)...")
        
        # 4. FFmpeg Kırpma
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
        await status_msg.edit_text(f"❌ Hata: {str(e)}")

if __name__ == "__main__":
    threading.Thread(target=run_fake_web_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url))
    print("Çift katmanlı bypass botu dinliyor...")
    app.run_polling()
