#!/bin/bash
# YouTube STT 서비스 설치 스크립트

echo "=== YouTube STT 서비스 설치 ==="

# Supervisor 설정 파일 복사
sudo cp /home/gihwaja/26.1.11.youtube.stt/youtube_stt.conf /etc/supervisor/conf.d/youtube_stt.conf

# Supervisor 재시작
sudo supervisorctl reread
sudo supervisorctl update

echo "=== 설치 완료 ==="
echo ""
echo "서비스 상태 확인:"
echo "  sudo supervisorctl status youtube-stt:*"
echo ""
echo "서비스 시작:"
echo "  sudo supervisorctl start youtube-stt:*"
echo ""
echo "서비스 중지:"
echo "  sudo supervisorctl stop youtube-stt:*"
echo ""
echo "로그 확인:"
echo "  sudo supervisorctl tail -f youtube-stt-celery"
echo "  sudo supervisorctl tail -f youtube-stt-flask"
echo ""
echo "웹 접속: http://100.67.151.71:9899"
