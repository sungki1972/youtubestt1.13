"""
YouTube STT Web Application
YouTube 링크를 입력받아 MP4 → MP3 → STT 텍스트 변환
"""
import os
import sys
import uuid
import shutil
import tempfile
import subprocess
import requests
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from openai import OpenAI
from pytubefix import YouTube

# 현재 디렉토리 설정
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# 환경 변수에서 설정 읽기 (Railway 배포용)
# Railway에서 환경 변수 설정 필요: SUPABASE_URL, SUPABASE_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
APP_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", os.environ.get("APP_URL", "localhost:9899"))
PORT = int(os.environ.get("PORT", 9899))

# 다운로드 디렉토리 (Railway에서는 /tmp 사용)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(current_dir, "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 미디어 파일 저장 디렉토리
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(current_dir, "media"))
os.makedirs(MEDIA_DIR, exist_ok=True)

# 업로드 허용 확장자
ALLOWED_EXTENSIONS = {'mp4', 'webm', 'mkv', 'avi', 'mov', 'm4a', 'mp3', 'wav'}

def allowed_file(filename):
    """허용된 파일 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Supabase 클라이언트 초기화
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# OpenAI 클라이언트 초기화
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Flask 앱 초기화
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'youtube-stt-secret-key')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB 제한


def send_telegram_message(message: str):
    """Telegram으로 메시지 전송"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Telegram 전송 실패: {e}")
        return None


def get_youtube_title(url: str) -> str:
    """pytubefix로 YouTube 제목 가져오기"""
    try:
        yt = YouTube(url)
        return yt.title
    except Exception as e:
        print(f"제목 가져오기 실패: {e}")
    return "제목 없음"


def download_youtube_audio(url: str, output_path: str) -> str:
    """pytubefix로 YouTube 오디오 다운로드"""
    yt = YouTube(url)
    audio_stream = yt.streams.filter(only_audio=True).order_by('abr').desc().first()
    if not audio_stream:
        raise Exception("오디오 스트림을 찾을 수 없습니다.")
    downloaded_file = audio_stream.download(output_path=os.path.dirname(output_path), filename=os.path.basename(output_path))
    return downloaded_file


def update_progress(record_id: str, progress: int, status: str = "processing"):
    """진행률 업데이트 헬퍼 함수"""
    try:
        supabase.table("youtube_stt").update({
            "status": status,
            "progress": progress
        }).eq("id", record_id).execute()
    except Exception as e:
        print(f"진행률 업데이트 실패: {e}")


