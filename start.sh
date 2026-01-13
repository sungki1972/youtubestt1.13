#!/bin/bash
set -e

# Railway가 PORT를 주입하지 않으면 기본값 사용
PORT=${PORT:-8080}

echo "Starting YouTube STT on port $PORT"
echo "Environment check:"
echo "  - SUPABASE_URL: ${SUPABASE_URL:+SET}"
echo "  - SUPABASE_KEY: ${SUPABASE_KEY:+SET}"
echo "  - OPENAI_API_KEY: ${OPENAI_API_KEY:+SET}"

exec gunicorn app:app \
    --bind "0.0.0.0:$PORT" \
    --workers 2 \
    --threads 4 \
    --timeout 300 \
    --access-logfile - \
    --error-logfile -
