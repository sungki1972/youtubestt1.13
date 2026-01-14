FROM python:3.11-slim

# 시스템 패키지 설치
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 작업 디렉토리
WORKDIR /app

# 의존성 먼저 설치 (v2 - 캐시 무효화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 복사
COPY . .

# 디렉토리 생성
RUN mkdir -p /app/downloads /app/media /tmp/downloads /tmp/media

# 시작 스크립트 복사 및 권한 설정
COPY start.sh /start.sh
RUN chmod +x /start.sh

# 실행
CMD ["/start.sh"]