def get_audio_duration(audio_path: str) -> float:
    """오디오 파일의 길이(초) 반환"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return 0


def split_audio_file(audio_path: str, max_duration: int = 600) -> list:
    """오디오 파일을 청크로 분할 (기본 10분 단위)"""
    duration = get_audio_duration(audio_path)
    if duration <= max_duration:
        return [audio_path]

    chunks = []
    chunk_dir = os.path.dirname(audio_path)
    base_name = os.path.splitext(os.path.basename(audio_path))[0]

    num_chunks = int(duration / max_duration) + 1

    for i in range(num_chunks):
        start_time = i * max_duration
        chunk_path = os.path.join(chunk_dir, f"{base_name}_chunk_{i}.mp3")

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-i', audio_path,
            '-ss', str(start_time),
            '-t', str(max_duration),
            '-acodec', 'libmp3lame', '-q:a', '4',
            chunk_path
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=300)
        chunks.append(chunk_path)

    return chunks


def transcribe_with_openai(audio_path: str, language: str = "ko") -> str:
    """OpenAI Whisper API로 STT 수행 (25MB 제한 대응)"""
    file_size = os.path.getsize(audio_path)
    max_size = 24 * 1024 * 1024  # 24MB

    if file_size <= max_size:
        with open(audio_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=language,
                response_format="text"
            )
        return transcript

    # 파일이 크면 청크로 나눠서 처리
    chunks = split_audio_file(audio_path)
    transcripts = []

    for chunk_path in chunks:
        chunk_size = os.path.getsize(chunk_path)
        if chunk_size > max_size:
            small_chunk = chunk_path.replace('.mp3', '_small.mp3')
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-i', chunk_path,
                '-acodec', 'libmp3lame', '-b:a', '64k',
                small_chunk
            ]
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=300)
            if chunk_path != audio_path:
                os.remove(chunk_path)
            chunk_path = small_chunk

        with open(chunk_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=language,
                response_format="text"
            )
        transcripts.append(transcript)

        if chunk_path != audio_path and os.path.exists(chunk_path):
            os.remove(chunk_path)

    return "\n\n".join(transcripts)


def process_youtube_stt_task(record_id: str, youtube_url: str):
    """백그라운드에서 YouTube STT 처리 (스레드)"""
    source_file = None
    mp3_path = None
    try:
        update_progress(record_id, 5)

        # 1. YouTube 제목 가져오기
        title = get_youtube_title(youtube_url)
        supabase.table("youtube_stt").update({
            "title": title,
            "progress": 10
        }).eq("id", record_id).execute()

        # 2. YouTube 다운로드
        update_progress(record_id, 15)
        audio_filename = f"{record_id}_audio"
        source_file = download_youtube_audio(youtube_url, os.path.join(DOWNLOAD_DIR, audio_filename))
        update_progress(record_id, 30)

        # 3. MP3 변환
        update_progress(record_id, 40)
        mp3_path = os.path.join(DOWNLOAD_DIR, f"{record_id}.mp3")
        ffmpeg_cmd = [
            'ffmpeg', '-y', '-i', source_file,
            '-vn', '-acodec', 'libmp3lame', '-q:a', '2',
            mp3_path
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=600)
        update_progress(record_id, 50)

        # 4. STT
        update_progress(record_id, 60)
        subtitle_text = transcribe_with_openai(mp3_path, language="ko")
        update_progress(record_id, 95)

        # 5. 저장
        supabase.table("youtube_stt").update({
            "subtitle": subtitle_text,
            "status": "completed",
            "progress": 100
        }).eq("id", record_id).execute()

        # 6. Telegram 알림
        protocol = "https" if "railway" in APP_URL.lower() else "http"
        detail_url = f"{protocol}://{APP_URL}/detail/{record_id}"
        telegram_message = f"""✅ <b>YouTube STT 완료</b>

<b>제목:</b> {title}
<b>링크:</b> {youtube_url}

<a href="{detail_url}">자막 보기</a>"""
        send_telegram_message(telegram_message)

        # 임시 파일 삭제
        for f in [source_file, mp3_path]:
            if f and os.path.exists(f):
                os.remove(f)

    except Exception as e:
        error_msg = str(e)
        supabase.table("youtube_stt").update({
            "status": f"error: {error_msg[:200]}",
            "progress": 0
        }).eq("id", record_id).execute()
        send_telegram_message(f"❌ STT 실패: {error_msg[:200]}")

        for f in [source_file, mp3_path]:
            if f and os.path.exists(f):
                os.remove(f)


def process_file_stt_task(record_id: str, file_path: str, original_filename: str):
    """백그라운드에서 파일 STT 처리 (스레드)"""
    mp3_path = None
    try:
        update_progress(record_id, 5)

        # 1. MP3 변환
        update_progress(record_id, 10)
        mp3_path = os.path.join(DOWNLOAD_DIR, f"{record_id}.mp3")

        if file_path.lower().endswith('.mp3'):
            shutil.copy(file_path, mp3_path)
        else:
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-i', file_path,
                '-vn', '-acodec', 'libmp3lame', '-q:a', '2',
                mp3_path
            ]
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=600)
        update_progress(record_id, 50)

        # 2. STT
        update_progress(record_id, 60)
        subtitle_text = transcribe_with_openai(mp3_path, language="ko")
        update_progress(record_id, 95)

        # 3. 저장
        supabase.table("youtube_stt").update({
            "subtitle": subtitle_text,
            "status": "completed",
            "progress": 100
        }).eq("id", record_id).execute()

        # 4. Telegram 알림
        protocol = "https" if "railway" in APP_URL.lower() else "http"
        detail_url = f"{protocol}://{APP_URL}/detail/{record_id}"
        telegram_message = f"""✅ <b>파일 STT 완료</b>

<b>파일명:</b> {original_filename}

