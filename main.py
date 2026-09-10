import os
import json
import subprocess
import requests
from groq import Groq
from google import genai

# --- AYARLAR VE ANAHTARLAR ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def send_telegram_video(video_path, caption):
    """Hazırlanan videoyu telefona onay için gönderir."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    with open(video_path, "rb") as video:
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "🚀 Instagram'da Paylaş", "callback_data": "publish"}],
                    [{"text": "🔄 Başka Kesit Bul", "callback_data": "retry"}]
                ]
            })
        }
        requests.post(url, data=data, files={"video": video})

def get_audio_and_transcript(youtube_url):
    """Videonun yalnızca sesini çeker ve Groq ile transkript alır."""
    # 1. Sesi hafif m4a olarak indir
    cmd_audio = f'yt-dlp -x --audio-format m4a -o "temp_audio.%(ext)s" "{youtube_url}"'
    subprocess.run(cmd_audio, shell=True, check=True)
    
    # 2. Groq Whisper ile transkript çıkar
    with open("temp_audio.m4a", "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=("temp_audio.m4a", f.read()),
            model="whisper-large-v3",
            response_format="verbose_json"
        )
    return transcription

def find_best_segment(transcript_segments):
    """Gemini ile en etkileyici 30-50 saniyelik aralığı bulur."""
    prompt = f"""
    Sen usta bir sosyal medya editörüsün. Aşağıdaki konuşma metni Siyer Vakfı sohbetine aittir.
    Bu metinden Instagram Reels için en çarpıcı, öğüt verici, duygu yoğunluğu yüksek ve tek başına dinlendiğinde anlamlı olan 35-50 saniyelik kesiti bul.

    Zaman damgalı metin:
    {json.dumps(transcript_segments, ensure_ascii=False)}

    SADECE aşağıdaki JSON formatında çıktı ver:
    {{"start": 120.5, "end": 165.2, "reason": "Burada anlatılan kıssa çok etkileyici"}}
    """
    
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def process_and_crop(youtube_url, start, end, output_filename="reels.mp4"):
    """Videonun sadece o aralığını indirir ve dikey (9:16) formata çevirir."""
    duration = end - start
    # yt-dlp ile doğrudan o aralığı çekip FFmpeg ile 1080x1920 dikey kırpma
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    cmd = (
        f'yt-dlp -ss {start} -to {end} "{youtube_url}" '
        f'-f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4" '
        f'--postprocessor-args "ffmpeg:-vf {filter_complex}" '
        f'-o "{output_filename}" --force-overwrites'
    )
    subprocess.run(cmd, shell=True, check=True)
    return output_filename