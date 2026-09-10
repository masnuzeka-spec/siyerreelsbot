import os
import re
import json
import time
import requests
import yt_dlp

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(text):
    requests.post(f"{TG_API}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def send_video(video_path, caption):
    with open(video_path, "rb") as f:
        requests.post(
            f"{TG_API}/sendVideo",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"video": f}
        )

def extract_video_id(url):
    match = re.search(r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None

def get_audio_and_transcribe(video_id):
    audio_file = "temp_audio.m4a"
    if os.path.exists(audio_file):
        os.remove(audio_file)
        
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'format': 'm4a/bestaudio/best',
        'outtmpl': 'temp_audio.%(ext)s',
        'overwrites': True,
        'quiet': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }]
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
        
    api_url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(audio_file, "rb") as f:
        files = {"file": (audio_file, f, "audio/m4a")}
        data = {
            "model": "whisper-large-v3",
            "response_format": "verbose_json"
        }
        res = requests.post(api_url, headers=headers, files=files, data=data, timeout=120)
    
    if os.path.exists(audio_file):
        os.remove(audio_file)
        
    res_json = res.json()
    return res_json.get("segments", [])

def find_best_segment(transcript_segments):
    condensed = [{"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]} for s in transcript_segments[:400]]
    prompt = f"""
Sen usta bir sosyal medya editörüsün. Aşağıdaki metin Siyer Vakfı dersine aittir.
Instagram Reels formatına en uygun, çarpıcı, öğüt verici, duygu yoğunluğu yüksek ve tek başına dinlendiğinde anlamlı olan 35-50 saniyelik kesiti seç.

Transkript:
{json.dumps(condensed, ensure_ascii=False)}

SADECE şu JSON şablonunda cevap ver, başka hiçbir metin ekleme:
{{"start": 120.0, "end": 165.0, "reason": "Kesitin seçilme gerekçesi"}}
"""
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    r = requests.post(url, json=payload, timeout=30)
    res_data = r.json()
    
    if "candidates" not in res_data:
        raise Exception(f"Gemini API Hatası: {json.dumps(res_data, ensure_ascii=False)}")
        
    text = res_data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

def download_and_crop(video_id, start, end, output_filename="reels.mp4"):
    if os.path.exists(output_filename):
        os.remove(output_filename)

    url = f"https://www.youtube.com/watch?v={video_id}"
    filter_complex = "crop=ih*(9/16):ih,scale=1080:1920"
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best',
        'outtmpl': output_filename,
        'overwrites': True,
        'quiet': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
        'download_ranges': yt_dlp.utils.download_range_func(None, [(start, end)]),
        'postprocessor_args': {'ffmpeg': ['-vf', filter_complex]}
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
        
    return output_filename

def process_video(raw_url):
    video_id = extract_video_id(raw_url)
    if not video_id:
        send_message("Geçersiz YouTube linki.")
        return

    send_message("⚡ Ses indiriliyor...")
    try:
        segments = get_audio_and_transcribe(video_id)
        send_message("🤖 Gemini en etkili kesiti seçiyor...")
        
        best = find_best_segment(segments)
        start = float(best["start"])
        end = float(best["end"])
        reason = best.get("reason", "Öne çıkan kesit")
        
        send_message(f"✂️ Dikey Reels kırpılıyor ({int(end - start)} sn)...")
        output_file = download_and_crop(video_id, start, end)
        
        caption = f"🎬 Reels Hazır!\n\n⏱️ Süre: {int(end - start)} sn\n💡 Vurgu: {reason}"
        send_video(output_file, caption)
        
        if os.path.exists(output_file):
            os.remove(output_file)
    except Exception as e:
        send_message(f"❌ Hata: {str(e)}")

def main():
    print("Bot basariyla baslatildi ve dinliyor (Native yt-dlp Modu)...")
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                if "youtube.com" in text or "youtu.be" in text:
                    process_video(text)
                elif text == "/start":
                    send_message("Selamlar! YouTube video linkini gönder, Reels kesitini hazırlayayım.")
        except Exception as e:
            time.sleep(3)

if __name__ == "__main__":
    main()