<a href="{detail_url}">자막 보기</a>"""
        send_telegram_message(telegram_message)

        # 임시 파일 삭제
        for f in [file_path, mp3_path]:
            if f and os.path.exists(f):
                os.remove(f)

    except Exception as e:
        error_msg = str(e)
        supabase.table("youtube_stt").update({
            "status": f"error: {error_msg[:200]}",
            "progress": 0
        }).eq("id", record_id).execute()
        send_telegram_message(f"❌ 파일 STT 실패: {error_msg[:200]}")

        for f in [file_path, mp3_path]:
            if f and os.path.exists(f):
                os.remove(f)


@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')


@app.route('/health')
def health():
    """헬스 체크 (Railway용)"""
    return jsonify({"status": "healthy"}), 200


@app.route('/media/<filename>')
def serve_media(filename):
    """미디어 파일 서빙"""
    return send_from_directory(MEDIA_DIR, filename)


@app.route('/api/submit', methods=['POST'])
def submit_youtube():
    """YouTube 링크 제출"""
    try:
        data = request.get_json()
        youtube_url = data.get('youtube_url', '').strip()

        if not youtube_url:
            return jsonify({"success": False, "error": "YouTube 링크를 입력해주세요."}), 400

        # Supabase에 레코드 생성
        result = supabase.table("youtube_stt").insert({
            "youtube_link": youtube_url,
            "status": "pending"
        }).execute()

        record_id = result.data[0]['id']

        # 백그라운드 스레드로 처리
        thread = threading.Thread(target=process_youtube_stt_task, args=(record_id, youtube_url))
        thread.daemon = True
        thread.start()

        return jsonify({
            "success": True,
            "record_id": record_id,
            "message": "처리가 시작되었습니다."
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/upload', methods=['POST'])
def upload_file():
    """MP4/오디오 파일 업로드"""
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "파일이 없습니다."}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "파일이 선택되지 않았습니다."}), 400

        if not allowed_file(file.filename):
            return jsonify({"success": False, "error": "허용되지 않는 파일 형식입니다."}), 400

        original_filename = secure_filename(file.filename)
        record_id = str(uuid.uuid4())
        ext = original_filename.rsplit('.', 1)[1].lower()
        saved_filename = f"{record_id}.{ext}"

        # 미디어 폴더에 저장
        media_path = os.path.join(MEDIA_DIR, saved_filename)
        file.save(media_path)

        # 작업용 파일 복사
        file_path = os.path.join(DOWNLOAD_DIR, saved_filename)
        shutil.copy(media_path, file_path)

        # Supabase에 레코드 생성
        result = supabase.table("youtube_stt").insert({
            "id": record_id,
            "youtube_link": f"/media/{saved_filename}",
            "title": original_filename,
            "status": "pending"
        }).execute()

        # 백그라운드 스레드로 처리
        thread = threading.Thread(target=process_file_stt_task, args=(record_id, file_path, original_filename))
        thread.daemon = True
        thread.start()

        return jsonify({
            "success": True,
            "record_id": record_id,
            "message": "파일 처리가 시작되었습니다."
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/list')
def get_list():
    """목록 조회"""
    try:
        result = supabase.table("youtube_stt").select("*").order("created_at", desc=True).execute()
        return jsonify({"success": True, "data": result.data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/detail/<record_id>')
def get_detail(record_id):
    """상세 조회"""
    try:
        result = supabase.table("youtube_stt").select("*").eq("id", record_id).execute()
        if result.data:
            return jsonify({"success": True, "data": result.data[0]})
        return jsonify({"success": False, "error": "레코드를 찾을 수 없습니다."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/detail/<record_id>')
def detail_page(record_id):
    """상세 페이지 (Telegram 링크용)"""
    try:
        result = supabase.table("youtube_stt").select("*").eq("id", record_id).execute()
        if result.data:
            return render_template('detail.html', record=result.data[0])
        return "레코드를 찾을 수 없습니다.", 404
    except Exception as e:
        return str(e), 500


@app.route('/api/delete/<record_id>', methods=['DELETE'])
def delete_record(record_id):
    """레코드 삭제"""
    try:
        supabase.table("youtube_stt").delete().eq("id", record_id).execute()
        return jsonify({"success": True, "message": "삭제되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/update/<record_id>', methods=['PATCH'])
def update_record(record_id):
    """레코드 수정 (제목 등)"""
    try:
        data = request.get_json()
        title = data.get('title', '').strip()

        if not title:
            return jsonify({"success": False, "error": "제목을 입력해주세요."}), 400

        supabase.table("youtube_stt").update({
            "title": title
        }).eq("id", record_id).execute()

        return jsonify({"success": True, "message": "수정되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
